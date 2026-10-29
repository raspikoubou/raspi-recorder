"""初回設定、login、server-side session、管理者認証を管理する。"""

from __future__ import annotations

import hmac
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock

from raspi_recorder.domain.errors import RevisionConflict
from raspi_recorder.ports.authentication import (
    CredentialRepository,
    CredentialSnapshot,
    PasswordHasher,
)
from raspi_recorder.ports.clock import Clock

_NANOSECONDS_PER_SECOND = 1_000_000_000
_IDLE_TIMEOUT_NS = 30 * 60 * _NANOSECONDS_PER_SECOND
_ABSOLUTE_TIMEOUT_NS = 8 * 60 * 60 * _NANOSECONDS_PER_SECOND
_ADMIN_AUTH_TIMEOUT_NS = 10 * 60 * _NANOSECONDS_PER_SECOND
_LOGIN_WINDOW_NS = 15 * 60 * _NANOSECONDS_PER_SECOND
_LOGIN_COOLDOWN_NS = 30 * _NANOSECONDS_PER_SECOND
_MAX_LOGIN_FAILURES = 5
_MAX_SERVER_SESSIONS = 1_024
_MAX_LOGIN_RATE_KEYS = 1_024
_SECURITY_STATE_PRUNE_NS = 60 * _NANOSECONDS_PER_SECOND


class AuthenticationError(RuntimeError):
    """認証失敗または不正な認証state。"""


@dataclass(slots=True)
class ServerSession:
    """process内で保持し、browser cookieにはopaque IDだけを出す。"""

    session_id: str
    csrf_token: str
    credential_revision: int
    created_mono_ns: int
    last_activity_mono_ns: int
    authenticated_mono_ns: int | None = None
    admin_authenticated_mono_ns: int | None = None


@dataclass(frozen=True, slots=True)
class AuthenticationStatus:
    """秘密を含めない画面向け認証状態。"""

    login_protection_enabled: bool
    admin_protection_enabled: bool
    login_password_configured: bool
    admin_password_configured: bool
    authenticated: bool
    csrf_token: str
    revision: int


class AuthenticationService:
    """認証stateとsessionのsingle-process owner。"""

    def __init__(
        self,
        repository: CredentialRepository,
        hasher: PasswordHasher,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._clock = clock
        self._lock = RLock()
        self._sessions: dict[str, ServerSession] = {}
        self._failures: dict[str, deque[int]] = defaultdict(deque)
        self._cooldown_until: dict[str, int] = {}
        self._last_session_prune_ns: int | None = None
        self._last_login_prune_ns: int | None = None

    def initialize(self) -> bool:
        """認証DBを準備する。利用者向けtokenは生成しない。"""
        return self._repository.initialize()

    def status(self, session_id: str | None) -> AuthenticationStatus:
        """必要ならanonymous sessionを作り、安全な状態だけを返す。"""
        with self._lock:
            session = self._resolve_session_locked(session_id)
            if session is None:
                session = self._new_session_locked()
            state = self._repository.load()
            return AuthenticationStatus(
                login_protection_enabled=state.login_enabled,
                admin_protection_enabled=state.admin_enabled,
                login_password_configured=state.login_password_hash is not None,
                admin_password_configured=state.admin_password_hash is not None,
                authenticated=self._is_authenticated_locked(state, session),
                csrf_token=session.csrf_token,
                revision=state.revision,
            )

    def session_for_request(self, session_id: str | None) -> ServerSession:
        """expired/invalid cookieをanonymous sessionへ置き換える。"""
        with self._lock:
            session = self._resolve_session_locked(session_id)
            return self._new_session_locked() if session is None else session

    def authorize(self, session: ServerSession) -> bool:
        """認証OFF、または有効なlogin sessionだけ許可する。"""
        with self._lock:
            if self._sessions.get(session.session_id) is not session:
                return False
            state = self._repository.load()
            return self._is_authenticated_locked(state, session)

    def require_recent_admin_authentication(self, session: ServerSession) -> None:
        """管理者保護ONの場合だけ同一sessionの10分以内の確認を要求する。"""
        with self._lock:
            if not self.authorize(session):
                raise AuthenticationError("authentication_required")
            state = self._repository.load()
            if not state.admin_enabled:
                return
            if state.admin_password_hash is None:
                raise AuthenticationError("administrator_setup_required")
            now = self._clock.monotonic_ns()
            confirmed = session.admin_authenticated_mono_ns
            if confirmed is None or now - confirmed > _ADMIN_AUTH_TIMEOUT_NS:
                raise AuthenticationError("reauthentication_required")

    def verify_csrf(self, session: ServerSession, token: object) -> bool:
        """同一sessionのCSRF tokenをconstant-timeで照合する。"""
        return (
            isinstance(token, str)
            and bool(token)
            and token.isascii()
            and hmac.compare_digest(session.csrf_token, token)
        )

    def login(
        self,
        remote_key: str,
        password: str,
        current_session: ServerSession | None = None,
    ) -> ServerSession:
        """login passwordを照合し、このbrowserのsessionを更新する。"""
        remote_key = self._remote_key("login", remote_key)
        if not isinstance(password, str) or len(password) > 1_024:
            raise AuthenticationError("login_failed")
        with self._lock:
            now = self._clock.monotonic_ns()
            self._check_password_attempt_locked(remote_key, now)
            state = self._repository.load()
            valid = state.login_password_hash is not None and self._hasher.verify(
                state.login_password_hash, password
            )
            if not valid:
                self._record_password_failure_locked(remote_key, now)
                raise AuthenticationError("login_failed")
            self._clear_password_failure_locked(remote_key)
            if (
                current_session is not None
                and self._sessions.get(current_session.session_id) is current_session
            ):
                self._sessions.pop(current_session.session_id, None)
            return self._new_authenticated_session_locked()

    def reauthenticate_admin(
        self, session: ServerSession, remote_key: str, password: str
    ) -> None:
        """管理者用passwordを再確認し、期限付きadmin権限を与える。"""
        remote_key = self._remote_key("admin", remote_key)
        if not isinstance(password, str) or len(password) > 1_024:
            raise AuthenticationError("reauthentication_failed")
        with self._lock:
            now = self._clock.monotonic_ns()
            # Re-authentication already requires an authenticated session, so
            # the shared login bucket must neither block nor be fed by it;
            # the per-source admin bucket still throttles brute force.
            self._check_password_attempt_locked(remote_key, now, shared=False)
            state = self._repository.load()
            if not state.admin_enabled:
                raise AuthenticationError("administrator_protection_disabled")
            if state.admin_password_hash is None:
                raise AuthenticationError("administrator_setup_required")
            valid = self._is_authenticated_locked(
                state, session
            ) and self._hasher.verify(state.admin_password_hash, password)
            if not valid:
                self._record_password_failure_locked(remote_key, now, shared=False)
                raise AuthenticationError("reauthentication_failed")
            self._clear_password_failure_locked(remote_key, shared=False)
            # Rotate the session id on privilege confirmation, as login
            # does, without extending the absolute session lifetime. The
            # CSRF token is intentionally kept: the web client retries the
            # original request with the token it already holds after a 428
            # re-authentication round trip, so rotating it here would break
            # that flow.
            self._sessions.pop(session.session_id, None)
            session.session_id = secrets.token_urlsafe(32)
            self._sessions[session.session_id] = session
            session.last_activity_mono_ns = now
            session.admin_authenticated_mono_ns = now

    def update_protections(
        self,
        session: ServerSession,
        *,
        login_enabled: bool,
        admin_enabled: bool,
        login_password: str | None,
        login_confirmation: str | None,
        admin_password: str | None,
        admin_confirmation: str | None,
        expected_revision: int,
    ) -> CredentialSnapshot:
        """用途別の保護とpasswordを保存し、古い権限を失効する。"""
        self._validate_expected_revision(expected_revision)
        if type(login_enabled) is not bool or type(admin_enabled) is not bool:
            raise ValueError("保護設定はboolで指定してください")
        login_password = self._optional_password(
            login_password, login_confirmation, "ログイン用"
        )
        admin_password = self._optional_password(
            admin_password, admin_confirmation, "管理者用"
        )
        with self._lock:
            state = self._repository.load()
            if not self.authorize(session):
                raise AuthenticationError("authentication_required")
            # 今回のrequest bodyでOFFを指定しても、現行admin保護を通過できない。
            if state.admin_enabled:
                self.require_recent_admin_authentication(session)
            if state.revision != expected_revision:
                raise RevisionConflict(expected_revision, state.revision)
            login_hash = (
                self._hasher.hash(login_password)
                if login_password is not None
                else None
            )
            admin_hash = (
                self._hasher.hash(admin_password)
                if admin_password is not None
                else None
            )
            if (
                login_enabled
                and state.login_password_hash is None
                and login_hash is None
            ):
                raise ValueError("ログイン保護をONにするにはパスワード登録が必要です")
            if (
                admin_enabled
                and state.admin_password_hash is None
                and admin_hash is None
            ):
                raise ValueError("管理者保護をONにするにはパスワード登録が必要です")
            saved = self._repository.update_protections(
                login_enabled=login_enabled,
                admin_enabled=admin_enabled,
                login_password_hash=login_hash,
                admin_password_hash=admin_hash,
                expected_revision=expected_revision,
            )
            self._sessions.clear()
            return saved

    def logout(self, session: ServerSession) -> None:
        """指定sessionを削除する。"""
        with self._lock:
            self._sessions.pop(session.session_id, None)

    def _resolve_session_locked(self, session_id: str | None) -> ServerSession | None:
        if not isinstance(session_id, str) or not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.credential_revision != self._repository.load().revision:
            self._sessions.pop(session_id, None)
            return None
        now = self._clock.monotonic_ns()
        if (
            now - session.last_activity_mono_ns > _IDLE_TIMEOUT_NS
            or now - session.created_mono_ns > _ABSOLUTE_TIMEOUT_NS
        ):
            self._sessions.pop(session_id, None)
            return None
        session.last_activity_mono_ns = now
        return session

    def _new_session_locked(self) -> ServerSession:
        now = self._clock.monotonic_ns()
        self._prune_sessions_locked(
            now, force=len(self._sessions) >= _MAX_SERVER_SESSIONS
        )
        session = ServerSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            credential_revision=self._repository.load().revision,
            created_mono_ns=now,
            last_activity_mono_ns=now,
        )
        self._sessions[session.session_id] = session
        return session

    def _prune_sessions_locked(self, now: int, *, force: bool = False) -> None:
        last_prune = self._last_session_prune_ns
        if (
            not force
            and last_prune is not None
            and now - last_prune < _SECURITY_STATE_PRUNE_NS
        ):
            return
        for session_id, session in tuple(self._sessions.items()):
            if (
                now - session.last_activity_mono_ns > _IDLE_TIMEOUT_NS
                or now - session.created_mono_ns > _ABSOLUTE_TIMEOUT_NS
            ):
                self._sessions.pop(session_id, None)
        if len(self._sessions) >= _MAX_SERVER_SESSIONS:
            victim = min(
                self._sessions.values(),
                key=lambda session: (
                    session.authenticated_mono_ns is not None,
                    session.last_activity_mono_ns,
                    session.created_mono_ns,
                ),
            )
            self._sessions.pop(victim.session_id, None)
        self._last_session_prune_ns = now

    def _prune_login_state_locked(self, now: int) -> None:
        last_prune = self._last_login_prune_ns
        if last_prune is not None and now - last_prune < _SECURITY_STATE_PRUNE_NS:
            return
        for key, failures in tuple(self._failures.items()):
            while failures and now - failures[0] > _LOGIN_WINDOW_NS:
                failures.popleft()
            if not failures and key != "__global__":
                self._failures.pop(key, None)
        for key, cooldown in tuple(self._cooldown_until.items()):
            if cooldown <= now:
                self._cooldown_until.pop(key, None)
        self._last_login_prune_ns = now

    def _check_password_attempt_locked(
        self, key: str, now: int, *, shared: bool = True
    ) -> None:
        self._prune_login_state_locked(now)
        self._ensure_login_key_locked(key)
        rate_keys = (key, "__global__") if shared else (key,)
        for rate_key in rate_keys:
            cooldown = self._cooldown_until.get(rate_key, 0)
            if cooldown > now:
                raise AuthenticationError("login_throttled")
            failures = self._failures[rate_key]
            while failures and now - failures[0] > _LOGIN_WINDOW_NS:
                failures.popleft()

    def _record_password_failure_locked(
        self, key: str, now: int, *, shared: bool = True
    ) -> None:
        rate_keys = (key, "__global__") if shared else (key,)
        for rate_key in rate_keys:
            failures = self._failures[rate_key]
            failures.append(now)
            while failures and now - failures[0] > _LOGIN_WINDOW_NS:
                failures.popleft()
            if rate_key == "__global__":
                # Arm the shared cooldown only when the threshold is first
                # crossed. Continued attack traffic must not keep renewing
                # the lockout for every legitimate user.
                if len(failures) == _MAX_LOGIN_FAILURES:
                    self._cooldown_until[rate_key] = now + _LOGIN_COOLDOWN_NS
            elif len(failures) >= _MAX_LOGIN_FAILURES:
                self._cooldown_until[rate_key] = now + _LOGIN_COOLDOWN_NS

    def _clear_password_failure_locked(self, key: str, *, shared: bool = True) -> None:
        rate_keys = (key, "__global__") if shared else (key,)
        for rate_key in rate_keys:
            self._failures.pop(rate_key, None)
            self._cooldown_until.pop(rate_key, None)

    def _ensure_login_key_locked(self, key: str) -> None:
        if key == "__global__" or key in self._failures:
            return
        remote_keys = tuple(item for item in self._failures if item != "__global__")
        if len(remote_keys) >= _MAX_LOGIN_RATE_KEYS:
            oldest = min(
                remote_keys,
                key=lambda item: (
                    self._failures[item][-1] if self._failures[item] else 0
                ),
            )
            self._failures.pop(oldest, None)
            self._cooldown_until.pop(oldest, None)
        self._failures[key] = deque()

    def _new_authenticated_session_locked(
        self, *, admin_confirmed: bool = False
    ) -> ServerSession:
        session = self._new_session_locked()
        now = self._clock.monotonic_ns()
        session.authenticated_mono_ns = now
        if admin_confirmed:
            session.admin_authenticated_mono_ns = now
        session.last_activity_mono_ns = now
        return session

    def _is_authenticated_locked(
        self, state: CredentialSnapshot, session: ServerSession
    ) -> bool:
        if (
            self._sessions.get(session.session_id) is not session
            or session.credential_revision != state.revision
        ):
            return False
        if not state.login_enabled:
            return True
        if state.login_password_hash is None:
            return False
        now = self._clock.monotonic_ns()
        authenticated_at = session.authenticated_mono_ns
        return (
            authenticated_at is not None
            and now - authenticated_at <= _ABSOLUTE_TIMEOUT_NS
            and now - session.last_activity_mono_ns <= _IDLE_TIMEOUT_NS
        )

    @staticmethod
    def _remote_key(prefix: str, remote_key: str) -> str:
        value = remote_key if isinstance(remote_key, str) and remote_key else "unknown"
        return f"{prefix}:{value[:128]}"

    @staticmethod
    def _validate_password(password: str, confirmation: str) -> None:
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("パスワードは8～128文字で指定してください")
        if password != confirmation:
            raise ValueError("確認用パスワードが一致しません")

    @classmethod
    def _optional_password(
        cls,
        password: str | None,
        confirmation: str | None,
        label: str,
    ) -> str | None:
        """空欄のままなら既存hashを保ち、入力時だけ対確認する。"""
        if password is None and confirmation is None:
            return None
        if password == "" and confirmation == "":
            return None
        if not isinstance(password, str) or not isinstance(confirmation, str):
            raise ValueError(f"{label}パスワードと確認入力を両方指定してください")
        try:
            cls._validate_password(password, confirmation)
        except ValueError as exc:
            raise ValueError(f"{label}{exc}") from exc
        return password

    @staticmethod
    def _validate_expected_revision(value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("expected_revisionは0以上の整数が必要です")
