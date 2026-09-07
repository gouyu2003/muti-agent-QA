"""加载并校验 V0 使用的单条 HotpotQA fixture。

职责边界：
- 只加载单条题目记录（fixture），不加载 HotpotQA 整份开发集；
- 只做结构与交叉校验，不合并不同题目、不改写任何证据文本；
- 不调用 LLM，不读取 .env。

坐标约定：
    HotpotQA 的 context 形如 [[title, [sent1, sent2, ...]], ...]，
    一个 title 对应一个段落，段落由若干句子组成。supporting_facts 的
    [title, idx] 中，idx 是句子在该标题段落内的**零起始索引**；
    本模块 LocatedFact.sentence_index 与之同义，也与 bm25_retriever 中
    EvidenceChunk.source_paragraph 同义（检索文档粒度是"句子"）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_PATH = PROJECT_ROOT / "data" / "hotpotqa" / "fixture_001.json"

REQUIRED_FIELDS = ("_id", "question", "answer", "type", "supporting_facts", "context")


class Paragraph(TypedDict):
    title: str
    sentences: list[str]


class LocatedFact(TypedDict):
    title: str
    sentence_index: int
    text: str


def load_fixture(path: Path | str = DEFAULT_FIXTURE_PATH) -> dict[str, Any]:
    """读取并校验单条 HotpotQA fixture，原样返回记录中的字段与值。"""
    fixture_path = Path(path)

    try:
        with fixture_path.open("r", encoding="utf-8") as file:
            record = json.load(file)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"fixture 文件不存在：{fixture_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"fixture 不是合法 JSON：{fixture_path}") from exc

    if not isinstance(record, dict):
        raise ValueError(f"fixture 根节点必须是 JSON 对象：{fixture_path}")

    validate_fixture(record, fixture_path)
    return record


def validate_fixture(record: dict[str, Any], path: Path) -> None:
    """校验 fixture 结构，任何问题都抛出带路径的 ValueError。"""
    missing = [name for name in REQUIRED_FIELDS if name not in record]
    if missing:
        raise ValueError(f"fixture {path} 缺少必填字段：{', '.join(missing)}")

    for name in ("_id", "question", "answer", "type"):
        value = record[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"fixture {path} 的 {name} 必须是非空字符串")

    validate_context(record["context"], path)
    validate_supporting_facts(record["supporting_facts"], path)

    # 交叉校验：每个 supporting fact 都必须能在 context 中定位到
    locate_supporting_facts(record)


def validate_context(context: Any, path: Path) -> None:
    if not isinstance(context, list) or not context:
        raise ValueError(f"fixture {path} 的 context 必须是非空列表")

    for index, item in enumerate(context):
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(
                f"fixture {path} 的 context[{index}] 必须是 [title, sentences]"
            )

        title, sentences = item
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"fixture {path} 的 context[{index}] 标题必须是非空字符串"
            )
        if (
            not isinstance(sentences, list)
            or not sentences
            or not all(isinstance(sentence, str) for sentence in sentences)
        ):
            raise ValueError(
                f"fixture {path} 的 context[{index}] 的 sentences 必须是非空字符串列表"
            )


def validate_supporting_facts(supporting_facts: Any, path: Path) -> None:
    if not isinstance(supporting_facts, list) or not supporting_facts:
        raise ValueError(f"fixture {path} 的 supporting_facts 必须是非空列表")

    for index, fact in enumerate(supporting_facts):
        if not isinstance(fact, list) or len(fact) != 2:
            raise ValueError(
                f"fixture {path} 的 supporting_facts[{index}] "
                "必须是 [title, sentence_index]"
            )

        title, sentence_index = fact
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"fixture {path} 的 supporting_facts[{index}] 标题必须是非空字符串"
            )
        if isinstance(sentence_index, bool) or not isinstance(sentence_index, int):
            raise ValueError(
                f"fixture {path} 的 supporting_facts[{index}] 的句子下标必须是整数"
            )


def build_paragraph_index(context: Any) -> dict[str, list[str]]:
    """把 context 转成 {title: sentences}，遇到重复标题直接报错而不是静默覆盖。"""
    index: dict[str, list[str]] = {}
    for title, sentences in context:
        if title in index:
            raise ValueError(
                f"fixture 的 context 出现重复标题：{title!r}；"
                "HotpotQA 一个标题应只对应一个段落"
            )
        index[title] = sentences
    return index


def locate_supporting_facts(record: dict[str, Any]) -> list[LocatedFact]:
    """把 supporting_facts 的 [title, idx] 定位成具体句子文本。"""
    index = build_paragraph_index(record["context"])
    located: list[LocatedFact] = []

    for title, sentence_index in record["supporting_facts"]:
        sentences = index.get(title)
        if sentences is None:
            raise ValueError(f"supporting fact 的标题 {title!r} 不在 context 中")
        if not 0 <= sentence_index < len(sentences):
            raise ValueError(
                f"supporting fact 的句子下标越界：{title!r} 只有 "
                f"{len(sentences)} 句，收到 {sentence_index}"
            )
        located.append(
            {
                "title": title,
                "sentence_index": sentence_index,
                "text": sentences[sentence_index],
            }
        )
    return located


def iter_paragraphs(record: dict[str, Any]) -> list[Paragraph]:
    """把 context 转成结构化段落列表，供 bm25_retriever 展开成句子文档。"""
    return [
        {"title": title, "sentences": list(sentences)}
        for title, sentences in record["context"]
    ]