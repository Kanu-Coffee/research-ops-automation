"""Dedicated filesystem capacity contracts; kernel probes are explicit opt-in."""

from dataclasses import replace
import errno
import io
import os
from pathlib import Path
import tempfile
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from researchops.errors import WorkspaceError
from researchops.runners.workspace_quota import DedicatedFilesystemQuota, _Mount, _mounts
from researchops.runners.launcher import IsolatedProcessLauncher


class TestDedicatedFilesystemQuota(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.phase = self.root / "phase"
        self.project.mkdir()
        self.phase.mkdir()
        info = self.root.stat()
        self.mount = _Mount(120, f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}", "/",
                            self.root, "ext4", frozenset({"rw", "nodev", "nosuid"}))
        self.capacity = SimpleNamespace(f_blocks=16, f_frsize=4096, f_files=32, f_flag=0,
                                        f_bavail=0, f_favail=0)
        self.backend = DedicatedFilesystemQuota(self.root, max_bytes=65536, max_inodes=32)
        self.mount_patch = patch("researchops.runners.workspace_quota._mounts", return_value=[self.mount])
        self.mount_reader = self.mount_patch.start()
        self.addCleanup(self.mount_patch.stop)
        self.capacity_patch = patch("researchops.runners.workspace_quota.os.fstatvfs", return_value=self.capacity)
        self.capacity_patch.start()
        self.addCleanup(self.capacity_patch.stop)

    def test_total_capacity_and_identity_are_recorded(self):
        evidence = self.backend.inspect([self.project, self.phase])
        self.assertEqual(evidence.total_bytes, 65536)
        self.assertEqual(evidence.total_inodes, 32)
        self.assertTrue(evidence.persistent)
        self.assertEqual(self.backend.inspect([self.project], expected=evidence), evidence)
        ready = self.backend.readiness([self.project])
        self.assertTrue(ready["ready"])
        self.assertFalse(ready["whole_runner_boundary_ready"])
        self.assertFalse(ready["kernel_exhaustion_verified"])

    def test_free_space_does_not_establish_byte_ceiling(self):
        self.capacity.f_blocks = 17
        with self.assertRaisesRegex(WorkspaceError, "Total filesystem byte"):
            self.backend.inspect([self.project])

    def test_zero_unlimited_and_oversized_inode_capacities_fail(self):
        for inodes in (0, 33, 2**64 - 1):
            with self.subTest(inodes=inodes):
                self.capacity.f_files = inodes
                self.assertFalse(self.backend.readiness([self.project])["ready"])

    def test_missing_byte_capacity_and_readonly_state_fail(self):
        for field, value in (("f_blocks", 0), ("f_frsize", 0), ("f_flag", os.ST_RDONLY)):
            original = getattr(self.capacity, field)
            setattr(self.capacity, field, value)
            with self.subTest(field=field):
                self.assertFalse(self.backend.readiness([self.project])["ready"])
            setattr(self.capacity, field, original)

    def test_invalid_limits_are_rejected(self):
        for limit in (0, -1, True, 1.0, "123"):
            for key in ("max_bytes", "max_inodes"):
                limits = {"max_bytes": 1, "max_inodes": 1, key: limit}
                with self.subTest(key=key, limit=limit), self.assertRaises(ValueError):
                    DedicatedFilesystemQuota(self.root, **limits)
        with self.assertRaises(ValueError):
            DedicatedFilesystemQuota(self.root, max_bytes=1, max_inodes=1, allow_volatile="yes")

    def test_host_roots_and_noncanonical_paths_are_rejected(self):
        for root in (Path("/"), Path("/tmp"), Path("/home"), Path("relative"), self.root / ".." / self.root.name):
            backend = DedicatedFilesystemQuota(root, max_bytes=65536, max_inodes=32)
            self.assertFalse(backend.readiness([self.project])["ready"])

    def test_ordinary_directory_is_not_a_quota_mount(self):
        self.mount_reader.return_value = []
        with self.assertRaisesRegex(WorkspaceError, "exact dedicated"):
            self.backend.inspect([self.project])

    def test_bind_subdirectory_and_stacked_mounts_fail(self):
        for mounts in ([replace(self.mount, root="/shared/task")], [self.mount, self.mount]):
            self.mount_reader.return_value = mounts
            self.assertFalse(self.backend.readiness([self.project])["ready"])

    def test_nested_mount_and_wrong_device_fail(self):
        self.mount_reader.return_value = [self.mount, replace(self.mount, mount_id=121, device="99:99", path=self.phase)]
        with self.assertRaisesRegex(WorkspaceError, "Nested mounts"):
            self.backend.inspect([self.project])
        self.mount_reader.return_value = [replace(self.mount, device="99:99")]
        with self.assertRaisesRegex(WorkspaceError, "device changed"):
            self.backend.inspect([self.project])

    def test_shared_host_filesystem_or_other_mount_alias_is_rejected(self):
        for alias in (Path("/"), Path("/home"), Path("/another-task-volume")):
            self.mount_reader.return_value = [self.mount, replace(self.mount, mount_id=121, path=alias)]
            with self.subTest(alias=alias), self.assertRaisesRegex(WorkspaceError, "Shared/aliased"):
                self.backend.inspect([self.project])

    def test_unsupported_filesystem_and_unsafe_mount_options_fail(self):
        for mount in (replace(self.mount, filesystem="overlay"),
                      replace(self.mount, options=frozenset({"rw", "nosuid"})),
                      replace(self.mount, options=frozenset({"ro", "nosuid", "nodev"}))):
            self.mount_reader.return_value = [mount]
            self.assertFalse(self.backend.readiness([self.project])["ready"])

    def test_tmpfs_never_claims_persistence(self):
        self.mount_reader.return_value = [replace(self.mount, filesystem="tmpfs")]
        with self.assertRaisesRegex(WorkspaceError, "volatile"):
            self.backend.inspect([self.project])
        backend = DedicatedFilesystemQuota(self.root, max_bytes=65536, max_inodes=32, allow_volatile=True)
        self.assertTrue(backend.readiness([self.project])["ready"])
        self.assertFalse(backend.inspect([self.project]).persistent)

    def test_writable_root_external_path_missing_path_and_empty_list_fail(self):
        for paths in ([self.root], [self.root.parent], [self.root / "missing"], [],
                      [self.project / ".." / "phase"], [Path("relative")]):
            self.assertFalse(self.backend.readiness(paths)["ready"])

    def test_symlink_in_root_or_writable_path_is_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(WorkspaceError, "Symlink"):
            self.backend.inspect([link])
        backend = DedicatedFilesystemQuota(link, max_bytes=65536, max_inodes=32)
        self.assertFalse(backend.readiness([link / "child"])["ready"])

    def test_group_writable_volume_fails(self):
        self.root.chmod(0o770)
        with self.assertRaisesRegex(WorkspaceError, "group/world writable"):
            self.backend.inspect([self.project])

    def test_foreign_owned_volume_fails(self):
        with patch("researchops.runners.workspace_quota.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(WorkspaceError, "owned by the launcher"):
                self.backend.inspect([self.project])

    def test_unreadable_mount_table_fails_and_releases_descriptors(self):
        self.mount_reader.side_effect = OSError(errno.EACCES, "permission denied")
        actual_close = os.close
        with patch("researchops.runners.workspace_quota.os.close", wraps=actual_close) as close:
            ready = self.backend.readiness([self.project])
        self.assertFalse(ready["ready"])
        self.assertIn("inspection failed", ready["blockers"][0])
        close.assert_called_once()

    def test_mount_root_replacement_during_inspection_fails(self):
        root_info = self.root.stat()
        replacement = os.stat_result((root_info.st_mode, root_info.st_ino + 1,
                                      root_info.st_dev, root_info.st_nlink, root_info.st_uid,
                                      root_info.st_gid, root_info.st_size, root_info.st_atime,
                                      root_info.st_mtime, root_info.st_ctime))
        actual_stat = os.stat

        def stat_with_replacement(path, *args, **kwargs):
            if path == self.root and kwargs.get("follow_symlinks") is False:
                return replacement
            return actual_stat(path, *args, **kwargs)

        with patch("researchops.runners.workspace_quota.os.stat", side_effect=stat_with_replacement):
            with self.assertRaisesRegex(WorkspaceError, "changed during quota inspection"):
                self.backend.inspect([self.project])

    def test_identity_and_capacity_changes_invalidate_previous_snapshot(self):
        evidence = self.backend.inspect([self.project])
        self.mount_reader.return_value = [replace(self.mount, mount_id=121)]
        with self.assertRaisesRegex(WorkspaceError, "changed since preparation"):
            self.backend.inspect([self.project], expected=evidence)
        self.mount_reader.return_value = [self.mount]
        self.capacity.f_blocks = 15
        with self.assertRaisesRegex(WorkspaceError, "changed since preparation"):
            self.backend.inspect([self.project], expected=evidence)

    def test_mount_table_changes_during_inspection_fail(self):
        self.mount_reader.side_effect = [[self.mount], [replace(self.mount, mount_id=121)]]
        with self.assertRaisesRegex(WorkspaceError, "changed during quota inspection"):
            self.backend.inspect([self.project])

    def test_mountinfo_escaped_names_parse_without_device_source_disclosure(self):
        self.mount_patch.stop()
        line = "12 1 7:1 / /task\\040volume rw,nosuid,nodev - ext4 /dev/private-name rw\n"
        with patch("builtins.open", unittest.mock.mock_open(read_data=line)):
            table = _mounts()
        self.assertEqual(table[0].path, Path("/task volume"))
        self.assertNotIn("private-name", repr(table))

    def test_corrupt_and_oversized_mountinfo_fail_closed(self):
        self.mount_patch.stop()
        for content in ("broken\n", "1 2 3:4 / /t rw - ext4\n", "x" * 4_000_001):
            with patch("builtins.open", unittest.mock.mock_open(read_data=content)):
                with self.assertRaises(WorkspaceError):
                    _mounts()


class TestLauncherFilesystemQuota(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project, self.tmp, self.output = (self.root / name for name in ("project", "tmp", "output"))
        for path in (self.project, self.tmp, self.output):
            path.mkdir()
        self.quota = MagicMock(spec=DedicatedFilesystemQuota)
        self.launcher = IsolatedProcessLauncher(workspace_quota=self.quota)

    def test_invalid_quota_blocks_before_process_or_cgroup_creation(self):
        self.quota.inspect.side_effect = WorkspaceError("not a dedicated quota volume")
        with patch.object(self.launcher, "readiness", return_value={"ready": True}), \
             patch.object(self.launcher, "_create_group") as group, \
             patch("researchops.runners.launcher.subprocess.Popen") as spawn:
            result = self.launcher.run(["/usr/bin/true"], self.output, self.project, self.tmp)
        self.assertFalse(result.success)
        self.assertFalse(result.isolation["spawned"])
        self.assertFalse(result.isolation["filesystem_quota_ready"])
        self.assertTrue(result.cleanup_verified)
        self.quota.inspect.assert_called_once_with([self.project, self.tmp, self.output])
        group.assert_not_called()
        spawn.assert_not_called()

    def test_changed_quota_kills_stopped_helper_without_resuming_task(self):
        from researchops.runners.workspace_quota import FilesystemQuotaSnapshot
        snapshot = FilesystemQuotaSnapshot(str(self.root), 1, "0:2", "ext4", 65536, 32, 1, True)
        self.quota.inspect.side_effect = [snapshot, WorkspaceError("quota identity changed")]
        proc = MagicMock()
        proc.pid = 12345
        proc.returncode = None
        proc.stdout, proc.stderr = io.BytesIO(), io.BytesIO()
        group = MagicMock()
        with patch.object(self.launcher, "readiness", return_value={"ready": True}), \
             patch.object(self.launcher, "build_namespace_command", return_value=["/mock/bwrap"]), \
             patch.object(self.launcher, "_create_group", return_value=group), \
             patch.object(self.launcher, "_kill_group", return_value=True) as cleanup, \
             patch("researchops.runners.launcher.subprocess.Popen", return_value=proc), \
             patch("researchops.runners.launcher.os.waitpid", return_value=(proc.pid, (signal.SIGSTOP << 8) | 0x7f)), \
             patch("researchops.runners.launcher.os.kill") as signal_process:
            result = self.launcher.run(["/usr/bin/true"], self.output, self.project, self.tmp)
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_verified)
        self.assertFalse(result.isolation["filesystem_quota_ready"])
        self.assertIn("quota identity changed", result.error_message)
        self.assertEqual(self.quota.inspect.call_count, 2)
        self.quota.inspect.assert_called_with([self.project, self.tmp, self.output], expected=snapshot)
        signal_process.assert_not_called()
        cleanup.assert_called_once_with(group, proc)
        group.rmdir.assert_called_once()

    def test_success_reports_only_verified_task_volume_scope(self):
        from researchops.runners.workspace_quota import FilesystemQuotaSnapshot
        snapshot = FilesystemQuotaSnapshot(str(self.root), 1, "0:2", "ext4", 65536, 32, 1, True)
        self.quota.inspect.return_value = snapshot
        proc = MagicMock()
        proc.pid, proc.returncode = 12345, 0
        proc.poll.return_value = 0
        proc.stdout, proc.stderr = io.BytesIO(), io.BytesIO()
        group = MagicMock()
        with patch.object(self.launcher, "readiness", return_value={"ready": True}), \
             patch.object(self.launcher, "build_namespace_command", return_value=["/mock/bwrap"]), \
             patch.object(self.launcher, "_create_group", return_value=group), \
             patch.object(self.launcher, "_kill_group", return_value=True), \
             patch("researchops.runners.launcher.subprocess.Popen", return_value=proc), \
             patch("researchops.runners.launcher.os.waitpid", return_value=(proc.pid, (signal.SIGSTOP << 8) | 0x7f)), \
             patch("researchops.runners.launcher.os.kill") as signal_process:
            result = self.launcher.run(["/usr/bin/true"], self.output, self.project, self.tmp)
        self.assertTrue(result.success)
        self.assertTrue(result.isolation["filesystem_quota_ready"])
        self.assertFalse(result.isolation["whole_runner_boundary_ready"])
        self.assertIn("excludes /tmp and /dev/shm", result.isolation["filesystem_quota_scope"])
        self.assertEqual(result.isolation["filesystem_quota"]["total_bytes"], 65536)
        self.assertEqual(self.quota.inspect.call_count, 2)
        signal_process.assert_called_once_with(proc.pid, signal.SIGCONT)


@unittest.skipUnless(os.environ.get("RESEARCHOPS_TEST_QUOTA_VOLUME"),
                     "Requires a dedicated disposable <=16MiB/256-inode quota volume; no live AI")
class TestKernelFilesystemExhaustion(unittest.TestCase):
    def test_kernel_refuses_bytes_and_inodes_at_volume_capacity(self):
        # Operator must provision a disposable volume, not an existing task.
        # No formatting/mounting/recursive deletion is performed by this test.
        root = Path(os.environ["RESEARCHOPS_TEST_QUOTA_VOLUME"])
        self.assertEqual([path for path in root.iterdir() if path.name != "lost+found"], [],
                         "Kernel probe volume must be empty except for ext4 lost+found")
        probe = root / "researchops-quota-kernel-probe"
        probe.mkdir(mode=0o700)
        created = []
        try:
            backend = DedicatedFilesystemQuota(root, max_bytes=16 * 1024 * 1024,
                                               max_inodes=256, allow_volatile=True)
            evidence = backend.inspect([probe])
            blob = probe / "byte-probe"
            created.append(blob)
            with self.assertRaises(OSError) as failure:
                with blob.open("xb", buffering=0) as stream:
                    for _ in range(evidence.total_bytes // 4096 + 2):
                        stream.write(b"x" * 4096)
            self.assertIn(failure.exception.errno, {errno.ENOSPC, errno.EDQUOT})
            blob.unlink()
            created.remove(blob)
            with self.assertRaises(OSError) as failure:
                for index in range(evidence.total_inodes + 1):
                    path = probe / f"inode-{index}"
                    with path.open("xb"):
                        pass
                    created.append(path)
            self.assertIn(failure.exception.errno, {errno.ENOSPC, errno.EDQUOT})
            self.assertEqual(backend.inspect([probe], expected=evidence), evidence)
        finally:
            for path in created:
                path.unlink(missing_ok=True)
            probe.rmdir()
