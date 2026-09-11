# External Delivery Gateway examples

> 구설계 참고 자료이며 현재 운영 설정으로 설치하지 않는다. 현재 내장 SMTP/보호된 설정/서울 시간 계약은 docs/09_EMAIL_AND_CREDENTIALS.md, 실행 가능한 배포 기준은 deploy/systemd/ 및 docs/17_DEPLOYMENT.md를 따른다. Host 인증 파일을 worker에 복사·symlink하지 않는다.

이 디렉터리의 YAML은 ResearchOps가 아니라 별도 외부 메일 발송 시스템이 소유할 수 있는 설정 형식을 설명한다.

- 공개 예시는 `example.com` 예약 도메인만 사용한다.
- 실제 group membership, mail profile/credential과 route 정책은 외부 시스템의 비공개 저장소에서 관리한다.
- ResearchOps handoff에는 `recipient_group_id`, subject, 검증된 HTML/text·attachment reference와 hash만 들어간다.
- Codex와 ResearchOps는 이 YAML을 읽거나 실제 주소를 해석하지 않는다.
- 외부 시스템은 `message_type + recipient_group_id` route로 sender profile을 선택하고 group ID를 To/Cc/Bcc로 해석한 뒤, idempotency를 적용해 발송하고 receipt를 반환한다.

`routes.example.yaml`은 group과 profile을 잇는 폐쇄형 예시다. 일치하는 route가 없을 때 fallback group/profile로 보내지 않고 요청을 거부한다. 예시 ID는 software-releases task의 allowlist와 이름만 맞춘 것이며 실제 membership이나 발송 승인이 준비됐다는 의미는 아니다.
