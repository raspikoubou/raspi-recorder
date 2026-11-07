#!/usr/bin/env python3.13
"""Dispatch boot recovery to the previous, already-installed release."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

STATE = Path("/var/lib/raspi-recorder-update")
JOURNAL = STATE / "journal.json"
OPT_ROOT = Path("/opt/raspi-recorder")
_OLD_TARGET = re.compile(
    r"^releases/(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.dev(?:0|[1-9][0-9]*))?$"
)


def main() -> int:
    try:
        state_info = STATE.lstat()
    except OSError as exc:
        raise SystemExit("privileged update state is unavailable") from exc
    if (
        not stat.S_ISDIR(state_info.st_mode)
        or state_info.st_uid != 0
        or state_info.st_gid != 0
        or stat.S_IMODE(state_info.st_mode) != 0o700
    ):
        raise SystemExit("privileged update state permissions are invalid")
    if JOURNAL.is_symlink():
        raise SystemExit("update recovery journal is a symlink")
    if not JOURNAL.exists():
        return 0
    info = JOURNAL.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 32 * 1024
    ):
        raise SystemExit("update recovery journal is invalid")
    try:
        journal = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("update recovery journal is unreadable") from exc
    if not isinstance(journal, dict) or journal.get("schema") != 1:
        raise SystemExit("update recovery journal schema is invalid")
    old_target = journal.get("old_target")
    if not isinstance(old_target, str) or not _OLD_TARGET.fullmatch(old_target):
        raise SystemExit("update recovery target is invalid")
    release_root = OPT_ROOT / old_target
    if (
        OPT_ROOT.is_symlink()
        or (OPT_ROOT / "releases").is_symlink()
        or release_root.is_symlink()
        or not release_root.is_dir()
    ):
        raise SystemExit("previous release is unavailable for recovery")
    executable = release_root / ".venv/bin/raspi-recorder"
    if executable.is_symlink() or not executable.is_file():
        raise SystemExit("previous release recovery executable is unavailable")
    executable_info = executable.lstat()
    if (
        executable_info.st_uid != 0
        or executable_info.st_mode & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise SystemExit("previous release recovery executable is unavailable")
    result = subprocess.run(
        [str(executable), "update-helper", "recover"],
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=180,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
