# 阶段 5～6 工程交付与验证

日期：2026-10-22。实现起点：`6702827`（此前业务代码为 `d3ee9b1`）。开发验证完成后，用户另行授权提交并推送 GitHub。

## 交付范围

新增 `python -m src.evaluation.experiment`，提供 prepare、preflight、run、resume、report。用户在 VS Code 终端手动运行真实模型实验；本次开发只使用本地合成数据与模型替身。

| 批次 | 交付 | 验证证据 |
| --- | --- | --- |
| A 配置与数据 | 严格配置、相对路径解析、确定性分层抽样、开发/评价/试跑清单、暴露题排除、哈希与坐标校验 | 清单可重复生成；更改 seed 后不能覆盖；无开发/评价 ID 交集；旧评测回归通过 |
| B 六组方法 | single_rag、pipeline_fixed、pipeline_adaptive、multi_expert、lead、debate，统一编译并调用 RunService | 六组真实本地协议图执行；gold 哨兵不进入运行 context/轨迹；Lead 仅执行被选专家；Debate 提案隔离；发布库字节不变 |
| C 持久化与恢复 | 独立任务库/运行库、冻结源码和配置、操作系统互斥锁、追加尝试、终态对账 | 模拟落盘后中断不重复调用；未知中断需显式补跑；失败成绩保留；跨进程锁；账本/运行库缺失拒绝重放 |
| D 报告与诊断 | Answer/SP/Joint、15 对方法比较、配对 bootstrap、逐题差值、类型/重复分组、错误审阅表 | Joint 非 F1 乘积案例；部分运行不报完整成绩；主表和补跑分离；原始检索覆盖可定位；未知用量不当零 |
| E 可操作交付 | 开发/可选试跑/评价三份配置，终端说明，离线端到端测试 | prepare/preflight 在合成数据上成功；run/resume/report 全链路离线通过；CLI 帮助可用；缺少显式模型许可参数时拒绝 run/resume |

复用现有 BM25、协议编译器、RunService、SQLiteRunRepository 和结构化输出/资源上限。原 Runtime 的唯一行为相关改动是为检索阶段输出附加 `retrieved_evidence_chunks` 供评测；该字段在生成后续阶段输入时移除，避免重复注入模型提示词或绕过证据共享约束。原问答、旧 batch、系统与知识管理接口未改写。

## 自动验证

全后端离线回归：**355 passed，3 deselected**，264.57 秒。

```powershell
.\venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp .pytest-tmp-stage56-full
```

3 项真实模型 integration 测试由项目默认配置排除。新实验测试另有网络连接及真实模型构造拦截；所有实验库位于 pytest 临时目录。

全量回归后仅补齐实验配置部分覆盖时的 40 次调用默认值、非 DeepSeek 模型请求策略元数据及对应断言，随后运行受影响的实验/旧评测专项回归：**38 passed**，89.87 秒。最终源码的受影响范围复验通过。

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_experiment.py tests/test_evaluation.py -p no:cacheprovider --basetemp .pytest-tmp-stage56-final
.\venv\Scripts\python.exe -m src.evaluation.experiment --help
```

CLI 帮助命令退出码 0；`git diff --check` 通过（只有 Git 的 LF/CRLF 转换提示）。没有修改依赖或前端，因此没有重新运行前端构建/浏览器验收。

## 用户运行后的验收

本次开发验证未下载完整数据、调用真实模型、写入日常业务数据库或启动/重启服务。完整数据集的约定路径在验收时不存在。

操作入口见 [六组实验终端操作说明](stage5-6-usage.md)：补齐 HotpotQA dev distractor 文件 → 调整配置 → prepare → preflight → 用户选择是否进行六题 smoke → 用户手动运行六组开发实验 → 查看报告并填写人工错误分析。保留评价实验另行启动，不要求 Pilot-100。

工程验证通过只表明工具与恢复/评分逻辑经过离线验证。供应商当前模型可用性、真实输出质量、实际耗时/用量及论文结论仍需真实运行。默认比较是相同可见语料和资源上限下的端到端比较；检索与模型调用量可能不同，不能直接归因为协作机制本身的因果收益。

同一实验 resume 要求源码/依赖/方法/数据/模型端点身份一致；修改参数或实现请新建实验。首次尝试的未知中断会持续标记主报告未完整，显式补跑只更新补充结果。API 请求中断不保证远端撤销或免于重复计费。
