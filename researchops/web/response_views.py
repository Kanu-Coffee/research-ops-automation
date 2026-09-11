"""Fixed labels for safe response parsing diagnostics on the Run page."""

import html


CODE_LABELS = {
    "response_missing": "구조화 응답이 없습니다.",
    "outer_json_invalid": "외부 응답의 JSON 문법을 확인할 수 없습니다.",
    "outer_size_exceeded": "외부 응답의 크기 제한을 초과했습니다.",
    "envelope_shape_invalid": "응답 envelope 형식이 계약과 다릅니다.",
    "response_json_type_invalid": "응답 envelope의 결과 값 형식이 계약과 다릅니다.",
    "inner_json_invalid": "내부 결과의 JSON 문법을 확인할 수 없습니다.",
    "inner_size_exceeded": "내부 결과의 크기 제한을 초과했습니다.",
    "inner_object_required": "내부 결과는 JSON 객체여야 합니다.",
    "file_reference_invalid": "결과 파일 참조 형식이 계약과 다릅니다.",
    "submission_file_unsafe": "제출 파일의 안전성 검증을 통과하지 못했습니다.",
    "submission_file_changed": "제출 파일이 선언된 상태와 다릅니다.",
    "submission_size_exceeded": "제출 파일의 크기 제한을 초과했습니다.",
    "submission_json_invalid": "제출 파일의 JSON 문법을 확인할 수 없습니다.",
    "submission_object_required": "제출 파일의 결과는 JSON 객체여야 합니다.",
    "submission_shape_invalid": "제출 파일의 결과 형식이 계약과 다릅니다.",
    "import_size_exceeded": "결과 반입 크기 제한을 초과했습니다.",
}
STAGE_LABELS = {"outer_json": "외부 JSON", "envelope": "응답 envelope",
                "inner_json": "내부 JSON", "inner_type": "내부 객체 형식",
                "submission": "제출 파일", "import": "결과 반입"}


def render_run_response_diagnostics(report):
    if not report or report.get("status") == "not_recorded":
        return ""
    if report.get("status") != "recorded":
        return '''<div class="card" id="response-diagnostics">
          <div class="card-header"><div class="card-title">결과 응답 진단</div></div>
          <p role="status">응답 오류 진단을 확인할 수 없습니다. 보관된 validation-report.json을 확인하세요.</p>
        </div>'''
    rows = []
    for item in report.get("errors", []):
        code = item.get("code")
        code = code if code in CODE_LABELS else "unknown"
        stage = STAGE_LABELS.get(item.get("stage"), "확인 안 됨")
        phase = {"research": "Research", "compose": "Compose"}.get(item.get("invocation_stage"), "확인 안 됨")
        position = []
        for key, label in (("line", "행"), ("column", "열")):
            value = item.get(key)
            if type(value) is int and 1 <= value <= 2 ** 31 - 1:
                position.append(f"{label} {value}")
        location = " · ".join(position) or "위치 정보 없음"
        rows.append(f'''<tr><td>{phase}</td><td>{stage}</td>
          <td><code>{code}</code><div class="form-help">{html.escape(CODE_LABELS.get(code, "진단 코드를 확인할 수 없습니다."))}</div></td>
          <td>{location}</td></tr>''')
    if not rows:
        return ""
    return f'''<div class="card" id="response-diagnostics">
      <div class="card-header"><div class="card-title">결과 응답 진단</div></div>
      <p class="form-help" style="margin-bottom:12px;">행·열은 표시된 해석 단계의 JSON 문자열 기준입니다. 위치를 확인할 수 없는 오류는 별도로 표시합니다.</p>
      <div style="overflow-x:auto;"><table><thead><tr><th>호출</th><th>해석 단계</th><th>오류</th><th>위치</th></tr></thead>
      <tbody>{''.join(rows)}</tbody></table></div>
    </div>'''
