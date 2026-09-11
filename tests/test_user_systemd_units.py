"""Owned-root user units are bounded templates, never service-starting tests."""

import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
TEMPLATES = SOURCE / "deploy" / "systemd-user"
UNIT_NAMES = (
    "researchops-worker.service",
    "researchops-web.service",
    "researchops-smtp.service",
    "researchops-scheduler.service",
    "researchops-scheduler.timer",
)


def _render(name, root, port=8765):
    return (TEMPLATES / name).read_text(encoding="utf-8").replace(
        "@ROOT@", str(root)
    ).replace("@PORT@", str(port))


def _unit(text):
    result = configparser.ConfigParser(interpolation=None, strict=False)
    result.optionxform = str
    result.read_string(text)
    return result


class UserSystemdUnitTests(unittest.TestCase):
    def test_exact_template_inventory_and_supported_placeholders(self):
        self.assertEqual({path.name for path in TEMPLATES.iterdir()}, set(UNIT_NAMES))
        for name in UNIT_NAMES:
            with self.subTest(name=name):
                text = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertLessEqual(set(re.findall(r"@[A-Z_]+@", text)),
                                     {"@ROOT@", "@PORT@"})
                self.assertNotRegex(_render(name, "/opt/researchops"), r"@[A-Z_]+@")

    def test_owned_root_paths_and_no_system_only_mount_namespace_directives(self):
        # These directives either escape the owned-root layout or require
        # mount/user namespace setup unavailable to a generic user unit here.
        forbidden = {
            "User", "Group", "DynamicUser", "StateDirectory", "RuntimeDirectory",
            "CacheDirectory", "LogsDirectory", "ConfigurationDirectory",
            "ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices",
            "PrivateUsers", "PrivateNetwork", "ProtectKernelTunables",
            "ProtectKernelModules", "ProtectControlGroups", "ReadWritePaths",
            "ReadOnlyPaths", "InaccessiblePaths", "BindPaths", "BindReadOnlyPaths",
            "TemporaryFileSystem", "RootDirectory", "RootImage", "ProtectProc",
            "ProcSubset", "ProtectClock", "ProtectHostname",
        }
        root = "/opt/researchops"
        for name in UNIT_NAMES:
            with self.subTest(name=name):
                text = _render(name, root)
                unit = _unit(text)
                self.assertNotIn("/etc/researchops", text)
                self.assertNotIn("/var/lib/researchops", text)
                if "Service" not in unit:
                    continue
                service = unit["Service"]
                self.assertFalse(forbidden.intersection(service))
                self.assertEqual(service["WorkingDirectory"], root)
                self.assertTrue(service["ExecStart"].startswith(
                    root + "/current/.venv/bin/researchctl --config "
                    + root + "/config/settings.yaml "
                ))
                self.assertIn("AssertPathExists=" + root + "/config/settings.yaml", text)
                self.assertIn("AssertPathIsDirectory=" + root + "/data", text)
                self.assertIn("Environment=TZ=Asia/Seoul", text)
                self.assertIn("Environment=RESEARCHOPS_ROOT=" + root + "/current", text)

    def test_service_limits_and_group_cleanup_are_explicit(self):
        for name in UNIT_NAMES:
            with self.subTest(name=name):
                unit = _unit(_render(name, "/opt/researchops"))
                if "Service" not in unit:
                    continue
                service = unit["Service"]
                self.assertEqual(service["UMask"], "0077")
                self.assertEqual(service["NoNewPrivileges"], "yes")
                self.assertEqual(service["RestrictSUIDSGID"], "yes")
                self.assertEqual(service["LockPersonality"], "yes")
                self.assertEqual(service["LimitCORE"], "0")
                self.assertEqual(service["LimitNOFILE"], "4096")
                self.assertLessEqual(int(service["TasksMax"]), 256)
                self.assertIn("MemoryHigh", service)
                self.assertIn("MemoryMax", service)
                self.assertEqual(service["MemorySwapMax"], "0")
                self.assertEqual(service["KillMode"], "control-group")
                self.assertEqual(service["TimeoutStopSec"], "45")
                self.assertEqual(service["StandardOutput"], "journal")
                self.assertEqual(service["StandardError"], "journal")
                self.assertNotIn("ExecStartPre", service)
                if name != "researchops-worker.service":
                    self.assertNotIn("ExecStartPost", service)
                if service["Type"] == "simple":
                    self.assertEqual(service["Restart"], "on-failure")
                    self.assertEqual(service["RestartSec"], "5")
                    self.assertEqual(unit["Unit"]["StartLimitBurst"], "5")

    def test_only_worker_delegates_and_can_create_runner_namespaces(self):
        for name in UNIT_NAMES:
            with self.subTest(name=name):
                unit = _unit(_render(name, "/opt/researchops"))
                if "Service" not in unit:
                    continue
                service = unit["Service"]
                if name == "researchops-worker.service":
                    self.assertEqual(service["Delegate"], "memory pids")
                    self.assertEqual(service["DelegateSubgroup"], "control")
                    self.assertEqual(service["Slice"], "app.slice")
                    self.assertEqual(service["ExecStartPost"],
                        "/bin/sh -ec 'echo +memory +pids > /sys/fs/cgroup/user.slice/"
                        "user-%U.slice/user@%U.service/app.slice/%n/cgroup.subtree_control'")
                    self.assertNotIn("RestrictNamespaces", service)
                else:
                    self.assertNotIn("Delegate", service)
                    self.assertEqual(service["RestrictNamespaces"], "yes")

    def test_web_uses_validated_config_bind_and_rendered_port(self):
        web = _unit(_render("researchops-web.service", "/opt/researchops", 18973))
        self.assertTrue(web["Service"]["ExecStart"].endswith(
            "web serve --port 18973"))
        self.assertNotIn("--host", web["Service"]["ExecStart"])

    def test_initial_daemons_do_not_pull_in_smtp_or_timer(self):
        for name in ("researchops-worker.service", "researchops-web.service"):
            unit = _unit(_render(name, "/opt/researchops"))
            self.assertEqual(unit["Install"]["WantedBy"], "default.target")
            for relation in ("Wants", "Requires", "BindsTo", "Upholds"):
                self.assertNotIn(relation, unit["Unit"])
            self.assertNotIn("Also", unit["Install"])

    def test_scheduler_is_timer_only_seoul_and_never_network_enabled(self):
        timer = _unit(_render("researchops-scheduler.timer", "/opt/researchops"))
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* *:*:00 Asia/Seoul")
        self.assertEqual(timer["Timer"]["Unit"], "researchops-scheduler.service")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")
        scheduler = _unit(_render("researchops-scheduler.service", "/opt/researchops"))
        self.assertNotIn("Install", scheduler)
        self.assertEqual(scheduler["Service"]["Type"], "oneshot")
        self.assertEqual(scheduler["Service"]["Restart"], "no")
        self.assertEqual(scheduler["Service"]["TimeoutStartSec"], "120")
        self.assertEqual(scheduler["Service"]["RestrictAddressFamilies"], "AF_UNIX")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable")
    def test_rendered_units_verify_in_user_context_without_starting_services(self):
        with tempfile.TemporaryDirectory(prefix="researchops-user-units-") as temporary:
            root = Path(temporary)
            executable = root / "current" / ".venv" / "bin" / "researchctl"
            executable.parent.mkdir(parents=True)
            # Verification checks this executable, but must never execute it.
            executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            executable.chmod(0o700)
            (root / "config").mkdir()
            (root / "config" / "settings.yaml").write_text("{}\n", encoding="utf-8")
            (root / "data").mkdir()
            user_runtime = root / "user-runtime"
            user_runtime.mkdir(mode=0o700)
            user_home = root / "user-home"
            user_home.mkdir(mode=0o700)
            units = root / "units"
            units.mkdir()
            for name in UNIT_NAMES:
                (units / name).write_text(_render(name, root), encoding="utf-8")
            checked = subprocess.run(
                [shutil.which("systemd-analyze"), "--user", "verify",
                 *[str(units / name) for name in UNIT_NAMES]],
                capture_output=True, text=True, timeout=20,
                env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "Asia/Seoul",
                     "HOME": str(user_home), "XDG_RUNTIME_DIR": str(user_runtime)},
            )
            if checked.returncode and any(reason in checked.stderr for reason in (
                "Failed to connect to user scope bus", "Failed to connect to bus",
                "Failed to connect to service manager", "$XDG_RUNTIME_DIR not set",
            )):
                self.skipTest("user systemd verification unavailable: " + checked.stderr.strip())
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            # The host may have unrelated vendor-unit warnings. Reject any
            # ignored/unknown directive in these templates, not vendor files.
            for line in checked.stderr.splitlines():
                if str(units) in line or any(name in line for name in UNIT_NAMES):
                    self.assertNotIn("Unknown", line)
                    self.assertNotIn("Failed", line)
                    self.assertNotIn("ignoring", line)


if __name__ == "__main__":
    unittest.main()
