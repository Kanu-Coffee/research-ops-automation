"""Read-only Task overview, with no draft, workspace or account settings."""

from html import escape
from urllib.parse import urlencode


def render_viewer_task(task_id, task, entity, runs_page):
    from researchops.web.views import render_base_layout, _badge, _escape
    name = (entity or {}).get("display_name", task_id)
    rows = "".join(f'<tr><td><a class="table-link" href="/runs/{escape(run["run_id"])}">{_escape(run["created_at"])}</a></td>'
                   f'<td>{_badge(run["status"])}</td><td>{_escape(run.get("local_date_display"))}</td></tr>' for run in runs_page["items"])
    if not rows:
        rows = '<tr><td colspan="3">실행 이력이 없습니다.</td></tr>'
    page, total, size = runs_page["page"], runs_page["total"], runs_page["page_size"]
    previous = f'<a class="btn btn-secondary" href="?page={page-1}">이전</a>' if page > 1 else ""
    following = f'<a class="btn btn-secondary" href="?page={page+1}">다음</a>' if page * size < total else ""
    return render_base_layout(name, f'''<p><a class="table-link" href="/tasks">← Task 목록</a></p>
      <div class="card-header"><h1>{escape(name)}</h1><span>{"예약 중" if task.get("enabled") else "수동 실행"}</span></div>
      <section class="card"><h2>실행 이력</h2><div class="table-scroll"><table><thead><tr><th>실행 시각 · 서울</th><th>상태</th><th>업무 날짜</th></tr></thead><tbody>{rows}</tbody></table></div>
      <nav aria-label="실행 이력 페이지">{previous}<span> {page}페이지 · {total}건 </span>{following}</nav></section>''',active_nav="tasks")
