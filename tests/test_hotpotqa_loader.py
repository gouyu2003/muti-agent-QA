import json

import pytest

from src.knowledge.hotpotqa_loader import (
    DEFAULT_FIXTURE_PATH,
    iter_paragraphs,
    load_fixture,
    locate_supporting_facts,
)


def test_load_fixture_reads_real_record():
    record = load_fixture()

    assert record["question"].strip()
    assert record["answer"] == "Chief of Protocol"
    assert record["type"] == "bridge"
    assert len(record["context"]) == 10


def test_context_is_title_sentences_pairs():
    record = load_fixture()

    for paragraph in iter_paragraphs(record):
        assert paragraph["title"].strip()
        assert paragraph["sentences"]
        assert all(isinstance(sentence, str) for sentence in paragraph["sentences"])


def test_every_supporting_fact_is_locatable():
    record = load_fixture()
    facts = locate_supporting_facts(record)

    assert [(fact["title"], fact["sentence_index"]) for fact in facts] == [
        (title, index) for title, index in record["supporting_facts"]
    ]
    assert all(fact["text"].strip() for fact in facts)


def test_load_fixture_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="不存在"):
        load_fixture(tmp_path / "missing.json")


def test_load_fixture_rejects_missing_context(tmp_path):
    record = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))
    record.pop("context")
    path = tmp_path / "no_context.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="context"):
        load_fixture(path)


def test_load_fixture_rejects_bad_context_item(tmp_path):
    record = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))
    record["context"] = [["Only Title"]]
    path = tmp_path / "bad_context.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=r"context\[0\]"):
        load_fixture(path)


def test_locate_rejects_out_of_range_sentence_index():
    record = json.loads(DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"))
    record["supporting_facts"] = [["Shirley Temple", 999]]

    with pytest.raises(ValueError, match="越界"):
        locate_supporting_facts(record)