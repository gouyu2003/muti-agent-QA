# 阶段 4.2：动态智能体实例与系统内配置管理

日期：2026-10-20（Asia/Shanghai）。代码实现、自动回归和接口侧真实模型核验完成。**浏览器人工验收按用户要求留待用户执行，本记录不将其视为已通过。** 未提交或推送 Git。

## 使用与支持范围

在“我的系统”创建或选择草稿，选择执行模板，从角色模板添加实例，再配置各实例。可编辑名称、说明、提示词、模型名、温度、Token 上限，以及检索角色的材料版本和 top_k；支持复制、删除、调整顺序和从其他系统复制。

| 执行模板 ID | 必需角色 | 执行规则 |
| --- | --- | --- |
| `multi_hop_qa_fixed` | Planner、Retriever、Validator 各 1 个 | 固定流水线 |
| `multi_hop_qa_v0` | 同上 | 证据不足补检索，冲突重规划 |
| `single_expert` | 1 个 Expert | 独立检索、作答并校验引用 |
| `multi_expert` | 1～3 个 Expert、1 个 Validator，可选 1 个 Planner | 串行或并行；全部专家完成后审核一次 |

每套系统保存 1～5 个实例。实例 ID 只含字母、数字、下划线或连字符，字母开头，最长 64 字符；复制生成新 ID，重命名不变更 ID。类型不可直接修改，需删除并从另一模板新建。首版角色组合由执行模板限定，不是自由图编辑器。

Expert、Retriever 各绑定一个固定材料版本。Planner 和 Validator 不独立检索；Retriever 不调用模型，不提供无效的提示词/模型编辑。模型凭证与连接地址继续来自服务器环境变量，实例配置只保存模型名及参数。模型请求超时 120 秒，空输出重试沿用实例的既有重试配置。

结构正确但角色或材料尚未补齐的草稿可以保存，返回 `configuration_errors`；不能试运行或发布。未保存修改同样不能试运行或发布。材料就绪、图结构和哈希在运行与发布前由服务端校验。

## 接口与存储

- `GET /api/v1/agent-templates` 返回四种角色的默认配置、输入输出字段和编辑能力。
- 原系统创建/保存接口新增 `agents` 列表及 `execution_mode`（`sequential`/`parallel`）。发布仍使用 `expected_revision`；运行仍指定系统 ID 加发布版本或草稿修订号。客户端不能提交任意图、代码、文件路径、凭证或执行快照。
- 新版草稿快照 `schema_version="2"` 保存实例、角色类型、编译流程、执行方式及按实例绑定的知识源版本和哈希。未传实例的旧调用继续兼容，旧发布版本在内存中读取，不重写历史内容或指纹。
- 多来源的运行摘要 `source_id=null`；多模型摘要 `model_name="multiple"`，前端显示“多个模型”。完整来源和模型逐实例展示。
- 每实例用量从 `metrics.agent_usage` 读取。SQLite 在结果 JSON 的保留键 `_agent_usage` 保存该信息，读取时还原到 metrics 并移除内部键；不修改旧运行表结构，也不迁移实验档案。
- 问答 API、CLI 与批量执行继续共用 RunService。原临时问答、两种流程及旧历史兼容。

创建单专家草稿的实例示例（省略系统名称等外层字段）：

```json
{
  "workflow_id": "single_expert",
  "execution_mode": "sequential",
  "agents": [{
    "agent_id": "medical_expert_1",
    "agent_type": "expert",
    "name": "领域专家",
    "description": "仅使用所选材料作答",
    "model_config": {
      "model_name": "deepseek-v4.1-flash",
      "system_prompt": "Answer using only supplied evidence; report missing evidence honestly.",
      "temperature": 0,
      "max_tokens": 4096
    },
    "source_id": "替换为已导入的材料 ID",
    "source_version": 1,
    "top_k": 3
  }]
}
```

## 执行、并发与失败语义

Registry 根据角色类型创建节点，实例 ID 不再绑定三个预置名称。服务端生成连线并验证模板角色和图契约；并行汇合等待全部专家，审核者只运行一次。

Expert 仅读取原问题、计划和自己的材料，不读取其他专家观点；串行执行也如此。结果写入 `expert_results[agent_id]`，使用一致的 reducer 合并到图状态和服务状态。汇总模板只有审核者写最终答案，单专家模板由该专家写最终答案。

每条新专家证据使用包含问题片段、来源、版本和原文位置的确定性 `evidence_id`；引用回填实际材料。审核者可以读取所有候选证据和独立观点，但观点不是事实证据，且不以多数投票替代证据判断。

轨迹使用实际实例 ID、名称和角色，前端支持同时显示多个运行中实例。LangGraph 启用即时事件输出，避免某个分支未结束时阻塞其他分支完成事件。

材料无答案、证据冲突属于正常完成但未验证的结果；节点或模型执行异常使整次运行失败，保留已完成输出。取消设置共享停止标记，阻止后续节点和重试，等待在途节点结束，再记录唯一终态；不承诺撤回供应商已经收到的请求。缺失用量保持未知，不将其记为零。

## 自动测试与接口侧真实核验

最终离线回归：**后端 239 passed，前端 33 passed。** 覆盖 1～5 个实例、自定义节点 ID、两种反馈重试、独立参数/材料、同步屏障验证并行、单次审核、同名片段隔离、互补材料的充分性合并、无答案/冲突、版本冻结、失败/取消收束，以及实例用量持久化。3 项既有 integration 测试按默认配置排除，不充当本阶段验收证据。

前端覆盖实例创建、复制、删除、跨系统复制、未保存/不完整草稿、修订冲突、动态并行状态和多来源发布系统运行入口。TypeScript 与生产构建通过；保留既有 JS 分包大于 500 kB 的体积提示。

本机 Windows Store Python 3.12 在沙箱内无法启动，在获准的外部执行环境可以正常运行；使用现有虚拟环境完成测试，没有更换项目依赖版本。前端 fork 测试进程出现启动超时，使用线程池完成回归。

```powershell
venv/Scripts/python.exe -m pytest -p no:cacheprovider --basetemp .pytest-tmp-stage42-release
npm --prefix frontend test -- --pool threads --maxWorkers 1 --testTimeout 15000
npm --prefix frontend run build
```

真实核验通过实际 HTTP/SSE 调用服务器和真实模型，未使用模型替身。测试资料为本地合成英文材料：地理材料给出 Cedar Institute 位于 Northbridge，历史材料给出创建于 2008。

| 场景 | 预期答案 | 无重试时的模型调用 |
| --- | --- | --- |
| 自定义流水线草稿 | Northbridge | 2 |
| 自定义流水线发布 v1 | Northbridge | 2 |
| Planner + 两位 Expert + Validator，串行草稿 | Northbridge、2008 | 4 |
| 同一专家系统并行发布 v2 | Northbridge、2008 | 4 |

空响应重试会增加实际调用与 Token；配套 JSON 保存实测值，不将逻辑节点数当作调用次数。

核对 SSE 与详情结果一致、引用对应固定材料、两专家并行重叠、审核者仅运行一次、旧 v1 不变、系统筛选与导出一致，以及每实例和总用量一致。四种场景之后另用最终源码复测并行 v2，记录独立源码指纹；各检查点的实际代码身份见配套 JSON。具体 run_id、配置指纹、实际模型与用量见 [stage42-live-results.json](stage42-live-results.json)。这些材料只验收工程行为，不代表通用问答准确率。

后端实际重启后，两套最终验收系统及五条完整运行记录的规范化内容 SHA-256 与重启前相同；包括草稿、全部发布版本、答案、实例用量和事件轨迹。最终并行复测的源码指纹与交付代码一致。

可复跑入口：`venv/Scripts/python.exe scripts/stage42_acceptance.py --allow-model-calls`。它会创建独立验收系统并产生真实模型调用，要求先按下一节设置隔离存储。

## 用户待执行的浏览器验收

当前开发服务地址为 `http://127.0.0.1:5173/`，后端为 `http://127.0.0.1:8000/`。本次服务使用被 Git 忽略的 `.pytest-live-stage42/` 作为隔离存储，保留真实验收系统与运行记录；正常启动时若不设置以下环境变量，将回到原有默认存储。

```powershell
$acceptanceRoot = Join-Path (Get-Location) '.pytest-live-stage42'
$env:MAQA_RUN_DB_PATH = Join-Path $acceptanceRoot 'runs.sqlite3'
$env:MAQA_SYSTEMS_DIR = Join-Path $acceptanceRoot 'systems'
$env:MAQA_KNOWLEDGE_DIR = Join-Path $acceptanceRoot 'knowledge'
$env:MAQA_EXPERIMENT_CATALOG_PATH = Join-Path $acceptanceRoot 'no-experiments.json'
venv/Scripts/python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

建议手动检查：

1. “我的系统”选择名称带“最终核验”的流水线或双专家系统，查看已保存实例与发布版本。
2. 新建草稿，添加、复制、删除、跨系统复制实例；调整专家顺序、专属提示词、模型参数和固定材料版本，保存后刷新。
3. 确认未保存修改、不完整角色和缺失材料不能发布或试运行；已发布版本只读。
4. 双专家在串行和并行下执行问答，查看独立结果、同时运行状态、审核结果及来源位置。
5. 打开系统历史，核对版本、实例配置、引用、每实例与总用量及 JSONL 导出；切换 v1/v2。
6. 检查桌面、平板和窄屏布局、键盘操作及错误提示。

这些页面操作和视觉布局尚未完成浏览器验收。Lead Agent、Debate、用户账号、任意节点代码、自由图编辑及记忆机制不在本阶段。

## 后续交互调整：协作流程默认实例

选择流程时，现在会为尚未修改的默认组合生成对应实例：两种流水线各为规划、检索、审核三个角色；单专家问答为一个专家；多专家汇总为规划者、专家 A、专家 B、审核者，默认串行。

读取已有草稿或修改过实例后，跨角色组合切换会要求选择使用新流程默认配置、保留现有实例或取消。两种流水线互切直接保留原实例 ID、提示词、模型参数及知识源绑定。新生成实例的知识源留待指定；保留不匹配的实例时即时提示缺少或超出的角色。切换仅修改编辑内容，需要保存草稿，已发布版本不变。

手动验收时，补充检查以上切换行为、弹窗取消及键盘操作。此前真实模型验收记录对应当时的代码检查点，本次前端交互调整不代表已重新完成浏览器或真实模型验收。

本次验证：前端 5 个测试文件、38 个用例全部通过（其中“我的系统”11 个用例，新增 5 个切换场景）；TypeScript 检查和生产构建通过，仍有大于 500 kB 的构建产物提示。首次完整测试停留在加载阶段后被终止，随后单文件测试及完整回归均通过。浏览器验收仍由用户自行执行。
