"""Read-only, permission-scoped Web queries, filtered before pagination."""

from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from researchops.errors import ValidationError
from researchops.services.scheduler import CronExpression

SEOUL = ZoneInfo("Asia/Seoul")


def next_occurrences(cron, *, count=3, now=None):
    expression = CronExpression(cron)
    start = now or datetime.now(timezone.utc)
    horizon = start + timedelta(days=366 * 13)
    values = []
    for _ in range(count):
        value = expression.occurrence_between(start, horizon, latest=False)
        if value is None:
            break
        values.append(value.astimezone(SEOUL).isoformat(timespec="minutes"))
        start = value
    return values


class WebAccessService:
    def __init__(self, app):
        self.app, self.db = app, app.db

    @staticmethod
    def _scope(principal, alias="t"):
        if principal.role == "admin":
            return "1=1", []
        if principal.role == "user":
            return (f"EXISTS(SELECT 1 FROM entity_catalog own WHERE own.kind='task' AND own.legacy_key={alias}.task_id AND own.owner_user_id=?)",
                    [principal.user_id])
        return (f"EXISTS(SELECT 1 FROM auth_user_tasks g WHERE g.user_id=? AND g.task_id={alias}.task_id)",
                [principal.user_id])

    @staticmethod
    def _page(page, page_size):
        try:
            page, page_size = int(page), int(page_size)
        except (ValueError, TypeError):
            raise ValidationError("페이지 번호를 확인하세요.")
        if not 1 <= page <= 1_000_000 or not 1 <= page_size <= 100:
            raise ValidationError("페이지 범위를 확인하세요.")
        return page, page_size

    @staticmethod
    def _search(value):
        if not isinstance(value, str) or len(value) > 200:
            raise ValidationError("검색어는 200자 이내로 입력하세요.")
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def tasks(self, principal, *, q="", status="", page=1, page_size=25, deleted=False):
        page, page_size = self._page(page, page_size)
        scope, params = self._scope(principal)
        conditions = [scope, "t.active_version_hash IS NOT NULL", "c.deleted_at IS NOT NULL" if deleted else "c.deleted_at IS NULL"]
        if q:
            conditions.append("(COALESCE(c.display_name,t.task_id) LIKE ? ESCAPE '\\' OR t.task_id LIKE ? ESCAPE '\\')")
            params += [self._search(q)] * 2
        if status in ("scheduled", "manual"):
            conditions.append({"scheduled": "t.enabled=1", "manual": "t.enabled=0"}[status])
        elif status:
            raise ValidationError("Task 필터를 확인하세요.")
        base = " FROM tasks t LEFT JOIN entity_catalog c ON c.kind='task' AND c.legacy_key=t.task_id WHERE " + " AND ".join(conditions)
        conn = self.db.get_connection()
        try:
            total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
            rows = conn.execute("SELECT t.*,COALESCE(c.display_name,t.task_id) name,c.entity_id,c.deleted_at,c.owner_user_id,(SELECT username FROM auth_users WHERE user_id=c.owner_user_id) owner_name" + base +
                                " ORDER BY c.entity_id DESC,t.task_id LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size]).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                latest = conn.execute("SELECT run_id,status,finished_at FROM scheduled_runs WHERE task_id=? ORDER BY created_at DESC,run_id DESC LIMIT 1",
                                      (item["task_id"],)).fetchone()
                item.update(display_name=item["name"], latest_run_id=latest["run_id"] if latest else None,
                            latest_status=latest["status"] if latest else None,
                            latest_finished_at=latest["finished_at"] if latest else None)
                item["next_scheduled_for"] = None
                if item["enabled"] and item["active_version_hash"]:
                    version = conn.execute("SELECT definition_json FROM task_versions WHERE version_hash=?", (item["active_version_hash"],)).fetchone()
                    try:
                        occurrences = next_occurrences(json.loads(version[0])["schedule"]["cron"],count=1)
                        item["next_scheduled_for"] = occurrences[0] if occurrences else None
                    except (ValueError, TypeError, KeyError, ValidationError):
                        pass
                if self.app.settings.environment == "production" and item["active_version_hash"] and item["delivery_mode"] == "handoff":
                    item["delivery_approved"] = True
                items.append(item)
            return {"items": items, "page": page, "page_size": page_size, "total": total}
        finally:
            conn.close()

    def runs(self, principal, *, task_id=None, status=None, q="", page=1, page_size=25):
        page, page_size = self._page(page, page_size)
        scope, params = self._scope(principal, "r")
        conditions = [scope]
        if task_id:
            conditions.append("r.task_id=?")
            params.append(task_id)
        if status == "attention":
            conditions.append("r.status IN ('failed','needs_attention','timed_out')")
        elif status == "active":
            conditions.append("r.status IN ('queued','running','awaiting_receipt')")
        elif status:
            conditions.append("r.status=?")
            params.append(status)
        if q:
            conditions.append("(COALESCE(c.display_name,r.task_id) LIKE ? ESCAPE '\\' OR r.run_id LIKE ? ESCAPE '\\')")
            params += [self._search(q)] * 2
        base = " FROM scheduled_runs r LEFT JOIN entity_catalog c ON c.kind='task' AND c.legacy_key=r.task_id WHERE " + " AND ".join(conditions)
        conn = self.db.get_connection()
        try:
            total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
            items = [dict(row) for row in conn.execute("SELECT r.*,COALESCE(c.display_name,r.task_id) task_name,c.owner_user_id,(SELECT username FROM auth_users WHERE user_id=c.owner_user_id) owner_name" + base +
                        " ORDER BY r.created_at DESC,r.run_id DESC LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size])]
            return {"items": items, "page": page, "page_size": page_size, "total": total}
        finally:
            conn.close()

    def dashboard(self, principal):
        scope, params = self._scope(principal, "r")
        task_scope, task_params = self._scope(principal)
        conn = self.db.get_connection()
        try:
            rows = conn.execute("SELECT r.status,COUNT(*) n FROM scheduled_runs r WHERE " + scope + " GROUP BY r.status", params)
            counts = {row["status"]: row["n"] for row in rows}
            scheduled = conn.execute("""SELECT t.task_id,c.display_name name,v.definition_json FROM tasks t
                LEFT JOIN entity_catalog c ON c.kind='task' AND c.legacy_key=t.task_id
                JOIN task_versions v ON v.version_hash=t.active_version_hash
                WHERE t.enabled=1 AND c.deleted_at IS NULL AND """ + task_scope, task_params).fetchall()
            upcoming = []
            for row in scheduled:
                try:
                    cron = json.loads(row["definition_json"])["schedule"]["cron"]
                    occurrences = next_occurrences(cron, count=1)
                    if occurrences:
                        upcoming.append({"task_id": row["task_id"], "name": row["name"] or row["task_id"], "scheduled_for": occurrences[0]})
                except (ValueError, TypeError, KeyError, ValidationError):
                    continue
            upcoming.sort(key=lambda item: (item["scheduled_for"], item["task_id"]))
            return {"counts": {"attention": sum(counts.get(key, 0) for key in ("failed", "needs_attention", "timed_out")),
                               "running": sum(counts.get(key, 0) for key in ("queued", "running", "awaiting_receipt")),
                               "scheduled": len(scheduled), "completed": counts.get("succeeded", 0)}, "upcoming": upcoming[:5]}
        finally:
            conn.close()
