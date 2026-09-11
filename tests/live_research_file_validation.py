"""Explicit opt-in native Research/Compose smoke with a synthetic protected PDF.

Run as ``python -m tests.live_research_file_validation --live --provider ...
--root /new/private/path``. Each invocation owns a fresh runtime and a loopback
fixture. Existing Tasks, CardRAG servers and SMTP are never used by this script.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from researchops.config import MediaProviderConfig
from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
from researchops.runners import production
from researchops.services.application import ApplicationService
from tests.media_fixture import SYNTHETIC_TOKEN
from tests.research_file_fixture import illustrated_source, target_image, task_instructions
from tests.support import isolated_settings


def _read_json(path, default):
    return json.loads(path.read_bytes()) if path.is_file() else default


def _hashes_match(archive):
    manifest = _read_json(archive / "artifact-manifest.json", {})
    return bool(manifest.get("artifacts")) and all(
        (archive / item["relative_path"]).is_file() and
        hashlib.sha256((archive / item["relative_path"]).read_bytes()).hexdigest() == item["sha256"]
        for item in manifest["artifacts"])


def _verify(app, run, archive, source, raw_pdf, expected_png, requests):
    validation = _read_json(archive / "validation-report.json", {})
    executions = validation.get("executions", [])
    mcp = [call for execution in executions for call in execution.get("isolation", {}).get("mcp_tools", [])]
    ledger = _read_json(archive / "research-acquisition/ledger.json", {})
    acquisitions = ledger.get("records", [])
    document = _read_json(archive / "result.json", {})
    ci = app.run_repo.get_composition_input(run.run_id) or {}
    artifacts = {item.get("path"): item for item in document.get("artifacts", [])}
    inline = ci.get("inline_artifacts", [])
    references = inline[0].get("derived_from", []) if len(inline) == 1 else []
    original_path, png_path = archive / "documents/original.pdf", archive / "images/figure.png"
    original_ok = original_path.is_file() and original_path.read_bytes() == raw_pdf
    png_ok = png_path.is_file() and png_path.read_bytes() == expected_png
    original_request_ok = artifacts.get("documents/original.pdf", {}).get("source") == source
    ids = artifacts.get("images/figure.png", {}).get("derived_from", [])
    ledger_ok = (len(acquisitions) == 1 and acquisitions[0].get("status") == "available" and
        acquisitions[0].get("sha256") == source["sha256"] and acquisitions[0].get("source") == source and
        acquisitions[0].get("size_bytes") == source["size_bytes"] and ledger.get("cleanup_verified") is True and
        ledger.get("failure_reason") is None and ledger.get("consumed_count") == 1)
    provenance_ok = (ledger_ok and ids == [acquisitions[0]["acquisition_id"]] and
        references == [{"acquisition_id": ids[0], "source_sha256": source["sha256"]}] and
        inline[0].get("record_ids") == ["figure-1"] and inline[0].get("path") == "images/figure.png" and
        artifacts.get("images/figure.png", {}).get("source") is None)
    html_path = archive / "email.html"
    cid_ok = bool(provenance_ok and html_path.is_file() and
        html_path.read_text().count("cid:" + inline[0]["cid"]) == 1)
    expected_paths = {"/resources/documents/" + source["document_id"],
                      "/sources/" + source["document_id"] + "/pdf"}
    fetch_ok = (len(requests) == 2 and {item["path"] for item in requests} == expected_paths and
                all(item["authenticated"] for item in requests))
    clean = len(executions) == 2 and all(item.get("cleanup_verified") is True for item in executions)
    research = next((item for item in executions if item.get("stage") == "research"), {})
    compose = next((item for item in executions if item.get("stage") == "compose"), {})
    session_closed = research.get("isolation", {}).get("research_acquisition", {}).get("cleanup_verified") is True
    compose_off = "research_acquisition" not in compose.get("isolation", {})
    no_secret = all(SYNTHETIC_TOKEN.encode() not in file.read_bytes()
        for file in archive.rglob("*") if file.is_file())
    results = {"stages": [item.get("stage") for item in executions],
        "native_model_calls": sum(item.get("isolation", {}).get("model_invocations", 0) for item in executions),
        "native_cli_calls": sum(item.get("isolation", {}).get("cli_invocations", 0) for item in executions),
        "mcp_call_count": len(mcp), "mcp_calls": mcp,
        "cleanup_verified": clean, "research_acquisition_closed": session_closed,
        "compose_acquisition_disabled": compose_off, "original_bytes_verified": original_ok,
        "target_figure_bytes_verified": png_ok, "original_source_preserved": original_request_ok,
        "acquisition_ledger_verified": ledger_ok, "derived_provenance_verified": provenance_ok,
        "inline_cid_verified": cid_ok, "protected_fetch_verified": fetch_ok,
        "protected_request_count": len(requests), "secret_absent_from_archive": no_secret,
        "archive_hashes_match": _hashes_match(archive), "input_version": ci.get("schema_version")}
    results["ok"] = (run.status == "succeeded" and results["stages"] == ["research", "compose"] and
        results["native_model_calls"] == 2 and results["native_cli_calls"] == 2 and len(mcp) == 0 and
        ci.get("schema_version") == 4 and all((clean, session_closed, compose_off, original_ok, png_ok,
            original_request_ok, ledger_ok, provenance_ok, cid_ok, fetch_ok, no_secret, results["archive_hashes_match"])))
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--provider", required=True, choices=("codex_exec", "antigravity_exec"))
    parser.add_argument("--root", type=Path, required=True, help="New private output directory; must not exist")
    parser.add_argument("--model", help="Optional model for both stages; omitted uses each CLI default")
    parser.add_argument("--binary", help="Optional selected native CLI executable")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if not 10 <= args.timeout_seconds <= 1800:
        parser.error("--timeout-seconds must be between 10 and 1800")
    binary = shutil.which(args.binary or ("codex" if args.provider == "codex_exec" else "agy"))
    if not binary or not shutil.which("pdfimages"):
        parser.error("The selected native CLI and pdfimages must already be installed")
    os.umask(0o077)
    root = args.root.resolve()
    root.mkdir(mode=0o700)
    report = {"provider": args.provider, "model": args.model, "ok": False,
              "business_task_retries": 0, "actual_smtp_calls": 0, "existing_cardrag_requests": 0,
              "fixture_only": True, "upstream_request_count": None}
    try:
        settings = isolated_settings(root / "runtime")
        settings.environment = "production"
        if args.provider == "codex_exec":
            settings.runner.codex_binary = binary
        else:
            settings.runner.antigravity_binary = binary
        save_delivery_config(BuiltinDeliveryConfig(enabled=False, auto_dispatch=False,
            recipient_groups={"synthetic-team": ["recipient@example.test"]}), settings.paths.delivery_config_file)
        expected_png = target_image(root / "expected")
        with illustrated_source(root) as state:
            settings.media.providers = {"cardrag": MediaProviderConfig(state["base_url"], state["token"])}
            app = ApplicationService(settings)
            app.tasks.create_production_task(task_id="synthetic-research-file-proof",
                name="Synthetic Research file extraction", instructions=task_instructions(state["source"]),
                runner_type=args.provider, model=args.model, recipient_group_id="synthetic-team", schedule_enabled=False)
            run = app.runs.enqueue_run("synthetic-research-file-proof", force_dry_run=True)
            report["run_id"] = run.run_id
            build_command = production.build_production_command

            def bounded(*values, **options):
                context = values[-1]
                context.timeout_seconds = min(context.timeout_seconds, args.timeout_seconds)
                return build_command(*values, **options)

            with patch.object(production, "build_production_command", side_effect=bounded), \
                    patch("smtplib.SMTP") as smtp, patch("smtplib.SMTP_SSL") as smtp_ssl:
                finished = app.runs.execute_run(run.run_id)
            report["actual_smtp_calls"] = smtp.call_count + smtp_ssl.call_count
            smtp.assert_not_called()
            smtp_ssl.assert_not_called()
            archive = settings.paths.run_archive_dir / run.task_id / run.run_id
            report.update(status=finished.status, error=finished.error_message, archive=str(archive))
            report.update(_verify(app, finished, archive, state["source"], state["raw"], expected_png, state["requests"]))
    except Exception as exc:
        report.update(ok=False, validation_exception=type(exc).__name__)
    finally:
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
