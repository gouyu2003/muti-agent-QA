"""基于 BM25Okapi 的纯本地确定性句子级检索器。

坐标约定（与 src/knowledge/hotpotqa_loader.py 一致）：
    HotpotQA 的 context 形如 [[title, [sent1, sent2, ...]], ...]，
    一个 title 对应一个段落。本模块把每**一句**作为一条独立文档展开，
    EvidenceChunk.source_paragraph 是句子在该标题段落内的**零起始索引**，
    与 supporting_facts 中的句子下标语义相同。

设计约束：
    - 纯本地、确定性：不调用 LLM，不读取 .env，不依赖任何远程服务；
    - tokenizer 固定为「小写 + 提取 [a-z0-9]+」，保证同一 query 多次
      检索结果完全一致（不引入 stemmer/停用词等不确定性组件）；
    - 索引文本 = 「段落标题 + 原句」。正文常以姓名变体/代词指代实体，
      段落标题（如 "Shirley Temple"）是关键召回信号；标题只参与排序，
      EvidenceChunk["text"] 永远保留原始句子文本；
    - search() 只做排序与截断，不修改证据文本，不依据模型常识补答案。
"""

from __future__ import annotations

import re
from typing import Any

from rank_bm25 import BM25Okapi

from src.state import EvidenceChunk

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# 相关性阈值：BM25 得分为 0 表示 query 与该句没有任何共有词，
# 这类"命中"纯属噪声。保留阈值后检索器才可能真正返回空结果，
# Retriever 才能据此标记 insufficient，而不是永远返回 top-k。
MIN_SCORE = 0.0


def tokenize(text: str) -> list[str]:
    """确定性 tokenizer：小写后提取字母/数字串。"""
    return _TOKEN_RE.findall(text.lower())


def _iter_sentence_docs(context: list[Any]) -> list[dict[str, Any]]:
    """把 context 展开为句子文档列表，每句一条。

    返回元素形如：
        {"text": ..., "source_title": ..., "source_paragraph": ...}
    source_paragraph 为该句在其标题段落内的零起始下标。
    """
    docs: list[dict[str, Any]] = []
    for title, sentences in context:
        for paragraph_index, sentence in enumerate(sentences):
            docs.append(
                {
                    "text": sentence,
                    "source_title": title,
                    "source_paragraph": paragraph_index,
                }
            )
    return docs


class BM25Retriever:
    """对单条 HotpotQA 记录（record = load_fixture() 的返回值）建句子级索引。"""

    def __init__(self, record: dict[str, Any]):
        context = record["context"]
        self._docs = _iter_sentence_docs(context)

        # 段落标题（如 "Shirley Temple"）一并参与索引：
        # 正文常以 "Shirley Temple Black"/"she" 等变体指代实体，单靠句子
        # 文本很难在第二跳被召回；标题加入只影响排序打分，
        # EvidenceChunk["text"] 仍保留原始句子，不做任何改写。
        tokenized_docs = [
            tokenize(doc["source_title"]) + tokenize(doc["text"])
            for doc in self._docs
        ]
        self._bm25 = BM25Okapi(tokenized_docs)

    def search(
        self, query: str, top_k: int = 3, min_score: float = MIN_SCORE
    ) -> list[EvidenceChunk]:
        """返回得分高于 min_score 的最多 top_k 条证据，按得分降序。

        得分不高于 min_score 的文档一律丢弃；因此查询与语料完全无关时
        返回空列表，由上层判定为证据不足。
        relevance_score 仅为相对排序依据，不承诺任何绝对含义。
        """
        if top_k <= 0:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(
            (index for index in range(len(self._docs)) if scores[index] > min_score),
            key=lambda index: scores[index],
            reverse=True,
        )[:top_k]

        return [
            {
                "text": self._docs[index]["text"],
                "source_title": self._docs[index]["source_title"],
                "source_paragraph": self._docs[index]["source_paragraph"],
                "relevance_score": float(scores[index]),
            }
            for index in ranked
        ]


def build_retriever(fixture_path: str | None = None) -> BM25Retriever:
    """便捷入口：读取默认/指定 fixture 并构建检索器。

    fixture_path 为 None 时使用 data/hotpotqa/fixture_001.json。
    """
    from src.knowledge.hotpotqa_loader import load_fixture

    if fixture_path is None:
        record = load_fixture()
    else:
        record = load_fixture(fixture_path)
    return BM25Retriever(record)
