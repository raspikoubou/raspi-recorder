"""Gunicorn WSGI entrypoint。engineは別systemd serviceで稼働する。"""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from raspi_recorder.adapters.authentication_store import AuthenticationStore
from raspi_recorder.adapters.engine_socket import EngineSocketClient
from raspi_recorder.adapters.system_clock import SystemClock
from raspi_recorder.adapters.web_app import create_app
from raspi_recorder.adapters.werkzeug_password_hasher import WerkzeugPasswordHasher
from raspi_recorder.application.authentication import AuthenticationService


def create_wsgi_app() -> Flask:
    """configured persistent auth store + Unix engine clientのWSGI app。"""
    data_directory = Path(
        os.environ.get("RASPI_RECORDER_DATA_DIR", "/var/lib/raspi-recorder")
    )
    socket_path = Path(
        os.environ.get(
            "RASPI_RECORDER_ENGINE_SOCKET", "/run/raspi-recorder/engine.sock"
        )
    )
    trusted_hosts = tuple(
        value.strip()
        for value in os.environ.get("RASPI_RECORDER_TRUSTED_HOSTS", "localhost").split(
            ","
        )
        if value.strip()
    )
    authentication = AuthenticationService(
        AuthenticationStore(data_directory / "authentication.sqlite3"),
        WerkzeugPasswordHasher(),
        SystemClock(),
    )
    return create_app(
        authentication,
        EngineSocketClient(socket_path),
        trusted_hosts=trusted_hosts,
    )


app: Flask = create_wsgi_app()
