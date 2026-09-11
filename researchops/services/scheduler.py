"""Validated cron and durable Seoul scheduling with atomic occurrence creation."""

from datetime import datetime, timedelta, timezone
import json
import uuid
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from researchops.config import Settings
from researchops.domain.models import ScheduledRun
from researchops.errors import ValidationError
from researchops.storage.repositories import require_catalog_active, require_catalog_references

SEOUL = ZoneInfo("Asia/Seoul")


class CronField:
    def __init__(self, field_str: str, min_val: int, max_val: int, is_dow: bool = False):
        self.field_str = field_str.strip()
        self.min_val,self.max_val,self.is_dow = min_val,max_val,is_dow
        self.allowed_values = self._parse()

    def _parse(self) -> Set[int]:
        values = set()
        try:
            for item in self.field_str.split(","):
                if not item or item != item.strip():
                    raise ValueError("empty cron element")
                pieces = item.split("/")
                if len(pieces)>2:
                    raise ValueError("multiple steps")
                span = pieces[0]
                step = int(pieces[1]) if len(pieces)==2 else 1
                if step<=0:
                    raise ValueError("step must be positive")
                if span=="*":
                    start,end=self.min_val,self.max_val
                elif "-" in span:
                    start,end=map(int,span.split("-"))
                else:
                    start=int(span)
                    end=self.max_val if len(pieces)==2 else start
                if not self.min_val<=start<=end<=self.max_val:
                    raise ValueError("cron value outside field range")
                values.update(range(start,end+1,step))
        except (ValueError,TypeError) as exc:
            raise ValidationError(f"Invalid cron field '{self.field_str}': {exc}") from exc
        if not values:
            raise ValidationError("Cron field contains no values")
        if self.is_dow and (0 in values or 7 in values):
            values.update((0,7))
        return values

    def matches(self,value:int)->bool:
        return value in self.allowed_values


class CronExpression:
    def __init__(self,expr:str):
        if not isinstance(expr,str) or len(expr.split())!=5:
            raise ValidationError("Cron requires minute hour day-of-month month day-of-week")
        self.expr=expr.strip()
        parts=self.expr.split()
        self.minute=CronField(parts[0],0,59)
        self.hour=CronField(parts[1],0,23)
        self.dom=CronField(parts[2],1,31)
        self.month=CronField(parts[3],1,12)
        self.dow=CronField(parts[4],0,7,is_dow=True)

    @classmethod
    def from_string(cls,expr):
        return cls(expr)

    def _day_matches(self,dt):
        dom,dow=self.dom.matches(dt.day),self.dow.matches((dt.weekday()+1)%7)
        # Vixie cron uses OR when neither day field begins with '*'.
        day=(dom and dow) if (self.dom.field_str.startswith("*") or self.dow.field_str.startswith("*")) else (dom or dow)
        return self.month.matches(dt.month) and day

    def matches(self,dt:datetime)->bool:
        return self._day_matches(dt) and self.hour.matches(dt.hour) and self.minute.matches(dt.minute)

    def occurrence_between(self,start:datetime,end:datetime,latest:bool=True):
        """Find one occurrence without scanning every minute of downtime."""
        start,end=start.astimezone(SEOUL),end.astimezone(SEOUL)
        day=(end if latest else start).replace(hour=0,minute=0,second=0,microsecond=0)
        delta=timedelta(days=-1 if latest else 1)
        while start.date()<=day.date()<=end.date():
            if self._day_matches(day):
                for hour in sorted(self.hour.allowed_values,reverse=latest):
                    for minute in sorted(self.minute.allowed_values,reverse=latest):
                        candidate=day.replace(hour=hour,minute=minute)
                        if start<candidate<=end:
                            return candidate.astimezone(timezone.utc)
            day+=delta
        return None


class SchedulerService:
    def __init__(self,settings:Settings,task_repo,run_repo,run_service,state_repo):
        self.settings,self.task_repo,self.run_repo=settings,task_repo,run_repo
        self.run_service,self.state_repo=run_service,state_repo

    def schedule_tick(self,now:Optional[datetime]=None)->List[Dict[str,Any]]:
        # The installation-wide switch must be checked before even reading or
        # advancing task watermarks. Per-task enablement is a separate gate.
        if self.settings.raw_config.get("scheduler", {}).get("enabled", True) is not True:
            return [{"status": "disabled", "reason": "scheduler_disabled"}]
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now=now.replace(tzinfo=SEOUL)
        current=now.astimezone(timezone.utc).replace(second=0,microsecond=0)
        results=[]
        for task in self.task_repo.list_tasks(enabled_only=True):
            task_id=task["task_id"]
            version=self.task_repo.get_active_version(task_id)
            if not version or not version.definition.schedule:
                results.append({"task_id":task_id,"status":"skipped","reason":"no_schedule_config"})
                continue
            config=version.definition.schedule
            try:
                if config.get("timezone","Asia/Seoul")!="Asia/Seoul":
                    raise ValidationError("schedule.timezone must be Asia/Seoul")
                cron=CronExpression(config["cron"])
                policy=config.get("misfire_policy","enqueue_once")
                if policy not in {"skip","enqueue_once","catch_up"}:
                    raise ValidationError("Unsupported misfire_policy")
                result=self._tick_task(task_id,version,cron,policy,current)
            except (ValidationError,KeyError) as exc:
                result={"task_id":task_id,"status":"error","reason":str(exc)}
            results.append(result)
        return results

    def _tick_task(self,task_id,version,cron,policy,current):
        with self.run_repo.db.transaction() as conn:
            try:
                require_catalog_active(conn, "task", task_id)
                require_catalog_references(conn, version.definition.to_dict())
            except ValidationError:
                return {"task_id":task_id,"status":"skipped","reason":"catalog_item_deleted"}
            active=conn.execute("SELECT active_version_hash,enabled FROM tasks WHERE task_id=?",(task_id,)).fetchone()
            if not active or not active["enabled"] or active["active_version_hash"]!=version.version_hash:
                return {"task_id":task_id,"status":"skipped","reason":"task_changed"}
            watermark=conn.execute("SELECT evaluated_through FROM scheduler_watermarks WHERE task_id=?",(task_id,)).fetchone()
            previous=datetime.fromisoformat(watermark[0]) if watermark else current-timedelta(minutes=1)
            if previous>=current:
                return {"task_id":task_id,"status":"skipped","reason":"already_scheduled"}
            search_start=current-timedelta(minutes=1) if policy=="skip" else previous
            occurrence=cron.occurrence_between(search_start,current,latest=policy!="catch_up")
            advance=current
            result={"task_id":task_id,"status":"not_due","task_time":current.astimezone(SEOUL).isoformat()}
            if occurrence:
                scheduled_for=occurrence.isoformat()
                existing=conn.execute("SELECT run_id FROM scheduled_runs WHERE task_id=? AND trigger_type='schedule' AND scheduled_for=?",(task_id,scheduled_for)).fetchone()
                pending=conn.execute("SELECT run_id,status FROM scheduled_runs WHERE task_id=? AND status='queued' ORDER BY created_at LIMIT 1",(task_id,)).fetchone()
                if existing:
                    result.update(status="skipped",reason="already_scheduled",existing_run_id=existing[0])
                elif pending:
                    result.update(status="coalesced",reason="task_already_active_in_status_queued",active_run_id=pending[0],scheduled_for=scheduled_for)
                    if policy=="catch_up":
                        advance=previous
                else:
                    business=occurrence.astimezone(SEOUL)
                    run=ScheduledRun(run_id="run-"+uuid.uuid4().hex,task_id=task_id,task_version_hash=version.version_hash,scheduled_for=scheduled_for,timezone="Asia/Seoul",local_date=business.strftime("%Y-%m-%d"),local_date_display=business.strftime("%Y.%m.%d"),trigger_type="schedule")
                    data=run.to_dict()
                    conn.execute(f"INSERT INTO scheduled_runs ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",tuple(data.values()))
                    conn.execute("INSERT INTO execution_controls(run_id) VALUES(?)",(run.run_id,))
                    from researchops.engine.execution_plan import build_execution_plan, resolve_task_stages
                    self.run_repo.insert_execution_plan(conn, run.run_id,
                        build_execution_plan(resolve_task_stages(version.definition),
                            selection_source={"kind": "task_version", "task_version_hash": version.version_hash}))
                    result.update(status="enqueued",run_id=run.run_id,scheduled_for=scheduled_for,cron=cron.expr)
                    if policy=="catch_up":
                        advance=occurrence
                details={**result,"evaluated_from":previous.isoformat(),"evaluated_through":advance.isoformat(),"misfire_policy":policy}
                conn.execute("INSERT INTO audit_events(entity_type,entity_id,event_type,details_json,occurred_at) VALUES('task',?,'schedule_decision',?,?)",(task_id,json.dumps(details),datetime.now(timezone.utc).isoformat()))
            conn.execute("INSERT INTO scheduler_watermarks VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET evaluated_through=excluded.evaluated_through",(task_id,advance.isoformat()))
            return result
