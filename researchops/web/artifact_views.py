"""Run-only artifact diagnostics, rendered from the controller's safe query DTO."""

import html
from urllib.parse import quote
from researchops.web.formatting import render_size


DECLARED_STATUS_LABELS = {
    "ready": "ready · 원천 준비됨",
    "pending": "pending · 원천 처리 중",
    "unavailable": "unavailable · 원천 제공 불가",
    "failed": "failed · 원천 실패",
    "error": "error · 원천 오류",
    "not_found": "not_found · 원천 자료 없음",
    "unknown": "선언 확인 안 됨",
    "unspecified": "원천 상태 선언 없음",
}
STATUS_LABELS = {"available": "확보·검증 완료", "failed": "확보·검증 실패", "excluded": "전달 대상 제외"}
ROLE_LABELS = {"attachment": "메일 첨부", "inline_image": "본문 이미지", "evidence": "연구 증거"}
SCOPE_LABELS = {"record": "record별 (record)", "run": "실행 전체 (run)", "legacy": "기존 형식 (legacy)"}
CONFIGURATION_ERRORS = {"invalid_artifact_role", "invalid_artifact_scope", "invalid_artifact_role_scope"}
DOWNLOAD_LABELS = {
    "missing": "현재 보관 파일을 찾을 수 없습니다.",
    "changed": "현재 파일의 크기·해시가 기록과 다릅니다.",
    "unavailable": "현재 파일을 안전하게 확인할 수 없습니다.",
    "not_archived": "이 항목의 다운로드 파일이 보관되지 않았습니다.",
}
REASON_LABELS = {
    "available": "파일을 확보하고 검증했습니다.",
    "evidence_only": "연구 증거로 보관하며 메일 작성에는 포함하지 않습니다.",
    "records_excluded": "연결된 record가 이번 전달 대상에서 제외됐습니다.",
    "legacy_unscoped_dedupe": "기존 파일의 record 연결을 확인할 수 없어 전달에서 제외했습니다.",
    "network_disabled": "이 실행에서는 외부 파일 조회가 허용되지 않습니다.",
    "provider_unavailable": "원문 제공자를 사용할 수 없습니다.",
    "local_file_missing": "선언된 파일을 확보하지 못했습니다.",
    "invalid_media": "파일 형식 검증을 통과하지 못했습니다.",
    "mime_type_mismatch": "실제 파일 형식이 선언과 다릅니다.",
    "source_hash_mismatch": "파일 해시가 선언과 다릅니다.",
    "source_size_mismatch": "파일 크기가 선언과 다릅니다.",
    "source_metadata_mismatch": "원본 문서 정보가 요청한 문서와 다릅니다.",
    "metadata_response_invalid": "문서 정보 응답을 검증하지 못했습니다.",
    "source_unauthorized": "원문 접근 인증을 확인하지 못했습니다.",
    "source_not_found": "원천에서 요청한 파일을 찾지 못했습니다.",
    "source_http_error": "원문 제공자가 오류를 반환했습니다.",
    "acquisition_interrupted": "파일 확보 작업이 중단됐습니다.",
    "cancelled": "파일 확보 작업이 취소됐습니다.",
    "unsafe_artifact": "파일 안전성 검증을 통과하지 못했습니다.",
    "malformed_artifact": "파일 요청 형식을 확인할 수 없습니다.",
    "invalid_artifact_role": "지원하지 않는 파일 용도(role)입니다.",
    "invalid_artifact_scope": "파일의 요청 범위(scope) 또는 record 연결 설정을 확인해야 합니다.",
    "invalid_artifact_role_scope": "파일 용도(role)와 요청 범위(scope)의 조합을 지원하지 않습니다.",
    "artifact_limit_exceeded": "요청 가능한 파일 수를 초과했습니다.",
    "artifact_write_failed": "확보한 파일을 저장하지 못했습니다.",
    **dict.fromkeys(("total_bytes_limit", "message_size_limit", "image_size_limit", "body_too_large"), "파일 용량 상한을 초과했습니다."),
    **dict.fromkeys(("phase_timeout", "timeout"), "파일 확보 제한 시간을 초과했습니다."),
    **dict.fromkeys(("destination_denied", "host_denied", "https_required", "port_denied", "invalid_url",
                    "peer_mismatch", "redirect_denied", "invalid_redirect", "too_many_redirects", "redirect_loop",
                    "https_downgrade_denied"), "원문 주소 또는 이동 경로가 허용된 조회 조건과 다릅니다."),
    **dict.fromkeys(("dns_failed", "connection_failed", "tls_verification_failed", "transport_failed"), "원문 서버에 안전하게 연결하지 못했습니다."),
    **dict.fromkeys(("headers_too_large", "incomplete_response", "invalid_response", "invalid_framing",
                    "unsupported_transfer_encoding", "unsupported_content_encoding", "too_many_chunks", "too_many_headers"), "원문 응답을 완전하게 검증하지 못했습니다."),
    **dict.fromkeys(("credential_unavailable", "provider_configuration_invalid"), "보호된 원문 제공자 설정을 확인해야 합니다."),
    **dict.fromkeys(("worker_start_failed", "worker_failed", "worker_cleanup_failed"), "파일 확보 프로세스를 정상적으로 완료하지 못했습니다."),
    "unknown": "진단 사유는 원본 보고서에서 확인하세요.",
}


def _e(value):
    return html.escape(str(value if value is not None else ""))


def render_run_artifact_report(report, run_id):
    """Do not invent missing files for absent, empty, or unreadable reports."""
    if not report or report.get("status") == "not_recorded":
        return ""
    if report.get("status") != "recorded":
        return '''<div class="card" id="requested-artifact-report">
          <div class="card-header"><div class="card-title">요청한 파일 확인</div></div>
          <p role="status">파일 진단 기록을 확인할 수 없습니다. 파일 누락 여부는 확정하지 않았습니다.</p>
          <p class="form-help">보관 파일 목록의 artifact-report.json과 archive manifest 원본을 확인하세요.</p>
        </div>'''
    entries = report.get("entries", [])
    if not entries:
        return ""
    rows = []
    for entry in entries:
        requested = entry.get("requested_record_ids", [])
        selected = entry.get("record_ids", [])
        reason_code = entry.get("reason_code")
        role = ("지원하지 않는 용도" if reason_code == "invalid_artifact_role" else
                ROLE_LABELS.get(entry.get("role"), "확인 안 됨"))
        if "requested_scope" in entry:
            requested_scope = entry["requested_scope"]
            scope = "요청 범위: " + (SCOPE_LABELS.get(requested_scope, "지원하지 않는 범위")
                if requested_scope is not None else "미지정 또는 확인 불가")
        else:
            scope = "기록된 범위: " + SCOPE_LABELS.get(entry.get("scope"), "확인 안 됨")
        association = "용도: " + role + " · " + scope
        if entry.get("scope") == "record":
            association += " · 요청 record: " + ", ".join(requested)
        if requested != selected and entry.get("scope") == "record":
            association += " · 전달 record: " + (", ".join(selected) or "없음")
        declaration = DECLARED_STATUS_LABELS.get(entry.get("declared_status"), DECLARED_STATUS_LABELS["unknown"])
        state = STATUS_LABELS.get(entry.get("status"), "확인 안 됨")
        if entry.get("status") == "failed" and reason_code in CONFIGURATION_ERRORS:
            state = "요청 설정 오류"
        reason = REASON_LABELS.get(reason_code, "")
        included = "포함" if entry.get("include_in_compose") is True else "미포함"
        if (entry.get("role") == "evidence" and entry.get("status") == "available"
                and entry.get("include_in_compose") is False):
            included = "연구 보관 전용 · 미포함"
        download = DOWNLOAD_LABELS.get(entry.get("download_status"), "다운로드 확인 안 됨")
        if entry.get("download_status") == "verified" and entry.get("download_path"):
            href = f'/runs/{quote(str(run_id), safe="")}/artifacts/{quote(entry["download_path"], safe="/")}'
            download = (f'<a href="{_e(href)}" class="btn btn-secondary btn-sm">파일 다운로드 &darr;</a>'
                        f'<span class="form-help"> {render_size(entry.get("size_bytes"))} · 해시 확인</span>')
        else:
            download = _e(download)
        rows.append(f'''<tr>
          <td><strong>{_e(entry.get("artifact_id"))}</strong><div class="form-help">{_e(association)}</div></td>
          <td>{_e(declaration)}</td>
          <td>{_e(state)}<div class="form-help">{_e(reason)}</div></td>
          <td>{included}</td><td>{download}</td>
        </tr>''')
    return f'''<div class="card" id="requested-artifact-report">
      <div class="card-header"><div class="card-title">요청한 파일 확인 ({len(entries)})</div></div>
      <p class="form-help" style="margin-bottom:12px;">원천의 준비 상태와 실행에서 확보한 파일을 구분합니다. 메일 작성 입력에 포함된 파일과 현재 다운로드 가능 여부를 확인하세요.</p>
      <div style="overflow-x:auto;"><table>
        <thead><tr><th>파일 · 연결 record</th><th>원천 선언</th><th>실행 확인</th><th>메일 작성 입력</th><th>현재 파일</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table></div>
    </div>'''
