"""Command line interface (researchctl) for ResearchOps."""

import argparse
from contextlib import ExitStack
import getpass
import threading
from datetime import datetime
import json
from pathlib import Path
import signal
import sys
from uuid import uuid4
from typing import Any, List, Optional

from researchops import __version__
from researchops.services.application import ApplicationService, create_application_service
from researchops.errors import ResearchOpsError
from researchops.config import load_settings
from researchops.operations import runtime_guard



def _retry_execution_settings(app, args):
    names = ("research", "compose")
    if not any(getattr(args, stage + "_" + key, None) is not None
               for stage in names for key in ("provider", "model", "effort")):
        return None
    previous = app.runs.show_run(args.run_id)["execution_settings"]
    selected = {}
    for stage in names:
        provider = getattr(args, stage + "_provider", None)
        model = getattr(args, stage + "_model", None)
        effort = getattr(args, stage + "_effort", None)
        if provider is None and model is None and effort is None:
            continue
        value = dict(previous[stage])
        if provider is not None and provider != value["type"]:
            value = {"type": provider, "model": None, "reasoning_effort": None}
        if model is not None:
            value["model"] = None if model == "default" else model
            value["reasoning_effort"] = None
        if effort is not None:
            value["reasoning_effort"] = None if effort == "default" else effort
        selected[stage] = value
    return selected


def format_output(data: Any, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        if isinstance(data, (dict, list)):
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(data)


def build_parser() -> argparse.ArgumentParser:
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Output results as JSON")
    common_parser.add_argument("--config", type=str, default=argparse.SUPPRESS, help="Path to settings YAML")

    parser = argparse.ArgumentParser(
        prog="researchctl",
        description="ResearchOps CLI - Universal Market Research Orchestrator",
        parents=[common_parser]
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # init
    subparsers.add_parser("init", parents=[common_parser], help="Initialize directories and database schema")

    # version
    subparsers.add_parser("version", parents=[common_parser], help="Print ResearchOps version")

    # doctor
    subparsers.add_parser("doctor", parents=[common_parser], help="Run system diagnostics and check tool capabilities")

    auth_parser = subparsers.add_parser("auth", parents=[common_parser], help="Initialize or recover Web login accounts")
    auth_sub = auth_parser.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("setup-token", parents=[common_parser], help="Issue a one-use initial admin token, valid for 30 minutes")
    auth_reset = auth_sub.add_parser("reset-password", parents=[common_parser], help="Reset a Web account password using a protected terminal prompt")
    auth_reset.add_argument("username", help="Existing Web login username")

    validation = subparsers.add_parser("validate-runners", parents=[common_parser],
        help="Validate bundled synthetic samples in a fresh runtime; --live explicitly enables model calls")
    validation.add_argument("--runner", action="append", choices=["fake", "codex_exec", "antigravity_exec"],
        help="Repeat to select engines (default: all; real preflight blockers return exit 78)")
    validation.add_argument("--output-parent", help="Existing directory for a new private evidence subdirectory (default: temporary directory)")
    validation.add_argument("--live", action="store_true", help="Explicitly authorize real CLI model calls for bundled synthetic samples only; SMTP remains dry-run")
    validation.add_argument("--sample", action="append", choices=["document-extract", "numeric-compare", "partial-coverage", "no-updates"], help="Repeat to select synthetic samples (default: all)")
    validation.add_argument("--timeout-seconds", type=int, default=120, help="Live per-phase deadline, 1–300 seconds")
    validation.add_argument("--model", help="Optional explicit provider model; requires --live")

    tool_validation = subparsers.add_parser("validate-runner-tools", parents=[common_parser],
        help="Explicit native file/code/web/MCP capability probes in temporary workspaces; no SMTP")
    tool_validation.add_argument("--runner", action="append", choices=["codex_exec", "antigravity_exec"])
    tool_validation.add_argument("--scenario", action="append", choices=["file-write", "file-code", "code-repair", "web-search", "mcp-research", "mcp-public-docs", "combined-research"])
    tool_validation.add_argument("--live", action="store_true", help="Authorize trusted native tool/model calls; preserve CLI permissions unless separate Agy auto-approval is selected")
    tool_validation.add_argument("--agy-dangerously-skip-permissions", action="store_true",
        help="Auto-approve ALL Agy tool permission requests in these trusted sessions; requires --live and only --runner antigravity_exec; sandbox retained by default")
    tool_validation.add_argument("--agy-no-sandbox", action="store_true",
        help="Explicitly approve sandbox-off Agy execution for selected synthetic code scenarios only; requires --live and --agy-dangerously-skip-permissions")
    tool_validation.add_argument("--output-parent", help="Existing parent of a fresh private evidence directory")
    tool_validation.add_argument("--timeout-seconds", type=int, default=180)
    tool_validation.add_argument("--model", help="Optional explicit provider model; requires --live")

    boundary = subparsers.add_parser("validate-runner-boundary", parents=[common_parser],
        help="Fixed synthetic model-to-isolated-code test; no operator runtime or SMTP")
    boundary.add_argument("--runner", action="append", choices=["codex_exec", "antigravity_exec"])
    boundary.add_argument("--live", action="store_true", help="Authorize fixed synthetic model calls and namespace/cgroup execution")
    boundary.add_argument("--cgroup-root", help="Existing dedicated delegated cgroup v2 subtree")
    boundary.add_argument("--output-parent", help="Existing parent for new private evidence")
    boundary.add_argument("--timeout-seconds", type=int, default=120)
    boundary.add_argument("--model", help="Optional explicit model; requires --live")

    # task
    task_parser = subparsers.add_parser("task", parents=[common_parser], help="Manage task definitions and versions")
    task_sub = task_parser.add_subparsers(dest="task_command", help="Task subcommands")

    task_sub.add_parser("sync", parents=[common_parser], help="Sync canonical task packages from tasks directory")
    task_sub.add_parser("list", parents=[common_parser], help="List all registered tasks")

    show_task_p = task_sub.add_parser("show", parents=[common_parser], help="Show task details")
    show_task_p.add_argument("task_id", type=str, help="Task ID")

    val_task_p = task_sub.add_parser("validate", parents=[common_parser], help="Validate active task package")
    val_task_p.add_argument("task_id", type=str, help="Task ID")

    en_task_p = task_sub.add_parser("enable", parents=[common_parser], help="Enable task")
    en_task_p.add_argument("task_id", type=str, help="Task ID")

    dis_task_p = task_sub.add_parser("disable", parents=[common_parser], help="Disable task")
    dis_task_p.add_argument("task_id", type=str, help="Task ID")

    appr_task_p = task_sub.add_parser("approve-delivery", parents=[common_parser], help="Approve live external delivery for task")
    appr_task_p.add_argument("task_id", type=str, help="Task ID")

    act_task_p = task_sub.add_parser("activate", parents=[common_parser], help="Activate sealed task version")
    act_task_p.add_argument("task_id", type=str, help="Task ID")
    act_task_p.add_argument("version_hash", type=str, help="Version hash")

    # task template
    template_p = task_sub.add_parser("template", parents=[common_parser], help="Manage task templates")
    template_sub = template_p.add_subparsers(dest="template_command", help="Template subcommands")
    template_sub.add_parser("list", parents=[common_parser], help="List available task templates")

    # task version
    version_p = task_sub.add_parser("version", parents=[common_parser], help="Manage task versions")
    version_sub = version_p.add_subparsers(dest="version_command", help="Version subcommands")

    v_list_p = version_sub.add_parser("list", parents=[common_parser], help="List versions of a task")
    v_list_p.add_argument("task_id", type=str, help="Task ID")

    v_act_p = version_sub.add_parser("activate", parents=[common_parser], help="Activate sealed task version")
    v_act_p.add_argument("task_id", type=str, help="Task ID")
    v_act_p.add_argument("version_hash", type=str, help="Version hash")

    v_dry_p = version_sub.add_parser("dry-run", parents=[common_parser], help="Execute dry-run for a specific candidate version")
    v_dry_p.add_argument("task_id", type=str, help="Task ID")
    v_dry_p.add_argument("version_hash", type=str, help="Candidate version hash")

    # run
    run_parser = subparsers.add_parser("run", parents=[common_parser], help="Enqueue or manage task runs")
    run_sub = run_parser.add_subparsers(dest="run_command", help="Run subcommands")

    run_exec_p = run_sub.add_parser("exec", parents=[common_parser], help="Execute task run")
    run_exec_p.add_argument("task_id", type=str, help="Task ID to execute")
    run_exec_p.add_argument("--dry-run", action="store_true", help="Force dry-run execution mode")
    run_exec_p.add_argument("--candidate", type=str, default=None, help="Specific candidate version hash to run")

    run_list_p = run_sub.add_parser("list", parents=[common_parser], help="List runs")
    run_list_p.add_argument("--task", type=str, default=None, help="Filter by task ID")
    run_list_p.add_argument("--status", type=str, default=None, help="Filter by status")
    run_list_p.add_argument("--limit", type=int, default=50, help="Max results")

    run_show_p = run_sub.add_parser("show", parents=[common_parser], help="Show details of a run")
    run_show_p.add_argument("run_id", type=str, help="Run ID")

    run_art_p = run_sub.add_parser("artifacts", parents=[common_parser], help="List archived artifacts for run")
    run_art_p.add_argument("run_id", type=str, help="Run ID")

    run_cancel_p = run_sub.add_parser("cancel", parents=[common_parser], help="Cancel a running run")
    run_cancel_p.add_argument("run_id", type=str, help="Run ID")

    run_logs_p = run_sub.add_parser("logs", parents=[common_parser], help="Get run logs")
    run_logs_p.add_argument("run_id", type=str, help="Run ID")

    run_retry_p = run_sub.add_parser("retry", parents=[common_parser], help="Retry a run")
    run_retry_p.add_argument("run_id", type=str, help="Run ID")
    run_retry_p.add_argument("--request-key", default=None, help="Stable key for replay-safe enqueue")

    run_comp_p = run_sub.add_parser("compose-only", parents=[common_parser], help="Re-run composition stage only")
    run_comp_p.add_argument("run_id", type=str, help="Run ID")
    run_comp_p.add_argument("--request-key", default=None, help="Stable key for replay-safe enqueue")

    run_send_p = run_sub.add_parser("send-email", parents=[common_parser],
        help="Queue the validated archived email without running Research or Compose")
    run_send_p.add_argument("run_id", help="Run containing the prepared email")
    run_send_p.add_argument("--request-key", default=None, help="Stable key for replay-safe enqueue")
    run_send_status_p = run_sub.add_parser("send-email-status", parents=[common_parser],
        help="Inspect prepared email eligibility without sending or invoking models")
    run_send_status_p.add_argument("run_id", help="Run to inspect")

    run_retry_p.add_argument("--scope", choices=("full", "compose_only"), default=None,
                             help="Explicit retry scope; omitted preserves the previous scope")
    for retry_parser in (run_retry_p, run_comp_p):
        for stage in ("research", "compose"):
            retry_parser.add_argument("--" + stage + "-provider", choices=("codex_exec", "antigravity_exec"), default=None)
            retry_parser.add_argument("--" + stage + "-model", default=None, help="Model ID, or default for CLI default")
            retry_parser.add_argument("--" + stage + "-effort", default=None, help="Model-supported effort, or default")

    # workspace
    ws_parser = subparsers.add_parser("workspace", parents=[common_parser], help="Inspect and manage task workspaces")
    ws_sub = ws_parser.add_subparsers(dest="ws_command", help="Workspace subcommands")

    ws_insp_p = ws_sub.add_parser("inspect", parents=[common_parser], help="Inspect task workspace")
    ws_insp_p.add_argument("task_id", type=str, help="Task ID")

    ws_snap_p = ws_sub.add_parser("snapshot", parents=[common_parser], help="Create a compressed snapshot of task workspace")
    ws_snap_p.add_argument("task_id", type=str, help="Task ID")

    ws_reset_p = ws_sub.add_parser("reset", parents=[common_parser], help="Reset task workspace project directory")
    ws_reset_p.add_argument("task_id", type=str, help="Task ID")
    ws_reset_p.add_argument("--confirm", action="store_true", required=True, help="Confirm reset action")

    ws_purge_p = ws_sub.add_parser("purge", parents=[common_parser], help="Purge entire task workspace directory")
    ws_purge_p.add_argument("task_id", type=str, help="Task ID")
    ws_purge_p.add_argument("--confirm", action="store_true", required=True, help="Confirm purge action")

    # handoff
    ho_parser = subparsers.add_parser("handoff", parents=[common_parser], help="Inspect and manage delivery handoffs")
    ho_sub = ho_parser.add_subparsers(dest="ho_command", help="Handoff subcommands")

    ho_list_p = ho_sub.add_parser("list", parents=[common_parser], help="List delivery handoffs")
    ho_list_p.add_argument("--task", type=str, default=None, help="Filter by task ID")
    ho_list_p.add_argument("--status", type=str, default=None, help="Filter by status")

    ho_show_p = ho_sub.add_parser("show", parents=[common_parser], help="Show handoff details")
    ho_show_p.add_argument("handoff_id", type=str, help="Handoff ID")

    ho_repub_p = ho_sub.add_parser("republish", parents=[common_parser], help="Republish handoff using exact idempotency key")
    ho_repub_p.add_argument("handoff_id", type=str, help="Handoff ID")

    # receipt
    rc_parser = subparsers.add_parser("receipt", parents=[common_parser], help="Import and reconcile delivery receipts")
    rc_sub = rc_parser.add_subparsers(dest="rc_command", help="Receipt subcommands")

    rc_imp_p = rc_sub.add_parser("import", parents=[common_parser], help="Import external delivery receipt")
    rc_imp_p.add_argument("receipt_file", type=str, help="Path to receipt JSON file")

    rc_rec_p = rc_sub.add_parser("reconcile", parents=[common_parser], help="Reconcile handoff with receipt")
    rc_rec_p.add_argument("handoff_id", type=str, help="Handoff ID")

    # delivery
    deliv_parser = subparsers.add_parser("delivery", parents=[common_parser], help="Built-in SMTP delivery configuration and testing")
    deliv_sub = deliv_parser.add_subparsers(dest="deliv_command", help="Delivery subcommands")

    deliv_cfg_p = deliv_sub.add_parser("config", parents=[common_parser], help="Inspect or update SMTP and recipient delivery config")
    deliv_cfg_p.add_argument("--set-host", type=str, default=None, help="Set SMTP host (e.g. smtp.gmail.com)")
    deliv_cfg_p.add_argument("--set-port", type=int, default=None, help="Set SMTP port (e.g. 587)")
    deliv_cfg_p.add_argument("--set-user", type=str, default=None, help="Set SMTP username / email")
    secret_args = deliv_cfg_p.add_mutually_exclusive_group()
    secret_args.add_argument("--set-password", action="store_true", help="Read SMTP password using a hidden prompt")
    secret_args.add_argument("--password-stdin", action="store_true", help="Read SMTP password from stdin")
    deliv_cfg_p.add_argument("--enabled", choices=["on", "off"], help="Enable or disable built-in SMTP delivery")
    deliv_cfg_p.add_argument("--set-sender", type=str, default=None, help="Set default sender email")
    deliv_cfg_p.add_argument("--set-sender-name", type=str, default=None, help="Set default sender name")
    deliv_cfg_p.add_argument("--tls", type=str, choices=["on", "off"], default=None, help="Toggle STARTTLS (on/off)")
    deliv_cfg_p.add_argument("--ssl", type=str, choices=["on", "off"], default=None, help="Toggle SSL (on/off)")
    deliv_cfg_p.add_argument("--add-recipient", nargs=2, metavar=("GROUP_ID", "EMAIL"), default=None, help="Add recipient email to group")
    deliv_cfg_p.add_argument("--remove-recipient", nargs=2, metavar=("GROUP_ID", "EMAIL"), default=None, help="Remove recipient email from group")

    deliv_test_p = deliv_sub.add_parser("test-connection", parents=[common_parser], help="Test connection and authentication to SMTP server")

    deliv_send_p = deliv_sub.add_parser("send-test", parents=[common_parser], help="Send a test email to verify delivery")
    deliv_send_p.add_argument("--to", type=str, required=True, help="Destination email address")

    deliv_disp_p = deliv_sub.add_parser("dispatch", parents=[common_parser], help="Dispatch prepared handoff package(s) via SMTP")
    deliv_disp_p.add_argument("--handoff-id", type=str, default=None, help="Specific handoff ID to dispatch (dispatches all pending if omitted)")
    smtp_worker = deliv_sub.add_parser("worker", parents=[common_parser], help="Consume durable SMTP queue")
    smtp_worker.add_argument("--once", action="store_true")
    smtp_worker.add_argument("--poll-interval", type=float, default=2.0)
    smtp_job = deliv_sub.add_parser("smtp-job", parents=[common_parser], help="Inspect queued SMTP diagnostic or delivery attempt")
    smtp_job.add_argument("job_id")
    retry_email = deliv_sub.add_parser("retry-email", parents=[common_parser],
        help="Queue another SMTP attempt for the preserved email, without research or composition")
    retry_email.add_argument("handoff_id")
    retry_email.add_argument("--request-key", default=None, help="Reuse this key for a replay-safe retry request")
    retry_email.add_argument("--allow-uncertain", action="store_true",
        help="Explicitly accept possible duplicate delivery after checking an uncertain attempt; requires --reason")
    retry_email.add_argument("--reason", default="", help="Delivery check and retry reason, required with --allow-uncertain (max 500 characters)")
    retry_status = deliv_sub.add_parser("retry-status", parents=[common_parser],
        help="Inspect email retry eligibility, attempts and next scheduled attempt without SMTP access")
    retry_status.add_argument("handoff_id")

    # schedule
    sched_parser = subparsers.add_parser("schedule", parents=[common_parser], help="Manage schedule and evaluate cron ticks")
    sched_sub = sched_parser.add_subparsers(dest="schedule_command", help="Schedule subcommands")
    sched_tick_p = sched_sub.add_parser("tick", parents=[common_parser], help="Evaluate cron schedules for enabled tasks and enqueue due runs")
    sched_tick_p.add_argument("--now", type=str, default=None, help="Reference ISO timestamp for schedule evaluation")

    # worker
    worker_parser = subparsers.add_parser("worker", parents=[common_parser], help="Queue consumption worker daemon")
    worker_sub = worker_parser.add_subparsers(dest="worker_command", help="Worker subcommands")
    worker_run_p = worker_sub.add_parser("run", parents=[common_parser], help="Start queue consumption worker loop")
    worker_run_p.add_argument("--poll-interval", type=float, default=2.0, help="Poll interval in seconds (default: 2.0)")
    worker_run_p.add_argument("--once", action="store_true", help="Process currently queued runs and exit")
    worker_run_p.add_argument("--concurrency", type=int, default=1, help="Max worker concurrency (default: 1)")

    # web
    web_parser = subparsers.add_parser("web", parents=[common_parser], help="Web UI inspection console")
    web_sub = web_parser.add_subparsers(dest="web_command", help="Web subcommands")
    web_serve_p = web_sub.add_parser("serve", parents=[common_parser], help="Start Web UI HTTP server")
    web_serve_p.add_argument("--host", type=str, default=None, help="Loopback host override")
    web_serve_p.add_argument("--port", type=int, default=None, help="Port override (otherwise web.port)")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    # Normalize only the root command; a task ID or "delivery worker" is not
    # a top-level worker invocation. Global options may precede the command.
    command_index = 0
    while command_index < len(argv):
        argument = argv[command_index]
        if argument == "--config":
            command_index += 2
        elif argument == "--json" or argument.startswith("--config="):
            command_index += 1
        else:
            break
    command = argv[command_index] if command_index < len(argv) else None
    next_index = command_index + 1
    run_cmds = {"exec", "list", "show", "artifacts", "cancel", "logs", "retry", "compose-only",
                "send-email", "send-email-status", "-h", "--help"}
    if command == "run" and next_index < len(argv):
        if not argv[next_index].startswith("-") and argv[next_index] not in run_cmds:
            argv.insert(next_index, "exec")
    defaults = {"schedule": "tick", "worker": "run", "web": "serve"}
    if command in defaults:
        if next_index == len(argv) or (argv[next_index].startswith("-") and argv[next_index] not in {"-h", "--help"}):
            argv.insert(next_index, defaults[command])
    if command == "task" and next_index < len(argv) and argv[next_index] == "template":
        template_index = next_index + 1
        if template_index == len(argv) or (argv[template_index].startswith("-") and argv[template_index] not in {"-h", "--help"}):
            argv.insert(template_index, "list")


    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help()
        return 0

    as_json = getattr(args, "json", False)
    config_path = getattr(args, "config", None)
    runtime_lifetime = ExitStack()

    try:
        if args.subcommand == "validate-runner-boundary":
            if config_path:
                parser.error("validate-runner-boundary never loads operator --config")
            from researchops.boundary_validation import validate_runner_boundary
            result = validate_runner_boundary(args.runner, live=args.live, cgroup_root=args.cgroup_root,
                output_parent=args.output_parent, timeout_seconds=args.timeout_seconds, model=args.model)
            format_output(result, as_json)
            return result["exit_code"]
        if args.subcommand == "validate-runner-tools":
            if config_path:
                parser.error("validate-runner-tools never loads operator --config")
            from researchops.runner_tool_validation import validate_runner_tools
            result = validate_runner_tools(args.runner, scenarios=args.scenario, live=args.live,
                output_parent=args.output_parent, timeout_seconds=args.timeout_seconds, model=args.model,
                agy_dangerously_skip_permissions=args.agy_dangerously_skip_permissions, agy_no_sandbox=args.agy_no_sandbox)
            format_output(result, as_json)
            return result["exit_code"]
        if args.subcommand == "validate-runners":
            if config_path:
                parser.error("validate-runners does not load operator --config; it creates an isolated runtime")
            from researchops.runner_validation import validate_runners
            result = validate_runners(args.runner, output_parent=args.output_parent, live=args.live,
                samples=args.sample, timeout_seconds=args.timeout_seconds, model=args.model)
            format_output(result, as_json)
            return result["exit_code"]
        settings = load_settings(config_path)
        runtime_lifetime.enter_context(runtime_guard(settings.paths.database))
        app = create_application_service(config_path)

        if args.subcommand == "version":
            format_output({"version": __version__}, as_json)
            return 0

        elif args.subcommand == "init":
            format_output({"status": "initialized", "db": str(app.settings.paths.database)}, as_json)
            return 0

        elif args.subcommand == "doctor":
            res = app.doctor.check_all()
            format_output(res, as_json)
            return 0 if res.get("overall_status") == "ok" else 1

        elif args.subcommand == "auth":
            if args.auth_command == "setup-token":
                # The operator explicitly requested this one-time secret. It is
                # never written to config, task workspaces, logs, or archives.
                token = app.auth.issue_setup_token()
                if as_json:
                    format_output({"setup_token": token, "expires_in_seconds": 1800}, True)
                else:
                    print(token)
                return 0
            if args.auth_command == "reset-password":
                import getpass
                import warnings
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", getpass.GetPassWarning)
                        password = getpass.getpass("새 비밀번호: ")
                        confirmation = getpass.getpass("새 비밀번호 확인: ")
                except getpass.GetPassWarning:
                    raise ResearchOpsError("비밀번호를 숨길 수 있는 터미널에서 실행하세요.") from None
                if password != confirmation:
                    raise ResearchOpsError("비밀번호 확인이 일치하지 않습니다.")
                app.auth.reset_password_from_cli(args.username, password)
                format_output({"status": "password_reset", "username": args.username}, as_json)
                return 0

        elif args.subcommand == "task":
            cmd = args.task_command
            if cmd == "sync":
                synced = app.tasks.sync_canonical_tasks()
                format_output([{"task_id": v.task_id, "hash": v.version_hash} for v in synced], as_json)
                return 0
            elif cmd == "list":
                format_output(app.tasks.list_tasks(), as_json)
                return 0
            elif cmd == "show":
                format_output(app.tasks.show_task(args.task_id), as_json)
                return 0
            elif cmd == "validate":
                ok, errs, warns = app.tasks.validate_task(args.task_id)
                format_output({"valid": ok, "errors": errs, "warnings": warns}, as_json)
                return 0 if ok else 1
            elif cmd == "enable":
                app.tasks.set_task_enabled(args.task_id, True)
                format_output({"task_id": args.task_id, "enabled": True}, as_json)
                return 0
            elif cmd == "disable":
                app.tasks.set_task_enabled(args.task_id, False)
                format_output({"task_id": args.task_id, "enabled": False}, as_json)
                return 0
            elif cmd == "approve-delivery":
                app.tasks.approve_delivery(args.task_id, True)
                format_output({"task_id": args.task_id, "delivery_approved": True}, as_json)
                return 0
            elif cmd == "activate":
                app.tasks.activate_version(args.task_id, args.version_hash)
                format_output({"task_id": args.task_id, "active_version_hash": args.version_hash}, as_json)
                return 0
            elif cmd == "template":
                tcmd = getattr(args, "template_command", None) or "list"
                if tcmd == "list":
                    format_output(app.tasks.list_templates(), as_json)
                    return 0
                else:
                    template_p.print_help()
                    return 0
            elif cmd == "version":
                vcmd = getattr(args, "version_command", None)
                if vcmd == "list":
                    format_output(app.tasks.list_versions(args.task_id), as_json)
                    return 0
                elif vcmd == "activate":
                    app.tasks.activate_version(args.task_id, args.version_hash)
                    format_output({"task_id": args.task_id, "active_version_hash": args.version_hash}, as_json)
                    return 0
                elif vcmd == "dry-run":
                    run = app.runs.enqueue_run(
                        task_id=args.task_id,
                        trigger_type="candidate_dry_run",
                        candidate_version_hash=args.version_hash
                    )
                    completed_run = app.runs.execute_run(run.run_id, force_dry_run=True)
                    format_output(app.runs.show_run(completed_run.run_id), as_json)
                    return 0 if completed_run.status in ("succeeded", "awaiting_receipt") else 1
                else:
                    version_p.print_help()
                    return 0
            else:
                task_parser.print_help()
                return 0

        elif args.subcommand == "run":
            rcmd = args.run_command
            if rcmd == "exec":
                run = app.runs.enqueue_run(
                    task_id=args.task_id,
                    trigger_type="candidate_dry_run" if args.candidate else "manual",
                    candidate_version_hash=args.candidate,
                    force_dry_run=args.dry_run,
                )
                completed_run = app.runs.execute_run(run.run_id, force_dry_run=args.dry_run)
                format_output(app.runs.show_run(completed_run.run_id), as_json)
                return 0 if completed_run.status in ("succeeded", "awaiting_receipt") else 1
            elif rcmd == "list":
                runs = app.runs.list_runs(task_id=args.task, status=args.status, limit=args.limit)
                format_output([
                    {
                        "run_id": r.run_id,
                        "task_id": r.task_id,
                        "status": r.status,
                        "phase": r.phase,
                        "created_at": r.created_at
                    }
                    for r in runs
                ], as_json)
                return 0
            elif rcmd == "show":
                format_output(app.runs.show_run(args.run_id), as_json)
                return 0
            elif rcmd == "artifacts":
                format_output(app.runs.get_run_artifacts(args.run_id), as_json)
                return 0
            elif rcmd == "cancel":
                r = app.runs.cancel_run(args.run_id)
                format_output({"run_id": r.run_id, "status": r.status}, as_json)
                return 0
            elif rcmd == "logs":
                logs = app.runs.get_run_logs(args.run_id)
                print(logs)
                return 0
            elif rcmd == "retry":
                selected = _retry_execution_settings(app, args)
                r = app.runs.retry_run(args.run_id, request_key=args.request_key, scope=args.scope,
                    execution_settings=selected, selection_source={"kind": "manual"} if selected else None)
                format_output(app.runs.show_run(r.run_id), as_json)
                return 0
            elif rcmd == "compose-only":
                selected = _retry_execution_settings(app, args)
                r = app.runs.compose_only(args.run_id, request_key=args.request_key,
                    execution_settings=selected, selection_source={"kind": "manual"} if selected else None)
                format_output(app.runs.show_run(r.run_id), as_json)
                return 0
            elif rcmd == "send-email":
                r = app.runs.send_prepared_email(args.run_id, request_key=args.request_key)
                format_output(app.runs.show_run(r.run_id), as_json)
                return 0
            elif rcmd == "send-email-status":
                format_output(app.runs.prepared_email_status(args.run_id), as_json)
                return 0
            else:
                run_parser.print_help()
                return 0

        elif args.subcommand == "workspace":
            wcmd = args.ws_command
            if wcmd == "inspect":
                format_output(app.workspaces.inspect(args.task_id), as_json)
                return 0
            elif wcmd == "snapshot":
                res = app.workspaces.snapshot(args.task_id)
                format_output(res, as_json)
                return 0
            elif wcmd == "reset":
                app.workspaces.reset(args.task_id)
                format_output({"task_id": args.task_id, "status": "reset"}, as_json)
                return 0
            elif wcmd == "purge":
                app.workspaces.purge(args.task_id)
                format_output({"task_id": args.task_id, "status": "purged"}, as_json)
                return 0

        elif args.subcommand == "handoff":
            hcmd = args.ho_command
            if hcmd == "list":
                hos = app.delivery.list_handoffs(task_id=args.task, status=args.status)
                format_output([
                    {
                        "handoff_id": h.handoff_id,
                        "task_id": h.task_id,
                        "run_id": h.run_id,
                        "mode": h.mode,
                        "status": h.status,
                        "recipient_group_id": h.recipient_group_id
                    }
                    for h in hos
                ], as_json)
                return 0
            elif hcmd == "show":
                format_output(app.delivery.show_handoff(args.handoff_id), as_json)
                return 0
            elif hcmd == "republish":
                ho = app.delivery.republish_handoff(args.handoff_id)
                format_output({"handoff_id": ho.handoff_id, "status": ho.status}, as_json)
                return 0

        elif args.subcommand == "receipt":
            rcmd = args.rc_command
            if rcmd == "import":
                receipt, handoff = app.delivery.import_receipt(Path(args.receipt_file))
                format_output({
                    "receipt_id": receipt.external_receipt_id,
                    "handoff_id": handoff.handoff_id,
                    "external_delivery_status": handoff.external_delivery_status,
                    "handoff_status": handoff.status
                }, as_json)
                return 0
            elif rcmd == "reconcile":
                ho = app.delivery.reconcile_receipt(args.handoff_id)
                format_output({"handoff_id": args.handoff_id, "status": ho.status if ho else "not_found"}, as_json)
                return 0
        elif args.subcommand == "delivery":
            dcmd = getattr(args, "deliv_command", "config") or "config"
            if dcmd == "config":
                cfg = app.delivery.get_delivery_config()
                dirty = False
                if getattr(args, "set_host", None) is not None:
                    cfg.smtp.host = args.set_host
                    dirty = True
                if getattr(args, "set_port", None) is not None:
                    cfg.smtp.port = args.set_port
                    dirty = True
                if getattr(args, "set_user", None) is not None:
                    cfg.smtp.username = args.set_user
                    dirty = True
                if args.set_password or args.password_stdin:
                    cfg.smtp.password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("SMTP password: ")
                    if not cfg.smtp.password:
                        raise ResearchOpsError("Password cannot be empty")
                    dirty = True
                if args.enabled is not None:
                    cfg.enabled = args.enabled == "on"
                    dirty = True
                if getattr(args, "set_sender", None) is not None:
                    cfg.smtp.sender_email = args.set_sender
                    dirty = True
                if getattr(args, "set_sender_name", None) is not None:
                    cfg.smtp.sender_name = args.set_sender_name
                    dirty = True
                if getattr(args, "tls", None) is not None:
                    cfg.smtp.use_tls = (args.tls == "on")
                    dirty = True
                if getattr(args, "ssl", None) is not None:
                    cfg.smtp.use_ssl = (args.ssl == "on")
                    dirty = True
                if getattr(args, "add_recipient", None) is not None:
                    gid, addr = args.add_recipient
                    if gid not in cfg.recipient_groups:
                        cfg.recipient_groups[gid] = []
                    if addr not in cfg.recipient_groups[gid]:
                        cfg.recipient_groups[gid].append(addr)
                    dirty = True
                if getattr(args, "remove_recipient", None) is not None:
                    gid, addr = args.remove_recipient
                    if gid in cfg.recipient_groups and addr in cfg.recipient_groups[gid]:
                        cfg.recipient_groups[gid].remove(addr)
                    dirty = True

                if dirty:
                    app.delivery.save_delivery_config(cfg)
                    print("Updated delivery configuration.")

                out = cfg.to_dict()
                # Mask password for display
                if out.get("smtp", {}).get("password"):
                    out["smtp"]["password"] = cfg.smtp.masked_password()
                format_output(out, as_json)
                return 0

            elif dcmd == "test-connection":
                ok, msg = app.delivery.test_smtp_connection()
                status_str = "QUEUED" if ok else "FAILED"
                format_output({"status": status_str, "success": ok, "message": msg}, as_json)
                return 0 if ok else 1

            elif dcmd == "send-test":
                ok, msg = app.delivery.send_test_email(args.to)
                status_str = "QUEUED" if ok else "FAILED"
                format_output({"status": status_str, "success": ok, "message": msg, "to": args.to}, as_json)
                return 0 if ok else 1

            elif dcmd == "retry-email":
                reason = args.reason.strip()
                if args.allow_uncertain and not reason:
                    raise ResearchOpsError("--allow-uncertain requires --reason describing the delivery check and duplicate-delivery decision")
                if len(reason) > 500:
                    raise ResearchOpsError("Email retry reason must be at most 500 characters")
                request_key = args.request_key or "email-retry-" + uuid4().hex
                job = app.delivery.retry_email(args.handoff_id, request_key=request_key,
                    allow_uncertain=args.allow_uncertain, reason=reason)
                format_output({**job, "request_key": request_key,
                    "message": "Preserved email retry request processed; inspect status for the current transmission result."}, as_json)
                return 0
            elif dcmd == "retry-status":
                format_output(app.delivery.email_retry_status(args.handoff_id), as_json)
                return 0
            elif dcmd == "smtp-job":
                job = app.delivery.show_smtp_job(args.job_id)
                if not job:
                    raise ResearchOpsError("SMTP job not found")
                format_output(job, as_json)
                return 0
            elif dcmd == "worker":
                stop = threading.Event()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: stop.set())
                if args.poll_interval <= 0:
                    raise ResearchOpsError("Poll interval must be positive")
                while not stop.is_set():
                    results = app.delivery.dispatch_all_pending()
                    if args.once:
                        format_output([{"handoff_id": hid, "success": ok, "message": msg}
                                       for hid, ok, msg in results], as_json)
                        return 0 if all(ok for _, ok, _ in results) else 1
                    stop.wait(args.poll_interval)
                return 0
            elif dcmd == "dispatch":
                if getattr(args, "handoff_id", None):
                    ok, rcpt, msg = app.delivery.dispatch_handoff(args.handoff_id)
                    format_output({
                        "handoff_id": args.handoff_id,
                        "success": ok,
                        "receipt_id": rcpt.external_receipt_id if rcpt else None,
                        "message": msg
                    }, as_json)
                    return 0 if ok else 1
                else:
                    results = app.delivery.dispatch_all_pending()
                    format_output([
                        {"handoff_id": hid, "success": ok, "message": msg}
                        for hid, ok, msg in results
                    ], as_json)
                    return 0

        elif args.subcommand == "schedule":
            scmd = getattr(args, "schedule_command", "tick") or "tick"
            if scmd == "tick":
                ref_time = None
                if getattr(args, "now", None):
                    ref_time = datetime.fromisoformat(args.now)
                results = app.scheduler.schedule_tick(now=ref_time)
                format_output(results, as_json)
                return 0

        elif args.subcommand == "worker":
            wcmd = getattr(args, "worker_command", "run") or "run"
            if wcmd == "run":
                poll_interval = getattr(args, "poll_interval", 2.0)
                once = getattr(args, "once", False)
                print(f"Starting ResearchOps Worker (worker_id={app.worker.worker_id}, once={once})...")

                def _handle_sig(sig, frame):
                    print(f"\nReceived signal {sig}, stopping worker gracefully...")
                    app.worker.stop()

                signal.signal(signal.SIGINT, _handle_sig)
                signal.signal(signal.SIGTERM, _handle_sig)

                if args.concurrency < 1 or args.concurrency > app.settings.runner.global_concurrency:
                    raise ResearchOpsError("Concurrency exceeds the configured global limit")
                if poll_interval <= 0:
                    raise ResearchOpsError("Poll interval must be positive")
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    futures = [pool.submit(app.worker.run_worker, poll_interval=poll_interval, once=once)
                               for _ in range(args.concurrency)]
                    completed = sum(future.result() for future in futures)
                print(f"Worker finished. Total runs processed: {completed}")
                return 0

        elif args.subcommand == "web":
            wcmd = getattr(args, "web_command", "serve") or "serve"
            if wcmd == "serve":
                from researchops.web.server import create_web_server
                server = create_web_server(app, host=args.host, port=args.port)
                host, port = server.server_address[:2]
                print(f"ResearchOps Web UI running on http://{host}:{port}/ (Asia/Seoul)")
                print("Press Ctrl+C to stop.")
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    print("\nShutting down ResearchOps Web UI...")
                finally:
                    server.server_close()
                return 0


    except ResearchOpsError as roe:
        print(f"Error: {roe}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 2
    finally:
        runtime_lifetime.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
