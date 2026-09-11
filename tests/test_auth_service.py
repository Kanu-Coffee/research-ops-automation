"""Web auth lifecycle and authorization use synthetic users in isolated SQLite."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from researchops.cli.main import build_parser, main
from researchops.domain.events import AuditEvent
from researchops.errors import NotFoundError, ValidationError
from researchops.services.audit_context import audit_actor, current_audit_actor
from researchops.services.auth_service import (
    AuthenticationError, AuthRateLimitError, AuthRequiredError, AuthService,
    AuthorizationError, BOOTSTRAP_SECONDS, LOGIN_ACCOUNT_LIMIT,
    LOGIN_WINDOW_SECONDS, PREAUTH_SECONDS, SESSION_ABSOLUTE_SECONDS,
    SESSION_IDLE_SECONDS, hash_password, verify_password,
)
from researchops.services.application import ApplicationService
from tests.support import isolated_settings, register_fixture_task


ADMIN_PASSWORD = "synthetic-admin-password"
VIEWER_PASSWORD = "synthetic-viewer-temporary"
NEW_PASSWORD = "synthetic-new-viewer-password"


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = isolated_settings(self.root)
        self.app = ApplicationService(self.settings)
        self.now = 1_800_000_000.0
        self.auth = AuthService(self.settings, self.app.db, clock=lambda: self.now)

    def admin(self):
        token = self.auth.issue_setup_token()
        return self.auth.setup(token, "admin", ADMIN_PASSWORD, "운영자")

    def viewer(self, admin, *, task_ids=(), username="viewer", role="viewer"):
        user = self.auth.create_user(admin.session.principal, username, "조회 담당", role, VIEWER_PASSWORD, task_ids)
        grant = self.auth.login(username, VIEWER_PASSWORD)
        self.assertTrue(grant.session.principal.must_change_password)
        return user, self.auth.change_password(grant.session.principal, VIEWER_PASSWORD, NEW_PASSWORD)

    def test_bootstrap_single_use_hashed_token_and_no_credentials_in_audit(self):
        self.assertFalse(self.auth.initialized())
        token = self.auth.issue_setup_token()
        with closing(self.app.db.get_connection()) as conn:
            row = conn.execute("SELECT * FROM auth_bootstrap").fetchone()
            self.assertNotEqual(row["token_hash"], token)
            self.assertEqual(row["expires_at"], self.now + BOOTSTRAP_SECONDS)
        grant = self.auth.setup(token, "  ADMIN  ", ADMIN_PASSWORD)
        self.assertEqual(grant.session.principal.username, "admin")
        self.assertEqual(grant.session.principal.role, "admin")
        self.assertFalse(grant.session.principal.must_change_password)
        self.assertTrue(self.auth.initialized())
        with self.assertRaises(ValidationError):
            self.auth.issue_setup_token()
        with self.assertRaises(AuthenticationError):
            self.auth.setup(token, "other", ADMIN_PASSWORD)
        with closing(self.app.db.get_connection()) as conn:
            user = conn.execute("SELECT * FROM auth_users").fetchone()
            session = conn.execute("SELECT * FROM auth_sessions").fetchone()
            audit = repr([dict(row) for row in conn.execute("SELECT * FROM audit_events")])
            self.assertEqual(session["token_hash"], hashlib.sha256(grant.token.encode()).hexdigest())
            self.assertNotEqual(user["password_hash"], ADMIN_PASSWORD)
            self.assertNotIn(grant.token, audit)
            self.assertNotIn(token, audit)
            self.assertNotIn(ADMIN_PASSWORD, audit)

    def test_bootstrap_expiry_rotation_and_concurrent_initialization(self):
        expired = self.auth.issue_setup_token()
        self.now += BOOTSTRAP_SECONDS
        with self.assertRaises(AuthenticationError):
            self.auth.setup(expired, "admin", ADMIN_PASSWORD)
        token = self.auth.issue_setup_token()
        with self.assertRaises(AuthenticationError):
            self.auth.setup(expired, "admin", ADMIN_PASSWORD)
        barrier = threading.Barrier(2)
        def setup(name):
            barrier.wait()
            try:
                self.auth.setup(token, name, ADMIN_PASSWORD)
                return True
            except (AuthenticationError, ValidationError):
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(setup, ("first", "second")))
        self.assertEqual(sum(outcomes), 1)
        with closing(self.app.db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0], 1)

    def test_password_hash_parameters_salts_unicode_and_limits(self):
        first, second = hash_password(ADMIN_PASSWORD), hash_password(ADMIN_PASSWORD)
        self.assertTrue(first.startswith("scrypt$131072$8$1$"))
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password(first, ADMIN_PASSWORD))
        self.assertFalse(verify_password(first, "incorrect-password"))
        self.assertFalse(verify_password("malformed", ADMIN_PASSWORD))
        self.assertFalse(verify_password(None, ADMIN_PASSWORD))
        for password in ("a" * 14, "b" * 129, "valid-password\x00hidden"):
            with self.subTest(length=len(password)), self.assertRaises(ValidationError):
                hash_password(password)
        unicode_password = "합성시험 비밀번호와 공백을 유지합니다"
        self.assertTrue(verify_password(hash_password(unicode_password), unicode_password))

    def test_preauth_expiry_and_csrf_are_separate_from_logged_in_sessions(self):
        preauth = self.auth.new_preauth_session()
        self.assertIsNone(preauth.session.principal)
        self.assertEqual(preauth.session.kind, "preauth")
        other = self.auth.new_preauth_session()
        self.assertNotEqual(preauth.session.csrf_token, other.session.csrf_token)
        self.now += PREAUTH_SECONDS
        self.assertIsNone(self.auth.resolve_session(preauth.token))
        self.assertIsNone(self.auth.resolve_session("malformed"))
        self.assertIsNone(self.auth.resolve_session(None))
        self.auth.new_preauth_session()
        with closing(self.app.db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)

    def test_preauth_storage_has_bound(self):
        preauth = self.auth.new_preauth_session()
        with self.app.db.transaction() as conn:
            conn.executemany("""INSERT INTO auth_sessions(token_hash,kind,csrf_token,created_at,last_seen_at,expires_at)
                VALUES(?,'preauth','csrf',?,?,?)""", ((str(n), self.now, self.now, self.now+PREAUTH_SECONDS) for n in range(999)))
        with self.assertRaises(AuthRateLimitError):
            self.auth.new_preauth_session()
        self.assertIsNotNone(self.auth.resolve_session(preauth.token))

    def test_polling_does_not_extend_idle_and_absolute_expiry_is_not_sliding(self):
        grant = self.admin()
        self.now += SESSION_IDLE_SECONDS - 1
        self.assertIsNotNone(self.auth.resolve_session(grant.token, touch=False))
        self.now += 1
        self.assertIsNone(self.auth.resolve_session(grant.token, touch=False))
        grant = self.auth.login("admin", ADMIN_PASSWORD)
        started = self.now
        for _ in range(11):
            self.now += 3600
            self.assertIsNotNone(self.auth.resolve_session(grant.token, touch=True))
        self.now = started + SESSION_ABSOLUTE_SECONDS
        self.assertIsNone(self.auth.resolve_session(grant.token))

    def test_session_logout_and_password_change_revoke_every_old_session(self):
        first = self.admin()
        second = self.auth.login("admin", ADMIN_PASSWORD)
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.session.csrf_token, second.session.csrf_token)
        self.auth.logout(first.token)
        self.assertIsNone(self.auth.resolve_session(first.token))
        self.assertIsNotNone(self.auth.resolve_session(second.token))
        changed = self.auth.change_password(second.session.principal, ADMIN_PASSWORD, NEW_PASSWORD)
        self.assertIsNone(self.auth.resolve_session(second.token))
        self.assertIsNotNone(self.auth.resolve_session(changed.token))
        self.assertFalse(changed.session.principal.must_change_password)
        with self.assertRaises(AuthenticationError):
            self.auth.login("admin", ADMIN_PASSWORD)

    def test_temporary_password_blocks_business_access_until_changed(self):
        admin = self.admin()
        self.auth.create_user(admin.session.principal, "viewer", "조회자", "viewer", VIEWER_PASSWORD)
        grant = self.auth.login("viewer", VIEWER_PASSWORD)
        self.assertTrue(grant.session.principal.must_change_password)
        with self.assertRaises(AuthorizationError):
            self.auth.allowed_task_ids(grant.session.principal)
        with self.assertRaises(AuthenticationError):
            self.auth.change_password(grant.session.principal, ADMIN_PASSWORD, NEW_PASSWORD)
        with self.assertRaises(ValidationError):
            self.auth.change_password(grant.session.principal, VIEWER_PASSWORD, VIEWER_PASSWORD)
        changed = self.auth.change_password(grant.session.principal, VIEWER_PASSWORD, NEW_PASSWORD)
        self.assertEqual(self.auth.allowed_task_ids(changed.session.principal), [])

    def test_viewer_grants_default_empty_and_revoke_without_waiting_for_session_expiry(self):
        register_fixture_task(self.app)
        admin = self.admin()
        user, viewer = self.viewer(admin)
        self.assertIsNone(self.auth.allowed_task_ids(admin.session.principal))
        self.assertEqual(self.auth.allowed_task_ids(viewer.session.principal), [])
        with self.assertRaises(NotFoundError):
            self.auth.require_task(viewer.session.principal, "software-releases")
        self.auth.update_user(admin.session.principal, user["user_id"], task_ids=["software-releases"])
        self.assertEqual(self.auth.require_task(viewer.session.principal, "software-releases"), "software-releases")
        run = self.app.runs.enqueue_run("software-releases")
        self.assertEqual(self.auth.require_run(viewer.session.principal, run.run_id), "software-releases")
        self.auth.update_user(admin.session.principal, user["user_id"], task_ids=[])
        self.assertIsNotNone(self.auth.resolve_session(viewer.token))
        with self.assertRaises(NotFoundError):
            self.auth.require_run(viewer.session.principal, run.run_id)
        with self.assertRaises(AuthorizationError):
            self.auth.list_users(viewer.session.principal)
        with self.assertRaises(ValidationError):
            self.auth.update_user(admin.session.principal, user["user_id"], task_ids=["unknown-task"])

    def test_disabled_role_changed_and_reset_accounts_revoke_existing_cookies(self):
        admin = self.admin()
        user, viewer = self.viewer(admin)
        self.auth.update_user(admin.session.principal, user["user_id"], active=False)
        self.assertIsNone(self.auth.resolve_session(viewer.token))
        with self.assertRaises(AuthRequiredError):
            self.auth.allowed_task_ids(viewer.session.principal)
        with self.assertRaises(AuthenticationError):
            self.auth.login("viewer", NEW_PASSWORD)
        self.auth.update_user(admin.session.principal, user["user_id"], active=True)
        viewer = self.auth.login("viewer", NEW_PASSWORD)
        self.auth.update_user(admin.session.principal, user["user_id"], role="admin")
        self.assertIsNone(self.auth.resolve_session(viewer.token))
        viewer = self.auth.login("viewer", NEW_PASSWORD)
        self.auth.reset_password(admin.session.principal, user["user_id"], VIEWER_PASSWORD)
        self.assertIsNone(self.auth.resolve_session(viewer.token))
        self.assertTrue(self.auth.login("viewer", VIEWER_PASSWORD).session.principal.must_change_password)

    def test_handoff_and_smtp_job_access_resolves_task_before_diagnostics(self):
        from researchops.delivery.queue import SmtpQueue
        from researchops.domain.models import DeliveryHandoff
        version = register_fixture_task(self.app)
        run = self.app.runs.enqueue_run(version.task_id)
        self.app.delivery_repo.save_handoff(DeliveryHandoff(
            handoff_id="handoff-fixture", idempotency_key="fixture",
            run_id=run.run_id, task_id=version.task_id, task_version_hash=version.version_hash,
            message_revision=1, message_type="report", recipient_group_id="synthetic-group",
            mode="dry_run", status="prepared", delivery_request={}, delivery_request_sha256="a" * 64))
        queue = SmtpQueue(self.app.db)
        queue.enqueue("job-fixture", "handoff-fixture", "fixture-message", b"synthetic mime", {}, "synthetic-revision")
        queue.enqueue("job-connection-test", None, "connection-test-message", b"synthetic mime", {}, "synthetic-revision")
        admin = self.admin()
        user, viewer = self.viewer(admin, task_ids=[version.task_id])
        self.assertEqual(self.auth.require_handoff(viewer.session.principal, "handoff-fixture"), version.task_id)
        self.assertEqual(self.auth.require_smtp_job(viewer.session.principal, "job-fixture"), version.task_id)
        self.assertIsNone(self.auth.require_smtp_job(admin.session.principal, "job-connection-test"))
        with self.assertRaises(NotFoundError):
            self.auth.require_smtp_job(viewer.session.principal, "job-connection-test")
        self.auth.update_user(admin.session.principal, user["user_id"], task_ids=[])
        for method, object_id in ((self.auth.require_handoff, "handoff-fixture"),
                                  (self.auth.require_smtp_job, "job-fixture"),
                                  (self.auth.require_smtp_job, "missing-job")):
            with self.subTest(object_id=object_id), self.assertRaises(NotFoundError):
                method(viewer.session.principal, object_id)

    def test_password_derivation_concurrency_is_bounded(self):
        from researchops.services.auth_service import _PASSWORD_SLOTS
        _PASSWORD_SLOTS.acquire()
        _PASSWORD_SLOTS.acquire()
        try:
            with self.assertRaises(AuthRateLimitError):
                hash_password(ADMIN_PASSWORD)
        finally:
            _PASSWORD_SLOTS.release()
            _PASSWORD_SLOTS.release()

    def test_last_admin_protection_is_transactional_under_concurrent_updates(self):
        first = self.admin()
        second_user, second = self.viewer(first, role="admin", username="second")
        with self.assertRaises(ValidationError):
            self.auth.update_user(first.session.principal, first.session.principal.user_id, active="false")
        barrier = threading.Barrier(2)
        def disable(grant):
            barrier.wait()
            try:
                self.auth.update_user(grant.session.principal, grant.session.principal.user_id, active=False)
                return True
            except (ValidationError, AuthRequiredError):
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(disable, (first, second)))
        self.assertEqual(sum(results), 1)
        with closing(self.app.db.get_connection()) as conn:
            active = conn.execute("SELECT * FROM auth_users WHERE active=1 AND role='admin'").fetchall()
        self.assertEqual(len(active), 1)
        grant = self.auth.login(active[0]["username"], ADMIN_PASSWORD if active[0]["username"] == "admin" else NEW_PASSWORD)
        with self.assertRaises(ValidationError):
            self.auth.update_user(grant.session.principal, grant.session.principal.user_id, role="viewer")

    def test_throttling_persists_across_service_restart_and_uses_account_not_proxy_ip(self):
        self.admin()
        with patch("researchops.services.auth_service.verify_password", return_value=False):
            for _ in range(LOGIN_ACCOUNT_LIMIT):
                with self.assertRaises(AuthenticationError):
                    self.auth.login("admin", "incorrect-password")
            restarted = AuthService(self.settings, self.app.db, clock=lambda: self.now)
            with self.assertRaises(AuthRateLimitError):
                restarted.login("ADMIN", "incorrect-password")
            with self.assertRaises(AuthenticationError):
                restarted.login("other", "incorrect-password")
        self.now += LOGIN_WINDOW_SECONDS
        self.assertIsNotNone(self.auth.login("admin", ADMIN_PASSWORD))

    def test_cli_reset_does_not_reactivate_user_and_does_not_return_password(self):
        admin = self.admin()
        user, viewer = self.viewer(admin)
        self.auth.update_user(admin.session.principal, user["user_id"], active=False)
        self.auth.reset_password_from_cli("viewer", VIEWER_PASSWORD)
        self.assertIsNone(self.auth.resolve_session(viewer.token))
        with self.assertRaises(AuthenticationError):
            self.auth.login("viewer", VIEWER_PASSWORD)
        self.auth.update_user(admin.session.principal, user["user_id"], active=True)
        self.assertFalse(self.auth.login("viewer", VIEWER_PASSWORD).session.principal.must_change_password)
        config = str(self.root / "settings.yaml")
        with patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]), redirect_stdout(io.StringIO()) as output:
            code = main(["--config", config, "auth", "reset-password", "viewer", "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn(NEW_PASSWORD, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["status"], "password_reset")
        self.assertEqual(build_parser().parse_args(["auth", "setup-token"]).auth_command, "setup-token")

    def test_owner_schema_is_v3_and_raw_sqlite_clients_do_not_need_audit_function(self):
        self.admin()
        self.app.db.init_schema()
        with sqlite3.connect(self.settings.paths.database) as conn:
            self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 3)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='request_audit_actor'").fetchone())
            conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                VALUES('test','old-worker','compatibility','{}','2026-09-11T00:00:00+00:00')""")
            self.assertEqual(conn.execute("SELECT actor FROM audit_events WHERE entity_id='old-worker'").fetchone()[0], "single-operator")

    def test_request_audit_actor_covers_raw_sql_preserves_explicit_actor_and_nested_context(self):
        self.assertEqual(current_audit_actor(), "")
        with audit_actor("user:outer"):
            self.app.state_repo.save_audit_event(AuditEvent("test", "outer", "changed", {}))
            with audit_actor("user:inner"):
                with self.app.db.transaction() as conn:
                    conn.execute("""INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at)
                        VALUES('test','inner','changed','{}','2026-09-11T00:00:00+00:00')""")
            self.app.state_repo.save_audit_event(AuditEvent("test", "worker", "changed", {}, actor="smtp-worker"))
            self.assertEqual(current_audit_actor(), "user:outer")
        self.assertEqual(current_audit_actor(), "")
        with closing(self.app.db.get_connection()) as conn:
            actors = {row["entity_id"]: row["actor"] for row in conn.execute("SELECT * FROM audit_events WHERE entity_type='test'")}
        self.assertEqual(actors, {"outer": "user:outer", "inner": "user:inner", "worker": "smtp-worker"})

    def test_concurrent_request_audit_identity_does_not_cross_threads(self):
        barrier = threading.Barrier(2)
        def write(name):
            with audit_actor("user:" + name):
                barrier.wait()
                self.app.state_repo.save_audit_event(AuditEvent("test", name, "changed", {}))
            return current_audit_actor()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(write, ("one", "two"))), ["", ""])
        with closing(self.app.db.get_connection()) as conn:
            actors = {row["entity_id"]: row["actor"] for row in conn.execute("SELECT * FROM audit_events WHERE entity_type='test'")}
        self.assertEqual(actors, {"one": "user:one", "two": "user:two"})


if __name__ == "__main__":
    unittest.main()
