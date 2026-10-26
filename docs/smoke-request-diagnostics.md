# Smoke 失败请求的独立诊断

注意：本工具要求当前源码指纹与被诊断实验完全一致。正式运行接入错误分类后，旧 `baseline-smoke-001` 的重建会被拒绝；不要解除校验。已有诊断保留在独立 JSONL 中，后续使用新实验复验，参见 [模型请求错误与 smoke 复验](model-request-errors.md)。以下命令仅适用于与当前源码一致的冻结实验。

本工具只诊断已冻结的真实 smoke 实验中，首次尝试失败且标记为 `request_error` 的审核或质疑节点。一次最多选择两个节点。原始实验目录、SQLite、账本、报告和首次尝试成绩均不修改，也不执行原实验的 resume。

在项目根目录的 VS Code PowerShell 终端运行离线预览：

```powershell
.\venv\Scripts\python.exe -B scripts/diagnose_smoke_requests.py
```

默认读取 `data/experiments/baseline-smoke-001`。预览重建提示词并显示其哈希、长度、方法和节点，`model_calls` 应为 0。它不读取模型凭证，也不写诊断文件。源码、模板、依赖、冻结资料或数据校验失败时拒绝重建。

用户手动执行真实诊断：

```powershell
.\venv\Scripts\python.exe -B scripts/diagnose_smoke_requests.py --allow-model-calls
```

这会产生新的真实模型请求和可能的费用：每个节点只调用一次，SDK 重试和输出修正均不执行。只发送原失败节点的重建提示词，不重新运行前面的规划或专家节点。模型、温度、输出上限、结构化输出及思考设置沿用冻结配置和当前已核验源码；单次请求超时为 120 秒。当前端点必须与原实验一致。

原始完整请求文本没有归档，SQLite 会排序 JSON 对象的字段。本工具使用保存的节点输入重建相同语义和材料的提示词，但其中协作消息的 JSON 字段顺序可能与当时不同，不能声称与原请求逐字节相同。预览及诊断均标明这一边界并记录新提示词哈希。

也可以仅诊断一个原始 run：

```powershell
.\venv\Scripts\python.exe -B scripts/diagnose_smoke_requests.py --run-id 119832e2-a5f7-456a-8925-87e59cab15f1 --allow-model-calls
```

诊断逐条打印并另存到 `data/experiments/diagnostics/*.jsonl`，其中包含原 run ID、提示词哈希、新请求时间，以及异常类型、HTTP 状态码、服务端错误码、错误信息和新 Request ID（供应商提供时）。密钥和端点脱敏；不保存完整提示词、回复正文、请求头或原始响应体。

- `content_filter`：供应商明确返回 `DataInspectionFailed` / `data_inspection_failed`。
- `bad_request_details_required`：HTTP 400，但还需根据 `provider_code`、`message` 确认原因。
- `request_succeeded`：本次请求获得正常 SDK 响应；没有验证答案或 JSON schema，不能当作原实验补跑成功。
- `model_calls: 0` 的错误表示客户端构造等调用前检查失败；没有发出模型请求。

退出码：0 为预览成功或全部诊断请求获得正常响应；1 为至少一个诊断请求异常（HTTP 400 也会返回 1，这是诊断结果）；2 为预检查失败；130 为用户中断。中断时已发出的远端请求可能仍计费。

新的诊断结果不能还原历史 HTTP 响应。即使重现相同错误，也需区分新旧 Request ID；若本次全部成功，原先两次失败的具体原因仍不能据此确定。
