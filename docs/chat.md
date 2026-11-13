# 连续对话实现说明

## 使用方式与配置

前端入口为 `/chat`，已有会话地址为 `/chat/:conversationId`。首次发送前，从“我的系统”选择一套系统及其一个已发布版本或已保存草稿修订；两者只能选其一。创建会话时服务端冻结当时的系统快照和上下文解析配置，后续各轮使用该快照，不随系统之后的修改变化。会话可重命名、查看配置和删除；运行中的会话须先停止再删除。

每次提交生成一对用户/助手消息及一个独立 Run，回答可查看引用与运行详情。会话和消息保存在运行时 SQLite 数据库的 `conversations` 和 `messages` 表；Run 仍由现有运行记录管理。删除会话会删除其消息，但保留关联 Run 和运行历史。创建和发送使用 `client_request_id` 防止相同请求重复创建；同一编号对应的内容改变时会报错。

## 上下文与模型费用

每轮从较新的历史中选择最多三组完整且均已完成的用户/助手消息，总内容最多 6000 字符；不截断已保存消息。原问题及解析主题可帮助理解后续提问，但历史不是事实证据。没有历史的首轮直接作为独立问题，不调用上下文解析模型；存在已完成历史但无法放进窗口时，要求用户补充主题。

有可用历史时，在获准执行的 Run 内调用上下文解析模型，返回结构化的意图、独立问题、澄清问题和使用的消息 ID。解析器处理追问、指代和格式要求；无法确定时返回澄清，停止该轮后续问答；无效结构或无效消息 ID 会使该轮失败，不以未经解析的歧义问题静默继续。非澄清结果的独立问题交给所选系统的正常问答流程。

上下文模型从冻结快照中按角色 `planner`、`lead`、`answer_validator`、`expert` 的顺序选择第一个配置了模型名的实例；同角色按实例顺序选择。它使用所选实例的模型配置，覆盖温度为 0、最大输出 token 为 512，并使用专门的解析提示词。上下文调用计入同一 Run 的 `chat_context` 请求数、token、耗时和错误统计。当前交互运行未配置单价计算，`estimated_cost_usd` 延续原有未知值（null），不根据 token 数伪造费用。普通系统问答和上下文解析可能调用真实模型并产生费用；本地 BM25 检索保持原有实现，此功能不引入 embedding 检索。

## 接口与运行控制

| 方法和路径 | 用途 |
| --- | --- |
| `POST /api/v1/conversations` | 传入 `system_id`、`system_version` 或 `draft_revision` 以及 `client_request_id`，创建并冻结会话 |
| `GET /api/v1/conversations` | 列出会话 |
| `GET /api/v1/conversations/{conversation_id}` | 读取会话及冻结配置 |
| `PATCH /api/v1/conversations/{conversation_id}` | 使用 `title` 重命名 |
| `DELETE /api/v1/conversations/{conversation_id}` | 删除会话及消息，保留 Run |
| `GET /api/v1/conversations/{conversation_id}/messages` | 按顺序读取消息 |
| `POST /api/v1/conversations/{conversation_id}/messages` | 传入 `question` 和 `client_request_id`，提交一轮并返回 Run 和消息标识 |
| `GET /api/v1/runs/{run_id}` | 查看运行详情 |
| `GET /api/v1/runs/{run_id}/status` | 查询后台运行状态 |
| `GET /api/v1/runs/{run_id}/events/stream` | 通过 SSE 订阅运行事件 |
| `POST /api/v1/runs/{run_id}/cancel` | 请求取消运行 |

聊天和现有实时问答共享单个交互式运行的准入限制；已有交互式 Run 运行时，新提交会被拒绝并提供活动 Run 信息。后台执行独立于浏览器连接。界面跟随 SSE 事件并在连接中断时重连；停止按钮请求取消，终态同步到消息。恢复检查会将已退出的运行标为中断或取消，保留已保存结果，不自动重发未确认的模型请求。客户端保存待确认提交及请求编号，以便刷新或连接问题后恢复提交。

## 来源与授权

`src/runtime/chat_messages.py` 仅将 Hello Agents / StockSage 的消息字典转换和对话转 LLM 消息两个小辅助功能适配为 mapping 输入；持久 ID 和时间戳由 SQLite 管理。没有引入其完整会话管理器作为依赖。来源、固定提交、具体路径、修改说明及 CC BY-NC-SA 4.0 授权文本见 `third_party/hello-agents/NOTICE.md` 及同目录文件；使用该适配代码时应遵循其授权条件。

## 本地验证

以下命令在项目根目录的 PowerShell 中执行，使用本项目 Windows 虚拟环境，禁止测试期间发起模型调用：

```powershell
$env:MAQA_DISABLE_MODEL_CALLS = '1'
.\venv\Scripts\python.exe -m pytest tests/test_chat.py -q
Set-Location frontend
npm.cmd test -- --run src/ChatView.test.tsx
npm.cmd run type-check
npm.cmd run build
```

浏览器验收使用隔离的后端 8013、前端 5174 端口及测试夹具，覆盖桌面和移动端交互；这不是实际模型回答或真实模型效果的证据。实际问答的费用和效果需要另行验证。
