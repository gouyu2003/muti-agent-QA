# 阶段 4.0 Smoke-10 对照

固定 5 道 bridge、5 道 comparison；模型参数、top_k 和协议重试上限保持一致。

| 指标 | V0 | V1 |
| --- | ---: | ---: |
| completion_rate | 0.9 | 1.0 |
| em | 0.2 | 0.5 |
| f1 | 0.39089991849582645 | 0.525 |
| sp_em | 0.0 | 0.1 |
| sp_f1 | 0.0 | 0.5655555555555556 |
| verification_pass_rate | 0.5555555555555556 | 0.6 |
| total_tokens_total | 287815 | 92561 |
| llm_call_count_total | 77 | 40 |
| duration_ms_total | 2310178 | 703515 |

## 逐题对照

| ID | 标准答案 | V0 答案 | V1 答案 | EM V0→V1 | F1 V0→V1 |
| --- | --- | --- | --- | --- | --- |
| 5ab9625e55429970cfb8eaa9 | Hennepin County | Hennepin County, Minnesota. | Hennepin County | 0.0→1.0 | 0.800→1.000 |
| 5a7b78c35542997c3ec971a9 | 1967 | Cannot determine: evidence lists multiple Cartoon Network series and multiple voice actresses (born 1967, 1968, 1982), without identifying which one is intended. |  | 0.0→0.0 | 0.087→0.000 |
| 5ae39f405542991a06ce9a02 | 1000 words | Insufficient evidence: no word count for Fredric Brown's "Knock" is provided. |  | 0.0→0.0 | 0.000→0.000 |
| 5a8d0940554299585d9e379e | Lawton Chiles | Lawton Chiles. | Lawton Chiles | 1.0→1.0 | 1.000→1.000 |
| 5ae81060554299540e5a572c | London | London. | London | 1.0→1.0 | 1.000→1.000 |
| 5a8db42f5542994ba4e3dd11 | SlamNation | SlamNation is the documentary that follows the National Poetry Slam, so it is about poetry. | SlamNation | 0.0→1.0 | 0.143→1.000 |
| 5abe8aad55429976d4830b60 | Kenneth John "Kenny" Ortega (born April 18, 1950) is an American producer, director, and choreographer. | Kenny Ortega was the American director who also worked as a choreographer. | Kenny Ortega | 0.0→0.0 | 0.000→0.250 |
| 5ab310bb554299233954fef9 | John le Carré | John le Carré; his MI6 career inspired his espionage fiction. |  | 0.0→0.0 | 0.462→0.000 |
| 5ac3d2ac554299741d48a330 | comedian | For architect Norman Foster: actor, director, comedian, producer, author. For director Norman Foster: comedian, producer, author. |  | 0.0→0.0 | 0.118→0.000 |
| 5a8a07af55429970aeb70222 | Orlando International Airport | Orlando International Airport served more people in 2015: over 40.2 million vs Asheville Regional Airport's approximately 787,000. | Orlando International Airport | 0.0→1.0 | 0.300→1.000 |

## 诊断观察

以下类别根据输出与标准标签计算，可以重叠，不直接证明错误原因。

- V0: {"gold_support_not_fully_retrieved": 4, "partial_answer_or_format": 6, "supporting_fact_mismatch": 10, "validator_rejection": 5, "answer_mismatch": 2, "runtime_failure_after_model_responses": 1}
- V1: {"supporting_fact_mismatch": 9, "gold_support_not_fully_retrieved": 7, "answer_mismatch": 4, "validator_rejection": 4, "partial_answer_or_format": 1}

本轮 V0 的最终支撑引用全部为空，无法执行坐标恢复，故支撑事实指标为零。这反映最终引用契约的缺口，不代表检索器没有命中证据；V1 的证据指标变化也包含引用回填修复的贡献。

完整逐题引用、run_id、配置哈希、恢复的 V0 坐标和诊断标签见同目录 JSON。

样本来自开发集，仅用于先导分析；verified 是内部判断，与标准答案正确率分开统计。不能据此宣称统计显著或泛化提升。

每个实验内部逐题串行，两版实验的部分运行时间重叠。耗时受远端共享服务负载影响，仅作探索性记录；后续正式延迟比较应隔离执行并重复测量。
