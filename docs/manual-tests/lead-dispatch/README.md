# Lead Agent 主导分派：完整手动测试

用例编号：MANUAL-LEAD-001

目标：验证 Lead 根据专家职责选择必要专家、分派具体任务、读取专家结果、汇总后提交审核，并在运行历史中保留真实分派记录。

材料均为虚构测试数据。本说明为待执行的手动测试用例；材料解析检查不代表真实模型执行已通过。

## 测试结构

总计五个实例，达到当前系统的五实例上限：

- Lead Coordinator：主导智能体，选择专家并分派任务。
- Founder Expert：领域专家，绑定机构创办历史材料。
- Location Expert：领域专家，绑定机构所在地材料。
- Catalog Expert：领域专家，绑定图书目录材料；对主问题不相关。
- Final Reviewer：回答与审核，检查证据并输出最终答案。

主问题的两条必要事实分别保存在两个知识源中。Lead 需要调用 Founder Expert 和 Location Expert；Catalog Expert 应保留在候选名单中，但本题不应执行。

典型轨迹：coordinate（Lead 分派）→ work（被选专家执行）→ coordinate（Lead 汇总并提交审核）→ review → END。

work 保留模板默认的“并行后汇合”。同一轮有多个任务时并行执行；如果模型分两轮分派，允许出现第二轮 work。不要只凭轮数或界面中的“并行”字样判定实际分派是否正确。

## 前提

- 前后端已启动，页面显示“服务正常”，当前配置的模型接口可用。
- 使用新的测试对象；如已有同名对象，可以统一修改名称后缀。
- 三份材料需要分别创建三个独立知识源，不能合并导入成一个知识源。
- 所有材料使用英文 UTF-8 Markdown；主问题同样使用英文。
- 本用例只需沿用当前可用模型、默认系统提示词及默认模型参数。

## 1. 导入三个独立知识源

进入“知识源” /knowledge，逐个创建：

| 知识源名称 | 只上传这个文件 | 新建后的固定版本 |
| --- | --- | --- |
| Lead测试-创办历史-01 | materials/founder_history.md | 1 |
| Lead测试-机构所在地-01 | materials/institution_locations.md | 1 |
| Lead测试-图书目录-01 | materials/book_catalog.md | 1 |

每份知识源导入后均应显示“可用”，文件数为 1，按当前解析规则片段数为 1。

检查材料预览，第 3 行分别应为：

- founder_history.md：Mira Solen founded the Aster Vale Research Institute.
- institution_locations.md：The Aster Vale Research Institute is located in Bellhaven.
- book_catalog.md：Silver Lantern Atlas has catalog number BK-204.

## 2. 创建并发布 Lead 协议

1. 进入“协作协议” /protocols。
2. 点击“从 Lead Agent 主导分派 创建”。
3. 协议名称填写“Lead测试协议-01”。
4. 保留角色槽位：lead 1 人、experts 1～3 人、reviewer 1 人。
5. 保留模板的阶段、出口、执行方式和执行次数上限；保留证据共享。
6. 展开“执行阶段”中的 coordinate · lead，在“阶段任务说明”填写：

> 根据候选专家的名称和说明，只选择回答当前问题所必需的专家。为每位被选专家分派具体、可独立核实的子任务，不要把完整问题原样分发给所有专家。不调用职责不相关的专家。读取任务结果后检查问题所需的事实是否齐全；齐全时汇总并提交审核，仅在必要事实缺失时追加有界轮次。只能使用专家提供的原文证据，不能自行补造事实。

7. 点击“校验协议”，预期校验通过。
8. 点击“保存协议草稿”，再点击“发布协议版本”，确认发布版本 v1。

## 3. 配置五个实例

1. 进入“我的系统” /systems，点击“新建系统”。
2. 系统名称填写“Lead测试系统-01”。
3. “执行协议”选择“我的已发布协议”中的“Lead测试协议-01 · v1”；必要时点击“刷新协议列表”。
4. 模板会默认生成 Lead、两位领域专家、审核者四个实例。点击一次“添加领域专家”，补成五个实例。
5. 在“智能体实例”展开各实例，按下表填写实例名称、说明与材料绑定：

| 实例角色 | 实例名称 | 实例说明 | 知识源与固定版本 |
| --- | --- | --- | --- |
| 主导智能体 | Lead Coordinator | 根据专家名称和说明选择相关专家、分派具体任务并汇总结果。 | 不绑定知识源 |
| 领域专家 | Founder Expert | 负责机构创办人、创办历史查询；材料不包含机构所在地或图书目录。 | Lead测试-创办历史-01，版本 1 |
| 领域专家 | Location Expert | 负责机构所在城市查询；材料不包含机构创办人或图书目录。 | Lead测试-机构所在地-01，版本 1 |
| 领域专家 | Catalog Expert | 负责图书名称与目录编号查询；材料不包含机构创办人或所在地。 | Lead测试-图书目录-01，版本 1 |
| 回答与审核 | Final Reviewer | 审核最终答案，检查问题要求的各项事实与原文证据是否齐全。 | 不绑定知识源 |

三位领域专家均设置检索 top_k = 3。全部实例保留当前可用模型和默认系统提示词。

实例说明需要实际填入。当前 Lead 输入包含候选专家的实例 ID、名称和说明，模型据此选择任务对象。

6. 返回“执行协议”区域，核对槽位绑定：

| 槽位 | 绑定实例 |
| --- | --- |
| lead | Lead Coordinator |
| experts | Founder Expert、Location Expert、Catalog Expert 三位都选中 |
| reviewer | Final Reviewer |

新增的第三位专家也必须绑定到 experts。是否在本题执行由 Lead 决定；保留该实例在候选槽位中，才能验证选择行为。

7. 确认没有未绑定实例、知识源待配置、版本待选择等提示。
8. 点击“创建草稿”，确认保存成功且无未保存修改。
9. 点击“发布版本”，确认系统发布版本 v1。
10. 在“已发布版本（只读）”区域选择 v1，点击“使用此版本问答”。

预期：自动进入 /run，系统名称显示“Lead测试系统-01 · v1”；三位专家分别绑定各自材料的版本 1。

## 4. 执行主问题

输入：

> Who founded the Aster Vale Research Institute, and in which city is it located?

点击“开始协同问答”，等待执行结束。

正确答案应同时包含：

- 创办人：Mira Solen。
- 所在城市：Bellhaven。

允许答案句式、次序和标点不同。例如：

> The Aster Vale Research Institute was founded by Mira Solen and is located in Bellhaven.

## 5. 检查 Lead 的实际分派行为

在实时结果的“协作调用记录”中逐项展开：

1. coordinate / lead：应能看到“继续分派”、具体任务和目标实例 ID。
2. 对照“本次智能体配置”或实例名称，把任务目标 ID 对应到专家。
3. Founder Expert 的任务应核实 Aster Vale Research Institute 的创办人。
4. Location Expert 的任务应核实该机构所在城市。
5. Catalog Expert 不应出现实际 work / dispatch 执行调用。
6. 查看两位相关专家的结果及证据。各专家只掌握部分事实，显示“提供部分信息，交由审核者汇总”可以是正常状态。
7. Lead 再次进入 coordinate 后，应读取结果并“提交审核”；如果确有必要事实缺失，可能追加第二轮分派。
8. review / Final Reviewer 完成后，答案同时包含 Mira Solen 和 Bellhaven，验证结论为通过。

主问题涉及的两位专家可以在同一轮并行查询，也可能被分两轮安排。检查的是实际任务、目标与证据，不要求固定调用轮数或固定调用 ID。

人工核对：

| 必需事实 | 证据来源 | 位置 |
| --- | --- | --- |
| 创办人为 Mira Solen | founder_history.md | 第 3 行，材料版本 1 |
| 所在城市为 Bellhaven | institution_locations.md | 第 3 行，材料版本 1 |

专家的单项结果不能代替整体正确性检查；最终答案必须同时满足两个事实。图书目录不能作为上述两项事实的证据。

## 6. 核对历史与持久性

1. 回到“我的系统”，选中“Lead测试系统-01”，点击“查看此系统历史”。
2. 确认进入 /history，并带有 systemId 参数。
3. 打开刚才的主问题记录，记录运行 ID。
4. 核对所属系统及发布版本 v1、答案两项事实、三位专家的材料绑定版本。
5. 展开历史中的“协作调用记录”，确认实际分派的任务、对象、轮次、Lead 汇总和最终审核均被保存。
6. 刷新浏览器后重新打开同一运行 ID，确认结果、证据和分派记录仍在。

主用例通过条件：

- [ ] 三个知识源分别导入成功，三个专家分别绑定正确的版本 1。
- [ ] Lead 协议和系统均发布为 v1，运行实际使用目标系统版本。
- [ ] Founder Expert 和 Location Expert 都收到相关任务，完成并提供原文证据。
- [ ] Catalog Expert 保留在候选槽位中，但没有本题的实际 work / dispatch 调用。
- [ ] Lead 检查专家结果并提交汇总审核，最终答案包含 Mira Solen 和 Bellhaven，验证通过。
- [ ] 运行历史及刷新后能追溯到同一运行 ID、证据与分派记录。

如果答案正确但调用了无关的 Catalog Expert，记录为“答案核对通过，专家选择检查未通过”，不要把两项合并判定。

## 可选对照：换问题，换被选专家

保持同一发布系统 v1，重新点击“使用此版本问答”，再输入：

> What is the catalog number of Silver Lantern Atlas?

预期答案：BK-204。

预期分派：Catalog Expert 收到目录编号查询任务；Founder Expert 和 Location Expert 不应出现这次运行的实际 work / dispatch 调用。

这应产生一个新的运行 ID。不要与主问题的专家调用记录混在一起检查。

## 执行记录

| 项目 | 实际结果 |
| --- | --- |
| 系统 ID / 发布版本 | |
| 协议 ID / 发布版本 | |
| 主问题运行 ID | |
| 主问题最终答案 / 验证结论 | |
| 实际被分派的专家与任务 | |
| Catalog Expert 是否实际执行 | |
| 两份关键证据文件及位置 | |
| 历史刷新后记录是否保留 | |
| 对照问题运行 ID / 答案（可选） | |
| 首个不符合预期的步骤与报错 | |
