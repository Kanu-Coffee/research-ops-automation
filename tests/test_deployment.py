"""Installation tooling cannot read existing runtime or silently enable services."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from researchops import deployment
from researchops.config import load_settings
from researchops.delivery.smtp_config import load_delivery_config


SOURCE = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="researchops-deploy-unit-")
        self.parent = Path(self.temp.name)
        self.prefix = self.parent / "new-install"

    def tearDown(self):
        self.temp.cleanup()

    def test_default_preflight_never_reads_target_contents_or_starts_process(self):
        secret = self.parent / "delivery_config.yaml"
        secret.write_text("DO_NOT_READ")
        with patch.object(deployment, "SYSTEM_TARGETS", (secret,)), \
                patch.object(Path, "read_text", side_effect=AssertionError("opened")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("opened")), \
                patch.object(subprocess, "run", side_effect=AssertionError("process")):
            report = deployment.host_preflight()
        self.assertIn("EXISTING_DEPLOYMENT_REQUIRES_REVIEW_NO_OVERWRITE", report["blockers"])
        self.assertFalse(report["system_install_performed"])
        self.assertFalse(report["services_started"])

    def test_plan_is_default_and_does_not_create_anything(self):
        report = deployment.stage_installation(SOURCE, self.prefix)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(report["applied"])
        self.assertFalse(report["production_runner_ready"])

    def test_existing_destination_is_preserved(self):
        self.prefix.mkdir()
        sentinel = self.prefix / "keep"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(deployment.DeploymentError, "already exists"):
            deployment.stage_installation(SOURCE, self.prefix, apply=True)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_symlink_destination_is_rejected(self):
        self.prefix.symlink_to(self.parent / "absent", target_is_directory=True)
        with self.assertRaisesRegex(deployment.DeploymentError, "Symlink"):
            deployment.stage_plan(SOURCE, self.prefix)

    def test_symlink_ancestor_is_rejected(self):
        link = self.parent / "link"
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(deployment.DeploymentError, "Symlink"):
            deployment.stage_plan(SOURCE, link / "new")

    def test_public_parent_is_rejected(self):
        self.parent.chmod(0o755)
        with self.assertRaisesRegex(deployment.DeploymentError, "private"):
            deployment.stage_plan(SOURCE, self.prefix)

    def test_parent_other_owner_is_rejected(self):
        with patch.object(deployment.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(deployment.DeploymentError, "owned"):
                deployment.stage_plan(SOURCE, self.prefix)

    def test_source_overlap_is_rejected(self):
        with self.assertRaisesRegex(deployment.DeploymentError, "outside"):
            deployment.stage_plan(SOURCE, SOURCE / "installation")

    def test_relative_traversal_and_unit_injection_paths_are_rejected(self):
        for invalid in (Path("relative"), self.parent / ".." / "target",
                        self.parent / "space target", self.parent / "a%t", Path("/")):
            with self.subTest(invalid=invalid), self.assertRaises(deployment.DeploymentError):
                deployment.stage_plan(SOURCE, invalid)

    def test_source_directory_symlink_rejected_before_destination_creation(self):
        source = self.parent / "source"
        source.mkdir()
        (source / "researchops").symlink_to(SOURCE / "researchops", target_is_directory=True)
        with self.assertRaises(deployment.DeploymentError):
            deployment.stage_installation(source, self.prefix, apply=True)
        self.assertFalse(self.prefix.exists())

    def test_missing_uv_does_not_create_destination(self):
        with patch.object(deployment.shutil, "which", return_value=None):
            with self.assertRaisesRegex(deployment.DeploymentError, "Existing uv"):
                deployment.stage_installation(SOURCE, self.prefix, apply=True)
        self.assertFalse(self.prefix.exists())

    def test_stage_excludes_runtime_uses_locked_offline_install_and_disabled_defaults(self):
        calls = []

        def run(command, *, cwd, env):
            calls.append((command, cwd, env))
            return subprocess.CompletedProcess(command, 0, stdout="usage: researchctl\n", stderr="")

        with patch.object(deployment, "_run", side_effect=run), \
                patch.object(deployment.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                patch.dict(os.environ, {"SMTP_PASSWORD": "not-forwarded", "RESEARCHOPS_CONFIG": "/secret",
                                        "UV_PROJECT_ENVIRONMENT": "/existing", "PYTHONPATH": "/injected"}):
            report = deployment.stage_installation(SOURCE, self.prefix, apply=True)
        self.assertTrue(report["applied"])
        self.assertEqual(report["systemd_unit_verify"], "passed")
        self.assertFalse(report["system_install_performed"])
        self.assertEqual(len(calls), 3)
        command = calls[0][0]
        for argument in ("--offline", "--frozen", "--no-editable", "--no-dev"):
            self.assertIn(argument, command)
        self.assertEqual(command[command.index("--link-mode") + 1], "copy")
        for _, _, env in calls:
            self.assertNotIn("SMTP_PASSWORD", env)
            self.assertNotIn("UV_PROJECT_ENVIRONMENT", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env["TZ"], "Asia/Seoul")
            self.assertEqual(env["RESEARCHOPS_CONFIG"], report["config"])
        app, runtime = Path(report["application"]), Path(report["runtime"])
        self.assertFalse((app / "ops_test_info").exists())
        self.assertFalse((app / "var").exists())
        self.assertFalse((runtime / "researchops.db").exists())
        self.assertEqual(list((self.prefix / "etc/researchops/tasks").iterdir()), [])
        settings = load_settings(report["config"])
        self.assertTrue(settings.delivery.global_handoff_kill_switch)
        self.assertEqual(settings.delivery.default_mode, "dry_run")
        self.assertEqual(settings.runner.default_type, "fake")
        self.assertFalse(settings.web.enabled)
        self.assertFalse(settings.raw_config["scheduler"]["enabled"])
        with patch.dict(os.environ, {}, clear=True):
            delivery = load_delivery_config(runtime / "delivery_config.yaml")
        self.assertFalse(delivery.enabled)
        self.assertFalse(delivery.auto_dispatch)
        self.assertEqual(delivery.recipient_groups, {})
        for secret in (Path(report["config"]), runtime / "delivery_config.yaml"):
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        for name in deployment.UNIT_NAMES:
            unit = (self.prefix / "units" / name).read_text()
            self.assertNotIn("StateDirectory=", unit)
            self.assertNotIn("RuntimeDirectory=", unit)
            if name.endswith(".service"):
                self.assertIn("ExecStart=" + report["executable"], unit)
                self.assertIn("ReadWritePaths=" + report["runtime"], unit)

    def test_failed_install_preserves_evidence_and_cannot_be_overwritten(self):
        with patch.object(deployment.shutil, "which", return_value="/existing/uv"), \
                patch.object(deployment, "_run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(deployment.DeploymentError, "Offline locked"):
                deployment.stage_installation(SOURCE, self.prefix, apply=True)
        report = json.loads((self.prefix / "installation-report.json").read_text())
        self.assertIn("Offline locked", report["failure"])
        self.assertFalse(report["applied"])
        with self.assertRaisesRegex(deployment.DeploymentError, "already exists"):
            deployment.stage_installation(SOURCE, self.prefix, apply=True)

    def test_cli_without_apply_is_nonmutating(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = deployment.main(["stage", "--source", str(SOURCE), "--prefix", str(self.prefix)])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["applied"])
        self.assertFalse(self.prefix.exists())


class UserDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="researchops-user-deploy-unit-")
        self.parent = Path(self.temp.name)
        self.root = self.parent / "installation"
        self.root.mkdir(mode=0o700)
        self.source = self.parent / "source"
        self.source.mkdir(mode=0o700)
        for name in deployment.SOURCE_FILES:
            (self.source / name).write_text('[project]\nversion="0.2.0"\n' if name == "pyproject.toml" else "fixture")
        for name in deployment.SOURCE_TREES:
            directory = self.source / name
            directory.mkdir()
            (directory / "sample.txt").write_text("synthetic source")
        for tree in ("systemd", "systemd-user"):
            directory = self.source / "deploy" / tree
            directory.mkdir(parents=True)
            for name in deployment.UNIT_NAMES:
                (directory / name).write_text(
                    "[Service]\nExecStart=@ROOT@/current/.venv/bin/researchctl "
                    "--config @ROOT@/config/settings.yaml web serve --host 127.0.0.1 --port @PORT@\n")
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def run_success(self, command, *, cwd, env):
        self.calls.append((command, cwd, env))
        output = "0.2.0\n" if "-c" in command else "usage: researchctl\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def test_user_preflight_reads_no_existing_config_or_subprocess(self):
        secret = self.root / "settings.yaml"
        secret.write_text("DO_NOT_OPEN")
        with patch.object(Path, "read_text", side_effect=AssertionError("read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("read")), \
                patch.object(subprocess, "run", side_effect=AssertionError("process")):
            report = deployment.user_preflight(self.root)
        self.assertIn("EXISTING_USER_DEPLOYMENT_REQUIRES_REVIEW_NO_OVERWRITE", report["blockers"])
        self.assertFalse(report["services_started"])

    def test_user_plan_is_read_only(self):
        with patch.object(deployment, "_run", side_effect=AssertionError("process")):
            report = deployment.user_installation(self.source, self.root)
        self.assertFalse(report["applied"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(report["runner_default"], "codex_exec")
        self.assertTrue(report["source_sha256"])

    def test_user_root_must_exist_be_private_and_owned(self):
        for mode in (0o755, 0o750, 0o777, 0o711, 0o600):
            self.root.chmod(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(deployment.DeploymentError, "0700"):
                deployment.user_installation(self.source, self.root, apply=True)
        self.root.chmod(0o700)
        with patch.object(deployment.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(deployment.DeploymentError, "0700"):
                deployment.user_installation(self.source, self.root, apply=True)
        with self.assertRaisesRegex(deployment.DeploymentError, "NOT_PROVISIONED"):
            deployment.user_installation(self.source, self.parent / "missing", apply=True)

    def test_user_install_root_and_overlap_rejected(self):
        with patch.object(deployment.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(deployment.DeploymentError, "UNPRIVILEGED"):
                deployment.user_installation(self.source, self.root, apply=True)
        with self.assertRaisesRegex(deployment.DeploymentError, "outside"):
            deployment.user_installation(self.source, self.source / "nested")

    def test_user_root_symlink_or_regular_file_rejected(self):
        link = self.parent / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(deployment.DeploymentError, "Symlink"):
            deployment.user_installation(self.source, link, apply=True)
        file = self.parent / "not-directory"
        file.write_text("preserved")
        with self.assertRaisesRegex(deployment.DeploymentError, "NOT_DIRECTORY"):
            deployment.user_installation(self.source, file, apply=True)
        self.assertEqual(file.read_text(), "preserved")

    def test_user_install_rejects_existing_contents_without_overwrite(self):
        sentinel = self.root / "existing.db"
        sentinel.write_text("existing-data")
        with self.assertRaisesRegex(deployment.DeploymentError, "NO_OVERWRITE"):
            deployment.user_installation(self.source, self.root, apply=True)
        self.assertEqual(sentinel.read_text(), "existing-data")
        self.assertEqual(list(self.root.iterdir()), [sentinel])

    def test_user_install_rejects_source_hardlinks_and_symlinks(self):
        readme = self.source / "README.md"
        os.link(readme, self.parent / "readme-alias")
        with self.assertRaisesRegex(deployment.DeploymentError, "hardlink"):
            deployment.user_installation(self.source, self.root, apply=True)
        self.assertEqual(list(self.root.iterdir()), [])
        (self.parent / "readme-alias").unlink()
        readme.unlink()
        readme.symlink_to(self.source / "uv.lock")
        with self.assertRaisesRegex(deployment.DeploymentError, "Symlink"):
            deployment.user_installation(self.source, self.root, apply=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_user_install_rejects_symlink_user_template_and_bad_version(self):
        template = self.source / "deploy/systemd-user" / deployment.UNIT_NAMES[0]
        template.unlink()
        template.symlink_to(self.source / "README.md")
        with self.assertRaisesRegex(deployment.DeploymentError, "Symlink"):
            deployment.user_installation(self.source, self.root, apply=True)
        template.unlink()
        template.write_text("[Service]\n")
        (self.source / "pyproject.toml").write_text('[project]\nversion="../bad"\n')
        with self.assertRaisesRegex(deployment.DeploymentError, "version"):
            deployment.user_installation(self.source, self.root, apply=True)

    def test_user_install_rejects_missing_uv_and_invalid_port_without_writes(self):
        with patch.object(deployment.shutil, "which", return_value=None):
            with self.assertRaisesRegex(deployment.DeploymentError, "UV_INSTALLATION"):
                deployment.user_installation(self.source, self.root, apply=True)
        for port in (0, 80, 1023, 65536, True, "8765"):
            with self.subTest(port=port), self.assertRaisesRegex(deployment.DeploymentError, "port"):
                deployment.user_installation(self.source, self.root, web_port=port, apply=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_user_install_safe_defaults_internal_link_logs_and_locked_package(self):
        with patch.object(deployment, "_run", side_effect=self.run_success), \
                patch.object(deployment.shutil, "which", side_effect=lambda name: "/existing/" + name), \
                patch.dict(os.environ, {"SMTP_PASSWORD": "secret", "PYTHONPATH": "/injected",
                                        "UV_PROJECT_ENVIRONMENT": "/existing-env", "RESEARCHOPS_CONFIG": "/private"}):
            report = deployment.user_installation(self.source, self.root, apply=True, web_port=18765)
        self.assertTrue(report["applied"])
        self.assertEqual(len(self.calls), 4)
        for argument in ("--frozen", "--offline", "--no-dev", "--no-editable"):
            self.assertIn(argument, self.calls[0][0])
        self.assertEqual(self.calls[0][0][self.calls[0][0].index("--link-mode") + 1], "copy")
        for command, cwd, env in self.calls:
            self.assertEqual(cwd, self.root)
            self.assertNotIn("SMTP_PASSWORD", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("UV_PROJECT_ENVIRONMENT", env)
            self.assertEqual(env["UV_LINK_MODE"], "copy")
            self.assertEqual(env["RESEARCHOPS_CONFIG"], report["config"])
            self.assertEqual(env["TZ"], "Asia/Seoul")
            self.assertNotIn("systemctl", " ".join(command))
            self.assertNotIn("init", command)
        settings = load_settings(report["config"])
        self.assertEqual(settings.runner.default_type, "codex_exec")
        self.assertEqual(settings.runner.codex_binary, "/existing/codex")
        self.assertEqual(settings.paths.data_dir, self.root / "data")
        self.assertEqual(settings.paths.tasks_dir, self.root / "config/tasks")
        self.assertEqual(settings.paths.schemas_dir, Path(report["application"]) / "schemas")
        self.assertTrue(settings.web.enabled)
        self.assertEqual(settings.web.bind, "127.0.0.1")
        self.assertEqual(settings.web.port, 18765)
        self.assertTrue(settings.delivery.global_handoff_kill_switch)
        self.assertEqual(settings.delivery.default_mode, "dry_run")
        self.assertFalse(settings.raw_config["scheduler"]["enabled"])
        with patch.dict(os.environ, {}, clear=True):
            smtp = load_delivery_config(self.root / "data/delivery_config.yaml")
        self.assertFalse(smtp.enabled)
        self.assertFalse(smtp.auto_dispatch)
        self.assertEqual(smtp.recipient_groups, {})
        self.assertEqual((self.root / "current").resolve(), Path(report["application"]))
        self.assertFalse((self.root / "data/researchops.db").exists())
        self.assertEqual(list((self.root / "config/tasks").iterdir()), [])
        for name in deployment.UNIT_NAMES:
            text = (self.root / "units" / name).read_text()
            self.assertNotIn("@ROOT@", text)
            self.assertNotIn("@PORT@", text)
            self.assertIn(str(self.root / "current/.venv/bin/researchctl"), text)
        for field in ("database_initialized", "services_started", "services_registered", "production_runner_ready"):
            self.assertFalse(report[field])
        self.assertEqual(report["systemd_user_unit_verify"], "passed")
        saved = json.loads(Path(report["report_path"]).read_text())
        self.assertEqual(saved, report)
        self.assertEqual(stat.S_IMODE(Path(report["config"]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(report["report_path"]).stat().st_mode), 0o600)

    def test_user_install_preserves_failed_package_evidence_and_refuses_retry(self):
        with patch.object(deployment.shutil, "which", return_value="/existing/uv"), \
                patch.object(deployment, "_run", return_value=subprocess.CompletedProcess([], 1, "failed", "build error")):
            with self.assertRaisesRegex(deployment.DeploymentError, "Offline locked"):
                deployment.user_installation(self.source, self.root, apply=True)
        reports = list((self.root / "evidence").glob("installation-*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertFalse(report["applied"])
        self.assertFalse(report["services_started"])
        self.assertIn("Offline locked", report["failure"])
        self.assertEqual((self.root / "evidence/offline-install.stderr.log").read_text(), "build error")
        with self.assertRaisesRegex(deployment.DeploymentError, "NO_OVERWRITE"):
            deployment.user_installation(self.source, self.root, apply=True)

    def test_user_install_partial_staging_failure_preserves_evidence(self):
        original_write = deployment._exclusive_write

        def fail_source(path, payload, mode=0o600):
            if path.name == "pyproject.toml":
                raise OSError("synthetic staging failure")
            return original_write(path, payload, mode)

        with patch.object(deployment, "_exclusive_write", side_effect=fail_source), self.assertRaises(OSError):
            deployment.user_installation(self.source, self.root, apply=True)
        report = json.loads(next((self.root / "evidence").glob("installation-*.json")).read_text())
        self.assertEqual(report["failure"], "OSError")
        self.assertFalse(report["applied"])
        self.assertFalse((self.root / "current").exists())

    def test_user_install_timeout_keeps_partial_output(self):
        timeout = subprocess.TimeoutExpired(["uv"], 120, output=b"partial build", stderr=b"partial error")
        with patch.object(deployment, "_run", side_effect=timeout), self.assertRaises(subprocess.TimeoutExpired):
            deployment.user_installation(self.source, self.root, apply=True)
        report = json.loads(next((self.root / "evidence").glob("installation-*.json")).read_text())
        self.assertEqual(report["failure"], "TimeoutExpired")
        self.assertTrue(report["commands"][0]["timed_out"])
        self.assertEqual((self.root / "evidence/offline-install.stdout.log").read_text(), "partial build")

    def test_user_install_unit_verify_failure_is_not_started(self):
        def run(command, *, cwd, env):
            result = self.run_success(command, cwd=cwd, env=env)
            if "verify" in command:
                result.returncode = 1
                result.stderr = "synthetic unit error"
            return result

        with patch.object(deployment, "_run", side_effect=run), \
                patch.object(deployment.shutil, "which", side_effect=lambda name: "/existing/" + name), \
                self.assertRaisesRegex(deployment.DeploymentError, "verification failed"):
            deployment.user_installation(self.source, self.root, apply=True)
        report = json.loads(next((self.root / "evidence").glob("installation-*.json")).read_text())
        self.assertFalse(report["services_started"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["systemd_user_unit_verify"], "failed")

    def test_user_install_version_mismatch_fails_without_opening_database(self):
        def run(command, *, cwd, env):
            result = self.run_success(command, cwd=cwd, env=env)
            if "-c" in command:
                result.stdout = "wrong-version\n"
            return result

        with patch.object(deployment, "_run", side_effect=run), \
                self.assertRaisesRegex(deployment.DeploymentError, "version differs"):
            deployment.user_installation(self.source, self.root, apply=True)
        self.assertFalse((self.root / "data/researchops.db").exists())

    def test_user_cli_default_is_nonmutating_and_preflight_reports_blockers(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = deployment.main(["user-install", "--source", str(self.source), "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["applied"])
        self.assertEqual(list(self.root.iterdir()), [])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = deployment.main(["user-preflight", "--root", str(self.parent / "absent")])
        self.assertEqual(code, 78)
        self.assertIn("USER_ROOT_NOT_PROVISIONED", json.loads(output.getvalue())["blockers"])


if __name__ == "__main__":
    unittest.main()
