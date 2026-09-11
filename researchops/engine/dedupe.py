"""Opt-in conservative exact content deduplication."""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from researchops.domain.models import TaskDefinition
from researchops.storage.repositories import StateRepository


def derive_entity_key(record: Dict[str, Any], key_fields: List[str]) -> Optional[str]:
    """Derive deterministic entity key string from record's key fields."""
    parts = []
    for kf in key_fields:
        val = record.get(kf)
        if val is None or str(val).strip() == "":
            return None
        parts.append(val)
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":")) if parts else None


def derive_content_fingerprint(record: Dict[str, Any], content_fields: List[str]) -> Optional[str]:
    """Derive deterministic canonical content fingerprint (SHA-256) from content fields."""
    if not content_fields or any(cf not in record or record[cf] is None for cf in content_fields):
        return None
    content_map = {}
    for cf in sorted(content_fields):
        if cf in record:
            content_map[cf] = record[cf]
    canonical_bytes = json.dumps(content_map, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


class ContentDeduplicator:
    def __init__(self, state_repo: StateRepository):
        self.state_repo = state_repo

    def process_records(
        self,
        task_def: TaskDefinition,
        records: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """Filter records by opt-in conservative exact dedupe against verified-sent history.

        Returns:
            (reportable_records, excluded_records, warnings)
        """
        state_conf = task_def.state or {}
        dedupe_conf = state_conf.get("dedupe") or {}
        enabled = dedupe_conf.get("enabled", False)

        if not enabled:
            # Dedupe is disabled by default; all records are reportable
            return list(records), [], []

        key_fields = dedupe_conf.get("key_fields", [])
        content_fields = dedupe_conf.get("content_fields", [])

        if not key_fields or not content_fields:
            # Misconfigured dedupe config
            return list(records), [], ["Dedupe enabled but missing key_fields or content_fields; skipped dedupe."]

        reportable: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for rec in records:
            entity_key = derive_entity_key(rec, key_fields)
            if not entity_key:
                # Ambiguous or missing key -> always include with warning
                reportable.append(dict(rec))
                warnings.append(
                    f"Record '{rec.get('record_id')}' missing key fields {key_fields}; preserved in composition."
                )
                continue

            fingerprint = derive_content_fingerprint(rec, content_fields)
            if fingerprint is None:
                reportable.append(dict(rec))
                warnings.append(f"Record '{rec.get('record_id')}' has incomplete comparison fields; preserved.")
                continue
            rec_copy = dict(rec)
            rec_copy["_entity_key"] = entity_key
            rec_copy["_content_fingerprint"] = fingerprint

            is_exact_match = self.state_repo.is_reported_item_unchanged(
                task_id=task_def.id,
                entity_key=entity_key,
                content_fingerprint=fingerprint
            )

            if is_exact_match:
                rec_copy["_dedupe_status"] = "already_reported_exact"
                excluded.append(rec_copy)
            else:
                reportable.append(dict(rec))

        return reportable, excluded, warnings
