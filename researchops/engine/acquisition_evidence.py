"""Preserve application-owned file acquisition evidence across Compose retries."""
import hashlib
from pathlib import PurePosixPath
import re

from researchops.errors import HardGateError
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import read_safe_bytes


def retain_acquisition_evidence(archive, parent_root, composition_input):
    """Copy verified originals; the receipt retains its original producing Run.

    The composition input has already been checked against its database hash.
    Its derivative references bind the particular acquired source bytes here.
    No credentials, network call, new receipt or new model interpretation occurs.
    """
    references = {}
    report = composition_input.get('artifact_report') or {}
    binding = report.get('acquisition_evidence')
    for item in report.get('entries', []):
        for reference in item.get('derived_from', []):
            key, digest = reference['acquisition_id'], reference['source_sha256']
            if key in references and references[key] != digest:
                raise HardGateError('Conflicting inherited acquisition references')
            references[key] = digest
    path = parent_root / 'research-acquisition/ledger.json'
    if not path.exists() and not path.is_symlink():
        if references or binding is not None:
            raise HardGateError('Inherited acquisition evidence is missing')
        return
    raw = read_safe_bytes(path, parent_root, 2_000_000)
    if binding is None:
        if references:
            raise HardGateError('Inherited acquisition evidence is not bound to its composition input')
        # Older inputs may carry an empty receipt written before the helper was
        # used. It is not authenticated derivative evidence and need not travel.
        return
    if (not isinstance(binding, dict) or set(binding) != {'run_id', 'attempt', 'ledger_sha256'} or
            not isinstance(binding['run_id'], str) or type(binding['attempt']) is not int or
            binding['attempt'] < 1 or hashlib.sha256(raw).hexdigest() != binding['ledger_sha256']):
        raise HardGateError('Inherited acquisition ledger changed')
    index_raw = read_safe_bytes(parent_root / 'artifact-manifest.json', parent_root, 2_000_000)
    index = strict_json_loads(index_raw, max_bytes=2_000_000)
    if (not isinstance(index, dict) or not isinstance(index.get('artifacts'), list) or
            index.get('task_id') != composition_input['task_id'] or
            index.get('run_id') != composition_input['run_id']):
        raise HardGateError('Inherited acquisition archive index is invalid')
    indexed = {}
    for item in index['artifacts']:
        name = item.get('relative_path') if isinstance(item, dict) else None
        if not isinstance(name, str) or name in indexed:
            raise HardGateError('Inherited acquisition archive index is invalid')
        indexed[name] = item

    def check_index(name, payload):
        item = indexed.get(name)
        if (item is None or item.get('sha256') != hashlib.sha256(payload).hexdigest() or
                item.get('size_bytes') != len(payload)):
            raise HardGateError('Inherited acquisition archive index differs from its file')

    check_index('research-acquisition/ledger.json', raw)
    ledger = strict_json_loads(raw, max_bytes=2_000_000)
    if (not isinstance(ledger, dict) or ledger.get('schema_version') != 1 or
            ledger.get('task_id') != composition_input['task_id'] or
            ledger.get('run_id') != binding['run_id'] or ledger.get('attempt') != binding['attempt'] or
            ledger.get('cleanup_verified') is not True or ledger.get('failure_reason') is not None or
            not isinstance(ledger.get('records'), list) or len(ledger['records']) > 256):
        raise HardGateError('Inherited acquisition evidence is invalid')
    originals, available, seen = [], {}, set()
    total = 0
    for record in ledger['records']:
        acquisition_id = record.get('acquisition_id') if isinstance(record, dict) else None
        if (not isinstance(acquisition_id, str) or
                not re.fullmatch(r'acq-[a-f0-9]{32}', acquisition_id) or acquisition_id in seen):
            raise HardGateError('Inherited acquisition receipt identity is invalid')
        seen.add(acquisition_id)
        if record.get('status') != 'available':
            if record.get('status') != 'failed' or record.get('original_path') is not None:
                raise HardGateError('Inherited acquisition receipt state is invalid')
            continue
        name = record.get('original_path')
        if (not isinstance(name, str) or not re.fullmatch(
                r'originals/' + re.escape(acquisition_id) + r'\.(pdf|png|jpg|gif)', name) or
                PurePosixPath(name).as_posix() != name):
            raise HardGateError('Inherited acquisition original path is invalid')
        relative = 'research-acquisition/' + name
        original = read_safe_bytes(parent_root / relative, parent_root, 20_000_000)
        check_index(relative, original)
        digest = hashlib.sha256(original).hexdigest()
        total += len(original)
        if (total > 20_000_000 or digest != record.get('sha256') or
                len(original) != record.get('size_bytes')):
            raise HardGateError('Inherited acquisition original changed')
        available[acquisition_id] = digest
        originals.append((relative, original))
    if any(available.get(key) != digest for key, digest in references.items()):
        raise HardGateError('Inherited derivative differs from its acquired source')
    archive.write('research-acquisition/ledger.json', raw)
    for name, original in originals:
        archive.write(name, original)
