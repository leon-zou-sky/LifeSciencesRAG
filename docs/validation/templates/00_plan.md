# 验证总计划（Validation Plan）

| 字段 | 内容 |
|---|---|
| 系统名称 | 生命科学 RAG PoC（MI/PV 知识检索系统） |
| 验证类型 | {{validation_type}} |
| 执行日期 | {{date}} |
| 执行人 | {{executor}} |
| 模型版本 | {{model}} |
| 触发原因 | {{trigger}} |

## 验证范围

- **IQ（安装确认）**：运行环境与模型指纹——系统装在正确的地基上，且"这个模型就是这个模型"
- **OQ（运行确认）**：标定测量——新配置/新模型在标准标注集上的分布与阈值推导过程
- **PQ（性能确认）**：黄金集端到端回归——真实业务探针下的行为断言

## 引用文件

- 需求可追溯矩阵：docs/validation/RTM.md
- 重标定 runbook：docs/model-selection-recalibration.md 附录 A
- 阈值配置：config/thresholds.yaml（单一事实源）
- 黄金集：config/golden_set.json（探针只增不改）

## 通过标准

1. IQ：环境与模型指纹记录完整，三方版本一致（golden_set ↔ thresholds.yaml ↔ 模型目录）
2. OQ：标定分布判断非"不可分"（相对判定不劣于现役），阈值取值理由已注释
3. PQ：黄金集回归 exit 0——归一/档位/漂移/版本四类断言全过
4. 已知缺口：全部登记于 RTM Deviation Log，无未登记缺口

## 签字

见 04_signature.md。本验证包任一部分未通过，签字页不得签署，系统不得进入生产使用。
