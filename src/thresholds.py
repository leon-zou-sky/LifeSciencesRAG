"""
判重阈值加载器 —— 运行时事实源是 MySQL threshold_config 表（BYOM，2026-08-20 下沉）

读路径：
    from src.thresholds import load_thresholds
    th = load_thresholds()
    th["inquiry"]["duplicate"]      # 问询判重上限
    th["inquiry"]["new"]            # 问询判重下限
    th["case_report"]["duplicate"]  # 病例并案上限（混合分）
    th["case_report"]["gray"]       # 病例并案下限
    th["generation"]["draft_min_top1"]  # 生成层资格闸
    th["tolerance"]                 # 回归漂移容差
    th["model"]                     # 本套阈值标定时的模型

单一事实源的迁移（设计文档 6.17）：
  - 2026-08-17 起：config/thresholds.yaml 单点（A.3-B）
  - 2026-08-20 起：MySQL threshold_config 为运行时事实源（BYOM：业务侧调参不走代码部署），
    yaml 降级为新环境种子（init_thresholds.py 引导入库）+ DB 不可用时的离线兜底
  - 改阈值一律走 scripts/set_threshold.py（强制 --by/--reason 留痕进 threshold_history），
    禁止直接 UPDATE 表（无留痕）或改 yaml（运行时不读）

模型路径同样单点化（换模型后全脚本必须同步切换，否则向量维度不匹配会写错 Collection）：
    from src.thresholds import load_model_path
    model = SentenceTransformer(load_model_path())
"""
import os
import sys
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "thresholds.yaml"
# 模型目录：默认 <上一级>/WeatherAgent/models（本地开发约定），
# 公开部署时用环境变量 LS_RAG_MODELS_DIR 指向任意模型仓（README「模型准备」节）
_MODELS_DIR = Path(os.environ["LS_RAG_MODELS_DIR"]) if os.environ.get("LS_RAG_MODELS_DIR") \
    else Path(__file__).resolve().parents[2] / "WeatherAgent" / "models"


def models_dir() -> Path:
    """模型仓目录（环境变量 LS_RAG_MODELS_DIR 可覆盖默认值）"""
    return _MODELS_DIR


def _load_from_yaml() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def _load_from_db() -> dict | None:
    """从 threshold_config 表读取。表不存在/DB 不可达/表为空 → None（交 yaml 兜底）"""
    from src.db import get_conn  # 延迟导入：纯本地计算场景不强依赖 DB
    try:
        conn = get_conn()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tkey, tvalue FROM threshold_config")
            rows = cur.fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    if not rows:
        return None
    flat = {}
    for r in rows:
        v = r["tvalue"]
        try:
            v = float(v)  # 阈值类键值都是数值；model 等非数值保持字符串
        except (TypeError, ValueError):
            pass
        flat[r["tkey"]] = v
    # 还原嵌套结构（inquiry.duplicate → th["inquiry"]["duplicate"]），与 yaml 形态一致
    th: dict = {}
    for k, v in flat.items():
        if "." in k:
            section, name = k.split(".", 1)
            th.setdefault(section, {})[name] = v
        else:
            th[k] = v
    return th


@lru_cache(maxsize=1)
def load_thresholds() -> dict:
    """DB 优先、yaml 兜底（进程内缓存一次）。换模型重标定/调阈值后重启进程生效。"""
    th = _load_from_db()
    if th is not None:
        return th
    print("⚠️ [thresholds] MySQL threshold_config 不可用，回退 config/thresholds.yaml。"
          "若近期用 set_threshold.py 调过参，当前读到的可能不是最新值！",
          file=sys.stderr)
    return _load_from_yaml()


def load_model_path() -> str:
    """现役模型路径 = 模型目录 + 阈值的 model 字段（与阈值同源，杜绝模型/阈值错配）"""
    path = _MODELS_DIR / load_thresholds()["model"]
    if not path.is_dir():
        raise FileNotFoundError(f"模型目录不存在: {path}（阈值 model 字段与实际模型文件不符）")
    return str(path)
