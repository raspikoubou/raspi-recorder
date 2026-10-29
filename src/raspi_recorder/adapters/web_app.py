"""Flask web adapterとserver-side authentication/session境界。"""

from __future__ import annotations

import ipaddress
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    make_response,
    render_template,
    request,
    stream_with_context,
)

from raspi_recorder.application.authentication import (
    AuthenticationError,
    AuthenticationService,
)
from raspi_recorder.domain.errors import RevisionConflict
from raspi_recorder.ports.engine import EngineGateway

_SESSION_COOKIE = "raspi_recorder_session"
_AUTH_ROUTES = {
    "/api/v1/auth/status",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/admin/reauthenticate",
}
_ROUTE_PATTERNS = (
    ("GET", re.compile(r"/api/v1/(status|capabilities|settings|recordings)$")),
    (
        "GET",
        re.compile(r"/api/v1/recordings/[A-Za-z0-9_-]{1,160}/(content|download)$"),
    ),
    (
        "GET",
        re.compile(r"/api/v1/(operations|recordings|exports)/[A-Za-z0-9_-]{1,160}$"),
    ),
    ("GET", re.compile(r"/api/v1/update/status$")),
    ("GET", re.compile(r"/api/v1/time/status$")),
    (
        "GET",
        re.compile(
            r"/api/v1/live/[12]/[1-9][0-9]*/(?:index\.m3u8|segment[0-9]{6}\.ts)$"
        ),
    ),
    ("GET", re.compile(r"/api/v1/cameras/[12]/preview\.jpg$")),
    ("GET", re.compile(r"/api/v1/exports/[A-Za-z0-9_-]{1,160}/download$")),
    ("PATCH", re.compile(r"/api/v1/settings$")),
    ("POST", re.compile(r"/api/v1/cameras/[12]/(start|stop)$")),
    ("POST", re.compile(r"/api/v1/live/[12]/demand$")),
    ("POST", re.compile(r"/api/v1/cameras/[12]/preview/demand$")),
    (
        "POST",
        re.compile(
            r"/api/v1/(exports|recordings/delete-preview|recordings/delete|time|update/check|update/apply)$"
        ),
    ),
)


def create_app(
    authentication: AuthenticationService,
    engine: EngineGateway,
    *,
    trusted_hosts: tuple[str, ...] = (
        "localhost",
        "127.0.0.1",
        "::1",
    ),
) -> Flask:
    """production web appを作る。engine停止時も録画の責務を持たない。"""
    package_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        static_folder=str(package_root / "static"),
        static_url_path="/static",
        template_folder=str(package_root / "templates"),
    )
    app.config.update(
        MAX_CONTENT_LENGTH=1_048_576,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        TRUSTED_HOSTS=tuple(item.lower() for item in trusted_hosts),
    )

    @app.before_request
    def secure_request() -> Response | None:
        g.request_id = secrets.token_hex(12)
        g.web_session = None
        if not _host_allowed(request.host, app.config["TRUSTED_HOSTS"]):
            return _error(400, "invalid_host", "許可されていないHostです")

        cookie_session = request.cookies.get(_SESSION_COOKIE)
        g.web_session = authentication.session_for_request(cookie_session)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin is not None and not _same_origin(
                origin, request.host, request.scheme
            ):
                return _error(403, "cross_origin", "同一originから操作してください")
            csrf = request.headers.get("X-CSRF-Token")
            if not authentication.verify_csrf(g.web_session, csrf):
                return _error(
                    403, "csrf_failed", "画面を再読み込みして再試行してください"
                )

        if request.path.startswith("/api/v1/") and request.path not in _AUTH_ROUTES:
            if not authentication.authorize(g.web_session):
                return _error(401, "authentication_required", "ログインしてください")
            if _requires_recent_admin_authentication(request.method, request.path):
                try:
                    authentication.require_recent_admin_authentication(g.web_session)
                except AuthenticationError as exc:
                    code = str(exc)
                    return _error(
                        428,
                        code,
                        "管理者用パスワードの確認が必要です",
                    )
        return None

    @app.after_request
    def secure_response(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=()")
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        session = getattr(g, "web_session", None)
        if session is not None:
            response.set_cookie(
                _SESSION_COOKIE,
                session.session_id,
                max_age=8 * 60 * 60,
                httponly=True,
                secure=request.is_secure,
                samesite="Lax",
                path="/",
            )
        return response

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/v1/auth/status")
    def auth_status() -> Response:
        state = authentication.status(g.web_session.session_id)
        return jsonify(
            {
                "login_protection_enabled": state.login_protection_enabled,
                "admin_protection_enabled": state.admin_protection_enabled,
                "login_password_configured": state.login_password_configured,
                "admin_password_configured": state.admin_password_configured,
                "authenticated": state.authenticated,
                "csrf_token": state.csrf_token,
                "revision": state.revision,
            }
        )

    @app.post("/api/v1/auth/login")
    def auth_login() -> Response:
        data = _json_object()
        _require_keys(data, {"password"})
        remote_key = request.remote_addr or "unknown"
        try:
            g.web_session = authentication.login(
                remote_key, data["password"], g.web_session
            )
        except AuthenticationError as exc:
            if str(exc) == "login_throttled":
                return _error(
                    429, "login_throttled", "しばらく待ってから再試行してください"
                )
            return _error(401, "login_failed", "パスワードを確認してください")
        return jsonify({"ok": True, "authenticated": True})

    @app.post("/api/v1/auth/logout")
    def auth_logout() -> Response:
        authentication.logout(g.web_session)
        g.web_session = authentication.session_for_request(None)
        return jsonify({"ok": True})

    @app.post("/api/v1/auth/admin/reauthenticate")
    def auth_admin_reauthenticate() -> Response:
        data = _json_object()
        _require_keys(data, {"password"})
        try:
            authentication.reauthenticate_admin(
                g.web_session, request.remote_addr or "unknown", data["password"]
            )
        except AuthenticationError as exc:
            code = str(exc)
            if code == "administrator_setup_required":
                return _error(428, code, "旧認証の管理者登録が必要です")
            if code == "login_throttled":
                return _error(429, code, "しばらく待ってから再試行してください")
            return _error(
                401, "reauthentication_failed", "パスワードを確認してください"
            )
        return jsonify({"ok": True})

    @app.post("/api/v1/auth/protections")
    def auth_protections() -> Response:
        data = _json_object()
        required = {"login_enabled", "admin_enabled", "expected_revision"}
        optional = {
            "login_password",
            "login_confirmation",
            "admin_password",
            "admin_confirmation",
        }
        if not required.issubset(data) or not set(data).issubset(required | optional):
            raise ValueError("request keyが不足または未知です")
        if (
            type(data["login_enabled"]) is not bool
            or type(data["admin_enabled"]) is not bool
            or type(data["expected_revision"]) is not int
        ):
            return _error(400, "invalid_auth_protections", "認証設定の値が不正です")
        try:
            state = authentication.update_protections(
                g.web_session,
                login_enabled=data["login_enabled"],
                admin_enabled=data["admin_enabled"],
                login_password=data.get("login_password"),
                login_confirmation=data.get("login_confirmation"),
                admin_password=data.get("admin_password"),
                admin_confirmation=data.get("admin_confirmation"),
                expected_revision=data["expected_revision"],
            )
        except RevisionConflict:
            return _error(
                409, "revision_conflict", "設定が更新されています。再読込してください"
            )
        except AuthenticationError as exc:
            code = str(exc)
            status_code = 428 if code != "authentication_required" else 401
            return _error(status_code, code, "認証状態を確認してください")
        except ValueError as exc:
            return _error(400, "invalid_auth_protections", str(exc))
        return jsonify(
            {
                "ok": True,
                "login_protection_enabled": state.login_enabled,
                "admin_protection_enabled": state.admin_enabled,
                "revision": state.revision,
            }
        )

    @app.route("/api/v1/<path:engine_path>", methods=["GET", "POST", "PATCH"])
    def engine_api(engine_path: str) -> Response:
        path = f"/api/v1/{engine_path}"
        if request.query_string:
            try:
                query = request.query_string.decode("ascii", errors="strict")
            except UnicodeDecodeError:
                return _error(400, "invalid_query", "クエリ文字列が不正です")
            path += "?" + query
        if not _engine_route_allowed(request.method, path):
            return _error(404, "not_found", "APIが見つかりません")
        clean_path = path.split("?", 1)[0]
        if request.method == "GET" and (
            clean_path.endswith(("/content", "/download"))
            or clean_path.endswith("/preview.jpg")
            or clean_path.startswith("/api/v1/live/")
        ):
            stream_request = getattr(engine, "stream", None)
            if not callable(stream_request):
                try:
                    result = engine.request(request.method, path, None)
                except Exception:
                    return _error(
                        503, "engine_unavailable", "録画engineを利用できません"
                    )
                return make_response(jsonify(result.body), result.status_code)
            try:
                streamed = stream_request(
                    request.method,
                    path.split("?", 1)[0],
                    range_header=request.headers.get("Range"),
                )
            except Exception:
                return _error(503, "engine_unavailable", "録画engineを利用できません")
            if hasattr(streamed, "chunks") and hasattr(streamed, "headers"):
                response = Response(
                    stream_with_context(streamed.chunks()),
                    status=streamed.status_code,
                    headers={
                        key: value
                        for key, value in streamed.headers.items()
                        if key
                        in {
                            "Content-Type",
                            "Content-Length",
                            "Content-Range",
                            "Accept-Ranges",
                            "Content-Disposition",
                            "Cache-Control",
                        }
                    },
                    direct_passthrough=True,
                )
                response.call_on_close(streamed.close)
                return response
            if hasattr(streamed, "body"):
                return make_response(jsonify(streamed.body), streamed.status_code)
            return _error(503, "engine_unavailable", "録画engineを利用できません")
        body = None if request.method == "GET" else _json_object()
        try:
            result = engine.request(request.method, path, body)
        except Exception:
            return _error(503, "engine_unavailable", "録画engineを利用できません")
        return make_response(jsonify(result.body), result.status_code)

    @app.errorhandler(413)
    def body_too_large(_: Exception) -> Response:
        return _error(413, "request_too_large", "送信データが大きすぎます")

    @app.errorhandler(ValueError)
    def invalid_request(error: ValueError) -> Response:
        # 内部の検証messageをそのまま返さず、詳細はserver側logにだけ残す。
        app.logger.warning("invalid request rejected: %s", error)
        return _error(400, "invalid_request", "リクエストが不正です")

    return app


def _json_object() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("JSON objectが必要です")
    return value


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError("request keyが不足または未知です")


def _engine_route_allowed(method: str, path: str) -> bool:
    clean_path = urlsplit(path).path
    return any(
        expected_method == method and pattern.fullmatch(clean_path)
        for expected_method, pattern in _ROUTE_PATTERNS
    )


def _requires_recent_admin_authentication(method: str, path: str) -> bool:
    return (method == "PATCH" and path == "/api/v1/settings") or (
        method == "POST"
        and path
        in {
            "/api/v1/recordings/delete",
            "/api/v1/time",
            "/api/v1/update/apply",
            "/api/v1/auth/protections",
        }
    )


def _host_allowed(host: str, trusted_hosts: tuple[str, ...]) -> bool:
    parsed = urlsplit(f"//{host}")
    hostname = parsed.hostname
    if hostname is None or parsed.username is not None or parsed.password is not None:
        return False
    normalized = hostname.lower().rstrip(".")
    if normalized in trusted_hosts:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback


def _same_origin(origin: str, host: str, scheme: str) -> bool:
    parsed = urlsplit(origin)
    return (
        parsed.scheme.lower() == scheme.lower()
        and parsed.netloc.lower() == host.lower()
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _error(status_code: int, code: str, message: str) -> Response:
    return make_response(
        jsonify(
            {
                "error": {"code": code, "message": message},
                "request_id": getattr(g, "request_id", "unknown"),
            }
        ),
        status_code,
    )
