"""Command replays keep the same source version and cannot enqueue duplicate mail."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from researchops.errors import ResearchOpsError, ValidationError
from researchops.services.application import ApplicationService
from tests.web_support import AuthenticatedWebRouter as WebRouter, admin_session, authenticated_headers
from tests.package_support import register_template
from tests.support import isolated_settings, fixture_runner, register_fixture_task


class TestCommandIdempotency(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings=isolated_settings(Path(self.temporary.name))
        self.app=ApplicationService(self.settings,custom_runner=fixture_runner(self.settings))
        self.version=register_fixture_task(self.app)

    def source(self):
        run=self.app.runs.enqueue_run("software-releases",force_dry_run=True)
        finished=self.app.runs.execute_run(run.run_id)
        self.assertEqual(finished.status,"succeeded",finished.error_message)
        return finished

    def test_simultaneous_retry_replays_return_one_child(self):
        source=self.source()
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs=list(pool.map(lambda _:self.app.runs.retry_run(source.run_id,request_key="retry-click"),range(2)))
        self.assertEqual(runs[0].run_id,runs[1].run_id)
        self.assertEqual(len(self.app.run_repo.list_runs()),2)
        self.assertEqual(runs[0].task_version_hash,source.task_version_hash)
        self.app.runs.execute_run(runs[0].run_id)
        replay=self.app.runs.retry_run(source.run_id,request_key="retry-click")
        self.assertEqual(replay.run_id,runs[0].run_id)
        self.assertEqual(replay.status,"succeeded")

    def test_compose_replays_keep_one_revision_and_reject_cross_action_key(self):
        source=self.source()
        first=self.app.runs.compose_only(source.run_id,request_key="compose-click")
        second=self.app.runs.compose_only(source.run_id,request_key="compose-click")
        self.assertEqual(first.run_id,second.run_id)
        self.assertEqual(len(self.app.run_repo.list_runs()),2)
        self.assertEqual(self.app.run_repo.get_execution_controls(first.run_id)["composition_revision"],2)
        with self.assertRaises(ValidationError):
            self.app.runs.retry_run(source.run_id,request_key="compose-click")

    def test_candidate_hash_is_task_bound_and_replay_payload_is_immutable(self):
        version=register_template(self.app, "other-market")
        with self.assertRaises(ResearchOpsError):
            self.app.runs.enqueue_run("software-releases",candidate_version_hash=version.version_hash)
        first=self.app.runs.enqueue_run("software-releases",candidate_version_hash=self.version.version_hash,request_key="candidate-click")
        task_md=self.settings.paths.tasks_dir/"software-releases"/"task.md"
        task_md.write_text(task_md.read_text()+"\nNew instructions\n")
        updated=self.app.tasks.sync_canonical_tasks()[0]
        self.app.task_repo.set_active_version("software-releases",updated.version_hash)
        replay=self.app.runs.enqueue_run("software-releases",candidate_version_hash=self.version.version_hash,request_key="candidate-click")
        self.assertEqual(first.run_id,replay.run_id)
        with self.assertRaises(ValidationError):
            self.app.runs.enqueue_run("software-releases",candidate_version_hash=updated.version_hash,request_key="candidate-click")

    def test_web_retry_and_compose_form_resubmission_share_request_key(self):
        source=self.source()
        router=WebRouter(self.app)
        headers={"Host":"localhost","Origin":"http://localhost","X-CSRF-Token":router.csrf_token}
        for action in ("retry","compose-only"):
            body=urlencode({"request_key":action+"-form"}).encode()
            first=router.handle_request("POST",f"/runs/{source.run_id}/{action}",body,
                "application/x-www-form-urlencoded",headers=headers)
            second=router.handle_request("POST",f"/runs/{source.run_id}/{action}",body,
                "application/x-www-form-urlencoded",headers=headers)
            self.assertEqual(first[0],303)
            self.assertNotIn("error=",first[1]["Location"])
            self.assertEqual(first[1]["Location"],second[1]["Location"])
