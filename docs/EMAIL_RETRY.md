# SMTP 진단과 이메일만 재시도

완성된 메시지의 전송을 Research/Compose와 분리해 재시도합니다. 원문·첨부·기존 attempt/receipt를 보존하고 전송 불확실 상태를 자동으로 재분류하거나 재발송하지 않습니다.

## SMTP 큐 등록 전 실패한 완성 메일

메일 검증 성공 후 Handoff 생성 전에 실패한 실행에도
**작성된 이메일 보내기**를 제공한다. 기존 Handoff/SMTP 시도가 있는 실행에는 기존
**작성된 이메일만 다시 전송**을 유지한다. 발송 불가 상태는 전송 영역에서 이유를
표시한다. 완성 메일이 있으면 전송 기능을 우선하고 조사/메일 내용 변경은 별도의
접힌 재실행 영역에 둔다. 오류 시 요청 키를 보존하고 차단 사유로 포커스를 이동한다.

```bash
researchctl run send-email-status RUN_ID --json
researchctl run send-email RUN_ID --request-key UNIQUE_REQUEST_KEY
```

이 명령은 `delivery_only` 자식 Run을 큐에 등록한다. 원본 실패 Run과 archive는
그대로 보존하며, 원본 Task 버전·업무 날짜·제목·HTML/text·첨부를 재사용한다.
원본 Run/revision/입력 hash/결과와 파일 hash/manifest hash는 새 실행 계획에 고정한다.
등록 시와 실행 시 원본 DB·archive·본문·첨부·수신자 결합을 다시 검증한다.
새 전달 계약에 필요한 Run ID/revision 결합만 새로 만들며 메일 원문은 바꾸지 않는다.
새 실행의 모델 호출 이력은 비어 있고 Web에는 **AI 실행 없음**을 표시한다.
발송 준비 중 실패한 전송 전용 실행을 재시도하면 원래 메시지 출처를 다시 사용하며
Research/Compose로 전환하지 않는다.

원본은 failed/timed_out, cleanup 확인, 미취소·비시험 실행, business Handoff 없음이
필요하다. 같은 Run 계열의 전송 중·대기·수락 또는 과거 전달 불확실 이력이 있으면
완성 메일의 새 전송을 차단한다. 원자적 Run/실행 계획/요청키 등록으로 중복 클릭을
같은 실행에 연결한다. 기존 SMTP 메시지의 불확실 수동 재시도 정책은 그대로다.

## 과거 Task 버전의 명시적 재실행과 발송

production의 retry/compose_only/delivery_only는 원본 Task 버전을 유지한다.
현재 활성 Task 버전이 다르면 원본과 현재의 **delivery 정의 전체가 동일한 경우에만**
별도 `run_delivery_authorizations`에 현재 활성 버전과 발송 계약 hash를 고정한다.
조사 지시나 AI 설정이 바뀌어도 사용자가 선택한 원본 결과/지시는 재사용할 수 있다.
발신 계정·수신자·발송 모드 등 delivery 정의가 달라지면 등록 전에 거부한다.

이 권한은 Run·계획·요청키와 같은 DB transaction에서 저장하며, 등록 중 활성 버전이
바뀌면 거부한다. 모델 호출 전·Handoff 생성·SMTP 검사·DATA 직전에도 고정된 활성
버전과 현재 정책을 확인한다. 일반 실행과 system alert에는 과거 버전 예외를 적용하지
않는다. 삭제·취소·전역 발송 중지·현재 발신/수신자 유효성 검사는 유지한다.
기존 archive/DB 행을 고치지 않는 가산 테이블이며 새 Run archive에도 권한을 기록한다.

## SMTP 경계와 timeout

연결·greeting·TLS·인증·MAIL·RCPT·DATA 명령·본문 전송·최종 응답을 구별한다.
기존 연결 생성자 내부의 최초 220과 implicit TLS는 connect 단계에 포함한다.
DATA에 대한 354 응답 이후, 본문을 보내기 **전**에 현재 정책·취소·내용 결합을
재검증하고 durable `data_started`를 기록한다. 본문 전송 가능성이 생긴 뒤 응답이
유실되면 `uncertain`을 유지한다. 명시적 4xx/5xx 거절은 확정 실패다.

보호된 SMTP 설정의 기존 `timeout_seconds`(기본 15초)는 연결·인증·envelope에 적용한다.
추가 필드는 `data_command_timeout_seconds: 120`, `body_timeout_seconds: 180`,
`final_reply_timeout_seconds: 600`이며 각각 1~3600초로 설정 가능하다.
새 필드가 생략됐거나 기본값이면 기존 delivery revision hash를 유지한다.
비기본값 변경은 새 revision으로 취급하며 기존 설정 파일을 자동으로 고치지 않는다.

예외 이름·bounded cause 종류·errno·timeout 여부·단계/소요 시간·본문 전송 진행 상태를
보존한다. SMTP 응답 원문·주소·인증·메일 본문은 진단에 넣지 않는다.
`body_bytes_confirmed=0`은 전송된 바이트가 없다는 증거가 아니다. sendall 일부 전송 후
실패할 수 있으므로 `body_started`가 true면 불확실성을 유지한다.

## 자동 재시도

새로 관찰한 본문 이전 일시적 연결/timeout 오류와 명시적인 SMTP 4xx 거절만 대상으로 한다.
기본 설정은 다음과 같으며 `settings.yaml`의 `delivery`에서 관리한다.

```yaml
delivery:
  smtp_auto_retry: true
  smtp_max_attempts: 4
  smtp_retry_base_seconds: 1800
  smtp_retry_expiry_seconds: 86400
```

최초 시도를 포함해 최대 4회, 실패 후 30·60·120분 간격이며 최초 시도 후 24시간을
넘겨 자동 전송하지 않는다. 상한은 10회/7일이다. 대기 시각과 만료 시각을 DB에 저장해
재시작 이후에도 지킨다. 실행 시점과 본문 전송 직전에도 만료를 확인한다.
명시적 수동 시도는 별도 운영 지시이며 오래된 내용/논리 날짜를 재작성하지 않는다.

5xx, 영구 DNS/인증/TLS 검증/설정/무결성 오류는 자동 재시도하지 않는다. 과거 오류는
새로운 원인 증거 없이 자동 재시도 대상으로 승격하지 않는다. 어느 선행 시도라도
전송 여부가 불확실하면 후속 수동 시도의 실패를 근거로 자동 재시도를 시작하지 않는다.
SMTP 수락 뒤 로컬 확정 실패도 자동 재발송하지 않는다. 알림·진단·시험 메일은 자동
재시도 대상이 아니며 실패 알림의 재귀 발송을 만들지 않는다.

## 수동 조작

Run 화면의 **이메일만 재시도**는 기존 MIME·제목·HTML/text·첨부·Message-ID·envelope를
재사용한다. 지연된 자동 시도는 같은 시도를 즉시 대기 상태로 당길 수 있다.
불확실 결과는 별도 동의 폼에서 중복 발송 가능성을 명시적으로 체크하고 사유를 남겨야 한다.
단순 미수신 주장이나 같은 Message-ID만으로 원격 중복 제거를 보장하지 않는다.
수락 이력이 있으면 이 기능으로 다시 전송할 수 없다.

```bash
researchctl delivery retry-email HANDOFF_ID
researchctl delivery retry-email HANDOFF_ID --request-key UNIQUE_REQUEST_KEY
researchctl delivery retry-email HANDOFF_ID --allow-uncertain --reason '운영자 확인 및 중복 가능성 감수 사유'
```

명령과 Web은 큐만 조작한다. 실제 전송은 별도 SMTP worker가 담당한다.
요청키는 중복 클릭/HTTP 재전송/동시 요청을 같은 결과에 연결한다.
확정 실패의 수동 재시도는 현재 활성 Task와 발신 계정·수신자·파일 안전성 및 내용 동일성을
검사한다. 인증/transport 설정만 바뀌고 원래 MIME/envelope와 현재 운영 정책이 일치하면
새 시도에 현재 revision을 고정한다. 기존 queued 시도는 네트워크 없이 종료하고 새로
기록하며, 원래 queued snapshot의 revision을 덮어쓰지 않는다. 주소나 본문이 바뀌면 거부한다.

전체 Retry Run과 Re-compose Only에도 같은 Run 계열의 미해결 전달/진행 상태 검사를
적용한다. 이 검사는 DB transaction 안에서 수행하며 dry-run을 불필요하게 차단하지 않는다.
독립된 정기 조사는 계속할 수 있고, 해당 조사에 전달하는 history는 대기 중인 재시도의
정확한 record 결합을 pending으로 제공해 과거 확정 실패로 오인하지 않게 한다.

## 이력·마이그레이션·복구

`smtp_attempts`의 과거 행·job ID·Message-ID·MIME·receipt·sidecar를 보존한다.
handoff/Message-ID의 단일 시도 UNIQUE 제약을 제거하고 handoff별 attempt 번호와
동시에 하나의 queued/sending 시도 제약을 둔다. 새 시도는 parent job, 요청키,
진단과 다음/만료 시각을 가진다. 모든 시도는 서로 다른 receipt 및 타임라인 단계로 남는다.

원래 Run/archive는 유지하고 DB의 Run/handoff 최신 상태를 갱신한다. 새로운 대기 시도는
`awaiting_receipt`, 수락은 succeeded, 확정 실패는 failed, 미해결 불확실은 needs_attention이다.
불확실 시도 뒤 수동 재시도가 확정 실패해도 원래 uncertain receipt를 논리 결과에 유지한다.
이전 receipt를 대체하는 성공은 검증된 같은 메시지의 시도/receipt 연결로 증명해야 하며,
worker에 제공하는 상태 수정 제안도 이전 receipt provenance를 보존한다.

자동 예약 실패는 이미 관찰한 SMTP 결과 기록을 rollback하지 않는다. 상태/권한 경쟁으로
예약을 만들지 못하면 실제 실패 receipt와 별도 예약 차단 audit를 남긴다.
배포 전에는 독립 DB 복사본에서 기존 열/행·receipt 불변성과 FK/integrity를 검증한다.
운영 전환은 실행/전송 중인 claim이 없을 때 수행하고, 과거 terminal uncertain은
보존 가능한 감사 이력으로 취급한다. DB migration은 current 링크 rollback만으로
되돌아가지 않으므로 새 데이터 발생 여부와 검증된 백업을 함께 판단한다.

구현 검증은 독립 runtime, SMTP double, 실제 smtplib의 메모리 소켓으로 수행한다.
실제 재발송은 명시적 전송 요청/화면 조작이 있을 때만 수행한다. 릴리스 회귀는 SMTP double로 수행하며 실제 메일을 보내지 않는다.
