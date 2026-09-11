# 06. Run 수명주기

Run은 수동 요청 또는 정확한 예약 시각으로 enqueue됩니다. DB transaction에서 Task 버전·논리 날짜·실행 계획·요청키를 고정하고 같은 Task는 직렬 실행합니다. Worker는 claim과 fencing으로 소유권을 얻은 뒤 다음 단계를 진행합니다.

1. 입력 snapshot·workspace 준비.
2. Research 실행과 terminal·process cleanup 확인.
3. 결과 구조·파일 안전성, 선택적 content dedupe, 요청 파일 취득.
4. 그룹·파일·record를 묶은 불변 Compose 입력 저장.
5. Compose 실행, 결과 반입·이메일 검증.
6. Archive 확정·handoff·SMTP 큐 등록.
7. SMTP attempt와 검증된 receipt로 최종 전달 상태 확정.

취소·timeout·실패·전송 불확실성을 구분합니다. 프로세스 또는 원격 turn 종료를 확인하지 못하면 확인 필요 상태와 필요한 claim을 보존합니다. 오래된 claim이라는 이유만으로 실행 중 프로세스를 무시하고 workspace를 재사용하지 않습니다.

`retry`는 새 Run이며 원래 논리 날짜와 원본 Task 버전을 유지합니다. `compose-only`는 검증된 기존 입력을 사용하고 Research를 다시 호출하지 않습니다. `send-email`은 검증된 원문을 재사용하는 전송 전용 실행입니다. [AI 설정](STAGE_MODEL_SETTINGS.md), [메일 재시도](EMAIL_RETRY.md), [타임라인](RUN_TIMELINE.md)을 참고하세요.
