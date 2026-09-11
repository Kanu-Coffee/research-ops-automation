# 이전 systemd 설계 예제

이 디렉터리의 `*.example`은 초기 설계 참고 자료이며 현 운영용 완성 unit으로 설치하지 않습니다. `__...__` placeholder와 오래된 격리·권한 전제를 포함합니다.

실제 사용자 소유 설치기는 [deploy/systemd-user](../../deploy/systemd-user/)를 렌더링합니다. 전용 계정 기반 관리형 배포의 참고 unit은 [deploy/systemd](../../deploy/systemd/)에 있습니다. 명령과 사전조건은 [설치](../../docs/USER_DEPLOYMENT.md), 현재 보안·SMTP·로그인 계약은 [보안](../../docs/12_SECURITY.md), [SMTP](../../docs/09_EMAIL_AND_CREDENTIALS.md), [인증](../../docs/WEB_UI_AUTH.md)을 따릅니다.

서비스 resource 제한과 작업 경로 설정은 동일 UID의 임의 코드로부터 host를 완전히 격리하는 보증이 아닙니다. 계정 인증 파일을 worker workspace로 복사·symlink하지 않습니다. 기존 서비스를 덮어쓰거나 전역 호스트 정책을 낮춰 예제의 실행을 강제하지 않습니다.
