"""Small, secret-free MCP registration and observed-call summaries."""

import html
from researchops.web.formatting import render_size
from researchops.runners.mcp_audit import safe_tool_timing
from researchops.web.timeline_views import format_seoul, format_duration


PROVIDERS = {"codex_exec": "Codex", "antigravity_exec": "Antigravity"}
CALL_STATUSES = {"succeeded": "응답 확인", "failed": "도구 오류",
                 "denied": "권한 거절", "unverified": "응답 확인 안 됨"}
CALL_ERRORS = {"MCP_TOOL_ERROR": "도구 실행 오류", "MCP_PROTOCOL_ERROR": "MCP 응답 오류",
               "MCP_PERMISSION_DENIED": "권한 거절", "MCP_OUTPUT_UNVERIFIED": "응답 증거 없음",
               "MCP_EVIDENCE_INVALID": "응답 증거 불일치"}


def _escape(value):
    return html.escape(str(value if value is not None else "—"))


def _registration_status(server, issues):
    if server.get("enabled") is False:
        return "비활성"
    if server.get("executable", {}).get("configured") and server.get("executable", {}).get("available") is False:
        return "실행 파일 없음"
    if server.get("blocked_env_names"):
        return "환경변수 전달 제한"
    if server.get("missing_env_names"):
        return "필수 환경변수 없음"
    if server.get("remote_env_names"):
        return "원격 환경변수 확인 필요"
    codes = {issue.get("code") for issue in issues if issue.get("server") == server.get("name")}
    if "server_transport_invalid" in codes:
        return "연결 방식 확인 필요"
    if "server_metadata_invalid" in codes:
        return "서버 설정 확인 필요"
    return "등록됨 · 연결 미확인"


def render_mcp_inventory(mcp):
    providers = mcp.get("providers", {})
    rows = []
    for provider, label in PROVIDERS.items():
        inventory = providers.get(provider, {})
        servers = inventory.get("servers", [])
        if not servers:
            codes = {issue.get("code") for issue in inventory.get("issues", [])}
            status = ("CLI 실행 파일 없음" if "cli_unavailable" in codes else
                      "설정 확인 필요" if codes else "직접 등록 서버 없음 · 전체 연결 목록 미확인")
            rows.append(f'<tr><td>{label}</td><td colspan="4">{status}</td></tr>')
        for server in servers:
            transport = {"http": "HTTP", "stdio": "STDIO"}.get(server.get("transport"), "확인 안 됨")
            auth = "설정 있음 · 인증 미확인" if server.get("authentication_configured") else "인증 미확인"
            rows.append(f'<tr><td>{label}</td><td><code>{_escape(server.get("name"))}</code></td>'
                        f'<td>{transport}</td><td>{_registration_status(server, inventory.get("issues", []))}</td><td>{auth}</td></tr>')
        for source in inventory.get("config_sources", []):
            if source.get("status") in {"invalid", "unsafe"}:
                scope = "프로젝트 설정" if source.get("scope") == "project" else "사용자 설정"
                reason = "형식 확인 필요" if source.get("status") == "invalid" else "읽기 권한·파일 안전성 확인 필요"
                rows.append(f'<tr><td>{label}</td><td colspan="4">{scope}: {reason}</td></tr>')
    return '''<div class="card" id="mcp-connections">
        <div class="card-header"><div class="card-title">MCP 연결 설정</div></div>
        <p class="form-help" style="margin-bottom:12px;">각 엔진에 등록한 활성 연결을 Research에서 사용합니다.
        이 화면은 현재 프로세스의 사용자 설정 점검이며 연결·인증 시험을 실행하지 않습니다.
        실제 Worker의 환경과 호출 결과는 실행 상세에서 확인하세요.</p>
        <div style="overflow-x:auto;"><table><thead><tr><th>엔진</th><th>서버</th><th>방식</th>
        <th>등록 상태</th><th>인증</th></tr></thead><tbody>''' + "".join(rows) + '''</tbody></table></div>
        <p class="form-help" style="margin-top:12px;">직접 등록한 MCP 설정의 요약입니다. 플러그인·앱 연결은
        엔진의 native 설정에 따라 추가될 수 있습니다. 선택 서버의 문제로 전체 실행을 차단하지 않습니다.</p>
        </div>'''


def render_mcp_run_audit(audit):
    if not audit or audit.get("status") == "not_recorded":
        return ""
    if audit.get("status") == "unavailable":
        content = '<p class="form-help">보존된 MCP 진단을 읽을 수 없습니다. 원본 검증 기록을 확인하세요.</p>'
    else:
        rows = []
        for phase in audit.get("phases", []):
            label = PROVIDERS.get(phase.get("provider"), "확인 안 됨")
            stage = {"research": "Research", "compose": "Compose"}.get(phase.get("stage"), "확인 안 됨")
            diagnostics = phase.get("trace_diagnostics")
            if diagnostics:
                complete = diagnostics.get("complete") is True
                observation = "로그 해석 완료" if complete else "로그 해석 불완전 · 확인된 호출만 표시"
                if diagnostics.get("legacy_diagnostics_missing"):
                    observation += " · 과거 실행의 부분 진단 미기록"
                else:
                    observation += (f' · 완료 MCP {_escape(diagnostics.get("completed_mcp_count", 0))}건'
                                    f' · 진행 중 도구 {_escape(diagnostics.get("pending_tool_count", 0))}건')
                if diagnostics.get("termination_reason"):
                    observation += " · 종료 원인: " + _escape(diagnostics["termination_reason"])
                if not complete and not diagnostics.get("model_activity_observed"):
                    observation += " · 모델 활동 여부 확인 불가"
                rows.append(f'<tr><td>{label}</td><td>{stage}</td><td colspan="4">{observation}</td></tr>')
            if not phase.get("tools"):
                empty_label = "MCP 호출 진단 미확인" if diagnostics and not diagnostics.get("complete") else "관찰된 MCP 호출 없음"
                rows.append(f'<tr><td>{label}</td><td>{stage}</td><td colspan="4">{empty_label}</td></tr>')
            for tool in phase.get("tools", []):
                status = CALL_STATUSES.get(tool.get("status"), "확인 안 됨")
                reason = CALL_ERRORS.get(tool.get("error_code"), "—")
                if tool.get("event_bytes") is not None:
                    reason += f' · 이벤트 {render_size(tool["event_bytes"])}'
                if tool.get("content_truncated"):
                    reason += " · 응답 생략 표시 있음"
                timing = safe_tool_timing(tool)
                if timing:
                    reason += (f'<br>관찰 시작: {format_seoul(timing.get("observed_started_at"))}'
                               f'<br>관찰 종료: {format_seoul(timing.get("observed_finished_at"))}'
                               f'<br>소요: {format_duration(timing.get("duration_ms"))}')
                else:
                    reason += '<br>호출 시각·소요 미기록'
                rows.append(f'<tr><td>{label}</td><td>{stage}</td><td><code>{_escape(tool.get("server"))}</code></td>'
                            f'<td><code>{_escape(tool.get("tool"))}</code></td><td>{status}</td><td>{reason}</td></tr>')
            if phase.get("omitted_tool_count"):
                rows.append(f'<tr><td>{label}</td><td>{stage}</td><td colspan="4">추가 호출 {_escape(phase["omitted_tool_count"])}건은 원본 검증 기록에서 확인하세요.</td></tr>')
        content = '''<div style="overflow-x:auto;"><table><thead><tr><th>엔진</th><th>단계</th>
            <th>서버</th><th>도구</th><th>호출 결과</th><th>진단</th></tr></thead><tbody>''' + "".join(rows) + '''</tbody></table></div>
            <p class="form-help" style="margin-top:12px;">보존된 실행 증거의 요약입니다. ‘응답 확인’은 해당 호출의
            오류 없는 응답 증거를 확인했다는 뜻이며, 현재 연결 상태나 업무 결과의 사실성을 보장하지 않습니다.
            호출 시간은 앱이 시작·완료 이벤트를 관찰한 서울 시각이며 서버 내부 처리 시간과 다를 수 있습니다.
            원본은 validation-report.json과 단계별 로그에 보존됩니다.</p>'''
    return '<div class="card" id="mcp-call-results"><div class="card-header"><div class="card-title">MCP 호출 결과</div></div>' + content + '</div>'
