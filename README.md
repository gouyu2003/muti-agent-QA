# Multi-Agent QA

基于 LangGraph 的多智能体协同问答系统。当前 V0 使用 Planner、Retriever、Answer-Validator 三个智能体，在一条真实 HotpotQA `bridge` 问题上完成“规划 → 本地证据检索 → 回答与验证”的端到端闭环，并支持证据不足时补充检索、证据冲突时重新规划。

## 系统结构

```text
React + TypeScript (frontend/)
          │ POST /api/v1/qa/stream
          │ Server-Sent Events
          ▼
FastAPI adapter (backend/)
          │ LangGraph tasks + updates
          ▼
Planner → Retriever → Answer-Validator
   ▲          ▲              │
   └ conflict ┴ insufficient ┘
          │
          ▼
HotpotQA fixture + local BM25
```

前后端只负责展示和传输，智能体及路由逻辑位于 `src/`。API 与命令行共用 `RunService`，运行记录和有序轨迹持久化到 SQLite。现有命令行入口 `python main.py` 保持可用。

## 环境要求

- Python 3.12
- Node.js 22
- npm 10+
- 阿里云百炼兼容接口的 API Key

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

### 前端

```powershell
Set-Location frontend
npm install
```

## 启动组会演示

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

后端交互文档位于 <http://127.0.0.1:8000/docs>。

## 选择流程与知识源（阶段 2）

系统清单位于 `system-design/systems/default.json`。默认流程 `multi_hop_qa_v0` 保留反馈重试；`multi_hop_qa_fixed` 在一次验证后结束。默认知识源 `hotpotqa_fixture` 使用真实 V0 样本；`demo_fixture_002` 是用于验证知识源切换的**合成演示材料**，不能用于论文评测。

命令行可以选择流程和知识源：

```powershell
.\venv\Scripts\python.exe main.py --workflow-id multi_hop_qa_fixed --source-id demo_fixture_002 "Which city contains Westhaven University?"
```

`POST /api/v1/qa/stream` 的 JSON 请求可以增加可选的 `workflow_id` 和 `source_id`；只传 `question` 仍使用原 V0 默认值。也可通过 `MAQA_SYSTEM_CONFIG_PATH` 指向另一份系统清单，命令行还支持 `--system-config`。清单中的文件路径相对于清单所在目录解析。未知流程、知识源、组件或非法连线会在开始运行前报错。

运行历史、详情、统计和 JSONL 导出分别通过 `/api/v1/runs`、`/api/v1/runs/{run_id}`、`/api/v1/stats/summary` 和 `/api/v1/exports/runs.jsonl` 访问。
每条运行记录保存实际执行的流程与知识源配置快照、配置 SHA-256 和知识文件 SHA-256。`code_version` 默认包含 Git 提交和当前 Python 运行时代码的 SHA-256；即使代码尚未提交，指纹也会随代码内容变化。无 Git 仓库时仍记录源码指纹；部署构建也可通过 `MAQA_CODE_VERSION` 增加构建标识，源码指纹始终保留。历史详情页展示这些标识，便于核对运行所用版本。

## 导入用户材料（阶段 3）

在“知识源”页面填写名称并上传英文 UTF-8（可带 BOM）的 `.txt`、`.md` 文件。
一份知识源可以包含多个文件：最多 10 个文件，单文件 2 MiB，总计 10 MiB，最多 20,000 个片段。
导入完成后可查看文件清单和带行号的片段预览，点击“用于问答”选择该知识源及版本。
材料无需包含题目、标准答案或支撑事实；实时页会展示文件数和片段数，不生成 HotpotQA 指标或示例题。

TXT 按空行分段，Markdown 保留标题层级并把正文作为普通文本处理，不执行 HTML、不读取链接。
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

默认存储为 `data/knowledge/knowledge.sqlite3` 和 `data/knowledge/<source_id>/<version>/`，已加入 Git 忽略规则。
可用 `MAQA_KNOWLEDGE_DIR` 指定目录；CLI 导入也支持全局 `--knowledge-dir` 和 `--system-config`。
自定义目录时，问答 API/CLI 需使用同一个 `MAQA_KNOWLEDGE_DIR`。
支持本地单用户、单后端进程；导入为同步操作，重启后中断的导入标记为失败，可重新导入。

HTTP 接口：

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/knowledge/sources` | multipart 字段 `name`、多个 `files`，创建知识源 |
| `GET /api/v1/knowledge/sources` | 查看知识源列表 |
| `GET /api/v1/knowledge/sources/{source_id}` | 查看版本、文件概况和错误 |
| `POST /api/v1/knowledge/sources/{source_id}/versions` | multipart 多个 `files`，导入整套材料的新版本 |
| `GET /api/v1/knowledge/sources/{source_id}/versions/{version}/chunks` | 使用 `limit`、`offset` 分页预览片段 |

导入失败返回 HTTP 422，`detail` 包含知识源 ID、失败版本、错误码和说明（名称校验等创建前错误没有版本 ID）。
问答请求可增加 `source_version`；省略时使用最新可用版本。预置 HotpotQA 记录不使用此字段。
`/api/v1/system/options` 返回已绑定且可用的文档源和版本；普通文档的题型、标准支撑事实与示例题为 `null`。

## 验证

后端与现有核心测试：

```powershell
.\venv\Scripts\python.exe -m pytest
```

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

该测试分别通过流式 API 和实际 CLI 进程执行一题，使用独立临时数据库，核对答案、相同 `run_id` 的 SSE/轨迹/历史详情，以及两个流程模板和两个本地知识源。缺少模型连接配置时测试会跳过；跳过不代表完成真实模型验收。

## 当前边界

当前支持已注册的三类智能体、两种流程模板、HotpotQA 单条记录和英文 TXT/MD 多文件知识源。
前端提供实时问答、运行历史和知识源导入、版本查看。DOCX/PDF、中文检索、OCR、向量检索和多知识源联合检索尚未实现。
系统包导出、异机启动、智能体配置编辑、拖拽式设计器和稳定的非流式对外问答 API 属于后续阶段。
