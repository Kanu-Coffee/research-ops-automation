"""Opt-in minimal Agy probe after an unsolicited schema-discovery failure."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import yaml
from researchops.domain.models import CompositionInput, TaskDefinition
from researchops.engine.message_validator import MessageValidator
from researchops.package.production_template import build_production_package
from researchops.runners.base import RunnerInvocationContext
from researchops.runners.production import TrustedProductionRunner


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--live', action='store_true', required=True)
    p.add_argument('--root', type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    root.mkdir(mode=0o700)
    project = root / 'project'
    project.mkdir()
    now = datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
    input_snapshot = CompositionInput(task_id='minimal-agy', run_id='synthetic-minimal-agy',
        task_version_hash='a' * 64, composition_revision=1, schema_version=3,
        recipient_routing_mode='catalog_name', recipient_groups=[
            {'recipient_group_id': 'synthetic-admin', 'display_name': 'Synthetic Admin'},
            {'recipient_group_id': 'synthetic-sample', 'display_name': 'Synthetic Empty'}],
        allowed_recipient_group_ids=['synthetic-admin', 'synthetic-sample'],
        reportable_records=[], inline_artifacts=[], attachments=[],
        run={'scheduled_for': now.isoformat(), 'timezone': 'Asia/Seoul', 'local_date': day,
             'local_date_display': day.replace('-', '.')},
        result={'status': 'no_updates', 'summary': 'Synthetic local calculation confirmed 150.', 'warnings': []},
        coverage={'complete': True, 'expected_target_count': 0, 'completed_target_count': 0, 'issues': []})
    runner = TrustedProductionRunner('antigravity_exec', 'agy')
    report = {'actual_smtp_calls': 0, 'business_run_retries': 0, 'calls': [], 'ok': False}
    boundary = ('All required input and output fields are written below. Do not discover any schema or other instructions. '
        'Never read parent directories, application files, control logs, transcripts, home, credentials or MCP metadata. '
        'Never use MCP or web tools. Use only the explicit local project command in Research; Compose needs no tools. ')
    try:
        for stage in ('research', 'compose'):
            dirs = {name: root / stage / name for name in ('input', 'tmp', 'output', 'logs')}
            for directory in dirs.values():
                directory.mkdir(parents=True)
            if stage == 'research':
                expected = {'status': 'success', 'summary': 'Synthetic calculation 125 + 25 = 150 confirmed.',
                    'records': [], 'coverage': input_snapshot.coverage, 'warnings': [], 'artifacts': []}
                instructions = (boundary + f'Run one native run_command in {project}: '
                    'python3 -c "from pathlib import Path; n=125+25; Path(\'proof.txt\').write_text(str(n)); print(Path(\'proof.txt\').read_text())". '
                    'After the command returns 150, immediately return response_json encoding exactly this complete research object: '
                    + json.dumps(expected) + '. No research wrapper or additional fields.')
            else:
                (dirs['input'] / 'composition-input.json').write_text(json.dumps(input_snapshot.to_dict()))
                result = {'recipient_group_name': 'Synthetic Empty', 'recipient_group_reason': 'Zero reportable records.',
                    'subject': f'Synthetic verification {day}', 'html_path': 'email.html', 'text_path': 'email.txt',
                    'included_record_ids': []}
                expected = {'composition_result': result,
                    'html': f'<html><head><title>Synthetic</title></head><body data-local-date="{day}"><p>{day.replace("-", ".")} Synthetic calculation confirmed 150. No reportable records.</p></body></html>',
                    'text': f'{day.replace("-", ".")} Synthetic calculation confirmed 150. No reportable records.'}
                instructions = (boundary + 'Routing rule: if reportable_records is empty return recipient_group_name=\"Synthetic Empty\"; otherwise \"Synthetic Admin\". '
                    'The input is empty. Immediately return response_json encoding this complete Compose response without tools: '
                    + json.dumps(expected))
            (dirs['input'] / 'task.md').write_text(instructions)
            ctx = RunnerInvocationContext('minimal-agy', 'synthetic-minimal-agy', 1, stage,
                reasoning_effort='low', timeout_seconds=180, network_profile='public-research' if stage == 'research' else 'none',
                trace_log_dir=dirs['logs'], local_date=day, local_date_display=day.replace('-', '.'), scheduled_for=now.isoformat())
            execution = getattr(runner, 'execute_' + stage)(dirs['input'], dirs['tmp'], dirs['output'], project, ctx)
            report['calls'].append({'stage': stage, 'success': execution.success, 'error': execution.error_message,
                                   'cleanup_verified': execution.cleanup_verified, 'isolation': execution.isolation})
            if not execution.success or not execution.cleanup_verified:
                break
        if len(report['calls']) == 2 and all(item['success'] for item in report['calls']):
            package = build_production_package(task_id='minimal-agy', name='Synthetic', instructions='Synthetic',
                runner_type='antigravity_exec', recipient_group_id='', recipient_routing_mode='catalog_name',
                cron='0 9 * * *', schedule_enabled=False)
            task = TaskDefinition(**yaml.safe_load(package['task.yaml']))
            result, _, _, hashes = MessageValidator(Path(__file__).resolve().parents[1] / 'schemas').validate_composition(
                root / 'compose/output', input_snapshot, task, json.loads(package['composition.schema.json']))
            report['selected_id'] = result.recipient_group_id
            report['recipient_resolution'] = hashes['recipient_resolution']
            report['ok'] = result.recipient_group_id == 'synthetic-sample' and (project / 'proof.txt').read_text() == '150'
    finally:
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in report.items() if key != 'calls'}, ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
