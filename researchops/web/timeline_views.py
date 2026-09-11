"""Recorded Run milestones and timings, without model or credential payloads."""

import html
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo
from researchops.web.formatting import render_size


PHASE_LABELS = {"preflight": "실행 준비", "research": "Research · 조사",
    "validate": "조사 결과 검증", "dedupe": "중복 확인", "artifact_acquisition": "요청 파일 확보",
    "prepare_compose": "메일 작성 입력 준비", "compose": "Compose · 메일 작성",
    "validate_message": "메일 검증", "handoff": "전달 준비", "finalize": "실행 기록 보관",
    "smtp": "메일 전달"}
STATE_LABELS = {"running": "진행 중", "succeeded": "완료", "failed": "실패", "cancelled": "취소",
    "timed_out": "시간 초과", "needs_attention": "확인 필요", "skipped": "건너뜀", "uncertain": "전달 결과 불확실"}
SUMMARY_LABELS = {"record_count": "조사 record", "reportable_count": "전달 record", "excluded_count": "제외 record",
    "artifact_count": "요청 파일", "available_count": "확보 파일", "failed_count": "실패 파일",
    "excluded_artifact_count": "제외 파일", "attachment_count": "첨부", "inline_count": "본문 이미지",
    "warning_count": "경고", "validation_error_count": "검증 오류", "stdout_bytes": "stdout 용량",
    "stderr_bytes": "stderr 용량", "event_count": "이벤트", "exit_code": "종료 코드",
    "cleanup_verified": "정리 확인", "composition_revision": "Composition revision", "file_count": "파일",
    "total_bytes": "전체 용량", "recipient_count": "수신자 수", "attempt_count": "시도 수",
    "smtp_reply_code": "SMTP 응답 코드", "queued_count": "대기 건수", "accepted_count": "수락 건수",
    "mime_bytes": "메일 MIME 용량"}
AUDIT_LABELS = {"run_enqueued": "실행 등록", "run_cancel_requested": "취소 요청", "run_retried": "재실행 등록",
    "compose_only_enqueued": "메일 재작성 등록", "worker_claimed_lease": "Worker 실행 인수",
    "stale_lease_blocked": "Worker 정리 확인 필요", "run_completed": "실행 종료 기록",
    "run_delivery_queued": "메일 전달 대기 등록", "run_delivery_data_started": "메일 DATA 전송 시작"}


def _e(value):
    return html.escape(str(value if value is not None else ""))


def format_seoul(value):
    if not isinstance(value, str) or len(value) > 40:
        return "미기록"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset().total_seconds() != 0:
            return "미기록"
        return instant.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except (ValueError, OverflowError):
        return "미기록"


def format_duration(value):
    if type(value) is not int or not 0 <= value < 2**63:
        return "미기록"
    seconds, millis = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}시간 " if hours else "") + (f"{minutes}분 " if minutes or hours else "") + f"{seconds}.{millis:03d}초"


def _summary_items(summary):
    if not isinstance(summary, dict):
        return []
    values = []
    for key, label in SUMMARY_LABELS.items():
        value = summary.get(key)
        if (type(value) is not int or not -(2**63) <= value < 2**63 or
                (key != "exit_code" and value < 0) or (key == "cleanup_verified" and value not in (0, 1))):
            continue
        values.append(f"{label}: {'확인' if value else '미확인'}" if key == "cleanup_verified" else
                      f"{label}: {render_size(value)}" if key.endswith("_bytes") else f"{label}: {value:,}")
    return values


def _live_step_duration(step):
    if not step.get("active"):
        return format_duration(step.get("duration_ms"))
    try:
        start, observed = (datetime.fromisoformat(step[key].replace("Z", "+00:00"))
                           for key in ("started_at", "observed_at"))
        return format_duration(int((observed - start).total_seconds() * 1000))
    except (ValueError, KeyError, TypeError, AttributeError):
        return "미기록"


def render_run_timeline(timeline, run, artifacts, *, mcp=False, artifact_report=False, response=False):
    timeline = timeline or {"status": "not_recorded", "steps": [], "run": run}
    timing = timeline.get("run", {})
    run_url = quote(str(run.get("run_id", "")), safe="")
    active = timing.get("active") is True
    finished_text = format_seoul(timing.get("finished_at"))
    if not timing.get("finished_at"):
        finished_text = "대기 중 · 종료되지 않음" if timing.get("queue_active") else "진행 중 · 종료되지 않음" if active else "종료 시각 미기록"
    facts = [("등록", format_seoul(timing.get("created_at"))), ("실행 시작", format_seoul(timing.get("started_at"))),
             ("실행 종료", finished_text)]
    fact_html = "".join(f'<div><dt>{label}</dt><dd>{_e(value)}</dd></div>' for label, value in facts)
    for label, key in (("대기", "queue_duration_ms"), ("실행 소요", "duration_ms"), ("전체 소요", "total_duration_ms")):
        running = (timing.get("queue_active") if key == "queue_duration_ms" else active and
                   (bool(timing.get("started_at")) or key == "total_duration_ms"))
        fact_html += (f'<div><dt>{label}</dt><dd><span data-run-duration="{key}">{format_duration(timing.get(key))}</span>'
                      + (' <small>진행 중</small>' if running else '') + '</dd></div>')
    files = {item.get("filename") for item in artifacts if isinstance(item, dict)}
    rows = []
    for step in timeline.get("steps", []):
        phase, state = step.get("phase"), step.get("state")
        label = PHASE_LABELS.get(phase, "확인되지 않은 단계")
        completed, running = step.get("completion_recorded") is True, step.get("active") is True
        state_label = STATE_LABELS.get(state, "확인 필요") if completed or running else "종료 미기록"
        color = {"succeeded": "#047857", "failed": "#b91c1c", "cancelled": "#9a3412", "timed_out": "#b91c1c"}.get(state, "#475569")
        duration = _live_step_duration(step)
        duration_attr = f' data-step-start="{_e(step.get("started_at"))}"' if running else ""
        links = []
        if phase in {"research", "compose"}:
            for stream in ("stdout", "stderr"):
                filename = f"logs/{phase}.{stream}"
                if filename in files:
                    links.append(f'<a class="table-link" href="/runs/{run_url}/artifacts/{filename}">{phase.title()} {stream} 로그</a>')
            if not links:
                links.append('<span class="form-help">단계별 로그: 보관 후 확인할 수 있습니다.</span>')
            if mcp:
                links.append('<a class="table-link" href="#mcp-call-results">MCP 호출·시간</a>')
            if response:
                links.append('<a class="table-link" href="#response-diagnostics">결과 응답 진단</a>')
        if phase in {"artifact_acquisition", "prepare_compose", "validate_message"} and artifact_report:
            links.append('<a class="table-link" href="#requested-artifact-report">요청 파일 진단</a>')
        counters = _summary_items(step.get("summary"))
        detail_summary = " · ".join(counters) if counters else "추가 집계 미기록"
        end = format_seoul(step.get("finished_at")) if completed else "진행 중" if running else "종료 미기록"
        step_id = _e(step.get("step_id"))
        rows.append(f'''<details class="run-step" data-step-id="{step_id}" data-step-phase="{_e(phase)}">
          <summary><span class="run-step-name">{label}</span><span style="color:{color};">{state_label}</span>
          <span class="run-step-duration"{duration_attr}>{duration}</span>{'<small>경과</small>' if running else ''}</summary>
          <div class="run-step-detail"><p>시작: {format_seoul(step.get('started_at'))}<br>종료: {end}
          <br>소요: <span{duration_attr}>{duration}</span>{' (진행 중 경과 시간)' if running else ''} · 시도: {_e(step.get('attempt'))}</p>
          <p>{detail_summary}</p><div class="run-step-links">{' '.join(links)}</div></div></details>''')
    notice = ""
    if not rows:
        notice = '<p class="form-help">상세 단계 시간은 기록되지 않았습니다. 과거 실행은 저장된 실행 시작·종료 시각과 관리 이벤트만 표시합니다.</p>'
    if timeline.get("invalid_event_count"):
        notice += '<p role="status">일부 단계 기록을 확인할 수 없어 부분 진단만 표시합니다. 해당 단계의 성공이나 종료를 추정하지 않습니다.</p>'
    if timeline.get("omitted_event_count"):
        notice += f'<p class="form-help">표시 한도로 단계 이벤트 {_e(timeline["omitted_event_count"])}건이 생략됐습니다. 전체 감사 기록을 확인하세요.</p>'
    return f'''<div class="card" id="run-timeline"><div class="card-header"><div class="card-title">실행 단계와 시간</div></div>
      <style>.run-times{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin:16px 0 24px}}
      .run-times dt{{font-size:12px;color:#64748b}}.run-times dd{{margin:4px 0 0;font-size:14px;overflow-wrap:anywhere}}
      .run-step{{border-top:1px solid #e2e8f0;padding:12px 0}}.run-step summary{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;cursor:pointer;font-size:13px;list-style:none}}
      .run-step summary:before{{content:'▸';color:#64748b}}.run-step[open] summary:before{{content:'▾'}}
      .run-step-name{{font-weight:600;flex:1;min-width:160px}}.run-step-duration{{font-variant-numeric:tabular-nums}}
      .run-step-detail{{margin:12px 0 0 20px;color:#475569;font-size:13px;line-height:1.8}}
      .run-step-links{{display:flex;flex-wrap:wrap;gap:12px}}</style>
      <p class="form-help">서울 시간 (Asia/Seoul). 전체 소요는 대기와 메일 전달 대기를 포함합니다.
      완료 단계의 소요는 저장된 측정값이며, 진행 중 경과 시간은 관찰 시각 기준입니다. 단계를 펼쳐 집계와 로그를 확인하세요.</p>
      <dl class="run-times">{fact_html}</dl>{notice}{''.join(rows)}</div>'''


def render_run_audit_events(events, meta=None):
    meta = meta or {"total_count": len(events), "omitted_count": 0}
    rows = []
    for event in events:
        event_type = event.get("event_type")
        label = AUDIT_LABELS.get(event_type, "기타 관리 이벤트") if isinstance(event_type, str) else "기타 관리 이벤트"
        code = event_type if isinstance(event_type, str) and event_type in AUDIT_LABELS else "other"
        summary = " · ".join(_summary_items(event.get("details"))) or "—"
        rows.append(f'<tr><td class="mono">{format_seoul(event.get("occurred_at"))}</td>'
                    f'<td><strong>{label}</strong><br><small>{code}</small></td><td>{summary}</td></tr>')
    omitted = meta.get("omitted_count", 0)
    note = f' · 최근 {len(events)}건 표시, 이전 {omitted}건 생략' if type(omitted) is int and omitted > 0 else ""
    return f'''<div class="card" id="run-audit-events"><div class="card-header"><div class="card-title">관리 기록 ({len(events)})</div></div>
      <p class="form-help">실행 등록·Worker 인수·취소·전달 요청의 관리 기록입니다. 단계별 진행 기록은 위 실행 시간표에 표시합니다.{note}</p>
      <div style="overflow-x:auto"><table><thead><tr><th>서울 시각</th><th>관리 이벤트</th><th>집계</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="3">관리 이벤트 미기록</td></tr>'}</tbody></table></div></div>'''
