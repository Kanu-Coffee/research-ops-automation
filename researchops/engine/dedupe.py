"""Opt-in conservative exact content deduplication."""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from researchops.domain.models import TaskDefinition
from researchops.storage.repositories import StateRepository
from researchops.errors import ValidationError


def validate_entity_aliases(config):
    """Validate explicit, operator-authored complete-key equivalence groups."""
    groups = config.get("entity_aliases", [])
    keys = config.get("key_fields", [])
    if not isinstance(groups, list) or len(groups) > 128:
        raise ValidationError("dedupe.entity_aliases must contain at most 128 groups")
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"identities", "evidence"}:
            raise ValidationError("Each dedupe alias group requires identities and evidence")
        identities = group["identities"]
        if not isinstance(group["evidence"], str) or not group["evidence"].strip() or len(group["evidence"]) > 2048:
            raise ValidationError("Dedupe identity aliases require bounded evidence")
        if not isinstance(identities, list) or not 2 <= len(identities) <= 8 or not keys:
            raise ValidationError("Dedupe alias groups require 2-8 complete identities")
        for identity in identities:
            if not isinstance(identity, dict) or set(identity) != set(keys) or any(
                not isinstance(v, str) or not v.strip() or len(v) > 256 for v in identity.values()
            ):
                raise ValidationError("Dedupe aliases must specify every key field as a nonempty string")
            key = derive_entity_key(identity, keys)
            if key in seen:
                raise ValidationError("Dedupe alias identities must not repeat or overlap groups")
            seen.add(key)


def equivalent_records(record, config):
    """Expand only declared exact identities; never infer equivalence from names."""
    key = derive_entity_key(record, config["key_fields"])
    for group in config.get("entity_aliases", []):
        if any(derive_entity_key(identity, config["key_fields"]) == key for identity in group["identities"]):
            # Preserve all non-identity content, including changed benefits.
            return [dict(record, **identity) for identity in group["identities"]]
    return [record]


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

        validate_entity_aliases(dedupe_conf)

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

            # Lookup both old and new identifiers without rewriting sent history.
            # The key and fingerprint must match the SAME equivalent record.
            matched = next((candidate for candidate in equivalent_records(rec, dedupe_conf)
                if self.state_repo.is_reported_item_unchanged(
                    task_id=task_def.id,
                    entity_key=derive_entity_key(candidate, key_fields),
                    content_fingerprint=derive_content_fingerprint(candidate, content_fields))), None)
            is_exact_match = matched is not None

            if is_exact_match:
                rec_copy["_dedupe_status"] = "already_reported_exact"
                if derive_entity_key(matched, key_fields) != entity_key:
                    rec_copy["_matched_entity_key"] = derive_entity_key(matched, key_fields)
                    rec_copy["_dedupe_match_basis"] = "configured_identity_alias"
                excluded.append(rec_copy)
            else:
                reportable.append(dict(rec))

        return reportable, excluded, warnings
