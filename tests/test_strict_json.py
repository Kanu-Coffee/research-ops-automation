"""Model-output JSON parsing rejects ambiguity without filtering business data."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from researchops.domain.models import TaskDefinition
from researchops.engine.technical_gate import TechnicalGateValidator
from researchops.errors import HardGateError
from researchops.strict_json import MAX_JSON_INTEGER_DIGITS, strict_json_loads


class TestStrictJson(unittest.TestCase):
    def test_unicode_and_json_values_are_preserved(self):
        payload = {"서울": [None, True, False, -20, 1.5, "\U0001f600"], "unknown": {"raw": "keep"}}
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(strict_json_loads(raw), payload)
        self.assertEqual(strict_json_loads(raw.encode()), payload)
        self.assertEqual(strict_json_loads(r'"\ud83d\ude00"'), "\U0001f600")
        self.assertEqual(strict_json_loads("[1, 2]"), [1, 2])
        self.assertIsNone(strict_json_loads("null"))

    def test_duplicate_keys_at_any_depth_cannot_overwrite_original_data(self):
        for raw in ('{"records":[1],"records":[]}',
                    '{"record":{"value":1,"value":2}}', '{"a":1,"\\u0061":2}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "Duplicate"):
                strict_json_loads(raw)

    def test_nonfinite_literals_and_exponent_overflow_are_rejected(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(number=number), self.assertRaisesRegex(ValueError, "Non-finite"):
                strict_json_loads('{"value":' + number + '}')

    def test_integer_limit_is_explicit_and_does_not_change_global_configuration(self):
        with patch("sys.set_int_max_str_digits", side_effect=AssertionError("No global changes")):
            for sign in ("", "-"):
                with self.subTest(sign=sign), self.assertRaisesRegex(ValueError, "digit limit"):
                    strict_json_loads(sign + "9" * (MAX_JSON_INTEGER_DIGITS + 1))

    def test_invalid_utf8_and_surrogate_escapes_are_rejected(self):
        for raw in (b'"\xff"', '"\ud800"', r'"\ud800"', r'{"\udfff": 1}'):
            with self.subTest(raw=repr(raw)), self.assertRaisesRegex(ValueError, "UTF-8|Unicode"):
                strict_json_loads(raw)

    def test_byte_limit_counts_multibyte_unicode_and_accepts_exact_boundary(self):
        raw = '"서울"'
        size = len(raw.encode())
        for value in (raw, raw.encode()):
            with self.subTest(value=value):
                self.assertEqual(strict_json_loads(value, max_bytes=size), "서울")
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    strict_json_loads(value, max_bytes=size - 1)

    def test_limits_and_input_types_fail_with_valueerror(self):
        for value in (None, 123, bytearray(b"{}")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                strict_json_loads(value)
        for limit in (None, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                strict_json_loads("{}", max_bytes=limit)

    def test_invalid_fenced_trailing_and_bom_json_is_not_silently_repaired(self):
        for raw in ("", "```json\n{}\n```", '{} trailing', '{}{}', '\ufeff{}', '{"x":}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "Invalid JSON syntax"):
                strict_json_loads(raw)

    def test_deep_nesting_is_reported_as_valueerror(self):
        with self.assertRaisesRegex(ValueError, "nesting limit"):
            strict_json_loads("[" * 10000 + "0" + "]" * 10000)


class TestResearchStrictJsonBoundary(unittest.TestCase):
    def setUp(self):
        self.gate = TechnicalGateValidator(Path(__file__).resolve().parents[1] / "schemas")
        self.task = TaskDefinition(id="test-task", name="Test", enabled=False,
            workspace={}, runner={"type": "fake"}, instructions={}, output={}, delivery={})

    def test_research_rejects_duplicate_records_and_nonfinite_business_fields(self):
        payloads = [
            '{"status":"success","summary":"original","records":[{"record_id":"one"}],"records":[]}',
            '{"status":"success","summary":"original","records":[{"record_id":"one","score":1e999}]}',
            '{"status":"success","summary":"original","records":[{"record_id":"one","score":NaN}]}',
        ]
        for raw in payloads:
            with self.subTest(raw=raw), self.assertRaisesRegex(HardGateError, "not valid JSON"):
                self.gate.validate_research_output(raw, self.task)

    def test_research_keeps_raw_bytes_values_and_business_warnings(self):
        raw = '{ "status":"success", "summary":"서울", "records":[{"record_id":"one","score":-20,"optional":null}] }'
        result, warnings = self.gate.validate_research_output(raw, self.task,
            {"properties": {"records": {"items": {"properties": {"optional": {"type": "string"}}}}}})
        self.assertEqual(result.raw_json, raw)
        self.assertEqual(result.records, [{"record_id": "one", "score": -20, "optional": None}])
        self.assertTrue(any(warning.startswith("Lint warning:") for warning in warnings))

    def test_research_retains_existing_twenty_megabyte_limit(self):
        raw = json.dumps({"status": "success", "summary": "x" * 1_000_001, "records": []})
        result, _ = self.gate.validate_research_output(raw, self.task)
        self.assertEqual(result.raw_json, raw)
        with self.assertRaisesRegex(HardGateError, "byte limit"):
            self.gate.validate_research_output(" " * 20_000_001, self.task)
