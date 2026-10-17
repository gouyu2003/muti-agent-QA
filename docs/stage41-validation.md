# 阶段 4.1：用户问答系统配置管理验收

验收日期：2026-10-17（Asia/Shanghai）。本阶段在现有工作区的实验历史接入改动上继续开发，没有提交或推送 Git。

## 用户操作

打开 `http://127.0.0.1:5173/`，进入顶部“我的系统”。

1. 在“知识源”导入英文 UTF-8 TXT/MD 材料，或使用预置材料。
2. 在“我的系统”点击“新建系统”，填写名称、说明，选择现有流程和知识源。用户材料必须明确选择版本。
3. 点击“创建草稿”或“保存草稿”。保存不等于发布，未保存修改会显示提示，并禁用试运行和发布。
4. 点击“试运行已保存草稿”进入问答页；页内显示草稿修订号，流程和知识源不能临时覆盖。
5. 回到系统页面，发布 `v1`。修改并保存草稿后可以发布 `v2`，`v1` 保持不变；重复发布同一个修订返回已有版本。
6. 在“已发布版本（只读）”选择版本，点击“使用此版本问答”。旧问答入口保留为“临时问答”。
7. 点击“查看此系统历史”，或在历史页按“系统筛选”查看记录。列表、详情和 JSONL 导出保留当时的名称、发布版本或草稿修订号。

三个智能体的模型、参数、提示词和所选协议随配置保存，界面只读展示；本阶段没有智能体编辑、动态节点、协议编辑或用户账号。

## 存储、迁移与版本语义

- 系统数据库默认 `data/systems/systems.sqlite3`，环境变量 `MAQA_SYSTEMS_DIR` 可覆盖所在目录。系统草稿修订从 1 递增，发布版本按系统独立递增。
- 保存时冻结模板内容；名称、说明等修改不触发已保存智能体配置的隐式更新。发布复制草稿快照，运行从快照构图，不读取最新模板替代旧配置。
- 用户材料绑定具体版本、语料与索引哈希；HotpotQA 预置材料绑定内容哈希。缺失或篡改时拒绝试运行/发布/启动该版本，不能回退到最新材料。
- 现有知识数据库迁移到 `PRAGMA user_version=2`，增加 `workspace_id='local'`。原来源 ID、版本、文件位置保持不变；旧 `system_id` 和 bindings 表保留兼容信息，不再限制本机工作区共享。
- 迁移前使用 SQLite backup API 保存 `data/knowledge/knowledge.pre-workspace-<时间>.sqlite3`，包含已提交的 WAL 内容。建表、加列、版本更新在事务内执行；失败回滚；再次初始化不会重复迁移或备份。
- 本机实际迁移备份：`knowledge.pre-workspace-20261017220820339848.sqlite3`。迁移回滚与旧格式读取使用独立测试库验证。
- 运行快照新增可选 `system_ref`，包含 `system_id/name/kind/system_version/draft_revision`；不增加运行库业务列。原 `_run_metadata` 中的实验与样本信息保持兼容。
- 旧记录不会被推断为新系统的发布版本。实验归档继续只读，不参与迁移。

## 接口与 CLI

系统管理接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET / POST | `/api/v1/systems` | 列表、创建草稿 |
| GET | `/api/v1/systems/{id}` | 当前草稿、修订与发布版本 |
| PUT | `/api/v1/systems/{id}/draft` | 保存草稿，携带 `expected_revision` |
| GET / POST | `/api/v1/systems/{id}/versions` | 列表、发布，发布携带 `expected_revision` |
| GET | `/api/v1/systems/{id}/versions/{version}` | 读取不可变版本 |

创建/保存字段为 `name`、`description`、`workflow_id`、`source_id`、`source_version`。草稿可以尚未选择完整配置；试运行和发布要求完整且校验通过。接口不接受客户端提交任意快照或修改智能体内容。

`POST /api/v1/qa/stream` 示例：

```json
{"question":"In what year was the Orion project launched?","system_id":"sys_d14bd0cce54344c58d7f721ef20a719d","system_version":"v1"}
```

草稿试运行使用 `system_id + draft_revision`。发布版本与草稿选择互斥；系统模式下不能覆盖流程、知识源或系统配置文件。过期修订返回 409，未知系统/版本返回 404，无效组合返回 422。

```powershell
venv/Scripts/python.exe main.py "In what year was the Orion project launched?" --system-id sys_d14bd0cce54344c58d7f721ef20a719d --system-version v1
```

也可使用 `--draft-revision` 代替 `--system-version`。系统模式必须明确提供问题。未提供系统选择的旧 API、CLI 与批量入口保持兼容。

`/api/v1/runs`、`/api/v1/stats/summary`、`/api/v1/exports/runs.jsonl` 新增同义的 `system_id` 筛选，可与状态、入口、方法、时间和实验筛选组合。

## 自动测试

最终结果：**218 项后端离线测试通过，3 项既有真实模型测试默认跳过；28 项前端测试通过；TypeScript 检查和生产构建通过。**

新增覆盖：两套系统持久化与共享固定材料版本、材料升级后的旧版本运行、模板冻结、发布幂等、并发修订冲突、不完整草稿、缺失/篡改材料、未知版本、迁移备份/幂等/事务回滚、CLI 参数互斥、SSE，以及完成/失败/取消记录的系统关联。列表、统计、详情和导出的一致性及旧实验读取一并回归。

```powershell
venv/Scripts/python.exe -m pytest --basetemp=.pytest-tmp-stage41-final
New-Item -ItemType Directory -Force .pytest-tmp-stage41-vitest | Out-Null
$env:TEMP = (Resolve-Path .pytest-tmp-stage41-vitest).Path
$env:TMP = $env:TEMP
npm --prefix frontend test
npm --prefix frontend run build
```

构建仍有单个 JS 分包超过 500 kB 的体积提示；构建成功。当前本机单工作区规模未增加分包改造。

## 浏览器与真实模型验收

通过独立 headless Chrome 对实际开发站点操作，创建了两套可在“我的系统”读取的验收系统。

| 系统 | ID | 配置与结果 |
| --- | --- | --- |
| 阶段4.1 · 研究问答验收 | `sys_f45fbc0a3aef4338bc523bc4adec5978` | 自适应流程、研究资料 v1；草稿 r1 试运行成功；随后发布 v1、保存 r2、发布 v2，并切回 v1 |
| 阶段4.1 · 项目问答验收 | `sys_d14bd0cce54344c58d7f721ef20a719d` | 固定三节点流程、项目资料 v1；发布 v1 后真实问答成功 |

| 运行 | 版本关联 | 答案 | 配置 SHA-256 |
| --- | --- | --- | --- |
| `a8f4566c-ef19-44cd-aae9-894249458f69` | 研究系统草稿 r1 | Northbridge | `eaa3bd4cfa7f34acccb32143dea24bb080f69f7045f9ccd8f55bd544d9caa26d` |
| `662705d2-9db4-47cf-877d-5ef714a37f63` | 项目系统 v1 | 2019 | `95040b549e4f8ec3164a2e414444992d3d445dd76050c3b1afc1c151609286b0` |

两次均 completed、verified=true，各 2 次模型调用；用量分别为 1478 和 2847 tokens，耗时分别约 12.5 秒和 23.5 秒。每条接受的引用均与其绑定语料的原文、来源 ID、版本、document_id、chunk_id 及行号核对一致。题目是本地验收材料，不据此评价通用问答准确率。

浏览器确认未保存修改禁用试运行/发布，已发布版本切换有效，系统模式锁定流程与知识源，历史可定位草稿 r1 和发布 v1，1440px/800px 均无横向溢出。浏览器刷新和后端实际重启后，两套系统的草稿、全部发布版本和两条完整运行轨迹一致。

旧实验 V0/V1 各 10 条、共 20 条仍可显示，包括 V0 的 1 条失败及其轨迹。两份实验数据库重启前后 SHA-256 不变，没有重跑 Smoke-10。

机器可读结果见 [stage41-live-results.json](stage41-live-results.json)。本地截图、服务日志和验收脚本位于被忽略的 `.pytest-tmp-stage41-ui/`；用户材料、系统数据库和运行数据库保持本地存储，不随 Git 上传。
