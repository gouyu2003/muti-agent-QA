# Multi-Agent QA

基于 LangGraph 的多智能体协同问答系统，已实现运行轨迹持久化、配置驱动的协作流程、用户材料导入与知识源版本管理，并增加阶段 4.0 的问答修复与最小 HotpotQA 批量评测。当前自适应方法标识为 `multi_agent_adaptive_v1`。

系统使用 Planner、Retriever、Answer-Validator 三个智能体，支持“固定三节点”和“反馈重试”两种流程。知识源可选择预置 HotpotQA 记录，也可通过界面或 CLI 导入一组英文 UTF-8 TXT/MD 文件。用户可以选择知识源及版本、测试问答，并从实时结果和历史记录追溯到具体文件与原文位置。

原 V0 的真实 HotpotQA `bridge` 样本仍作为默认演示与回归基线保留。反馈重试流程支持证据不足时补充检索、证据冲突时重新规划；固定流程在一次验证后结束。

阶段 4.0 的 Planner 允许 1–4 个必要子问题；Retriever 继续使用本地 BM25，有命中时显示“待验证”，由 Validator 判断能否回答原问题。`system-design/agents/retriever.json` 的 `retrieval_config.top_k` 默认 3，必须是正整数。第二跳优先检索从前一步原文中识别的新实体对应材料。HotpotQA 最终引用包含标题与零起始句子索引，TXT/MD 引用保留文件、版本和行号；服务器只回填能唯一定位的原文。

阶段 4.0 的验收与复跑说明见 [docs/stage4-validation.md](docs/stage4-validation.md)。最小实验入口为 `python -m src.evaluation.batch`，输入数据与实验数据库保存在被 Git 忽略的 `data/experiments/`，固定样本清单和可审阅报告保存在 `docs/`。

## 系统结构

```text
React + TypeScript (frontend/)
  ├─ 实时问答：选择流程、知识源和版本
  ├─ 运行历史：轨迹、证据、统计、JSONL 导出
  └─ 知识源：多文件上传、版本查看、片段预览
          │ HTTP / SSE
          ▼
FastAPI (backend/)              CLI (main.py / src.knowledge.cli)
          │                               │
          ├─────────── 共用服务 ────────────┤
          │                               │
          ▼                               ▼
RunService + 配置驱动 Runtime       知识源导入服务
          │                       解析 → 切分 → 建索引 → 发布版本
          ▼                               │
LangGraph                                 ▼
  Planner → Retriever → Answer-Validator   知识源 SQLite + 原件/片段/索引
  固定流程：一次验证后结束                  data/knowledge/
  反馈流程：证据不足返回检索，冲突返回规划     │
          │                                │
          ├─ Retriever：本地 BM25 ←─────────┘
          │  选择预置 HotpotQA 或固定版本的用户文档
          ▼
运行记录 + 有序轨迹 SQLite
data/runtime/runs.sqlite3
```

前端负责用户交互，后端提供接口适配；智能体、路由、Runtime 和知识源导入逻辑位于 `src/`。问答 API 与 CLI 共用 `RunService`，界面上传与 CLI 导入共用知识源导入服务。运行记录库与知识源管理库分别保存，现有命令行入口 `python main.py` 保持可用。

## 环境要求

- Python 3.12
- Node.js 22
- npm 10+
- 可访问的 OpenAI 兼容模型接口及其 API Key；项目示例使用阿里云百炼托管的 DeepSeek

项目不会提交 `.env`。先复制 `.env.example`，再在本机填写真实配置。

## 安装

### Python

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --timeout 120 --retries 10 -r requirements-api.txt
Copy-Item .env.example .env
```

`requirements-api.txt` 会继承现有核心依赖，并增加 FastAPI、Uvicorn 与 multipart 文件上传依赖。

### 模型配置

在项目根目录的 `.env` 中填写以下变量，变量名保留现有 DeepSeek 命名：

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 模型接口的真实 API Key |
| `DEEPSEEK_BASE_URL` | OpenAI 兼容接口地址，按服务提供方填写 |
| `DEEPSEEK_MODEL` | 智能体配置未指定模型名时的备用值 |

模型名优先使用 `system-design/agents/` 下智能体 JSON 的 `model_config.model_name`，只有该值缺失或为空时才使用 `DEEPSEEK_MODEL`。当前预置配置指定了 `deepseek-v4-flash-0731`/`deepseek-v4.1-flash`/；切换模型时需更新相应智能体的配置，仅修改 `.env` 中的 `DEEPSEEK_MODEL` 不会覆盖 JSON 中的模型名。

后端与 CLI 启动时加载 `.env`。单纯导入材料和本地检索不调用外部模型；执行问答需要模型连接配置。

### 前端

```powershell
Set-Location frontend
npm install
```

## 启动与使用

使用两个 PowerShell 终端。

终端一，在项目根目录启动后端：

```powershell
.\venv\Scripts\Activate.ps1
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

终端二，启动前端：

```powershell
Set-Location frontend
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>。页面会自动加载 HotpotQA 示例问题，点击“开始协同问答”即可实时查看三个智能体的执行轨迹、问题拆解、检索证据和最终验证结果。

使用自己的材料时，进入“知识源”页面，创建知识源并上传文件，再点击“用于问答”。在实时运行页选择流程、核对知识源版本并输入英文问题。切换知识源会保留已输入的问题；用户文档不提供自动生成的示例题。运行期间流程、知识源和版本选择会锁定。

后端交互文档位于 <http://127.0.0.1:8000/docs>。

## 选择流程与知识源（阶段 2）

系统清单位于 `system-design/systems/default.json`。默认流程 `multi_hop_qa_v0` 保留反馈重试；`multi_hop_qa_fixed` 在一次验证后结束。默认知识源 `hotpotqa_fixture` 使用真实 V0 样本；`demo_fixture_002` 是用于验证知识源切换的**合成演示材料**，不能用于论文评测。

命令行可以选择流程和知识源：

```powershell
.\venv\Scripts\python.exe main.py --workflow-id multi_hop_qa_fixed --source-id demo_fixture_002 "Which city contains Westhaven University?"
```

`POST /api/v1/qa/stream` 的 JSON 请求可以增加可选的 `workflow_id` 和 `source_id`；只传 `question` 使用当前默认配置。`multi_hop_qa_v0` 保留为兼容流程 ID，当前执行 V1，实际方法以快照中的 `method_id` 和协议版本为准。原始 V0 需要从冻结源码独立复跑。也可通过 `MAQA_SYSTEM_CONFIG_PATH` 指向另一份系统清单，命令行还支持 `--system-config`。清单中的文件路径相对于清单所在目录解析。未知流程、知识源、组件或非法连线会在开始运行前报错。

运行历史、详情、统计和 JSONL 导出分别通过 `/api/v1/runs`、`/api/v1/runs/{run_id}`、`/api/v1/stats/summary` 和 `/api/v1/exports/runs.jsonl` 访问。
每条运行记录保存实际执行的流程与知识源配置快照、配置 SHA-256 和知识文件 SHA-256。`code_version` 默认包含 Git 提交和当前 Python 运行时代码的 SHA-256；即使代码尚未提交，指纹也会随代码内容变化。无 Git 仓库时仍记录源码指纹；部署构建也可通过 `MAQA_CODE_VERSION` 增加构建标识，源码指纹始终保留。历史详情页展示这些标识，便于核对运行所用版本。

运行记录与轨迹默认保存到 `data/runtime/runs.sqlite3`，可通过 `MAQA_RUN_DB_PATH` 指定数据库路径。问答 API 与 CLI 使用同一数据库时，其运行记录会出现在同一历史列表中；默认数据库及其 SQLite 辅助文件已加入 Git 忽略规则。

## 导入用户材料（阶段 3）

在“知识源”页面填写名称并上传英文 UTF-8（可带 BOM）的 `.txt`、`.md` 文件。
一份知识源可以包含多个文件：最多 10 个文件，单文件 2 MiB，总计 10 MiB，最多 20,000 个片段。
导入完成后可查看文件清单和带行号的片段预览，点击“用于问答”选择该知识源及版本。
材料无需包含题目、标准答案或支撑事实；实时页会展示文件数和片段数，不生成 HotpotQA 指标或示例题。

TXT 按空行分段，Markdown 识别以 `#` 至 `######` 加空白开头的标题并保留其层级；代码围栏内的标题标记按正文处理。下划线式标题（Setext）暂不识别为标题层级。正文作为普通文本解析，不执行 HTML、不读取外部链接。
长段落在空白边界切分，每片段最多 600 字符，不重叠；单个超长词按字符切分。
行号从 1 开始；字符位置基于去除 BOM、统一换行后的文本，采用起始包含、结束不包含的区间。
原始文件同时保留，证据正文始终从实际索引片段回填。

更新知识源时选择“导入新版本”，上传整套材料。旧版本保留；失败版本不能运行，上一个成功版本继续可用。
问答开始前固定所选版本，实时证据和运行历史显示知识源版本、文件、标题位置和行号。
普通文档使用本地 BM25；为了支持很小的语料，普通文档的非正 IDF 使用正数下限，HotpotQA 评分不变。
`verified` 是模型的内部验证结果，不能代替标准答案准确率。

CLI 使用同一个导入服务。在项目根目录执行：

```powershell
$knowledgeImport = .\venv\Scripts\python.exe -m src.knowledge.cli create --name "Cedar Institute notes" data/examples/stage3/people.md data/examples/stage3/institute.txt | ConvertFrom-Json
$knowledgeImport.source_id
.\venv\Scripts\python.exe -m src.knowledge.cli list
.\venv\Scripts\python.exe main.py --workflow-id multi_hop_qa_fixed --source-id $knowledgeImport.source_id --source-version 1 "Which city hosts the institute founded by Mara Vale?"
.\venv\Scripts\python.exe -m src.knowledge.cli add-version $knowledgeImport.source_id data/examples/stage3/people.md data/examples/stage3/institute.txt
```

上述材料是自建合成演示语料。预期多跳答案为 Northbridge，开设年份为 2008，材料没有提供 Mara Vale 的配偶信息。

使用用户知识源运行 CLI 问答时必须明确提供英文问题；不传问题的默认 HotpotQA 演示行为只适用于原有入口。

默认存储为 `data/knowledge/knowledge.sqlite3` 和 `data/knowledge/<source_id>/<version>/`，已加入 Git 忽略规则。
可用 `MAQA_KNOWLEDGE_DIR` 指定目录；CLI 导入也支持全局 `--knowledge-dir` 和 `--system-config`。
自定义目录时，问答 API/CLI 需使用同一个 `MAQA_KNOWLEDGE_DIR`；CLI 全局参数放在 `create`、`add-version` 或 `list` 子命令之前。用户知识源按系统清单的 `system_id` 管理，导入与问答应使用同一系统配置。
支持本地单用户、单后端进程；导入为同步操作，重启后中断的导入标记为失败，可重新导入。

HTTP 接口：

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/knowledge/sources` | multipart 字段 `name`、多个 `files`，创建知识源 |
| `GET /api/v1/knowledge/sources` | 查看知识源列表 |
| `GET /api/v1/knowledge/sources/{source_id}` | 查看版本、文件概况和错误 |
| `POST /api/v1/knowledge/sources/{source_id}/versions` | multipart 多个 `files`，导入整套材料的新版本 |
| `GET /api/v1/knowledge/sources/{source_id}/versions/{version}/chunks` | 使用 `limit`、`offset` 分页预览片段 |

已创建版本的导入失败返回 HTTP 422，`detail` 包含 `source_id`、`version`、`code` 和 `message`。名称校验等创建前错误没有版本 ID；缺少必填表单字段时使用 FastAPI 的请求校验错误格式。访问不存在的知识源返回 HTTP 404。
问答请求可增加正整数 `source_version`；省略时使用最新成功导入的版本。预置 HotpotQA 记录没有文档版本，向其提交 `source_version` 会被拒绝；不存在或未就绪的用户文档版本也不能运行。
`/api/v1/system/options` 返回已绑定且可用的文档源和版本；普通文档的题型、标准支撑事实与示例题为 `null`。

## 验证

后端与现有核心测试：

```powershell
.\venv\Scripts\python.exe -m pytest
```

常规测试默认排除 `integration` 标记的真实模型测试。Windows 下若遇到 pytest 临时目录权限问题，可增加 `--basetemp .pytest-tmp`。

前端：

```powershell
Set-Location frontend
npm run type-check
npm run test
npm run build
```

真实模型集成测试会产生外部调用，默认不执行；需要时显式运行：

```powershell
.\venv\Scripts\python.exe -m pytest -m integration -v
```

当前包含两个真实模型测试：

- 原有 API/CLI 回归：分别通过流式 API 和实际 CLI 进程执行一题，核对答案、相同 `run_id` 的 SSE/轨迹/历史详情，以及两个流程模板和两个预置知识源。
- 用户文档验收：通过上传 API 导入两份英文材料，固定文档版本后执行单跳、跨文件多跳和材料无答案三题，并检查结果及引用来源字段。

缺少模型连接配置时测试会跳过；跳过不代表完成真实模型验收。测试使用临时运行数据库；文档验收还使用独立临时知识目录。

只运行用户文档真实模型验收时，在项目根目录执行：

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_integration_deepseek.py -m integration -k document --basetemp .pytest-live-stage3 -v
```

阶段 3 的已记录验收结果和人工证据核对见 [阶段 3 验收记录](docs/stage3-validation.md)，实际模型输出见 [验收结果 JSON](docs/stage3-live-results.json)。该记录注明执行日期与验收限制，不代表每次复验都会产生相同模型输出。

## 当前边界

当前支持已注册的三类智能体、两种流程模板、HotpotQA 单条记录和英文 TXT/MD 多文件知识源。
前端提供实时问答、运行历史和知识源导入、版本查看。DOCX/PDF、中文检索、OCR、向量检索和多知识源联合检索尚未实现。
系统包导出、异机启动、智能体配置编辑、拖拽式设计器和稳定的非流式对外问答 API 属于后续阶段。
