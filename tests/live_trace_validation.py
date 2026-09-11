"""Opt-in synthetic native Research/Compose verification; SMTP is blocked."""

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

from researchops.delivery.smtp_config import BuiltinDeliveryConfig, save_delivery_config
from researchops.runners import production
from researchops.services.application import ApplicationService
from tests.support import isolated_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--provider', choices=['codex_exec', 'antigravity_exec'], required=True)
    parser.add_argument('--transport', choices=['http', 'stdio', 'none'], required=True)
    args = parser.parse_args()
    if (args.provider == 'antigravity_exec') != (args.transport == 'none'):
        parser.error('Agy uses only the existing local synthetic native path')
    os.umask(0o077)
    root = args.root.resolve()
    root.mkdir(mode=0o700)
    settings = isolated_settings(root / 'runtime')
    settings.environment = 'production'
    settings.runner.codex_binary = 'codex'
    settings.runner.antigravity_binary = 'agy'
    settings.raw_config.setdefault('network_profiles', {})['public-research'] = {'mode': 'native'}
    save_delivery_config(BuiltinDeliveryConfig(enabled=False, auto_dispatch=False,
        recipient_groups={'synthetic-admin': ['admin@example.test'], 'synthetic-sample': ['sample@example.test']}),
        settings.paths.delivery_config_file)
    app = ApplicationService(settings)
    receipts = root / 'receipts'
    receipts.mkdir(mode=0o700)
    fixture = Path(__file__).with_name('large_mcp_fixture.py').resolve()
    server_process = None
    server_log = None
    build = production.build_production_command
    if args.transport == 'http':
        server_log = (root / 'http-server.stderr').open('wb')
        server_process = subprocess.Popen([sys.executable, str(fixture), '--http', '--receipts', str(receipts)],
            stdout=subprocess.PIPE, stderr=server_log)
        port = json.loads(server_process.stdout.readline())['port']
        fixture_settings = {'url': f'http://127.0.0.1:{port}/mcp'}
    else:
        fixture_settings = {'command': sys.executable, 'args': [str(fixture), '--receipts', str(receipts)]}

    def fixture_builder(provider, *values, **options):
        context = values[-1]
        context.timeout_seconds = 300
        context.reasoning_effort = 'low'
        argv, stdin = build(provider, *values, **options)
        if provider == 'codex_exec' and context.invocation_stage == 'research':
            fixture_config = {**fixture_settings, 'enabled': True, 'required': True,
                              'startup_timeout_sec': 20, 'default_tools_approval_mode': 'auto'}
            argv[-1:-1] = ['-c', 'mcp_servers.researchops_large_probe=' + production._toml(fixture_config)]
        return argv, stdin

    instructions = '''This is a synthetic transport and recipient-routing test. Never access real business tasks or send email.
In Research, use ONLY researchops_large_probe/fetch_page to fetch pages 1, 2 and 3 exactly once each. Do not use any other MCP or web tools. Each response is deliberately large: ignore the padding but preserve its actual receipt and numeric fact. Return three small records containing page, receipt and fact, plus status=success, a brief summary, full coverage, warnings and artifacts=[]. Do not wrap this result under a research key.
In Compose, use the provided reportable_records. If at least one record exists select recipient_group_name=synthetic-admin, otherwise select recipient_group_name=synthetic-sample. Include every reportable record, its actual receipt and fact in both mail bodies. Do not do new research. Follow all supplied date and output schema requirements.
'''
    if args.provider == 'antigravity_exec':
        instructions = '''This is an isolated synthetic local code and recipient-routing test. Never use MCP, web, external connectors, business tasks or SMTP.
In Research, use your native shell/code tool to calculate 125+25, write exactly 150 to project_dir/proof.txt and read it back. Do not return the proof file as an attachment. On actual success return status=success, summary mentioning the observed result 150, records=[], complete coverage, warnings=[], artifacts=[]. Do not wrap this result under a research key.
In Compose, follow task.md: when reportable_records is empty select recipient_group_name=synthetic-sample; otherwise synthetic-admin. Write a complete email describing the actual empty synthetic result with the supplied Seoul date and no invented records. Do not perform new research or use network tools.
'''
    task_id = 'synthetic-large-' + args.transport
    app.tasks.create_production_task(task_id=task_id, name='Synthetic transport verification',
        instructions=instructions, runner_type=args.provider, recipient_routing_mode='catalog_name',
        schedule_enabled=False)
    run = app.runs.enqueue_run(task_id, force_dry_run=True)
    report = {'provider': args.provider, 'transport': args.transport,
              'run_id': run.run_id, 'business_run_retries': 0, 'actual_smtp_calls': 0}
    try:
        with patch.object(production, 'build_production_command', side_effect=fixture_builder), patch('smtplib.SMTP') as smtp:
            result = app.runs.execute_run(run.run_id)
        smtp.assert_not_called()
        archive = settings.paths.run_archive_dir / task_id / run.run_id
        validation = json.loads((archive / 'validation-report.json').read_bytes())
        calls = [json.loads(line) for path in receipts.glob('*.jsonl') for line in path.read_text().splitlines()]
        tool_calls = [item for item in calls if item.get('event') == 'tool_call']
        research = app.run_repo.get_research_result(run.run_id)
        composed = app.run_repo.get_composition_result(run.run_id)
        captured_bytes = (archive / 'logs/research.stdout').stat().st_size
        report.update(status=result.status, error=result.error_message, archive=str(archive),
            captured_research_bytes=captured_bytes, receipt_count=len(tool_calls),
            selected_id=composed.recipient_group_id if composed else None,
            executions=validation.get('executions', []))
        if args.transport != 'none':
            expected = {entry['receipt'] for entry in tool_calls}
            observed = {entry.get('receipt') for entry in research.records} if research else set()
            report['receipts_correlated'] = (len(tool_calls) == 3 and
                sorted(entry['page'] for entry in tool_calls) == [1, 2, 3] and
                len(expected) == 3 and expected == observed and research is not None and
                len(research.records) == 3 and all(entry.get('fact') == entry.get('page', -1) * 7
                    and entry.get('receipt') in expected for entry in research.records))
            report['large_trace_verified'] = captured_bytes > 2_000_000
            expected_group = 'synthetic-admin'
        else:
            proof = settings.paths.task_workspaces_dir / task_id / 'project/proof.txt'
            report['local_proof_verified'] = proof.is_file() and proof.read_text().strip() == '150'
            expected_group = 'synthetic-sample'
        executions = validation.get('executions', [])
        observed_mcp = [tool for execution in executions for tool in execution.get('isolation', {}).get('mcp_tools', [])]
        native_events = [json.loads((archive / f'logs/{stage}.events.json').read_text())
                         for stage in ('research', 'compose') if (archive / f'logs/{stage}.events.json').exists()]
        observed_names = [tool['name'] for events in native_events for event in events for tool in event.get('tools', [])]
        if args.transport == 'none':
            report['scope_verified'] = not observed_mcp and all(name in
                {'run_command', 'view_file', 'write_to_file'} for name in observed_names)
        else:
            report['scope_verified'] = len(observed_mcp) == 3 and all(
                tool.get('server') == 'researchops_large_probe' and tool.get('tool') == 'fetch_page'
                and tool.get('success') for tool in observed_mcp) and all(name == 'mcp_tool_call' for name in observed_names)
        report['ok'] = (result.status == 'succeeded' and composed is not None and
            composed.recipient_group_id == expected_group and report['scope_verified'] and
            (report.get('local_proof_verified') if args.transport == 'none' else
             report.get('receipts_correlated') and report.get('large_trace_verified')))
    finally:
        if server_process is not None:
            server_process.terminate()
            server_process.wait(timeout=10)
            server_process.stdout.close()
            server_log.close()
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != 'executions'}, ensure_ascii=False))
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
