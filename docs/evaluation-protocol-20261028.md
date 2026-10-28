# 六种问答协议正式评价方案

确定日期：2026-10-28，在 100 题开发实验完成后、正式评价启动前确定。本文件是本地分析计划，不追溯性声称为独立预注册。

## 研究范围及固定版本

研究问题：六种现有问答协议在共同资源上限下，其端到端回答质量、证据质量、完成可靠性与实际开销有何差异。协议包含不同的检索、审核及协作步骤，因此不将比较解释为等实际费用实验或单一协议因素的因果实验。

执行代码基线为 `main` 的 `8b5c0bb6dc0b30ed171b05f493943ab9e6a83344`，运行源码 SHA-256 为 `727bcd49052e6f39d7e4ceeca50300e0dbea4c0f61761b215298f91e421261da`。本次准备仅补充文档和本地记录，运行源码与实验配置不变。若后续提交仅包含文档，仍须保留相同源码、方法及配置指纹，并记录首次实际启动时的 Git 版本。

使用 CLI `src.evaluation.experiment`；配置 `experiments/evaluation.local.json`；输出 `data/experiments/baseline-evaluation-001/`。平台批量评测首版可独立验收，不用平台重新抽题或代替本轮 CLI 评价清单。

首次 run 会归档实际代码版本、源码压缩包、依赖、配置、模型端点摘要、数据清单和方法定义。准备阶段不创建正式评价运行目录、不调用模型；恢复时沿用首次运行的冻结身份，不能绕过源码或端点变化检查。

## 数据与规模

- 数据文件：`data/experiments/datasets/hotpot_dev_distractor_v1.qc.json`，7,404 条；SHA-256：`c25430a3e1a130ad3b3d3a0a775ae2197a7eaba58194034aec79e0080e876f61`。
- 固定划分：`data/experiments/splits/hotpot-dev-seed42-budget-v2/evaluation.json`；文件 SHA-256：`3046dfc42cf533dd5a9e0c43f5803eac2f37f736919d90a436e968fdd89490a2`。
- 1,000 道不同题目，800 道 bridge、200 道 comparison；种子 42，按题型比例抽样。保留此前已选定的同一批题，不重新随机抽样。
- 已登记的旧开发 300 题、新开发 100 题、smoke 与既有已知曝光题排除在评价范围外；新旧开发与评价的交集均为零。
- 每题六方法各执行一次，共 6,000 项计划任务。标准答案、支持事实只在评分侧使用，Runtime 只接收该题公开问题和 context。

## 方法与共同条件

方法顺序表：`single_rag`、`pipeline_fixed`、`pipeline_adaptive`、`multi_expert`、`lead`、`debate`。同一道题配对比较；沿用配置默认的逐题方法顺序轮换，整批顺序执行，不并行启动另一份相同实验。

| 参数 | 固定值 |
| --- | --- |
| 模型 | deepseek-v4.1-flash；保持当前供应商、地域和端点 |
| temperature / 单次最大输出 | 0.0 / 4,096 tokens |
| 输出格式 / 思考设置 | JSON Object / disabled |
| 结构化输出修正 | 最多追加 2 次 |
| SDK 重试 | 0；由统一请求层执行有界重试 |
| 瞬时请求错误重试 | 最多追加 1 次，等待 1 秒；408、429、500、502、503、504 或连接/超时错误 |
| 不自动重试的请求错误 | 内容审核、认证/权限、明确额度/计费错误和需要诊断的坏请求 |
| 检索 top_k | 3 |
| 专家数 | 多专家模板 2 个；单智能体模板 1 个 |
| 专家执行模式 | sequential |
| 自适应流水线检索 / 规划重试上限 | 3 / 2 |
| Lead / Debate 回合数 | 2 / 2 |
| 单题节点 / 模型调用上限 | 60 / 40 |
| 单题预留 Token 估算上限 | 1,000,000；不是实际 Token 或人民币硬限额 |
| 单题 / 单节点超时 | 600 / 120 秒 |
| 每题每方法重复数 | 1 |

上述值包含配置类默认值；以运行时 `config.resolved.json`、各方法快照及请求策略归档核对。瞬时请求重试和结构输出修正是不同层的有界机制，全部实际调用计入上限与开销。模型别名不代表供应商权重永久不变，应保留模型名称、请求 ID 和运行时间。

## 指标与分析规则

主要质量指标为 **Answer F1**，分母为每方法计划的全部 1,000 项首次尝试。次要质量指标为 Answer EM、SP EM/F1 和 Joint EM/F1。所有六方法均报告，不根据评价成绩删掉方法、改变主要指标或更换分母。

可靠性报告 completed/failed 数量与比例、内容审核、连接/超时、结构输出失败、回合触限、证据不足/空答案等类别。completed 表示流程完成，不等同于答案正确；自动错误标签是观察依据，不单独证明根因。对轮数触限同时查看最后审核节点。

开销报告实际模型调用次数、输入/输出/总 Token 和任务耗时的总量、均值、中位数、P95，以及整批墙钟时间。未知用量保持未知，并报告已知用量分母；任务级汇总未知时可补充已知逐请求用量，但不能将其称为完整费用。实际扣费单独依据供应商账单。

主要配对比较为其余五种方法相对 single_rag 的 Answer F1 差值。沿用报告器对同一道题差值按题重采样，2,000 次 bootstrap，固定种子及现有配对/指标派生种子，报告 95% 百分位区间。报告器保留其余配对与指标，作为次要、探索性分析；不把未经多重比较校正的区间宣称为整体显著性结论。

bridge/comparison 分层结果、SP/Joint 指标和错误类别用于解释总体结果。每题每方法只有一次真实运行，因此不能报告三次模型重复的均值/标准差，也不能将按题 bootstrap 区间解释为模型调用随机性区间。开发集上的排序不能替代本轮评价结论。

## 失败、补跑与方案变化

- 首次尝试为主表。failed 项保留并对质量指标计零；供应商内容审核拦截也保留。所有尝试均计入总开销。
- 默认不补跑已有失败；若之后明确补跑，追加尝试，最新尝试只作补充，不能覆盖主表或删除原失败。
- cancelled、interrupted 和未启动项会使正式主表保持不完整，不用已完成子集均值冒充完整 1,000 题结果。
- 保持模型、提示词、检索、轮数、评分、方法定义和样本清单固定。出现需要修改实现的系统问题时暂停，保存旧档案；变更后另建版本和输出目录，不覆盖原实验。
- 正式评价启动后不通过改参数、替换题目或选择性补跑改善主表。开发复盘的结构错误与内容拦截按现有策略记录。

## 启动授权与运行操作

用户于 2026-10-28 确认节省计划剩余 30 元、学生代金券剩余 275 元，并明确要求不再核算余额、按现有规模继续准备。该记录来源于用户确认，不是新的云端账单审计；本次不调整规模或云账户设置。

由用户在 VS Code PowerShell 终端执行，下列预检成功后才会启动真实调用：

```powershell
Set-Location 'C:\Users\ShenTianyu(CN-TV-T1)\schoolwork\multi-agent-qa'
.\venv\Scripts\python.exe -B -m src.evaluation.experiment preflight --config experiments/evaluation.local.json
if ($LASTEXITCODE -eq 0) {
    .\venv\Scripts\python.exe -B -m src.evaluation.experiment run --config experiments/evaluation.local.json --allow-model-calls
}
```

预检应显示 samples=1000、repeats=1、planned_tasks=6000、model_calls=0。已有清单已通过校验，无需再次 prepare 或重建划分。运行后先观察前 10—20 项，保持电脑接电、终端打开、网络可用并关闭自动睡眠。运行可能跨过多个时段，不以启动时段推断整批价格。

孤立的内容审核或结构失败按规则记录；若连续出现认证、额度或连接等系统错误，手动暂停检查。一次 Ctrl+C 请求完成当前任务后停止；第二次会强制中断，可能留下未知调用状态。CLI 没有按连续错误次数或人民币余额自动停机的功能。

未改代码、依赖、模板及模型端点时，暂停后继续尚未启动的任务：

```powershell
.\venv\Scripts\python.exe -B -m src.evaluation.experiment resume --experiment data/experiments/baseline-evaluation-001 --allow-model-calls
```

该命令默认不会重发 failed、interrupted 或 cancelled 的已有尝试。若有状态不明项，先核对原运行记录再决定是否明确补跑，不能用重新执行 run 来覆盖或重启。

执行完会自动生成报告，终端输出报告目录。需要另行汇总时执行以下离线命令：

```powershell
.\venv\Scripts\python.exe -B -m src.evaluation.experiment report --experiment data/experiments/baseline-evaluation-001
```

原始进度保存在 `logs/progress.jsonl`，逐题详情在 `runs/`，首次尝试及全尝试账本、冻结源码和最终报告均保留在本轮独立输出目录。每方法全部 1,000 项产生 completed/failed 终态后才报告完整集成绩。
