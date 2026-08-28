# 药物警戒判重的阈值与权重标定：统计学方法应用调研

> 调研对象：生命科学（药物警戒 / Pharmacovigilance, PV）领域，病例重复检测（duplicate detection）中
> **匹配权重如何确定、判定阈值如何标定**的统计学方法及工业实践。
> 调研日期：2026-08-19。所有结论附出处，区分「公开披露」与「推断」。
> 关联文档：rag-industry-research.md（行业全景）、vector-db-construction-design.md 6.15（本 PoC 的标定实践）。

---

## 1. 问题定义：为什么判重的阈值/权重是个统计学问题

PV 判重的判定对象是「两份报告是否指向同一真实病例」。输入是多字段比对证据
（患者/药品/事件/时间等），输出是三态决策（并案 / 人工复核 / 独立）。

统计学在这个问题里承担三个角色：

1. **权重从哪来**：每个字段的吻合「值多少分」——工程拍值 or 从数据估计
2. **阈值画在哪**：匹配分的判定切点——需要已知答案的标注对分布来支撑
3. **性能怎么证**：灵敏度 / 特异度 / F1 是否达标——需要统计显著性支撑验证结论

GxP 场景的特殊约束：阈值与权重属于**验证状态的一部分**，任何调整须走变更控制并留痕；
运行时阈值不得自适应漂移（同一输入必须同一输出，否则审计失效）。

---

## 2. 方法谱系：五代方案

| 代际 | 方法 | 权重来源 | 阈值来源 | 代表 |
|---|---|---|---|---|
| ① 精确规则 | 字段完全相等 | 无 | 无（布尔） | 早期 PV 系统 |
| ② 加权打分 | 字段权重加权求和 | 工程经验拍定 | 小规模标注对实测分布 | 本 PoC（0.5/0.3/0.2）、Argus 传统模式 |
| ③ 概率记录链接 | Fellegi–Sunter 似然比 / hit-miss 模型 | **从已知重复对与数据库频率统计估计** | 已知重复对 vs 随机对的分数分布对比 | WHO-UMC vigiMatch 2014/2017 |
| ④ 机器学习分类器 | 标注对训练分类器（线性 SVM 等） | **训练自动学习** | 训练集交叉验证 + 性能目标 | vigiMatch2025 |
| ⑤ 向量语义混合 | embedding 语义分 + 结构化规则分融合 | 工程拍定 + 标定集实测 | 分层标定对分布空档 | 本 PoC（向量×0.5+规则×0.5）、各厂 AI 新品 |

注：②⑤的差别在是否引入语义层；③④的差别在权重是统计估计还是监督学习。
工业系统普遍是组合态（如 ⑤+硬规则否决），没有哪家纯靠单一代际。

---

## 3. 统计学方法详解

### 3.1 概率记录链接（Fellegi–Sunter / hit-miss 模型）

vigiMatch 的方法基础（Norén et al. 2014，UMC）：

- 每对记录计算**匹配分**：字段吻合加分、不符扣分（hit-miss）
- 每个字段的权重 = **似然比**：该吻合在真重复对中出现的概率 ÷ 在随机记录对中出现的概率
- **频率敏感**：稀有值吻合权重更高。例：两份报告同来自安道尔（VigiBase 中约 800 份）
  比同来自韩国（约 90 万份）更能说明问题——稀有事件的偶合概率低
- 多元素字段（药品/事件）：权重随吻合元素数量及各元素在库中的常见度变化
  （5 味药中吻合 4 味 >> 吻合 1 味常见药）
- 显式补偿共报药品-事件之间的相关性（基于全球报告率）
- **启发式硬规则**：患者侧要素（年龄/性别/发病日期/姓名首字母）净贡献必须 > 0，
  否则不予标记——与本 PoC 的「性别不符硬否决」同思想
- 模型参数估计所需的种子数据可以很少：初版仅用 **38 对经确认的重复对** + 全库频率

阈值标定：比较已知重复对与随机记录对的匹配分分布；
vigiMatch2017 的启发式取**已知重复对匹配分的均值**作为疑似阈值。

### 3.2 监督学习标定（vigiMatch2025）

2025 年升级版（Barrett et al., arXiv:2504.03729）从纯概率链接转向预测建模：

- **线性 SVM** 分类器（药品/疫苗分开建模；线性核使分数仍可解释为证据加权和——
  可解释性是 PV 场景的硬约束，深度学习黑盒在此不受欢迎）
- 国家别 hit-miss 模型：减少报告模式与全球库差异较大国家的假阳性
- 利用报告上**全部唯一日期**，包括从自由文本叙述中抽取的日期
- 训练集：扩充后的 1,500+ 标注对
- 实测性能：精确率 疫苗 92% / 药品 54%（旧模型 41%）；召回 疫苗 80~85% / 药品 40~86%
  （旧模型 24~53%）——**药品判重在全球尺度上仍是远未解决的问题**

### 3.3 阈值选择的经典统计量

小规模标定阶段（本 PoC 量级）到工业验证阶段使用的统计工具：

| 工具 | 用途 | 出处/基准 |
|---|---|---|
| ROC 曲线 / AUC | 阈值全景评估 | 分类器评估标准方法 |
| Youden 指数（J = 灵敏度+特异度−1） | 最优切点选择 | 本 PoC calibrate.py 三条建议线之一 |
| F1 最大化 | 精确率/召回率调和平均切点 | 同上 |
| 分位数（P5 等） | 保证正样本覆盖率的保守切点 | 同上 |
| 分布空档取中 | 小样本阶段的工程权宜 | 本 PoC 三次标定实践 |
| Cohen's kappa | 编码/判定一致性 | 行业基准 0.80~0.90（Vitrana） |

### 3.4 假阳/假阴的代价不对称

PV 判重中两类错误的代价极不对称：

- **假并案（false merge）**：两个真实病例被合成一个 → 重复报告漏报 → 安全性信号统计失真，监管责任
- **假独立（false new）**：重复报告未识别 → 重复计数 → 信号虚高，资源浪费

行业共识是假并案更严重，因此阈值偏向保守（高精确率优先），
配合人工复核带（gray zone）吸收不确定性——Veeva / Argus / vigiMatch 全部如此，
AI 始终处于「列候选」的辅助位，最终并案由人确认。

---

## 4. 工业界性能基准与验证框架

### 4.1 验收基准（Vitrana 行业基准，PV-AI 系统）

- 灵敏度（真重复识别率）：**92~97%**
- 特异度（避免错误链接）：**96~99%**
- F1：**0.90~0.95**
- 编码一致性 Cohen's kappa：0.80~0.90
- 要求：「应按预期工作流验证并在使用中持续监控」

### 4.2 验证框架

- **TransCelerate BioPharma**：AI 在 PV 中的验证框架（行业联盟标准）
- **CIOMS Working Group XIV**：药物警戒中 AI 应用的国际理事会指南
- GxP/CSV 通用约束：阈值与权重变更走变更控制、证据归档、机器不出批准结论（人签字）

### 4.3 样本量与可信度

- 置信区间随样本量收窄：14 对全对仅能支撑「准确率 ≳75%」级别的结论；
  工业级验证需要数百至上千标注对支撑 95%+ 指标结论
- **多样性优先于数量**：渠道覆盖（医院/药店/热线）、字段残缺形态、难负样本
  （一字之差药品、同类药、同患者不同事件）必须专门构造
- **时效性**：报告模式漂移使旧标注失效，需要持续点检（探针回归）+ 定期重标
- 用户纠正（人工闸的接受/拒绝决定）是标注数据的持续矿藏——
  Veeva Vault Safety.AI 公开披露追踪 accept/reject 用于再训练；vigiMatch 迭代同此路径

---

## 5. 各家实践对照

| 厂商/机构 | 方案代际 | 权重/阈值方法 | 公开程度 | 出处 |
|---|---|---|---|---|
| WHO-UMC vigiMatch | ③ → ④（2025） | 似然比统计估计 → 线性 SVM 训练；阈值自标注对分布 | **完全公开**（论文级） | [PubMed 24627310](https://pubmed.ncbi.nlm.nih.gov/24627310/)、[arXiv:2504.03729](https://arxiv.org/pdf/2504.03729) |
| Oracle Argus | ② 为主，2024 加⑤（可选开关） | 传统：Oracle Text 档案 + 字段规则；Smart Duplicate Search：AI 匹配分，默认关闭 | 功能公开，算法不公开 | [Oracle 发布稿](https://www.prnewswire.com/news-releases/oracle-advances-safety-case-management-for-life-science-organizations-302192427.html)、[官方文档](https://docs.oracle.com/en/industries/life-sciences/argus-safety/8.4.3/aeoaj/duplicate-search.html) |
| ArisGlobal LifeSphere | ⑤ AI 原生（NavaX 引擎） | 患者人口学/药品/事件/日期比对自动标记 | 功能公开，算法不公开 | [IntuitionLabs 综述](https://intuitionlabs.ai/articles/pharmacovigilance-software-systems-overview) |
| Veeva Vault Safety | ⑤（Safety.AI） | 未公开；披露追踪用户接受/拒绝再训练 | 功能公开，算法不公开 | [Veeva 官方帮助](https://safety.veevavault.help/en/gr/760310/)、[IntuitionLabs PDF](https://intuitionlabs.ai/pdfs/an-overview-of-pharmacovigilance-pv-software-systems.pdf) |
| 本 PoC | ②+⑤+硬否决 | 工程权重拍定（0.5/0.3/0.2）+ 三道硬否决；阈值经 14 对分层标定实测定线 | 全公开（本仓库） | 设计文档 6.3/6.15、config/case_calibration_pairs.json |

---

## 6. 对本 PoC 的映射与借鉴

### 6.1 已对齐的实践

- **硬否决 ≈ vigiMatch 患者要素净贡献 >0**：性别/事件/药品零重合三条否决与
  vigiMatch2017 启发式同思想——患者侧证据必须为正的才谈得上疑似
- **阈值自标注对分布定线**：与 vigiMatch「已知重复对 vs 随机对分布对比」同源，
  差别在负样本规模（7 手工构造 vs 百万级随机对）
- **灰色带 + 人工闸**：与全行业的「AI 辅助位」共识一致；vigiMatch2025 药品类
  精确率仅 54% 的事实是这一哲学最硬的依据
- **badcase 只增不改**：与 Veeva 追踪 accept/reject 再训练同路径

### 6.2 可借鉴的进化方向（按优先级）

1. **频率敏感权重**（vigiMatch 核心思想）：药品/事件吻合的权重应随该值在库中的
   常见度变化——「辛伐他汀+胺碘酮联用」的吻合比「二甲双胍」的吻合更值钱。
   落地前提：库内报告量足够统计频率分布（当前样本量不支持，标定集攒厚后再议）
2. **真实随机对作负样本**：标定负样本从手工构造扩展到库内随机配对抽样，
   更贴近生产的假阳分布
3. **日期全要素利用**：vigiMatch2025 使用报告上全部日期（含叙述文本抽取），
   本 PoC 仅用 onset_date 单字段
4. **监督学习权重**：标注对达数百对量级后，可用线性模型（保可解释性）学习权重，
   替代工程拍值——vigiMatch2025 证明线性 SVM 在此场景足够且可审计

### 6.3 明确不借鉴的

- 黑盒深度学习判重：可解释性是 PV 审计硬约束，vigiMatch2025 坚持线性核即是明证
- 运行时阈值自适应：GxP 变更控制要求阈值冻结、调整留痕（见本 PoC 设计文档 6.15）

---

## 7. 出处汇总

**方法论原始文献**
- Norén GN et al. (2014). *Performance of probabilistic method to detect duplicate
  individual case safety reports*. Drug Safety. [PubMed 24627310](https://pubmed.ncbi.nlm.nih.gov/24627310/)
- Barrett JW, Erlanson N, Félix China J, Norén GN (2025). *A Scalable Predictive Modelling
  Approach to Identifying Duplicate Reports*（vigiMatch2025）. [arXiv:2504.03729](https://arxiv.org/pdf/2504.03729)
- WHO-UMC 方法页：[who-umc.org/research/vigimethods](https://who-umc.org/research/vigimethods/)

**行业综述与基准**
- *Artificial intelligence in pharmacovigilance: advancing drug safety monitoring and
  regulatory integration*. [PMC12317250](https://pmc.ncbi.nlm.nih.gov/articles/PMC12317250/)
- Vitrana. *Benchmarking AI Solutions in Pharmacovigilance: KPIs, Validation, and Best Practices*.
  [vitrana.com](https://www.vitrana.com/blogs/benchmarking-ai-solutions-pharmacovigilance-kpis-best-practices)
- IntuitionLabs. *An Overview of Pharmacovigilance (PV) Software Systems*.
  [PDF](https://intuitionlabs.ai/pdfs/an-overview-of-pharmacovigilance-pv-software-systems.pdf)
- IntuitionLabs. *AI Agents in Pharmacovigilance: A Technical Overview*.
  [PDF](https://intuitionlabs.ai/pdfs/ai-agents-in-pharmacovigilance-a-technical-overview.pdf)
- IntuitionLabs. *Pharmacovigilance Systems: Argus vs LifeSphere vs Veeva*.
  [PDF](https://intuitionlabs.ai/pdfs/pharmacovigilance-systems-argus-vs-lifesphere-vs-veeva.pdf)

**厂商官方资料**
- Oracle. *Oracle Advances Safety Case Management*（Smart Duplicate Search 发布，2024-07）.
  [PR Newswire](https://www.prnewswire.com/news-releases/oracle-advances-safety-case-management-for-life-science-organizations-302192427.html)
- Oracle Argus Safety 8.4.3. *Duplicate Search*. 
  [官方文档](https://docs.oracle.com/en/industries/life-sciences/argus-safety/8.4.3/aeoaj/duplicate-search.html)
- Veeva Vault Safety Help. *Duplicate Detection for Cases*.
  [safety.veevavault.help](https://safety.veevavault.help/en/gr/760310/)
- Veeva Vault Release Notes 26R1（临床试验受试者匹配增强）.
  [rn.veevavault.help](https://rn.veevavault.help/zh-cn/gr/whats-new-in-26r1/)

**本 PoC 内部文档**
- 设计文档 6.3（混合判重架构）/ 6.15（病例标定实践：三道硬否决 + 阈值重锚）
- config/case_calibration_pairs.json（14 对分层标定对）
- config/thresholds.yaml case_report 段（阈值及标定证据注释）
- scripts/measure_case_gate.py / test_cases.py（测量与断言探针）

---

## 8. 本地存档（docs/references/）

以下来源已下载本地存档（2026-08-19），断网可查：

| 本地文件 | 来源 |
|---|---|
| vigimatch2025-arxiv2504.03729.pdf | vigiMatch2025 论文（arXiv，8 页，SVM 方案与性能数字一手出处） |
| ai-in-pharmacovigilance-review-PMC12317250.pdf | AI 在 PV 中的应用综述（EuropePMC，16 页） |
| intuitionlabs-pv-software-overview.pdf | PV 软件系统综述（含 Veeva AI 学习用户纠正的披露） |
| intuitionlabs-ai-agents-in-pv.pdf | PV 领域 AI Agent 技术综述 |
| vitrana-benchmarking-ai-pv.html | Vitrana 行业性能基准（网页存档） |

存档缺口：
- **IntuitionLabs「Argus vs LifeSphere vs Veeva」对比 PDF**：链接 2026-08-19 实测 404 失效，
  正文第 5 节的对照信息以该站综述版（已存档）为准
- vigiMatch 2014 原始论文（Drug Safety 期刊）正文付费墙，PubMed 仅摘要；
  方法细节经 vigiMatch2025 论文与 UMC 官方方法页转述核对
