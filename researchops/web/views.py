"""HTML view renderers for ResearchOps Web UI."""

import html
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional
from researchops.web.schedule_form import schedule_summary
from researchops.web.mcp_views import render_mcp_inventory
from researchops.web.formatting import render_size


def _escape(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str) and len(val) >= 19 and val[4:5] == "-" and "T" in val:
        try:
            instant = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if instant.tzinfo is not None:
                val = instant.astimezone(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
        except ValueError:
            pass
    return html.escape(str(val))


def _badge(status: str) -> str:
    s = (status or "").lower()
    labels = {"succeeded": "완료", "running": "진행 중", "queued": "대기", "preflight": "준비 중",
        "research": "조사 중", "validate": "검증 중", "dedupe": "중복 확인", "compose": "메일 작성",
        "validate_message": "메일 검증", "handoff": "전달 준비", "finalize": "기록 보관",
        "awaiting_receipt": "전송 대기", "prepared": "준비됨", "published": "전달됨", "acknowledged": "수락됨",
        "sent": "전송됨", "accepted": "수락됨", "smtp_accepted": "SMTP 서버 수락", "failed": "실패",
        "needs_attention": "확인 필요", "timed_out": "시간 초과", "cancelled": "취소", "uncertain": "전달 결과 불확실"}
    kind = "success" if s in {"succeeded", "smtp_accepted", "sent", "acknowledged"} else "danger" if s in {
        "failed", "needs_attention", "timed_out"} else "warning" if s in {"uncertain", "awaiting_receipt"} else "active" if s in {
        "running", "preflight", "research", "validate", "compose", "validate_message", "handoff"} else "neutral"
    return f'<span class="status-badge status-{kind}" data-status="{_escape(s)}">{_escape(labels.get(s, status))}</span>'


OPERATING_LABELS = {
    "Global email sending is disabled": "전체 발송 중지 상태",
    "SMTP delivery is disabled": "메일 자동 발송 켜기",
    "SMTP server is required": "SMTP 서버 입력",
    "Sender email is required": "발신 주소 입력",
    "Gmail account address is required": "Gmail 계정 입력",
    "Gmail app password is required": "앱 비밀번호 입력",
    "SMTP password is required": "앱 비밀번호 입력",
    "Create a recipient group with at least one email address": "수신자 그룹 만들기",
}


def _operating_notice(state: Optional[Dict[str, Any]]) -> str:
    from researchops.web.layout import can_write, is_admin
    if not state or not state.get("production") or not state.get("missing") or not can_write():
        return ""
    labels = {**OPERATING_LABELS}
    if not is_admin():
        labels.update({"Global email sending is disabled": "관리자가 전체 발송을 중지했습니다",
                       "SMTP delivery is disabled": "관리자가 메일 자동 발송을 꺼두었습니다"})
    return ('<div class="alert"><span><strong>메일 설정을 완료하세요.</strong> '
        + ' · '.join(_escape(labels.get(item, item)) for item in state["missing"])
        + '</span><a href="/delivery" class="btn btn-secondary">메일 설정</a></div>')


def render_base_layout(title: str, body_html: str, active_nav: str = "dashboard", flash: Optional[Dict[str, str]] = None) -> str:
    from researchops.web.layout import render_layout
    return render_layout(title, body_html, active_nav=active_nav, flash=flash)


def render_dashboard(doctor_data, tasks, recent_runs, flash=None, operating_state=None, dashboard_data=None):
    from researchops.web.list_views import dashboard
    return dashboard(doctor_data, tasks, recent_runs, flash, operating_state, dashboard_data)


def render_tasks_list(tasks, flash=None, *, deleted=False, pagination=None, filters=None):
    from researchops.web.list_views import tasks_list
    return tasks_list(tasks, flash, deleted=deleted, pagination=pagination, filters=filters)


def render_task_detail(
    task_id: str,
    task_info: Dict[str, Any],
    runs: List[Dict[str, Any]],
    ws_info: Optional[Dict[str, Any]] = None,
    versions: Optional[List[Dict[str, Any]]] = None,
    flash: Optional[Dict[str, str]] = None,
    production: bool = False,
    recipient_names: Optional[Dict[str, str]] = None,
    sender_names: Optional[Dict[str, str]] = None,
) -> str:
    from researchops.web.catalog_views import lifecycle_control, name_control
    status = task_info.get("status", {})
    active_version = task_info.get("active_version") or {}
    definition = active_version.get("definition") or {}
    removed = bool(status.get("deleted_at"))
    name = status.get("display_name") or task_info.get("name") or definition.get("name") or task_id

    ver_hash = _escape(active_version.get("hash", "-"))
    runner_conf = definition.get("runner", {})
    from researchops.engine.execution_plan import resolve_task_stages
    from researchops.web.ai_settings import settings_summary
    ai_summary = settings_summary(resolve_task_stages(definition)) if runner_conf else "AI 설정 미기록"
    delivery_conf = definition.get("delivery", {})
    schedule_conf = definition.get("schedule", {})

    is_enabled = bool(status.get("enabled"))
    enabled_badge = '<span style="color:#10b981; font-weight:600;">예약 중</span>' if is_enabled else '<span style="color:#64748b;">수동 실행</span>'
    delivery_approved = bool(status.get("delivery_approved"))
    approval_badge = '<span style="color:#10b981; font-weight:600;">발송 허용</span>' if delivery_approved else '<span style="color:#f59e0b; font-weight:600;">발송 승인 전</span>'
    delivery_mode = _escape(status.get("delivery_mode", "dry_run"))
    if production:
        approval_badge = ('<span style="color:#10b981; font-weight:600;">실제 메일 발송</span>'
            if delivery_conf.get("mode") == "handoff" else '<span style="color:#64748b;">발송 없는 Task 설정</span>')
    allowed_groups = delivery_conf.get("allowed_recipient_group_ids", [])
    catalog_routing = delivery_conf.get("recipient_routing_mode", "legacy_ids") == "catalog_name"

    ws_locked = ws_info.get("locked", False) if ws_info else False
    ws_lock_badge = '<span style="color:#ef4444; font-weight:600;">사용 중</span>' if ws_locked else '<span style="color:#10b981;">대기</span>'

    run_rows = ""
    for r in runs[:15]:
        run_id = _escape(r.get("run_id"))
        st = r.get("status", "")
        ph = r.get("phase", "")
        dt = _escape(r.get("local_date", ""))
        run_rows += f"""
        <tr>
            <td><a href="/runs/{run_id}" class="table-link mono">{run_id}</a></td>
            <td>{dt}</td>
            <td>{_badge(st)}</td>
            <td><code>{_escape(ph)}</code></td>
            <td><a href="/runs/{run_id}" class="btn btn-secondary btn-sm">상세 보기</a></td>
        </tr>
        """

    if not run_rows:
        run_rows = '<tr><td colspan="5" style="text-align:center; color:#94a3b8; padding:18px;">이 Task의 실행 이력이 없습니다.</td></tr>'

    versions_rows = ""
    if versions:
        for v in versions:
            v_hash = _escape(v.get("version_hash", ""))
            v_sealed = _escape(v.get("sealed_at", "-"))
            is_active = v.get("is_active", False)
            st_label = '<span style="color:#10b981; font-weight:600;">현재 버전</span>' if is_active else '<span style="color:#64748b;">보관 버전</span>'
            act_btn = ""
            if not is_active and not removed:
                act_btn = f"""
                <form method="POST" action="/tasks/{_escape(task_id)}/activate" style="display:inline-block; margin-right:4px;">
                    <input type="hidden" name="version_hash" value="{v_hash}">
                    <button type="submit" class="btn btn-success btn-sm" onclick="return confirm('이 버전을 적용할까요?');">이 버전 적용</button>
                </form>
                """
            versions_rows += f"""
            <tr>
                <td><code class="mono">{v_hash[:16]}...</code></td>
                <td>{v_sealed}</td>
                <td>{st_label}</td>
                <td style="white-space:nowrap;">{act_btn}</td>
            </tr>
            """

    toggle_schedule_btn = f"""
    <form method="POST" action="/tasks/{_escape(task_id)}/disable" style="display:inline-block;">
        <button type="submit" class="btn btn-secondary btn-sm">예약 끄기</button>
    </form>
    """ if is_enabled else f"""
    <form method="POST" action="/tasks/{_escape(task_id)}/enable" style="display:inline-block;">
        <button type="submit" class="btn btn-success btn-sm">예약 켜기</button>
    </form>
    """
    toggle_delivery_btn = f"""
    <form method="POST" action="/tasks/{_escape(task_id)}/approve-delivery" style="display:inline-block;">
        <input type="hidden" name="approved" value="false">
        <button type="submit" class="btn btn-secondary btn-sm">발송 허용 취소</button>
    </form>
    """ if delivery_approved else f"""
    <form method="POST" action="/tasks/{_escape(task_id)}/approve-delivery" style="display:inline-block;">
        <input type="hidden" name="approved" value="true">
        <button type="submit" class="btn btn-secondary btn-sm">발송 허용</button>
    </form>
    """
    if production:
        toggle_delivery_btn = ""

    task_actions = (lifecycle_control(f"/tasks/{task_id}/restore", restore=True) if removed else f'''
        <form method="POST" action="/tasks/{_escape(task_id)}/run" style="display:inline-block;">
            <button type="submit" class="btn btn-primary btn-sm">&#9654; 지금 실행</button></form>
        {toggle_schedule_btn}{toggle_delivery_btn}
        <a href="/tasks/{_escape(task_id)}/edit" class="btn btn-secondary btn-sm">수정</a>
        <a href="/tasks/new?clone={_escape(task_id)}" class="btn btn-secondary btn-sm">복제</a>
        {lifecycle_control(f"/tasks/{task_id}/delete")}''')

    body = f"""
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
        <a href="/tasks" class="table-link">&larr; Task 목록</a>
        <div style="display:flex; gap:10px; align-items:center;">
            {task_actions}
        </div>
    </div>

    <div class="grid-stats">
        <div class="stat-card">
            <div class="stat-label">Task 이름 {'· 삭제됨' if removed else ''}</div>
            <div class="stat-value" style="font-size:18px;">{_escape(name)}</div>
            <div class="form-help">관리번호 #{_escape(status.get('entity_id', ''))} · 내부 참조 {_escape(task_id)}</div>
            {'' if removed else '<details><summary>이름 변경</summary>' + name_control(f'/tasks/{task_id}/rename', name) + '</details>'}
            <div style="font-size:12px; color:#64748b; margin-top:4px;">{enabled_badge} &bull; {approval_badge}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">현재 버전</div>
            <div class="stat-value mono" style="font-size:14px;">{ver_hash[:16]}...</div>
            <div style="font-size:12px; color:#64748b; margin-top:4px;">저장: {_escape(active_version.get('sealed_at', '-'))}</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">단계별 AI & 일정</div>
            <div style="font-size:13px; overflow-wrap:anywhere;">{_escape(ai_summary)}</div>
            <div style="font-size:12px; color:#64748b; margin-top:4px;">{_escape(schedule_summary(schedule_conf.get('cron', '-')))}</div>
            <div class="form-help">Cron: <code>{_escape(schedule_conf.get('cron', '-'))}</code> · Asia/Seoul</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">작업 공간</div>
            <div class="stat-value" style="font-size:16px;">{ws_lock_badge}</div>
            <div style="font-size:12px; color:#64748b; margin-top:4px;">파일 {ws_info.get('total_files', 0) if ws_info else 0}개 · {render_size(ws_info.get('total_bytes', 0) if ws_info else 0)}</div>
        </div>
    </div>

    <div class="card">
        <div class="card-header">
            <div class="card-title">Task 설정</div>
        </div>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px;">
            <div>
                <h4 style="font-size:14px; margin-bottom:8px;">{'등록 그룹명으로 수신자 선택' if catalog_routing else '수신자 그룹'}</h4>
                {'<p class="form-help">task.md의 규칙에 따라 메일 작성 시점의 활성 수신자 그룹 이름 중 하나를 선택합니다. 실제 주소는 AI에 전달되지 않습니다.</p>' if catalog_routing else ''}
                <ul style="margin-left:20px; font-size:14px; color:#334155;">
                    {''.join(f'<li>{_escape((recipient_names or {}).get(g, g))}</li>' for g in allowed_groups)}
                </ul>
            </div>
            <div>
                <h4 style="font-size:14px; margin-bottom:8px;">실행 설정</h4>
                <p style="font-size:13px; color:#475569;">결과 없을 때 발송: <strong>{delivery_conf.get('send_on_empty', True)}</strong></p>
                <p style="font-size:13px; color:#475569;">일부 결과 처리: <code>{_escape(delivery_conf.get('partial_policy', 'send_with_warning'))}</code></p>
                <p style="font-size:13px; color:#475569;">전달 방식: <code>{delivery_mode}</code></p>
                <p style="font-size:13px; color:#475569;">발신 계정: {_escape((sender_names or {}).get(delivery_conf.get('sender_profile_id', 'default'), delivery_conf.get('sender_profile_id', 'default')))} · <a href="/delivery?sender={_escape(delivery_conf.get('sender_profile_id', 'default'))}">설정 보기</a></p>
            </div>
        </div>
    </div>

    {f'''
    <div class="card">
        <div class="card-header">
            <div class="card-title">이전 버전</div>
        </div>
        <div class="table-scroll"><table>
            <thead>
                <tr>
                    <th>버전</th>
                    <th>저장 시각</th>
                    <th>상태</th>
                    <th>작업</th>
                </tr>
            </thead>
            <tbody>
                {versions_rows}
            </tbody>
        </table></div>
    </div>
    ''' if versions_rows else ''}

    <div class="card">
        <div class="card-header">
            <div class="card-title">실행 이력 ({len(runs)})</div>
        </div>
        <div class="table-scroll"><table>
            <thead>
                <tr>
                    <th>실행 ID</th>
                    <th>업무일</th>
                    <th>상태</th>
                    <th>단계</th>
                    <th>작업</th>
                </tr>
            </thead>
            <tbody>
                {run_rows}
            </tbody>
        </table></div>
    </div>
    """
    return render_base_layout(f"Task {task_id}", body, active_nav="tasks", flash=flash)


def render_task_create(
    templates: List[Any],
    existing_tasks: List[Dict[str, Any]],
    clone_source: Optional[str] = None,
    flash: Optional[Dict[str, str]] = None,
    recipient_groups: Optional[Dict[str, List[str]]] = None,
    operating_state: Optional[Dict[str, Any]] = None,
    editor_values: Optional[Dict[str, Any]] = None,
    editor_mode: str = "create",
    sender_profiles: Optional[Dict[str, Any]] = None,
    recipient_names: Optional[Dict[str, str]] = None,
    sender_names: Optional[Dict[str, str]] = None,
    model_catalog: Optional[Dict[str, Any]] = None,
) -> str:
    from researchops.web.task_editor import task_editor_body
    mode = "clone" if clone_source and editor_mode == "create" else editor_mode
    body = task_editor_body(editor_values, mode=mode, recipient_groups=recipient_groups,
        sender_profiles=sender_profiles, recipient_names=recipient_names, sender_names=sender_names,
        notice=_operating_notice(operating_state), model_catalog=model_catalog)
    title = "Task 수정" if mode == "edit" else "Task 복제" if mode == "clone" else "운영 Task 만들기"
    return render_base_layout(title, body, active_nav="tasks", flash=flash)


def render_runs_list(runs, filter_status=None, flash=None, *, pagination=None, filters=None):
    from researchops.web.list_views import runs_list
    return runs_list(runs, filter_status, flash, pagination=pagination, filters=filters)


def _render_research_summary(value) -> str:
    """Keep the original plain text accessible without expanding the result grid."""
    text = "" if value is None else str(value)
    if not text:
        return '<p class="form-help">요약이 없습니다.</p>'
    full = html.escape(text)
    if len(text) <= 400 and text.count("\n") < 6:
        return f'<div class="research-summary-text" id="research-summary-text">{full}</div>'
    preview = html.escape(text[:400].rstrip()) + ("…" if len(text) > 400 else "")
    return f'''<details class="research-summary-details">
        <summary><span class="summary-show">요약 전체 보기</span><span class="summary-hide">요약 접기</span></summary>
        <div class="research-summary-text research-summary-full" id="research-summary-text"
             role="region" aria-label="조사 요약 전문" tabindex="0">{full}</div>
      </details><p class="research-summary-preview">{preview}</p>'''


def render_run_detail(run_data, artifacts, events, flash=None, *, model_catalog=None, retry_values=None,
                      current_task=None, send_email_error=None, send_email_request_key=None, tab=None, partial=False):
    from researchops.web.run_views import render_detail
    return render_detail(run_data, artifacts, events, flash, model_catalog=model_catalog,
        retry_values=retry_values, current_task=current_task, send_email_error=send_email_error,
        send_email_request_key=send_email_request_key, tab=tab, partial=partial)


def render_doctor_view(doctor_data: Dict[str, Any], flash: Optional[Dict[str, str]] = None) -> str:
    db = doctor_data.get("database", {})
    dirs = doctor_data.get("directories", {}).get("details", {})
    schemas = doctor_data.get("schemas", {})
    runners = doctor_data.get("runners", {})
    overall = doctor_data.get("overall_status", "unknown")

    runner_rows = ""
    for rname, rinfo in runners.items():
        if rname == "isolation":
            continue
        avail = rinfo.get("available", False)
        st = '<span style="color:#10b981; font-weight:600;">&#10003; 사용 가능</span>' if avail else '<span style="color:#ef4444;">&#10007; Missing</span>'
        bin_path = _escape(rinfo.get("binary_path") or "-")
        runner_rows += f"""
        <tr>
            <td><strong>{_escape(rname)}</strong></td>
            <td>{st}</td>
            <td><code class="mono">{bin_path}</code></td>
        </tr>
        """

    dir_rows = ""
    for dname, dinfo in dirs.items():
        exists = dinfo.get("exists", False)
        writable = dinfo.get("writable", False)
        st = '<span style="color:#10b981;">&#10003; 사용 가능</span>' if (exists and writable) else '<span style="color:#ef4444;">&#10007; Issue</span>'
        res_path = _escape(dinfo.get("path", "-"))
        dir_rows += f"""
        <tr>
            <td><code>{_escape(dname)}</code></td>
            <td>{st}</td>
            <td><code class="mono">{res_path}</code></td>
        </tr>
        """

    body = f"""
    <div class="card">
        <div class="card-header">
            <div class="card-title">시스템 상태</div>
            {_badge(overall)}
        </div>
        <div class="grid-stats">
            <div class="stat-card">
                <div class="stat-label">데이터베이스</div>
                <div class="stat-value" style="font-size:18px;">SQLite {_escape(db.get('sqlite_version', '-'))}</div>
                <div style="font-size:12px; color:#64748b; margin-top:4px;">{_escape(db.get('db_path', '-'))}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">결과 계약 검사</div>
                <div class="stat-value" style="font-size:18px;">{'Valid' if schemas.get('ok') else 'Missing'}</div>
                <div style="font-size:12px; color:#64748b; margin-top:4px;">Missing: {len(schemas.get('missing_schemas', []))}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-header">
            <div class="card-title">AI 실행 도구</div>
        </div>
        <div class="table-scroll"><table>
            <thead>
                <tr>
                    <th>도구</th>
                    <th>상태</th>
                    <th>실행 파일</th>
                </tr>
            </thead>
            <tbody>
                {runner_rows}
            </tbody>
        </table></div>
    </div>

    {render_mcp_inventory(doctor_data.get("mcp", {}))}

    <div class="card">
        <div class="card-header">
            <div class="card-title">저장 경로</div>
        </div>
        <div class="table-scroll"><table>
            <thead>
                <tr>
                    <th>용도</th>
                    <th>상태</th>
                    <th>경로</th>
                </tr>
            </thead>
            <tbody>
                {dir_rows}
            </tbody>
        </table></div>
    </div>
    """
    return render_base_layout("시스템 상태", body, active_nav="doctor", flash=flash)


def render_delivery_view(config, flash=None, operating_state=None, sender_profile_id="default", creating_sender=False,
                         form_values=None, catalog_entries=None):
    from researchops.web.delivery_settings import render_settings
    return render_settings(config, flash, operating_state, sender_profile_id, creating_sender, form_values, catalog_entries)
