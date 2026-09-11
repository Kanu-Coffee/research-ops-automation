"""Fixed, bounded provider error classifications; never publish raw error text."""

import re


_QUOTA_PREFIXES = ("Individual quota reached.", "You have exhausted your quota on this model.")
_RESET = re.compile(r"(?:^|\s)Resets in (?:(\d{1,3})d)?(?:(\d{1,3})h)?(?:(\d{1,3})m)?(?:(\d{1,3})s)?\.(?:\s|$)")
_STREAM_INTERRUPTED = "The stream was interrupted. Please continue the task you were working on."


def agy_error_diagnostic(result):
    """Inspect only the provider result.error field, never tool/model response text."""
    if result.get("status") == "SUCCESS" and not result.get("error"):
        return None
    error = result.get("error")
    if isinstance(error, str) and len(error) <= 16384 and error.startswith(_QUOTA_PREFIXES):
        diagnostic = {"code": "quota_exhausted"}
        reset = _RESET.search(error)
        if reset and any(reset.groups()):
            seconds = sum(int(value or 0) * unit for value, unit in zip(reset.groups(), (86400, 3600, 60, 1)))
            if 0 < seconds <= 31 * 86400:
                diagnostic["reset_after_seconds"] = seconds
        return diagnostic
    return {"code": "stream_interrupted" if error == _STREAM_INTERRUPTED else "provider_error"}
