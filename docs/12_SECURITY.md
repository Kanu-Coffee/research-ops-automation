# 12. 보안 경계와 한계

## 신뢰 모델

Production은 신뢰된 내부 사용자가 작성한 Task를 native Codex/Antigravity CLI로 실행합니다. 조사에 사용하는 외부 문서는 근거 자료이며 추가 실행 권한을 부여하는 지시가 아닙니다. Task 실행 계정과 접근 가능한 host 자료를 운영자가 선택해야 합니다.

Codex는 workspace-write, Antigravity는 호출별 도구 자동 승인과 sandbox 해제를 사용합니다. 파일 경로 분리·작업 지시·사후 감사가 같은 OS 계정의 host 접근을 강제로 격리하지는 않습니다. 전체 egress mediation, 비밀정보와 임의 코드의 강제 격리, 영구 디스크/inode quota, 비신뢰 tenant 격리는 완성되지 않았습니다. 불특정 사용자의 임의 Task를 실행하는 공개 서비스로 사용하지 않습니다.

## 적용하는 보호

- 실제 이메일 주소·SMTP 자격증명·미디어 토큰을 worker 입력과 archive에 넣지 않습니다. 인증 CLI는 기존 로그인을 사용하고 인증 파일을 workspace에 복사·symlink하지 않습니다.
- Task와 단계별 staging, 앱 소유 DB·archive·SMTP 설정의 역할을 분리합니다. 안전하지 않은 경로·symlink/hardlink·변조 파일·과대 응답을 거부합니다.
- Timeout·취소·process cleanup·terminal 응답과 도구 진단을 확인합니다. 미확인 정리를 성공 처리하거나 잠금을 임의 해제하지 않습니다.
- 권한·소유권을 목록·검색·개별 자료 접근에 적용합니다. 세션·CSRF·Host·실제 proxy peer를 검사하고 외부 Web은 HTTPS를 요구합니다.
- 이메일 원문·첨부·수신자 결합을 전송 직전까지 검증합니다. DATA 이후 불확실한 전송은 자동 재발송하지 않습니다.
- 공용 이미지 취득은 정확한 HTTPS host, DNS/IP와 redirect를 검사합니다. 보호 PDF는 등록된 loopback endpoint와 토큰 파일 참조만 사용합니다.

## 운영 자료와 공개 자료

설정·DB·주소·계정·archive·원본 로그·백업은 private 경로와 적절한 권한으로 보관합니다. 원본 도구 응답과 Task에는 사용자가 입력한 업무정보가 포함될 수 있습니다. 공개 이슈에 올리기 전 별도로 축약·익명화합니다. 예제의 예약 도메인과 가상 이름을 실제 운영 값으로 바꾼 파일은 커밋하지 않습니다.

Web RBAC는 앱 접근 제어이며 동일 UID native runner의 OS 보안 경계를 대체하지 않습니다. systemd resource 설정은 서비스 단위 제한이며 일반 Task의 모든 격리 acceptance를 대신하지 않습니다. [프록시](../deploy/nginx/README.md), [인증](WEB_UI_AUTH.md), [경계 개발 검증](RUNNER_BOUNDARY_VALIDATION.md)을 함께 참고하세요.
