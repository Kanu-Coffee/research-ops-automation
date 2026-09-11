"""Protected Web accounts, revocable sessions and task-scoped read access.

This service has no SMTP dependency and never places credentials in a worker
workspace. Existing CLI and worker commands continue to use host authority.
"""

from contextlib import closing
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Callable, Optional

from researchops.errors import NotFoundError, ResearchOpsError, ValidationError


SESSION_IDLE_SECONDS = 2 * 60 * 60
SESSION_ABSOLUTE_SECONDS = 12 * 60 * 60
PREAUTH_SECONDS = 15 * 60
BOOTSTRAP_SECONDS = 30 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_ACCOUNT_LIMIT = 10
LOGIN_GLOBAL_LIMIT = 60
_PASSWORD_SLOTS = threading.BoundedSemaphore(2)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}")


class AuthRequiredError(ResearchOpsError):
    status_code = 401


class AuthenticationError(AuthRequiredError):
    pass


class AuthorizationError(ResearchOpsError):
    status_code = 403


class AuthRateLimitError(ResearchOpsError):
    status_code = 429
    retry_after = LOGIN_WINDOW_SECONDS


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    display_name: str
    role: str
    must_change_password: bool = False

    @property
    def audit_actor(self):
        return "user:" + self.user_id

    @property
    def is_admin(self):
        return self.role == "admin" and not self.must_change_password


@dataclass(frozen=True)
class BrowserSession:
    principal: Optional[Principal]
    token_digest: str
    csrf_token: str
    kind: str
    created_at: float
    last_seen_at: float
    expires_at: float


@dataclass(frozen=True)
class SessionGrant:
    token: str
    session: BrowserSession


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _validate_password(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 128:
        raise ValidationError("비밀번호는 15~128자로 입력하세요.")
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        raise ValidationError("비밀번호에 사용할 수 없는 문자가 있습니다.") from None
    if "\x00" in password:
        raise ValidationError("비밀번호에 사용할 수 없는 문자가 있습니다.")
    return encoded


def _derive(password: bytes, salt: bytes) -> bytes:
    # Bound the aggregate 128 MiB derivation memory in the threaded Web server.
    if not _PASSWORD_SLOTS.acquire(blocking=False):
        raise AuthRateLimitError("잠시 후 다시 로그인하세요.")
    try:
        return hashlib.scrypt(password, salt=salt, n=2**17, r=8, p=1,
                              dklen=64, maxmem=256 * 1024 * 1024)
    finally:
        _PASSWORD_SLOTS.release()


def hash_password(password: str) -> str:
    value = _validate_password(password)
    salt = secrets.token_bytes(16)
    derived = _derive(value, salt)
    return "scrypt$131072$8$1$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(derived).decode("ascii")


def verify_password(encoded: Optional[str], password: str) -> bool:
    # Missing accounts still perform the same bounded derivation. Passwords are
    # never truncated or normalized, including when checking an old credential.
    valid = True
    try:
        value = _validate_password(password)
    except ValidationError:
        value, valid = b"invalid-password-placeholder", False
    salt, expected = bytes(16), bytes(64)
    try:
        algorithm, n, r, p, salt_text, digest_text = (encoded or "").split("$")
        if (algorithm, n, r, p) != ("scrypt", "131072", "8", "1"):
            raise ValueError
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
        if len(salt) != 16 or len(expected) != 64:
            raise ValueError
    except (ValueError, TypeError):
        salt, expected, valid = bytes(16), bytes(64), False
    return hmac.compare_digest(_derive(value, salt), expected) and valid


def _username(value):
    if not isinstance(value, str):
        raise ValidationError("아이디는 영문 소문자, 숫자, 점, 밑줄, 하이픈 3~64자로 입력하세요.")
    value = value.strip().lower()
    if not _USERNAME.fullmatch(value):
        raise ValidationError("아이디는 영문 소문자, 숫자, 점, 밑줄, 하이픈 3~64자로 입력하세요.")
    return value


def _display_name(value, fallback):
    value = value.strip() if isinstance(value, str) else ""
    value = value or fallback
    if len(value) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError("표시 이름은 80자 이내로 입력하세요.")
    return value


def _principal(row):
    return Principal(row["user_id"], row["username"], row["display_name"], row["role"],
                     bool(row["must_change_password"]))


class AuthService:
    def __init__(self, settings, db, *, clock: Callable[[], float] = time.time):
        self.settings, self.db, self._clock = settings, db, clock

    def initialized(self) -> bool:
        with closing(self.db.get_connection()) as conn:
            return conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone() is not None

    def _audit(self, conn, actor, event_type, user_id, details=None):
        from datetime import datetime, timezone
        conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,actor,occurred_at)
            VALUES('user',?,?,?,?,?)""", (user_id, event_type,
            json.dumps(details or {}, ensure_ascii=False), actor,
            datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()))

    def issue_setup_token(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone():
                raise ValidationError("초기 관리자 설정이 이미 완료되었습니다.")
            conn.execute("DELETE FROM auth_bootstrap")
            conn.execute("INSERT INTO auth_bootstrap(singleton,token_hash,expires_at) VALUES(1,?,?)",
                         (token_digest(token), now + BOOTSTRAP_SECONDS))
            self._audit(conn, "host-cli", "auth_setup_token_issued", "bootstrap")
        return token

    def _new_session(self, conn, user=None):
        now = self._clock()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        kind = "authenticated" if user is not None else "preauth"
        expires_at = now + (SESSION_ABSOLUTE_SECONDS if user is not None else PREAUTH_SECONDS)
        digest = token_digest(token)
        conn.execute("DELETE FROM auth_sessions WHERE expires_at<=? OR (kind='authenticated' AND last_seen_at<=?)",
                     (now, now - SESSION_IDLE_SECONDS))
        if user is None:
            # Public login pages cannot grow the session store without a bound.
            count = conn.execute("SELECT COUNT(*) FROM auth_sessions WHERE kind='preauth'").fetchone()[0]
            if count >= 1000:
                raise AuthRateLimitError("잠시 후 다시 로그인 화면을 여세요.")
        conn.execute("""INSERT INTO auth_sessions(token_hash,user_id,kind,csrf_token,created_at,
            last_seen_at,expires_at,credential_revision) VALUES(?,?,?,?,?,?,?,?)""",
            (digest, user["user_id"] if user else None, kind, csrf, now, now, expires_at,
             user["credential_revision"] if user else None))
        return SessionGrant(token, BrowserSession(_principal(user) if user else None, digest, csrf,
                                                  kind, now, now, expires_at))

    def new_preauth_session(self) -> SessionGrant:
        with self.db.transaction() as conn:
            return self._new_session(conn)

    def resolve_session(self, raw_token, *, touch=True) -> Optional[BrowserSession]:
        if not isinstance(raw_token, str) or not _TOKEN.fullmatch(raw_token):
            return None
        now = self._clock()
        # Polling does not acquire a write transaction or extend idle lifetime.
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT * FROM auth_sessions WHERE token_hash=?", (token_digest(raw_token),)).fetchone()
            if row is None or row["expires_at"] <= now:
                return None
            user = None
            if row["kind"] == "authenticated":
                if row["last_seen_at"] + SESSION_IDLE_SECONDS <= now:
                    return None
                user = conn.execute("SELECT * FROM auth_users WHERE user_id=?", (row["user_id"],)).fetchone()
                if (user is None or not user["active"] or
                        user["credential_revision"] != row["credential_revision"]):
                    return None
            last_seen = row["last_seen_at"]
            if touch and user is not None:
                # The predicates prevent a concurrent disable/reset/logout from
                # resurrecting or extending a revoked session.
                changed = conn.execute("""UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?
                    AND expires_at>? AND last_seen_at>? AND EXISTS(
                    SELECT 1 FROM auth_users u WHERE u.user_id=auth_sessions.user_id
                    AND u.active=1 AND u.credential_revision=auth_sessions.credential_revision)""",
                    (now, row["token_hash"], now, now-SESSION_IDLE_SECONDS)).rowcount
                conn.commit()
                if not changed:
                    return None
                last_seen = now
            return BrowserSession(_principal(user) if user else None, row["token_hash"], row["csrf_token"],
                                  row["kind"], row["created_at"], last_seen, row["expires_at"])

    def logout(self, raw_token):
        if isinstance(raw_token, str) and _TOKEN.fullmatch(raw_token):
            with self.db.transaction() as conn:
                row = conn.execute("SELECT user_id FROM auth_sessions WHERE token_hash=?", (token_digest(raw_token),)).fetchone()
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_digest(raw_token),))
                if row and row["user_id"]:
                    self._audit(conn, "user:" + row["user_id"], "auth_logout", row["user_id"])

    def _reserve_attempt(self, username):
        now = self._clock()
        key = "account:" + hashlib.sha256(username.encode("utf-8", errors="replace")).hexdigest()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM auth_login_attempts WHERE window_started<=?", (now - LOGIN_WINDOW_SECONDS,))
            for attempt_key, limit in (("global", LOGIN_GLOBAL_LIMIT), (key, LOGIN_ACCOUNT_LIMIT)):
                row = conn.execute("SELECT attempt_count FROM auth_login_attempts WHERE attempt_key=?", (attempt_key,)).fetchone()
                if row and row[0] >= limit:
                    raise AuthRateLimitError("로그인 시도가 많습니다. 5분 후 다시 시도하세요.")
            for attempt_key in ("global", key):
                conn.execute("""INSERT INTO auth_login_attempts(attempt_key,window_started,attempt_count)
                    VALUES(?,?,1) ON CONFLICT(attempt_key) DO UPDATE SET attempt_count=attempt_count+1""", (attempt_key, now))
        return key

    def setup(self, bootstrap_token, username, password, display_name="") -> SessionGrant:
        username = _username(username)
        display_name = _display_name(display_name, username)
        self._reserve_attempt("bootstrap")
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT * FROM auth_bootstrap WHERE singleton=1").fetchone()
            valid = (isinstance(bootstrap_token, str) and _TOKEN.fullmatch(bootstrap_token) and
                     row is not None and row["expires_at"] > self._clock() and
                     hmac.compare_digest(row["token_hash"], token_digest(bootstrap_token)))
        if not valid:
            raise AuthenticationError("초기 설정 토큰이 올바르지 않거나 만료되었습니다.")
        encoded = hash_password(password)
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone():
                raise ValidationError("초기 관리자 설정이 이미 완료되었습니다.")
            consumed = conn.execute("DELETE FROM auth_bootstrap WHERE singleton=1 AND token_hash=? AND expires_at>?",
                                    (token_digest(bootstrap_token), self._clock())).rowcount
            if not consumed:
                raise AuthenticationError("초기 설정 토큰이 올바르지 않거나 만료되었습니다.")
            user_id = "usr-" + secrets.token_hex(12)
            now = self._clock()
            conn.execute("""INSERT INTO auth_users(user_id,username,display_name,password_hash,role,active,
                must_change_password,credential_revision,created_at,updated_at) VALUES(?,?,?,?,'admin',1,0,1,?,?)""",
                (user_id, username, display_name, encoded, now, now))
            user = conn.execute("SELECT * FROM auth_users WHERE user_id=?", (user_id,)).fetchone()
            from researchops.services.ownership import adopt_unowned
            adopt_unowned(conn, user_id)
            self._audit(conn, "user:" + user_id, "auth_initial_admin_created", user_id)
            return self._new_session(conn, user)

    def login(self, username, password) -> SessionGrant:
        normalized = username.strip().lower() if isinstance(username, str) else ""
        key = self._reserve_attempt(normalized[:256])
        with closing(self.db.get_connection()) as conn:
            user = conn.execute("SELECT * FROM auth_users WHERE username=?", (normalized,)).fetchone() if _USERNAME.fullmatch(normalized) else None
        valid = verify_password(user["password_hash"] if user else None, password)
        if not valid or user is None or not user["active"]:
            raise AuthenticationError("아이디 또는 비밀번호가 올바르지 않습니다.")
        with self.db.transaction() as conn:
            current = conn.execute("SELECT * FROM auth_users WHERE user_id=?", (user["user_id"],)).fetchone()
            if (current is None or not current["active"] or
                    current["credential_revision"] != user["credential_revision"]):
                raise AuthenticationError("아이디 또는 비밀번호가 올바르지 않습니다.")
            conn.execute("DELETE FROM auth_login_attempts WHERE attempt_key=?", (key,))
            self._audit(conn, "user:" + current["user_id"], "auth_login", current["user_id"])
            return self._new_session(conn, current)

    def _current_user(self, conn, principal, *, admin=False, allow_password_change=False):
        if not isinstance(principal, Principal):
            raise AuthRequiredError("로그인이 필요합니다.")
        row = conn.execute("SELECT * FROM auth_users WHERE user_id=?", (principal.user_id,)).fetchone()
        if row is None or not row["active"]:
            raise AuthRequiredError("다시 로그인하세요.")
        if not allow_password_change and row["must_change_password"]:
            raise AuthorizationError("먼저 비밀번호를 변경하세요.")
        if admin and row["role"] != "admin":
            raise AuthorizationError("관리자 권한이 필요합니다.")
        return row

    def require_admin(self, principal):
        with closing(self.db.get_connection()) as conn:
            return _principal(self._current_user(conn, principal, admin=True))

    @staticmethod
    def _task_ids(conn, task_ids):
        if isinstance(task_ids, (str, bytes)) or task_ids is None:
            raise ValidationError("허용할 작업 목록을 선택하세요.")
        try:
            values = list(task_ids)
        except TypeError:
            raise ValidationError("허용할 작업 목록을 선택하세요.") from None
        if len(values) > 10000 or any(not isinstance(item, str) or not item or len(item) > 128 for item in values):
            raise ValidationError("허용할 작업 목록이 올바르지 않습니다.")
        values = sorted(set(values))
        for task_id in values:
            if not conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
                raise ValidationError("존재하는 작업을 선택하세요.")
        return values

    @staticmethod
    def _user_view(conn, row):
        counts = dict(conn.execute("SELECT kind,COUNT(*) FROM entity_catalog WHERE owner_user_id=? GROUP BY kind", (row["user_id"],)))
        return {"user_id": row["user_id"], "username": row["username"], "display_name": row["display_name"],
                "role": row["role"], "active": bool(row["active"]),
                "must_change_password": bool(row["must_change_password"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "owned_task_count": counts.get("task", 0),
                "owned_sender_count": counts.get("sender", 0),
                "owned_recipient_group_count": counts.get("recipient_group", 0),
                "task_ids": [r[0] for r in conn.execute("SELECT task_id FROM auth_user_tasks WHERE user_id=? ORDER BY task_id", (row["user_id"],))]}

    def list_users(self, actor):
        with closing(self.db.get_connection()) as conn:
            self._current_user(conn, actor, admin=True)
            return [self._user_view(conn, row) for row in conn.execute("SELECT * FROM auth_users ORDER BY username")]

    def create_user(self, actor, username, display_name, role, temporary_password, task_ids=()):
        self.require_admin(actor)
        username = _username(username)
        display_name = _display_name(display_name, username)
        if role not in ("admin", "user", "viewer"):
            raise ValidationError("권한은 관리자, 사용자 또는 조회자로 선택하세요.")
        encoded = hash_password(temporary_password)
        with self.db.transaction() as conn:
            self._current_user(conn, actor, admin=True)
            grants = self._task_ids(conn, task_ids)
            if conn.execute("SELECT 1 FROM auth_users WHERE username=?", (username,)).fetchone():
                raise ValidationError("이미 사용 중인 아이디입니다.")
            user_id, now = "usr-" + secrets.token_hex(12), self._clock()
            conn.execute("""INSERT INTO auth_users(user_id,username,display_name,password_hash,role,active,
                must_change_password,credential_revision,created_at,updated_at) VALUES(?,?,?,?,?,1,1,1,?,?)""",
                (user_id, username, display_name, encoded, role, now, now))
            if role == "viewer":
                conn.executemany("INSERT INTO auth_user_tasks(user_id,task_id) VALUES(?,?)", ((user_id, task_id) for task_id in grants))
            self._audit(conn, actor.audit_actor, "auth_user_created", user_id, {"role": role, "task_ids": grants if role == "viewer" else []})
            return self._user_view(conn, conn.execute("SELECT * FROM auth_users WHERE user_id=?", (user_id,)).fetchone())

    def update_user(self, actor, user_id, *, display_name=None, role=None, active=None, task_ids=None):
        if role is not None and role not in ("admin", "user", "viewer"):
            raise ValidationError("권한은 관리자, 사용자 또는 조회자로 선택하세요.")
        if active is not None and type(active) is not bool:
            raise ValidationError("계정 상태가 올바르지 않습니다.")
        with self.db.transaction() as conn:
            self._current_user(conn, actor, admin=True)
            row = conn.execute("SELECT * FROM auth_users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise NotFoundError("계정을 찾을 수 없습니다.")
            new_role = role if role is not None else row["role"]
            new_active = active if active is not None else bool(row["active"])
            if row["role"] == "admin" and row["active"] and (new_role != "admin" or not new_active):
                if conn.execute("SELECT COUNT(*) FROM auth_users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise ValidationError("마지막 활성 관리자는 비활성화하거나 조회자로 변경할 수 없습니다.")
            name = _display_name(display_name, row["username"]) if display_name is not None else row["display_name"]
            grants = self._task_ids(conn, task_ids) if task_ids is not None else None
            revoke = new_role != row["role"] or new_active != bool(row["active"])
            conn.execute("""UPDATE auth_users SET display_name=?,role=?,active=?,updated_at=?,
                credential_revision=credential_revision+? WHERE user_id=?""", (name, new_role, int(new_active), self._clock(), int(revoke), user_id))
            if revoke:
                conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            if new_role != "viewer" or grants is not None or new_role != row["role"]:
                conn.execute("DELETE FROM auth_user_tasks WHERE user_id=?", (user_id,))
                if new_role == "viewer" and grants is not None:
                    conn.executemany("INSERT INTO auth_user_tasks(user_id,task_id) VALUES(?,?)", ((user_id, task_id) for task_id in grants))
            self._audit(conn, actor.audit_actor, "auth_user_updated", user_id,
                        {"role": new_role, "active": new_active, "grants_changed": grants is not None})
            return self._user_view(conn, conn.execute("SELECT * FROM auth_users WHERE user_id=?", (user_id,)).fetchone())

    def reset_password(self, actor, user_id, new_password):
        self.require_admin(actor)
        encoded = hash_password(new_password)
        with self.db.transaction() as conn:
            self._current_user(conn, actor, admin=True)
            if not conn.execute("SELECT 1 FROM auth_users WHERE user_id=?", (user_id,)).fetchone():
                raise NotFoundError("계정을 찾을 수 없습니다.")
            self._replace_password(conn, user_id, encoded, must_change=True)
            self._audit(conn, actor.audit_actor, "auth_password_reset", user_id)

    def reset_password_from_cli(self, username, new_password):
        """Host authority recovery does not silently reactivate a disabled user."""
        username, encoded = _username(username), hash_password(new_password)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM auth_users WHERE username=?", (username,)).fetchone()
            if row is None:
                raise NotFoundError("계정을 찾을 수 없습니다.")
            self._replace_password(conn, row["user_id"], encoded, must_change=False)
            self._audit(conn, "host-cli", "auth_password_recovered", row["user_id"])

    def _replace_password(self, conn, user_id, encoded, *, must_change):
        conn.execute("""UPDATE auth_users SET password_hash=?,must_change_password=?,
            credential_revision=credential_revision+1,updated_at=? WHERE user_id=?""",
            (encoded, int(must_change), self._clock(), user_id))
        conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    def change_password(self, principal, current_password, new_password) -> SessionGrant:
        with closing(self.db.get_connection()) as conn:
            row = self._current_user(conn, principal, allow_password_change=True)
        if not verify_password(row["password_hash"], current_password):
            raise AuthenticationError("현재 비밀번호가 올바르지 않습니다.")
        if current_password == new_password:
            raise ValidationError("현재 비밀번호와 다른 비밀번호를 입력하세요.")
        encoded = hash_password(new_password)
        with self.db.transaction() as conn:
            current = self._current_user(conn, principal, allow_password_change=True)
            if current["credential_revision"] != row["credential_revision"]:
                raise AuthenticationError("인증정보가 변경되었습니다. 다시 로그인하세요.")
            self._replace_password(conn, current["user_id"], encoded, must_change=False)
            self._audit(conn, principal.audit_actor, "auth_password_changed", principal.user_id)
            return self._new_session(conn, conn.execute("SELECT * FROM auth_users WHERE user_id=?", (principal.user_id,)).fetchone())

    def allowed_task_ids(self, principal):
        with closing(self.db.get_connection()) as conn:
            row = self._current_user(conn, principal)
            if row["role"] == "admin":
                return None
            if row["role"] == "user":
                return [r[0] for r in conn.execute("SELECT legacy_key FROM entity_catalog WHERE kind='task' AND owner_user_id=? ORDER BY legacy_key", (row["user_id"],))]
            return [r[0] for r in conn.execute("SELECT task_id FROM auth_user_tasks WHERE user_id=? ORDER BY task_id", (principal.user_id,))]

    def _require_task(self, conn, principal, task_id, *, manage=False):
        user = self._current_user(conn, principal)
        if manage and user["role"] == "viewer":
            raise AuthorizationError("조회자는 이 작업을 수행할 수 없습니다.")
        if not conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
            raise NotFoundError("작업을 찾을 수 없습니다.")
        if user["role"] == "user":
            allowed = conn.execute("SELECT 1 FROM entity_catalog WHERE kind='task' AND legacy_key=? AND owner_user_id=?", (task_id, user["user_id"])).fetchone()
        else:
            allowed = user["role"] == "admin" or conn.execute("SELECT 1 FROM auth_user_tasks WHERE user_id=? AND task_id=?", (principal.user_id, task_id)).fetchone()
        if not allowed:
            raise NotFoundError("작업을 찾을 수 없습니다.")
        return task_id

    def require_task(self, principal, task_id, *, manage=False):
        with closing(self.db.get_connection()) as conn:
            return self._require_task(conn, principal, task_id, manage=manage)

    def require_run(self, principal, run_id, *, manage=False):
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT task_id FROM scheduled_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError("실행을 찾을 수 없습니다.")
            return self._require_task(conn, principal, row["task_id"], manage=manage)

    def require_handoff(self, principal, handoff_id, *, manage=False):
        with closing(self.db.get_connection()) as conn:
            row = conn.execute("SELECT task_id FROM delivery_handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            if row is None:
                raise NotFoundError("메일 결과를 찾을 수 없습니다.")
            return self._require_task(conn, principal, row["task_id"], manage=manage)

    def _require_resource(self, conn, principal, kind, key):
        user = self._current_user(conn, principal)
        if user["role"] == "viewer":
            raise AuthorizationError("조회자는 발신 계정과 수신자 그룹을 관리할 수 없습니다.")
        row = conn.execute("SELECT owner_user_id FROM entity_catalog WHERE kind=? AND legacy_key=?", (kind, key)).fetchone()
        if row is None or (user["role"] != "admin" and row[0] != user["user_id"]):
            raise NotFoundError("자료를 찾을 수 없습니다.")
        return key

    def require_sender(self, principal, sender_profile_id):
        with closing(self.db.get_connection()) as conn:
            return self._require_resource(conn, principal, "sender", sender_profile_id)

    def require_recipient_group(self, principal, group_id):
        with closing(self.db.get_connection()) as conn:
            return self._require_resource(conn, principal, "recipient_group", group_id)

    require_group = require_recipient_group

    def require_smtp_job(self, principal, job_id):
        with closing(self.db.get_connection()) as conn:
            user = self._current_user(conn, principal)
            row = conn.execute("""SELECT s.handoff_id,s.envelope_json,h.task_id FROM smtp_attempts s
                LEFT JOIN delivery_handoffs h ON h.handoff_id=s.handoff_id WHERE s.job_id=?""", (job_id,)).fetchone()
            if row is None or (row["task_id"] is None and user["role"] == "viewer"):
                raise NotFoundError("메일 결과를 찾을 수 없습니다.")
            if row["task_id"] is None and user["role"] == "user":
                self._require_resource(conn, principal, "sender", json.loads(row["envelope_json"]).get("sender_profile_id", "default"))
            return self._require_task(conn, principal, row["task_id"]) if row["task_id"] else None
