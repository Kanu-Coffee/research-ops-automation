# ResearchOps 개발 지침

이 저장소 전체에 적용한다. 제품은 `codex exec`와 `agy -p`를 실행 엔진으로 사용하는 조사·이메일 자동화 애플리케이션이다.

## 시작

1. `START_HERE.md`, `docs/STATUS.md`와 작업에 관련된 계약 문서를 읽는다.
2. 현재 소스·설정 schema·테스트를 확인한다. 과거 대화나 특정 호스트의 승인 이력을 저장소 계약으로 추정하지 않는다.
3. 작고 검증 가능한 변경을 구현하고 관련 테스트를 실행한다. 구현과 문서를 함께 갱신한다.

## 유지할 계약

- CLI와 Web은 공통 application service를 사용한다. 업무 로직을 화면이나 CLI 핸들러에 중복 구현하지 않는다.
- Research와 Compose를 분리한다. Task별 고정 workspace와 호출별 staging, 앱 소유 감사 영역을 구분한다.
- Production native runner는 신뢰된 내부 사용자용이다. 동일 UID의 host 접근이나 모든 egress가 강제로 격리됐다고 설명하지 않는다.
- 읽을 수 있는 업무 record를 중요도·사실성·coverage 판단으로 조용히 삭제하지 않는다. 선택적 content dedupe는 검증된 발송 이력과 정확히 같은 key/content만 제외한다.
- Compose는 이름/opaque ID로 그룹 하나를 선택한다. 실제 주소·SMTP 자격증명·연결 토큰은 모델이나 worker workspace에 넣지 않는다.
- 검증을 통과한 제목·HTML·plain text는 그대로 보존하고 같은 hash로 전달한다. 앱이 본문을 다시 렌더링하지 않는다.
- Run archive·기존 Task 버전·발송 시도와 receipt를 덮어쓰지 않는다. 실행 claim·fencing·취소·cleanup·중복 방지를 유지한다.
- SMTP 성공은 모든 RCPT와 최종 DATA 수락이다. DATA 이후 불확실한 전송은 자동 재발송하지 않는다.
- 업무 날짜·예약·표시는 Asia/Seoul이며 재시도·Compose-only는 원래 논리 날짜를 유지한다.
- Web의 관리자·사용자·조회자, 불변 소유권, Task별 조회 권한 및 Host/Origin/CSRF·proxy peer 검사를 유지한다.

## 변경과 검증

- 기본 검증은 새 임시 runtime·합성 입력·fake runner·SMTP double을 사용한다. 운영 DB·Task·예약·실제 주소를 테스트 fixture로 사용하지 않는다.
- 실제 모델 개발 검증은 명시적 `--live` 경로를 사용한다. CLI 로그인 파일을 출력·복사·symlink하지 않는다. 도구 자기보고와 실제 산출물·종료 증거를 구분한다.
- 외부 메시지 전송·운영 데이터 삭제·배포는 현재 사용자 지시 범위에 따라 판단한다. 이 파일 자체가 그러한 외부 작업을 승인하지 않는다.
- 비밀정보·개인 식별자·운영 task.md/email_spec.md·DB·archive를 커밋하지 않는다. 예제는 가상의 software-releases 프로젝트와 예약 도메인을 사용한다.
- 기존 스크립트와 uv lock을 우선한다. `rg`로 필요한 범위만 검색하고 관련 회귀·compile·build를 수행한다. 검증하지 않은 항목을 통과로 기록하지 않는다.
- 중요한 설계 변경은 `docs/DECISIONS.md`, 공개 릴리스 범위와 제약은 `docs/STATUS.md`에 간결하게 기록한다. 사내 사고 기록·호스트·계정·Run 식별자는 공개 문서에 남기지 않는다.
