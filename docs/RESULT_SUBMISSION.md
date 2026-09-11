# 최종 결과 제출과 안전한 응답 진단

Production은 결과를 프로그램으로 직렬화하고, 작은 고정 파일 참조를 최종 응답으로 반환합니다. 중첩 JSON 문자열을 수작업으로 escape해 작성할 때 생길 수 있는 오류를 줄이며 앱은 파일과 응답을 독립적으로 다시 검증합니다.

## Production 파일 참조 v2

Research와 Compose는 동일한 파일 참조 계약을 사용한다. CLI의 최종 schema는 다음 네
필드만 받는다. 이 `transport_version`은 CompositionInput의 v2/v3/v4와 별개다.

```json
{
  "transport_version": 2,
  "response_file": "submission.json",
  "sha256": "원본 JSON bytes의 소문자 SHA-256 64자리",
  "size_bytes": 1234
}
```

앱은 호출별 새 work 디렉터리에 표준 라이브러리만 사용하는 직렬화 helper와 빈 제출
디렉터리를 준비한다. Worker 입력의 `submission_helper`, `submission_dir`는 이 호출의
경로이며 주소·토큰·SMTP 설정·감사 영역을 포함하지 않는다. Worker는 분석 코드에서 완성한
Python dict를 helper에 전달한다. summary에 JSON 설정을 넣을 때도 `json.dumps`를 사용한다.

```python
import json
import sys

sys.path.insert(0, submission_helper)
from researchops.runners.result_submission import submit_result

document["summary"] = "테스트 설정: " + json.dumps(settings, ensure_ascii=False)
print(submit_result(document, "research", submission_dir))
```

Compose는 `composition_result`, `html`, `text` 세 키를 담은 dict를 제출한다. 제목·본문
문자열을 그대로 직렬화하며 HTML/text를 다시 렌더링하거나 수정하지 않는다. 결과의
`html_path=email.html`, `text_path=email.txt` 고정 경로도 제출 전에 확인한다.

Helper는 JSON 자료형·Unicode·숫자·중첩 한도·단계별 구조·크기를 검사하고 엄격한 재파싱을
통과한 UTF-8 bytes를 `submission.json`에 기록한다. 파일은 새로 생성하며 기존 파일을
덮어쓰지 않는다. stdout에는 프로그램이 계산한 hash/크기 envelope만 출력한다. Worker는
그 작은 객체를 최종 응답으로 제출하며 파일 본문을 다시 문자열로 escape하지 않는다.

## 반입 경계

앱은 helper의 성공 메시지만으로 결과를 인정하지 않는다. 기존 프로세스 cleanup·원격
terminal·권한 거부·Agy generated-file 출처 검증을 통과한 다음 아래 검사를 수행한다.

- 참조의 버전·정확한 키 집합·고정 파일명·hash 형식·정수 크기를 검사한다. URL이나 임의
  절대/상대 경로를 따르지 않는다. 결과 파일은 현재 호출의 `.researchops-submission/` 아래에만 있다.
- 각 경로 단계의 symlink, hardlink, 일반 파일 여부, 실행 계정 소유권, 읽는 중 변경을
  검사한다. 실제 크기와 SHA-256이 최종 envelope와 일치해야 한다.
- 파일 원문은 UTF-8 strict JSON으로 재검증한다. 중복 키·NaN/Infinity·비정상 Unicode·
  과도한 중첩·잘린 JSON·문자열 대신 객체가 필요한 위치의 타입 오류를 거부한다.
- Research/Compose 결과 파일의 직렬화와 단계별 구조·용량을 다시 검사한 뒤 고정 출력
  경로로 반입한다. 취소·무결성·검증 실패 시 결과 반입과 후속 handoff를 진행하지 않는다.

제출 JSON은 **1,000,000 bytes**, 최종 결과 파일 합계도 기존 **1,000,000 bytes** 제한을
유지한다. 최종 envelope의 같은 제한, 별도의 64 MiB trace, 20,000,000-byte artifact
제한은 서로 다른 예산이다. helper/제출 전용 디렉터리는 Research artifact로 반입하지 않는다.

Hash가 확인된 원본 제출 bytes는 보호 감사 영역의 `logs/research.submission.json` 또는
`logs/compose.submission.json`에 남긴다. JSON 문법 오류로 거부돼도 이 증거는 보존하며,
일반 Run 오류 카드에는 본문·문서 키·파일 경로·decoder 원문을 출력하지 않는다.

## 호환성과 CLI 검증

기존 `{"response_json":"..."}` envelope도 계속 엄격히 수신한다. opt-in output-only 개발
runner는 도구를 사용하지 않는 기존 문자열 계약을 유지하고 공통 오류 분류를 사용한다.
자동 unescape·정규식 치환·느슨한 parser·실패 응답의 자동 재실행은 추가하지 않는다.
Task 문서·출력 schema·수신자 모드·기존 archive·DB 계약을 변환하거나 덮어쓰지 않는다.

정적 네 필드 schema와 기존 문자열 envelope의 호환을 유지합니다. 임의 키 객체를 그대로 받는 schema는 provider마다 다르게 지원될 수 있으므로 검증 없이 전환하지 않습니다. CLI version/help와 독립 합성 호출로 terminal·helper·반입까지 확인하며 메타데이터 조회나 exit 0만으로 기능 성공을 판단하지 않습니다.

## 오류 진단

`response_diagnostic`은 고정 `code`, `stage`와 선택적 `line`, `column`, `offset`만 담는다.
행·열은 1부터, offset은 0부터 세는 해당 단계 JSON의 Unicode 문자 위치다. 정확한 위치를
알 수 없는 중복 키·타입·Unicode·크기 오류는 위치를 `null`로 둔다.

| 구분 | 대표 코드 |
|---|---|
| 바깥 JSON·크기 | `outer_json_invalid`, `outer_size_exceeded` |
| envelope·참조 형태 | `envelope_shape_invalid`, `response_json_type_invalid`, `file_reference_invalid` |
| 기존 문자열 내부 JSON·크기·타입 | `inner_json_invalid`, `inner_size_exceeded`, `inner_object_required` |
| 제출 파일 경로/링크/소유권·변경 | `submission_file_unsafe`, `submission_file_changed` |
| 제출 문서 문법·크기·단계 구조 | `submission_json_invalid`, `submission_size_exceeded`, `submission_shape_invalid` |
| 결과 파일 합계 | `import_size_exceeded` |

완료된 MCP/모델 활동과 결과 제출 실패는 별개로 보존한다. 예전 진단 필드가 없는 Run에도
없는 원인을 추측해 추가하지 않으며, 기존 실패 archive의 오류를 소급 수정하지 않는다.
