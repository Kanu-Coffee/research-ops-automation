"""Task-first overview and compact, responsive list views."""

from urllib.parse import urlencode
from datetime import datetime
from zoneinfo import ZoneInfo

from researchops.web.layout import can_write, is_admin, owner_note


def _helpers():
    from researchops.web.views import _escape, _badge, render_base_layout
    return _escape, _badge, render_base_layout


def _date(value):
    e, _, _ = _helpers()
    if not value:
        return '—'
    try:
        instant = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if instant.tzinfo is None:
            return e(value)
        local = instant.astimezone(ZoneInfo('Asia/Seoul'))
        exact = e(local.isoformat(timespec='seconds'))
        return f'<time datetime="{exact}" title="{exact}">{local.strftime("%Y.%m.%d %H:%M")}</time>'
    except (ValueError, OverflowError):
        return e(value)


def _phase(value):
    from researchops.web.timeline_views import PHASE_LABELS
    e, _, _ = _helpers()
    return e(PHASE_LABELS.get(value, value or '—'))


def _pagination(path, pagination, filters):
    if not pagination:
        return ""
    e, _, _ = _helpers()
    page, size, total = (max(1, int(pagination.get("page", 1))),
                         max(1, int(pagination.get("page_size", 25))), int(pagination.get("total", 0)))
    pages = max(1, (total + size - 1) // size)
    query = {key: value for key, value in (filters or {}).items() if value}
    def link(number, text):
        return f'<a class="btn btn-secondary" href="{e(path + "?" + urlencode({**query, "page": number}))}">{text}</a>'
    return ('<nav class="pagination" aria-label="목록 페이지">'
        + (link(page - 1, '이전') if page > 1 else '<span></span>')
        + f'<span>총 {total:,}개 · {page} / {pages}쪽</span>'
        + (link(page + 1, '다음') if page < pages else '<span></span>') + '</nav>')


def _run_rows(runs, *, compact=False):
    e, badge, _ = _helpers()
    rows = []
    for run in runs:
        rid, tid = e(run.get('run_id')), e(run.get('task_id'))
        name = e(run.get('task_name') or run.get('task_id'))
        rows.append(f'''<tr><td class="primary-cell" data-label="Task"><a class="table-link" href="/runs/{rid}">{name}</a>
          <div class="form-help mono">{rid}</div>{owner_note(run)}</td><td data-label="상태">{badge(run.get('status', ''))}</td>
          <td data-label="현재 단계">{_phase(run.get('phase'))}</td>
          <td data-label="시작 (서울)">{_date(run.get('started_at') or run.get('created_at'))}</td>
          <td class="actions-cell"><a class="btn btn-secondary btn-sm" href="/runs/{rid}">상세 보기</a></td></tr>''')
    return ''.join(rows) or '<tr><td colspan="5"><div class="empty-state"><strong>실행 이력이 없습니다.</strong>선택한 조건을 바꾸거나 Task에서 실행을 시작하세요.</div></td></tr>'


def _run_table(runs):
    return '<div class="table-scroll"><table class="responsive-list"><thead><tr><th>Task / 실행</th><th>상태</th><th>현재 단계</th><th>시작 (서울)</th><th>작업</th></tr></thead><tbody>' + _run_rows(runs) + '</tbody></table></div>'


def dashboard(doctor_data, tasks, recent_runs, flash=None, operating_state=None, dashboard_data=None):
    e, badge, layout = _helpers()
    data = dashboard_data or {}
    counts = data.get('counts', {})
    attention = counts.get('attention', sum(run.get('status') in {'failed', 'needs_attention', 'timed_out'} for run in recent_runs))
    running = counts.get('running', sum(run.get('status') in {'queued', 'running', 'awaiting_receipt'} for run in recent_runs))
    scheduled = counts.get('scheduled', sum(bool(task.get('enabled')) for task in tasks))
    completed = counts.get('completed', sum(run.get('status') == 'succeeded' for run in recent_runs))
    stats = ''.join(f'<a href="{href}" class="stat-card" style="text-decoration:none;color:inherit"><div class="stat-label">{label}</div><div class="stat-value">{int(value):,}</div></a>'
        for label, value, href in [('확인 필요', attention, '/runs?status=attention'), ('진행 중', running, '/runs?status=active'),
                                  ('예약 중인 Task', scheduled, '/tasks?status=scheduled'), ('최근 완료', completed, '/runs?status=succeeded')])
    next_rows = ''.join(f'<tr><td class="primary-cell"><a class="table-link" href="/tasks/{e(item.get("task_id"))}">{e(item.get("name") or item.get("task_id"))}</a></td><td data-label="예약 (서울)">{_date(item.get("scheduled_for") or item.get("next_scheduled_for"))}</td></tr>'
        for item in data.get('upcoming', []))
    next_html = ('<table class="responsive-list"><thead><tr><th>Task</th><th>예약 (서울)</th></tr></thead><tbody>' + next_rows + '</tbody></table>'
                 if next_rows else '<p class="muted">예정된 실행이 없습니다.</p>')
    quick = '<a class="btn btn-primary" href="/tasks/new">Task 만들기</a>' if can_write() else ''
    from researchops.web.views import _operating_notice
    start = ''
    if not tasks and can_write():
        settings = '<a class="btn btn-secondary" href="/delivery">메일 설정</a>'
        start = f'<section class="card"><h2>첫 Task 시작하기</h2><p class="muted">조사 내용과 메일 기준을 설정하세요.</p><div class="actions">{settings}{quick}</div></section>'
    body = f'''<div class="page-heading"><div><h1>대시보드</h1><p>실행 현황과 다음 일정을 확인하세요.</p></div>{quick}</div>
      {_operating_notice(operating_state)}<div class="grid-stats">{stats}</div>{start}
      <section class="card"><div class="card-header"><h2 class="card-title">최근 실행</h2><a class="table-link" href="/runs">전체 보기 →</a></div>{_run_table(data.get('recent', recent_runs))}</section>
      <section class="card"><div class="card-header"><h2 class="card-title">다음 예약</h2><a class="table-link" href="/tasks?status=scheduled">예약 Task 보기 →</a></div>{next_html}</section>'''
    return layout('대시보드', body, 'dashboard', flash)


def tasks_list(tasks, flash=None, *, deleted=False, pagination=None, filters=None):
    e, badge, layout = _helpers()
    filters = filters or {}
    rows = []
    from researchops.web.catalog_views import lifecycle_control
    for task in tasks:
        tid, name = e(task.get('task_id')), e(task.get('name') or task.get('display_name') or task.get('task_id'))
        removed = bool(task.get('deleted_at'))
        version = task.get('active_version_hash')
        state = '삭제됨' if removed else '적용된 버전 없음' if not version else '예약 중' if task.get('enabled') else '수동 실행'
        latest = badge(task.get('latest_status', '')) if task.get('latest_status') else '<span class="muted">실행 전</span>'
        if task.get('latest_run_id'):
            latest = f'<a class="table-link" href="/runs/{e(task["latest_run_id"])}">{latest}</a>'
        actions = ''
        if can_write():
            if removed:
                actions = lifecycle_control(f'/tasks/{task["task_id"]}/restore', restore=True)
            else:
                actions = f'<form method="POST" action="/tasks/{tid}/run"><button class="btn btn-primary btn-sm" {"disabled" if not version else ""}>지금 실행</button></form><a class="btn btn-secondary btn-sm" href="/tasks/{tid}/edit">수정</a>'
        rows.append(f'''<tr><td class="primary-cell" data-label="Task"><a class="table-link" href="/tasks/{tid}">{name}</a><div class="form-help">#{e(task.get('entity_id', ''))}</div>{owner_note(task)}</td>
          <td data-label="상태">{state}</td><td data-label="최근 실행">{latest}</td><td data-label="다음 예약">{_date(task.get('next_scheduled_for'))}</td>
          <td class="actions-cell"><div class="actions">{actions}<a class="btn btn-quiet btn-sm" href="/tasks/{tid}">상세</a></div></td></tr>''')
    q = e(filters.get('q', ''))
    options = ''.join(f'<option value="{key}" {"selected" if filters.get("status", "") == key else ""}>{label}</option>' for key, label in [('', '모든 상태'), ('scheduled', '예약 중'), ('manual', '수동 실행')])
    new = '<a class="btn btn-primary" href="/tasks/new">Task 만들기</a>' if can_write() else ''
    extras = f'<a class="btn btn-quiet" href="{"/tasks" if deleted else "/tasks?view=deleted"}">{"현재 Task" if deleted else "삭제된 Task"}</a>' if can_write() else ''
    body = f'''<div class="page-heading"><h1>{'삭제된 Task' if deleted else 'Task'}</h1>{new}</div>
      <form class="filter-bar" method="GET" action="/tasks"><div class="form-group"><label class="form-label" for="task-search">Task 검색</label><input id="task-search" class="form-input" type="search" name="q" value="{q}" placeholder="이름 또는 관리번호"></div>
      <div class="form-group"><label class="form-label" for="task-status">상태</label><select id="task-status" class="form-select" name="status">{options}</select></div>
      {'<input type="hidden" name="view" value="deleted">' if deleted else ''}<button class="btn btn-secondary">검색</button></form>
      <section class="card"><div class="table-scroll"><table class="responsive-list"><thead><tr><th>Task</th><th>상태</th><th>최근 실행</th><th>다음 예약 (서울)</th><th>작업</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="5"><div class="empty-state"><strong>표시할 Task가 없습니다.</strong>검색 조건을 확인하거나 새 Task를 만드세요.</div></td></tr>'}</tbody></table></div>{_pagination('/tasks', pagination, {**filters, **({'view':'deleted'} if deleted else {})})}</section>
      <div class="actions">{extras}</div>'''
    return layout('Task', body, 'tasks', flash)


def runs_list(runs, filter_status=None, flash=None, *, pagination=None, filters=None):
    e, _, layout = _helpers()
    filters = filters or {'status': filter_status or ''}
    options = ''.join(f'<option value="{key}" {"selected" if filters.get("status", "") == key else ""}>{label}</option>' for key, label in [('', '모든 상태'), ('attention', '확인 필요'), ('active', '진행 중·대기'), ('succeeded', '완료'), ('failed', '실패'), ('needs_attention', '운영 확인 필요'), ('awaiting_receipt', '전송 대기'), ('cancelled', '취소')])
    task_filter = f'<input type="hidden" name="task_id" value="{e(filters["task_id"])}">' if filters.get('task_id') else ''
    body = f'''<div class="page-heading"><h1>실행 이력</h1></div>
      <form class="filter-bar" method="GET" action="/runs"><div class="form-group"><label class="form-label" for="run-search">실행 검색</label><input id="run-search" class="form-input" type="search" name="q" value="{e(filters.get('q',''))}" placeholder="Task 이름 또는 실행 ID"></div>
      <div class="form-group"><label class="form-label" for="run-status">상태</label><select id="run-status" class="form-select" name="status">{options}</select></div>{task_filter}<button class="btn btn-secondary">검색</button></form>
      <section class="card">{_run_table(runs)}{_pagination('/runs', pagination, filters)}</section>'''
    return layout('실행 이력', body, 'runs', flash)
