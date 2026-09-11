"""Bounded, read-only windows into preserved raw runner logs."""

import codecs
import re

from researchops.errors import NotFoundError, ValidationError
from researchops.web.file_response import StreamingFileBody

LOG_NAMES = frozenset({"research.stdout", "research.stderr", "compose.stdout", "compose.stderr"})


def read_log_window(app, run_id, name, offset="0"):
    if name not in LOG_NAMES or not re.fullmatch(r"[0-9]{1,18}", str(offset)):
        raise ValidationError("로그 파일과 읽기 위치를 확인하세요.")
    offset = int(offset)
    target = app.runs.get_run_archive_file(run_id, "logs/" + name)
    if target is None:
        raise NotFoundError("보존된 로그를 찾을 수 없습니다.")
    content = StreamingFileBody(target, target.parent.parent)
    try:
        if offset > content.size:
            raise ValidationError("로그 읽기 범위를 확인하세요.")
        content.stream.seek(offset)
        raw = content.stream.read(min(65536, content.size - offset))
        # Keep the next window on a complete UTF-8 boundary. Invalid legacy log
        # bytes are visibly replaced only in this preview; downloads stay exact.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(raw, final=offset + len(raw) >= content.size)
        pending, _ = decoder.getstate()
        next_offset = offset + len(raw) - len(pending)
        return {"text": text, "offset": offset, "next_offset": next_offset,
                "total_bytes": content.size, "has_more": next_offset < content.size}
    finally:
        content.close()
