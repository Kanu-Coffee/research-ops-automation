# 운영 가이드

앱 로그인 뒤 발신 계정·수신자 그룹·Task를 등록하고 실행합니다. 아래 프로젝트·그룹 이름은 가상의 소프트웨어 릴리스 보고 예시입니다. 실제 이메일 주소와 비밀번호는 UI의 보호된 설정에서 직접 입력합니다.

## 1. 계정과 전달 설정

관리자는 모든 Task와 계정·시스템 설정을 관리합니다. 사용자는 본인의 발신 계정·수신자 그룹·Task를 만들고 운영합니다. 조회자는 관리자가 허용한 Task의 결과만 읽습니다. [권한 안내](WEB_UI_AUTH.md)에 소유권·계정 비활성화의 영향을 설명합니다.

1. **발신 계정**에서 계정 관리 이름을 정하고 SMTP 서비스·호스트·포트·TLS·로그인·비밀번호·From 주소를 저장합니다. Gmail 기본 조합은 STARTTLS 587이며 일반 SMTP와 TLS 465도 지원합니다.
2. **수신자 그룹**에 예를 들어 `release-team`, `release-stakeholders`를 만들고 운영자가 승인한 실제 주소를 입력합니다. 모델에는 그룹 이름/ID만 제공됩니다.
3. 관리자는 전역 전달 사용 여부를 확인합니다. 연결 확인·테스트 메일은 별도 버튼입니다. 연결 확인은 인증 진단이고, 테스트 메일은 입력한 주소로 실제 메시지를 보내는 행동입니다.

앱 설정 파일의 실제 발송 구성은 아래와 같습니다. 초기 설치의 다른 설정을 보존하고 이 항목만 의도적으로 변경합니다. 비밀번호와 주소는 여기에 적지 않습니다.

```yaml
environment: production
scheduler:
  enabled: true
delivery:
  publisher: builtin_smtp
  default_mode: handoff
  global_handoff_kill_switch: false
  require_verified_receipt_for_success: true
```

보호된 delivery 설정의 enabled와 유효 발신 계정, 별도 SMTP worker도 필요합니다. 설정 파일을 직접 변경한 경우 관련 서비스를 재시작해 새 설정을 읽힙니다. Production은 Task 발행마다 candidate dry-run이나 별도 `approve-delivery` 기록을 요구하지 않습니다.

## 2. Task 작성

**새 Task**에서 조사 내용 → 메일·수신자 → 실행·일정 → 확인 순서로 작성합니다.

- 조사 지시문: 가상 Atlas Notes·Cedar Board의 버전 변경, 배포일, 주요 변경사항, 근거 URL을 정리하도록 작성합니다. 실운영에서는 사용자가 선택한 신뢰할 수 있는 자료를 지정합니다.
- 메일 규격: 독자·문체·제목 규칙·단락·변경 없음 처리·첨부 정책을 작성합니다. 앱의 기술적인 CID/hash 규칙을 문서에 반복해 넣을 필요는 없습니다.
- 수신자 선택: 새 Task의 등록 그룹명 모드에서 예를 들어 일반 업데이트는 `release-team`, 중요한 호환성 변경은 `release-stakeholders`를 선택하도록 규칙을 작성합니다. 주소는 쓰지 않습니다.
- AI 설정: Research와 Compose에 각각 엔진·모델·사고 수준을 선택합니다. 모델 목록 새로고침은 메타데이터 조회이며 실제 추론 성공을 보증하지 않습니다.
- 마지막 행동: **저장만**이 기본입니다. **즉시 실행** 또는 **예약**을 선택하면 실제 모델 호출과 발송으로 이어집니다.

[공개 software-releases package](../examples/tasks/software-releases/README.md)는 fake runner·네트워크 없음·예약 OFF·dry-run의 독립 합성 예제입니다. 운영 task.md/email_spec.md에서 가져온 내용이 아니며 그대로 실제 공개 소프트웨어 소식을 조사하는 package도 아닙니다.

## 3. 수정·복제·삭제

일반 수정과 **고급 설정 · YAML** 저장은 같은 Task의 새 불변 버전을 적용합니다. ID·소유자·workspace·실행 이력과 사용자가 바꾸지 않은 예약 상태를 보존합니다. 진행 중 실행·전송이나 다른 창의 먼저 저장된 변경이 있으면 충돌을 안내합니다. 저장만으로 즉시 실행하지 않습니다.

고급 화면은 YAML과 두 지시문을 편집하고 추가 schema·보조 파일을 보존합니다. Task ID·소유자·전달 방식·예약 ON/OFF는 이 화면에서 변경할 수 없습니다. 보조 파일을 수정하려면 canonical package를 CLI로 검증·등록합니다. Draft/임시 저장 화면은 없으며 과거 호환 자료는 자동 삭제·발행하지 않습니다.

복제는 새 ID·관리번호로 만들고 원본 지시문·설정·schema·보조 파일을 가져오되 원본 Run·workspace·발송 이력을 복사하지 않습니다. 예약은 기본 OFF입니다.

이름 변경은 참조 키와 전달 revision을 바꾸지 않습니다. 삭제는 catalog 비활성화이며 Task는 예약을 끄고 목록에서 숨깁니다. 이력·비밀번호·파일을 물리 삭제하지 않습니다. 복구해도 자동 예약을 다시 켜지 않습니다. 사용 중인 그룹·발신 계정이나 실행·전송 중인 Task 삭제는 거부할 수 있습니다.

## 4. 실행 상태와 실패 대응

Run 상세의 **요약 / 이메일 / 파일 / 로그·진단**에서 진행 단계와 원문을 확인합니다. 조회된 모델·도구 호출, 부분 trace와 실제 terminal·cleanup을 구분합니다. 보존된 과거 실행에 없는 단계 시간을 추정하지 않습니다.

- **전체 재실행:** Research부터 새로 호출합니다.
- **메일 재작성:** 저장된 Research·파일·그룹 입력을 재사용해 Compose만 호출합니다.
- **작성된 이메일 보내기:** 검증된 메일이 있고 handoff 전에 실패했다면 AI 호출 없이 원문을 전송 준비합니다.
- **이메일만 재시도:** 기존 SMTP 메시지의 원문·첨부·Message-ID·envelope로 새 전송 시도를 등록합니다.

자동 실패 복구가 설정·본문·수신자를 바꾸지 않습니다. 원본 Task 버전과 논리 날짜를 유지하고 현재 전달 계약이 달라졌으면 재사용을 거부할 수 있습니다. 수락 이력이나 해결되지 않은 전달이 있으면 중복 방지 검사가 적용됩니다. 상세 조건과 명령은 [단계별 AI](STAGE_MODEL_SETTINGS.md), [이메일 재시도](EMAIL_RETRY.md)를 따릅니다.

SMTP **수락**은 서버가 모든 RCPT와 최종 DATA를 수락했다는 의미입니다. 수신함 도착 여부는 별도입니다. `uncertain`은 본문 전송 가능성 이후 응답이 불명확한 상태이므로 자동 재전송하지 않습니다. 원격 전달을 확인한 뒤 중복 가능성을 인지한 명시적 수동 재시도만 허용합니다.

## 5. 예약과 dedupe

시간대는 서울입니다. 전역 scheduler와 timer, Task 예약이 모두 켜져야 자동 enqueue합니다. 동일 Task는 직렬 실행하고 이미 queued 실행이 있으면 이후 예약을 합칠 수 있습니다. 오래 걸리는 작업의 모든 미실행 슬롯을 자동 재현하지 않습니다.

Content dedupe는 기본 OFF입니다. ON이면 지정 key/content가 검증된 전체 과거 발송과 정확히 같을 때만 제외합니다. 이름 유사도·AI fingerprint·단순 전일 비교를 사용하지 않습니다. 예를 들어 값이 A→B→A이면 마지막 A가 과거와 같아 제외될 수 있습니다. TTL·날짜별 초기화·직전 상태 비교가 필요한 보고에는 이 기능을 그런 의미로 사용하지 않습니다.

모든 record가 제외되어도 `send_on_empty=true`이면 변경 없음 Compose/전달을 진행하고 false이면 생략합니다. 실행/예약/메일의 중복 처리 방지는 content dedupe와 무관하게 유지합니다.

## 6. 파일과 MCP

Research가 명시적으로 요청한 PDF·공식 이미지만 앱이 취득합니다. 원천의 ready 선언, 실제 확보·실패·제외, Compose 포함 여부와 현재 다운로드 가능 여부는 다릅니다. 기본 파일 취득 실패는 진단을 남기고 확보한 내용으로 계속하며, Task의 명시적 `hold`/`announce_missing` 정책을 따릅니다. 검증 완료 파일의 변조나 안전성 실패는 계속 발송하지 않습니다.

CardRAG 지원은 선택적 source protocol입니다. 등록된 loopback endpoint와 보호된 토큰 참조를 운영자가 구성해야 하며 기본 가상 소프트웨어 예제에는 필요하지 않습니다. [미디어 계약](MEDIA_ARTIFACTS.md), [Research 파일 활용](RESEARCH_FILE_ACCESS.md)을 참고하세요.

각 CLI의 native MCP 설정과 인증을 사용합니다. 한 엔진에 등록한 서버가 다른 엔진에도 자동 등록되지 않습니다. System Health/Doctor의 등록 메타데이터와 실제 Run의 도구 성공은 별개입니다. Provider별 예방 제어·generated-file 감사의 한계는 [MCP 연동](MCP_INTEGRATION_PLAN.md)을 확인합니다.

## 서비스와 자료 보존

```bash
systemctl --user status researchops-worker.service researchops-web.service researchops-smtp.service --no-pager
systemctl --user list-timers researchops-scheduler.timer
journalctl --user -u researchops-worker.service -u researchops-smtp.service --since today
```

관리자의 계정 비활성화는 로그인·세션만 막고 기존 Task 예약·실행·SMTP를 멈추지 않습니다. 중단 대상은 별도로 예약/실행 상태를 확인합니다. 업그레이드와 복원은 [백업 가이드](BACKUP_AND_UPGRADE.md)대로 전체 서비스를 같은 버전으로 전환하며 원장을 과거 백업으로 자동 되돌리지 않습니다.
