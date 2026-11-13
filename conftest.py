"""仓库级 pytest 配置守卫：只允许固定的临时目录名，避免残留目录堆积。

历史问题：每次验证都新起一个 `--basetemp .pytest-tmp-xxx`，pytest 从不清理，
于是项目根出现 .pytest-tmp-chat-final、.pytest-tmp-gsm-step5-v2 等几十个目录。
约定为：离线回归只用 `.pytest-tmp`，真实模型测试只用 `.pytest-live`。
"""
from pathlib import Path

import pytest

UNIQUE_BASETEMP = ".pytest-tmp"
LIVE_PREFIX = ".pytest-live"


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    basetemp = getattr(config.option, "basetemp", None)
    if not basetemp:
        config.option.basetemp = UNIQUE_BASETEMP
        return

    raw = str(basetemp)
    name = Path(raw).name
    if name == UNIQUE_BASETEMP or name.startswith(LIVE_PREFIX):
        return

    candidate = Path(raw).expanduser()
    root = Path(config.rootpath)
    if candidate.is_absolute() and root not in candidate.resolve().parents:
        # 目录在仓库之外，由调用方自行负责清理，不干预。
        return

    config.option.basetemp = UNIQUE_BASETEMP
    config.issue_config_time_warning(
        UserWarning(
            f"--basetemp {raw} ignored: this repo reuses {UNIQUE_BASETEMP} only "
            f"({LIVE_PREFIX} for live-model runs). Archive evidence JSON under docs/."
        ),
        stacklevel=2,
    )
