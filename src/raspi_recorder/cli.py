"""local初期化・認証初期化・engine read-only診断CLI。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from raspi_recorder import __version__
from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.adapters.engine_socket import EngineSocketClient, EngineSocketError
from raspi_recorder.adapters.recording_catalog import RecordingCatalog
from raspi_recorder.adapters.settings_repository import SettingsRepository
from raspi_recorder.adapters.system_clock import SystemClock
from raspi_recorder.application.settings_service import SettingsService

_DEFAULT_DATA_DIRECTORY = Path("/var/lib/raspi-recorder")
_DEFAULT_SOCKET_PATH = Path("/run/raspi-recorder/engine.sock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="raspi-recorder")
    parser.add_argument(
        "--version", action="version", version=f"raspi-recorder {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("initialize", help="初回DBを準備する")
    initialize.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA_DIRECTORY)
    reset = commands.add_parser("auth-reset", help="ローカル操作で認証だけを初期化する")
    reset.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA_DIRECTORY)
    status = commands.add_parser("status", help="engineの状態を読み取る")
    status.add_argument("--socket", type=Path, default=_DEFAULT_SOCKET_PATH)
    engine = commands.add_parser("engine", help="録画engine serviceを起動する")
    engine.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA_DIRECTORY)
    engine.add_argument("--socket", type=Path, default=_DEFAULT_SOCKET_PATH)
    commands.add_parser("clock-helper", help=argparse.SUPPRESS)
    update_helper = commands.add_parser("update-helper", help=argparse.SUPPRESS)
    update_helper.add_argument("action", choices=("serve", "recover"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "initialize":
            return _initialize(args.data_dir)
        if args.command == "auth-reset":
            return _auth_reset(args.data_dir)
        if args.command == "status":
            response = EngineSocketClient(args.socket).request("GET", "/api/v1/status")
            print(json.dumps(response.body, ensure_ascii=False, indent=2))
            return 0 if response.status_code == 200 else 1
        if args.command == "engine":
            return _run_engine(args.data_dir, args.socket)
        if args.command == "clock-helper":
            from raspi_recorder.adapters.clock_helper import main as clock_helper_main

            return clock_helper_main()
        if args.command == "update-helper":
            from raspi_recorder.adapters.update_helper import main as update_helper_main

            return update_helper_main([args.action])
    except (OSError, RuntimeError, ValueError, EngineSocketError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 2


def _initialize(data_directory: Path) -> int:
    previous_umask = os.umask(0o007)
    try:
        data_directory.mkdir(parents=True, exist_ok=True, mode=0o2770)
        os.chmod(data_directory, 0o2770)
        clock = SystemClock()
        settings = SettingsService(
            SettingsRepository(data_directory / "settings.sqlite3"), clock
        )
        settings.initialize()
        RecordingCatalog(data_directory / "recordings.sqlite3").initialize()
        authentication_database = data_directory / "authentication.sqlite3"
        try:
            authentication_info = authentication_database.lstat()
        except FileNotFoundError:
            authentication_info = None
        if authentication_info is not None and not stat.S_ISREG(
            authentication_info.st_mode
        ):
            raise RuntimeError("authentication DBはregular fileである必要があります")
        authentication_umask = os.umask(0o077)
        try:
            created = AuthenticationStore(authentication_database).initialize()
        finally:
            os.umask(authentication_umask)
    finally:
        os.umask(previous_umask)
    if created:
        print(
            "Web画面をパスワード登録なしで利用できます。必要な保護は設定から有効にしてください。"
        )
    else:
        print("既存設定を保持しました。必要な場合はWeb画面から設定してください。")
    return 0


def _auth_reset(data_directory: Path) -> int:
    database = data_directory / "authentication.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("認証DBがありません。先にinitializeしてください")
    if os.geteuid() != 0 and database.stat().st_uid != os.geteuid():
        raise PermissionError("認証初期化にはDB所有者またはrootが必要です")
    AuthenticationStore(database).reset_for_local_setup()
    print(
        "認証情報を消去し、ログイン保護・管理者保護をOFFに戻しました。録画と設定は保持されています。"
    )
    return 0


def _run_engine(data_directory: Path, socket_path: Path) -> int:
    from raspi_recorder.engine.runtime import RecorderRuntime

    runtime = RecorderRuntime(data_directory, socket_path=socket_path)
    stopping = threading.Event()

    def request_stop(_signal_number: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    runtime.start()
    try:
        stopping.wait()
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
