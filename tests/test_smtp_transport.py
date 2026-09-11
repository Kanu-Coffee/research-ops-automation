"""SMTP protocol and timeout regressions with a connected in-memory peer only."""
from collections import deque
from dataclasses import asdict, replace
import errno
import hashlib
import json
from pathlib import Path
import smtplib
import socket
import ssl
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from researchops.delivery.smtp_config import (BuiltinDeliveryConfig, SmtpSettings,
    SMTP_PHASE_TIMEOUT_DEFAULTS, delivery_revision, load_delivery_config, save_delivery_config)
from researchops.delivery.smtp_transport import failure_result, send_message
from researchops.delivery.smtp_dispatcher import SmtpDispatcher
from researchops.errors import DeliveryError


class ConnectedPeer:
    debuglevel = 0

    def __init__(self, *, replies=((354, b"go"), (250, b"accepted")), send_error=None):
        self.events = []
        self.replies = deque(replies)
        self.mail_reply = (250, b"sender accepted")
        self.rcpt_replies = deque(((250, b"recipient accepted"),))
        self.send_error = send_error
        self.sock = self
        self.timeout = None
        self.wire = None

    def settimeout(self, seconds):
        self.timeout = seconds
        self.events.append(("timeout", seconds))

    def mail(self, sender):
        self.events.append(("mail", self.timeout))
        if isinstance(self.mail_reply, Exception):
            raise self.mail_reply
        return self.mail_reply

    def rcpt(self, recipient):
        self.events.append(("rcpt", self.timeout))
        reply = self.rcpt_replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def rset(self):
        self.events.append(("rset", self.timeout))

    def putcmd(self, command):
        self.events.append((command, self.timeout))

    def getreply(self):
        self.events.append(("getreply", self.timeout))
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def send(self, wire):
        self.events.append(("send", self.timeout))
        self.wire = wire
        if self.send_error is not None:
            raise self.send_error


class ScriptedSocket:
    """Exercise smtplib's actual getreply/send wrappers without opening a socket."""
    def __init__(self, replies, *, fail_body_write=False):
        self.replies = deque(replies)
        self.fail_body_write = fail_body_write
        self.writes = []
        self.timeout = None

    def settimeout(self, seconds):
        self.timeout = seconds

    def makefile(self, mode):
        return self

    def readline(self, maximum):
        item = self.replies.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    def sendall(self, raw):
        if self.fail_body_write and raw.endswith(b"\r\n.\r\n"):
            self.writes.append(raw[:5])  # A partial write can precede the timeout.
            raise TimeoutError("synthetic private socket detail")
        self.writes.append(raw)

    def close(self):
        pass


def wrapped_timeout():
    try:
        try:
            raise TimeoutError("sensitive socket host and body")
        except OSError as cause:
            raise smtplib.SMTPServerDisconnected("sensitive SMTP reply") from cause
    except smtplib.SMTPServerDisconnected as exc:
        return exc


class SmtpTransportTests(unittest.TestCase):
    envelope = {"sender": "sender@example.test", "recipients": ["recipient@example.test"]}
    body = b"Subject: synthetic\r\n\r\nhello\r\n"

    def submit(self, peer, *, body=None, before_body=None, **kwargs):
        def record_boundary():
            peer.events.append(("durable_body_boundary", peer.timeout))
        return send_message(peer, self.body if body is None else body, self.envelope,
                            before_body=before_body or record_boundary, **kwargs)

    def test_success_uses_separate_timeouts_and_persists_before_wire_write(self):
        peer = ConnectedPeer()
        observed = []
        result = self.submit(peer, on_stage=observed.append)
        self.assertEqual(result.status, "smtp_accepted")
        self.assertEqual(result.server_reply, "250")
        self.assertFalse(result.retryable)
        operations = [event for event in peer.events if event[0] != "timeout"]
        self.assertEqual(operations, [("mail", 15), ("rcpt", 15), ("data", 120),
            ("getreply", 120), ("durable_body_boundary", 120), ("send", 180), ("getreply", 600)])
        self.assertEqual(peer.timeout, 15)
        self.assertEqual([event["stage"] for event in observed],
                         ["mail", "rcpt", "data_command", "body_transfer", "final_reply"])
        self.assertTrue(result.diagnostics["body_started"])
        self.assertTrue(result.diagnostics["body_completed"])
        self.assertTrue(result.diagnostics["final_reply_received"])
        self.assertEqual(result.diagnostics["body_bytes_confirmed"], len(peer.wire))
        for original in ("sender@example.test", "recipient@example.test", "synthetic", "hello"):
            self.assertNotIn(original, json.dumps(asdict(result)))

    def test_wire_dot_stuffing_matches_smtplib_without_changing_mime_bytes(self):
        for body in (b"Subject: hi\r\n\r\n.one\r\n..two\r\n.\r\n", b"line without CRLF", "서울\r\n.끝".encode()):
            with self.subTest(body=body):
                standard = ConnectedPeer()
                smtplib.SMTP.data(standard, body)
                peer = ConnectedPeer()
                result = self.submit(peer, body=body)
                self.assertEqual(result.status, "smtp_accepted")
                self.assertEqual(peer.wire, standard.wire)
                self.assertEqual(result.diagnostics["mime_bytes"], len(body))
                self.assertEqual(result.diagnostics["wire_bytes"], len(peer.wire))

    def test_two_recipients_all_accepted_before_data(self):
        peer = ConnectedPeer()
        peer.rcpt_replies.append((250, b"second accepted"))
        envelope = {**self.envelope, "recipients": [*self.envelope["recipients"], "second@example.test"]}
        result = send_message(peer, self.body, envelope, before_body=lambda: None)
        self.assertEqual(result.status, "smtp_accepted")
        kinds = [event[0] for event in peer.events]
        self.assertLess(max(index for index, kind in enumerate(kinds) if kind == "rcpt"), kinds.index("data"))

    def test_pre_body_disconnect_preserves_timeout_cause_and_is_retryable(self):
        for stage in ("mail", "rcpt", "data_command"):
            with self.subTest(stage=stage):
                peer = ConnectedPeer(replies=(wrapped_timeout(),)) if stage == "data_command" else ConnectedPeer()
                if stage == "mail":
                    peer.mail_reply = wrapped_timeout()
                elif stage == "rcpt":
                    peer.rcpt_replies = deque((wrapped_timeout(),))
                result = self.submit(peer)
                self.assertEqual(result.status, "failed")
                self.assertTrue(result.retryable)
                self.assertEqual(result.error_code, "smtp_timeout")
                self.assertEqual(result.diagnostics["stage"], stage)
                self.assertEqual(result.diagnostics["exception_class"], "SMTPServerDisconnected")
                self.assertEqual(result.diagnostics["cause_classes"], ["TimeoutError"])
                self.assertNotIn("sensitive", json.dumps(asdict(result)))
                self.assertIsNone(peer.wire)
                self.assertFalse(any(event[0] == "durable_body_boundary" for event in peer.events))

    def test_disconnect_during_write_or_final_reply_is_uncertain_never_retryable(self):
        for peer, stage, completed in ((ConnectedPeer(send_error=wrapped_timeout()), "body_transfer", False),
                (ConnectedPeer(replies=((354, b"go"), wrapped_timeout())), "final_reply", True)):
            with self.subTest(stage=stage):
                result = self.submit(peer)
                self.assertEqual(result.status, "uncertain")
                self.assertFalse(result.retryable)
                self.assertEqual(result.error_code, "smtp_timeout")
                self.assertEqual(result.diagnostics["stage"], stage)
                self.assertEqual(result.diagnostics["body_completed"], completed)
                self.assertEqual(result.diagnostics["body_bytes_confirmed"], len(peer.wire) if completed else 0)

    def test_actual_smtplib_wrapped_read_timeout_distinguishes_handshake_from_final_reply(self):
        for stage in ("data_command", "final_reply"):
            with self.subTest(stage=stage):
                replies = [b"250 sender\r\n", b"250 recipient\r\n"]
                if stage == "final_reply":
                    replies.append(b"354 send body\r\n")
                replies.append(TimeoutError("private read timeout"))
                peer = smtplib.SMTP(local_hostname="synthetic.test")
                peer.sock = ScriptedSocket(replies)
                boundaries = []
                result = send_message(peer, self.body, self.envelope, before_body=lambda: boundaries.append(True))
                self.assertEqual(result.status, "failed" if stage == "data_command" else "uncertain")
                self.assertEqual(result.retryable, stage == "data_command")
                self.assertEqual(result.error_code, "smtp_timeout")
                self.assertEqual(result.diagnostics["cause_classes"], ["TimeoutError"])
                self.assertEqual(result.diagnostics["stage"], stage)
                self.assertEqual(boundaries, [] if stage == "data_command" else [True])
                self.assertNotIn("private", json.dumps(asdict(result)))

    def test_actual_smtplib_partial_body_write_timeout_never_claims_zero_transmission(self):
        peer = smtplib.SMTP(local_hostname="synthetic.test")
        fake_socket = ScriptedSocket([b"250 sender\r\n", b"250 recipient\r\n", b"354 body\r\n"],
                                     fail_body_write=True)
        peer.sock = fake_socket
        result = send_message(peer, self.body, self.envelope, before_body=lambda: None)
        self.assertEqual(result.status, "uncertain")
        self.assertFalse(result.retryable)
        self.assertEqual(result.error_code, "smtp_timeout")
        self.assertTrue(result.diagnostics["body_started"])
        self.assertFalse(result.diagnostics["body_completed"])
        self.assertEqual(result.diagnostics["body_bytes_confirmed"], 0)
        self.assertEqual(fake_socket.writes[-1], self.body[:5])

    def test_mail_recipient_and_data_rejections_distinguish_temporary_from_permanent(self):
        for stage in ("mail", "rcpt", "data_command", "final_reply"):
            for code in (421, 450, 550, 552):
                with self.subTest(stage=stage, code=code):
                    peer = ConnectedPeer()
                    rejected = (code, b"private recipient or message")
                    if stage == "mail":
                        peer.mail_reply = rejected
                    elif stage == "rcpt":
                        peer.rcpt_replies = deque((rejected,))
                    elif stage == "data_command":
                        peer.replies = deque((rejected,))
                    else:
                        peer.replies = deque(((354, b"go"), rejected))
                    result = self.submit(peer)
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.retryable, code < 500)
                    self.assertEqual(result.server_reply, str(code))
                    self.assertEqual(result.diagnostics["body_started"], stage == "final_reply")
                    self.assertNotIn("private", json.dumps(asdict(result)))
                    if stage != "final_reply":
                        self.assertIsNone(peer.wire)
                    if stage == "rcpt":
                        self.assertTrue(any(event[0] == "rset" for event in peer.events))

    def test_invalid_data_handshake_never_sends_and_invalid_final_reply_is_uncertain(self):
        for bad in ((250, b"unexpected"), (354.0, b"float"), (True, b"bool"), None, "private text"):
            with self.subTest(bad=bad):
                peer = ConnectedPeer(replies=(bad,))
                result = self.submit(peer)
                self.assertEqual(result.status, "failed")
                self.assertFalse(result.retryable)
                self.assertEqual(result.error_code, "smtp_invalid_reply")
                self.assertIsNone(peer.wire)
        for bad in ((354, b"unexpected"), (250.0, b"float"), None):
            result = self.submit(ConnectedPeer(replies=((354, b"go"), bad)))
            self.assertEqual(result.status, "uncertain")
            self.assertFalse(result.retryable)

    def test_boundary_rejection_blocks_body_and_does_not_become_network_retry(self):
        peer = ConnectedPeer()
        def reject():
            raise DeliveryError("private cancellation or permission details")
        result = self.submit(peer, before_body=reject)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.retryable)
        self.assertFalse(result.diagnostics["body_started"])
        self.assertIsNone(peer.wire)
        self.assertNotIn("private", json.dumps(asdict(result)))

    def test_progress_callback_failure_after_boundary_is_uncertain(self):
        peer = ConnectedPeer()
        def record(event):
            if event["stage"] == "body_transfer":
                raise OSError(errno.ENOSPC, "private disk path")
        result = self.submit(peer, on_stage=record)
        self.assertEqual(result.status, "uncertain")
        self.assertFalse(result.retryable)
        self.assertTrue(result.diagnostics["body_started"])
        self.assertIsNone(peer.wire)

    def test_invalid_input_and_timeout_do_not_send_commands(self):
        for settings in (SmtpSettings(body_timeout_seconds=0), SmtpSettings(final_reply_timeout_seconds=True)):
            peer = ConnectedPeer()
            result = self.submit(peer, timeouts=settings)
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.retryable)
            self.assertFalse(any(event[0] in ("mail", "data", "send") for event in peer.events))

    def test_phase_timeouts_can_be_configured_independently(self):
        peer = ConnectedPeer()
        result = self.submit(peer, timeouts=SmtpSettings(timeout_seconds=7, data_command_timeout_seconds=8,
            body_timeout_seconds=9, final_reply_timeout_seconds=10))
        self.assertEqual(result.status, "smtp_accepted")
        self.assertEqual([event[1] for event in peer.events if event[0] == "timeout"], [7, 7, 8, 9, 10, 7])

    def test_connect_failure_classification_never_leaks_exception_or_unknown_metadata(self):
        cases = [(wrapped_timeout(), "smtp_timeout", True),
            (smtplib.SMTPAuthenticationError(535, b"private auth"), "smtp_auth_rejected", False),
            (smtplib.SMTPAuthenticationError(454, b"private auth"), "smtp_auth_rejected", True),
            (ssl.SSLCertVerificationError("private certificate host"), "smtp_tls_verification_failed", False),
            (socket.gaierror(socket.EAI_AGAIN, "private host"), "smtp_dns_failed", True),
            (socket.gaierror(socket.EAI_NONAME, "private host"), "smtp_dns_failed", False),
            (ConnectionRefusedError(errno.ECONNREFUSED, "private host"), "smtp_connection_failed", True),
            (PermissionError(errno.EACCES, "private file"), "smtp_transport_failed", False)]
        for exc, code, retryable in cases:
            with self.subTest(code=code, retryable=retryable):
                result = failure_result(exc, diagnostics={"sender": "private address", "elapsed_ms": 12,
                    "stage_timings_ms": {"connect": 12, "private stage": 12}})
                self.assertEqual(result.error_code, code)
                self.assertEqual(result.retryable, retryable)
                self.assertNotIn("private", json.dumps(asdict(result)))

    def test_exception_context_cycle_and_subclass_name_are_bounded(self):
        class SensitiveClassName(smtplib.SMTPServerDisconnected):
            pass
        first, second = SensitiveClassName("secret"), TimeoutError("secret")
        first.__context__, second.__context__ = second, first
        result = failure_result(first)
        self.assertEqual(result.error_code, "smtp_timeout")
        self.assertNotIn("SensitiveClassName", json.dumps(asdict(result)))


class SmtpPhaseSettingsTests(unittest.TestCase):
    def old_revision(self, config, sender="default"):
        old_names = ("host", "port", "use_tls", "use_ssl", "username", "password",
                     "sender_email", "sender_name", "timeout_seconds")
        smtp = config.get_sender(sender)
        payload = {"smtp": {key: getattr(smtp, key) for key in old_names},
                   "recipient_groups": config.recipient_groups}
        if sender != "default":
            payload["sender_profile_id"] = sender
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def test_defaults_preserve_exact_old_revisions_for_default_and_named_senders(self):
        config = BuiltinDeliveryConfig(sender_profiles={"team": SmtpSettings(username="synthetic@example.test")})
        for sender in ("default", "team"):
            self.assertEqual(delivery_revision(config, sender), self.old_revision(config, sender))

    def test_nondefault_phase_budget_binds_only_selected_profile(self):
        config = BuiltinDeliveryConfig(sender_profiles={"team": SmtpSettings()})
        original = {sender: delivery_revision(config, sender) for sender in ("default", "team")}
        config.sender_profiles["team"].final_reply_timeout_seconds = 900
        self.assertEqual(delivery_revision(config), original["default"])
        self.assertNotEqual(delivery_revision(config, "team"), original["team"])
        config.sender_profiles["team"].final_reply_timeout_seconds = 600
        self.assertEqual(delivery_revision(config, "team"), original["team"])

    def test_configuration_roundtrip_and_strict_bounds(self):
        config = BuiltinDeliveryConfig(smtp=SmtpSettings(data_command_timeout_seconds=121,
            body_timeout_seconds=181, final_reply_timeout_seconds=601))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "smtp.yaml"
            save_delivery_config(config, path)
            restored = load_delivery_config(path)
            self.assertEqual(asdict(restored.smtp), asdict(config.smtp))
        for name in SMTP_PHASE_TIMEOUT_DEFAULTS:
            for bad in (0, -1, 3601, True, 1.0, "120", None):
                with self.subTest(name=name, bad=bad), self.assertRaises(DeliveryError):
                    replace(SmtpSettings(), **{name: bad}).validate()


class SmtpConnectionStageTests(unittest.TestCase):
    def settings(self, **changes):
        return SmtpSettings(host="smtp.synthetic.test", username="synthetic@example.test",
                            password="synthetic-secret", **changes)

    def test_starttls_and_auth_stages_are_reported_without_secrets(self):
        observed = []
        peer = MagicMock()
        with patch("smtplib.SMTP", return_value=peer) as connect:
            result = SmtpDispatcher._connect(self.settings(), on_stage=observed.append)
        self.assertIs(result, peer)
        connect.assert_called_once_with("smtp.synthetic.test", 587, timeout=15)
        self.assertEqual([item["stage"] for item in observed],
                         ["connect", "greeting", "tls", "greeting", "auth", "auth"])
        self.assertEqual(set(observed[-1]["stage_timings_ms"]), {"connect", "greeting", "tls", "auth"})
        for secret in ("synthetic-secret", "synthetic@example.test", "smtp.synthetic.test"):
            self.assertNotIn(secret, json.dumps(observed))
        self.assertTrue(all(not item["body_started"] for item in observed))

    def test_connect_greeting_tls_and_auth_failures_preserve_stage_and_exception(self):
        for stage in ("connect", "greeting", "tls", "auth"):
            with self.subTest(stage=stage):
                error = wrapped_timeout()
                peer, observed = MagicMock(), []
                if stage != "connect":
                    operation = {"greeting": peer.ehlo, "tls": peer.starttls, "auth": peer.login}[stage]
                    operation.side_effect = error
                with patch("smtplib.SMTP", return_value=peer,
                           side_effect=error if stage == "connect" else None):
                    with self.assertRaises(smtplib.SMTPServerDisconnected) as raised:
                        SmtpDispatcher._connect(self.settings(), on_stage=observed.append)
                self.assertIs(raised.exception, error)
                self.assertEqual(observed[-1]["stage"], stage)
                classified = failure_result(raised.exception, stage=observed[-1]["stage"], diagnostics=observed[-1])
                self.assertEqual(classified.error_code, "smtp_timeout")
                self.assertTrue(classified.retryable)
                self.assertNotIn("sensitive", json.dumps(asdict(classified)))

    def test_implicit_tls_constructor_remains_connect_and_no_starttls_is_requested(self):
        peer, observed = MagicMock(), []
        with patch("smtplib.SMTP_SSL", return_value=peer):
            SmtpDispatcher._connect(self.settings(use_tls=False, use_ssl=True, port=465), on_stage=observed.append)
        self.assertEqual([item["stage"] for item in observed], ["connect", "greeting", "auth", "auth"])
        peer.starttls.assert_not_called()

    def test_close_failure_does_not_replace_original_auth_rejection(self):
        peer = MagicMock()
        original = smtplib.SMTPAuthenticationError(535, b"private authentication details")
        peer.login.side_effect = original
        peer.close.side_effect = OSError("private close failure")
        with patch("smtplib.SMTP", return_value=peer), self.assertRaises(smtplib.SMTPAuthenticationError) as raised:
            SmtpDispatcher._connect(self.settings())
        self.assertIs(raised.exception, original)


if __name__ == "__main__":
    unittest.main()
