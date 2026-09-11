"""Direct YAML/document editing with no persisted intermediate package."""

import html


def _e(value):
    return html.escape(str(value if value is not None else ""))


def render_task_advanced_editor(values, *, error=None):
    from researchops.web.views import render_base_layout
    task_id = _e(values.get("task_id"))
    fields = [("config_yaml", "YAML 설정", "task.yaml"),
              ("task_md", "조사 지시", "task.md"),
              ("email_spec_md", "메일 작성 규격", "email_spec.md")]
    tabs = ''.join(f'<button type="button" role="tab" id="advanced-tab-{key}" '
        f'aria-controls="advanced-panel-{key}" aria-selected="false" tabindex="-1" data-advanced-tab="{key}">{label}</button>'
        for key, label, _ in fields)
    panels = ''.join(f'<section role="tabpanel" id="advanced-panel-{key}" '
        f'aria-labelledby="advanced-tab-{key}" data-advanced-panel="{key}">'
        f'<label class="form-label" for="advanced-{key}">{label} · <code>{filename}</code></label>'
        f'<textarea class="form-textarea" id="advanced-{key}" name="{key}" maxlength="100000" '
        f'{"required" if key != "email_spec_md" else ""} spellcheck="false">{_e(values.get(key, ""))}</textarea></section>'
        for key, label, filename in fields)
    extras = ''.join(f'<li><code>{_e(name)}</code></li>' for name in values.get("supplemental_files", []))
    preserved = f'<details class="advanced-preserved"><summary>함께 보존하는 파일</summary><ul>{extras}</ul></details>' if extras else ''
    guard = ''.join(f'<input type="hidden" name="{key}" value="{_e(values.get(key, ""))}">'
                    for key in ("expected_version_hash", "expected_updated_at"))
    request = f'<input type="hidden" name="request_key" value="{_e(values["request_key"])}">' if values.get("request_key") else ''
    error_html = f'<p class="alert alert-error" id="advanced-error" role="alert" tabindex="-1">{_e(error)}</p>' if error else ''
    body = f'''<style>
    .advanced-editor{{max-width:1100px;margin:auto;min-width:0;padding-bottom:24px}}
    .advanced-editor h1{{font-size:26px;margin:12px 0}}.advanced-editor .table-link{{display:inline-flex;align-items:center;min-height:44px}}
    .advanced-tabs{{display:flex;gap:8px;overflow-x:auto;border-bottom:1px solid #dbe3ed;margin:20px 0}}
    .advanced-tabs button{{font:inherit;font-size:14px;border:0;border-bottom:3px solid transparent;background:transparent;padding:12px;min-height:48px;white-space:nowrap;cursor:pointer}}
    .advanced-tabs [aria-selected="true"]{{border-bottom-color:#2563eb;color:#1d4ed8}}
    .advanced-editor section{{margin-bottom:20px}}.advanced-editor textarea{{width:100%;min-height:420px;line-height:1.6;font-family:ui-monospace,monospace;scroll-margin:100px 0 120px;tab-size:2}}
    .advanced-editor [hidden]{{display:none!important}}.advanced-editor .save-bar{{justify-content:space-between;gap:12px}}.advanced-editor .inline-actions{{display:flex;gap:8px;flex-shrink:0}}.advanced-editor .save-bar>span{{word-break:keep-all}}.advanced-preserved{{margin:20px 0}}
    .advanced-preserved summary{{min-height:44px;cursor:pointer}}.advanced-preserved ul{{padding-left:20px;overflow-wrap:anywhere}}
    .advanced-editor :focus-visible{{outline:3px solid #93c5fd;outline-offset:2px}}
    @media(max-width:900px){{.advanced-editor{{padding-bottom:calc(100px + env(safe-area-inset-bottom))}}.advanced-editor h1{{font-size:23px}}.advanced-editor textarea{{font-size:16px;min-height:350px}}.advanced-tabs button{{flex:1;padding:12px 8px;font-size:13px}}.advanced-editor .save-bar>span{{font-size:12px;max-width:160px}}}}
    </style><div class="advanced-editor"><a href="/tasks/{task_id}/edit" class="table-link">일반 설정</a>
    <h1>고급 설정 · {_e(values.get("name") or values.get("task_id"))}</h1>
    <p class="form-help">저장하면 새 버전으로 적용됩니다. 예약 상태는 유지합니다.</p>{error_html}
    <form method="POST" action="/tasks/{task_id}/advanced" id="task-advanced-form">{guard}{request}
    <div class="advanced-tabs" role="tablist" aria-label="Task 고급 설정" hidden>{tabs}</div>{panels}{preserved}
    <div class="save-bar"><span id="advanced-save-state" role="status">저장하면 적용됩니다</span><div class="inline-actions">
    <a class="btn btn-secondary" href="/tasks/{task_id}">취소</a><button class="btn btn-primary" type="submit">변경사항 저장</button></div></div>
    </form></div>{ADVANCED_SCRIPT}'''
    return render_base_layout("Task 고급 설정", body, active_nav="tasks")


ADVANCED_SCRIPT = r'''<script>
(() => {
  const form=document.getElementById('task-advanced-form');if(!form)return;
  const tabs=[...form.querySelectorAll('[data-advanced-tab]')], panels=[...form.querySelectorAll('[data-advanced-panel]')];
  let selected=0,submitting=false;const tabKey='researchops.advanced.tab:'+location.pathname;
  const snapshot=()=>JSON.stringify([...form.querySelectorAll('textarea')].map(el=>el.value));const initial=snapshot();
  const activate=(index,focus=false)=>{selected=index;tabs.forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;panels[i].hidden=i!==index;});if(focus)tabs[index].focus();try{sessionStorage.setItem(tabKey,tabs[index].dataset.advancedTab);}catch{}};
  tabs.forEach((tab,i)=>{tab.addEventListener('click',()=>activate(i));tab.addEventListener('keydown',event=>{
    const key=event.key;let next=i;if(key==='ArrowRight')next=(i+1)%tabs.length;else if(key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;else if(key==='Home')next=0;else if(key==='End')next=tabs.length-1;else return;event.preventDefault();activate(next,true);
  });});
  form.querySelector('[role="tablist"]').hidden=false;try{selected=Math.max(0,tabs.findIndex(tab=>tab.dataset.advancedTab===sessionStorage.getItem(tabKey)));}catch{}activate(selected);
  form.addEventListener('invalid',event=>{const panel=event.target.closest('[data-advanced-panel]');if(panel)activate(panels.indexOf(panel));},true);
  form.addEventListener('input',()=>{document.getElementById('advanced-save-state').textContent=snapshot()===initial?'저장하면 적용됩니다':'저장하지 않은 변경사항';});
  form.addEventListener('submit',()=>{submitting=true;});
  window.addEventListener('beforeunload',event=>{if(!submitting&&snapshot()!==initial){event.preventDefault();event.returnValue='';}});
  const error=document.getElementById('advanced-error');
  if(error){const field=error.textContent.includes('조사 지시')?'task_md':error.textContent.includes('메일 작성 규격')?'email_spec_md':'config_yaml';activate(tabs.findIndex(tab=>tab.dataset.advancedTab===field));error.focus();}
})();
</script>'''
