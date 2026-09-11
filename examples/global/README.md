# 전역 설정 예제

`settings.example.yaml`은 개발용 ResearchOps 설정이다. 실제 계정, 수신자, 운영 경로와
업무 Task를 포함하지 않는다. 사용 방법은 [빠른 시작](../../README.md)과
[운영 가이드](../../docs/PRODUCTION_OPERATIONS.md)를 따른다.

- 기본은 `environment: development`, dry-run, 발송 차단이다. 합성 검증을 위한
  설정이며 실제 운영 Task의 실행·발송 전에 별도 dry-run 승인을 요구하는 정책이 아니다.
- SMTP 계정·비밀번호·수신자 주소는 앱이 관리하는 보호된 delivery 설정에 저장한다.
  Task와 worker에는 opaque 그룹·발신 계정 ID만 전달한다.
- Task 패키지, 불변 버전, 공용 project, 실행 archive, 전달 outbox는 용도별 경로를 쓴다.
  Web은 Task를 직접 생성·수정하며 CLI의 후보 버전·draft 호환 명령은 별도로 유지된다.
- Web은 기본 비활성이며 켜면 앱의 관리자 개설·로그인·역할·Task 조회 권한을 사용한다.
  인증이 없는 서비스로 배포하지 않는다. 관리자 개설은 사용자가 직접 완료한다.
- 기본 bind는 loopback이다. 원격 프록시는 `allow_remote_proxy: true`와 정확한
  socket peer, Host·Origin allowlist, TLS 구성을 함께 적용해야 한다. CSRF는 유지한다.
- Production native runner는 신뢰 운영자 모드다. task 범위와 timeout·결과 검증을
  적용하지만 같은 UID의 모든 host 접근이나 네트워크를 OS 수준에서 격리한다는 보장은 없다.
- `examples/external-delivery/`는 legacy gateway 계약 예제다. 현재 SMTP 사용에는
  해당 YAML을 설치하거나 OAuth credential 파일을 만들 필요가 없다.

상대 경로는 설정 파일 위치가 아니라 loader가 결정한 프로젝트/runtime root 기준이다.
설치 시 [배포 가이드](../../docs/17_DEPLOYMENT.md)의 경로와 소유권 설정을 확인한다.
