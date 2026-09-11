"""Shared, catalog-backed Research/Compose controls and immutable run settings."""

import html
import json
from datetime import datetime
from zoneinfo import ZoneInfo


PROVIDERS = {"codex_exec": "Codex", "antigravity_exec": "Antigravity (Agy)"}
STAGES = {"research": "Research · 조사", "compose": "Compose · 메일 작성"}


def _e(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _catalog_time(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return ""
        return instant.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S") + " 서울"
    except (AttributeError, TypeError, ValueError):
        return ""


def stage_settings(values=None):
    values = values or {}
    legacy = {"type": values.get("runner_type") or "codex_exec", "model": values.get("model") or None,
              "reasoning_effort": values.get("reasoning_effort") or None}
    supplied = values.get("stage_settings") or {}
    supplied = supplied if isinstance(supplied, dict) else {}
    return {stage: _safe_setting(supplied.get(stage), legacy) for stage in STAGES}


def _safe_setting(value, fallback=None):
    if (not isinstance(value, dict) or any(value.get(key) is not None and not isinstance(value.get(key), str)
                                         for key in ("type", "model", "reasoning_effort"))):
        return dict(fallback or {})
    return {key: value.get(key) for key in ("type", "model", "reasoning_effort")}


def _provider(catalog, kind):
    return next((item for item in (catalog or {}).get("providers", []) if item.get("type") == kind), {})


def _display_selection(setting, catalog):
    """Agy family controls retain the precise selected variant without rewriting it."""
    value = _safe_setting(setting)
    value["type"] = value.get("type") or "codex_exec"
    value["model"] = value.get("model") or ""
    value["reasoning_effort"] = value.get("reasoning_effort") or ""
    for model in _provider(catalog, value["type"]).get("models", []):
        if model.get("id") == value["model"]:
            return value
        for effort in model.get("efforts", []):
            if effort.get("model") == value["model"]:
                value.update(model=model["id"], reasoning_effort=effort["value"])
                return value
    return value


def _options(items, selected):
    return "".join(f'<option value="{_e(key)}"{" selected" if key == selected else ""}>{_e(label)}</option>'
                   for key, label in items)


def _stage_card(stage, setting, catalog, prefix, *, reused=False):
    value = _display_selection(setting, catalog)
    kind, model, effort = value["type"], value["model"], value["reasoning_effort"]
    provider = _provider(catalog, kind)
    provider_options = list(PROVIDERS.items())
    if kind not in PROVIDERS:
        provider_options.append((kind, kind + " · 기존 설정"))
    models = provider.get("models", [])
    model_options = [("", "CLI 기본 모델")]
    model_options += [(item["id"], item.get("label", item["id"])) for item in models]
    known = next((item for item in models if item["id"] == model), None)
    legacy = bool(model and known is None)
    if legacy:
        model_options.append((model, model + " · 기존 저장값 (목록 미확인)"))
    family = bool(known and known.get("family"))
    efforts = [("", "사고 수준 선택" if family else "모델 기본값")]
    if known:
        efforts += [(item["value"], item.get("label", item["value"]))
                    for item in known.get("efforts", []) if item["value"]]
    if effort and effort not in {key for key, _ in efforts}:
        efforts.append((effort, effort + " · 기존 저장값"))
    sid = f"{prefix}-{stage}"
    fields = ""
    for key, label, choices, selected in (("provider", "AI 엔진", provider_options, kind),
                                          ("model", "모델", model_options, model),
                                          ("effort", "사고 수준 (effort)", efforts, effort)):
        fixed = key == "effort" and known and not family and len(efforts) == 1
        options = _options(choices, selected)
        if key == "effort" and family:
            options = options.replace('<option value=""', '<option value="" disabled', 1)
        fields += f'''<div class="form-group"><label for="{sid}-{key}" class="form-label">{label}</label>
          <select id="{sid}-{key}" name="{stage}_{key}" class="form-select" data-ai-field="{key}"{' disabled' if reused or fixed else ''}
            {'required' if key == 'effort' and family else ''} data-ai-fixed="{'true' if fixed else 'false'}"
            aria-describedby="{sid}-help">{options}</select></div>'''
    note = ("기존 조사 결과를 재사용합니다. Research 모델은 호출하지 않습니다." if reused else
            "출처 조사와 자료 수집에 사용할 AI입니다." if stage == "research" else
            "확정된 조사 결과로 메일 제목과 본문을 작성할 AI입니다.")
    warning = "기존 저장값이 현재 목록에 없습니다. 그대로 유지하거나 확인된 모델을 선택하세요." if legacy else ""
    return f'''<fieldset class="ai-stage" data-ai-stage="{stage}"><legend>{STAGES[stage]}</legend>
      <p class="form-help" id="{sid}-help" data-ai-help>{note}</p>{fields}
      <p class="form-help ai-warning" data-ai-warning role="status">{warning}</p></fieldset>'''


def settings_summary(settings):
    parts = []
    for stage, label in STAGES.items():
        value = _safe_setting((settings or {}).get(stage))
        parts.append(f'{label}: {PROVIDERS.get(value.get("type"), value.get("type") or "미기록")} / '
                     f'{value.get("model") or "CLI 기본 모델"} / {value.get("reasoning_effort") or "모델 기본값"}')
    return " · ".join(parts)


def render_ai_controls(settings, catalog=None, *, prefix="task-ai", mode="task", scope="full",
                       baseline=None, current_task=None, selection_source="previous", source_version=""):
    catalog = catalog or {"providers": [], "stale": True}
    baseline = baseline or settings
    cards = "".join(_stage_card(stage, settings.get(stage, {}), catalog, prefix,
                               reused=mode == "retry" and scope == "compose_only" and stage == "research")
                    for stage in STAGES)
    catalog_note = ("저장된 모델 목록을 사용합니다. 필요하면 목록을 새로 확인하세요."
                    if not catalog.get("stale") else "모델 목록이 오래되었거나 아직 확인되지 않았습니다. 기존 저장값은 유지됩니다.")
    checked = _catalog_time(catalog.get("fetched_at"))
    if checked:
        catalog_note += " 마지막 확인: " + checked
    config = {"catalog": catalog, "baseline": baseline, "currentTask": current_task,
              "mode": mode, "scope": scope, "selectionSource": selection_source}
    source_controls = ""
    if mode == "retry":
        source_controls = f'''<input type="hidden" name="selection_source" value="{_e(selection_source)}">
          <input type="hidden" name="selection_version_hash" value="{_e(source_version)}">
          <div class="ai-toolbar"><button type="button" class="btn btn-secondary btn-sm" data-ai-restore>이전 실행 설정으로 복원</button>
          <button type="button" class="btn btn-secondary btn-sm" data-ai-load-task{' disabled' if not current_task else ''}>현재 Task 설정 불러오기</button></div>
          <p class="form-help" data-ai-source>이전 실행의 설정을 기본값으로 표시합니다. 현재 Task 설정은 불러온 뒤 적용됩니다.</p>'''
    return f'''<section class="ai-settings" data-ai-settings id="{_e(prefix)}" aria-label="단계별 AI 설정">
      <h2 style="font-size:18px; margin-bottom:8px;">단계별 AI 설정</h2>
      <p class="form-help">Research와 Compose의 AI 엔진, 모델, 사고 수준을 각각 선택하세요.</p>{source_controls}
      <div class="ai-toolbar"><button type="button" class="btn btn-secondary btn-sm" data-ai-copy{' hidden disabled' if mode == 'retry' and scope == 'compose_only' else ''}>Research 설정을 Compose에 복사</button>
      <button type="button" class="btn btn-secondary btn-sm" data-ai-refresh>모델 목록 새로고침</button></div>
      <p class="form-help" data-ai-catalog-status role="status" aria-live="polite">{catalog_note}</p>
      <div class="ai-stage-grid">{cards}</div>
      <p class="ai-summary" data-ai-summary aria-live="polite">{_e(settings_summary(settings))}</p>
      {'<p class="form-help" data-ai-changes aria-live="polite">이전 실행 대비 AI 설정 변경 없음</p>' if mode == 'retry' else ''}
      <noscript><p class="form-help">Provider·모델의 연동 선택과 설정 복사는 자바스크립트를 켜면 사용할 수 있습니다. 기존 선택값은 저장할 수 있습니다.</p></noscript>
      <script type="application/json" data-ai-config>{_json(config)}</script>
    </section>{AI_STYLE}{AI_SCRIPT}'''


def render_execution_history(settings, plan=None):
    if not settings:
        return ""
    plan = plan or {}
    rows = ""
    for stage, label in STAGES.items():
        value = settings.get(stage) or {}
        reused = stage == "research" and plan.get("scope") == "compose_only"
        rows += f'''<tr><th scope="row">{label}{' · 기존 결과 재사용' if reused else ''}</th>
          <td>{_e(PROVIDERS.get(value.get('type'), value.get('type') or '미기록'))}</td>
          <td>{_e(value.get('model') or 'CLI 기본 모델')}</td><td>{_e(value.get('reasoning_effort') or '모델 기본값')}</td></tr>'''
    return f'''<section class="card" aria-labelledby="run-ai-history"><h2 id="run-ai-history" class="card-title">이 실행의 AI 설정 · 변경 불가</h2>
      <p class="form-help">실행 당시 저장된 설정입니다. 아래 재실행 설정은 새로운 Run에 적용됩니다.</p>
      <div style="overflow-x:auto;"><table class="data-table"><thead><tr><th scope="col">단계</th><th scope="col">AI 엔진</th><th scope="col">모델</th><th scope="col">사고 수준</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''


def render_retry_panel(run_data, catalog=None, *, values=None, current_task=None):
    run = run_data.get("run", {})
    retry = run_data.get("retry_settings") or {}
    if run.get("status") in {"queued", "running", "awaiting_receipt"} or not retry:
        return ""
    values = values or {}
    baseline = retry.get("execution_settings") or run_data.get("execution_settings") or {}
    supplied = values.get("execution_settings") or {}
    supplied = supplied if isinstance(supplied, dict) else {}
    settings = {stage: _safe_setting(supplied.get(stage), baseline.get(stage)) for stage in STAGES}
    available = retry.get("compose_available", False)
    scope = values.get("scope") or retry.get("default_scope", "compose_only" if available else "full")
    source = retry.get("source_composition") or {}
    uncertain = bool((run_data.get("email_retry") or {}).get("uncertain")) or (run_data.get("handoff") or {}).get("status") == "uncertain"
    options = [("compose_only", "메일 작성만 다시 실행 · 기존 조사 재사용"), ("full", "전체 다시 실행 · Research + Compose")]
    scope_options = "".join(f'<option value="{key}"{" selected" if key == scope else ""}{" disabled" if key == "compose_only" and not available else ""}>{label}</option>' for key, label in options)
    controls = render_ai_controls(settings, catalog, prefix="retry-ai", mode="retry", scope=scope,
        baseline=baseline, current_task=current_task, selection_source=values.get("selection_source", "previous"),
        source_version=values.get("selection_version_hash", ""))
    return f'''<section class="card" id="run-retry"><h2 class="card-title">설정 확인 후 재실행</h2>
      <p class="form-help">새 Run을 만들어 실행합니다. 성공하면 원본 실행에 고정된 Task의 발송 설정에 따라 이메일을 전송합니다.</p>
      <form method="POST" action="/runs/{_e(run.get('run_id'))}/retry-configured" id="run-retry-form">
        {'<input type="hidden" name="request_key" value="' + _e(values['request_key']) + '">' if values.get('request_key') else ''}
        <div class="form-group"><label for="retry-scope" class="form-label">재실행 범위</label>
          <select id="retry-scope" name="scope" class="form-select">{scope_options}</select></div>
        <p class="form-help">{'재사용할 조사: ' + _e(source.get('run_id', run.get('run_id'))) + ' · 작성 입력 revision ' + _e(source.get('revision', source.get('composition_revision', ''))) if available else '재사용 가능한 확정 조사 입력이 없어 전체 실행만 가능합니다.'}</p>
        {controls}
        {'<p role="alert">이전 이메일의 전달 결과를 먼저 확인하세요. 재실행은 현재 사용할 수 없습니다.</p>' if uncertain else ''}
        <button type="submit" class="btn btn-primary" data-ai-submit{' disabled' if uncertain else ''}>{'메일 재작성·발송' if scope == 'compose_only' else '전체 재실행·발송'}</button>
      </form></section>'''


AI_STYLE = '''<style>
.ai-settings {margin:22px 0; min-width:0;} .ai-stage-grid {display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:14px 0;}
.ai-stage {min-width:0; border:1px solid #cbd5e1; border-radius:8px; padding:16px;} .ai-stage legend {padding:0 6px; font-weight:600;}
.ai-stage select {width:100%; min-width:0;} .ai-stage .form-help {overflow-wrap:anywhere;} .ai-toolbar {display:flex; flex-wrap:wrap; gap:8px; margin:12px 0;}
.ai-summary {padding:12px; border-radius:6px; background:#f1f5f9; overflow-wrap:anywhere; font-size:13px;} .ai-warning {color:#92400e;}
.ai-stage[data-reused="true"] {background:#f8fafc;} @media(max-width:640px) {.ai-stage-grid {grid-template-columns:1fr;} .ai-toolbar button {min-height:44px;}}
</style>'''


AI_SCRIPT = r'''<script>
window.researchopsInitAISettings = () => {
  document.querySelectorAll('[data-ai-settings]:not([data-ai-ready])').forEach(root => {
    root.dataset.aiReady = 'true';
    const config = JSON.parse(root.querySelector('[data-ai-config]').textContent);
    let catalog = config.catalog || {providers:[]}, wasReused = null, fullResearch = null;
    const form = root.closest('form'), stages = ['research','compose'];
    const field = (stage,key) => root.querySelector('[name="'+stage+'_'+key+'"]');
    const provider = type => (catalog.providers || []).find(item => item.type === type) || {models:[]};
    const read = stage => ({type:field(stage,'provider').value,model:field(stage,'model').value,reasoning_effort:field(stage,'effort').value});
    const display = raw => {
      const value = {type:raw.type || 'codex_exec',model:raw.model || '',reasoning_effort:raw.reasoning_effort || ''};
      const models = provider(value.type).models || [];
      if (!models.some(item => item.id === value.model)) {
        for (const item of models) {
          const variant = (item.efforts || []).find(effort => effort.model === value.model);
          if (variant) return {...value,model:item.id,reasoning_effort:variant.value};
        }
      }
      return value;
    };
    const replaceOptions = (select,values,selected) => {
      select.replaceChildren(...values.map(([value,label]) => new Option(label,value,false,value === selected)));
      select.value = selected;
    };
    const render = (stage,raw) => {
      const value = display(raw), p = field(stage,'provider');
      if (![...p.options].some(option => option.value === value.type)) p.add(new Option(value.type+' · 기존 저장값',value.type));
      p.value = value.type;
      const models = provider(value.type).models || [], known = models.find(item => item.id === value.model);
      const modelOptions = [['','CLI 기본 모델'],...models.map(item => [item.id,item.label || item.id])];
      if (value.model && !known) modelOptions.push([value.model,value.model+' · 기존 저장값 (목록 미확인)']);
      replaceOptions(field(stage,'model'),modelOptions,value.model);
      const efforts = [['',known?.family ? '사고 수준 선택' : '모델 기본값'],...(known?.efforts || []).filter(item => item.value).map(item => [item.value,item.label || item.value])];
      if (value.reasoning_effort && !efforts.some(([key]) => key === value.reasoning_effort)) efforts.push([value.reasoning_effort,value.reasoning_effort+' · 기존 저장값']);
      replaceOptions(field(stage,'effort'),efforts,value.reasoning_effort);
      const effortSelect=field(stage,'effort');
      effortSelect.required=Boolean(known?.family);
      effortSelect.options[0].disabled=Boolean(known?.family);
      effortSelect.dataset.aiFixed=String(Boolean(known && !known.family && efforts.length === 1));
      effortSelect.disabled=effortSelect.dataset.aiFixed === 'true';
      root.querySelector('[data-ai-stage="'+stage+'"] [data-ai-warning]').textContent = value.model && !known ? '기존 저장값이 현재 목록에 없습니다. 그대로 유지하거나 확인된 모델을 선택하세요.' : '';
    };
    const selectSource = (kind,version='') => {
      if (!form.elements.selection_source) return;
      form.elements.selection_source.value = kind;
      form.elements.selection_version_hash.value = version;
      root.querySelector('[data-ai-source]').textContent = kind === 'task' ? '현재 Task 설정을 불러왔습니다. 변경 내용을 확인하세요.' : kind === 'custom' ? '이 재실행에 사용할 설정을 직접 변경했습니다.' : '이전 실행 설정을 사용합니다.';
    };
    const label = (stage,raw=read(stage)) => {
      const value=display(raw), model=(provider(value.type).models || []).find(item => item.id === value.model);
      const engine=[...field(stage,'provider').options].find(option => option.value === value.type)?.textContent || value.type;
      const effort=(model?.efforts || []).find(item => item.value === value.reasoning_effort);
      const parts=[engine,model?.label || value.model || 'CLI 기본 모델',effort?.label || value.reasoning_effort || (model?.family ? '사고 수준 선택' : '모델 기본값')];
      return (stage === 'research' ? 'Research' : 'Compose')+': '+parts.join(' / ');
    };
    const update = () => {
      const scope = form.elements.scope?.value || 'full', reused = config.mode === 'retry' && scope === 'compose_only';
      if (reused && wasReused !== true) {fullResearch=read('research');render('research',config.baseline.research || {});}
      if (!reused && wasReused === true && fullResearch) render('research',fullResearch);
      wasReused=reused;
      const research = root.querySelector('[data-ai-stage="research"]');
      research.dataset.reused = String(reused);
      research.querySelectorAll('select').forEach(select => {select.disabled = reused || select.dataset.aiFixed === 'true';});
      research.querySelector('[data-ai-help]').textContent = reused ? '기존 조사 결과를 재사용합니다. Research 모델은 호출하지 않습니다.' : '출처 조사와 자료 수집에 사용할 AI입니다.';
      root.querySelector('[data-ai-copy]').disabled = reused;
      root.querySelector('[data-ai-copy]').hidden = reused;
      root.querySelector('[data-ai-summary]').textContent = (reused ? 'Research: 기존 조사 결과 재사용' : label('research'))+' → '+label('compose');
      form.querySelectorAll('[data-ai-save-summary]').forEach(item => {item.textContent=root.querySelector('[data-ai-summary]').textContent;});
      const diff = root.querySelector('[data-ai-changes]');
      if (diff) {
        const changed = stages.filter(stage => !(reused && stage === 'research')).filter(stage => JSON.stringify(read(stage)) !== JSON.stringify(display(config.baseline[stage] || {})));
        diff.textContent = changed.length ? '이전 실행 대비 변경: '+changed.map(stage => {
          return label(stage,config.baseline[stage] || {})+' → '+label(stage);
        }).join(' · ') : '이전 실행 대비 AI 설정 변경 없음';
      }
      const submit = form.querySelector('[data-ai-submit]');
      if (submit) submit.textContent = reused ? '메일 재작성·발송' : '전체 재실행·발송';
    };
    stages.forEach(stage => {
      field(stage,'provider').addEventListener('change',() => {render(stage,{type:field(stage,'provider').value});
        root.querySelector('[data-ai-stage="'+stage+'"] [data-ai-warning]').textContent='AI 엔진을 바꾸어 모델과 사고 수준을 기본값으로 초기화했습니다.';
        selectSource('custom');update();});
      field(stage,'model').addEventListener('change',() => {const value=read(stage);value.reasoning_effort='';render(stage,value);selectSource('custom');update();});
      field(stage,'effort').addEventListener('change',() => {selectSource('custom');update();});
    });
    root.querySelector('[data-ai-copy]').addEventListener('click',() => {render('compose',read('research'));selectSource('custom');update();});
    root.querySelector('[data-ai-restore]')?.addEventListener('click',() => {stages.forEach(stage => render(stage,config.baseline[stage] || {}));fullResearch=config.baseline.research;selectSource('previous');update();});
    root.querySelector('[data-ai-load-task]')?.addEventListener('click',() => {
      if (!config.currentTask) return;
      const relevant=form.elements.scope?.value === 'compose_only' ? ['compose'] : stages;
      relevant.forEach(stage => render(stage,config.currentTask.stage_settings[stage] || {}));
      fullResearch=config.currentTask.stage_settings.research;
      selectSource('task',config.currentTask.expected_version_hash);update();
    });
    form.elements.scope?.addEventListener('change',update);
    root.querySelector('[data-ai-refresh]').addEventListener('click',async event => {
      const button=event.currentTarget, status=root.querySelector('[data-ai-catalog-status]'); button.disabled=true;
      status.textContent='모델 목록을 확인하고 있습니다…';
      try {
        const response=await fetch('/api/model-catalog/refresh',{method:'POST',headers:{'X-CSRF-Token':form.elements.csrf_token.value}});
        const result=await response.json(); if (!response.ok) throw new Error(result.error || '모델 목록을 확인하지 못했습니다.');
        const saved=Object.fromEntries(stages.map(stage => [stage,read(stage)]));catalog=result;
        stages.forEach(stage => render(stage,saved[stage]));
        status.textContent=result.stale ? '일부 모델 목록을 확인하지 못했습니다. 기존 선택은 유지됩니다.' : '모델 목록을 새로 확인했습니다. 기존 선택은 유지됩니다.';update();
        if (result.fetched_at) {const checked=new Date(result.fetched_at);if (!Number.isNaN(checked.getTime())) status.textContent+=' 마지막 확인: '+checked.toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' 서울';}
      } catch(error) {status.textContent=error.message || '목록 확인 실패. 기존 선택은 유지됩니다.';}
      finally {button.disabled=false;}
    });
    update();
  });
};
window.researchopsInitAISettings();
</script>'''
