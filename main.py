"""命令行入口：输入一个英文问题，跑完 V0 图并打印结果。

用法（在项目根目录执行）：

    python main.py "What government position was held by the woman who portrayed Corliss Archer in the film Kiss and Tell?"
    python main.py                      # 留空则使用下面的 DEFAULT_QUESTION
    python main.py "..." --thread-id t1 # 指定 thread_id，配合 InMemorySaver 复用会话

职责边界：
- 只做四件事：接收问题、构建图、执行、打印结果；
- 只有这里调用 load_dotenv()，库代码（src/*）一律不读 .env；
- 真实调用 DeepSeek，需要 .env 中的 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL。
"""

from __future__ import annotations

import argparse
from typing import Any, Optional, Sequence

from src.graph import build_graph

DEFAULT_QUESTION = (
    "What government position was held by the woman who portrayed "
    "Corliss Archer in the film Kiss and Tell?"
)
DEFAULT_THREAD_ID = "cli"
RECURSION_LIMIT = 30


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-Agent QA V0：Planner / Retriever / Answer-Validator 多跳问答"
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="英文问题；留空则使用内置的 HotpotQA fixture 问题",
    )
    parser.add_argument(
        "--thread-id",
        default=DEFAULT_THREAD_ID,
        help=f"LangGraph thread_id，默认 {DEFAULT_THREAD_ID}",
    )
    return parser.parse_args(argv)


def run(question: str, thread_id: str = DEFAULT_THREAD_ID) -> dict[str, Any]:
    """构建图并执行一次，返回最终状态。"""
    app = build_graph()
    return app.invoke(
        {"question": question},
        config={
            "configurable": {"thread_id": thread_id},
            "recursion_limit": RECURSION_LIMIT,
        },
    )


def format_result(state: dict[str, Any]) -> str:
    """把最终状态渲染成人类可读文本。"""
    evidence = state.get("supporting_evidence") or []

    lines = [
        "===== 最终输出 =====",
        f"问题：{state.get('question', '')}",
        f"答案：{state.get('answer') or '(无)'}",
        f"验证通过：{state.get('verified', False)}",
        f"失败类型：{state.get('failure_type', 'none')}",
        f"验证说明：{state.get('verification_note', '')}",
        "",
        f"支撑证据（{len(evidence)} 条）：",
    ]
    if evidence:
        for item in evidence:
            lines.append(
                f"  [{item.get('sq_id', '')}] ({item.get('source_title', '')}) "
                f"{item.get('text', '')}"
            )
    else:
        lines.append("  (无)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import sys

    from dotenv import load_dotenv

    # Windows 下输出被重定向时 stdout 可能是 cp1252，中文标签会抛 UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv()  # 只在此处加载 .env，绝不打印或回显其中的值

    args = parse_args(argv)
    question = " ".join(args.question).strip() or DEFAULT_QUESTION

    try:
        state = run(question, thread_id=args.thread_id)
    except Exception as exc:  # 节点抛异常时给出可读错误，而不是整段 traceback
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1

    print(format_result(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
