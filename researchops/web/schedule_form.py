"""Seoul schedule presets shared by form rendering and POST validation."""

import re
from collections.abc import Callable

from researchops.errors import ValidationError
from researchops.services.scheduler import CronExpression


PRESETS = {"daily": "매일", "weekdays": "평일 (월~금)", "weekly": "매주",
           "monthly": "매월", "hourly": "매시간", "custom": "고급: Cron 직접 입력"}
WEEKDAYS = {"1": "월요일", "2": "화요일", "3": "수요일", "4": "목요일",
            "5": "금요일", "6": "토요일", "0": "일요일"}


def cron_from_form(field: Callable[[str, str], str]) -> str:
    preset = field("schedule_preset", "")
    if not preset or preset == "custom":
        cron = field("cron", "0 9 * * *").strip()
    else:
        if preset not in PRESETS:
            raise ValidationError("반복 일정 종류를 다시 선택하세요.")
        clock = field("schedule_time", "09:00")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", clock):
            raise ValidationError("실행 시간을 00:00~23:59 형식으로 입력하세요.")
        hour, minute = (int(part) for part in clock.split(":"))
        if preset == "hourly":
            minute_text = field("schedule_minute", str(minute))
            if not re.fullmatch(r"[0-9]{1,2}", minute_text) or not 0 <= int(minute_text) <= 59:
                raise ValidationError("매시간 실행할 분을 0~59 사이에서 선택하세요.")
            cron = f"{int(minute_text)} * * * *"
        elif preset == "weekly":
            weekday = field("schedule_weekday", "1")
            if weekday not in WEEKDAYS:
                raise ValidationError("실행 요일을 선택하세요.")
            cron = f"{minute} {hour} * * {weekday}"
        elif preset == "monthly":
            day = field("schedule_monthday", "1")
            if not re.fullmatch(r"[0-9]{1,2}", day) or not 1 <= int(day) <= 31:
                raise ValidationError("매월 실행할 날짜를 1~31 사이에서 선택하세요.")
            cron = f"{minute} {hour} {int(day)} * *"
        else:
            cron = f"{minute} {hour} * * {'1-5' if preset == 'weekdays' else '*'}"
    try:
        CronExpression(cron)
    except (ValueError, ValidationError) as exc:
        raise ValidationError("반복 일정이 올바르지 않습니다. Cron은 분 시 일 월 요일의 5개 항목입니다.") from exc
    return cron


def schedule_fields(cron: str) -> dict[str, str]:
    """Only recognize exact presets; preserve all unsupported expressions unchanged."""
    result = {"schedule_preset": "custom", "schedule_time": "09:00", "schedule_weekday": "1",
              "schedule_monthday": "1", "schedule_minute": "0", "cron": cron}
    parts = cron.split()
    if len(parts) != 5 or not parts[0].isdigit() or not 0 <= int(parts[0]) <= 59:
        return result
    minute, hour, day, month, weekday = parts
    if month != "*":
        return result
    result["schedule_minute"] = str(int(minute))
    if hour == "*" and day == weekday == "*":
        result["schedule_preset"] = "hourly"
        return result
    if not hour.isdigit() or not 0 <= int(hour) <= 23:
        return result
    result["schedule_time"] = f"{int(hour):02d}:{int(minute):02d}"
    if day == "*" and weekday in ("*", "1-5"):
        result["schedule_preset"] = "daily" if weekday == "*" else "weekdays"
    elif day == "*" and weekday in WEEKDAYS:
        result.update(schedule_preset="weekly", schedule_weekday=weekday)
    elif day.isdigit() and 1 <= int(day) <= 31 and weekday == "*":
        result.update(schedule_preset="monthly", schedule_monthday=str(int(day)))
    return result


def schedule_summary(cron: str) -> str:
    state = schedule_fields(cron)
    preset = state["schedule_preset"]
    if preset == "custom":
        return f"사용자 지정 · {cron} · 서울 시간"
    if preset == "hourly":
        return f"매시간 {state['schedule_minute']}분 · 서울 시간"
    label = PRESETS[preset]
    if preset == "weekly":
        label += " " + WEEKDAYS[state["schedule_weekday"]]
    elif preset == "monthly":
        label += " " + state["schedule_monthday"] + "일"
    return f"{label} {state['schedule_time']} · 서울 시간"
