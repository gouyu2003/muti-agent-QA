# 阶段 3 验收记录

日期：2026-09-30（Asia/Shanghai）。

首版支持英文 UTF-8 TXT/MD，多文件知识源、不可变版本、CLI/HTTP 导入、本地 BM25、片段定位和历史查看。
用户数据默认保存在已忽略的 `data/knowledge/`。本记录使用的是项目自建合成语料，不是用户资料或论文评测集。

## 自动验收

- 后端离线测试：162 passed，2 个真实模型测试默认排除。
- 前端测试：21 passed。
- 前端 TypeScript 检查和生产构建通过。
- 覆盖编码、BOM、换行、长段落、Markdown 标题/代码围栏、文件大小/数量/片段上限、失败导入、索引失败、重启恢复、旧版本、篡改检测、跨文件真实图执行、引用回填、上传页面和版本选择。
- CLI 使用实际子进程导入并读取同一个持久化知识库；API 测试使用固定模型输出验证图执行、SSE 与运行历史。

## 真实模型验收

通过生产 API、实际模型连接和独立临时数据库导入 `data/examples/stage3/people.md` 与 `institute.txt`。
每题使用 `multi_hop_qa_fixed`、文档版本 1，均完成一次三个节点的执行。

| 场景 | 实际结果 | 证据核对 | 耗时 |
| --- | --- | --- | --- |
| Cedar Institute 开设年份 | `2008`，内部验证通过 | `institute.txt` 第 1 行明确写出年份 | 12.141 s |
| Mara Vale 创办机构所在城市 | `Northbridge hosts Cedar Institute, founded by marine biologist Mara Vale.`，内部验证通过 | `people.md` 第 5 行说明创办关系；`institute.txt` 第 1 行说明城市 | 19.785 s |
| Mara Vale 配偶 | `The provided evidence does not state who Mara Vale’s spouse is; no spouse is mentioned.`，内部验证未通过，`insufficient` | 原材料没有配偶信息，模型没有输出配偶姓名 | 23.692 s |

已逐条查看实际答案及服务端回填的引用文本、文件名、行号和版本。缺失答案场景仍返回了一些相关度不足的支撑条目，但结论正确标记为证据不足；这些条目不构成配偶事实的证据。
同一原文可被不同子问题引用，保留子问题对应关系。
本次小语料检查证明数据流和定位能力，不证明一般问答准确率；`verified` 不是标准答案评价指标。

实际输出和 Token/调用指标保存在 [stage3-live-results.json](stage3-live-results.json)。
浏览器布局未进行人工视觉验收；上传和切换交互已由前端测试验证。

## 复验命令

```powershell
.\venv\Scripts\python.exe -m pytest --basetemp .pytest-tmp
Set-Location frontend
npm test -- --run
npm run build
```

在项目根目录显式执行真实模型验收（需要本机 `.env`，会调用外部模型）：

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_integration_deepseek.py -m integration -k document --basetemp .pytest-live-stage3 -v
```

本次命令结果为 1 passed、1 deselected；选中的测试包含上表三题。
原 V0 API/CLI 真实模型回归测试仍保留，但本次没有重复运行。
