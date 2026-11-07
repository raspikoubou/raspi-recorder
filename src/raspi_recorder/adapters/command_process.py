"""更新用commandとその子孫を同じprocess groupで停止する。"""

from __future__ import annotations

import os
import signal
import subprocess


def run_isolated_command(
    arguments: list[str],
    timeout: float,
    *,
    env: dict[str, str],
    user: int | None = None,
    group: int | None = None,
    extra_groups: list[int] | None = None,
) -> int:
    """timeoutや中断では、pipから起動されたbuild/compilerも終了させる。"""
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        user=user,
        group=group,
        extra_groups=extra_groups,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5.0)
        raise
