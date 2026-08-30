# 验证与测试手册：每个脚本防什么、在哪跑、什么时候跑

> 本文档是全部验证/测试脚本的目录与操作手册——回答三个问题：
> **它是干什么的（防什么问题）？在什么环境跑（依赖哪些服务）？什么时候该跑它？**
> 体系归属见 [compliance-system-design.md](compliance-system-design.md)（双体系总纲），
> 本文档是那套体系的"零件清单+使用说明"。

---

## 1. 术语速查

| 术语 | 含义 | 本仓库对应物 |
|---|---|---|
| **探针（probe）** | 一条预设输入+期望输出，用来戳系统的某个行为 | `config/*golden*.json`、`pii_probes.json`、`answer_golden.json` 里的每一条 |
| **冒烟测试（smoke test）** | 最小集快速验证"系统没散架"，断言行为分类不断言具体内容 | `test_answers.py`（断言五终态路由，不断言草稿文本） |
| **回归（regression）** | 变更后重跑全量探针，确认旧能力没被破坏 | `regression.py` |
| **标定（calibration）** | 用人工标注集给阈值找初值（≈ GxP 的 OQ） | `calibrate.py` |
| **终审** | 标定初值必须在真实业务探针上再过一遍（≈ PQ） | `regression.py` 接在 calibrate 后面跑 |
| **对账（reconcile）** | 运行态实际值 vs 锚定值逐键比对 | `reconcile_thresholds.py` |
| **蓝绿（blue-green）** | 新影子库先建好、验完再切流，旧库留观察期，不 drop-first | `rebuild_shadow.py` |
| **负对照（negative control）** | 故意注入坏值，验证闸门真的会拦（闸门有效性本身的测试） | 演练文档记录的两阶段实验 |
| **点火（kickstart）** | 手动触发一次 launchd 定时任务，验证"定时通道本身"能跑通 | `launchctl kickstart gui/$(id -u)/com.lsrag.daily-verify` |
| **五终态** | 生成层每个问题的五种合法去向 | standard_answer / insufficient_evidence / coverage_gap / draft_passed / draft_failed_human |

**统一运行方式**：`conda run -n py311 python scripts/xxx.py`
**统一退出码约定**：`0`=绿（全过） / `1`=红（有失败） / `2`=基础设施不可达（不是系统故障，是环境没起）

---

## 2. 总表：验证脚本一览

| 脚本 | 一句话作用 | 防什么问题 | 依赖环境 | 何时跑 |
|---|---|---|---|---|
| `regression.py` | 检索黄金集回归：10 探针四类断言（归一/档位/漂移≤0.03/版本） | 检索能力悄悄退化；旧版说明书抢 top1；阈值被改坏 | Milvus | 改阈值/换模型/入库后；每日定时 |
| `test_answers.py` | 生成层冒烟：6 探针断言五终态路由 | LLM 该拒答时硬答（幻觉发出）；契约改坏 | Milvus + Ollama | 改 prompt/生成层代码后；每日定时 |
| `test_cases.py` | 病例并案断言：14 对标定对逐条过生产打分路径 | 病例阈值漂移；三道硬否决被改坏 | Milvus | 改病例规则/阈值后；每日定时 |
| `test_pii.py` | PII 对抗探针：20 条（脱敏率+误报防线） | 隐私漏脱敏进 LLM；过度脱敏误伤药名 | 无（纯本地规则） | 改 pii_guard 后；每日定时 |
| `test_inquiries.py` | 问询库判重测试（三档分流+人工闸） | 判重路由错档 | Milvus | 改判重逻辑后 |
| `reconcile_thresholds.py` | 阈值对账：DB 实测值 vs 锚定值 7 键逐比 | **混合态**（部分键被改、回归看不出来——回归输出对阈值不敏感） | MySQL | 任何阈值变更后必跑；每日定时 |
| `calibrate.py` | 阈值标定：24 对标注集上算三条建议线 | 换模型后阈值拍脑袋 | Milvus | 换 embedding 模型时（标定定初值） |
| `measure_gate.py` | 资格闸灵敏度实测（10 分层探针，只检索不调 LLM） | 闸值拦不住"撞文体"的无关问题 | Milvus | 调资格闸阈值时 |
| `measure_case_gate.py` | 病例阈值实测（复刻生产打分路径） | 病例阈值脱离实际分数分布 | Milvus | 调病例阈值时 |
| `rebuild_shadow.py` | 蓝绿影子重建/切流 | 换模型时全库重建不中断服务、可回滚 | Milvus | 换 embedding 模型时 |
| `set_threshold.py` | 阈值变更（强制 --by/--reason 四要素留痕） | 阈值被无痕乱改，审计链断 | MySQL | 唯一合法的改阈值入口 |
| `init_thresholds.py` | 阈值种子（幂等，--reseed 强制覆盖） | 新环境 DB 无阈值 | MySQL | 环境初始化 |
| `import_calibration_pairs.py` | 客户标定对导入（CSV/JSON） | 客户数据进不了标定流程 | MySQL | 新客户接入 |
| `calibration_report.py` | 标定报告（Markdown+JSON 落 reports/） | 客户阈值建议无据可依 | MySQL | 客户标定时 |
| `check_coverage.py` | 覆盖率对账：问询/病例涉及的药品 vs 库中说明书 | 召回天花板——排序救不了库里没有的东西 | MySQL + Milvus | 定期/补库后 |
| `validate_citation.py` | 引用断言中间件（独立交付，任意 RAG 可用） | 别家 RAG 输出的幻觉引用 | 无（输入草稿+chunks） | 作为外挂中间件接 CI |
| `gen_validation_pack.py` | 验证包证据自动采集（环境/模型 SHA/实跑归档） | 验证包手工拼凑、证据不可复现 | Milvus + MySQL | 出验证包时 |
| `scheduled_verification.py` | 每日持续验证调度器（五套件+三态+留痕+通知） | **时间维度漂移**：没人动系统但系统悄悄变了 | MySQL + Milvus + Ollama | launchd 每日 07:23 自动；也可手动 |

---

## 3. 环境依赖矩阵

| 依赖 | 需要它的脚本 | 怎么起 |
|---|---|---|
| MySQL (:3307) | reconcile / set_threshold / init_thresholds / import_calibration_pairs / calibration_report / check_coverage / gen_validation_pack | `docker compose up -d`（项目根目录） |
| Milvus (:19531) | regression / test_answers / test_cases / test_inquiries / calibrate / measure_* / rebuild_shadow / check_coverage | 同上（⚠️ demo 用 :19531，与 WeatherAgent :19530 严格隔离） |
| Ollama (:11434) | test_answers / answer.py | 打开 Ollama.app（或常驻终端 `ollama serve`） |
| 无外部依赖 | test_pii / validate_citation / scheduled_verification 自身 | 直接跑 |

**环境没起的后果**：`scheduled_verification.py` 前置检查会把依赖不可达的套件记 **skipped → 总结论 incomplete（exit 2）**——不是红，但也不是绿。全绿报告必须意味着每一项真的跑过。

---

## 4. 场景操作手册：什么情况跑什么

### 4.1 改了 prompt 或生成层代码（llm.py / citation_check.py / answer.py）

```bash
conda run -n py311 python scripts/test_answers.py        # 6 探针五终态全绿才算完
```
注意：改 SYSTEM_PROMPT 必须递增 PROMPT_VERSION（留痕进 trace，历史答复可复现）。

### 4.2 改了阈值（只能走 set_threshold.py，禁止直接改 DB）

```bash
conda run -n py311 python scripts/set_threshold.py --key inquiry.duplicate --value 0.85 --by 姓名 --reason "真实理由"
conda run -n py311 python scripts/regression.py          # 终审
conda run -n py311 python scripts/reconcile_thresholds.py # 对账收口——对账是收工条件
```
runbook 三纪律：理由栏禁预填 / 对账是收工条件 / 回归绿≠采纳推荐值（决策表裁判）。

### 4.3 换 embedding 模型（重标定四级验证门禁）

```bash
conda run -n py311 python scripts/rebuild_shadow.py --model bge-xxx   # ① 蓝绿建影子
conda run -n py311 python scripts/calibrate.py                        # ② 标定定初值
#   人工定线（set_threshold.py 落阈值）
conda run -n py311 python scripts/regression.py                       # ③ 独立终审
#   切流（别名原子切换）→ 观察期 → regression --rebaseline 重录基线     # ④ 蓝绿切流
```
详见 [model-selection-recalibration.md](model-selection-recalibration.md) 附录 A runbook。

### 4.4 每天早上 07:23 自动发生什么

launchd 点火 `scheduled_verification.py` → 基础设施前置检查 → 五套件
（reconcile → regression → cases → pii → answers）依次跑 → 三态结论：
- **green**：什么都不用做；
- **red**：桌面通知 → 看 `reports/scheduled/<时间戳>.log` 定位失败套件；
- **incomplete**：桌面通知 → 把 Docker 容器和 Ollama 起起来，不算系统故障。

手动触发一次（点火测试）：`launchctl kickstart gui/$(id -u)/com.lsrag.daily-verify`
历史记录：`reports/scheduled/verify_runs.jsonl`（每条可回放）。

### 4.5 新客户接入（客户标定流程）

```bash
conda run -n py311 python scripts/import_calibration_pairs.py --csv 客户标注.csv
conda run -n py311 python scripts/calibration_report.py          # 出建议阈值
conda run -n py311 python scripts/set_threshold.py ...           # 应用（留痕）
conda run -n py311 python scripts/regression.py                  # 终审
conda run -n py311 python scripts/reconcile_thresholds.py        # 对账
```
全流程演练记录（含负对照实验）：[drill-customer-calibration-2026-08-25.md](drill-customer-calibration-2026-08-25.md)。

### 4.6 出验证包（GxP 交付）

```bash
conda run -n py311 python scripts/gen_validation_pack.py         # 证据自动采集 → docs/validation/packs/
# 模板签字页由人签署——机器只采证据，不出批准结论
```

---

## 5. 设计原则（为什么长这样）

1. **exit code 是唯一契约**——调度器/CI 只看退出码，不解析输出文本（文本会变，语义会漂移）；
2. **探针只增不改**——检验规程受控，改一条历史结论就无法复现；
3. **skipped ≠ green**——环境没起不是"通过"，incomplete 必须显式可见；
4. **机器只报告不处置**——红了通知人，不自动回滚/重跑/修阈值；自动处置动作永远是人发起；
5. **断言行为分类，不断言具体内容**——LLM 输出不可 diff，断言五终态路由而非草稿文本；
6. **每个脚本都能单独手动跑**——定时调度只是承载，手动与定时行为完全一致（可复现）。

---

## 6. 迭代记录

| 日期 | 变更 |
|---|---|
| 2026-08-30 | 初版：18 个验证相关脚本全量编目（总表+环境矩阵+场景手册），配合持续验证层落地 |
