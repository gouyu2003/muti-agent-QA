# 智能体定义模板说明文档 v0.1

> 作者：沈天宇  
> 日期：2026-08-23  
> 对应文件：`agents/` 目录下的 JSON 文件

---

## 一、目录结构

```
system-design/agents/
├── agent_template_schema.json   # 字段定义规范（JSON Schema）
├── planner.json                 # 规划智能体模板
├── retriever.json               # 检索与证据智能体模板
├── answer_validator.json        # 回答与验证智能体模板
└── README.md                    # 本文档
```

---

## 二、模板字段速查表

| 字段名 | 类型 | 是否必填 | 说明 |
|--------|------|---------|------|
| `agent_id` | string | ✅ | 唯一标识符，运行时节点命名和路由使用 |
| `agent_name` | string | ✅ | 可读名称，用于日志和展示 |
| `version` | string | ✅ | 版本号（major.minor.patch） |
| `role` | string | ✅ | 角色类型：`planner` / `retriever` / `answer_validator` / `custom` |
| `domain` | string | ✅ | 应用领域（V0 固定 `multi_hop_qa`） |
| `description` | string | 推荐 | 角色职责自然语言描述 |
| `reasoning_method` | string | ✅ | 推理策略：`cot` / `react` / `rag` / `direct` / `custom` |
| `knowledge_sources` | array | ✅ | 可访问的知识源（文档库/向量库/API/无） |
| `tools` | array | 可选 | 可调用的工具（Python 函数/API 调用等） |
| `model_config` | object | ✅ | LLM 配置（模型名、温度、最大 token、系统提示词） |
| `neighbor_agents` | array | ✅ | 协作关系（上游/下游/并行，通信方式） |
| `input_schema` | object | ✅ | 从共享 State 读取的字段定义 |
| `output_schema` | object | ✅ | 写回共享 State 的字段定义 |
| `failure_handling` | object | ✅ | 失败处理（最大重试次数、失败跳转目标、兜底消息） |

---

## 三、三类智能体对比

### 3.1 角色职责对比

| 维度 | Planner（规划） | Retriever（检索） | Answer-Validator（回答+验证） |
|------|----------------|------------------|-----------------------------|
| **核心任务** | 分析问题，拆解子问题 | 对子问题检索证据 | 综合答案，验证证据 |
| **推理策略** | CoT（链式推理） | RAG（检索增强） | CoT（推理+判断） |
| **知识源** | 无（依赖 LLM 内部知识） | 本地数据集 + 向量库 | 无（依赖 Retriever 提供的证据） |
| **LLM 温度** | 0.3（适度创造性） | 0.1（高确定性，减少幻觉） | 0.2（准确判断） |
| **主要输出** | 子问题列表 + 检索策略 | 证据片段 + 充分性评估 | 最终答案 + 验证结论 |
| **触发重循环** | 被 Validator 的 conflict 触发 | 被 Validator 的 insufficient 触发 | 主动判断并触发下游 |

### 3.2 输入输出关系

```
用户问题
    │
    ▼
┌────────────────────────────────────────────────────┐
│  Planner 输入：question, conflict_flag(可选)        │
│  Planner 输出：sub_questions, retrieval_strategy    │
└────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────┐
│  Retriever 输入：sub_questions, retrieval_strategy  │
│                  retrieval_supplement_request(可选) │
│  Retriever 输出：evidence_chunks                    │
└────────────────────────────────────────────────────┘
    │
    ▼
┌────────────────────────────────────────────────────────────────────┐
│  Answer-Validator 输入：question, sub_questions, evidence_chunks    │
│  Answer-Validator 输出（三种情况）：                                 │
│    • verified=true  → answer + supporting_evidence + 输出结果       │
│    • insufficient   → retrieval_supplement_request → 回 Retriever   │
│    • conflict       → conflict_flag + conflict_note → 回 Planner    │
└────────────────────────────────────────────────────────────────────┘
```

### 3.3 重试次数上限设计

| 智能体 | 最大重试 | 失败跳转 |
|--------|---------|---------|
| Planner | 2 次 | terminate（终止） |
| Retriever | 3 次 | answer_validator（用现有证据继续） |
| Answer-Validator | 2 次 | terminate（输出当前最佳答案） |

---

## 四、共享状态（AgentState）字段汇总

运行时，三个智能体通过 LangGraph 的共享状态（`TypedDict`）传递信息。以下是 V0 共享状态的完整字段：

```python
from typing import TypedDict, List, Optional

class EvidenceChunk(TypedDict):
    text: str
    source_title: str
    source_paragraph: int
    relevance_score: float

class SubQuestion(TypedDict):
    id: str
    question: str
    keywords: List[str]
    depends_on: List[str]

class AgentState(TypedDict):
    # ---- 输入 ----
    question: str                              # 用户原始问题

    # ---- Planner 写入 ----
    sub_questions: List[SubQuestion]           # 子问题列表
    retrieval_strategy: str                    # sequential / parallel
    plan_retry_count: int                      # 重规划次数

    # ---- Retriever 写入 ----
    evidence_chunks: List[dict]                # 每个子问题的证据片段
    retrieval_retry_count: int                 # 补充检索次数

    # ---- Answer-Validator 写入 ----
    answer: str                                # 最终答案
    verified: bool                             # 验证是否通过
    failure_type: str                          # none / insufficient / conflict
    conflict_flag: bool                        # 冲突标记（触发重规划）
    conflict_note: str                         # 冲突说明
    retrieval_supplement_request: Optional[dict]  # 补充检索请求
    verification_note: str                     # 验证过程说明
    supporting_evidence: List[dict]            # 最终依赖的证据
    validation_retry_count: int                # 验证循环次数
```

---

## 五、如何新增一个自定义智能体

如果未来需要新增第四类智能体（如 `conflict_resolver` 冲突裁决智能体），只需：

1. 复制任意一个 JSON 模板；
2. 修改 `agent_id`、`agent_name`、`role`（填 `custom`）、`description`；
3. 修改 `system_prompt` 为新角色的职责描述；
4. 修改 `input_schema` 和 `output_schema` 定义新的读写字段；
5. 在 `neighbor_agents` 中声明与哪些智能体协作；
6. 在协同协议文件（`protocols/multi_hop_qa.json`）中将新智能体加入协同图。

---

## 六、后续扩展方向

| 当前 V0 限制 | V1 扩展方向 |
|------------|------------|
| 固定三类智能体 | 支持任意自定义角色 |
| 知识源路径硬编码 | 知识源在运行时动态指定 |
| 单一领域（多跳QA） | 支持多领域（医疗/法律/学术等）切换 |
| LLM 固定为 gpt-4o-mini | 支持任意模型（国产模型/本地模型） |
| 串行实例化 | 同类智能体并行副本 |
