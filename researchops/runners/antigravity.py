"""Production Antigravity adapter for trusted operator-authored tasks."""

from pathlib import Path
from researchops.runners.production import TrustedProductionRunner


class AntigravityRunner(TrustedProductionRunner):
    def __init__(self, antigravity_binary: str = "agy", *, cgroup_root: Path | None = None):
        self.antigravity_binary = antigravity_binary
        super().__init__("antigravity_exec", antigravity_binary, cgroup_root=cgroup_root)
