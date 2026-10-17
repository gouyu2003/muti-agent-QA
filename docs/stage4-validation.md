# 阶段 4.0 验收与复跑

日期：2026-10-17（Asia/Shanghai）。

本阶段修改问答正确性、证据定位及最小批量评测。继续使用三个固定节点和本地 BM25；节点、流程 ID 保持兼容。自适应方法为 `multi_agent_adaptive_v1`，固定流程方法为 `multi_agent_fixed_v1`，系统版本为 `0.3.0`。

## 行为与接口

- Planner 允许 1–4 个必要子问题；重复 ID、未知依赖或循环依赖降级为原问题单问；重规划清理旧补检索请求。
- 模型返回截断或非法 JSON 时，Planner 降级到原问题，Validator 按未验证处理；空响应也消耗检索重试预算，严格遵守协议上限。
- `retrieval_config.top_k` 为正整数，默认 3，通过实际 Runtime 注入并进入配置快照。
- BM25 命中状态为 `unknown`，未命中为 `insufficient`，历史 `sufficient` 仍可读取。命中不等于语义充分。
- 依赖检索排除先前问题已知的实体，并优先检索新桥接实体所属材料，防止第二跳被重复的第一跳关键词淹没。不增加 LLM 调用。
- Validator 围绕原问题判断必要事实与实体身份；无关子问题缺失不自动阻断正确答案。空答案、无有效引用或矛盾验证字段不能通过。
- 优先使用标题/句子索引或 chunk_id 定位。带标题的旧文本引用必须在指定来源内唯一匹配；缺少标题时只允许在该子问题所有候选来源中唯一、精确匹配原文。错误标题、错误索引和歧义引用不回退猜测。输出原文和坐标由服务器填写。
- `RunContext.experiment_id/sample_id` 默认空，元数据存入已有配置快照 JSON，不新增业务列；历史记录正常读取。
- 批量 Runtime 使用独立的 `hotpotqa_context` 加载器，拒绝包含标签的文件。原有完整 fixture 加载接口不变。

## 数据与基线

原始提交：`1418c18a38e98dc674a0ff79fa942bdb8a131a3b`。先增加批量适配与评分工具，不改三个节点的原始实现，之后冻结 V0 源码、配置、依赖和文件哈希。

- 基线清单：[stage4-baseline.json](stage4-baseline.json)。
- 固定十题清单：[stage4-smoke-10.json](stage4-smoke-10.json)，使用 `stage4-smoke-v1:<题目 ID>` 的 SHA-256 排序，各取 5 道 bridge 和 comparison，不依据回答结果选题。
- 完整数据共 7,405 条。原官方主机下载失败，使用固定 revision 的原始数据镜像；下载地址、数据哈希、授权及限制见 [stage4-data-provenance.json](stage4-data-provenance.json)。未独立验证与已不可用原始主机的逐字节一致性。
- 标准答案、支撑事实在评分侧保存，模型、检索器、配置快照和运行事件仅收到问题与上下文。每题独立建立检索器。
- V0 缺少最终引用索引时，评分器只按标题与完全相同的原文唯一恢复，保留原始引用并记录恢复坐标。

评分与 HotpotQA 官方脚本对齐；已对照执行 7 组答案边界案例和 4 组支撑事实边界案例，全部一致。EM/F1 的分母包含所有选中样本，调用失败记零分。`verified` 独立统计，不作为答案正确率。

## 复跑命令

以下命令从项目根目录执行。真实模型需要已有 `.env` 或对应环境变量。每次 `--output` 必须使用新目录；程序拒绝覆盖已有实验。

```powershell
.\venv\Scripts\python.exe -m pytest --basetemp .pytest-tmp-stage4
Set-Location frontend
npm test
npm run build
Set-Location ..

.\venv\Scripts\python.exe -m pytest tests/test_integration_deepseek.py tests/test_stage4_live.py -m integration --basetemp .pytest-live-stage4-check -v

# 数据集放到 data/experiments/stage4/hotpot_dev_distractor_v1.json 后：
.\venv\Scripts\python.exe -m src.evaluation.batch run --dataset data/experiments/stage4/hotpot_dev_distractor_v1.json --manifest docs/stage4-smoke-10.json --output data/experiments/stage4/smoke-v1-new

# 另建样本清单仅用于初始化；不得覆盖已冻结清单：
.\venv\Scripts\python.exe -m src.evaluation.batch select --dataset data/experiments/stage4/hotpot_dev_distractor_v1.json --manifest data/experiments/stage4/new-manifest.json
```

V0 独立复跑：将 `data/experiments/stage4/baseline-v0/source.zip` 解压至独立目录，配置相同模型环境，从该目录用项目虚拟环境解释器执行同一个 `src.evaluation.batch run`。模型参数、top_k=3、样本和协议上限保持一致。`source.zip` 不包含 `.env`、用户资料和日常运行数据库；配置环境变量后可在其他路径执行。

比较入口：

```powershell
.\venv\Scripts\python.exe scripts/stage4_report.py --v0 data/experiments/stage4/smoke-v0-01 --v1 data/experiments/stage4/smoke-v1-validated --output docs
```

脚本检查样本清单、标签、模型参数、top_k、重试上限、源代码版本一致性及十题终态，输出 Markdown 和 JSON。调用失败需要保留并在修复后另建实验重跑；已成功收到模型响应、随后发生的原版流程错误属于基线可靠性结果，保留失败终态并记零分，不排除该题。具体异常原因无法从公开轨迹确定时不作推断。

## 实际验收结果

- 后端离线：204 passed，3 个真实模型测试默认排除。
- 前端：23 passed；TypeScript 检查与生产构建通过。构建仍有现有的单包体积提示，不影响构建完成。
- 真实模型：3 passed，包含 5 次完整 API/CLI/文档源运行，以及 4 个 Validator 边界场景，见 [stage4-live-results.json](stage4-live-results.json)。原始 HotpotQA 示例返回 `Chief of Protocol of the United States`；文档单跳返回 `2008`、多跳返回 `Northbridge`、无答案场景验证未通过。错误规划中的无关子问题不阻断答案；缺失一跳、同名实体身份不匹配和事实冲突均拒绝通过。
- V0 的三节点、BM25、图和原始提示词/协议与原提交逐文件核对，仅存在 Git 检出时的 CRLF 行尾差异，见 [stage4-baseline-fidelity.json](stage4-baseline-fidelity.json)。
- 两版 Smoke-10 均有 10 题终态记录；V0 为 9 completed / 1 failed，V1 为 10 completed。V0 失败题的所有模型调用均返回了完整 Token 用量，失败发生于流程执行阶段，公开错误未保留具体异常类型，因此不推断精确原因。该题保留失败状态并计零分。

| 指标 | V0 | V1 |
| --- | ---: | ---: |
| 完成率 | 90% | 100% |
| 答案 EM | 20.0% | 50.0% |
| 答案 F1 | 39.09% | 52.50% |
| 支撑事实 EM | 0.0% | 10.0% |
| 支撑事实 F1 | 0.0% | 56.56% |
| 内部验证通过率（已完成题为分母） | 55.56% | 60.0% |
| 模型调用次数 | 77 | 40 |
| 总 Token | 287,815 | 92,561 |
| 总耗时 | 2,310.178 s | 703.515 s |

V0 最终支撑引用全部为空，无法恢复坐标，因此证据指标为零。这不代表 V0 没有检索到证据；V1 的证据指标改善包含引用契约与回填修复的贡献。V1 的 20 条最终引用均已对照原始上下文句子核验，10 份上下文文件均只包含问题、ID 和 context，文件哈希与运行快照一致。

逐题审阅表明，新增的三个 EM 命中主要来自答案粒度和格式规范，不能解读为新解出了三道题或推理能力普遍提升。已知仍有比较题实体覆盖、非专名关系召回和标准标签粒度问题，具体见错误分析。

两版最终预测均使用未经修改的官方评测脚本复核，四项答案/支撑事实指标与批量工具一致。Windows 下使用 Python UTF-8 模式，并将官方脚本的 ujson 导入映射为标准库 json，仅适配依赖与编码，不修改评分逻辑。核验记录见 [stage4-official-metrics.json](stage4-official-metrics.json)。

最终实验目录为 `data/experiments/stage4/smoke-v0-01` 和 `smoke-v1-validated`，包含独立 SQLite、逐题轨迹、标签隔离上下文、JSONL、官方格式预测、评分标签及汇总。可审阅结果见 [stage4-smoke-comparison.md](stage4-smoke-comparison.md)、[完整 JSON](stage4-smoke-comparison.json) 和 [错误分析](stage4-error-analysis.md)。当前 V1 冻结包位于 `data/experiments/stage4/checkpoint-v1-validated/source.zip`，源码指纹为 `589093980c109329252a493b7ca70c60d0840173b86f2b1640d43449e289e018`。

本次开发保留早期 V1 尝试及失败回归日志，用于定位桥接检索与引用格式问题；这些尝试不作为最终 V1 对照结果。

本阶段仅为开发集十题先导实验，不能证明统计显著或广泛泛化。诊断标签表示可观察现象，允许重叠，不直接证明错误原因。后续 Pilot-100 根据逐题错误决定优化重点。

两版实验各自逐题串行，但部分执行时间重叠；耗时受远端共享模型服务负载影响，仅作探索性记录，不作为严格的延迟比较。正式实验应隔离执行并重复测量。
