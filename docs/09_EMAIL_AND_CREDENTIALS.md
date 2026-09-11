# 09. 이메일과 비밀정보

기본 운영 전달은 앱 소유 `builtin_smtp`입니다. 발신 계정·주소 그룹·비밀번호는 보호된 delivery 설정에 저장하고 worker에는 그룹 이름/opaque ID와 Task의 발신 계정 ID만 제공합니다. 모델이 실제 주소나 발신 비밀번호를 결정하거나 조회하지 않습니다.

Web에서 Gmail/STARTTLS 587, 일반 SMTP/STARTTLS 또는 implicit TLS 465를 구성할 수 있습니다. 계정 관리 이름과 메일 From 표시 이름은 별개입니다. 빈 비밀번호 입력은 기존 값을 유지합니다. 연결 확인과 테스트 메일은 별도 행동이며 테스트 메일은 실제 발송입니다. 서비스 정책·인증 방법은 사용하려는 SMTP 공급자의 최신 안내를 따릅니다.

초기 설치는 발송 차단·dry-run·SMTP 비활성 상태입니다. 운영자가 production 발송 설정을 켜고 유효한 Task·그룹·계정을 지정하면 Task 실행은 실제 큐 등록으로 이어집니다. SMTP worker가 고정된 envelope/MIME/revision을 재검증하고 전송합니다. 다른 소유자의 그룹·계정은 선택할 수 없습니다.

성공은 모든 RCPT와 최종 DATA 성공 응답입니다. 일부 수신자 거절, DATA 전 확정 실패, DATA 이후 불확실 전송을 구분합니다. `uncertain`은 자동 재전송하지 않습니다. 확정된 일시적 실패는 제한된 자동 재시도 정책을 적용할 수 있으며 [EMAIL_RETRY](EMAIL_RETRY.md)에 명시되어 있습니다.

비밀번호·토큰을 명령행 인수, 저장소, 일반 로그 또는 worker 환경에 넣지 않습니다. CLI 비밀번호 변경에는 `researchctl delivery config --set-password`의 숨김 입력을 사용합니다. DB 백업에는 계정·소유권·세션 정보가 포함되고 별도의 delivery 설정에는 실제 주소·SMTP 비밀번호가 있으므로 백업도 private하게 보관합니다.

외부 filesystem outbox와 receipt 호환 경로도 남아 있습니다. SMTP의 로컬 검증과 외부 receipt를 같은 신뢰 근거로 취급하지 않으며 [외부 delivery 예제](../examples/external-delivery/README.md)를 참고합니다.
