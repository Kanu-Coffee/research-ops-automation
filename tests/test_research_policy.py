"""Research content lint preserves readable records; composition contracts gate."""

import json
from pathlib import Path
import unittest

from researchops.domain.models import ScheduledRun, TaskDefinition
from researchops.engine.composition_input import CompositionInputBuilder
from researchops.engine.technical_gate import TechnicalGateValidator
from researchops.errors import HardGateError


class TestResearchPolicy(unittest.TestCase):
    def setUp(self):
        self.schemas=Path(__file__).resolve().parents[1]/"schemas"
        self.gate=TechnicalGateValidator(self.schemas)
        self.task=TaskDefinition(id="test-task",name="Test",enabled=False,workspace={},runner={"type":"fake"},instructions={},output={},delivery={"allowed_recipient_group_ids":["test-group"]})

    def test_shallow_optional_business_schema_errors_warn_without_losing_data(self):
        payload={"status":"success","summary":"Readable research","records":[{"name":"kept","_source":"retained"}],"optional_notes":123,"metadata":{}}
        schema={"type":"object","properties":{"optional_notes":{"type":"string"},
            "metadata":{"type":"object","required":["label"]},
            "records":{"type":"array","items":{"type":"object","required":["optional_description"]}}}}
        result,warnings=self.gate.validate_research_output(json.dumps(payload),self.task,schema)
        self.assertEqual(result.records[0]["name"],"kept")
        self.assertEqual(result.records[0]["_source"],"retained")
        self.assertEqual(json.loads(result.raw_json),payload)
        self.assertEqual(len([warning for warning in warnings if warning.startswith("Lint warning:")]),3)

    def test_minimum_envelope_is_hard_even_if_task_schema_allows_anything(self):
        for payload in ({"summary":"missing status","records":[]},
                        {"status":"success","summary":"bad records","records":["unreadable"]}):
            with self.assertRaises(HardGateError):
                self.gate.validate_research_output(json.dumps(payload),self.task,{})

    def test_required_composition_record_contract_remains_hard(self):
        payload={"status":"success","summary":"kept","records":[{"record_id":"record-one","name":"readable"}]}
        result,_=self.gate.validate_research_output(json.dumps(payload),self.task,
            {"properties":{"records":{"items":{"required":["routing_category"]}}}})
        run=ScheduledRun("run-one","test-task","a"*64,"2026-09-05T00:00:00+00:00","Asia/Seoul","2026-09-05","2026.09.05","manual")
        with self.assertRaises(HardGateError):
            CompositionInputBuilder(self.schemas).build_composition_input(self.task,run,result,result.records,
                record_schema={"type":"object","required":["record_id","routing_category"]})
