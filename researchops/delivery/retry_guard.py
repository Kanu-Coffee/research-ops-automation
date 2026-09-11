"""Serialize retries against pending or uncertain delivery in the same run family."""

from researchops.errors import DeliveryError


def require_retry_family_clear(conn, run_id, *, email_only=False, prepared_email=False):
    root, seen = run_id, set()
    while root not in seen:
        seen.add(root)
        row = conn.execute('SELECT parent_run_id FROM scheduled_runs WHERE run_id=?',(root,)).fetchone()
        if not row or not row[0]:
            break
        root = row[0]
    rows = conn.execute('''WITH RECURSIVE family(run_id) AS (
        SELECT ? UNION SELECT r.run_id FROM scheduled_runs r JOIN family f ON r.parent_run_id=f.run_id)
        SELECT r.run_id,r.status,h.status AS delivery_status,h.external_delivery_status,
            EXISTS(SELECT 1 FROM smtp_attempts a WHERE a.handoff_id=h.handoff_id
                AND a.status IN ('smtp_accepted','uncertain')) AS accepted_or_uncertain_attempt,
            EXISTS(SELECT 1 FROM delivery_receipts d WHERE d.handoff_id=h.handoff_id
                AND d.status IN ('sent','accepted','smtp_accepted','uncertain')) AS accepted_or_uncertain_receipt
        FROM family f
        JOIN scheduled_runs r ON r.run_id=f.run_id
        LEFT JOIN delivery_handoffs h ON h.run_id=r.run_id AND h.message_type!='system_alert'
        LEFT JOIN execution_controls e ON e.run_id=r.run_id
        WHERE COALESCE(e.force_dry_run,0)=0 AND r.trigger_type!='candidate_dry_run' ''',(root,)).fetchall()
    for row in rows:
        if prepared_email and (row['delivery_status'] in ('sent','acknowledged','uncertain')
                or row['external_delivery_status'] == 'smtp_accepted'
                or row['accepted_or_uncertain_attempt'] or row['accepted_or_uncertain_receipt']):
            raise DeliveryError('Related email was accepted or its delivery is uncertain; stored mail cannot be sent again')
        if email_only and row['run_id'] == run_id:
            continue
        if (row['status'] in ('queued','running','awaiting_receipt') or
                row['delivery_status'] in ('published','queued','uncertain')):
            raise DeliveryError('Related email is pending or uncertain; use the email retry controls after checking delivery')
