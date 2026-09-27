"""命令行入口：通过统一 RunService 执行并持久化一次 V0 问答。"""

from __future__ import annotations

import argparse
from typing import Any, Optional, Sequence

from src.runtime.context import build_v0_run_context
from src.runtime.models import RunDetail
from src.runtime.service import RunService
from src.runtime.sqlite_repository import SQLiteRunRepository


DEFAULT_QUESTION = (
    "What government position was held by the woman who portrayed "
    "Corliss Archer in the film Kiss and Tell?"
)


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
        "--run-id",
        "--thread-id",
        dest="run_id",
        default=None,
        help=(
            "指定本次运行 ID；默认自动生成 UUID。"
            "--thread-id 作为旧参数名兼容保留"
        ),
    )
    parser.add_argument("--workflow-id", default=None)
    parser.add_argument("--source-id", default=None)
    parser.add_argument("--system-config", default=None,
                        help="系统清单 JSON 路径；默认使用内置系统")
    return parser.parse_args(argv)


def build_default_run_service() -> RunService:
    """构造 CLI 使用的默认持久化运行服务。"""

    return RunService(SQLiteRunRepository())


def run(
    question: str,
    run_id: str | None = None,
    *,
    run_service: RunService | None = None,
    workflow_id: str | None = None,
    source_id: str | None = None,
    catalog_path: str | None = None,
) -> RunDetail:
    """通过统一 RunService 执行一次 CLI 运行并返回持久化详情。"""

    service = run_service or build_default_run_service()
    context = build_v0_run_context(
        question,
        entrypoint="cli",
        run_id=run_id,
        workflow_id=workflow_id,
        source_id=source_id,
        catalog_path=catalog_path,
    )
    return service.run(context)


def format_result(
    state: dict[str, Any],
    *,
    run_id: str | None = None,
) -> str:
    """把最终公开状态渲染成人类可读文本。"""

    evidence = state.get("supporting_evidence") or []
    lines = ["===== 最终输出 ====="]
    if run_id is not None:
        lines.append(f"运行 ID：{run_id}")
    lines.extend(
        [
            f"问题：{state.get('question', '')}",
            f"答案：{state.get('answer') or '(无)'}",
            f"验证通过：{state.get('verified', False)}",
            f"失败类型：{state.get('failure_type', 'none')}",
            f"验证说明：{state.get('verification_note', '')}",
            "",
            f"支撑证据（{len(evidence)} 条）：",
        ]
    )
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

    # Windows 下输出被重定向时 stdout 可能不是 UTF-8。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv()  # 只在入口加载 .env，不打印或回显其中的值。

    args = parse_args(argv)
    question = " ".join(args.question).strip() or DEFAULT_QUESTION

    try:
        options = {}
        if args.workflow_id is not None:
            options["workflow_id"] = args.workflow_id
        if args.source_id is not None:
            options["source_id"] = args.source_id
        if args.system_config is not None:
            options["catalog_path"] = args.system_config
        detail = run(question, run_id=args.run_id, **options)
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1

    record = detail.run
    if record.status == "failed":
        error_type = record.error.error_type if record.error else "execution_error"
        message = record.error.message if record.error else "多智能体流程执行失败"
        print(f"运行失败 [{error_type}]：{message}")
        print(f"运行 ID：{record.context.run_id}")
        return 1

    if record.status != "completed":
        print(f"运行未完成：{record.status}")
        print(f"运行 ID：{record.context.run_id}")
        return 1

    final_state = record.final_state or {"question": question}
    print(format_result(final_state, run_id=record.context.run_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
