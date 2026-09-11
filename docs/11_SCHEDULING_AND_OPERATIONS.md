# 11. 예약과 실행 운영

Cron, 업무 날짜, 화면과 SMTP Date는 `Asia/Seoul` 기준입니다. DB에 저장하는 UTC 순간 시각과 논리 날짜를 구분합니다. 재시도와 Compose-only는 원래 논리 날짜를 유지합니다.

Scheduler timer가 주기적으로 `researchctl schedule tick`을 실행하고 due Task를 큐에 등록합니다. 전역 scheduler, Task의 예약 설정, 실제 timer가 모두 필요합니다. 같은 Task와 정확한 예약 시각의 중복 tick은 한 번으로 처리합니다. 하루 여러 시각을 같은 날짜라는 이유로 합치지 않습니다.

같은 Task는 직렬 실행하며 이미 queued 실행이 있으면 후속 예약이 coalesced될 수 있습니다. 기본 `enqueue_once`는 작업이 주기보다 길 때 놓친 모든 슬롯을 무한히 쌓지 않습니다. 이는 content dedupe와 별개입니다.

새 Task 저장의 기본은 실행 없음입니다. 사용자가 즉시 실행 또는 예약을 선택하면 production에서는 실제 모델 호출과 발송으로 이어집니다. Task 삭제는 예약을 끄고 catalog에서 숨기며 복구해도 예약을 다시 켜지 않습니다. 계정 비활성화는 기존 예약을 중단하지 않으므로 중단하려는 Task에서 예약을 별도로 끕니다.

서비스 명령, 실패·확인 필요 상태와 재시도는 [운영 가이드](PRODUCTION_OPERATIONS.md), [EMAIL_RETRY](EMAIL_RETRY.md)를 따릅니다.
