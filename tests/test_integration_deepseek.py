"""真实 DeepSeek 集成测试：默认不执行。

常规 `python -m pytest` 会因 pytest.ini 的 addopts（`-m "not integration"`）跳过本文件，
保证日常测试离线、稳定、不依赖 API key。

需要真实调用时显式开启：

    python -m pytest -m integration -v

.env 中缺少 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL 时本测试跳过（skip），不算失败。
本文件不打印、不断言任何 key 相关内容。
"""

import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.integration

QUESTION = (
    "What government position was held by the woman who portrayed "
    "Corliss Archer in the film Kiss and Tell?"
)
EXPECTED_ANSWER = "Chief of Protocol"


def _env_ready() -> bool:
    load_dotenv()
    return bool(
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        and os.environ.get("DEEPSEEK_BASE_URL", "").strip()
    )


@pytest.mark.skipif(
    not _env_ready(), reason="缺少 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL，跳过真实调用"
)
def test_real_deepseek_end_to_end():
    from main import run

    state = run(QUESTION, thread_id="integration")

    assert state["sub_questions"], "Planner 应产出子问题"
    assert state["evidence_chunks"], "Retriever 应产出证据分组"
    assert EXPECTED_ANSWER.lower() in (state.get("answer") or "").lower()
