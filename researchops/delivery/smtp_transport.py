"""SMTP submission stages and safe diagnostics, without connection or queue ownership.

The caller owns TLS/authentication and persists ``before_body`` before any message
bytes can leave. A body write can succeed partially even when sendall raises, so
only a final explicit rejection can make an error after that boundary retryable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import errno
import re
import smtplib
import socket
import ssl
import time
from typing import Callable

from researchops.delivery.smtp_config import SmtpSettings, validate_address
from researchops.errors import DeliveryError


@dataclass(frozen=True)
class SmtpExchangeResult:
    status: str
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    server_reply: str | None = None
    diagnostics: dict = field(default_factory=dict)


_STAGES = frozenset(("connect", "greeting", "tls", "auth", "mail", "rcpt",
                     "data_command", "body_transfer", "final_reply", "preflight"))
_EXCEPTION_TYPES = (TimeoutError, ConnectionResetError, ConnectionRefusedError,
    ConnectionAbortedError, BrokenPipeError, ssl.SSLCertVerificationError, ssl.SSLError,
    socket.gaierror, smtplib.SMTPAuthenticationError, smtplib.SMTPDataError,
    smtplib.SMTPConnectError, smtplib.SMTPHeloError, smtplib.SMTPResponseException,
    smtplib.SMTPServerDisconnected, smtplib.SMTPNotSupportedError, smtplib.SMTPException,
    PermissionError, OSError, DeliveryError, ValueError, TypeError)
_TRANSIENT_ERRNOS = frozenset((errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
    errno.ECONNABORTED, errno.EPIPE, errno.EHOSTUNREACH, errno.ENETUNREACH,
    errno.ENETDOWN, errno.EHOSTDOWN))


def _exception_name(exc):
    # Subclasses may have caller-controlled names. Return a known base type only.
    return next((kind.__name__ for kind in _EXCEPTION_TYPES if isinstance(exc, kind)), "Exception")


def _exception_chain(exc):
    chain, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(chain) < 8:
        chain.append(exc)
        seen.add(id(exc))
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return chain


def failure_result(exc, *, stage="connect", body_started=False, diagnostics=None):
    """Classify connection or exchange failure without storing exception text.

    smtplib wraps socket read/write timeouts as SMTPServerDisconnected; inspect
    bounded exception causes instead of matching its potentially sensitive text.
    """
    stage = stage if stage in _STAGES else "preflight"
    supplied = diagnostics if isinstance(diagnostics, dict) else {}
    details = {"schema_version": 1}
    for key in ("mime_bytes", "wire_bytes", "body_bytes_confirmed", "elapsed_ms", "timeout_seconds"):
        value = supplied.get(key)
        if type(value) is int and 0 <= value <= 2 ** 63 - 1:
            details[key] = value
    for key in ("body_completed", "final_reply_received"):
        if type(supplied.get(key)) is bool:
            details[key] = supplied[key]
    timings = supplied.get("stage_timings_ms")
    if isinstance(timings, dict):
        details["stage_timings_ms"] = {name: value for name, value in timings.items()
            if isinstance(name, str) and name in _STAGES and type(value) is int and 0 <= value <= 2 ** 63 - 1}
    details.update(stage=stage, body_started=bool(body_started))
    chain = _exception_chain(exc)
    details["exception_class"] = _exception_name(exc)
    details["cause_classes"] = list(dict.fromkeys(_exception_name(item) for item in chain[1:]))
    os_error = next((item for item in reversed(chain) if isinstance(item, OSError)), None)
    if os_error is not None and type(os_error.errno) is int and -65535 <= os_error.errno <= 65535:
        details["errno"] = os_error.errno
    timed_out = any(isinstance(item, TimeoutError) or
                    isinstance(item, OSError) and item.errno == errno.ETIMEDOUT for item in chain)
    details["timed_out"] = timed_out
    reply_error = next((item for item in chain if isinstance(item, smtplib.SMTPResponseException)), None)
    reply_code = reply_error.smtp_code if reply_error is not None else None
    if type(reply_code) is not int or not 400 <= reply_code <= 599:
        reply_code = None
    if timed_out:
        code, message, transient = "smtp_timeout", "SMTP operation timed out", True
    elif any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
        code, message, transient = "smtp_tls_verification_failed", "SMTP TLS verification failed", False
    elif reply_code is not None:
        code = "smtp_auth_rejected" if isinstance(reply_error, smtplib.SMTPAuthenticationError) else "smtp_reply_rejected"
        message, transient = "SMTP server rejected the operation", 400 <= reply_code < 500
    elif any(isinstance(item, socket.gaierror) for item in chain):
        dns_error = next(item for item in chain if isinstance(item, socket.gaierror))
        code, message, transient = "smtp_dns_failed", "SMTP hostname lookup failed", dns_error.errno == socket.EAI_AGAIN
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        code, message, transient = "smtp_disconnected", "SMTP connection interrupted", True
    elif os_error is not None and os_error.errno in _TRANSIENT_ERRNOS:
        code, message, transient = "smtp_connection_failed", "SMTP connection failed", True
    elif isinstance(exc, DeliveryError):
        code, message, transient = "smtp_preflight_rejected", "SMTP submission checks failed", False
    else:
        code, message, transient = "smtp_transport_failed", "SMTP operation failed", False
    # This generic helper cannot establish which command a nested SMTP exception
    # belongs to. The send loop handles explicit DATA rejections at the exact site.
    status = "uncertain" if body_started else "failed"
    return SmtpExchangeResult(status, message, code, bool(transient and not body_started),
                              str(reply_code) if reply_code is not None else None, details)


def _reply_code(reply):
    if (not isinstance(reply, tuple) or len(reply) != 2 or type(reply[0]) is not int or
            not 100 <= reply[0] <= 599):
        return None
    return reply[0]


def send_message(server, mime_bytes: bytes, envelope: dict, *, before_body: Callable[[], None],
                 on_stage: Callable[[dict], None] | None = None,
                 timeouts: SmtpSettings | None = None) -> SmtpExchangeResult:
    """Submit one already-connected message. Never retry or close the connection.

    ``on_stage`` is called before each operation with safe progress metadata.
    ``before_body`` must atomically persist the submission boundary and recheck
    cancellation/permission. Returning successfully authorizes this single body.
    """
    limits = timeouts or SmtpSettings()
    started = time.monotonic()
    stage_started = started
    stage = "preflight"
    details = {"schema_version": 1, "stage": stage, "body_started": False,
               "body_completed": False, "final_reply_received": False,
               "mime_bytes": len(mime_bytes) if isinstance(mime_bytes, bytes) else 0,
               "wire_bytes": 0, "body_bytes_confirmed": 0, "stage_timings_ms": {}}

    def snapshot():
        now = time.monotonic()
        timings = dict(details["stage_timings_ms"])
        timings[stage] = timings.get(stage, 0) + round(max(0, now - stage_started) * 1000)
        return {**details, "stage_timings_ms": timings,
                "elapsed_ms": round(max(0, now - started) * 1000)}

    def begin(name, timeout):
        nonlocal stage, stage_started
        now = time.monotonic()
        timings = details["stage_timings_ms"]
        timings[stage] = timings.get(stage, 0) + round(max(0, now - stage_started) * 1000)
        stage, stage_started = name, now
        details.update(stage=name, timeout_seconds=timeout)
        if on_stage is not None:
            on_stage(snapshot())
        if server.sock is None:
            raise smtplib.SMTPServerDisconnected("Missing SMTP connection")
        server.sock.settimeout(timeout)

    def rejected(code, operation):
        valid_rejection = code is not None and 400 <= code <= 599
        status = "failed" if valid_rejection or not details["body_started"] else "uncertain"
        return SmtpExchangeResult(status,
            "SMTP server rejected " + operation if valid_rejection else "Unexpected SMTP reply",
            "smtp_" + stage + "_rejected" if valid_rejection else "smtp_invalid_reply",
            bool(valid_rejection and code < 500), str(code) if code is not None else None, snapshot())

    try:
        limits.validate()
        if not isinstance(mime_bytes, bytes) or not mime_bytes or not isinstance(envelope, dict):
            raise DeliveryError("Invalid SMTP message snapshot")
        validate_address(envelope.get("sender"))
        recipients = envelope.get("recipients")
        if not isinstance(recipients, list) or not recipients:
            raise DeliveryError("Invalid SMTP recipient snapshot")
        for recipient in recipients:
            validate_address(recipient)
        if not callable(before_body) or on_stage is not None and not callable(on_stage):
            raise DeliveryError("Missing SMTP submission boundary callback")
        # Match smtplib.data(bytes): do not normalize or decode the MIME snapshot.
        wire = re.sub(br"(?m)^\.", b"..", mime_bytes)
        if not wire.endswith(b"\r\n"):
            wire += b"\r\n"
        wire += b".\r\n"
        details["wire_bytes"] = len(wire)
        begin("mail", limits.timeout_seconds)
        code = _reply_code(server.mail(envelope["sender"]))
        if code is None or not 200 <= code < 300:
            return rejected(code, "MAIL")
        for recipient in recipients:
            begin("rcpt", limits.timeout_seconds)
            code = _reply_code(server.rcpt(recipient))
            if code is None or not 200 <= code < 300:
                # A failed RSET cannot change the known fact that no body began.
                try:
                    server.rset()
                except Exception:
                    pass
                return rejected(code, "RCPT")
        begin("data_command", limits.data_command_timeout_seconds)
        server.putcmd("data")
        code = _reply_code(server.getreply())
        if code != 354:
            return rejected(code, "DATA")
        before_body()
        details["body_started"] = True
        begin("body_transfer", limits.body_timeout_seconds)
        server.send(wire)
        details.update(body_completed=True, body_bytes_confirmed=len(wire))
        begin("final_reply", limits.final_reply_timeout_seconds)
        code = _reply_code(server.getreply())
        details["final_reply_received"] = code is not None
        if code is not None and 200 <= code < 300:
            return SmtpExchangeResult("smtp_accepted", server_reply=str(code), diagnostics=snapshot())
        return rejected(code, "message body")
    except Exception as exc:
        return failure_result(exc, stage=stage, body_started=details["body_started"], diagnostics=snapshot())
    finally:
        # Do not make the caller's QUIT inherit the much longer final-reply wait.
        try:
            if server.sock is not None:
                server.sock.settimeout(limits.timeout_seconds)
        except Exception:
            pass
