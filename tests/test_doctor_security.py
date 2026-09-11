"""Doctor must distinguish live ownership from unsafe legacy readiness."""
from pathlib import Path
import tempfile
import unittest
from researchops.services.application import ApplicationService
from tests.support import isolated_settings, fixture_runner, register_fixture_task


class TestDoctorSecurity(unittest.TestCase):
    def test_live_lock_is_healthy_but_credential_bridge_blocks_readiness(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = isolated_settings(Path(temp))
            app = ApplicationService(settings, custom_runner=fixture_runner(settings))
            register_fixture_task(app)
            run = app.runs.enqueue_run("software-releases")
            _, lease = app.run_repo.claim_next_run("doctor-test", run_id=run.run_id)
            app.workspace_mgr.acquire_workspace_lock("software-releases", run.run_id, lease.fencing_token)
            health = app.doctor.check_all()
            self.assertTrue(health["runtime_security"]["ok"], health)
            self.assertTrue(health["internal_fake_ready"])
            project = app.workspace_mgr.get_task_workspace_dir("software-releases") / "project"
            (project / ".codex").mkdir()
            health = app.doctor.check_all()
            self.assertFalse(health["runtime_security"]["ok"])
            self.assertFalse(health["internal_fake_ready"])
            self.assertFalse(health["live_runner_ready"])
