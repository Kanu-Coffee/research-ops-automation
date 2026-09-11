# 10. 상태·파일·로그

SQLite는 Task·버전·Run·claim·예약 watermark·권한·SMTP attempt/receipt와 content dedupe 이력을 관리합니다. Worker project와 앱 소유 archive·DB·delivery 설정을 논리적으로 분리합니다.

Run archive에는 입력 snapshot, 구조화 결과, 메일 원문, validation report, invocation 로그, artifact manifest/hash를 보존합니다. Archive는 생성 당시 불변 증거이며 이후 SMTP 시도는 별도 원장과 receipt로 추가합니다. DB 최신 상태를 과거 archive의 상태로 덮어쓰지 않습니다.

Production trace 기본 상한은 호출별 stdout+stderr 64 MiB, JSONL 이벤트당 8 MiB, 10,000건, 스트림별 화면 미리보기 64 KiB입니다. 최종 응답/반입 1,000,000 bytes와 파일·MIME 한도는 별도입니다. 중단된 trace의 완료 도구는 부분 진단으로 남지만 실행 성공·cleanup 증거를 대신하지 않습니다.

파일은 안전한 상대 경로·일반 파일·링크·소유권·읽는 중 변경·형식·크기·hash를 검사합니다. 다운로드도 권한과 현재 bytes를 다시 확인합니다. 보관 기간 정리는 기본 dry-run이며 apply 시 복구 가능한 retired 영역으로 이동합니다. [백업](BACKUP_AND_UPGRADE.md), [미디어](MEDIA_ARTIFACTS.md), [대용량 로그](LARGE_TRACE_AND_RECIPIENT_ROUTING.md)를 참고하세요.
