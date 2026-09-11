"""Preserved-message retry controls and safe SMTP attempt history."""

from datetime import datetime
import html
from uuid import uuid4
from zoneinfo import ZoneInfo


STATUS_LABELS = {"queued": "전송 대기", "sending": "전송 중", "smtp_accepted": "SMTP 서버 수락",
                 "connection_ok": "연결 확인", "failed": "전송 실패", "uncertain": "전달 결과 불확실"}
BLOCK_LABELS = {
    "SMTP_DISPATCHER_UNAVAILABLE": "메일 전송 서비스를 사용할 수 없습니다.",
    "SMTP_ALREADY_ACCEPTED": "이미 SMTP 서버가 수락한 이메일입니다.",
    "SMTP_DELIVERY_UNCERTAIN": "전달 여부가 불확실하여 자동 재전송하지 않습니다.",
    "SMTP_RETRY_PENDING": "이미 진행 중이거나 대기 중인 전송 시도가 있습니다.",
    "SMTP_RETRY_NOT_ELIGIBLE": "현재 상태에서는 이메일만 재전송할 수 없습니다.",
    "Handoff was not found": "이메일 전달 정보를 찾을 수 없습니다.",
    "No SMTP attempt exists yet": "아직 SMTP 전송 시도가 없습니다.",
    "SMTP server already accepted this email": "이미 SMTP 서버가 수락한 이메일입니다.",
    "Email transmission is in progress": "이메일 전송이 진행 중입니다.",
    "Email is already queued for immediate dispatch": "이메일이 이미 전송 대기 중입니다.",
    "System alerts do not support email retry": "시스템 알림에는 이메일 재시도를 제공하지 않습니다.",
    "SMTP delivery is uncertain; another attempt may deliver a duplicate": "전달 여부가 불확실하여 자동 재전송하지 않습니다.",
    "Email snapshot could not be verified": "보존된 이메일 원문을 검증할 수 없습니다.",
}


def _e(value):
    return html.escape(str(value if value is not None else ""))


def _seoul(value):
    if not isinstance(value, str) or len(value) > 50:
        return "—"
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return "—"
        return instant.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError):
        return "—"


def render_email_retry(status, handoff_id, *, run_status=None):
    if not isinstance(status, dict) or not handoff_id:
        return ""
    attempts = status.get("attempts", [])
    attempts = [attempt for attempt in attempts if isinstance(attempt, dict)] if isinstance(attempts, list) else []
    accepted = (run_status == "succeeded" or status.get("status") == "smtp_accepted"
                or any(attempt.get("status") == "smtp_accepted" for attempt in attempts))
    uncertain = status.get("uncertain") is True
    eligible = status.get("eligible") is True and not uncertain and not accepted
    manual_uncertain = status.get("can_retry_uncertain") is True and uncertain and not accepted
    request_key = "email-retry-" + uuid4().hex
    action = "/handoffs/" + _e(handoff_id) + "/retry-email"
    form = ""
    if eligible or manual_uncertain:
        fields = f'<input type="hidden" name="request_key" value="{request_key}">'
        if manual_uncertain:
            fields += '''<p style="color:#92400e; margin:12px 0;">이전 전송이 이미 도착했을 수 있습니다.
                수신 여부를 확인한 뒤 재전송을 선택하세요.</p>
                <label style="display:block; margin-bottom:12px;"><input type="checkbox" name="allow_uncertain" value="true" required>
                이전 메일의 중복 발송 위험을 확인하고 원문 재전송을 요청합니다.</label>
                <label class="form-label" for="email-retry-reason">확인 내용 · 재전송 사유</label>
                <textarea id="email-retry-reason" name="reason" class="form-textarea" maxlength="500" required
                    style="min-height:80px;" placeholder="수신 여부 확인 내용과 재전송 사유를 입력하세요."></textarea>'''
        form = (f'<form method="POST" action="{action}" id="email-retry-form">{fields}'
                '<button type="submit" class="btn btn-primary" style="margin-top:12px;">작성된 이메일만 다시 전송</button></form>')
        if manual_uncertain:
            form = '<details id="uncertain-email-retry"><summary>전달 여부 확인 후 수동 재전송</summary>' + form + '</details>'
    block = status.get("block_reason")
    note = BLOCK_LABELS.get(block, block) if isinstance(block, str) else ""
    if accepted:
        note = "이미 SMTP 서버가 수락한 이메일입니다. 원문 재전송은 제공하지 않습니다."
    elif uncertain:
        note = "전달 결과가 불확실하여 자동 재전송하지 않습니다."
        if block and not manual_uncertain:
            note += " " + str(BLOCK_LABELS.get(block, block))
    next_at = status.get("next_attempt_at")
    next_info = ('<p style="margin:12px 0;"><strong>다음 전송 예정 (서울): '
                 + _seoul(next_at) + '</strong></p>') if next_at else ""
    rows = []
    for index, attempt in enumerate(attempts, 1):
        number = attempt.get("attempt_number", index)
        number = number if type(number) is int and number > 0 else index
        label = STATUS_LABELS.get(attempt.get("status"), "확인 필요")
        diagnostic = attempt.get("error") or attempt.get("error_code") or "—"
        rows.append(f'<tr><td>{number}</td><td>{label}</td><td>{_seoul(attempt.get("created_at"))}</td>'
                    f'<td>{_seoul(attempt.get("updated_at"))}</td><td>{_e(diagnostic)}</td></tr>')
    history = ('<div class="smtp-attempt-scroll" tabindex="0" role="region" aria-label="이메일 전송 시도 이력. 좁은 화면에서는 가로로 스크롤할 수 있습니다." '
               'style="overflow-x:auto; margin-top:18px;"><table style="min-width:720px; white-space:nowrap;"><thead><tr>'
               '<th>시도</th><th>결과</th><th>등록 (서울)</th><th>갱신 (서울)</th><th>진단</th>'
               '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>') if rows else ''
    return ('<section class="card" id="smtp-delivery"><div class="card-header">'
            '<h2 class="card-title">이메일 전송 · 재시도</h2></div>'
            '<p class="form-help" style="margin-bottom:12px;">보존된 메일 원문과 첨부를 사용합니다. 조사와 메일 작성 모델을 다시 실행하지 않습니다.</p>'
            + (f'<p class="form-help" role="status">{_e(note)}</p>' if note else "")
            + next_info + form + history + '</section>')


def render_prepared_email(status, run_id, *, run_status=None, error=None, request_key=None):
    """Send validated archived mail when no SMTP handoff was created yet."""
    state = status if isinstance(status, dict) else {}
    eligible = state.get("eligible") is True and run_status not in {"queued", "running", "awaiting_receipt"}
    reason = state.get("block_reason")
    note = reason if isinstance(reason, str) and reason else (
        "작성된 이메일과 첨부 파일을 사용할 수 있습니다." if eligible else
        "전송할 수 있는 확정 이메일 원문이 아직 없습니다.")
    source = state.get("source_run_id")
    revision = state.get("source_revision")
    source_note = ""
    if isinstance(source, str) and source:
        source_note = f'<p class="form-help">작성 원본: <a href="/runs/{_e(source)}">{_e(source)}</a>'
        if type(revision) is int and revision > 0:
            source_note += f' · 작성 revision {revision}'
        source_note += '</p>'
    form = ""
    if eligible or (error and request_key):
        key = request_key or "email-send-" + uuid4().hex
        form = f'''<form method="POST" action="/runs/{_e(run_id)}/send-email" id="prepared-email-form">
          <input type="hidden" name="request_key" value="{_e(key)}">
          <button type="submit" class="btn btn-primary" style="margin-top:12px;"{'' if eligible else ' disabled aria-describedby="prepared-email-block-reason"'}>작성된 이메일 보내기</button>
          {'' if eligible else '<p class="form-help">발송 조건을 확인한 뒤 <a href="/runs/' + _e(run_id) + '#smtp-delivery">전송 가능 여부를 다시 확인</a>하세요.</p>'}
        </form>'''
    alert = (f'<p id="prepared-email-error" role="alert" tabindex="-1" style="color:#991b1b; margin:12px 0;">{_e(error)}</p>'
             '<script>document.getElementById("prepared-email-error").focus();</script>') if error else ""
    return (f'<section class="card" id="smtp-delivery" aria-labelledby="prepared-email-title">'
            '<div class="card-header"><h2 class="card-title" id="prepared-email-title">이메일 전송</h2></div>'
            '<p class="form-help">보존된 이메일 제목·본문·첨부를 그대로 전송합니다. Research와 Compose 모델을 호출하지 않습니다.</p>'
            + source_note + f'<p class="form-help" role="status" id="prepared-email-block-reason">{_e(note)}</p>' + alert + form + '</section>')
