"""CLI/Web queries expose nested evidence safely and bound log output."""

import os
from pathlib import Path
import tempfile
import unittest

from researchops.errors import NotFoundError, ResearchOpsError
from researchops.services.application import ApplicationService
from tests.package_support import register_template
from tests.support import isolated_settings, fixture_runner, register_fixture_task


class TestArchiveQueries(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings=isolated_settings(Path(self.temporary.name))
        self.app=ApplicationService(self.settings,custom_runner=fixture_runner(self.settings))
        register_fixture_task(self.app)
        self.run=self.app.runs.enqueue_run("software-releases",force_dry_run=True)
        finished=self.app.runs.execute_run(self.run.run_id)
        self.assertEqual(finished.status,"succeeded",finished.error_message)
        self.archive=self.settings.paths.run_archive_dir/self.run.task_id/self.run.run_id

    def test_phase_logs_and_nested_artifacts_are_queryable(self):
        logs=self.app.runs.get_run_logs(self.run.run_id)
        self.assertIn("[logs/research.stdout]",logs)
        self.assertIn("Fake research finished.",logs)
        self.assertIn("[logs/compose.stderr]",logs)
        names={item["filename"] for item in self.app.runs.get_run_artifacts(self.run.run_id)}
        self.assertIn("run-manifest.json",names)
        self.assertIn("logs/research.stdout",names)
        self.assertIn("task-snapshot/task.md",names)
        path=self.app.runs.get_run_archive_file(self.run.run_id,"logs/research.stdout")
        self.assertEqual(path,self.archive/"logs/research.stdout")
        self.assertIn(b"Fake research",self.app.runs.read_run_archive_file(self.run.run_id,"logs/research.stdout"))

    def test_legacy_top_level_log_stays_compatible(self):
        run=self.app.runs.enqueue_run("software-releases")
        archive=self.settings.paths.run_archive_dir/run.task_id/run.run_id
        archive.mkdir()
        (archive/"run.log").write_text("Legacy runner output\n")
        self.assertEqual(self.app.runs.get_run_logs(run.run_id),"Legacy runner output\n")
        self.assertEqual(self.app.runs.get_run_artifacts(run.run_id)[0]["filename"],"run.log")

    def test_logs_are_bounded_and_oversize_is_explicit(self):
        (self.archive/"logs/research.stdout").write_bytes(b"x"*2_000_001)
        result=self.app.runs.get_run_logs(self.run.run_id)
        self.assertIn("Log omitted",result)
        self.assertIn("Fake compose finished.",result)
        self.assertLessEqual(len(result.encode("utf-8")),2_000_000)
        with self.assertRaises(ResearchOpsError):
            self.app.runs.read_run_archive_file(self.run.run_id,"logs/research.stdout",max_bytes=100)

    def test_traversal_symlinks_and_hardlinks_are_rejected(self):
        for name in ("../email.html","/etc/passwd","logs/../../email.html","logs\\research.stdout","./email.html","logs//research.stdout"):
            with self.subTest(name=name),self.assertRaises(ResearchOpsError):
                self.app.runs.get_run_archive_file(self.run.run_id,name)
        link=self.archive/"linked.txt"
        link.symlink_to(self.archive/"email.txt")
        with self.assertRaises(ResearchOpsError):
            self.app.runs.read_run_archive_file(self.run.run_id,"linked.txt")
        with self.assertRaises(ResearchOpsError):
            self.app.runs.get_run_artifacts(self.run.run_id)
        link.unlink()
        os.link(self.archive/"email.txt",link)
        with self.assertRaises(ResearchOpsError):
            self.app.runs.get_run_archive_file(self.run.run_id,"linked.txt")

    def test_version_queries_use_application_service_contract(self):
        version = register_template(self.app, "query-task")
        details = self.app.tasks.show_task("query-task")
        self.assertEqual(details["status"]["task_id"], "query-task")
        self.assertEqual([item["version_hash"] for item in self.app.tasks.list_versions("query-task")], [version.version_hash])
        self.assertNotIn("drafts", details)
        self.assertNotIn("drafts_count", details)
        with self.assertRaises(NotFoundError):
            self.app.tasks.show_task("missing-task")
