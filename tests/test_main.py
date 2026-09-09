"""main.py 的离线测试：只测参数解析与结果渲染，不建图、不调用 LLM。"""

from main import DEFAULT_QUESTION, format_result, parse_args


def test_parse_args_joins_words_into_one_question():
    args = parse_args(["What", "government", "position", "was", "held?"])
    assert " ".join(args.question) == "What government position was held?"
    assert args.thread_id == "cli"


def test_parse_args_defaults_to_builtin_question():
    args = parse_args([])
    assert " ".join(args.question) == "" or not args.question
    assert DEFAULT_QUESTION.startswith("What government position")


def test_parse_args_accepts_custom_thread_id():
    assert parse_args(["q", "--thread-id", "t9"]).thread_id == "t9"


def test_format_result_renders_core_fields():
    state = {
        "question": "Q?",
        "answer": "Chief of Protocol",
        "verified": True,
        "failure_type": "none",
        "verification_note": "证据充分",
        "supporting_evidence": [
            {"sq_id": "sq_1", "text": "E1", "source_title": "T1"},
            {"sq_id": "sq_2", "text": "E2", "source_title": "T2"},
        ],
    }
    text = format_result(state)

    assert "Chief of Protocol" in text
    assert "验证通过：True" in text
    assert "支撑证据（2 条）" in text
    assert "T1" in text and "T2" in text


def test_format_result_tolerates_empty_state():
    text = format_result({"question": "Q?"})

    assert "(无)" in text
    assert "支撑证据（0 条）" in text


def test_main_returns_one_and_reports_error_on_failure(monkeypatch, capsys):
    """节点抛异常时必须给出可读错误并返回非零退出码，而不是抛 traceback。"""
    import main as main_module

    def boom(question, thread_id="cli"):
        raise RuntimeError("节点炸了")

    monkeypatch.setattr(main_module, "run", boom)
    assert main_module.main(["Q?"]) == 1
    assert "运行失败" in capsys.readouterr().out
