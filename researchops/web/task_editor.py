"""Task creation steps and focused editing tabs, preserving application contracts."""

import html
import json
from typing import Any

from researchops.web.schedule_form import PRESETS, WEEKDAYS, schedule_fields, schedule_summary
from researchops.web.task_editor_assets import EDITOR_STYLE, EDITOR_SCRIPT, FIELD_SCRIPT


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _options(values: dict[str, str], selected: str) -> str:
    return "".join(f'<option value="{_e(key)}"{" selected" if key == selected else ""}>{_e(label)}</option>'
                   for key, label in values.items())


def task_editor_body(values: dict | None = None, *, mode: str = "create", recipient_groups=None,
                     sender_profiles=None, recipient_names=None, sender_names=None, notice: str = "",
                     model_catalog=None) -> str:
    from researchops.web.ai_settings import render_ai_controls, stage_settings, settings_summary
    from researchops.web.views import OPERATING_LABELS
    labels_json = json.dumps(OPERATING_LABELS, ensure_ascii=False).replace("<", "\\u003c")
    value = dict(values or {})
    editing, cloning = mode == "edit", mode == "clone"
    wizard = not editing
    task_id = str(value.get("task_id", ""))
    panel_scope = f"&amp;task_id={_e(task_id)}" if editing else ""
    cron = str(value.get("cron", "0 9 * * *"))
    schedule = schedule_fields(cron)
    schedule.update({key: str(value[key]) for key in schedule if key in value})
    groups = {key: addresses for key, addresses in (recipient_groups or {}).items() if addresses}
    selected_group = value.get("recipient_group_id", "")
    routing_mode = value.get("recipient_routing_mode") or ("legacy_ids" if editing or cloning or selected_group else "catalog_name")
    catalog_routing = routing_mode == "catalog_name"
    catalog_names = ''.join(f'<li>{_e((recipient_names or {}).get(key, key))}</li>' for key in groups)
    group_options = {key: f"{(recipient_names or {}).get(key, key)} ({len(addresses)}명)" for key, addresses in groups.items()}
    if selected_group and selected_group not in group_options:
        group_options[selected_group] = f"{selected_group} (수신자 설정 확인 필요)"
    sender_options = {key: (sender_names or {}).get(key, key) for key in (sender_profiles or {})}
    if sender_names is None:
        sender_options.setdefault("default", "기본 발신 계정")
    selected_sender = value.get("sender_profile_id", "default")
    if selected_sender not in sender_options:
        sender_options[selected_sender] = selected_sender + " (설정 확인 필요)"
    form_action = f"/tasks/{_e(task_id)}/edit" if editing else "/tasks/production/create"
    title = "Task 수정" if editing else "Task 복제" if cloning else "새 Task"
    source_fields = (f'<input type="hidden" name="source_task_id" value="{_e(value.get("source_task_id", ""))}">'
                     f'<input type="hidden" name="source_version_hash" value="{_e(value.get("source_version_hash", ""))}">') if cloning else ""
    guard = (f'<input type="hidden" name="expected_version_hash" value="{_e(value.get("expected_version_hash", ""))}">'
             f'<input type="hidden" name="expected_updated_at" value="{_e(value.get("expected_updated_at", ""))}">') if editing else ""
    schedule_enabled = value.get("schedule_enabled", False) in (True, "true")
    launch_mode = value.get("launch_mode") or ("run" if value.get("action") == "create_and_run" else "save")
    multi_note = ('<p class="form-help">기존 복수 그룹 규칙 유지: '
                  + _e(", ".join(value.get("recipient_group_ids", []))) + '</p>') if len(value.get("recipient_group_ids", [])) > 1 else ""
    disabled = "" if groups else "disabled"
    if editing:
        actions = f'<a class="btn btn-secondary" href="/tasks/{_e(task_id)}">취소</a><button type="submit" class="btn btn-primary" data-task-publish {disabled}>변경사항 저장</button>'
    else:
        actions = (f'<button type="button" class="btn btn-secondary" data-editor-back hidden>이전</button>'
                   '<button type="button" class="btn btn-primary" data-editor-next hidden>다음</button>'
                   f'<button type="submit" name="action" value="create" class="btn btn-primary" data-editor-submit data-task-publish {disabled}>Task 저장</button>')
    tabs = [("research", "조사 내용" if wizard else "조사"), ("email", "메일·수신자"), ("execution", "실행·일정")]
    if wizard:
        tabs.append(("review", "확인"))
    navigation = ''.join(f'<button type="button" id="editor-tab-{key}" role="tab" aria-controls="editor-panel-{key}" aria-selected="false" tabindex="-1" data-editor-tab="{key}">{f"<span>{i + 1}</span>" if wizard else ""}{label}</button>' for i, (key, label) in enumerate(tabs))
    schedule_toggle = (f'<label class="editor-toggle"><input type="checkbox" name="schedule_enabled" value="true" {"checked" if schedule_enabled else ""}> 이 일정으로 자동 실행·발송</label>') if not wizard else '<p class="form-help">예약 시작 여부는 마지막 확인 단계에서 선택합니다.</p>'
    launch_choices = ''.join(f'<label class="launch-choice"><input type="radio" name="launch_mode" value="{key}" {"checked" if launch_mode == key else ""}><span><strong>{label}</strong><small>{help_text}</small></span></label>' for key, label, help_text in [
        ("save", "저장만", "Task를 저장합니다. 실행과 예약은 꺼져 있습니다."),
        ("run", "지금 실행·발송", "저장 후 조사를 시작하고 이메일을 보냅니다. 반복 예약은 꺼져 있습니다."),
        ("schedule", "예약 시작", "저장한 일정부터 자동으로 조사·발송합니다."),
    ])
    review = f'''<section id="editor-panel-review" role="tabpanel" aria-labelledby="editor-tab-review" data-editor-panel="review" tabindex="-1">
        <h2>설정 확인</h2><dl class="editor-review"><dt>Task</dt><dd data-editor-summary="name">{_e(value.get('name')) or '이름 입력 필요'}</dd>
        <dt>발신 계정</dt><dd data-editor-summary="sender"></dd><dt>수신자</dt><dd data-editor-summary="recipient"></dd>
        <dt>일정</dt><dd data-editor-summary="schedule">{_e(schedule_summary(cron))}</dd><dt>AI</dt><dd data-ai-save-summary>{_e(settings_summary(stage_settings(value)))}</dd></dl>
        <fieldset class="launch-choices"><legend>저장 후 할 일</legend>{launch_choices}</fieldset>
      </section>''' if wizard else ""
    return f'''{EDITOR_STYLE}
    <div class="task-editor"><a href="/tasks" class="table-link editor-back-link">Task 목록</a>
    <h1>{title}</h1><div id="editor-operating-status">{notice}</div>
    <form method="POST" action="{form_action}" id="task-editor-form" data-editor-mode="{'wizard' if wizard else 'tabs'}" data-has-groups="{'true' if groups else 'false'}">
      {guard}{source_fields}<input type="hidden" name="task_id" value="{_e(task_id) if editing else ''}">
      <div class="editor-tabs" role="tablist" aria-label="{'Task 생성 단계' if wizard else 'Task 설정'}" hidden>{navigation}</div>
      <p id="editor-validation" role="alert" tabindex="-1" hidden></p>
      <div class="editor-grid"><div class="editor-main">
        <section id="editor-panel-research" role="tabpanel" aria-labelledby="editor-tab-research" data-editor-panel="research" tabindex="-1">
          <div class="form-group"><label for="task-name" class="form-label">Task 이름</label>
            <input id="task-name" name="name" class="form-input" maxlength="200" value="{_e(value.get('name', ''))}" required autocomplete="off"></div>
          <div class="form-group"><div class="editor-label-row"><label class="form-label" for="task-md">조사 지시</label>
            <label class="btn btn-secondary btn-sm editor-import" for="task-file">파일 불러오기<input id="task-file" type="file" accept=".md,.txt,text/plain,text/markdown" data-import-target="task-md"></label></div>
            <textarea id="task-md" name="task_md" class="form-textarea" maxlength="100000" required placeholder="조사 주제, 대상, 기간, 출처와 비교 항목을 작성하세요.">{_e(value.get('task_md', value.get('instructions', '')))}</textarea>
            <p class="form-help">비밀번호·실제 이메일 주소는 설정에서 관리하세요.</p></div>
        </section>
        <section id="editor-panel-email" role="tabpanel" aria-labelledby="editor-tab-email" data-editor-panel="email" tabindex="-1">
          <div class="responsive-grid">
            <div class="form-group"><div class="editor-label-row"><label for="task-sender" class="form-label">발신 계정</label>
              <a class="table-link" href="/delivery?panel=sender&amp;new_sender=true{panel_scope}" data-settings-panel="sender">계정 추가</a></div>
              <select id="task-sender" name="sender_profile_id" class="form-select" required>{_options(sender_options, selected_sender)}</select></div>
            <div class="form-group"><div class="editor-label-row"><label for="recipient-routing-mode" class="form-label">수신자 선택</label>
              <a class="table-link" href="/delivery?panel=group{panel_scope}#recipient-groups" data-settings-panel="group">그룹 추가</a></div>
              <select id="recipient-routing-mode" name="recipient_routing_mode" class="form-select">{_options({'catalog_name': '조사 지시에 따라 AI가 그룹 선택', 'legacy_ids': '지정 그룹 사용'}, routing_mode)}</select>
              <div id="recipient-legacy-fields" {'hidden' if catalog_routing else ''}><label for="task-recipient" class="form-label">수신자 그룹</label>
                <select id="task-recipient" name="recipient_group_id" class="form-select" {'' if catalog_routing else 'required'}><option value="">그룹 선택</option>{_options(group_options, selected_group)}</select>{multi_note}</div>
              <div id="recipient-catalog-fields" {'' if catalog_routing else 'hidden'}><p class="form-help">조사 지시에 아래 그룹 이름과 선택 조건을 적으세요.</p>
                <ul id="recipient-catalog-names" class="editor-group-names">{catalog_names}</ul></div></div>
          </div>
          <p id="editor-no-groups" role="alert" {'' if not groups else 'hidden'}>수신자 그룹을 추가하면 저장할 수 있습니다.</p>
          <div class="form-group"><div class="editor-label-row"><label class="form-label" for="email-spec-md">메일 작성 규격</label>
            <label class="btn btn-secondary btn-sm editor-import" for="email-spec-file">파일 불러오기<input id="email-spec-file" type="file" accept=".md,.txt,text/plain,text/markdown" data-import-target="email-spec-md"></label></div>
            <textarea id="email-spec-md" name="email_spec_md" class="form-textarea" maxlength="100000" placeholder="제목, 언어, 섹션, 표, 출처 표기와 스타일을 작성하세요.">{_e(value.get('email_spec_md', '한국어 업무 보고서로 작성합니다. 핵심 요약, 상세 조사 결과, 출처 링크를 포함하고 HTML과 일반 텍스트 메일을 모두 작성합니다.'))}</textarea>
            <p class="form-help">{'비워 두면 기존 메일 규격을 유지합니다.' if editing or cloning else '비워 두면 기본 메일 규격을 사용합니다.'}</p></div>
        </section>
        <section id="editor-panel-execution" role="tabpanel" aria-labelledby="editor-tab-execution" data-editor-panel="execution" tabindex="-1">
          {render_ai_controls(stage_settings(value), model_catalog)}
          <fieldset class="editor-schedule" id="schedule-editor"><legend>반복 일정 · 서울 시간</legend>{schedule_toggle}
            <div class="responsive-grid">
              <div class="form-group"><label class="form-label" for="schedule-preset">반복 주기</label><select id="schedule-preset" name="schedule_preset" class="form-select">{_options(PRESETS,schedule['schedule_preset'])}</select></div>
              <div class="form-group" data-schedule="daily weekdays weekly monthly"><label class="form-label" for="schedule-time">실행 시간</label><input id="schedule-time" type="time" name="schedule_time" class="form-input" value="{_e(schedule['schedule_time'])}"></div>
              <div class="form-group" data-schedule="weekly"><label class="form-label" for="schedule-weekday">요일</label><select id="schedule-weekday" name="schedule_weekday" class="form-select">{_options(WEEKDAYS,schedule['schedule_weekday'])}</select></div>
              <div class="form-group" data-schedule="monthly"><label class="form-label" for="schedule-monthday">날짜</label><select id="schedule-monthday" name="schedule_monthday" class="form-select">{_options({str(i):f'{i}일' for i in range(1,32)},schedule['schedule_monthday'])}</select><p class="form-help">해당 날짜가 없는 달은 건너뜁니다.</p></div>
              <div class="form-group" data-schedule="hourly"><label class="form-label" for="schedule-minute">매시간 실행할 분</label><select id="schedule-minute" name="schedule_minute" class="form-select">{_options({str(i):f'{i}분' for i in range(60)},schedule['schedule_minute'])}</select></div>
              <div class="form-group" data-schedule="custom"><label class="form-label" for="schedule-cron">Cron 표현식</label><input id="schedule-cron" name="cron" class="form-input mono" value="{_e(cron)}"><p class="form-help">분 시 일 월 요일</p></div>
            </div><p id="schedule-summary" class="form-help" aria-live="polite">{_e(schedule_summary(cron))}</p>
            <div class="editor-schedule-preview"><strong>다음 실행 3회</strong><ol id="schedule-occurrences" aria-live="polite"><li>일정을 확인하고 있습니다.</li></ol></div>
            <noscript><p class="form-help">일정 미리보기와 단계 탐색은 JavaScript를 켜면 사용할 수 있습니다. 선택한 반복 주기의 항목만 저장됩니다.</p></noscript>
          </fieldset>
        </section>{review}
      </div>
      <aside class="editor-summary"><details open><summary>설정 요약</summary><dl><dt>Task</dt><dd data-editor-summary="name">{_e(value.get('name')) or '이름 입력 필요'}</dd>
        <dt>발신 계정</dt><dd data-editor-summary="sender">{_e(sender_options.get(selected_sender))}</dd><dt>수신자</dt><dd data-editor-summary="recipient"></dd>
        <dt>일정</dt><dd data-editor-summary="schedule">{_e(schedule_summary(cron))}</dd><dt>AI</dt><dd data-ai-save-summary>{_e(settings_summary(stage_settings(value)))}</dd>
        <dt>저장 후</dt><dd data-editor-summary="launch">{'저장만' if wizard else '현재 예약 유지'}</dd></dl></details></aside>
      </div>
      <p id="file-import-status" class="form-help" role="status" aria-live="polite"></p>
      <div class="editor-actions"><span id="editor-save-state" role="status">{'복제본 · 예약 꺼짐' if cloning else '저장하면 적용됩니다'}</span><div>{actions}</div></div>
    </form>
    {('<p class="form-help"><a href="/tasks/' + _e(task_id) + '/advanced">고급 설정 · YAML</a></p>') if editing else ''}
    </div>
    <dialog id="editor-settings-panel" aria-labelledby="editor-settings-title"><div class="editor-panel-header"><h2 id="editor-settings-title">설정 추가</h2><button type="button" class="btn btn-secondary" data-settings-close>닫기</button></div><iframe title="설정 추가" id="editor-settings-frame"></iframe><p id="editor-options-status" role="status"></p></dialog>
    <script type="application/json" id="editor-operating-labels">{labels_json}</script>
    {FIELD_SCRIPT}{EDITOR_SCRIPT}'''
