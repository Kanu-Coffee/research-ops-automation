"""Progressive enhancement for Task tabs, creation steps, and local form state."""

EDITOR_STYLE = '''<style>
.task-editor {max-width:1280px;margin:0 auto;min-width:0;padding-bottom:24px}.task-editor h1{font-size:26px;margin:12px 0 22px}.editor-back-link{display:inline-flex;min-height:44px;align-items:center}.task-editor h2{font-size:18px;margin:0 0 20px}.editor-tabs{display:flex;gap:4px;border-bottom:1px solid #dbe3ed;margin-bottom:24px;overflow-x:auto}.editor-tabs button{border:0;border-bottom:3px solid transparent;background:transparent;min-height:48px;padding:10px 16px;white-space:nowrap;color:#475569;font-family:inherit;font-size:14px;font-weight:600;cursor:pointer}.editor-tabs button[aria-selected="true"]{color:#1d4ed8;border-bottom-color:#2563eb}.editor-tabs button span{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:#eef2f7;margin-right:7px;font-size:12px}.editor-tabs button[aria-selected="true"] span{background:#dbeafe}.editor-grid{display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:28px;align-items:start}.editor-main,.editor-main section,.editor-main .form-group{min-width:0}.editor-main{padding:24px;background:#fff;border:1px solid #e2e8f0;border-radius:10px}.editor-main .form-textarea{min-height:280px;line-height:1.65}.editor-label-row{display:flex;gap:12px;justify-content:space-between;align-items:center;margin-bottom:8px}.editor-label-row .form-label{margin:0}.editor-label-row .table-link{font-size:13px;white-space:nowrap;display:inline-flex;align-items:center;min-height:44px}.editor-import{position:relative;overflow:hidden;min-height:36px;margin:0;cursor:pointer;white-space:nowrap;font-weight:500}.editor-import input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}.editor-import:focus-within{outline:3px solid #93c5fd;outline-offset:2px}.editor-summary{position:sticky;top:24px;border-left:1px solid #dbe3ed;padding-left:22px;min-width:0}.editor-summary summary{font-size:15px;font-weight:700;min-height:44px;cursor:pointer}.editor-summary dl,.editor-review{font-size:14px;margin:0}.editor-summary dt,.editor-review dt{font-size:12px;color:#64748b;margin:16px 0 5px}.editor-summary dd,.editor-review dd{margin:0;overflow-wrap:anywhere;line-height:1.65}.editor-review{display:grid;grid-template-columns:90px minmax(0,1fr);gap:12px;margin:0 0 24px;padding:16px 0;border-bottom:1px solid #e2e8f0}.editor-review dt{margin:0}.editor-group-names{display:flex;flex-wrap:wrap;gap:6px;list-style:none;padding:0;margin:10px 0}.editor-group-names li{font-size:13px;padding:4px 8px;background:#f1f5f9;border-radius:4px;overflow-wrap:anywhere}.editor-schedule{border:1px solid #dbe3ed;padding:18px;border-radius:8px;margin-top:24px;min-width:0}.editor-schedule legend{font-size:16px;font-weight:650;padding:0 8px}.editor-schedule>.form-help{margin-bottom:16px}.editor-toggle{display:flex;align-items:center;gap:8px;min-height:44px;margin-bottom:12px}.editor-toggle input,.launch-choice input{width:18px;height:18px;flex-shrink:0;accent-color:#2563eb}.editor-schedule-preview{font-size:13px;padding-top:16px;border-top:1px solid #e2e8f0}.editor-schedule-preview ol{padding-left:20px;line-height:1.9}.launch-choices{border:0;padding:0;min-width:0}.launch-choices legend{font-size:15px;font-weight:650;margin-bottom:12px}.launch-choice{display:flex;align-items:center;gap:12px;padding:14px;border:1px solid #dbe3ed;border-radius:7px;margin-bottom:10px;cursor:pointer}.launch-choice:has(input:checked){border-color:#2563eb;background:#eff6ff}.launch-choice strong{display:block;font-size:14px}.launch-choice small{display:block;font-size:12px;color:#64748b;margin-top:5px;line-height:1.5}.editor-actions{position:sticky;bottom:0;display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:24px;padding:14px 0;background:#f8fafc;border-top:1px solid #dbe3ed;z-index:5}.editor-actions>div{display:flex;gap:10px}.editor-actions .btn{min-height:44px}.editor-actions>span{font-size:12px;color:#64748b}.editor-actions [hidden],.task-editor [hidden]{display:none!important}#editor-validation{padding:12px;background:#fef2f2;color:#991b1b;border:1px solid #fecaca;border-radius:6px}#editor-settings-panel{width:min(680px,calc(100vw - 48px));height:min(840px,calc(100dvh - 48px));padding:0;border:1px solid #cbd5e1;border-radius:12px;margin:auto;overflow:hidden}#editor-settings-panel[open]{display:flex;flex-direction:column}#editor-settings-panel::backdrop{background:rgb(15 23 42 / .4)}.editor-panel-header{display:flex;align-items:center;justify-content:space-between;padding:16px;border-bottom:1px solid #e2e8f0;gap:12px}.editor-panel-header h2{font-size:18px;margin:0}#editor-settings-frame{width:100%;flex:1;border:0;min-height:0}#editor-options-status{padding:0 16px;font-size:13px}.task-editor :is(input,select,textarea,button,a):focus-visible{outline:3px solid #93c5fd;outline-offset:2px}.task-editor input,.task-editor select,.task-editor textarea{scroll-margin-bottom:110px;scroll-margin-top:90px}
@media(max-width:1050px){.editor-grid{grid-template-columns:minmax(0,1fr) 210px;gap:18px}.editor-summary{padding-left:16px}.editor-main{padding:20px}.editor-tabs button{padding:10px 12px}}
@media(max-width:900px){.task-editor{padding-bottom:calc(100px + env(safe-area-inset-bottom))}.task-editor h1{font-size:23px;margin:4px 0 16px}.editor-grid{grid-template-columns:minmax(0,1fr);gap:18px}.editor-summary{position:static;border-left:0;border-top:1px solid #dbe3ed;padding:12px 4px 0}.editor-main{padding:16px}.editor-tabs{margin-bottom:16px}.editor-tabs button{padding:10px 8px;font-size:13px;flex:1}.editor-tabs button span{display:none}.editor-main .form-textarea{min-height:250px}.editor-main .responsive-grid{grid-template-columns:minmax(0,1fr)}.task-editor :is(input,select,textarea){font-size:16px!important}.editor-label-row .btn{font-size:12px}.editor-actions{position:fixed;bottom:0;left:0;right:0;padding:12px max(16px,env(safe-area-inset-right)) calc(12px + env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left));margin:0;background:#fff;box-shadow:0 -2px 12px rgb(15 23 42 / .05)}.editor-actions>span{max-width:95px;font-size:11px}.editor-actions>div{margin-left:auto;gap:8px}.editor-actions .btn{font-size:14px;padding:10px 14px}.editor-schedule{padding:14px}.editor-review{grid-template-columns:72px minmax(0,1fr)}#editor-settings-panel{width:100vw;max-width:none;height:100dvh;max-height:none;border:0;border-radius:0}.editor-panel-header{padding:calc(16px + env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) 16px max(16px,env(safe-area-inset-left))}#editor-options-status{padding:0 max(16px,env(safe-area-inset-right)) env(safe-area-inset-bottom) max(16px,env(safe-area-inset-left))}}
@media(prefers-reduced-motion:reduce){.task-editor *{scroll-behavior:auto!important}}
</style>'''

FIELD_SCRIPT = r'''<script>
(() => {
  const routing = document.getElementById('recipient-routing-mode');
  if (routing) {
    const updateRouting = () => {
      const catalog = routing.value === 'catalog_name';
      document.getElementById('recipient-legacy-fields').hidden = catalog;
      document.getElementById('recipient-catalog-fields').hidden = !catalog;
      document.getElementById('task-recipient').required = !catalog;
    };
    routing.addEventListener('change', updateRouting); updateRouting();
  }
  const preset = document.getElementById('schedule-preset');
  const update = () => {
    const kind = preset.value;
    document.querySelectorAll('[data-schedule]').forEach(el => {el.hidden = !el.dataset.schedule.split(' ').includes(kind);});
    let label = preset.options[preset.selectedIndex].text;
    if (kind === 'custom') label = '사용자 지정 · ' + document.getElementById('schedule-cron').value;
    else if (kind === 'hourly') label += ' ' + document.getElementById('schedule-minute').value + '분';
    else {
      if (kind === 'weekly') {const el = document.getElementById('schedule-weekday');label += ' ' + el.options[el.selectedIndex].text;}
      if (kind === 'monthly') label += ' ' + document.getElementById('schedule-monthday').value + '일';
      label += ' ' + document.getElementById('schedule-time').value;
    }
    document.getElementById('schedule-summary').textContent = label + ' · 서울 시간';
  };
  document.getElementById('schedule-editor').addEventListener('change', update); update();
  document.querySelectorAll('[data-import-target]').forEach(input => input.addEventListener('change', async () => {
    const file = input.files[0]; if (!file) return;
    const status = document.getElementById('file-import-status');
    try {
      if (file.size > 400000) throw new Error('파일은 400 KB 이하로 선택하세요.');
      const bytes = await file.arrayBuffer();
      let text;
      try {text = new TextDecoder('utf-8', {fatal: true}).decode(bytes);}
      catch {throw new Error('UTF-8로 저장된 Markdown 또는 텍스트 파일을 선택하세요.');}
      if ([...text].length > 100000 || text.includes('\u0000')) throw new Error('UTF-8 텍스트 100,000자 이하의 파일을 선택하세요.');
      const target = document.getElementById(input.dataset.importTarget);
      if (target.value.trim() && !window.confirm('현재 입력란을 선택한 파일 내용으로 바꿀까요?')) return;
      target.value = text; status.textContent = file.name + ' 내용을 불러왔습니다. 저장하면 적용됩니다.';
      target.dispatchEvent?.(new Event('input', {bubbles:true}));
    } catch (error) {status.textContent = error.message || '파일을 읽지 못했습니다.';}
    finally {input.value = '';}
  }));
})();
</script>'''

EDITOR_SCRIPT = r'''<script>
(() => {
  const form = document.getElementById('task-editor-form'); if (!form) return;
  const tabs = [...form.querySelectorAll('[data-editor-tab]')];
  const panels = [...form.querySelectorAll('[data-editor-panel]')];
  const wizard = form.dataset.editorMode === 'wizard';
  const previous = form.querySelector('[data-editor-back]'), next = form.querySelector('[data-editor-next]');
  const submit = form.querySelector('[data-editor-submit]');
  const error = document.getElementById('editor-validation');
  const saveState = document.getElementById('editor-save-state');
  let active = 0, submitting = false, previewTimer = null, previewRequest = 0, scheduleInvalidField = null;
  let optionStatuses = new Map(), optionsRequest = 0;
  // Only tab location is remembered. Task text and settings stay in this form.
  const tabKey = 'researchops.editor.tab:' + location.pathname + location.search;
  const successfulValues = () => JSON.stringify([...new FormData(form)].filter(([key,value]) =>
    !['csrf_token','request_key','action'].includes(key) && typeof value === 'string'));
  let baseline = successfulValues();
  const changed = () => successfulValues() !== baseline;
  const selectedText = name => {const input = form.elements.namedItem(name); return input?.selectedOptions?.[0]?.textContent || '선택 필요';};
  const updateOperatingNotice = () => {
    const root = document.getElementById('editor-operating-status');
    const missing = optionStatuses.get(form.elements.namedItem('sender_profile_id').value);
    if (!root || !Array.isArray(missing)) return;
    root.replaceChildren();if (!missing.length) return;
    const labels = JSON.parse(document.getElementById('editor-operating-labels')?.textContent || '{}');
    const alert = document.createElement('div');alert.className='alert';
    const text=document.createElement('span');text.textContent=missing.map(item=>labels[item]||item).join(' ');
    const link=document.createElement('a');link.href='/delivery';link.className='btn btn-secondary';link.textContent='메일 설정';
    alert.append(text,link);root.append(alert);
  };
  const summary = () => {
    const launch = form.elements.namedItem('launch_mode')?.value || '';
    const routing = form.elements.namedItem('recipient_routing_mode').value;
    const scheduled = form.elements.namedItem('schedule_enabled')?.checked;
    const values = {
      name: form.elements.namedItem('name').value.trim() || '이름 입력 필요',
      sender: selectedText('sender_profile_id'),
      recipient: routing === 'catalog_name' ? '조사 지시에 따라 AI가 그룹 선택' : selectedText('recipient_group_id'),
      schedule: document.getElementById('schedule-summary').textContent,
      launch: launch ? ({save:'저장만 · 예약 꺼짐',run:'지금 실행·발송 · 반복 예약 꺼짐',schedule:'예약 시작'}[launch]) : (scheduled ? '예약 켜짐 · 즉시 실행 없음' : '예약 꺼짐 · 즉시 실행 없음')
    };
    form.querySelectorAll('[data-editor-summary]').forEach(node => {node.textContent = values[node.dataset.editorSummary] || '';});
    const ai = form.querySelector('[data-ai-summary]');
    if (ai) form.querySelectorAll('[data-ai-save-summary]').forEach(node => {node.textContent = ai.textContent;});
    if (submit) submit.textContent = {save:'Task 저장',run:'저장하고 지금 실행·발송',schedule:'저장하고 예약 시작'}[launch] || 'Task 저장';
    saveState.textContent = changed() ? '저장하지 않은 변경사항' : '저장하면 적용됩니다';
    updateOperatingNotice();
  };
  const activate = (index, focus = false) => {
    active = Math.min(Math.max(index,0),panels.length - 1);
    tabs.forEach((tab,i) => {tab.setAttribute('aria-selected',String(i === active));tab.tabIndex = i === active ? 0 : -1;});
    panels.forEach((panel,i) => {panel.hidden = i !== active;});
    if (wizard) {previous.hidden = active === 0;next.hidden = active === panels.length - 1;submit.hidden = active !== panels.length - 1;}
    try {sessionStorage.setItem(tabKey,String(active));} catch {}
    if (focus) {tabs[active].focus();tabs[active].scrollIntoView({block:'nearest',inline:'nearest'});}
    summary();
  };
  const invalid = scope => [...scope.querySelectorAll('input,select,textarea')].find(field => field.willValidate && !field.validity.valid);
  const showInvalid = field => {
    const panel = field.closest('[data-editor-panel]');
    if (panel) activate(panels.indexOf(panel));
    const label = field.labels?.[0]?.textContent.trim() || '입력값';
    error.textContent = label + ': ' + field.validationMessage;error.hidden = false;
    field.focus();field.scrollIntoView({block:'center'});field.reportValidity();
  };
  const advance = index => {
    if (wizard && index > active) {
      for (let i=0;i<index;i++) {const field = invalid(panels[i]);if (field) {showInvalid(field);return;}}
    }
    error.hidden = true;activate(index,true);
  };
  tabs.forEach((tab,index) => {
    tab.addEventListener('click',() => advance(index));
    tab.addEventListener('keydown',event => {
      let target;
      if (event.key === 'ArrowRight') target=(index+1)%tabs.length;
      if (event.key === 'ArrowLeft') target=(index-1+tabs.length)%tabs.length;
      if (event.key === 'Home') target=0;if (event.key === 'End') target=tabs.length-1;
      if (target !== undefined) {event.preventDefault();advance(target);}
    });
  });
  previous?.addEventListener('click',() => advance(active - 1));
  next?.addEventListener('click',() => advance(active + 1));
  // Native validity runs after the right panel has been revealed.
  form.noValidate = true;
  form.addEventListener('submit',event => {
    if (submitting) {event.preventDefault();return;}
    const field = invalid(form);
    if (field) {event.preventDefault();showInvalid(field);return;}
    if (wizard && active !== panels.length - 1) {event.preventDefault();advance(active + 1);return;}
    if (form.dataset.hasGroups !== 'true' && event.submitter?.value !== 'save') {
      event.preventDefault();activate(1);error.textContent='수신자 그룹을 추가하세요.';error.hidden=false;error.focus();return;
    }
    submitting = true;saveState.textContent='저장하고 있습니다…';
    // Do not disable successful controls: their action and version fields must be posted.
    form.querySelectorAll('button[type="submit"]').forEach(button => button.setAttribute('aria-disabled','true'));
    try {sessionStorage.removeItem(tabKey);} catch {}
  });
  window.addEventListener('pageshow',() => {submitting=false;form.querySelectorAll('[aria-disabled="true"]').forEach(button => button.removeAttribute('aria-disabled'));});
  window.addEventListener('beforeunload',event => {if (changed() && !submitting) {event.preventDefault();event.returnValue='';}});
  form.addEventListener('input',() => {summary();error.hidden=true;});
  form.addEventListener('change',summary);
  form.querySelector('[data-ai-copy]')?.addEventListener('click',summary);
  const aiSummary = form.querySelector('[data-ai-summary]');
  if (aiSummary) new MutationObserver(summary).observe(aiSummary,{childList:true,characterData:true,subtree:true});
  const preview = async () => {
    const request = ++previewRequest;
    const params = new URLSearchParams();
    ['schedule_preset','schedule_time','schedule_weekday','schedule_monthday','schedule_minute','cron'].forEach(key => params.set(key,form.elements.namedItem(key).value));
    const list = document.getElementById('schedule-occurrences');
    try {
      const response = await fetch('/api/schedule/preview?' + params,{credentials:'same-origin'});
      if (request !== previewRequest) return;
      if (!response.ok) {
        if (response.status === 400) {
          scheduleInvalidField = form.elements.namedItem(form.elements.namedItem('schedule_preset').value === 'custom' ? 'cron' : 'schedule_preset');
          scheduleInvalidField.setCustomValidity('반복 일정이 올바르지 않습니다. 입력한 일정을 확인하세요.');
        }
        throw new Error('일정을 확인하세요.');
      }
      scheduleInvalidField?.setCustomValidity('');scheduleInvalidField=null;
      const result = await response.json();if (request !== previewRequest) return;
      const values = (result.occurrences || []).slice(0,3);
      list.replaceChildren(...(values.length ? values : ['다음 실행 일정을 찾지 못했습니다.']).map(value => {const item=document.createElement('li');const date=new Date(value);item.textContent=Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'long',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(date);return item;}));
    } catch (cause) {if (request === previewRequest) {const item=document.createElement('li');item.textContent='미리보기 없음 · ' + (cause.message || '다시 확인하세요.');list.replaceChildren(item);}}
  };
  const scheduleChanged = () => {
    ++previewRequest;scheduleInvalidField?.setCustomValidity('');scheduleInvalidField=null;
    clearTimeout(previewTimer);previewTimer=setTimeout(preview,250);summary();
  };
  document.getElementById('schedule-editor').addEventListener('input',scheduleChanged);
  document.getElementById('schedule-editor').addEventListener('change',scheduleChanged);
  const dialog = document.getElementById('editor-settings-panel'), frame=document.getElementById('editor-settings-frame');
  let returnFocus=null, originalIds=null, settingsKind='';
  const refreshOptions = async (announce = true) => {
    const request=++optionsRequest, editedKind=settingsKind, beforeIds=new Set(originalIds || []);
    const status=document.getElementById('file-import-status');
    try {
      const scope=form.elements.namedItem('task_id')?.value;
      const response=await fetch('/api/task-options'+(scope?'?task_id='+encodeURIComponent(scope):''),{credentials:'same-origin'});if (!response.ok) throw new Error();
      const result=await response.json();if (request !== optionsRequest) return;
      optionStatuses=new Map((result.senders || []).filter(item=>Array.isArray(item.missing)).map(item=>[item.id,item.missing]));
      [['sender_profile_id',result.senders || []],['recipient_group_id',result.groups || []]].forEach(([key,items]) => {
        const select=form.elements.namedItem(key), before=select.value;
        const choices=items.map(item => new Option(item.name + (key === 'recipient_group_id' ? ' (' + item.count + '명)' : ''),item.id));
        if (key === 'recipient_group_id') choices.unshift(new Option('그룹 선택',''));
        if (before && !items.some(item => item.id === before)) choices.push(new Option(before + ' (설정 확인 필요)',before));
        select.replaceChildren(...choices);select.value=before;
        if ((key === 'sender_profile_id' ? 'sender' : 'group') === editedKind) {
          const added=items.filter(item => !beforeIds.has(item.id));if (added.length === 1) select.value=added[0].id;
        }
      });
      const groups=result.groups || [];
      document.getElementById('recipient-catalog-names').replaceChildren(...groups.map(item => {const li=document.createElement('li');li.textContent=item.name;return li;}));
      form.dataset.hasGroups=String(groups.length > 0);document.getElementById('editor-no-groups').hidden=groups.length > 0;
      form.querySelectorAll('[data-task-publish]').forEach(button => {button.disabled=groups.length === 0;});
      summary();if (announce) status.textContent='설정 목록을 새로 확인했습니다.';
    } catch {if (announce && request === optionsRequest) status.textContent='설정 목록을 불러오지 못했습니다. 입력을 유지한 채 다시 설정 창을 열고 닫아 주세요.';}
  };
  document.querySelectorAll('[data-settings-panel]').forEach(link => link.addEventListener('click',event => {
    if (!dialog.showModal) return;
    event.preventDefault();returnFocus=link;settingsKind=link.dataset.settingsPanel;
    originalIds=new Set([...form.elements.namedItem(settingsKind === 'sender' ? 'sender_profile_id' : 'recipient_group_id').options].map(option => option.value));
    document.getElementById('editor-settings-title').textContent=settingsKind === 'sender' ? '발신 계정 추가' : '수신자 그룹 추가';
    frame.title=document.getElementById('editor-settings-title').textContent;frame.src=link.href;dialog.showModal();
  }));
  document.querySelector('[data-settings-close]').addEventListener('click',() => dialog.close());
  dialog.addEventListener('close',() => {frame.removeAttribute('src');refreshOptions();returnFocus?.focus();});
  const details=form.querySelector('.editor-summary details');if (matchMedia('(max-width:900px)').matches) details.open=false;
  form.querySelector('.editor-tabs').hidden=false;
  let initial=0;try {initial=Number(sessionStorage.getItem(tabKey)) || 0;} catch {}
  activate(initial);preview();refreshOptions(false);
})();
</script>'''
