"""Strict response failures expose categories and positions, never model content."""

from dataclasses import FrozenInstanceError
import json
import traceback
import unittest
from unittest.mock import patch

from researchops.runners.response_transport import (
    FileResponseReference, RESPONSE_DIAGNOSTIC_CODES, RESPONSE_DIAGNOSTIC_STAGES,
    ResponseTransportError, parse_response_envelope, parse_response_transport, parse_submission_document,
)
from researchops.runners.tool_events import _response, _response_details


class ResponseTransportTests(unittest.TestCase):
    def failure(self, value, code, stage, *, max_bytes=1_000_000, parser=parse_response_transport):
        with self.assertRaises(ResponseTransportError) as caught:
            parser(value, max_bytes=max_bytes)
        error = caught.exception
        self.assertEqual((error.code, error.stage), (code, stage))
        self.assertEqual(set(error.to_dict()), {"code", "stage", "line", "column", "offset"})
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        for rendered in (str(error), repr(error), repr(error.to_dict()), ''.join(traceback.format_exception(error))):
            self.assertNotIn("private_payload_key", rendered)
            self.assertNotIn("synthetic-secret-value", rendered)
        return error

    def test_legacy_text_and_decoded_envelopes_preserve_exact_json_values(self):
        expected = {"title": '서울 "상품"', "path": r"C:\reports\sample", "html": "<p>line\n둘째😀</p>"}
        envelope = {"response_json": json.dumps(expected, ensure_ascii=False)}
        for value in (envelope, json.dumps(envelope, ensure_ascii=False)):
            self.assertEqual(parse_response_transport(value), expected)
            self.assertEqual(parse_response_envelope(value), envelope)
            self.assertEqual(_response(value), (expected, None))
            self.assertEqual(_response_details(value), (expected, None))

    def test_outer_and_inner_syntax_positions_use_their_own_decoded_source(self):
        source = '{\n "private_payload_key": "한글😀",\n "value": }'
        outer = self.failure(source, "outer_json_invalid", "outer_json")
        inner = self.failure({"response_json": source}, "inner_json_invalid", "inner_json")
        self.assertEqual((outer.line, outer.column, outer.offset), (inner.line, inner.column, inner.offset))
        self.assertEqual(outer.line, 3)
        self.assertEqual(source[outer.offset], "}")
        self.assertEqual(outer.column, 11)
        self.assertGreater(len(source[:outer.offset].encode()), outer.offset)

    def test_envelope_shape_string_type_and_inner_type_have_distinct_codes(self):
        self.failure(None, "response_missing", "envelope")
        for value in ([], 1, b'{"response_json":"{}"}', "null", {},
                      {"response_json": "{}", "private_payload_key": "synthetic-secret-value"}):
            self.failure(value, "envelope_shape_invalid", "envelope")
        for value in (None, 1, {}, [], b"{}"):
            self.failure({"response_json": value}, "response_json_type_invalid", "envelope")
        for value in ("[]", "null", "1", '"synthetic-secret-value"'):
            self.failure({"response_json": value}, "inner_object_required", "inner_type")

    def test_json_is_never_unescaped_repaired_or_unwrapped_twice(self):
        for value in ('```json\n{"response_json":"{}"}\n```',
                      r'{\"response_json\":\"{}\"}'):
            self.failure(value, "outer_json_invalid", "outer_json")
        for value in (r'{\"private_payload_key\":\"synthetic-secret-value\"}',
                      '{"private_payload_key":"literal\nnewline"}', "{} trailing", "{not-json}"):
            self.failure({"response_json": value}, "inner_json_invalid", "inner_json")
        nested = {"response_json": '{"private_payload_key":"synthetic-secret-value"}'}
        self.assertEqual(parse_response_transport({"response_json": json.dumps(nested)}), nested)
        self.failure({"response_json": json.dumps(json.dumps({"a": 1}))}, "inner_object_required", "inner_type")

    def test_interchange_rejections_remain_strict_without_fabricated_positions(self):
        invalid = ['{"private_payload_key":1,"private_payload_key":2}', '{"value":NaN}',
                   '{"value":Infinity}', '{"value":1e999}', '{"value":"\\ud800"}',
                   '{"value":' + '9' * 4301 + '}', '[' * 129 + '0' + ']' * 129]
        for source in invalid:
            for value, stage, code in ((source, "outer_json", "outer_json_invalid"),
                                       ({"response_json": source}, "inner_json", "inner_json_invalid")):
                with self.subTest(stage=stage, prefix=source[:12]):
                    error = self.failure(value, code, stage)
                    self.assertEqual((error.line, error.column, error.offset), (None, None, None))

    def test_outer_and_inner_byte_limits_have_exact_utf8_boundaries(self):
        inner = json.dumps({"title": "한글😀"}, ensure_ascii=False)
        envelope = {"response_json": inner}
        outer = json.dumps(envelope, ensure_ascii=False)
        self.assertEqual(parse_response_transport(outer, max_bytes=len(outer.encode())), {"title": "한글😀"})
        self.failure(outer, "outer_size_exceeded", "outer_json", max_bytes=len(outer.encode()) - 1)
        native_outer_bytes = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertEqual(parse_response_transport(envelope, max_bytes=native_outer_bytes), {"title": "한글😀"})
        self.failure(envelope, "outer_size_exceeded", "outer_json", max_bytes=native_outer_bytes - 1)
        self.failure(envelope, "outer_size_exceeded", "outer_json", max_bytes=len(inner.encode()))

    def test_legacy_one_megabyte_inner_cannot_bypass_native_outer_budget(self):
        inner = '{"value":"' + "x" * (1_000_000 - len('{"value":""}')) + '"}'
        self.assertEqual(len(inner.encode()), 1_000_000)
        self.failure({"response_json": inner}, "outer_size_exceeded", "outer_json")
        self.assertEqual(len(parse_submission_document(inner)["value"]), len(inner) - len('{"value":""}'))

    def test_submission_documents_use_safe_strict_json_type_size_and_location_diagnostics(self):
        raw = '{\n "private_payload_key": "한글😀",\n "value": }'.encode()
        error = self.failure(raw, "submission_json_invalid", "submission", parser=parse_submission_document)
        self.assertEqual((error.line, error.column), (3, 11))
        self.assertEqual(raw.decode()[error.offset], "}")
        for source in (b'{"value":1,"value":2}', b'{"value":NaN}', b'{"value":"\xff"}', b'{"value":"\\ud800"}'):
            self.failure(source, "submission_json_invalid", "submission", parser=parse_submission_document)
        self.failure(b"[]", "submission_object_required", "submission", parser=parse_submission_document)
        self.assertEqual(parse_submission_document(b"{}", max_bytes=2), {})
        self.failure(b"{}", "submission_size_exceeded", "submission", max_bytes=1, parser=parse_submission_document)

    def test_response_details_parses_once_and_legacy_wrapper_returns_safe_string(self):
        value = {"response_json": '{"private_payload_key": "synthetic-secret-value"'}
        with patch("researchops.runners.tool_events.parse_response_transport", wraps=parse_response_transport) as parse:
            document, error = _response_details(value)
        parse.assert_called_once_with(value)
        self.assertIsNone(document)
        self.assertIsInstance(error, ResponseTransportError)
        self.assertEqual(_response(value), (None, str(error)))
        self.assertNotIn("private_payload_key", str(error))

    def test_file_reference_is_typed_immutable_and_does_not_open_files(self):
        envelope = {"transport_version": 2, "response_file": "submission.json", "sha256": "a" * 64, "size_bytes": 1}
        with patch("builtins.open") as opened:
            reference = parse_response_transport(json.dumps(envelope))
        opened.assert_not_called()
        self.assertIsInstance(reference, FileResponseReference)
        self.assertNotIsInstance(reference, dict)
        self.assertEqual(reference.to_dict(), envelope)
        self.assertEqual(parse_response_transport(envelope), reference)
        with self.assertRaises(FrozenInstanceError):
            reference.response_file = "other.json"
        self.assertEqual(parse_response_transport({**envelope, "size_bytes": 1_000_000}).size_bytes, 1_000_000)

    def test_file_reference_rejects_unsafe_path_extra_source_version_and_size_types(self):
        valid = {"transport_version": 2, "response_file": "submission.json", "sha256": "a" * 64, "size_bytes": 100}
        changes = [{"transport_version": 1}, {"transport_version": True}, {"transport_version": 2.0},
            {"response_file": "../submission.json"}, {"response_file": "/tmp/submission.json"},
            {"response_file": "submission.json\u0000"}, {"sha256": "A" * 64}, {"sha256": "b" * 63},
            {"size_bytes": 0}, {"size_bytes": -1}, {"size_bytes": True},
            {"size_bytes": 100.0}, {"response_json": "{}"}, {"private_payload_key": "synthetic-secret-value"}]
        for change in changes:
            with self.subTest(change=change):
                self.failure({**valid, **change}, "file_reference_invalid", "envelope")
        for field in valid:
            value = {key: child for key, child in valid.items() if key != field}
            self.failure(value, "file_reference_invalid", "envelope")
        self.failure({**valid, "size_bytes": 1_000_001}, "submission_size_exceeded", "submission")

    def test_public_diagnostic_constructor_only_allows_fixed_categories_and_positions(self):
        self.assertIn("submission_file_unsafe", RESPONSE_DIAGNOSTIC_CODES)
        self.assertIn("submission", RESPONSE_DIAGNOSTIC_STAGES)
        for kwargs in ({"code": "synthetic-secret-value", "stage": "envelope"},
                       {"code": "inner_json_invalid", "stage": "private_payload_key"},
                       {"code": "inner_json_invalid", "stage": "inner_json", "offset": True}):
            with self.assertRaises(ValueError) as caught:
                ResponseTransportError(**kwargs)
            self.assertNotIn("synthetic-secret-value", str(caught.exception))
            self.assertNotIn("private_payload_key", str(caught.exception))
