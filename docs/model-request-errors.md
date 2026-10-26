# 模型请求错误与 smoke 复验

正式运行现在保存经过脱敏的 HTTP 状态码、供应商错误码、错误信息、Request ID、异常类型和错误分类。供应商未提供的字段保持 `null`，不从历史 HTTP 400 推断错误原因。

## 错误与重试

- `content_filter`：例如 `data_inspection_failed`。运行以 `provider_content_filter` 失败，不重试，不改成证据不足，不删除或替换题目，首次尝试仍按失败计分。
- HTTP 400 参数错误、401/403 鉴权错误，以及明确的欠费/免费额度耗尽错误：直接失败。
- HTTP 408、429、500、502、503、504 或 SDK/HTTP 连接及超时异常：同一请求最多额外重试 1 次，等待 1 秒。SDK 自带重试保持为 0。已识别的内容审核、账务或权限错误优先禁止重试。
- 请求重试与 JSON 输出修正分开：每轮输出请求最多发出 2 次；当前 `output_retries=2` 最多有 3 轮输出修正，即单次 JSON 节点最多 6 次网络调用，仍受单题调用次数、预留 Token 和时间预算约束。请求异常不会进入 JSON 修正或重新检索流程。
- 每次请求重试都重新检查取消、调用预算和剩余时间，记入 `llm_call_count`。错误请求的用量未知，整题 Token 合计保持未知，已获得响应的单次用量仍保留。超时请求可能已被远端执行及计费。

## 查看位置

运行详情或 `runs/<run_id>.json` 的 `run.metrics.agent_usage.<agent_id>.calls` 中，`outcome=request_error` 的条目包含 `request_error`、`will_retry`、等待秒数和失败请求提示词的 SHA-256/长度。请求错误记录不保存完整提示词、原始响应体或请求头；错误信息中的已知密钥、端点和请求文本会脱敏。

失败协议节点的 `output.request_error`、`run_failed` 事件、终端进度 JSON 和 `logs/progress.jsonl` 也包含脱敏诊断。已有的节点输入和证据归档行为保持一致。

报告的 `labels` 会区分 `provider_content_filter` 等请求错误；临时故障重试后流程完成时会有 `request_retry_recovered`，其答案仍按正常评分规则评价。旧记录只有 `request_error` 且没有供应商详情时保留未知，不事后补写结论。

## 本次诊断的证据边界

2026-10-26 对旧 smoke 两个节点重建请求后，Debate 的新请求收到 HTTP 400 / `data_inspection_failed`（Request ID `afc61e70-ce75-9100-aeb1-1edad890874b`），明确属于输入内容审核拦截。Adaptive 的新请求获得正常响应（Request ID `be7b655d-95ea-93e3-b519-e16f234edbb2`），但未做答案/schema 校验。

这些是新请求的结果。原始完整请求没有归档，重建时 JSON 字段顺序可能不同；不能用新返回恢复历史 HTTP 响应，也不能据此确定原 Adaptive 400 的原因。诊断不会改写旧 smoke 的 34 完成、2 失败结果。

## 用户手动复验

源码改进后会产生新的源码指纹，旧实验的 `resume` 及旧源码诊断重建应继续拒绝执行。使用新配置 `experiments/smoke-request-errors.local.json`，数据、六题划分、六种方法和模型参数沿用原 smoke，仅输出改为 `data/experiments/baseline-smoke-002`。它是一次新实验，共 36 个任务，可能产生模型费用。

先在项目根目录的 VS Code PowerShell 中做离线预检：

```powershell
.\venv\Scripts\python.exe -B -m src.evaluation.experiment preflight --config experiments/smoke-request-errors.local.json
```

准备真实复验时由用户手动启动：

```powershell
.\venv\Scripts\python.exe -B -m src.evaluation.experiment run --config experiments/smoke-request-errors.local.json --allow-model-calls
```

内容审核仍可能使部分任务失败；本次改进让原因和重试开销可见，不保证供应商接受所有请求。若新目录已经存在，应按新实验冻结信息使用 `resume`，不能覆盖旧输出。
