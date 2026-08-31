# 生命科学 RAG PoC

基于 Milvus + MySQL + Neo4j + BGE 的知识检索系统，模拟 Veeva Vault / Safety 场景。

**MySQL 是源，Milvus 是影子**：文档/问询全文存 MySQL，`embedding_updated_at IS NULL` 为待同步标记，同步幂等可重跑。影子只放 `status='现行'` 的内容——作废/已合并/草稿留 MySQL 审计，不进检索。

## 文档类型与权威性（A 类文档：curated，走版本管理）

| Collection | 文档类型 | 权威级别 | 检索方式 |
|---|---|---|---|
| drug_inserts | 药品说明书（含双版本改版演练） | A（最高） | 关键词精确 |
| guidelines | 诊疗指南/共识（单条推荐意见拆块） | A-（准权威） | 向量语义 |
| clinical_papers | 临床论文 | B（参考） | 向量语义 |
| case_reports | 病例分析/不良事件 | C（经验） | 混合检索 |

版本管理：作废版本留 MySQL 审计、影子按 id 精准摘除；回归含**版本断言**防过期口径泄漏。

## 事件流数据（B 类：判重 + 人工确认）

| Collection | 数据 | 判重逻辑 |
|---|---|---|
| mi_inquiries | 医学问询（热线/微信/代表转达） | ≥0.85 自动判重(计数+1) / 0.70~0.85 人工确认 / <0.70 新问题(draft) |
| case_reports | 多渠道病例上报（GVP 场景） | 向量粗筛 + 四要素规则精排（药品50%/时间窗30%/患者20%），hybrid ≥0.87 并案 / 0.78~0.87 人工闸 / <0.78 新病例 |

问询入库前过**归一层**（`src/normalization.py`）：商品名/俗名/错别字 → 通用名
（立普妥→阿托伐他汀、格华止→二甲双胍、可达龙→胺碘酮……），归一发生在 embedding 之前。
`status=draft` 的问询不进检索也不进判重基准池（`status=='approved'` 过滤）。

## 快速开始

```bash
# 1. 起全部依赖（Milvus :19531 / Neo4j :7687 / MySQL :3308）
docker compose up -d

# 2. 数据采集（模拟真实多格式接入：PDF/CSV/TXT/XML → MySQL → Milvus）
python scripts/make_inbox_samples.py   # 生成 inbox 样品文件；--simvastatin 只补辛伐他汀
python scripts/ingest.py               # 扫描 data/inbox 全流程，处理完归档 processed/

# 3. 文档类数据：MySQL 灌库 + 同步 Milvus（幂等）
python scripts/migrate_to_mysql.py            # 全量；--seed-only / --sync-only

# 4. 各专题库初始化（幂等）
python scripts/init_inquiries.py              # ① 问询底库（--rebuild 重建）
python scripts/init_guidelines.py             # ② 指南库（含作废版本管理）
python scripts/init_cases.py                  # ③ 多渠道病例并案（混合判重）
python scripts/init_drug_versions.py --phase chaos   # ④ 双版本：制造旧版未作废混乱态
python scripts/init_drug_versions.py --phase retire  # ④ 改版生效：旧版作废+影子摘除

# 5. 查询 & 问答（--role 角色画像：hotline/medical/pv，默认 general）
python scripts/query.py "阿托伐他汀和红霉素联用需要注意什么" --role hotline
python scripts/ask.py "阿托伐他汀和红霉素可以联用吗"    # 图谱+向量混合，带引用

# 6. 覆盖率对账（药品缺口 → 补库工单）
python scripts/check_coverage.py

# 7. 药物警戒文献监测（LLM 抽取 → 图谱去重 → 信号分级）
python scripts/pv_monitor.py                  # --dry-run 只看不写
```

## 验证（入库 ≠ 生效，回归过了才算）

```bash
python scripts/regression.py       # 黄金集回归门禁：归一/档位/漂移/版本四类断言，失败 exit 1
python scripts/test_inquiries.py   # 问询库演示版测试：归一/判重三档/权限过滤/幂等
python scripts/calibrate.py        # 判重阈值标定：24 对标注集 → 两簇分布 + 三条建议线
python scripts/rebuild_shadow.py --model-path <模型> --tag <标识>   # 换模型蓝绿影子重建（--cutover 切流）
```

黄金集探针配置：`config/golden_set.json`（探针只增不改，badcase 修复即固化）。阈值运行时事实源为 MySQL `threshold_config` 表（变更走 `scripts/set_threshold.py`，强制 `--by`/`--reason` 留痕进审计表）；`config/thresholds.yaml` 为新环境种子 + 离线兜底。

## 模型准备

Embedding 模型为 BGE（现役 bge-small-zh-v1.5），从 HuggingFace 下载到本地模型仓后，用环境变量指向：

```bash
# 默认模型目录见 src/thresholds.py（本地开发约定），用环境变量覆盖为任意模型仓
export LS_RAG_MODELS_DIR=/path/to/models   # 目录下应有 bge-small-zh-v1.5/
```

LLM 起草层用本地 Ollama（qwen3:4b）或火山引擎 Ark 接口（`.env` 配 `ARK_API_KEY` / `ARK_MODEL_ENDPOINT`，见 `src/llm_config.py`）。

## 文档地图

| 类型 | 文档 | 内容 |
|---|---|---|
| 方案概述 | [docs/solution-overview.md](docs/solution-overview.md) | **全仓库导览**：定位/架构/实测指标/核心能力/设计细节 |
| 体系设计 | [docs/compliance-system-design.md](docs/compliance-system-design.md) | **双体系总纲**：业务系统 × 验证合规体系分家设计——组成清单/变更控制/治理铁律/缺口台账 |
| 概述（英文） | [docs/design-overview-en.md](docs/design-overview-en.md) | **Design overview (English, condensed)** |
| 详细设计 | [docs/vector-db-construction-design.md](docs/vector-db-construction-design.md) | 构建设计 + 第 6 章实测驱动的设计迭代（18 条） |
| 调研报告 | [docs/rag-industry-research.md](docs/rag-industry-research.md) | 设计依据调研：Veeva 美国/中国、医疗 RAG 同行、中美合规差异、四级验证门禁谱系（2.7） |
| 专项调研 | [docs/pv-duplicate-detection-threshold-research.md](docs/pv-duplicate-detection-threshold-research.md) | PV 判重阈值/权重的统计学标定（vigiMatch 等五代方案谱系） |
| 评估方案 | [docs/model-selection-recalibration.md](docs/model-selection-recalibration.md) | Embedding 模型选型与重构标定：换模型场景/两阶段重标定/国内方案对比/退出标准 + **附录 C 三模型实测（蓝绿重标定已全程验证）** |
| 演练报告 | [docs/drill-customer-calibration-2026-08-25.md](docs/drill-customer-calibration-2026-08-25.md) | 客户标定全流程演练 + 负对照实验：故意注入坏阈值验证闸门拦截行为（闸灵敏度图谱） |
| 工程规范 | [docs/rag-engineering-standards.md](docs/rag-engineering-standards.md) | 代码工程规范 17 条（团队范本，每条带事故溯源） |
| 验证手册 | [docs/verification-playbook.md](docs/verification-playbook.md) | 18 个验证脚本编目：各自防什么问题/运行环境依赖/什么场景跑哪个/退出码契约 |
| 验证包 | [docs/validation/](docs/validation/README.md) | **GxP 验证包**：RTM 可追溯矩阵 + IQ/OQ/PQ 模板 + 签字页 + gen_validation_pack.py 证据自动采集 |
