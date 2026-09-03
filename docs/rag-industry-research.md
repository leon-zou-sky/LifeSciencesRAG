# RAG 设计依据调研（生命科学方向）

> 本 PoC 的设计思路借鉴了哪些公开方案、Veeva 美国团队怎么做、医疗 RAG 同行怎么做。
> 纯调研记录，供设计决策溯源。

## 1. 设计决策的出处分层

| 决策 | 依据 | 出处 |
|---|---|---|
| chunk 加【药名】【章节】前缀 | **Anthropic Contextual Retrieval**：给每块补上下文再编码，检索失败率降 49%（5.7%→2.9%）；混合 embedding+BM25 | [Anthropic Contextual RAG 解读](https://www.analyticsvidhya.com/blog/2024/11/anthropics-contextual-rag/)（我们做了前缀版，没做 LLM 生成上下文版） |
| 医学域需要专用语料+基准测试 | **MedRAG / MIRAGE**：7663 题医学 RAG 基准，PubMed 语料让 LLM 准确率最高 +18% | [arXiv:2402.13178](https://arxiv.org/abs/2402.13178)（黄金集是其工程简化版） |
| 病例/事件要判重 | **GVP Module VI 重复病例管理附录**——欧盟法规明确要求 PV 系统做重复报告管理 | [GVP VI Addendum I](https://pharmatimesofficial.com/project/guideline-on-good-pharmacovigilance-practices-gvp-module-vi-addendum-i-duplicate-management-of-suspected-adverse-reaction-reports/)（三档判重是它的实现） |
| 向量判重三档阈值 0.93/0.85 | **本项目实测**：BGE 分数压缩区间 0.83~0.92 内标定 | 无外部出处，换模型必须重标定 |
| 权威权重 A=1.0/B=0.95/C=0.9 | **调参拍的**，原则是"同等相关时高权威优先，相关性碾压时低权威能翻身" | 无出处，需业务验证 |
| 归一先于编码 | 工程共识 + 实测踩坑（子串误替换） | 词表映射是 NLP 标准做法 |
| MySQL 源 / Milvus 影子 | 工程共识（source of truth / 读写分离） | 类比 CDC、搜索引擎索引重建 |

## 2. Veeva 美国团队的 RAG/AI 思路

**架构路线**："平台内嵌 Agent + 客户可自带模型"，不是做一个通用 RAG 中台：

1. **LLM 无关（LLM-agnostic）**：默认 Claude（AWS Bedrock）/ Azure AI Foundry，客户可自带模型；核心卖点是**数据不出 Vault 安全边界**——权限、审计跟踪原生继承（[IntuitionLabs: Veeva AI Agents](https://intuitionlabs.ai/pdfs/veeva-ai-agents-agentic-ai-for-the-life-sciences-industry.pdf)）
2. **按应用场景出 Agent，不出通用问答**：2025/12 第一波 CRM（Free Text/Voice/Pre-call Agent）+ PromoMats（Quick Check/Content Agent）；2026/04 Safety+Quality；2026/08 Clinical/Regulatory/Medical；2026/12 Clinical Data（[IntuitionLabs: Veeva AI Roadmap](https://intuitionlabs.ai/articles/veeva-ai-roadmap-crm-bot-agents-2026)）
3. **数据通路**：Vault Direct Data API（2024 推出，2025/2 起免费，号称 100× 取数速度）——把 Vault 数据投影到外部检索管道的官方通路（[IntuitionLabs: Vault LLM Integration](https://intuitionlabs.ai/articles/veeva-vault-llm-rag-direct-data-api)）
4. **早期客户**：Roche、BMS、Moderna、Novo Nordisk

**和本 PoC 思路的对照**：

| 维度 | Veeva 美国团队 | 本 PoC |
|---|---|---|
| 模型 | Claude/Bedrock，客户可换 | Ark/豆包（国内等效位） |
| 数据边界 | 不出 Vault，权限审计原生继承 | MySQL 源层 + draft/approved 状态机 |
| 落地形态 | 场景化 Agent（QA 检查、MLR 审查） | 场景化：问询助手/文献监测/判重 |
| 知识供给 | Direct Data API 投影出来做检索 | 多格式采集 → 源层 → 影子同步 |

**对中国的关键推论**：Claude/Bedrock 在国内不可用，Veeva 中国团队必须换国产模型（豆包/DeepSeek/Qwen）重做这套 Agent——所有实测标定的东西（判重阈值）要重新标定，且国内数据更乱（商品名/口语/错别字），归一层要比美国版厚得多。

## 2.5 中美合规差异与多租户可审计合规（2026-08 补充）

**Vault 平台级合规底座（中美共用）**：审计追踪 append-only（who/when/what/why，对标 21 CFR Part 11 / EU Annex 11）、对象级权限模型（AI Agent 继承权限）、CSV 计算机化系统验证（IQ/OQ/PQ）、多租户统一发版。

**中美差异**：

| 维度 | 美国 | 中国 | 根源 |
|---|---|---|---|
| 模型层 | Claude/Bedrock | 不可用，换国产模型 | 云服务准入+数据出境 |
| 数据驻留 | 全球多区域 AWS/Azure | 数据不出境（PIPL/数据安全法/等保2.0），健康医疗数据境内存储 | PIPL、《数据安全法》 |
| 部署形态 | 全球多租户共享 stack | 中国区独立部署（推断，参考 Salesforce 中国模式） | 数据本地化+云牌照 |
| 法规锚点 | FDA/EMA 体系 | NMPA《药品记录与数据管理要求》（对标 ALCOA+）、中国 GVP（2021）、人类遗传资源管理条例 | 监管体系不同 |
| 标定参数 | 一套阈值 | 换模型必须全库重标 | 模型不同 |

**推论**：合规架构可平移（状态机/审计/版本作废照搬），标定参数不可平移（阈值/黄金集基线/归一词表全部重测）。

**多租户 SaaS 可审计合规 = 四件事，本 PoC 各有最小实现**：

| 合规要求 | 生产做法 | 本 PoC 雏形 |
|---|---|---|
| 租户隔离 | tenant_id 行级隔离 / Milvus partition_key | 设计预留（共享底库+租户私有层） |
| 审计留痕 | append-only audit trail | 源层状态机：作废/已合并不删只标 |
| 权限到检索层 | RBAC 继承查询过滤 | 角色画像视图（hotline/medical/pv） |
| 持续验证 | 版本升级附验证包 | 黄金集回归（含版本断言），"入库≠生效" |
| AI 决策可审 | human-in-the-loop | draft/approved 两道人工闸 |

## 2.6 生产级检索链路增强方向（2026-08 补充）

> 三条都是"PoC 不做、生产必做"的清单项，与设计文档 2.4 呼应。每条附方案说明与常见误解澄清。

> **选型总纲（2026-08-18 补）**：检索栈每层的启用都应有触发条件，不是栈越高越好——
> **规则定位（状态机/字典/阈值线）> 单向量检索 > 向量+Reranker > 多轮检索/agent 循环**。
> 确定性机制能框死的绝不动用概率性机制；每往右一层，延迟、验证负担、不可解释性都涨一截。
> 本 PoC 的护栏（6.14）、资格闸（6.13）、归一层、status 硬过滤都在最左端；
> Reranker 的合理战场是长文档语义稀释（如大篇幅论文粗排分数被全文平均）且精排断言开始失败的场景——
> 且第一刀是切块策略，Reranker 是第二刀。

### ① Reranker 精排（治双塔分数压缩）

**正确链路**：多路召回（双塔粗排）→ **Reranker 精排相关性**（BGE-Reranker / Cohere Rerank，cross-encoder 对 query+doc 联合编码，只跑 top-20 因为贵）→ 权威×角色业务加权 → 输出。

**Reranker 解决什么问题**：双塔模型（bi-encoder）query 和文档分开编码，分数压缩在窄带（本 PoC 实测 BGE 中文 0.83~0.92，见设计文档 6.2），高分段 0.952 vs 0.951 是噪声。Cross-encoder 联合编码区分度远高于双塔，专门修这个。

**两个常见误解（需要澄清）**：
- ❌ "Reranker 能学到说明书>论文的权威偏好"——cross-encoder 只打相关性分，没有权威概念。修权威错排的是权威加权层（本 PoC 已实现），Reranker 修的是相关性错排，两层各管一维
- ❌ "说明书没有 off-label 内容时 Reranker 能把说明书顶上去"——内容不在说明书里，Reranker 只会把它排更低。挡 off-label 的是权威加权 + A 级优先，不是 Reranker

**方案说明**：本 PoC 采用权威×角色加权融合排序，生产形态在加权前加一层 Reranker——动机来自实测：双塔模型在 BGE 高分段（0.83~0.92）分数压缩，top3 之间拉不开（设计文档 6.2）。Cross-encoder 对 query+doc 联合编码，区分度天然高一个量级，只对 top-20 运行故成本可控。权威性与相关性是两个正交维度：Reranker 管相关性，业务加权管合规偏好，两层串联。

### ② Embedding 模型选型：BGE-large-zh vs BGE-M3（中美双语场景）

| 维度 | BGE-large-zh（本 PoC） | BGE-M3 |
|---|---|---|
| 上下文 | 512 tokens（超长截断） | 8192 tokens |
| 语言 | 中文优化 | 100+ 语言，中英混合 |
| 输出 | 单向量（dense） | dense + sparse + multi-vector 一体 |
| 适用 | 纯中文场景 | 进口药英文说明书、中美数据对齐 |

**对 Veeva 中国团队的推论**：进口药品说明书/文献是中英混合的，BGE-M3 是明显更优解；且它的 sparse 输出直接喂 Milvus 混合检索（见③），不用单独维护 ES/BM25。

**一个别踩的坑**：换 M3 后"长文档不用二次切分"是错的——按法定章节拆块的目的不是绕过 512 限制，是**检索粒度 + 引用精度**（回答能点到"相互作用章"）。M3 解决截断压力，拆块策略照留。

**与 M5 联动**：换模型 = 全库重建 + 阈值重标定（设计文档 6.9 蓝绿路径），模型评估本身也走黄金集——M3 vs large-zh 各跑一遍回归，用数据说话。完整的换模型场景清单、两阶段重标定流程、国内方案对比与退出标准，见 [model-selection-recalibration.md](model-selection-recalibration.md)。

### ③ Milvus 标量过滤 × 混合检索

**标量过滤本 PoC 已实现，值得讲的是"两个过滤位的取舍"**：

| 过滤位 | 本 PoC 实例 | 适用 | 特点 |
|---|---|---|---|
| 同步时过滤 | sync SQL `status='现行'` | 作废/已合并内容 | **物理不进影子**，防线前移，合规最严 |
| 查询时过滤 | `filter='status=="approved"'`（问询库） | 角色/租户等动态条件 | 同一影子多视图，灵活但靠每次查询自觉 |

生产延伸：多租户隔离用 Milvus `partition_key`（接 2.5 节租户隔离行），比 filter 表达式更强的物理隔离。

**混合检索（dense+sparse）是真缺口**：纯向量对罕见精确实体天然弱——药品编号、CAS 号、病例号（CASE-2024-102）、版本号，关键词一打一个准但向量分不出。Milvus 2.5 原生支持多向量混合检索（RRFRanker/WeightedRanker 融合），配 BGE-M3 的 sparse 输出一个模型搞定。Self-MedRAG（第 3 节）的 BM25+向量混合就是这个思路的学术版。**注意**：RRF 在这里的工位是"通道融合"（治尺度不可比），不是顶层排序——顶层排序仍是权威×角色业务加权，两者的完整对比与串联关系见设计文档 6.11。

**方案说明**：过滤分两层实施：同步时硬过滤保证作废内容物理不进影子（合规防线）；查询时标量过滤支持动态视图（问询库即以此实现 approved 可见性）。生产形态补混合检索：纯向量对病例号、CAS 号等精确实体不敏感，dense+sparse 融合是标准解法，Milvus 2.5 原生支持。

## 2.7 重标定四级验证门禁：方法论谱系与国际实践（2026-08 补充）

本 PoC 的重标定部署流程（附录 A runbook：标定 → 人工定线 → 独立终审 → 切流）设置了**四级验证门禁**——新构建的向量集与重标定配置必须逐级通过才能接触查询流量。四级门禁不是单一框架的产物，而是四套成熟体系在 AI 场景的组装；经得起审计的原因不是新颖，是每一级都能在受监管行业找到原型。

### 四级门禁的理论出处

| 门禁 | 理论来源 | 原体系名称 |
|---|---|---|
| ① 标定（模型资格判定，calibrate.py） | 制药设备验证 V 模型（IQ/OQ/PQ） | OQ 运行确认："设备在标称范围内运转正确吗" |
| ② 独立终审（端到端断言，regression.py） | 同上 + GAMP 5 | PQ 性能确认："用我的真实物料，产出合格吗" |
| ③ 配置三方一致性（golden_set ↔ thresholds.yaml ↔ 模型目录） | GAMP 5 配置管理 + ALCOA+ Attributable | 配置基线可归因、可重建 |
| ④ 蓝绿切流（别名原子切换 + 源层可重建） | 软件工程蓝绿部署（Fowler 归纳） | Blue-Green Deployment |
| 贯穿原则：标定/终审分离（两套数据、两个入口） | 美联储 SR 11-7 模型风险管理（2011） | Independent Validation / Effective Challenge（有效质疑） |

两个关键原则：

- **OQ/PQ 分层**：标定（标准标注集上量分布）≈ OQ；终审（真实业务探针端到端）≈ PQ。GxP 审计员审制药设备就是这两段论，这套语言他们天然认。
- **有效质疑（SR 11-7）**：模型开发者与验证者必须分离，验证用独立数据和方法"挑战"而非复述。本 PoC 标定用标注对、终审用黄金集探针正是此原则——2026-08-17 演练中阈值从 0.80/0.60 修正为 0.85/0.70，就是终审对初值的一次有效质疑（详见模型文档附录 C.3）；若标定终审同一套数据，该问题不会暴露。

### 行业参照系

| 体系 | 做法 | 与本 PoC 对应 |
|---|---|---|
| Google ML Test Score（Breck et al. 2017） | ML 系统上线前按数据/模型/基础设施/监控四类打分 | 四类断言 ≈ 模型+数据测试项 |
| 银行 MRM 团队（SR 11-7 落地） | 独立模型验证部门，无验证报告不得上线 | 人工定线与独立终审的角色分离 |
| Champion-Challenger（风控/推荐通行做法） | 现役与挑战者并行，挑战者线下达标才替换 | 现役 Collection 与影子并存期 |
| FDA CSA 指南（2022） | 风险驱动验证：高风险深测，低风险复用厂商测试 | 断言按业务风险设计（版本断言防过期口径），非形式主义覆盖率 |

### Veeva 美国团队的验证模式（公开资料）

- **固定节奏发布 + 预置验证包**：Vault 每年 3 个大版本，厂商随版本提供现成验证文档（IQ/OQ 由厂商完成并交付），客户只做影响评估 + 风险驱动的 PQ 回归——GAMP 5/CSA 的"复用厂商验证"原则的商业化（[Veeva Vault QMS Validation Guide](https://govalidation.com/knowledge/qms/veeva-vault-qms-validation-guide/)、[24R3 Release Notes](https://rn.veevavault.help/en/gr/about-the-24r3-release/)）
- **风险分级验证**：客户 QA 按功能对患者安全的影响分级投入验证深度（[Vault CRM 风险驱动验证案例](https://www.therxcloud.com/case-study-risk-based-validation-for-veeva-vault-crm/)）
- **对本 PoC 的映射**："厂商 OQ + 客户 PQ"的分工恰好对应门禁①②的分离——平台标定替代不了客户真实数据上的终审。BYOM 场景"模型归客户、验证标准归平台"即此分工的延伸。Veeva 的 SaaS 验证经济学本质：客户购买的不只是软件，是"每次升级附带现成验证证据"。

### 诚实边界

- 四级门禁是上述**公开方法论在 PoC 尺度的裁剪组装**，非对 Veeva 内部实现的复刻；其内部模型治理细节外界不可见
- 验证文档形态已建立：docs/validation/（RTM 可追溯矩阵 + IQ/OQ/PQ 受控模板 + 签字页 + gen_validation_pack.py 证据自动采集）。与正式 CSV 验证包的剩余差距：电子签名（Part 11）未实现、模板未经真实 QA 评审流程固化——PoC 尺度下为演示形态

## 2.8 Veeva Vault Safety 病例处理流程与本 PoC 对照（2026-08-19 补充）

> 2.7 回答"验证方法论从哪来"，本节回答"工业级产品的业务流程长什么样"——
> 依据 Veeva 官方帮助文档（一手资料），把 Vault Safety 病例处理全流程与本 PoC 逐环对照。
> 结论先行：**本 PoC 每个环节都有工业级对应物，设计哲学同构；差距在法规引擎与界面形态，不在流程逻辑**。

### Vault Safety 病例处理全流程

```
① Intake（收件）
   多渠道进件：邮件/传真/文献/呼叫中心记录 → 生成 Inbox Item
   Safety.AI 的 Case Intake Agent 用 NLP 把非结构化文本抽成结构化字段

② Triage（分诊）
   评估首要不良事件的严重性（seriousness）→ 决定病例优先级和法规时限
   （严重 15 天 / 非严重 30 天上报死线，自动计算 due date）

③ 晋级为 Case（Promotion）——判重在这里发生
   Inbox Item 晋升为正式 Case 时，系统自动跑 duplicate detection，
   弹出 Potential Matches 页面，人逐条比对（Case Version Compare 高亮差异）
   后决定：新病例 / 并入既有病例 / 标记 Invalid

④ Data Entry & Coding
   补全四要素，MedDRA 编码不良事件、WHODrug 编码药品（AI 给建议，人确认）

⑤ Medical Review
   医学审评：叙述生成（Narrative Agent 自动起草）、因果关系评估、
   预期性/严重性确认

⑥ QC → 上报
   质检后经 E2B(R3) 网关直报 FDA / EudraVigilance 等监管机构

⑦ 生命周期终态
   Closed / Superseded（被新版取代）/ Voided / Nullified / Invalid
```

（来源：[Case Intake Overview](https://safety.veevavault.help/en/gr/01139)、[Duplicate Detection for Cases](https://safety.veevavault.help/en/gr/760310/)、[Case Processing Overview](https://safety.veevavault.help/en/lr/01171)）

### 逐环对照表

| Veeva 工业级 | 本 PoC | 差距本质 |
|---|---|---|
| Inbox Item（待定性缓冲） | draft 状态（人工闸前缓冲） | 形态一致 |
| **晋级时自动判重 + 人工比对决定** | **三档判重 + 灰色带人工闸 + 药品集合护栏（6.14）** | 逻辑同构；他们规则+相似度，我们多向量层 |
| Potential Matches 页面逐条比对 | trace 留痕 + 人工复核（界面未做） | 差 UI 形态 |
| MedDRA / WHODrug 标准编码 | 药品别名表（手工字典） | 他们接国际标准词典；我们的图谱/词表演进路径（调研 2.6 讨论） |
| seriousness → 15/30 天死线自动计算 | 无 | 法规引擎，PoC 不需要 |
| E2B(R3) 直报监管 | 无 | 监管报送通道（国内对应 CDR-ADR 系统） |
| Narrative Agent 起草叙述 | LLM grounded 起草 + 引用校验（6.13） | 同一代技术，同一层人工复核 |
| Invalid 终态（重复不删，转态留档） | 已合并/作废状态机 | 一致 |

### 三个设计哲学重合点（本 PoC 的工业验证）

1. **判重触发点在"晋级"不在"入库"**：Veeva 同样先宽容进件（Inbox Item 随便进），在变成正式 Case 的一刻才判重+人工确认——与本 PoC"draft 全收、approved 要过闸"完全同构。**宽容入口、严格晋级**是行业标准姿势
2. **AI 全在辅助位**：Intake Agent 抽字段、Narrative Agent 起草、编码给建议——每个 AI 动作后都站着人确认，没有全自动档。本 PoC"辅助模式 + 草稿后缀 + 引用校验"即此原则的迷你实现
3. **判重结果终态化留存**：判为重复的病例转 Invalid 留档不删——与本 PoC"已合并留痕不进影子"一致（留存优先于整洁）

### 结论

本 PoC 本质上是把 Vault Safety 的"判重-晋级"段与 Vault Medical 的"判重-答复"段，用向量+LLM 技术栈重实现的可验证迷你版。流程逻辑层面无实质差距；待补项（MedDRA/WHODrug 词典、法规时限引擎、E2B 报送、比对 UI）均为**生产化阶段的接入工作**，不影响架构判断的成立。

## 2.9 AI 输出验证与护栏：ISPE 五层对照 + 责任分界 + 国内格局（2026-08-20 补充）

> 与 2.8 的关系：2.8 调研的是**业务流程层**（病例怎么处理），本节调研的是 **AI 输出验证层**（生成的内容怎么管）。
> 两次调研结论不同但不矛盾：Veeva 在平台层做合规治理，把"AI 说得对不对"的验证责任明确推给客户侧——
> 本 PoC 的生成层设计填的正是这个空白。

### Veeva AI 的责任分界（2025-12 AI Agents 发布后）

Veeva AI Agents（CRM Bot/Voice Agent 等，Safety/Quality Agent 排期 2026-04）的策略是**平台层治理**：

| Veeva 做的（平台层） | Veeva 明确让客户自己做的 |
|---|---|
| AI 跑在已验证的 Vault 平台内 | AI 工作流的输出质量验证（21 CFR Part 11 / EU Annex 11） |
| 权限隔离：AI 只能看到用户有权看的数据 | 更新 SOP：AI 建议如何被复核/批准/拒绝 |
| 全审计追踪、数据不出界 | 上线前的数据质量治理 |
| LLM-agnostic：客户可自带模型（=BYOM） | 风险分级验证（GAMP 5 思路） |
| 每版本提供 IQ/OQ 文档（ComplianceDocs） | — |

**对本 PoC 的意义**：资格闸+引用断言+人工复核+留痕这套，不是"Veeva 已经做了的"，而是**"Veeva 告诉客户必须自己补的"**——方向正确性有工业界背书。

### ISPE GAMP AI Guardrails 五层 × 本 PoC 对照

ISPE（GAMP 制定者）2026 年发布的 RAG 输出护栏五层方案，与本 PoC 逐条对照：

| ISPE 层 | 内容 | 本 PoC 对应物 | 状态 |
|---|---|---|---|
| L1 引用校验 | 每条事实陈述必须链接到已验证来源，无引用=幻觉标记 | citation_check.py：引用存在性 + 编号越界断言（幻觉可探测形态） | ✅ 同构 |
| L2 规则化合规检查 | 对照法规要求数据库的规则断言 | 药品集合护栏（dedup_guard）、病例三道硬否决 | ✅ 同构 |
| L3 敏感信息筛查 | NLP 检测机密信息/PII | src/pii_guard.py：规则版（姓名/电话/身份证/病历号/地址）+ answer.py 集成（原始留痕、脱敏进 LLM/编码） | ✅ 规则版已落地（2026-08-22）；NER 增强待做 |
| L4 置信度阈值 | 分档最低线，低于线转人工 | 资格闸 0.60 + 判重三档灰色带 | ✅ 同构 |
| L5 毒性/语气分析 | 输出内容安全 | 内部草稿场景，优先级低 | ○ 未做 |

再对照 ISPE GAMP AI Guide 生命周期四阶段要求：锁定静态模型（temperature=0 + 模型 digest + PROMPT_VERSION 钉死）✅ / 持续漂移监控（回归探针 + 漂移容差 ±0.03）✅ / 审计日志（answers.jsonl 全要素 trace）✅ / 变更控制（set_threshold 四要素强制留痕）✅ / 偏差测试（bias testing）⚠️ 差距②。

### 本 PoC 完成清单（截至 2026-08-20，对账用）

| 能力面 | 完成内容 | 验证状态 |
|---|---|---|
| 问询判重 | 三档分流 + 双集合护栏（药品+部位/人群） + 别名归一 | 24 对标定 + 回归 12 探针绿 |
| 检索底座 | 权威加权融合 + 版本作废管理 + 覆盖缺口明示 | 文档探针绿 |
| 病例并案 | 向量粗筛+四要素规则精排 + 三道硬否决 | 14 对病例标定 14/14 绿 |
| 生成层 | 资格闸 + grounded 起草 + 引用断言 + 有界重试 + 五终态 | 冒烟 6/6 绿（未升 PQ，G-06） |
| 阈值治理 | 单点配置 → BYOM 下沉 MySQL + 变更强制留痕 | 变更演练+回归验证过 |
| 换模型 | 蓝绿重建 + 重标定 runbook | bge-small 演练全程通过 |
| 验证文档 | RTM + IQ/OQ/PQ 模板 + 验证包自动采集 | gen_validation_pack 可用 |

### 生产对齐优化路线（按优先级，向 ISPE/Veeva 看齐）

1. ~~L3 敏感信息筛查层~~（2026-08-22 规则版已落地：src/pii_guard.py 五类实体 + answer.py 双版本留痕集成）。剩余待做：NER 模型增强（规则版对无上下文线索的姓名/昵称覆盖弱）、对抗样本探针集、与药企合规团队对齐脱敏粒度
2. ~~结构化输出硬约束~~（2026-08-28 已落地：gp-2.0 JSON 结构契约，Ollama format:json 解码级约束 + 引用结构化字段断言 + 复核后缀渲染层强制附加，见设计文档 6.18）
3. **再验证触发器自动化**：探针现在是手动跑——生产形态是漂移超阈自动告警并触发重标定流程（ISPE iSpeak 持续验证闭环）
4. **接入真实数据源的基础架构**：
   - 说明书：NMPA 药品说明书库 / 药品本位码体系（替代手写样本）
   - 词典服务：MedDRA（事件编码，MSSO 订阅制）/ WHODrug（药品编码）——解决"事件不符否决"目前用包含匹配兜底的问题
   - PV 对接：E2B(R3) 报送网关、法规时限引擎（15/30 天）
   - 数据接入：ingest 管道已有 PDF/CSV/XML，补 OCR（扫描版说明书）与定时增量同步
5. **偏差测试**（GAMP AI Guide 项目阶段要求）：对标定集做分层偏差分析（渠道/年龄段/药品类别维度）
6. **权限隔离**（Veeva 平台层特性）：多租户场景下检索结果按用户权限过滤——AI 只能引用用户有权看的资料

### 国内方案格局（2026-08 调研）

| 类型 | 代表 | 公开的做法 | 可借鉴点 |
|---|---|---|---|
| PV 软件龙头 | 太美医疗（eSafety/太美智研） | 《个例安全报告智能录入白皮书》；AI 辅助病例录入、PV 全球战略 | 病例录入智能化是最成熟的国内 PV-AI 落地场景 |
| PV 信息化 | 百奥知 eSafety | 信息化+AI 的 PV 系统 | 与 Argus 同代的国产对标 |
| 药学知识库老牌 | 四川美康 | 20 年合理用药知识库 + DeepSeek 大模型 + 知识图谱 + RAG（AI 处方审核/药学问答） | **知识库+图谱+RAG 三件套**是其主打形态；处方审核场景的规则引擎积累深 |
| 云厂商平台 | 腾讯云知识引擎（大参林"AI小参"）、华为盘古（天士力/东阳光） | LLM+RAG 框架 + OCR + 多模态，药品知识问答服务 5 万员工 | 平台侧工程化（OCR/多模态/长文本 embedding）成熟，但**验证与标定细节零披露** |
| 药企自建 | 强生（法规知识库问答）、美敦力（患者随访问答） | 内部知识库 + 大模型问答 | 面向医学部/法务部的法规问答是真实高频场景 |

**国内格局的判断**：功能落地（录入自动化、知识问答）国内已很热闹，但**输出验证、阈值标定、变更留痕这套"验证层"的公开方案几乎空白**——各厂宣传都停在"接入了大模型"，没人讲"怎么证明它说得对"。本 PoC 的差异化恰好在这层：四级验证门禁、标定证据链、断言化探针，这套东西在公开资料里只有 ISPE/Veeva 级别的国际玩家在系统性地做。

**推论：验证层可独立交付（2026-08-20 补）**。资格闸+引用断言+人工复核+留痕这套有三个与被验证对象解耦的特性——模型无关（闸卡检索分数分布、断言查形式合法性，客户底下挂 DeepSeek/Qwen/豆包原样工作）、知识库无关（断言不查内容对错，查引用编号存在性/越界/拒答标记，换客户的库一行不用改）、法规语言通用（审计留痕/IQ-OQ/风险分级，中美 GxP 体系都认）。因此它可以从本 PoC 剥离为独立守门中间件或服务，对国内任何一家功能层玩家都是补短板。两个前置约束：① 阈值不可平移——0.60 这类数字贴着"模型×数据分布"标定，换客户必须重标，所以交付物是"参数+标定方法+标定工具"而非一组常数（粘性反而更高）；② ~~L3 PII 筛查是国内合规硬前置（个保法），对外交付前必须补齐~~ 规则版已落地（2026-08-22），对外交付前需补 NER 增强与合规粒度对齐。

**三件套交付形态（2026-08-22 落地）**：

| 交付件 | 形态 | 入口 |
|---|---|---|
| PII 筛查脱敏 | 管道前置层（库） | `src/pii_guard.py`：Detector 接口可插拔，规则版覆盖姓名/电话/身份证/病历号/地址；answer.py 已集成（原始问询留审计、脱敏文本进 LLM/检索编码，trace 双版本留痕） |
| 引用合法性断言 | 独立 CLI 中间件 | `scripts/validate_citation.py`：单条/批量 JSONL 两种模式，退出码 0/1 可直接接 CI 或客户管道 |
| 阈值标定服务 | 数据导入 + 报告生成 | `scripts/import_calibration_pairs.py`（客户 CSV/JSON 标定对导入，默认追加只增不改）+ `scripts/calibration_report.py`（生成 Markdown/JSON 双格式标定报告，含三建议线、可分性判断、与现役对比、后续动作清单） |

标定报告的一个已知设计点：可分性判断基于标注对直接余弦（top1 检索分的代理指标），代理层"不可分"不等于生产不可用——报告只在 F1<0.70（对标 MiniLM 负对照）时才硬建议换模型，否则给 F1 初值并强制要求 regression 终审定线，与附录 C"对余弦定初值、回归定终值"一致。

## 3. 其他医疗/生命科学 RAG 是否同一思路

学术界的医疗 RAG（MedRAG 家族）骨架相同、重心不同：

| 系统 | 思路 | 与本 PoC 相同 | 与本 PoC 不同 |
|---|---|---|---|
| [MedRAG (MIRAGE)](https://arxiv.org/abs/2402.13178) | 领域语料+检索器+LLM 基准 | 领域专用语料（PubMed 最优）优于通用 | 偏 QA 准确率评测，不管写入治理 |
| [i-MedRAG](https://pmc.ncbi.nlm.nih.gov/articles/PMC11997844/) | LLM 迭代追问再检索 | 承认单次检索不够 | 用 LLM 多轮，我们用代码编排控 token |
| [Self-MedRAG](https://arxiv.org/abs/2601.04531) | BM25+向量混合检索+RRF+自反思 | 混合检索（权威融合+标量过滤同族） | 无人工闸概念 |
| [Discuss-RAG](https://arxiv.org/pdf/2504.21252) | Agent 先讨论再检索 | Agent 编排 | 学术刷榜路线，不管生产合规 |
| PV 行业 AI（EMA/FDA/ICH 2025 指南） | 病例处理自动化+人工监督 | [GVP 判重要求](https://pharmatimesofficial.com/project/guideline-on-good-pharmacovigilance-practices-gvp-module-vi-addendum-i-duplicate-management-of-suspected-adverse-reaction-reports/)、[agentic PV 综述](https://jmai.amegroups.org/article/view/10388/html) 都强调 human oversight | 行业系统重审计追踪，学术系统不管 |

**结论**：检索侧（混合检索、领域语料、上下文补全）全世界殊途同归；**写入侧治理（判重三档、状态机、回归门禁、版本作废）是本 PoC 的差异化**，恰好也是 GxP 药企场景最看重的——学术界不研究这个，因为论文不需要过审计。

## 4. 来源清单

**Veeva**：
- [IntuitionLabs: Veeva AI Agents — Agentic AI for Life Sciences](https://intuitionlabs.ai/pdfs/veeva-ai-agents-agentic-ai-for-the-life-sciences-industry.pdf)
- [IntuitionLabs: Veeva AI Roadmap — CRM Bot, Agents, 2026 Rollout](https://intuitionlabs.ai/articles/veeva-ai-roadmap-crm-bot-agents-2026)
- [IntuitionLabs: Veeva Vault LLM Integration — RAG & Direct Data API Patterns](https://intuitionlabs.ai/articles/veeva-vault-llm-rag-direct-data-api)
- [IntuitionLabs: Veeva AI Agents — Pharmacovigilance Case Processing](https://intuitionlabs.ai/articles/veeva-ai-agents-pharmacovigilance-case-processing)

**Veeva Vault Safety 官方帮助文档（2.8 一手来源）**：
- [Case Intake Overview](https://safety.veevavault.help/en/gr/01139)
- [Duplicate Detection for Cases](https://safety.veevavault.help/en/gr/760310/)
- [Case Processing Overview](https://safety.veevavault.help/en/lr/01171)

**检索方法论**：
- [Anthropic Contextual RAG 解读（Analytics Vidhya）](https://www.analyticsvidhya.com/blog/2024/11/anthropics-contextual-rag/)
- [Towards AI: Anthropic's New RAG Approach](https://towardsai.net/p/machine-learning/anthropics-new-rag-approach)

**医疗 RAG 学术**：
- [MedRAG / MIRAGE 基准（arXiv:2402.13178）](https://arxiv.org/abs/2402.13178)
- [i-MedRAG（PMC11997844）](https://pmc.ncbi.nlm.nih.gov/articles/PMC11997844/)
- [Self-MedRAG（arXiv:2601.04531）](https://arxiv.org/abs/2601.04531)
- [Discuss-RAG（arXiv:2504.21252）](https://arxiv.org/pdf/2504.21252)
- [医疗 RAG 系统综述（PMC12157099）](https://pmc.ncbi.nlm.nih.gov/articles/PMC12157099/)

**药物警戒法规与 AI**：
- [GVP Module VI Addendum I — 重复报告管理](https://pharmatimesofficial.com/project/guideline-on-good-pharmacovigilance-practices-gvp-module-vi-addendum-i-duplicate-management-of-suspected-adverse-reaction-reports/)
- [EMA/FDA/ICH 2025 AI 指南解读（Vitrana）](https://www.vitrana.com/blogs/ai-pharmacovigilance-ema-fda-ich-guidelines)
- [PV 中 agentic AI 综述（JMAI）](https://jmai.amegroups.org/article/view/10388/html)

**AI 输出验证与护栏（2.9）**：
- [ISPE Pharmaceutical Engineering: AI Guardrails for RAG Outputs（GAMP AI Guide 配套，2026）](https://ispe.org/pharmaceutical-engineering)
- [ISPE iSpeak: AI 持续验证与再验证触发](https://ispe.org/ispeak)
- [ValGenesis: AI Agent 验证方法](https://www.valgenesis.com/blog)
- [Veeva Vault CRM 风险驱动验证案例（Therxcloud）](https://www.therxcloud.com/case-study-risk-based-validation-for-veeva-vault-crm/)
- [Clarkston: Veeva AI 解读](https://clarkstonconsulting.com/insights/)

**国内方案格局（2.9）**：
- [太美医疗：个例安全报告智能录入白皮书](https://cn.taimei.com/cn/6388)
- [太美医疗：AI 战略会（AI 辅助病例录入）](https://cn.taimei.com/cn/6439)
- [美康：AI 智核（知识图谱+RAG+DeepSeek）](https://www.medicom.com.cn/)
- [腾讯云知识引擎 × 大参林"AI小参"](https://baby.ifeng.com/c/8O2LdZ4TdIt)
- [沙丘智库：药企大模型应用案例集（强生/美敦力/天士力等 7 家）](https://www.shaqiu.cn/)
- [华为盘古药物分子大模型 × 天士力"数智本草"](https://www.huaweicloud.com/)
- [微软 PIKE-RAG：专业领域知识抽取与推理](https://www.microsoft.com/en-us/research/project/pike-rag/)
