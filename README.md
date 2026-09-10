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

前后端只负责展示和传输，三个智能体及路由逻辑仍位于 `src/`。现有命令行入口 `python main.py` 保持可用。

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

## V0 边界

当前演示版不包含配置编辑、知识库上传、算法切换、运行历史、用户登录、数据库和公网部署。协同协议 JSON 当前用于定义与校验，LangGraph 节点和边仍由 Python 构建；协议配置驱动建图属于后续 V1。
