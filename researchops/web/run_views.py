"""Run detail tabs with bounded rendering and refreshable selected content."""

import json
from urllib.parse import quote

from researchops.web.layout import can_write, visible_controls
from researchops.web.formatting import render_size


TABS = [('overview', '개요'), ('email', '이메일'), ('files', '파일'), ('logs', '로그')]


def _e(value):
    from researchops.web.views import _escape
    return _escape(value)


def _files(run_id, artifacts, page=1, *, logs=False):
    selected = [item for item in artifacts if str(item.get('filename', '')).startswith('logs/')] if logs else artifacts
    start = (page - 1) * 100
    rows = []
    for artifact in selected[start:start + 100]:
        name = str(artifact.get('filename', ''))
        rows.append(f'<tr><td class="primary-cell"><code>{_e(name)}</code></td><td data-label="크기">{render_size(artifact.get("size_bytes"))}</td>'
            f'<td class="actions-cell"><a class="btn btn-secondary btn-sm" href="/runs/{run_id}/artifacts/{_e(quote(name, safe="/"))}" target="_blank" rel="noopener">{"원문 보기" if logs else "다운로드"}</a></td></tr>')
    pagination = ''
    if len(selected) > 100:
        from researchops.web.list_views import _pagination
        pagination = _pagination(f'/runs/{run_id}', {'page': page, 'page_size': 100, 'total': len(selected)}, {'tab': 'logs' if logs else 'files'})
    return ('<section class="card"><div class="card-header"><h2 class="card-title">' + ('원본 로그' if logs else '보관 파일')
        + f' ({len(selected)})</h2></div><div class="table-scroll"><table class="responsive-list"><thead><tr><th>파일</th><th>크기</th><th>작업</th></tr></thead><tbody>'
        + (''.join(rows) or '<tr><td colspan="3"><p class="empty-state">아직 보관된 파일이 없습니다.</p></td></tr>') + '</tbody></table></div>' + pagination + '</section>')


def _email(run_data, run_id, *, send_email_error=None, send_email_request_key=None):
    from researchops.web.smtp_views import render_email_retry, render_prepared_email
    run, handoff, comp = run_data.get('run', {}), run_data.get('handoff'), run_data.get('composition')
    if handoff and handoff.get('handoff_id'):
        delivery = render_email_retry(run_data.get('email_retry'), handoff['handoff_id'], run_status=run.get('status'))
        if send_email_error:
            delivery = '<div id="prepared-email-error" class="alert alert-error" role="alert" tabindex="-1">' + _e(send_email_error) + '</div>' + delivery
        retry_state = run_data.get('email_retry') or {}
        if (can_write() and retry_state.get('can_republish') is True and
                retry_state.get('uncertain') is not True and handoff.get('status') != 'uncertain'):
            delivery += (f'<details class="card"><summary>전달 요청 복구</summary><form method="POST" '
                f'action="/handoffs/{_e(handoff["handoff_id"])}/republish"><button class="btn btn-secondary" type="submit">'
                '보존된 전달 요청 다시 게시</button></form></details>')
    else:
        delivery = render_prepared_email(run_data.get('prepared_email'), run.get('run_id'), run_status=run.get('status'),
            error=send_email_error, request_key=send_email_request_key)
    if comp:
        preview = f'''<section class="card"><div class="card-header"><h2 class="card-title">{_e(comp.get('subject') or '작성된 이메일')}</h2>
          <div class="actions"><a class="btn btn-secondary btn-sm" href="/runs/{run_id}/preview/html" target="_blank" rel="noopener">새 창에서 보기</a>
          <a class="btn btn-secondary btn-sm" href="/runs/{run_id}/preview/text" target="_blank" rel="noopener">텍스트 보기</a></div></div>
          <iframe title="작성된 이메일 미리보기" src="/runs/{run_id}/preview/html" sandbox="" loading="lazy" style="width:100%;height:520px;border:1px solid #dce3ed;border-radius:6px;background:#fff"></iframe></section>'''
    else:
        preview = '<section class="card"><p class="empty-state">아직 작성된 이메일이 없습니다.</p></section>'
    evidence = ''
    if handoff:
        evidence = '<details class="card" id="run-delivery-results"><summary>전달 상세</summary><dl>'
        for label, key in [('수신자 그룹', 'recipient_group_id'), ('전달 상태', 'status'), ('외부 결과', 'external_delivery_status'), ('중복 방지 키', 'idempotency_key')]:
            evidence += f'<dt class="muted">{label}</dt><dd class="mono">{_e(handoff.get(key, "—"))}</dd>'
        evidence += '</dl></details>'
    return delivery + preview + evidence


def _log_reader(run_id, artifacts):
    allowed = {'research.stdout', 'research.stderr', 'compose.stdout', 'compose.stderr'}
    names = [str(item.get('filename', '')).removeprefix('logs/') for item in artifacts
             if str(item.get('filename', '')).startswith('logs/') and str(item.get('filename', '')).removeprefix('logs/') in allowed]
    if not names:
        return ''
    options = ''.join(f'<option value="{_e(name)}">{_e(name)}</option>' for name in names)
    return f'''<section class="card" data-log-reader data-run-id="{run_id}"><h2 class="card-title">원본 로그 읽기</h2>
      <p class="form-help">최대 65.5 KB씩 표시합니다. 전체 내용은 아래 원문 링크에서 확인하세요.</p>
      <div class="filter-bar"><div class="form-group"><label class="form-label" for="raw-log-file">로그 파일</label><select id="raw-log-file" class="form-select" data-log-file>{options}</select></div><button type="button" class="btn btn-secondary" data-load-log>로그 불러오기</button></div>
      <p class="form-help" data-log-range role="status">아직 불러오지 않았습니다.</p>
      <details hidden><summary>정확한 바이트 위치</summary><p class="mono" data-log-exact></p></details>
      <pre tabindex="0" aria-label="선택한 원본 로그의 일부" data-log-content hidden></pre>
      <button type="button" class="btn btn-secondary" data-next-log disabled>다음 부분</button></section>'''


def render_detail(run_data, artifacts, events, flash=None, *, model_catalog=None, retry_values=None, current_task=None,
                  send_email_error=None, send_email_request_key=None, tab=None, partial=False):
    from researchops.web.context import get_query
    from researchops.web.views import _badge, _render_research_summary, render_base_layout
    query = get_query()
    tab = tab or query.get('tab') or ('email' if send_email_error else 'overview')
    if tab not in {key for key, _ in TABS}:
        tab = 'overview'
    from researchops.web.list_views import _phase
    run = run_data.get('run', {})
    rid, tid = _e(run.get('run_id')), _e(run.get('task_id'))
    status = run.get('status', '')
    try:
        page = max(1, min(100000, int(query.get('page', '1'))))
    except (ValueError, TypeError):
        page = 1
    actions = ''
    if can_write():
        if status in {'queued', 'running', 'preflight', 'research', 'validate', 'dedupe', 'compose', 'validate_message', 'handoff'}:
            actions += f'<form method="POST" action="/runs/{rid}/cancel"><button type="submit" class="btn btn-danger" onclick="return confirm(\'이 실행을 취소할까요?\');">실행 취소</button></form>'
        if isinstance(run_data.get('prepared_email'), dict) and run_data['prepared_email'].get('eligible'):
            actions += f'<a class="btn btn-primary" href="/runs/{rid}?tab=email#smtp-delivery">작성된 이메일 보내기</a>'
    header = f'''<section class="card"><div class="card-header"><h2 class="card-title">{_e(run.get('task_name') or run.get('task_id'))}</h2>{_badge(status)}</div>
      <div class="run-facts"><span>Task <a class="table-link" href="/tasks/{tid}">{tid}</a></span><span>업무일 {_e(run.get('local_date', '—'))}</span><span>시도 {_e(run.get('attempt', 0))}</span><span>현재 단계 {_phase(run.get('phase'))}</span></div>
      <details><summary class="muted">실행 식별 정보</summary><code>{rid}</code></details>
      {'<div class="alert alert-error" role="alert">' + _e(run.get('error_message')) + '</div>' if run.get('error_message') else ''}<div class="actions">{actions}</div></section>'''
    panel = ''
    if tab == 'overview':
        from researchops.web.timeline_views import render_run_timeline
        from researchops.web.ai_settings import render_execution_history, render_retry_panel
        timeline = render_run_timeline(run_data.get('timeline'), run, artifacts,
            mcp=bool(run_data.get('mcp_audit')), artifact_report=bool(run_data.get('artifact_report')), response=bool(run_data.get('response_diagnostics')))
        for anchor, destination in [('mcp-call-results', 'logs'), ('response-diagnostics', 'logs'), ('requested-artifact-report', 'files'), ('smtp-delivery', 'email')]:
            timeline = timeline.replace(f'href="#{anchor}"', f'href="/runs/{rid}?tab={destination}#{anchor}"')
        panel += timeline
        research = run_data.get('research')
        if isinstance(research, dict):
            count = research.get('record_count')
            count = str(count) if type(count) is int and 0 <= count < 2**63 else '미기록'
            panel += ('<section class="card" id="research-stage-results"><div class="card-header"><h2 class="card-title">조사 결과</h2>'
                + f'<span class="muted">{count}개 항목</span></div>' + _render_research_summary(research.get('summary', ''))
                + '<details class="research-coverage"><summary>조사 범위 상세</summary><pre>' + _e(json.dumps(research.get('coverage', {}), ensure_ascii=False, indent=2)) + '</pre></details></section>')
        scope = (run_data.get('execution_plan') or {}).get('scope')
        if scope == 'delivery_only':
            source = ((run_data.get('prepared_email') or {}).get('source_run_id')
                or ((run_data.get('execution_plan') or {}).get('source_message') or {}).get('run_id') or run.get('parent_run_id'))
            source_link = f'<p class="form-help">작성 원본: <a href="/runs/{_e(source)}">{_e(source)}</a></p>' if source else ''
            panel += '<section class="card"><h2 class="card-title">이메일 전송 전용 · AI 실행 없음</h2><p class="form-help">작성된 이메일 원문과 첨부를 사용합니다.</p>' + source_link + '</section>'
        else:
            panel += '<details class="card"><summary>사용한 AI 설정</summary>' + render_execution_history(run_data.get('execution_settings'), run_data.get('execution_plan')) + '</details>'
            if can_write():
                retry = render_retry_panel(run_data, model_catalog, values=retry_values, current_task=current_task)
                if retry:
                    panel += '<details class="card" id="change-email-content"' + (' open' if retry_values or (flash and flash.get('type') == 'error') else '') + '><summary>설정 확인 후 재실행</summary>' + retry + '</details>'
    elif tab == 'email':
        panel = _email(run_data, rid, send_email_error=send_email_error, send_email_request_key=send_email_request_key)
    elif tab == 'files':
        from researchops.web.artifact_views import render_run_artifact_report
        panel = render_run_artifact_report(run_data.get('artifact_report'), run.get('run_id', '')) + _files(rid, artifacts, page)
    else:
        from researchops.web.response_views import render_run_response_diagnostics
        from researchops.web.mcp_views import render_mcp_run_audit
        from researchops.web.timeline_views import render_run_audit_events
        panel = _log_reader(rid, artifacts)
        panel += render_run_response_diagnostics(run_data.get('response_diagnostics'))
        panel += render_mcp_run_audit(run_data.get('mcp_audit'))
        raw_events = run_data.get('audit_events', events) or []
        search = str(query.get('q', '')).strip()
        phase = str(query.get('phase', '')).strip()
        selected = [event for event in raw_events if (not search or search.casefold() in json.dumps(event, ensure_ascii=False, default=str).casefold()) and
            (not phase or phase == str(event.get('phase') or (event.get('details') or {}).get('phase', '')))]
        panel += f'''<form class="filter-bar" method="GET" action="/runs/{rid}"><input type="hidden" name="tab" value="logs"><div class="form-group"><label class="form-label" for="log-search">표시된 관리 기록 검색</label><input id="log-search" class="form-input" type="search" name="q" value="{_e(search)}"></div><button class="btn btn-secondary">검색</button></form>'''
        if search or phase:
            panel += f'<p class="form-help">현재 불러온 관리 기록 {len(raw_events)}건 중 {len(selected)}건이 일치합니다. 원본 로그 전체를 검색한 결과가 아닙니다.</p>'
        panel += render_run_audit_events(selected[(page - 1) * 100:page * 100], run_data.get('audit_events_meta')).replace('단계별 진행 기록은 위 실행 시간표에 표시합니다.', '단계별 진행 기록은 개요 탭에서 확인하세요.')
        if len(selected) > 100:
            from researchops.web.list_views import _pagination
            panel += _pagination(f'/runs/{rid}', {'page': page, 'page_size': 100, 'total': len(selected)}, {'tab': 'logs', 'q': search, 'phase': phase})
        panel += _files(rid, artifacts, 1, logs=True)
    refresh_active = status in {'queued', 'running', 'awaiting_receipt', 'preflight', 'research', 'validate', 'dedupe', 'compose', 'validate_message', 'handoff'} or (run_data.get('email_retry') or {}).get('status') in {'queued', 'sending'}
    panel_html = visible_controls(f'<div data-run-view data-refresh-active="{str(refresh_active).lower()}" data-run-status="{_e(status)}" data-run-tab="{tab}">{header}<section id="run-panel-{tab}" aria-label="{dict(TABS)[tab]}">{panel}</section></div>')
    if partial:
        return panel_html
    nav = ''.join(f'<a href="/runs/{rid}?tab={key}"' + (' aria-current="page"' if tab == key else '') + f'>{label}</a>' for key, label in TABS)
    body = f'''<a class="back-link" href="/runs">← 실행 이력</a><div class="page-heading"><div><h1>실행 상세</h1></div></div>
      <nav class="tabs" aria-label="실행 상세 영역">{nav}</nav>
      <div class="actions" style="margin-bottom:14px"><button class="btn btn-quiet btn-sm" type="button" data-refresh-now>새로고침</button><button class="btn btn-quiet btn-sm" type="button" data-pause-refresh aria-pressed="false">자동 갱신 일시정지</button><span class="run-refresh-status" data-refresh-status role="status"></span></div>{panel_html}'''
    if (tab == 'overview' and can_write()
            and (run_data.get('execution_plan') or {}).get('scope') != 'delivery_only'
            and 'window.researchopsInitAISettings' not in body):
        from researchops.web.ai_settings import AI_SCRIPT
        body += AI_SCRIPT
    return render_base_layout('실행 상세', body, 'runs', flash)
