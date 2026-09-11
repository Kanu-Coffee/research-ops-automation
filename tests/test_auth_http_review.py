"""Independent regressions for account forms and hostile boundary inputs."""

from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from researchops.services.application import ApplicationService
from researchops.web.router import WebRouter
from tests.support import isolated_settings, register_fixture_task


class FormInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.options = []
        self.feed(html.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs.setdefault(attrs["name"], []).append(attrs)
        if tag == "option":
            self.options.append(attrs)


class AuthenticationReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = isolated_settings(Path(self.tmp.name))
        self.app = ApplicationService(self.settings)
        register_fixture_task(self.app)
        self.router = WebRouter(self.app)
        self.admin = self.app.auth.setup(self.app.auth.issue_setup_token(),
            "review-admin", "Synthetic review admin password")

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, path, *, method="GET", data=None, csrf=None):
        headers = {"Host": "localhost", "Origin": "http://localhost",
            "Cookie": "researchops_local_session=" + self.admin.token,
            "X-CSRF-Token": self.admin.session.csrf_token if csrf is None else csrf}
        return self.router.handle_request(method, path,
            urlencode(data or {}, doseq=True).encode(),
            "application/x-www-form-urlencoded", headers=headers)

    def create_viewer(self):
        return self.app.auth.create_user(self.admin.session.principal,
            "history-reader", "History Reader", "viewer",
            "Synthetic viewer temporary password", ["software-releases"])

    def test_deleted_task_grant_survives_account_editor_round_trip(self):
        user = self.create_viewer()
        with self.app.db.transaction() as conn:
            conn.execute("UPDATE entity_catalog SET deleted_at=? WHERE kind='task' AND legacy_key=?",
                ("2026-09-11T00:00:00Z", "software-releases"))
        code, _, body = self.request("/settings/users?user=" + user["user_id"])
        self.assertEqual(code, 200)
        form = FormInputs(body)
        selected = [item["value"] for item in form.inputs.get("task_ids", []) if "checked" in item]
        self.assertEqual(selected, ["software-releases"])
        self.assertIn("삭제됨 · 이력 조회".encode(), body)
        code, _, _ = self.request("/settings/users/" + user["user_id"], method="POST",
            data={"username": user["username"], "display_name": "Updated Reader",
                "role": "viewer", "active": "true", "task_ids": selected})
        self.assertEqual(code, 303)
        saved = next(item for item in self.app.auth.list_users(self.admin.session.principal)
            if item["user_id"] == user["user_id"])
        self.assertEqual(saved["task_ids"], ["software-releases"])

    def test_password_reset_error_keeps_original_account_fields(self):
        user = self.create_viewer()
        code, _, body = self.request("/settings/users/" + user["user_id"] + "/reset-password",
            method="POST", data={"temporary_password": "short-secret"})
        self.assertEqual(code, 400)
        form = FormInputs(body)
        self.assertEqual(form.inputs["username"][0]["value"], "history-reader")
        self.assertEqual(form.inputs["display_name"][0]["value"], "History Reader")
        self.assertIn("checked", form.inputs["active"][0])
        self.assertIn("checked", form.inputs["task_ids"][0])
        self.assertNotIn(b"short-secret", body)

    def test_preview_word_in_query_keeps_application_csrf_and_csp(self):
        code, headers, body = self.request("/settings/users?return=/preview/html")
        self.assertEqual(code, 200)
        self.assertIn(b'name="csrf_token"', body)
        self.assertIn("form-action 'self'", headers.get("Content-Security-Policy", ""))

    def test_non_ascii_csrf_is_rejected_without_server_error(self):
        # Latin-1 is representable in a raw HTTP header; a URL-encoded form also
        # accepts arbitrary Unicode without violating the transport grammar.
        self.assertEqual(self.request("/logout", method="POST", csrf="é")[0], 403)
        self.assertEqual(self.request("/logout", method="POST", csrf="",
            data={"csrf_token": "한글"})[0], 403)

    def test_non_ascii_numeric_log_offset_is_validation_error(self):
        run = self.app.runs.enqueue_run("software-releases")
        code, _, _ = self.request(f"/api/runs/{run.run_id}/logs?" +
            urlencode({"file": "research.stdout", "offset": "²"}))
        self.assertEqual(code, 400)

    def test_invalid_earlier_log_bytes_do_not_split_valid_unicode_at_window_end(self):
        run = self.app.runs.enqueue_run("software-releases")
        directory = self.settings.paths.run_archive_dir / "software-releases" / run.run_id / "logs"
        directory.mkdir(parents=True)
        raw = b"\xff" + b"x" * 65534 + "한글".encode() + b"tail"
        path = directory / "research.stdout"
        path.write_bytes(raw)
        offset, texts = 0, []
        while offset < len(raw):
            code, _, body = self.request(f"/api/runs/{run.run_id}/logs?file=research.stdout&offset={offset}")
            self.assertEqual(code, 200)
            result = json.loads(body)
            self.assertGreater(result["next_offset"], offset)
            texts.append(result["text"])
            offset = result["next_offset"]
        self.assertEqual("".join(texts), raw.decode("utf-8", errors="replace"))
        self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
