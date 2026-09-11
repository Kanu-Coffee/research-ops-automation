"""Small shared controls for human names and reversible catalog deletion."""

import html


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def name_control(action, name):
    return (f'<form method="POST" action="{esc(action)}" style="display:flex;gap:6px;flex-wrap:wrap;">'
            f'<input name="display_name" aria-label="표시 이름" class="form-input" '
            f'value="{esc(name)}" maxlength="200" required style="min-width:140px;flex:1;">'
            '<button class="btn btn-secondary btn-sm" type="submit">이름 변경</button></form>')


def lifecycle_control(action, *, restore=False):
    message = ('삭제 목록에서 복구할까요? 자동 예약은 다시 켜지지 않습니다.' if restore else
               '목록과 실행 대상에서 삭제할까요? 과거 이력은 보존하며 삭제 목록에서 복구할 수 있습니다.')
    return (f'<form method="POST" action="{esc(action)}" style="display:inline-block;">'
            f'<button class="btn btn-secondary btn-sm" type="submit" onclick="return confirm(\'{message}\');">'
            f'{"복구" if restore else "삭제"}</button></form>')


def deleted_delivery_entries(entries):
    rows = []
    for kind, items in entries.items():
        for item in items:
            if item.get("deleted_at"):
                rows.append('<tr><td>' + ('수신자 그룹' if kind == 'recipient_group' else '발신 계정') +
                    '</td><td>' + esc(item['display_name']) + '</td><td>#' + esc(item['entity_id']) +
                    '</td><td>' + lifecycle_control(
                        f'/delivery/catalog/{kind}/{item["legacy_key"]}/restore', restore=True) + '</td></tr>')
    return ('<details class="card"><summary>삭제된 그룹·발신 계정 (' + str(len(rows)) + ')</summary>'
            '<p class="form-help">삭제는 선택 목록에서 제외하는 처리입니다. 이력 참조와 보호된 설정은 보존됩니다.</p>'
            '<div class="table-scroll"><table><thead><tr><th>종류</th><th>이름</th><th>관리번호</th><th>관리</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></details>')
