# 阶段 5～6：六组实验终端操作说明

所有命令从项目根目录的 VS Code PowerShell 终端执行。工具直接使用 RunService，不需要启动前后端服务。只有你输入带 `--allow-model-calls` 的 run/resume 命令才会调用模型。代码开发和离线测试不会替你产生真实实验成绩。

## 1. 准备完整数据集

当前仓库的 `data/hotpotqa/fixture_001.json` 只有一条真实题目；合成 demo 也不能替代完整集。请从 [HotpotQA 官方页面](https://hotpotqa.github.io/) 获取 **Dev set (distractor)**，保存到：

```text
data/experiments/datasets/hotpot_dev_distractor_v1.json
```

数据必须是原始 HotpotQA JSON 数组，含 `_id/question/answer/supporting_facts/context/type`。Hugging Face 的 parquet 或另一种列结构需先转换，不能只改扩展名。工具不自动下载，不执行模型来检查数据。

官方主机不可用时，历史使用过的镜像及固定 revision 见 `docs/stage4-data-provenance.json`；其记录哈希为 `e3da074df24e8369009918aa5cdbdd254dadcde4c63f7569d36afd6f2268caa8`。这只是该镜像的历史身份，不声称与当前官方文件逐字节相同。选定来源后在配置的 `dataset_source` 写明实际 URL/revision，可加 `dataset_sha256` 固定预期哈希。

```powershell
Get-FileHash -Algorithm SHA256 data/experiments/datasets/hotpot_dev_distractor_v1.json
```

不要在源码、配置或报告中写 API Key；`.env` 使用既有 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`。显式 `model.name` 优先于环境默认模型。

## 2. 修改配置

- `experiments/baselines.local.json`：六组，开发 300 题，每组每题重复 1 次。
- `experiments/smoke.local.json`：同一划分中的 6 题，六组各 1 次，可选工程试跑。
- `experiments/evaluation.local.json`：保留的 1,000 题，六组各重复 3 次。仅在最终方案冻结后由你启动。

精简配置中未列出的参数采用默认值；`preflight` 展示实际模型、上限、任务总数。要保证三份配置的模型/资源完全相同，可以从完整开发配置复制并仅修改 `purpose`、`output`、`repeats`；精简配置不会自动继承开发配置的后续编辑。

数据集、输出目录、划分目录等相对路径都相对于 **配置文件所在目录**。六种 methods 为 `single_rag/pipeline_fixed/pipeline_adaptive/multi_expert/lead/debate`。模型、温度、输出长度、检索 top_k、专家数、串并行、重试轮数、资源上限和重复次数均可在新实验开始前修改。Key/Base URL 不放配置文件。

默认保留集是官方 dev 的内部划分，不是官方 test。已分析过的旧十题及示例 ID 会被排除出保留集。新建划分目录时，将以前的开发/试跑清单列入 `exposure_manifests`（相对于配置文件），或在 `exclude_evaluation_ids` 列出已暴露题，避免重新抽进评价集。工具无法识别未登记的人工暴露。

抽样默认按 bridge/comparison 比例，seed=42。所有方法用相同样本。改数量/seed/抽样规则后应使用新的划分目录；不得覆盖已冻结清单。同一划分的三份配置应使用相同 `splits`、来源和排除规则。

默认 300×6×1=1,800 次完整问答，1,000×6×3=18,000 次完整问答；复杂流程每次可能调用模型多次。这些规模可以调整，不要求先跑 Pilot-100。

## 3. 生成清单并离线预检

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment prepare --config experiments/baselines.local.json
.\venv\Scripts\python.exe -m src.evaluation.experiment preflight --config experiments/baselines.local.json
```

prepare 校验 JSON、ID、证据坐标并生成 development/evaluation/smoke 三份清单。不会启动评价实验，也不会把答案传给 Runtime。相同配置可重复 prepare；已存在但内容不同则拒绝覆盖。

preflight 校验全部清单、六种方法编译、输出位置与任务数量，不访问模型。它不能证明 API Key 正确、模型可用或账户余额充足。

首次 run 会冻结当前源码、依赖、数据清单、完整配置和方法快照。它只接受新的空实验目录；旧目录用 resume。缺失或改动账本、运行库或冻结资料时会拒绝恢复，避免误重放。

## 4. 用户手动启动

可选六题试跑：

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment preflight --config experiments/smoke.local.json
.\venv\Scripts\python.exe -m src.evaluation.experiment run --config experiments/smoke.local.json --allow-model-calls
```

开发集六组实验：

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment run --config experiments/baselines.local.json --allow-model-calls
```

终端输出总体任务位置、方法、样本 ID、repeat/attempt、状态、耗时和已知 Token。逐题答案、状态与轨迹即时写入独立 SQLite 和 `runs/`，日志写入 `logs/progress.jsonl`；不会等全部结束才保存。输出目录已存在时 run 拒绝覆盖。

同一题的六种方法默认依次执行，并轮换方法顺序。协议内部默认串行，若要比较并行性能需新建配置。保持电脑不休眠、终端进程运行。关掉终端后需要手动恢复；本工具不是后台调度服务。

评价阶段命令（不是现在必须运行的步骤）：

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment preflight --config experiments/evaluation.local.json
.\venv\Scripts\python.exe -m src.evaluation.experiment run --config experiments/evaluation.local.json --allow-model-calls
```

## 5. 中断与恢复

第一次 Ctrl+C 停止新任务派发，等待当前题完成；第二次为强制中断。已经发送到供应商的请求可能仍计费。

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment resume --experiment data/experiments/baseline-dev-001 --allow-model-calls
```

默认只跑未启动任务。已完成及已失败终态不重复执行；如果终态已入运行库而评分/导出中断，恢复时只重建结果。真正状态不明的请求标为 interrupted，不自动重发。

明确补跑失败/状态不明题：

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment resume --experiment data/experiments/baseline-dev-001 --retry-status failed,interrupted --allow-model-calls
```

补跑保存新 attempt，主表仍使用首次尝试。最新尝试结果单列为 `latest_attempt_supplement`；总开销包含所有尝试。取消终态如需重试可加 `cancelled`。不允许重试 completed 或取最好成绩。

恢复核对源代码、依赖版本、模板、冻结配置、数据、清单、方法及模型端点。变化后需另建实验，不能中途偷偷换模型/参数。源码冻结包不含 `.env`、用户材料和业务数据库。OS 文件锁会在进程退出后自动释放；不要手工删除活跃进程的锁文件。

如果初始化在 provenance/任务表完成前被强制终止，保留该目录作诊断，改用新 output 重新开始；不要手工拼接账本。缺失或损坏数据库不视作可自动恢复。

## 6. 本地生成报告与审阅

```powershell
.\venv\Scripts\python.exe -m src.evaluation.experiment report --experiment data/experiments/baseline-dev-001
```

report 不调用模型。不要在该实验正在运行时生成报告（单写者锁会拒绝）。每次生成新时间戳目录，保留旧报告与人工审阅内容；`reports/latest.json` 指向最新版。

- `report.md`：主表与解释边界。
- `summary.json`：EM/F1、SP、Joint、分题型、分重复、用量、延迟、错误标签、15 对方法比较及样本配对 bootstrap 区间。
- `per-question.csv`：全部逐题与尝试数据。
- `paired-differences.csv`：同题、同次重复中两方法的首次尝试差值（right − left），并列状态与 run_id。
- `review.csv`：按题查看原答案、gold、证据、run_id 和标签；填写人工类别/置信度/备注。重新生成不会覆盖旧审阅文件。
- `examples.json`：按预先固定哈希规则选择的错误样例。
- `predictions/*.json` 与 `gold.json`：首次尝试的官方格式预测及评分侧标签，可供官方脚本复核。
- 实验根目录 `results.jsonl`：由账本重建的全尝试导出。

未完整执行时主表分数为 null/“—”，`observed_scores` 仅是已评分部分，不得当作完整集结果。失败计零，内部 verified 不代表官方正确率。每对方法须覆盖同一完整样本清单及全部重复才计算配对区间；全体方法完整后总报告才标记完整。取消/状态不明的首次尝试会保留不完整标记。

检索覆盖统计使用每次实际检索输出，初次为各检索实例首次调用证据的并集，累计为全部检索调用并集。缺失坐标/旧轨迹没有原始检索字段时记未知，不用最终引用猜测检索召回。自动错误标签是可观察现象，根因仍需人工审阅/消融验证。

用量缺失保持未知；`reported_total` 是有记录部分之和。`max_reserved_tokens` 是 UTF-8 字节与最大输出组成的保守资源预留，不是实际 Token 或账单金额上限。若配置 `price`（currency、as_of、input_per_million、output_per_million），仅按用户费率估算，未建模的缓存/推理费率和未知调用费用仍需供应商账单核对。

## 7. 从已有发布系统派生方法（可选）

默认无需创建 UI 系统。可用 `published_systems` 为某个方法指定只读的系统数据库路径、system_id、version，例如：

```json
"published_systems": {
  "debate": {"database": "../data/systems/systems.sqlite3", "system_id": "替换为真实ID", "version": "v1"}
}
```

只支持已发布 schema-3、语言 V2、与对应标准模板结构一致的版本；自定义图不能冒用标准方法名称。保留角色提示词与来源指纹，但实验公共模型/检索/预算参数覆盖该副本；每题材料换成当前 context。原系统与业务数据库只读，不修改已发布版本。新来源可能引入提示词差异，需在论文方法说明中披露，公平六组对比优先使用默认模板。

## 8. 离线验证边界

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_experiment.py tests/test_evaluation.py -p no:cacheprovider --basetemp .pytest-tmp-stage56-user
```

使用合成题目与模型替身，验证工程逻辑；不下载数据，不提供论文成绩。最终真实模型兼容性、六种方法的实际回答质量与成本，都需要由你运行后核验。
