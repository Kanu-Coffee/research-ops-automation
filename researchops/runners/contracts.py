"""Read-only contracts for unavailable real AI execution boundaries.

This module does not authenticate, invoke a provider, or offer a capability flag
that enables real execution. Installing a binary cannot complete a missing
credential broker, network proxy, or quota backend.
"""

import shutil
from typing import Any, Dict

from researchops.runners.base import RunnerExecutionResult, RunnerInvocationContext
from researchops.runners.launcher import IsolatedProcessLauncher


PREFLIGHT_BLOCKED_EXIT = 78


def real_runner_preflight(binary: str, provider: str,
                          launcher: IsolatedProcessLauncher) -> Dict[str, Any]:
    """Describe independent blockers without opening credentials or spawning.

    ``authenticated=None`` means not inspected, not an authentication failure.
    ``available`` describes executable discovery only; ``ready`` stays false
    until the actual missing backends are implemented and accepted.
    """
    binary_path = shutil.which(binary)
    isolation = launcher.readiness()
    blockers = []
    if binary_path is None:
        blockers.append({
            "id": "RUNNER-BINARY-UNAVAILABLE", "owner": "operator",
            "category": "configuration", "scope": provider,
            "reason": "Configured executable is not discoverable or executable",
            "resume_condition": "Provide an installed, executable CLI path in runner configuration",
        })
    for identifier, reason, resume in (
        ("INT-CREDENTIAL-BROKER", "Data-only model-to-isolated-code development path exists; hostile-task provider control is not accepted",
         "Verify native-tool prevention and protected control dispatch for general tasks"),
        ("INT-RESEARCH-PROXY", "Bounded public fetch backend exists; operating runner egress mediation is not connected or accepted",
         "Connect the protected fetch dispatcher and verify all task egress uses its restrictions"),
        ("INT-WORKSPACE-QUOTA", "Dedicated-filesystem capacity backend exists; persistent volumes and all writable mounts are not accepted",
         "Provision dedicated volumes, verify byte/inode exhaustion and cover auxiliary writable mounts"),
    ):
        blockers.append({"id": identifier, "owner": "development", "category": "implementation",
                         "scope": provider, "reason": reason, "resume_condition": resume})
    if not isolation["ready"]:
        blockers.append({
            "id": "EXT-RUNNER-ISOLATION", "owner": "operator", "category": "host_prerequisite",
            "scope": provider, "reason": "; ".join(isolation["blockers"]),
            "resume_condition": "Provision bubblewrap and a delegated cgroup v2 memory/pids subtree",
        })
    return {
        "provider": provider, "available": binary_path is not None,
        "binary_path": binary_path, "version": None,
        "authenticated": None, "authentication_status": "not_checked",
        "credential_contents_inspected": False, "ready": False, "spawned": False,
        "blocker": "RUNNER-BOUNDARY-INCOMPLETE", "blockers": blockers,
        "internal_missing": [item["id"] for item in blockers if item["owner"] == "development"],
        "operator_missing": [item["id"] for item in blockers if item["owner"] == "operator"],
        "isolation": isolation,
        "development_components": {
            "model_to_isolated_code_probe": "validate-runner-boundary --live",
            "public_fetch_backend": "implemented_not_operating_egress_gate",
            "filesystem_capacity_backend": "implemented_requires_dedicated_volume",
        },
        "notes": "Executable discovery is not live readiness. No provider invocation or authentication was attempted.",
    }


def blocked_execution(context: RunnerInvocationContext,
                      preflight: Dict[str, Any]) -> RunnerExecutionResult:
    """Return a held invocation, distinguishable from a provider/process failure."""
    reason = "RUNNER-BOUNDARY-INCOMPLETE: " + "; ".join(
        f"{item['id']}: {item['reason']}" for item in preflight["blockers"])
    return RunnerExecutionResult(
        success=False, exit_code=PREFLIGHT_BLOCKED_EXIT, stdout="", stderr=reason,
        error_message=reason, cleanup_verified=True,
        isolation=preflight,
        events=[{
            "type": "runner_blocked", "provider": preflight["provider"],
            "task_id": context.task_id, "run_id": context.run_id,
            "stage": context.invocation_stage, "attempt": context.attempt,
            "fencing_token": context.fencing_token,
            "reason": reason, "blockers": preflight["blockers"],
            "spawned": False, "authentication_status": "not_checked",
        }],
    )
