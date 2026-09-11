"""Read-only readiness checks; host authentication files are never inspected."""

from pathlib import Path
from typing import Any, Dict, Optional

from researchops.runners.launcher import IsolatedProcessLauncher
from researchops.runners.contracts import real_runner_preflight


class RunnerCapabilityProbe:
    def __init__(self, codex_binary: str = "codex", antigravity_binary: str = "agy",
                 cgroup_root: Optional[Path] = None, *, trusted_operator: bool = False):
        self.codex_binary = codex_binary
        self.antigravity_binary = antigravity_binary
        self.cgroup_root = cgroup_root
        self.trusted_operator = trusted_operator

    def probe_all(self) -> Dict[str, Any]:
        return {"fake_runner": {"available": True, "authenticated": False, "ready": True,
                                "version": "built-in", "notes": "No network or credentials"},
                "codex": self.probe_codex(), "antigravity": self.probe_antigravity(),
                "isolation": self.probe_isolation()}

    def _probe_runner(self, binary: str, provider: str) -> Dict[str, Any]:
        if self.trusted_operator:
            from researchops.runners.codex import CodexRunner
            from researchops.runners.antigravity import AntigravityRunner
            runner = CodexRunner(binary, cgroup_root=self.cgroup_root) if provider == "codex" else AntigravityRunner(binary, cgroup_root=self.cgroup_root)
            return runner.preflight()
        return real_runner_preflight(binary, provider,
            IsolatedProcessLauncher(cgroup_root=self.cgroup_root))

    def probe_codex(self) -> Dict[str, Any]:
        return self._probe_runner(self.codex_binary, "codex")

    def probe_antigravity(self) -> Dict[str, Any]:
        return self._probe_runner(self.antigravity_binary, "antigravity")

    def probe_isolation(self) -> Dict[str, Any]:
        readiness = IsolatedProcessLauncher(cgroup_root=self.cgroup_root).readiness()
        return {**readiness, "active_tier": "trusted-operator-production" if self.trusted_operator else readiness["tier"],
                "environment_sanitization": True, "process_tree_watchdog": True,
                "bwrap_available": bool(readiness["bwrap_path"]),
                "cgroup_v2_available": Path("/sys/fs/cgroup/cgroup.controllers").exists(),
                "hostile_task_ready": False,
                "live_runner_ready": self.trusted_operator and any(
                    self._probe_runner(binary, provider).get("ready", False) for binary, provider in
                    ((self.codex_binary, "codex"), (self.antigravity_binary, "antigravity"))),
                "notes": "Production native runners use the operator's account; hostile-task isolation is not claimed." if self.trusted_operator else "Hostile-task runner boundary is incomplete."}
