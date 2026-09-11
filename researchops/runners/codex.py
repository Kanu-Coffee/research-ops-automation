"""Production Codex adapter for trusted operator-authored task packages."""

from pathlib import Path
from researchops.runners.production import TrustedProductionRunner


class CodexRunner(TrustedProductionRunner):
    def __init__(self, codex_binary: str = "codex", *, cgroup_root: Path | None = None):
        self.codex_binary = codex_binary
        super().__init__("codex_exec", codex_binary, cgroup_root=cgroup_root)
