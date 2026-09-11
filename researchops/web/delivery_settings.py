"""Simple sender and recipient setup over the existing delivery commands."""

from types import SimpleNamespace
from urllib.parse import quote, urlencode

from researchops.delivery.smtp_config import SmtpSettings
from researchops.errors import DeliveryError, NotFoundError


def _e(value):
    from researchops.web.views import _escape
    return _escape(value)


def _field(name, label, value='', *, kind='text', hint='', required=False, autocomplete=None):
    return f'<div class="form-group"><label class="form-label" for="smtp-{name}">{label}</label><input id="smtp-{name}" class="form-input" name="{name}" type="{kind}" value="{_e(value)}"' + (' required' if required else '') + (f' autocomplete="{autocomplete}"' if autocomplete else '') + '>' + (f'<p class="form-help">{hint}</p>' if hint else '') + '</div>'


def render_settings(config, flash=None, operating_state=None, sender_profile_id='default', creating_sender=False,
                    form_values=None, catalog_entries=None):
    from researchops.web.context import get_query
    from researchops.web.views import render_base_layout
    from researchops.web.catalog_views import name_control, lifecycle_control, deleted_delivery_entries
    from researchops.web.layout import is_admin, principal, owner_note
    query = get_query()
    panel = query.get('panel')
    tab = 'groups' if panel == 'group' or query.get('tab') == 'groups' else 'accounts'
    embedded = panel in {'sender', 'group'}
    panel_field = f'<input type="hidden" name="panel" value="{panel}">' if embedded else ''
    if embedded and query.get('task_id'):
        panel_field += f'<input type="hidden" name="task_id" value="{_e(query["task_id"])}">'
    cancel_url = '/delivery' + ('?' + urlencode({key: query[key] for key in ('panel', 'task_id') if query.get(key)}) if embedded else '')
    catalogs = catalog_entries or {}
    senders = {item['legacy_key']: item for item in catalogs.get('sender', [])}
    groups = {item['legacy_key']: item for item in catalogs.get('recipient_group', [])}
    active_senders = {key: value for key, value in config.all_senders().items() if not senders.get(key, {}).get('deleted_at')}
    active_groups = {key: value for key, value in config.recipient_groups.items() if not groups.get(key, {}).get('deleted_at')}
    if (principal() or {}).get('role') == 'user':
        active_senders = {key: value for key, value in active_senders.items() if key in senders}
        active_groups = {key: value for key, value in active_groups.items() if key in groups}
        if form_values is None and not creating_sender:
            if 'sender' in query and sender_profile_id not in active_senders:
                raise DeliveryError('등록된 발신 계정을 선택하세요.')
            if not active_senders:
                creating_sender, sender_profile_id = True, ''
            elif sender_profile_id not in active_senders and 'sender' not in query:
                sender_profile_id = next(iter(active_senders))
            elif sender_profile_id not in active_senders:
                raise DeliveryError('등록된 발신 계정을 선택하세요.')
    try:
        smtp = SmtpSettings() if creating_sender else config.get_sender(sender_profile_id)
    except (DeliveryError, NotFoundError):
        if form_values is None:
            raise
        # Failed submissions keep their supplied identity; never silently save as default.
        smtp = SmtpSettings()
    if form_values is not None:
        smtp = SimpleNamespace(**{key: form_values.get(key, getattr(smtp, key, '')) for key in
            ('host', 'port', 'username', 'sender_email', 'sender_name', 'use_tls', 'use_ssl')}, password_configured=smtp.password_configured)
    sender_label = (form_values.get('display_name', '') if form_values is not None else '' if creating_sender else senders.get(sender_profile_id, {}).get('display_name', sender_profile_id))
    enabled = bool(form_values.get('enabled')) if form_values is not None else config.enabled
    nav = '' if embedded else ('<nav class="tabs" aria-label="메일 설정 영역">'
        f'<a href="/delivery?tab=accounts" {"aria-current=page" if tab == "accounts" else ""}>발신 계정</a>'
        f'<a href="/delivery?tab=groups#recipient-groups" {"aria-current=page" if tab == "groups" else ""}>수신자 그룹</a></nav>')
    content = ''
    if tab == 'accounts':
        rows = []
        if not embedded:
            for key, account in active_senders.items():
                display = _e(senders.get(key, {}).get('display_name', key))
                manage = '' if key == 'default' else lifecycle_control(f'/delivery/catalog/sender/{key}/delete')
                rows.append(f'<tr><td class="primary-cell"><a class="table-link" href="/delivery?sender={_e(quote(key))}#sender-form">{display}</a><div class="form-help">#{_e(senders.get(key, {}).get("entity_id", ""))}</div>{owner_note(senders.get(key, {}))}</td>'
                    f'<td data-label="발신 주소">{_e(account.sender_email or account.username) or "미입력"}</td><td data-label="인증">{"저장됨" if account.password_configured else "입력 필요"}</td>'
                    f'<td class="actions-cell"><div class="actions"><a class="btn btn-secondary btn-sm" href="/delivery?sender={_e(quote(key))}#sender-form">수정</a>{manage}</div></td></tr>')
            if rows:
                content += '<section class="card" id="sender-accounts"><div class="card-header"><h2 class="card-title">발신 계정</h2><a class="btn btn-primary" href="/delivery?new_sender=true#sender-form">계정 추가</a></div><div class="table-scroll"><table class="responsive-list"><thead><tr><th>계정</th><th>발신 주소</th><th>인증</th><th>관리</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></section>'
            else:
                content += '<p class="muted">발신 계정을 추가하면 Task에서 선택할 수 있습니다.</p>'
        security = 'ssl' if smtp.use_ssl else 'starttls'
        gmail = smtp.host == 'smtp.gmail.com'
        password_hint = ('저장된 비밀번호 유지 · 변경할 때만 입력' if smtp.password_configured else 'Gmail은 앱 비밀번호를 입력하세요.')
        if form_values is not None:
            password_hint = '새 비밀번호는 다시 입력하세요. 비워 두면 기존 비밀번호를 유지합니다.'
        password_hint += ' <a href="https://support.google.com/mail/answer/185833?hl=ko" target="_blank" rel="noopener noreferrer">앱 비밀번호 도움말</a>'
        global_control = ('<hr class="section-divider"><label class="check-field"><input type="checkbox" name="enabled" value="true" '
            + ('checked' if enabled else '') + '>메일 자동 발송 사용</label><p class="form-help">전체 발신 계정에 적용됩니다.</p>') if is_admin() else ''
        form = f'''<section class="card" id="sender-form"><div class="card-header"><h2 class="card-title">{'발신 계정 추가' if creating_sender else _e(sender_label) + ' 설정'}</h2></div>
          <ol class="settings-steps" aria-label="발신 계정 설정 순서"><li><span>1</span>메일 서비스</li><li><span>2</span>계정 입력</li><li><span>3</span>저장</li></ol>
          <form method="POST" action="/delivery/save">{panel_field}<input type="hidden" name="create_sender" value="{'true' if creating_sender else 'false'}"><input type="hidden" name="sender_profile_id" value="{'' if creating_sender and form_values is None else _e(sender_profile_id)}">
          <fieldset data-smtp-provider style="border:0;padding:0;margin:0"><legend class="form-label">메일 서비스</legend><div class="settings-options">
            <label class="setting-option"><input type="radio" name="provider" value="gmail" {'checked' if gmail else ''}>Gmail</label><label class="setting-option"><input type="radio" name="provider" value="custom" {'' if gmail else 'checked'}>다른 SMTP</label></div></fieldset>
          {_field('display_name', '계정 이름', sender_label, required=True)}
          <div class="responsive-grid">{_field('username', '로그인 계정', smtp.username, autocomplete='username')}{_field('password', '앱 비밀번호', '', kind='password', hint=password_hint, required=creating_sender, autocomplete='new-password')}</div>
          <div class="responsive-grid">{_field('sender_name', '메일에 표시할 이름', smtp.sender_name)}{_field('sender_email', '발신 주소', smtp.sender_email, kind='email', hint='비워 두면 로그인 계정 주소 사용')}</div>
          <details data-smtp-custom {'open' if not gmail else ''}><summary>서버·보안 설정</summary>
          <div class="responsive-grid">{_field('host', 'SMTP 서버', smtp.host, required=True)}{_field('port', '포트', smtp.port, kind='number', required=True)}</div>
          <div class="form-group"><label class="form-label" for="smtp-security">보안 연결</label><select id="smtp-security" class="form-select" name="security"><option value="starttls" {'selected' if security == 'starttls' else ''}>STARTTLS · 587</option><option value="ssl" {'selected' if security == 'ssl' else ''}>TLS · 465</option></select></div></details>
          {global_control}
          <div class="save-bar"><a class="btn btn-secondary" href="{_e(cancel_url)}">취소</a><button type="submit" class="btn btn-primary">{'계정 추가' if creating_sender else '변경 사항 저장'}</button></div></form></section>'''
        content += form
        if not creating_sender:
            content += f'<section class="card"><div class="card-header"><h2 class="card-title">연결 확인</h2><form method="POST" action="/delivery/test-connection">{panel_field}<input type="hidden" name="sender_profile_id" value="{_e(sender_profile_id)}"><button class="btn btn-secondary">저장한 계정 연결 확인</button></form></div><p class="form-help">SMTP 로그인까지 확인하며 이메일은 보내지 않습니다.</p></section>'
    else:
        items = []
        for gid, addresses in active_groups.items():
            display = _e(groups.get(gid, {}).get('display_name', gid))
            chips = ''.join(f'<span class="recipient-chip"><span>{_e(address)}</span><form method="POST" action="/delivery/groups/remove">{panel_field}<input type="hidden" name="group_id" value="{_e(gid)}"><input type="hidden" name="email" value="{_e(address)}"><button type="submit" aria-label="{_e(address)} 삭제">×</button></form></span>' for address in addresses)
            manage = '' if gid == 'researchops-admins' else lifecycle_control(f'/delivery/catalog/recipient_group/{gid}/delete')
            items.append(f'<details class="card"><summary>{display} <span class="muted">· {len(addresses)}명</span></summary>{owner_note(groups.get(gid, {}))}<div class="actions">{name_control(f"/delivery/catalog/recipient_group/{gid}/rename", groups.get(gid, {}).get("display_name", gid))}{manage}</div><div>{chips or "<p class=muted>등록된 수신자가 없습니다.</p>"}</div>'
                f'<form class="filter-bar" method="POST" action="/delivery/groups/add">{panel_field}<input type="hidden" name="group_id" value="{_e(gid)}"><div class="form-group"><label class="form-label" for="email-{_e(gid)}">수신자 추가</label><input id="email-{_e(gid)}" type="email" name="email" class="form-input" required></div><button class="btn btn-secondary">추가</button></form></details>')
        content += '<section id="recipient-groups">' + (''.join(items) if not embedded else '')
        content += f'''<section class="card"><h2 class="card-title">수신자 그룹 만들기</h2><form method="POST" action="/delivery/groups/create" style="margin-top:18px">{panel_field}
          <div class="form-group"><label class="form-label" for="new-recipient-group">그룹 이름</label><input id="new-recipient-group" name="display_name" class="form-input" maxlength="200" required></div>
          <div class="form-group"><label class="form-label" for="new-group-emails">이메일 주소</label><textarea id="new-group-emails" name="emails" class="form-textarea" required placeholder="한 줄에 주소 하나 또는 쉼표로 구분"></textarea><p class="form-help">최대 100명 · 중복 주소 자동 정리</p></div>
          <div class="save-bar"><button type="submit" class="btn btn-primary">그룹 만들기</button></div></form></section></section>'''
    if not embedded:
        content += deleted_delivery_entries(catalogs)
    heading = '' if embedded else '<div class="page-heading"><h1>메일 설정</h1></div>'
    return render_base_layout('메일 설정', heading + nav + content, 'delivery', flash)
