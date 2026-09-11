"""Small authentication and account-management screens."""

from html import escape


def _e(value):
    return escape(str(value if value is not None else ""), quote=True)


def auth_page(kind="login", *, error="", next_path="/dashboard", username=""):
    setup = kind == "setup"
    title = "관리자 개설" if setup else "로그인"
    token = ('<div class="form-group"><label class="form-label" for="setup-token">초기 설정 토큰</label>'
             '<input class="form-input" id="setup-token" name="setup_token" autocomplete="off" required>'
             '<p class="form-help">서버에서 발급한 30분 유효 토큰을 입력하세요.</p></div>') if setup else ""
    error_html = f'<p class="auth-error" role="alert" tabindex="-1" id="auth-error">{_e(error)}</p>' if error else ""
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
      <link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/icon-32.png"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
      <title>{title} - ResearchOps</title><style>
      *{{box-sizing:border-box}}body{{margin:0;background:#f8fafc;color:#172033;font:16px/1.5 system-ui,-apple-system,sans-serif}}
      main{{max-width:440px;margin:9vh auto;padding:calc(28px + env(safe-area-inset-top)) max(28px,env(safe-area-inset-right)) calc(28px + env(safe-area-inset-bottom)) max(28px,env(safe-area-inset-left))}}.auth-card{{padding:28px;background:white;border:1px solid #e2e8f0;border-radius:12px}}
      h1{{font-size:24px;margin:0 0 24px}}.brand{{font-size:21px;color:#2563eb;font-weight:750;margin-bottom:24px}}
      .form-group{{margin:0 0 18px}}.form-label{{display:block;font-weight:600;margin-bottom:7px}}.form-input{{width:100%;min-height:44px;padding:10px 12px;border:1px solid #b8c3d1;border-radius:6px;font:inherit}}
      .form-input:focus-visible,button:focus-visible,a:focus-visible{{outline:3px solid #93c5fd;outline-offset:2px}}.form-help{{font-size:13px;color:#526176}}
      button{{min-height:44px;font:inherit;border:0;border-radius:6px;padding:10px 16px;cursor:pointer}}.primary{{background:#2563eb;color:white;width:100%;margin-top:8px}}
      .auth-error{{padding:12px;color:#991b1b;background:#fef2f2;border:1px solid #fecaca;border-radius:6px}}a{{color:#2563eb}}.password-field{{display:flex;gap:6px}}.password-field input{{min-width:0}}
      @media(max-width:900px){{main{{margin:4vh auto;padding:calc(16px + env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) calc(16px + env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left))}}.auth-card{{padding:22px}}}}
      </style></head><body><main><div class="brand">ResearchOps</div><section class="auth-card"><h1>{title}</h1>{error_html}
      <form method="POST" action="/{'setup' if setup else 'login'}" data-auth-form>
        <input type="hidden" name="next" value="{_e(next_path)}">{token}
        <div class="form-group"><label class="form-label" for="username">아이디</label><input class="form-input" id="username" name="username" value="{_e(username)}" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="64" required></div>
        <div class="form-group"><label class="form-label" for="password">비밀번호</label><div class="password-field"><input class="form-input" type="password" id="password" name="password" autocomplete="{'new-password' if setup else 'current-password'}" {'minlength="15"' if setup else ''} maxlength="128" required><button type="button" id="show-password" aria-controls="password" aria-pressed="false">표시</button></div>
        {'<p class="form-help">15~128자로 입력하세요.</p>' if setup else ''}</div>
        <button class="primary" type="submit">{'관리자 계정 만들기' if setup else '로그인'}</button>
      </form></section></main><script>
      document.getElementById('show-password').addEventListener('click',function(){{const e=document.getElementById('password');const show=e.type==='password';e.type=show?'text':'password';this.textContent=show?'숨김':'표시';this.setAttribute('aria-pressed',String(show));}});
      document.getElementById('auth-error')?.focus();
      </script></body></html>'''


def password_page(*, must_change=False, error=""):
    from researchops.web.views import render_base_layout
    notice = '<p class="form-help">임시 비밀번호를 새 비밀번호로 변경하면 시작할 수 있습니다.</p>' if must_change else ""
    body = f'''<section class="card" style="max-width:620px"><h1>비밀번호 변경</h1>{notice}
      <form method="POST" action="/account/password" data-auth-form>
      <div class="form-group"><label class="form-label" for="current-password">현재 비밀번호</label><input class="form-input" type="password" id="current-password" name="current_password" autocomplete="current-password" required></div>
      <div class="form-group"><label class="form-label" for="new-password">새 비밀번호</label><input class="form-input" type="password" id="new-password" name="new_password" autocomplete="new-password" minlength="15" maxlength="128" required><p class="form-help">15~128자. 다른 기기의 로그인은 종료됩니다.</p></div>
      <div class="form-group"><label class="form-label" for="confirm-password">새 비밀번호 확인</label><input class="form-input" type="password" id="confirm-password" name="confirm_password" autocomplete="new-password" required></div>
      <button class="btn btn-primary" type="submit">비밀번호 변경</button></form></section>'''
    return render_base_layout("비밀번호 변경", body, active_nav="account", flash={"type":"error","message":error} if error else None)


def users_page(users, tasks, *, selected=None, error="", success=""):
    from researchops.web.views import render_base_layout
    rows = []
    roles = {"admin": "관리자", "user": "사용자", "viewer": "조회자"}
    def owned_summary(value):
        return (f'Task {int(value.get("owned_task_count", 0)):,}개 · 발신 계정 {int(value.get("owned_sender_count", 0)):,}개'
                f' · 수신자 그룹 {int(value.get("owned_recipient_group_count", 0)):,}개')
    for user in users:
        user_id = user["user_id"]
        task_ids = user.get("task_ids", [])
        resources = f'조회 Task {len(task_ids):,}개' if user['role'] == 'viewer' else owned_summary(user)
        rows.append(f'<tr><td class="primary-cell"><a class="table-link" href="/settings/users?user={_e(user_id)}">{_e(user.get("display_name") or user["username"])}</a><div class="form-help">{_e(user["username"])}</div></td>'
                    f'<td data-label="역할">{roles.get(user["role"], "—")}</td><td data-label="상태">{"활성" if user.get("active",user.get("is_active",True)) else "비활성"}</td>'
                    f'<td data-label="관리 항목">{resources}</td></tr>')
    value = selected or {}
    editing = bool(value.get("user_id"))
    grants = set(value.get("task_ids", []))
    task_checks = "".join(f'<label class="task-grant" data-search="{_e((task.get("name") or task.get("display_name") or task["task_id"]).lower())}" style="display:flex;align-items:center;gap:10px;min-height:44px"><input type="checkbox" name="task_ids" value="{_e(task["task_id"])}" {"checked" if task["task_id"] in grants else ""}>{_e(task.get("name") or task.get("display_name") or task["task_id"])}{" (삭제됨 · 이력 조회)" if task.get("deleted_at") else ""}</label>' for task in tasks)
    password = ('<div class="form-group"><label class="form-label" for="temporary-password">임시 비밀번호</label><input class="form-input" id="temporary-password" type="password" name="temporary_password" autocomplete="new-password" minlength="15" maxlength="128" required><p class="form-help">사용자는 처음 로그인한 뒤 비밀번호를 변경합니다.</p></div>') if not editing else ""
    active = (f'<label style="display:flex;align-items:center;gap:10px;min-height:44px"><input type="checkbox" name="active" value="true" {"checked" if value.get("active",value.get("is_active",True)) else ""}>계정 활성</label><p class="form-help">비활성화해도 기존 예약과 메일 작업은 계속됩니다.</p>') if editing else ""
    owned = f'<p class="form-help">소유 항목: {owned_summary(value)}</p>' if editing else ''
    reset = f'''<details class="card"><summary>비밀번호 재설정</summary><form method="POST" action="/settings/users/{_e(value.get('user_id'))}/reset-password"><div class="form-group"><label class="form-label" for="reset-password">새 임시 비밀번호</label><input class="form-input" id="reset-password" type="password" name="temporary_password" minlength="15" maxlength="128" autocomplete="new-password" required></div><button class="btn btn-secondary">재설정하고 기존 로그인 종료</button></form></details>''' if editing else ""
    body = f'''<style>#user-editor :is(input,select,textarea){{scroll-margin-bottom:100px}}#user-editor .save-bar{{margin-top:18px}}</style><div class="card-header"><h1>사용자 관리</h1><a class="btn btn-primary" href="/settings/users?new=true#user-editor">계정 추가</a></div>
      <section class="card"><div class="table-scroll"><table class="responsive-list"><thead><tr><th>계정</th><th>역할</th><th>상태</th><th>관리 항목</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
      <section class="card" id="user-editor"><h2>{"계정 수정" if editing else "계정 추가"}</h2><form method="POST" action="/settings/users{'/' + _e(value['user_id']) if editing else ''}">
      <div class="responsive-grid"><div class="form-group"><label class="form-label" for="user-name">아이디</label><input class="form-input" id="user-name" name="username" value="{_e(value.get('username',''))}" autocomplete="off" required maxlength="64" {'readonly' if editing else ''}></div>
      <div class="form-group"><label class="form-label" for="display-name">표시 이름</label><input class="form-input" id="display-name" name="display_name" value="{_e(value.get('display_name',''))}" maxlength="80"></div></div>
      <div class="form-group"><label class="form-label" for="user-role">역할</label><select class="form-select" id="user-role" name="role"><option value="user" {"selected" if value.get('role','user') == 'user' else ''}>사용자</option><option value="viewer" {"selected" if value.get('role') == 'viewer' else ''}>조회자</option><option value="admin" {"selected" if value.get('role') == 'admin' else ''}>관리자</option></select><p class="form-help" id="role-help"></p></div>{owned}
      {password}{active}<fieldset id="task-grants" style="border:1px solid #e2e8f0;padding:16px;margin:16px 0"><legend>조회 가능한 Task</legend><label class="form-label" for="grant-search">Task 검색</label><input class="form-input" id="grant-search" type="search" placeholder="Task 이름"><div style="max-height:360px;overflow:auto">{task_checks or '<p>등록된 Task가 없습니다.</p>'}</div><p class="form-help">선택한 Task의 결과·메일·파일·상세 로그를 볼 수 있습니다.</p></fieldset>
      <div class="save-bar"><button class="btn btn-primary" type="submit">{"변경사항 저장" if editing else "계정 만들기"}</button></div></form></section>{reset}
      <script>(()=>{{const role=document.getElementById('user-role');const update=()=>{{const grants=document.getElementById('task-grants');grants.hidden=role.value!=='viewer';grants.querySelectorAll('input').forEach(input=>input.disabled=role.value!=='viewer');document.getElementById('role-help').textContent={{user:'자신의 Task·발신 계정·수신자 그룹을 만들고 관리합니다.',viewer:'지정된 Task의 결과와 이력만 조회합니다.',admin:'전체 항목과 사용자·시스템 설정을 관리합니다.'}}[role.value]||'';}};role.addEventListener('change',update);update();document.getElementById('grant-search').addEventListener('input',e=>document.querySelectorAll('.task-grant').forEach(el=>el.hidden=!el.dataset.search.includes(e.target.value.toLowerCase())));}})();</script>'''
    flash = {"type": "error", "message": error} if error else {"type": "success", "message": success} if success else None
    return render_base_layout("사용자 관리", body, active_nav="users", flash=flash)
