"""CLI integration tests using bin/researchctl."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.support import SOURCE_ROOT, isolated_settings, register_fixture_task, fixture_runner
from researchops.services.application import ApplicationService


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_root = Path(self.tmp.name)
        settings = isolated_settings(self.repo_root)
        app = ApplicationService(settings, custom_runner=fixture_runner(settings))
        register_fixture_task(app)
        app.workspace_mgr.init_task_workspace("software-releases")
        self.cli_bin = SOURCE_ROOT / "bin/researchctl"

    def _run_cli(self, *args):
        cmd = [str(self.cli_bin)] + list(args)
        proc = subprocess.run(
            cmd,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(self.repo_root / "home"),
                 "RESEARCHOPS_ROOT": str(self.repo_root),
                 "RESEARCHOPS_CONFIG": str(self.repo_root / "settings.yaml")},
            timeout=20,
        )
        return proc

    def test_version_command(self):
        proc = self._run_cli("version", "--json")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertIn("version", data)

    def test_nested_smtp_worker_is_not_rewritten_as_research_worker(self):
        proc = self._run_cli("delivery", "worker", "--once", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_global_config_prefix_is_preserved(self):
        proc = self._run_cli("--config", str(self.repo_root / "settings.yaml"), "task", "list", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)[0]["task_id"], "software-releases")

    def test_doctor_command(self):
        proc = self._run_cli("doctor", "--json")
        self.assertEqual(proc.returncode, 1, f"Error: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("overall_status"), "degraded")
        self.assertTrue(data["internal_fake_ready"])
        self.assertFalse(data["live_runner_ready"])
        self.assertTrue(data.get("database", {}).get("ok"))

    def test_task_list_and_show(self):
        proc = self._run_cli("task", "list", "--json")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)

        # Show software-releases
        proc2 = self._run_cli("task", "show", "software-releases", "--json")
        self.assertEqual(proc2.returncode, 0, f"Error: {proc2.stderr}")
        task_data = json.loads(proc2.stdout)
        self.assertEqual(task_data["status"]["task_id"], "software-releases")

    def test_task_template_list(self):
        proc = self._run_cli("task", "template", "list", "--json")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        templates = json.loads(proc.stdout)
        self.assertIsInstance(templates, list)
        self.assertTrue(len(templates) > 0)
        t_ids = [t["template_id"] for t in templates]
        self.assertIn("software-releases", t_ids)

    def test_task_version_list(self):
        proc = self._run_cli("task", "version", "list", "software-releases", "--json")
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        versions = json.loads(proc.stdout)
        self.assertIsInstance(versions, list)
        self.assertTrue(len(versions) > 0)
        self.assertIn("version_hash", versions[0])

    def test_workspace_snapshot_and_inspect(self):
        # Inspect workspace
        proc_insp = self._run_cli("workspace", "inspect", "software-releases", "--json")
        self.assertEqual(proc_insp.returncode, 0, f"Error: {proc_insp.stderr}")
        insp_data = json.loads(proc_insp.stdout)
        self.assertEqual(insp_data["task_id"], "software-releases")

        # Snapshot workspace
        proc_snap = self._run_cli("workspace", "snapshot", "software-releases", "--json")
        self.assertEqual(proc_snap.returncode, 0, f"Error: {proc_snap.stderr}")
        snap_data = json.loads(proc_snap.stdout)
        self.assertEqual(snap_data["task_id"], "software-releases")
        snapshot_path = Path(snap_data["snapshot_path"])
        self.assertTrue(snapshot_path.exists())

        # Cleanup snapshot file after test
        snapshot_path.unlink(missing_ok=True)

    def test_retired_working_copy_command_rejects_without_creating_task(self):
        proc = self._run_cli("task", "draft", "create", "--template", "software-releases", "--task-id", "test-opt-draft", "--json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)
        self.assertEqual(proc.stdout, "")
        listed = self._run_cli("task", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertFalse(any(task["task_id"] == "test-opt-draft" for task in json.loads(listed.stdout)))


if __name__ == "__main__":
    unittest.main()
