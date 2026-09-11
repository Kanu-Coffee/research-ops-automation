"""A bounded, address-free projection of locally verified delivery evidence.

Worker ledgers and prose never authenticate a receipt. Source messages must
match the stored composition, archived bytes, local SMTP attempt and receipt.
Missing/retired evidence stays unresolved and never becomes permission to resend.
"""

import hashlib
import json
import re

from researchops.engine.archive import canonical_json
from researchops.errors import HardGateError, ResearchOpsError
from researchops.package.loader import compute_package_hash
from researchops.strict_json import strict_json_loads
from researchops.workspace.security import read_safe_bytes

MAX_HISTORY_ENTRIES = 500
MAX_HISTORY_BYTES = 256 * 1024


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _version_series(version):
    context = (version.definition.state or {}).get("research_context")
    if context and context.get("enabled"):
        return context.get("series_id")
    # Narrow bootstrap: the operator's sealed JSON configuration, never a
    # model's summary/ledger or a substring anywhere in arbitrary prose.
    text = version.package_files.get("task.md", "")
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, flags=re.DOTALL)
    candidates = []
    for block in blocks:
        try:
            config = strict_json_loads(block)
        except ValueError:
            continue
        if (isinstance(config, dict) and isinstance(config.get("active_issuers"), list)
                and "test_reference_date" in config and "test_series_id" in config):
            candidates.append(config["test_series_id"])
    return candidates[0] if len(candidates) == 1 else None


def _record_bindings(records, included_ids):
    by_id = {r["record_id"]: r for r in records}
    if len(by_id) != len(records) or set(included_ids) != set(by_id) or len(included_ids) != len(by_id):
        raise HardGateError("Delivery history record set differs from immutable composition")
    result = []
    for rid in sorted(included_ids):
        record = by_id[rid]
        # Only exact stored identities; no name matching or whole business prose.
        item = {key: record[key] for key in ("record_id", "product_key", "issuer", "product_code",
            "product_lineage_id", "report_kind", "correction_id") if key in record}
        if any(value is not None and (not isinstance(value, str) or len(value) > 512
                or "@" in value or any(ord(c) < 32 for c in value)) for value in item.values()):
            raise HardGateError("Unsafe delivery history record identity")
        result.append(item)
    return result


def _verified_retry_receipts(delivery_repo, handoff, current_receipt, *, verify_logical_outcome=True):
    """Bind retry receipts to one immutable message without replacing old proof."""
    conn = delivery_repo.db.get_connection()
    try:
        rows = conn.execute("""SELECT a.job_id,a.parent_job_id,a.attempt_number,a.status,a.phase,
            a.message_id,a.mime_sha256,
            a.mime_bytes=original.mime_bytes AND a.envelope_json=original.envelope_json AS same_message
            FROM smtp_attempts a JOIN smtp_attempts original
              ON original.handoff_id=a.handoff_id AND original.attempt_number=1
            WHERE a.handoff_id=? ORDER BY a.attempt_number LIMIT ?""",
            (handoff.handoff_id, MAX_HISTORY_ENTRIES + 1)).fetchall()
        if len(rows) > MAX_HISTORY_ENTRIES:
            raise HardGateError("Delivery history SMTP attempt limit exceeded")
        chain, previous = [], None
        for number, row in enumerate(rows, 1):
            if (row["attempt_number"] != number or row["parent_job_id"] != previous
                    or not row["same_message"] or row["message_id"] != rows[0]["message_id"]
                    or row["mime_sha256"] != rows[0]["mime_sha256"]):
                raise HardGateError("Delivery history SMTP retry identity mismatch")
            receipt_row = conn.execute("SELECT receipt_json,receipt_sha256 FROM delivery_receipts "
                "WHERE external_receipt_id=?", ("smtp-" + row["job_id"],)).fetchone()
            if not receipt_row or row["phase"] != "finished":
                raise HardGateError("Delivery history SMTP retry proof unavailable")
            raw = receipt_row["receipt_json"]
            receipt = strict_json_loads(raw)
            if (not isinstance(receipt, dict)
                    or _digest(raw.encode("utf-8")) != receipt_row["receipt_sha256"]
                    or receipt.get("external_receipt_id") != "smtp-" + row["job_id"]
                    or receipt.get("handoff_id") != handoff.handoff_id
                    or receipt.get("idempotency_key") != handoff.idempotency_key
                    or receipt.get("delivery_request_sha256") != handoff.delivery_request_sha256
                    or receipt.get("status") != row["status"]
                    or receipt.get("external_message_ref") != row["message_id"]
                    or receipt.get("proof") != {"type": "local_smtp_attempt", "issuer": "researchops-smtp",
                                                "value": row["job_id"]}):
                raise HardGateError("Delivery history SMTP retry receipt mismatch")
            status = {"failed": "failed_not_sent", "uncertain": "uncertain",
                      "smtp_accepted": "smtp_accepted"}.get(row["status"])
            if status is None or any(item["status"] == "smtp_accepted" for item in chain):
                raise HardGateError("Delivery history SMTP retry state mismatch")
            chain.append({"receipt_id": receipt["external_receipt_id"],
                "receipt_sha256": receipt_row["receipt_sha256"], "status": status,
                "attempt_number": number, "occurred_at": receipt["occurred_at"]})
            if receipt["external_receipt_id"] == current_receipt.external_receipt_id:
                if (verify_logical_outcome and status == "failed_not_sent"
                        and any(item["status"] == "uncertain" for item in chain)):
                    raise HardGateError("Delivery history cannot discard an earlier uncertain attempt")
                return chain
            previous = row["job_id"]
    finally:
        conn.close()
    raise HardGateError("Delivery history current receipt is outside its SMTP attempt chain")


def _pending_retry(delivery_repo, handoff):
    """An active retry reserves its exact records even before a new receipt exists."""
    conn = delivery_repo.db.get_connection()
    try:
        rows = conn.execute("""SELECT a.job_id,a.status,a.attempt_number,a.parent_job_id,a.next_attempt_at,
            parent.attempt_number AS parent_number,
            a.mime_bytes=original.mime_bytes AND a.envelope_json=original.envelope_json
              AND a.message_id=original.message_id AND a.mime_sha256=original.mime_sha256 AS same_message
            FROM smtp_attempts a LEFT JOIN smtp_attempts original
              ON original.handoff_id=a.handoff_id AND original.attempt_number=1
            LEFT JOIN smtp_attempts parent ON parent.job_id=a.parent_job_id AND parent.handoff_id=a.handoff_id
            WHERE a.handoff_id=? AND a.status IN ('queued','sending') LIMIT 2""",
            (handoff.handoff_id,)).fetchall()
    finally:
        conn.close()
    if not rows or len(rows) == 1 and rows[0]["attempt_number"] == 1:
        return None
    if (len(rows) != 1 or not rows[0]["same_message"] or rows[0]["parent_number"] is None
            or rows[0]["attempt_number"] != rows[0]["parent_number"] + 1):
        raise HardGateError("Delivery history pending SMTP retry binding mismatch")
    row = rows[0]
    previous = delivery_repo.get_receipt("smtp-" + row["parent_job_id"])
    if previous is None:
        raise HardGateError("Delivery history pending retry has no prior receipt")
    chain = _verified_retry_receipts(delivery_repo, handoff, previous, verify_logical_outcome=False)
    if chain[-1]["status"] == "smtp_accepted":
        raise HardGateError("Delivery history cannot retry an accepted message")
    return {"status": "pending", "pending_attempt": {
        "attempt_number": row["attempt_number"], "status": row["status"],
        "next_attempt_at": row["next_attempt_at"]}, "smtp_attempt_receipts": chain}


def _verified_message(settings, run_repo, delivery_repo, version, handoff, series_id):
    run = run_repo.get_run(handoff.run_id)
    revision = handoff.message_revision
    stored = run_repo.get_composition_input_record(handoff.run_id, revision)
    result = run_repo.get_composition_result(handoff.run_id, revision)
    if (not run or not stored or not result or run.task_id != handoff.task_id
            or run.task_version_hash != handoff.task_version_hash
            or stored["task_id"] != handoff.task_id or stored["task_version_hash"] != handoff.task_version_hash):
        raise HardGateError("Delivery history has no matching immutable composition")
    value = stored["input"]
    if (value["task_id"] != handoff.task_id or value["run_id"] != run.run_id
            or value["task_version_hash"] != handoff.task_version_hash
            or value["composition_revision"] != revision
            or _digest(canonical_json(value)) != stored["input_sha256"]):
        raise HardGateError("Delivery history composition binding mismatch")
    archive = settings.paths.run_archive_dir / handoff.task_id / handoff.run_id
    def read(name, limit=20_000_000):
        return read_safe_bytes(archive / name, archive, limit)
    if read("composition-input.json") != canonical_json(value):
        raise HardGateError("Delivery history archive differs from the stored input")
    sealed_files = {name: content.encode("utf-8") for name, content in version.package_files.items()}
    if compute_package_hash(sealed_files) != handoff.task_version_hash:
        raise HardGateError("Delivery history task version hash mismatch")
    if read("task-snapshot/task.md") != sealed_files["task.md"]:
        raise HardGateError("Delivery history sealed task archive mismatch")
    request_raw = read("delivery-request.json")
    request = strict_json_loads(request_raw, max_bytes=20_000_000)
    if _digest(request_raw) != handoff.delivery_request_sha256 or request != handoff.delivery_request:
        raise HardGateError("Delivery history handoff hash mismatch")
    for key, expected in (("task_id", handoff.task_id), ("run_id", run.run_id),
            ("task_version_hash", handoff.task_version_hash), ("message_revision", revision),
            ("handoff_id", handoff.handoff_id), ("idempotency_key", handoff.idempotency_key)):
        if request.get(key) != expected:
            raise HardGateError("Delivery history handoff identity mismatch")
    if request.get("subject") != result.subject or request.get("recipient_group_id") != result.recipient_group_id:
        raise HardGateError("Delivery history result differs from the handoff")
    body_hashes = {}
    for kind, path in (("html", result.html_path), ("text", result.text_path)):
        body = request["body"][kind]
        raw = read(path)
        if body["path"] != path or body["sha256"] != _digest(raw) or body["size_bytes"] != len(raw):
            raise HardGateError("Delivery history body hash mismatch")
        body_hashes[kind] = body["sha256"]
    from researchops.delivery.artifact_integrity import require_stored_composition
    require_stored_composition(delivery_repo.db, version.definition, run.run_id, handoff.task_version_hash,
        revision, result.recipient_group_id, schemas_dir=settings.paths.schemas_dir,
        archive_root=archive, file_root=archive, request=request)
    entry = {"task_id": handoff.task_id, "series_id": series_id, "run_id": run.run_id,
        "parent_run_id": run.parent_run_id, "task_version_hash": handoff.task_version_hash,
        "composition_revision": revision, "handoff_id": handoff.handoff_id,
        "delivery_request_sha256": handoff.delivery_request_sha256, "body_sha256": body_hashes,
        "record_bindings": _record_bindings(value["reportable_records"], result.included_record_ids),
        "status": "unresolved", "receipt_id": None, "receipt_sha256": None,
        "bootstrap": "research_context" not in value}
    pending = _pending_retry(delivery_repo, handoff)
    if pending is not None:
        entry.update(pending)
        return entry
    receipt = delivery_repo.get_receipt(handoff.external_receipt_id) if handoff.external_receipt_id else None
    if receipt is None:
        return entry
    if (handoff.receipt_trust_status != "verified_local_smtp"
            or receipt.proof.get("type") != "local_smtp_attempt"
            or receipt.proof.get("issuer") != "researchops-smtp"
            or receipt.handoff_id != handoff.handoff_id or receipt.idempotency_key != handoff.idempotency_key
            or receipt.delivery_request_sha256 != handoff.delivery_request_sha256
            or receipt.status != handoff.external_delivery_status or receipt.status != handoff.status):
        raise HardGateError("Delivery history receipt binding mismatch")
    conn = delivery_repo.db.get_connection()
    try:
        receipt_row = conn.execute("SELECT receipt_json,receipt_sha256 FROM delivery_receipts WHERE external_receipt_id=?",
            (receipt.external_receipt_id,)).fetchone()
        attempt = conn.execute("SELECT handoff_id,status,phase FROM smtp_attempts WHERE job_id=?",
            (receipt.proof.get("value"),)).fetchone()
    finally:
        conn.close()
    if (not receipt_row or _digest(receipt_row["receipt_json"].encode("utf-8")) != receipt_row["receipt_sha256"]
            or receipt_row["receipt_sha256"] != handoff.receipt_sha256
            or strict_json_loads(receipt_row["receipt_json"]) != receipt.to_dict()
            or not attempt or attempt["handoff_id"] != handoff.handoff_id
            or attempt["status"] != receipt.status or attempt["phase"] != "finished"):
        raise HardGateError("Delivery history local SMTP proof mismatch")
    states = {"smtp_accepted": "smtp_accepted", "failed": "failed_not_sent", "uncertain": "uncertain"}
    if receipt.status not in states:
        raise HardGateError("Delivery history has unsupported delivery state")
    entry.update(status=states[receipt.status], receipt_id=receipt.external_receipt_id,
        receipt_sha256=handoff.receipt_sha256, occurred_at=receipt.occurred_at)
    retry_receipts = _verified_retry_receipts(delivery_repo, handoff, receipt)
    if len(retry_receipts) > 1:
        entry["smtp_attempt_receipts"] = retry_receipts
    return entry


def build_delivery_history(settings, task_repo, run_repo, delivery_repo, task, run, context):
    result = {"schema_version": 1, "task_id": task.id, "series_id": context["series_id"],
        "origin_run_id": run.run_id, "parent_run_id": run.parent_run_id,
        "entries": [], "unresolved": [], "history_complete": True,
        "limits": {"entries": MAX_HISTORY_ENTRIES, "bytes": MAX_HISTORY_BYTES}}
    # Query identifiers only, never envelope addresses, MIME, credentials or server replies.
    conn = delivery_repo.db.get_connection()
    try:
        rows = conn.execute("SELECT handoff_id FROM delivery_handoffs WHERE task_id=? AND run_id!=? "
            "AND message_type!='system_alert' ORDER BY created_at DESC,handoff_id DESC LIMIT ?",
            (task.id, run.run_id, MAX_HISTORY_ENTRIES + 1)).fetchall()
    finally:
        conn.close()
    if len(rows) > MAX_HISTORY_ENTRIES:
        result["history_complete"] = False
        result["unresolved"].append({"reason": "history_entry_limit"})
    seen = set()
    for row in rows[:MAX_HISTORY_ENTRIES]:
        try:
            handoff = delivery_repo.get_handoff(row["handoff_id"])
            version = task_repo.get_version(handoff.task_version_hash) if handoff else None
            series = _version_series(version) if version else None
        except (ResearchOpsError, ValueError, KeyError, TypeError):
            result["history_complete"] = False
            result["unresolved"].append({"handoff_id": row["handoff_id"], "reason": "evidence_binding_failed"})
            continue
        if handoff is None:
            result["history_complete"] = False
            result["unresolved"].append({"handoff_id": row["handoff_id"], "reason": "evidence_binding_failed"})
            continue
        if series is None:
            result["history_complete"] = False
            result["unresolved"].append({"run_id": handoff.run_id,
                "composition_revision": handoff.message_revision, "reason": "series_binding_unavailable"})
            continue
        if series != context["series_id"]:
            continue
        try:
            entry = _verified_message(settings, run_repo, delivery_repo, version, handoff, context["series_id"])
            key = entry["receipt_id"] or entry["handoff_id"]
            if key in seen:
                continue
            seen.add(key)
            result["entries"].append(entry)
            if len(canonical_json(result)) > MAX_HISTORY_BYTES - 1024:
                result["entries"].pop()
                result["history_complete"] = False
                result["unresolved"].append({"reason": "history_byte_limit"})
                break
        except (ResearchOpsError, ValueError, KeyError, TypeError, OSError):
            # A corrupt/retired message cannot hide other valid receipts; the
            # unresolved run is explicit, without reflecting sensitive exceptions.
            result["history_complete"] = False
            result["unresolved"].append({"run_id": handoff.run_id,
                "composition_revision": handoff.message_revision, "reason": "evidence_binding_failed"})
    return _bounded_history(result)


def _bounded_history(result):
    """The complete serialized envelope is bounded, including all diagnostics."""
    if len(canonical_json(result)) <= MAX_HISTORY_BYTES:
        return result
    result["history_complete"] = False
    result["truncated"] = True
    if "ledger_reconciliation" in result:
        result["ledger_reconciliation"] = {"available": False, "reason": "history_byte_limit", "updates": []}
    while len(canonical_json(result)) > MAX_HISTORY_BYTES:
        if result["entries"]:
            result["entries"].pop()
        elif result["unresolved"]:
            result["unresolved"].pop()
        else:
            raise HardGateError("History envelope exceeds its configured byte limit")
    return result


def matching_delivery_entry(message, entry):
    """A prepared ledger message may transition only on an exact evidence match."""
    if (not isinstance(message, dict) or type(message.get("composition_revision")) is not int
            or entry.get("status") not in {"smtp_accepted", "failed_not_sent", "uncertain", "pending"}):
        return False
    if all(message.get(key) == entry.get(key) for key in
            ("task_id", "series_id", "run_id", "composition_revision", "body_sha256", "record_bindings")):
        return True
    # Explicit legacy aliases: a fully materialized record->product map and
    # both original body digests are required. No fuzzy field/name inference.
    if any(key in message for key in ("series_id", "body_sha256", "record_bindings")):
        return False
    bindings = entry.get("record_bindings", [])
    if any(not isinstance(x.get("product_key"), str) for x in bindings):
        return False
    mapping = {item["record_id"]: item["product_key"] for item in bindings}
    return (all(message.get(key) == entry.get(key) for key in ("task_id", "run_id", "composition_revision"))
        and message.get("test_series_id") == entry.get("series_id")
        and {"html": message.get("html_sha256"), "text": message.get("text_sha256")} == entry.get("body_sha256")
        and message.get("record_product_map") == mapping
        and isinstance(message.get("included_record_ids"), list)
        and sorted(message["included_record_ids"]) == sorted(mapping)
        and isinstance(message.get("product_keys"), list)
        and sorted(message["product_keys"]) == sorted(set(mapping.values())))


def attach_ledger_reconciliation(history, ledger_bytes):
    """Read-only suggested transitions; the worker keeps ownership of its ledger."""
    if ledger_bytes is None:
        history["ledger_reconciliation"] = {"available": False, "reason": "ledger_not_present", "updates": []}
        return _bounded_history(history)
    try:
        ledger = strict_json_loads(ledger_bytes, max_bytes=2_000_000)
        if (not isinstance(ledger, dict) or ledger.get("task_id") != history["task_id"]
                or ledger.get("test_series_id", ledger.get("series_id")) != history["series_id"]
                or not isinstance(ledger.get("messages"), dict)):
            raise ValueError
        updates, unresolved = [], []
        for key, message in ledger["messages"].items():
            if (not isinstance(key, str) or len(key) > 255 or "@" in key
                    or any(ord(c) < 32 for c in key) or not isinstance(message, dict)):
                raise ValueError
            candidates = [entry for entry in history["entries"] if entry["run_id"] == message.get("run_id")
                and entry["composition_revision"] == message.get("composition_revision")]
            matches = [entry for entry in candidates if matching_delivery_entry(message, entry)]
            if len(matches) == 1:
                entry = matches[0]
                if entry["status"] == "pending":
                    prior = next((item for item in entry.get("smtp_attempt_receipts", [])
                        if item["receipt_id"] == message.get("receipt_id")
                        and item["receipt_sha256"] == message.get("receipt_sha256")
                        and item["status"] == message.get("status")
                        and item["status"] in {"failed_not_sent", "uncertain"}), None)
                    without_receipt = (message.get("receipt_id") is None and message.get("receipt_sha256") is None
                        and message.get("status") in {"pending", "prepared", "unresolved"})
                    if prior is None and not without_receipt:
                        unresolved.append({"message_id": key, "reason": "previous_delivery_conflict"})
                        continue
                    if message.get("status") != "pending" or prior is not None:
                        update = {"message_id": key, "run_id": entry["run_id"],
                            "composition_revision": entry["composition_revision"], "status": "pending",
                            "receipt_id": None, "receipt_sha256": None,
                            "transition_reason": "smtp_retry_pending", "pending_attempt": entry["pending_attempt"]}
                        if prior is not None:
                            update["previous_receipt"] = prior
                        updates.append(update)
                    continue
                if (message.get("receipt_id") == entry["receipt_id"]
                        and message.get("receipt_sha256") == entry["receipt_sha256"]
                        and message.get("status") == entry["status"]):
                    continue
                # A verified later attempt may change this message's effective
                # outcome. Preserve the exact predecessor receipt in the update;
                # arbitrary receipt conflicts and prior acceptance stay immutable.
                previous_receipt = next((item for item in entry.get("smtp_attempt_receipts", [])[:-1]
                    if item["receipt_id"] == message.get("receipt_id")
                    and item["receipt_sha256"] == message.get("receipt_sha256")
                    and item["status"] == message.get("status")
                    and (item["status"] == "failed_not_sent" or
                         item["status"] == "uncertain" and entry["status"] == "smtp_accepted")), None)
                if (message.get("receipt_id") not in (None, entry["receipt_id"])
                        or message.get("receipt_sha256") not in (None, entry["receipt_sha256"])) or message.get("status") not in (
                        "pending", "prepared", "unresolved", entry["status"]):
                    if previous_receipt is None:
                        unresolved.append({"message_id": key, "reason": "previous_delivery_conflict"})
                        continue
                update = {"message_id": key, "run_id": entry["run_id"],
                    "composition_revision": entry["composition_revision"], "status": entry["status"],
                    "receipt_id": entry["receipt_id"], "receipt_sha256": entry["receipt_sha256"],
                    "occurred_at": entry["occurred_at"]}
                if previous_receipt is not None:
                    update.update(transition_reason="verified_smtp_retry", previous_receipt=previous_receipt)
                updates.append(update)
            elif message.get("status") in ("pending", "prepared", "uncertain", "unresolved"):
                unresolved.append({"message_id": key, "reason": "no_exact_verified_message"})
        history["ledger_reconciliation"] = {"available": True, "ledger_sha256": _digest(ledger_bytes),
            "updates": updates, "unresolved": unresolved, "applied": False}
    except (ValueError, TypeError, KeyError):
        history["ledger_reconciliation"] = {"available": False, "reason": "ledger_invalid_or_mismatched", "updates": []}
    return _bounded_history(history)
