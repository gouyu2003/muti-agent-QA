# 连续对话实现说明

## 使用方式与配置

前端入口为 `/chat`，已有会话地址为 `/chat/:conversationId`。首次发送前，从“我的系统”选择一套系统及其一个已发布版本或已保存草稿修订；两者只能选其一。创建会话时服务端冻结模型、知识源与协议等系统快照，后续各轮使用该快照，不随系统之后的修改变化。会话可重命名、查看配置和删除；运行中的会话须先停止再删除。

当前聊天策略为 `chat-hybrid-v2`。已有会话下一次新提交也使用该策略；原会话快照、原始上下文配置、旧消息和旧 Run 不重写。新 Run 在准入事务中从冻结系统快照生成并保存本轮完整的上下文与通用生成配置，包括提示词、模型参数、知识源描述和策略版本。已排队、进行中和重复请求编号对应的 Run 继续使用原配置。会话详情的 `chat_policy_version` 表示下一次新提交将使用的策略版本。

每次提交生成一对用户/助手消息及一个独立 Run，回答可查看引用与运行详情。会话和消息保存在运行时 SQLite 数据库的 `conversations` 和 `messages` 表；Run 仍由现有运行记录管理。删除会话会删除其消息，但保留关联 Run 和运行历史。创建和发送使用 `client_request_id` 防止相同请求重复创建；同一编号对应的内容改变时会报错。

## 上下文与模型费用

每轮从较新的历史中选择最多三组完整且均已完成的用户/助手消息，总内容最多 6000 字符；不截断已保存消息。原问题及解析主题可帮助理解后续提问，但历史不是事实证据。首轮与后续轮次都调用上下文理解与分流模型。历史超窗时，完整独立问题仍可回答，无法消解的追问请求澄清。

在获准执行的 Run 内，解析器结合当前问题、历史和冻结系统的知识源名称、文件元数据（每个源最多 30 个文件）判断语义，返回意图、独立问题、`knowledge_question`、`general_question`、澄清问题和使用的消息 ID。寒暄、能力询问、常识、一般建议、写作和代码统一为通用请求；学校政策、机构专属事实及明确要求基于资料的问题继续执行所选系统的原检索与验证流程。分流不能只按关键词或检索结果数量判断。指代不明时澄清，不自动执行事实问答。

混合请求先执行知识库部分，再调用通用生成；通用模型只读取本轮已验证结论或资料不足/冲突提示，不读取未经验证的原始答案。程序拼接两个段落，不让通用模型重写已验证答案。资料不足与冲突返回明确的用户说明，原答案和验证理由保留在节点轨迹中。通用生成失败时，该轮标记为失败，保留先前保存的知识库部分。新增调用使用现有可取消用量采集器与诊断机制，结构化输出最多修复一次，不因解析或请求失败自动切换回答路径。

上下文模型从冻结快照中按角色 `planner`、`lead`、`answer_validator`、`expert` 的顺序选择第一个配置了模型名的实例；同角色按实例顺序选择。解析使用温度 0、输出上限 1024 token；通用生成复用该模型，使用独立提示词、温度 0.3、输出上限 2048 token。分别计入同一 Run 的 `chat_context` 和 `chat_general` 请求、token、耗时与错误统计。纯通用问题通常两次模型调用，知识库问题增加一次分流调用，混合请求还会增加一次通用调用；结构修复和现有请求重试另计。当前交互运行未配置单价计算，`estimated_cost_usd` 保持 null。本地 BM25、普通运行、批量评测和独立 GSM8K 核心均保持原流程；没有增加联网搜索或 embedding 检索。

## 回复结构与验证统计

保留 `answer`、`verified`、`failure_type` 与引用字段。新增 `response_parts`，元素为 `{kind: "knowledge" | "general", content: "正文"}`；`answer` 同时保存可独立阅读的完整 Markdown。`verification_applicable=false` 表示纯通用或澄清，不能标记为验证通过；混合请求为 true，`verified` 仅对应知识库部分。聊天页与运行详情分段展示，通用部分不展示知识库验证通过标签或引用。新字段随 SSE 终态、消息、Run 详情及 JSONL 导出保存。

两套历史统计均返回 `verification_applicable_runs`（已完成且需验证的运行数），`verification_pass_rate` 为已验证运行数除以该值。总运行数、完成率、用量和耗时仍包含所有分支。分母为零时接口返回 0，界面显示“—”。无新字段的旧记录保留原解释，已标识的历史澄清不计入验证分母；旧空答案不回填。

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
.\venv\Scripts\python.exe -m pytest tests/test_chat.py tests/test_run_service.py tests/test_backend_api.py tests/test_experiment_history.py tests/test_sqlite_repository.py -q -p no:cacheprovider
Set-Location frontend
npm.cmd test -- --run src/ChatView.test.tsx src/AnswerContent.test.tsx src/HistoryView.test.tsx
npm.cmd run type-check
npm.cmd run build
```

浏览器验收使用隔离前端预览和模拟 API 数据，覆盖桌面、移动端、混合回复验证标签与零验证分母展示；不连接实际后端或模型。这不是实际模型回答或真实分流效果的证据，实际问答的费用和效果需要另行验证。
