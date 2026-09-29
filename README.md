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

`requirements-api.txt` 会继承现有核心依赖，并增加 FastAPI 与 Uvicorn。现有 `requirements.txt` 保持不变。

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

当前支持已注册的三类智能体、两种流程模板以及单条 HotpotQA 格式记录的本地 BM25 检索。前端仍是 V0 实时演示和运行历史，没有配置编辑、用户材料上传或拖拽式设计器。系统包导出、异机启动和稳定的非流式对外问答 API 属于后续阶段。
