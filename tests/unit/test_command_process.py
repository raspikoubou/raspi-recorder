from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from raspi_recorder.adapters.command_process import run_isolated_command


def test_timeout_stops_the_build_child_as_well_as_its_parent(tmp_path):
    pid_file = tmp_path / "child.pid"
    # Keep the parent alive until the deadline while a child can keep writing
    # to staging. A direct subprocess.run timeout only kills the parent.
    program = (
        "import os,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"open({str(pid_file)!r},'w').write(str(child.pid)); "
        "time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_isolated_command([sys.executable, "-c", program], 0.5, env=dict(os.environ))
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            try:
                status = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if status == "Z":  # dead, awaiting adoption/reaping by the host init
                break
            time.sleep(0.01)
        else:
            pytest.fail("timed-out build left a running child")
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_normal_command_returns_its_exit_code():
    assert (
        run_isolated_command([sys.executable, "-c", "raise SystemExit(7)"], 2, env={})
        == 7
    )
