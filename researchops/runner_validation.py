"""Repeatable synthetic validation, isolated from all operator runtime settings.

Real engines are preflight-only by default. Explicit live approval permits
bounded output-only calls on bundled synthetic tasks, never fake substitution.
"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from zoneinfo import ZoneInfo

import yaml

from researchops.config import bundled_path, load_settings
from researchops.runners.antigravity import AntigravityRunner
from researchops.runners.codex import CodexRunner
from researchops.runners.fake import FakeRunner
from researchops.services.application import ApplicationService
from researchops.workspace.security import read_safe_bytes


SAMPLE_NAMES = ("document-extract", "numeric-compare", "partial-coverage", "no-updates")
RUNNER_NAMES = ("fake", "codex_exec", "antigravity_exec")


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _check_sample(app, version, package, *, runner=None, runner_name="fake"):
    expected = _json(package / "fixtures/expected.json")
    app.orchestrator.custom_runner = runner or FakeRunner(fixtures_dir=package / "fixtures")
    queued = app.runs.enqueue_run(version.task_id, trigger_type="candidate_dry_run",
        candidate_version_hash=version.version_hash, scheduled_for=expected["scheduled_for"],
        force_dry_run=True)
    run = app.runs.execute_run(queued.run_id, force_dry_run=True)
    archive = app.settings.paths.run_archive_dir / run.task_id / run.run_id
    checks = {"run_succeeded": run.status == expected["run_status"],
              "seoul_date": run.local_date == expected["logical_date"] and run.timezone == "Asia/Seoul",
              "workspace_released": not app.workspace_mgr.is_locked(run.task_id)[0],
              "lease_released": app.run_repo.get_lease(run.run_id) is None,
              "not_activated": not app.task_repo.get_active_version(run.task_id)}
    research = app.run_repo.get_research_result(run.run_id)
    comp_input = app.run_repo.get_composition_input(run.run_id)
    composition = app.run_repo.get_composition_result(run.run_id)
    handoff = app.delivery_repo.get_handoff_for_run(run.run_id)
    original = _json(package / "fixtures/sample-result.json")
    records = (comp_input or {}).get("reportable_records", [])
    by_id = {record["record_id"]: record for record in records}
    checks.update({
        "research_status": research is not None and research.status == expected["research_status"],
        "records_preserved": records == original["records"] and len(records) == expected["record_count"],
        "record_ids": [record["record_id"] for record in records] == expected["record_ids"],
        "sample_values": all(by_id.get(rid, {}).get(key) == value
            for rid, fields in expected["record_assertions"].items() for key, value in fields.items()),
        "warnings_preserved": research is not None and len(research.warnings) >= expected["min_warning_count"],
        "coverage": all((comp_input or {}).get("coverage", {}).get(key) == expected[expected_key]
            for key, expected_key in (("complete", "coverage_complete"),
                ("expected_target_count", "expected_target_count"), ("completed_target_count", "completed_target_count"))),
        "composition_contract": composition is not None and composition.recipient_group_id == expected["recipient_group_id"]
            and composition.included_record_ids == expected["record_ids"],
        "dry_run_only": handoff is not None and handoff.mode == "dry_run" and handoff.published_at is None,
        "archive_present": (archive / "artifact-manifest.json").is_file(),
    })
    if checks["archive_present"]:
        index = _json(archive / "artifact-manifest.json")
        checks["archive_hashes"] = all(
            hashlib.sha256(read_safe_bytes(archive / entry["relative_path"], archive, 100_000_000)).hexdigest() == entry["sha256"]
            for entry in [index["run_manifest"], *index["artifacts"]])
        checks["research_bytes_preserved"] = (archive / "result.json").is_file() and (
            archive / "result.json").read_bytes() == (package / "fixtures/sample-result.json").read_bytes()
    if runner is not None:
        checks["records_preserved"] = research is not None and records == research.records and len(records) == expected["record_count"]
        checks.pop("research_bytes_preserved", None)
        checks["research_snapshot_preserved"] = (archive / "result.json").is_file() and research is not None and _json(archive / "result.json")["records"] == research.records
        checks["two_live_responses"] = len(runner.calls) == 2 and all(call["terminal_response"] and call["success"] for call in runner.calls)
    return {"sample": package.name, "task_id": run.task_id, "runner": runner_name,
            "evidence_type": "live_model_pipeline" if runner else "synthetic_fixture_pipeline", "status": "passed" if all(checks.values()) else "failed",
            "run_id": run.run_id, "run_status": run.status, "local_date": run.local_date,
            "record_count": len(records), "archive": str(archive), "checks": checks,
            "error": run.error_message,
            "model_invocations": sum(call["model_invocations"] for call in runner.calls) if runner else 0,
            "cli_invocations": sum(call["cli_invocations"] for call in runner.calls) if runner else 0,
            "calls": runner.calls if runner else []}


def _run_validation(root, selected, cases, preflight, *, live=False, samples=SAMPLE_NAMES, timeout_seconds=120, model=None):
    # Assets follow the installed module, never RESEARCHOPS_ROOT. Operator
    # environment overrides must not substitute production tasks or schemas.
    module_root = Path(__file__).resolve().parents[1]
    source = bundled_path(module_root, "examples/runner-validation")
    schemas = bundled_path(module_root, "schemas")
    for name in SAMPLE_NAMES:
        if not (source / name / "fixtures/expected.json").is_file():
            raise ValueError(f"Missing bundled validation sample: {name}")
        if any(path.is_symlink() for path in (source / name).rglob("*")):
            raise ValueError(f"Validation sample cannot contain symlinks: {name}")
    config = {"environment": "test", "timezone": "Asia/Seoul",
        "paths": {"tasks_dir": str(root / "tasks"), "data_dir": str(root / "runtime"),
                  "database": str(root / "runtime/researchops.db"), "schemas_dir": str(schemas)},
        "runner": {"default_type": "fake"},
        "delivery": {"default_mode": "dry_run", "global_handoff_kill_switch": True},
        "web": {"enabled": False}}
    config_file = root / "settings.yaml"
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    settings = load_settings(config_file)
    settings.paths.repo_root = root
    # One read-only probe per engine. Blocked sample dependencies do not create
    # repeated adapter calls, provider sessions, or misleading failed run rows.
    for name, runner in (("codex_exec", CodexRunner()), ("antigravity_exec", AntigravityRunner())):
        if name not in selected:
            continue
        preflight[name] = runner.preflight()
        if live:
            continue
        for sample in samples:
            cases.append({"sample": sample, "runner": name, "status": "blocked",
                "evidence_type": "preflight_only", "model_invocations": 0,
                "blocked_by": ["LIVE-VALIDATION-NOT-REQUESTED"] + [item["id"] for item in preflight[name]["blockers"]]})
    if "fake" in selected:
        for name in samples:
            shutil.copytree(source / name, root / "tasks" / name, symlinks=True)
        app = ApplicationService(settings)
        versions = {version.task_id: version for version in app.tasks.sync_canonical_tasks()}
        for name in samples:
            package = root / "tasks" / name
            expected = _json(package / "fixtures/expected.json")
            try:
                cases.append(_check_sample(app, versions[expected["task_id"]], package))
            except Exception as exc:
                recent = app.runs.list_runs(task_id=expected["task_id"], limit=1)
                cases.append({"sample": name, "runner": "fake", "status": "failed",
                    "evidence_type": "synthetic_fixture_pipeline", "error": str(exc), "model_invocations": 0,
                    "run_id": recent[0].run_id if recent else None,
                    "archive": str(settings.paths.run_archive_dir / recent[0].task_id / recent[0].run_id) if recent else None})
    if live:
        from researchops.runners.development_runner import OutputOnlyDevelopmentRunner
        for provider in selected:
            if provider == "fake":
                continue
            live_root = root / "live" / provider
            live_root.mkdir(parents=True, mode=0o700)
            live_config = {**config, "paths": {"tasks_dir": str(live_root / "tasks"),
                "data_dir": str(live_root / "runtime"), "database": str(live_root / "runtime/researchops.db"),
                "schemas_dir": str(schemas)}}
            live_config_path = live_root / "settings.yaml"
            live_config_path.write_text(yaml.safe_dump(live_config), encoding="utf-8")
            live_settings = load_settings(live_config_path)
            live_settings.paths.repo_root = live_root
            for name in samples:
                package = live_root / "tasks" / name
                shutil.copytree(source / name, package, symlinks=True)
                definition = yaml.safe_load((package / "task.yaml").read_text())
                definition["runner"]["type"] = provider
                definition["runner"]["timeout_seconds"] = timeout_seconds
                (package / "task.yaml").write_text(yaml.safe_dump(definition, allow_unicode=True), encoding="utf-8")
            live_app = ApplicationService(live_settings)
            versions = {version.task_id: version for version in live_app.tasks.sync_canonical_tasks()}
            held_reason = None
            for name in samples:
                if held_reason:
                    cases.append({"sample": name, "runner": provider, "status": "blocked",
                        "evidence_type": "live_model_pipeline", "model_invocations": 0,
                        "blocked_by": [held_reason]})
                    continue
                package = live_root / "tasks" / name
                expected = _json(package / "fixtures/expected.json")
                runner = OutputOnlyDevelopmentRunner(provider, name, approval=True,
                    evidence_dir=live_root / "control-evidence" / name, timeout_seconds=timeout_seconds, model=model)
                try:
                    case = _check_sample(live_app, versions[expected["task_id"]], package, runner=runner, runner_name=provider)
                except Exception as exc:
                    recent = live_app.runs.list_runs(task_id=expected["task_id"], limit=1)
                    case = {"sample": name, "runner": provider, "status": "failed", "evidence_type": "live_model_pipeline",
                        "error": str(exc), "model_invocations": sum(call["model_invocations"] for call in runner.calls),
                        "cli_invocations": sum(call["cli_invocations"] for call in runner.calls), "calls": runner.calls,
                        "run_id": recent[0].run_id if recent else None,
                        "archive": str(live_settings.paths.run_archive_dir / recent[0].task_id / recent[0].run_id) if recent else None}
                cases.append(case)
                if any(not call["cleanup_verified"] for call in runner.calls):
                    held_reason = "REMOTE-CLEANUP-UNVERIFIED"
                elif any(not call["terminal_response"] for call in runner.calls):
                    held_reason = "PROVIDER-INVOCATION-UNVERIFIED"


def validate_runners(runners=None, *, output_parent=None, live=False, samples=None, timeout_seconds=120, model=None):
    """Create a fresh private runtime/report directory; never open default config.

    The caller may select an existing parent for retained evidence, not an
    existing runtime to reuse or overwrite. Live is an explicit synthetic-only grant.
    Once the directory exists, setup failures also produce a retained report.
    """
    selected = tuple(dict.fromkeys(RUNNER_NAMES if runners is None else runners))
    if not selected or any(name not in RUNNER_NAMES for name in selected):
        raise ValueError("Select fake, codex_exec, or antigravity_exec")
    sample_names = tuple(dict.fromkeys(SAMPLE_NAMES if samples is None else samples))
    if not sample_names or any(name not in SAMPLE_NAMES for name in sample_names):
        raise ValueError("Select only bundled synthetic sample names")
    if type(live) is not bool or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
        raise ValueError("Live must be boolean and timeout must be between 1 and 300 seconds")
    if (model is not None or timeout_seconds != 120) and not live:
        raise ValueError("Model/timeout options require explicit live validation")
    parent = Path(output_parent).resolve(strict=True) if output_parent is not None else None
    root = Path(tempfile.mkdtemp(prefix="researchops-validation-", dir=parent))
    cases, preflight = [], {}
    try:
        _run_validation(root, selected, cases, preflight, live=live, samples=sample_names, timeout_seconds=timeout_seconds, model=model)
    except Exception as exc:
        cases.append({"sample": None, "runner": None, "status": "failed",
            "evidence_type": "suite_setup", "error": str(exc), "model_invocations": 0})
    totals = {status: sum(case["status"] == status for case in cases) for status in ("passed", "failed", "blocked")}
    exit_code = 1 if totals["failed"] else 78 if totals["blocked"] else 0
    report = {"schema_version": 1, "created_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "timezone": "Asia/Seoul", "status": "failed" if totals["failed"] else "blocked" if totals["blocked"] else "passed",
        "exit_code": exit_code, "evidence_dir": str(root), "report_path": str(root / "report.json"),
        "totals": totals, "model_invocations": sum(case.get("model_invocations", 0) for case in cases),
        "cli_invocations": sum(case.get("cli_invocations", 0) for case in cases),
        "upstream_request_count": None if live else 0, "live_approved": live,
        "production_runner_ready": False, "smtp_send_attempts": 0,
        "operator_config_loaded": False, "preflight": preflight, "cases": cases,
        "limitations": ["Synthetic fixture success does not validate real model behavior or output quality.",
            "Live synthetic validation uses an output-only control process; hostile-task OS/network/quota isolation is not complete.",
            "Existing CLI login is used only with explicit live approval. Credentials are never copied into tasks. Operator runtime/SMTP/cgroup configuration is not loaded.",
            "model_invocations counts confirmed terminal model responses; provider-side request/retry counts are not known."]}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
