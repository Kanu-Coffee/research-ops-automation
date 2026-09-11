"""Unit tests for MessageValidator and HTMLSecurityParser."""

import json
import base64
import hashlib
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from researchops.domain.models import CompositionInput, TaskDefinition
from researchops.engine.message_validator import HTMLSecurityParser, MessageValidator, validate_media
from researchops.errors import ValidationError


class TestMessageValidator(unittest.TestCase):
    def setUp(self):
        self.validator = MessageValidator(Path(__file__).resolve().parents[1] / "schemas")
        self.temp_dir = tempfile.mkdtemp()
        self.stage_out = Path(self.temp_dir) / "output"
        self.stage_out.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sample_task_def(self):
        return TaskDefinition(
            id="test-task",
            name="Test Task",
            enabled=True,
            workspace={"mode": "persistent_task"},
            runner={"type": "fake"},
            instructions={"research_files": ["task.md"], "compose_files": ["task.md"]},
            output={"research_schema": "output.schema.json"},
            delivery={
                "mode": "dry_run",
                "allowed_recipient_group_ids": ["team-product", "team-execs"]
            }
        )

    def _sample_comp_input(self, record_ids=None):
        rec_ids = ["rec-1"] if record_ids is None else record_ids
        return CompositionInput(
            schema_version=2,
            task_id="test-task",
            run_id="run-1",
            task_version_hash="hash1",
            composition_revision=1,
            run={
                "scheduled_for": "2026-09-04T00:00:00Z",
                "timezone": "Asia/Seoul",
                "local_date": "2026-09-04",
                "local_date_display": "2026.09.04"
            },
            result={"status": "success", "summary": "Found 1", "warnings": []},
            coverage={"complete": True, "expected_target_count": 1, "completed_target_count": 1, "issues": []},
            allowed_recipient_group_ids=["team-product", "team-execs"],
            reportable_records=[{"record_id": rid, "title": f"Title {rid}"} for rid in rec_ids],
            inline_artifacts=[],
            attachments=[]
        )

    def _write_valid(self, html=None, record_ids=None):
        comp_input = self._sample_comp_input(record_ids)
        data = {"recipient_group_id":"team-product","recipient_group_reason":"Monitor",
            "subject":"서울 시장 보고서","html_path":"email.html","text_path":"email.txt",
            "included_record_ids":[record["record_id"] for record in comp_input.reportable_records]}
        rows = "".join(f'<div data-record-id="{record["record_id"]}">Record</div>' for record in comp_input.reportable_records)
        html = html or f'<!DOCTYPE html><html><head><title>시장</title></head><body data-local-date="2026-09-04">{rows}</body></html>'
        (self.stage_out/"composition-result.json").write_bytes(json.dumps(data,ensure_ascii=False).encode())
        (self.stage_out/"email.html").write_bytes(html.encode())
        (self.stage_out/"email.txt").write_bytes("서울 시장\r\n둘째 줄\n".encode())
        return comp_input,self._sample_task_def()

    def _validate(self, comp_input=None, task=None):
        return self.validator.validate_composition(self.stage_out,comp_input or self._sample_comp_input(),task or self._sample_task_def())

    @staticmethod
    def _png():
        def chunk(kind,content):
            return struct.pack(">I",len(content))+kind+content+struct.pack(">I",zlib.crc32(kind+content)&0xffffffff)
        return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(b"\x00\xff\x00\x00"))+chunk(b"IEND",b"")

    def _inline(self, content=None):
        raw = self._png() if content is None else content
        (self.stage_out/"chart.png").write_bytes(raw)
        return {"path":"chart.png","cid":"chart-1","mime_type":"image/png",
            "size_bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}

    def test_original_utf8_crlf_bytes_are_not_sanitized_or_reencoded(self):
        comp,task = self._write_valid()
        originals = {name:(self.stage_out/name).read_bytes() for name in ("email.html","email.txt","composition-result.json")}
        _,html,text,hashes = self._validate(comp,task)
        self.assertEqual(html.encode(),originals["email.html"])
        self.assertEqual(text.encode(),originals["email.txt"])
        for kind,name in (("html","email.html"),("text","email.txt"),("composition_result","composition-result.json")):
            self.assertEqual(hashes[kind],hashlib.sha256(originals[name]).hexdigest())
            self.assertEqual((self.stage_out/name).read_bytes(),originals[name])

    def test_invalid_utf8_is_rejected_in_each_message_file(self):
        for filename in ("email.html","email.txt","composition-result.json"):
            with self.subTest(filename=filename):
                self._write_valid()
                (self.stage_out/filename).write_bytes(b"\xff\xfeinvalid")
                with self.assertRaises(ValidationError):
                    self._validate()

    def test_symlink_hardlink_fifo_and_directory_are_rejected_without_blocking(self):
        source = Path(self.temp_dir)/"outside.txt"
        source.write_text("outside")
        for filename in ("email.html","email.txt","composition-result.json"):
            for filetype in ("symlink","hardlink","fifo","directory"):
                with self.subTest(filename=filename,filetype=filetype):
                    self._write_valid()
                    target = self.stage_out/filename
                    target.unlink()
                    if filetype == "symlink":
                        target.symlink_to(source)
                    elif filetype == "hardlink":
                        os.link(source,target)
                    elif filetype == "fifo":
                        os.mkfifo(target)
                    else:
                        target.mkdir()
                    try:
                        with self.assertRaises(ValidationError):
                            self._validate()
                    finally:
                        target.rmdir() if filetype == "directory" else target.unlink()

    def test_css_and_uri_resource_obfuscations_are_rejected_as_whole_messages(self):
        attacks = [
            '<div style=\'background-image:image-set("https://remote.test/pixel" 1x)\'>X</div>',
            '<style>.x{background:-webkit-image-set("https://remote.test/pixel" 1x)}</style>',
            '<div style=\'background:image("https://remote.test/pixel")\'>X</div>',
            '<div style="background:u\\72l(https://remote.test/pixel)">X</div>',
            '<div style="background:u/**/rl(https://remote.test/pixel)">X</div>',
            '<style>@import "https://remote.test/styles.css";</style>',
            '<div style="width:expression(alert(1))">X</div>',
            '<a href="java&#x73;cript:alert(1)">X</a>',
            '<a href="java\nscript:alert(1)">X</a>',
            '<a href="data:text/html,%3Cscript%3Ealert(1)%3C/script%3E">X</a>',
            '<img src="data:image/png;base64,abcd">',
            '<img srcset="https://remote.test/a 1x">',
            '<a href="https://safe.test" ping="https://remote.test/ping">X</a>',
            '<meta http-equiv="refresh" content="0;url=https://remote.test">',
            '<!--[if mso]><img src="https://remote.test/pixel"><![endif]-->',
            '<plaintext><img src="https://remote.test/pixel"></plaintext>',
            '<svg><a xlink:href="https://remote.test">X</a></svg>',
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                self._write_valid('<html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Record</div>'+attack+'</body></html>')
                original = (self.stage_out/"email.html").read_bytes()
                with self.assertRaises(ValidationError):
                    self._validate()
                self.assertEqual((self.stage_out/"email.html").read_bytes(),original)

    def test_safe_inline_css_and_ordinary_links_are_preserved(self):
        html = '<html><head><style>@media screen and (max-width:600px){.card{color:#123}}</style></head><body data-local-date="2026-09-04"><div class="card" style="padding:10px" data-record-id="rec-1"><a href="https://example.test/report">Report</a></div></body></html>'
        comp,task = self._write_valid(html)
        self.assertEqual(self._validate(comp,task)[1],html)

    def test_duplicate_record_date_or_cid_markers_are_rejected(self):
        attacks = [
            '<div data-record-id="rec-1">Duplicate</div>',
            '<span data-local-date="2026-09-04">Duplicate date</span>',
            '<meta name="researchops-local-date" content="2026-09-04">',
            '<img src="cid:chart-1"><img src="cid:chart-1">',
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                comp,task = self._write_valid('<html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Record</div>'+attack+'</body></html>')
                if "cid:" in attack:
                    comp.inline_artifacts.append(self._inline())
                with self.assertRaises(ValidationError):
                    self._validate(comp,task)

    def test_inline_cid_is_unique_declared_and_a_valid_raster_image(self):
        comp,task = self._write_valid('<html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Record</div><img src="cid:chart-1"></body></html>')
        artifact = self._inline()
        comp.inline_artifacts.append(artifact)
        self._validate(comp,task)
        comp.inline_artifacts.append(dict(artifact))
        with self.assertRaises(ValidationError):
            self._validate(comp,task)
        comp.inline_artifacts.clear()
        with self.assertRaises(ValidationError):
            self._validate(comp,task)

    def test_fake_magic_headers_truncation_and_trailing_active_payload_are_rejected(self):
        good_png = self._png()
        good_gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
        validate_media(good_png,"image/png")
        validate_media(good_gif,"image/gif")
        for raw,mime in ((b"\x89PNG\r\n\x1a\n<script>x</script>","image/png"),
                         (good_png[:-5],"image/png"),(good_png+b"<script>x</script>","image/png"),
                         (b"GIF89a<script>x</script>","image/gif"),(good_gif[:-1],"image/gif"),
                         (good_gif+b"<html>payload</html>","image/gif"),
                         (b"\xff\xd8\xff<script>x</script>","image/jpeg"),(b"%PDF-1.7 incomplete","application/pdf")):
            with self.subTest(mime=mime,raw=raw[:16]):
                with self.assertRaises(ValueError):
                    validate_media(raw,mime)

    def test_mime_spoof_and_changed_artifact_hash_reject_the_composition(self):
        comp,task = self._write_valid('<html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Record</div><img src="cid:chart-1"></body></html>')
        comp.inline_artifacts.append(self._inline(b"\x89PNG\r\n\x1a\n<script>spoof</script>"))
        with self.assertRaises(ValidationError):
            self._validate(comp,task)
        comp.inline_artifacts[:] = [self._inline()]
        (self.stage_out/"chart.png").write_bytes(b"changed")
        with self.assertRaises(ValidationError):
            self._validate(comp,task)

    def test_document_structure_and_fixed_output_paths_are_enforced(self):
        for html in ('<html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Record</div></body><body></body></html>',
                     '<html><head><meta data-record-id="rec-1"></head><body data-local-date="2026-09-04"></body></html>',
                     '<html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Record</div></body></html>outside'):
            with self.subTest(html=html):
                self._write_valid(html)
                with self.assertRaises(ValidationError):
                    self._validate()
        comp,task = self._write_valid()
        task.output["html_path"] = "expected.html"
        with self.assertRaises(ValidationError):
            self._validate(comp,task)

    def test_optional_coverage_warnings_and_empty_records_are_not_content_rejected(self):
        comp,task = self._write_valid(record_ids=[])
        comp.result["status"] = "partial"
        comp.result["warnings"] = ["Optional issuer data unavailable"]
        comp.coverage["complete"] = False
        self._validate(comp,task)

    def test_ambiguous_duplicate_json_fields_are_rejected(self):
        self._write_valid()
        path = self.stage_out/"composition-result.json"
        raw = path.read_text()
        path.write_text('{"subject":"A different original title",'+raw[1:])
        with self.assertRaises(ValidationError):
            self._validate()

    def test_nonfinite_composition_values_fail_at_json_boundary(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            with self.subTest(token=token):
                self._write_valid()
                path = self.stage_out / "composition-result.json"
                path.write_text('{"untrusted_extra":' + token + ',' + path.read_text()[1:])
                with self.assertRaisesRegex(ValidationError, "Non-finite JSON"):
                    self._validate()

    def test_json_artifacts_use_the_same_unambiguous_utf8_contract(self):
        raw = '{"서울":-20,"optional":null}'.encode()
        validate_media(raw, "application/json")
        for payload in (b'{"x":1,"x":2}', b'{"x":1e999}', b'{"x":NaN}', b'{"x":"\\ud800"}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_media(payload, "application/json")

    def test_business_date_uses_scheduled_seoul_date_across_utc_midnight(self):
        comp,task = self._write_valid()
        comp.run["scheduled_for"] = "2026-09-03T15:01:00Z"
        self._validate(comp,task)
        comp.run["timezone"] = "UTC"
        with self.assertRaises(ValidationError):
            self._validate(comp,task)
        comp.run["timezone"] = "Asia/Seoul"
        comp.run["scheduled_for"] = "2026-09-04T15:01:00Z"
        with self.assertRaises(ValidationError):
            self._validate(comp,task)

    def test_html_security_parser_detects_violations(self):
        # 1. Active tags
        parser = HTMLSecurityParser()
        parser.feed("<p>Hello</p><script>alert(1)</script>")
        self.assertTrue(any("script" in err for err in parser.errors))

        # 2. Event handler
        parser = HTMLSecurityParser()
        parser.feed('<div onclick="bad()">Click</div>')
        self.assertTrue(any("onclick" in err for err in parser.errors))

        # 3. Dangerous URI
        parser = HTMLSecurityParser()
        parser.feed('<a href="javascript:doBad()">Link</a>')
        self.assertTrue(any("javascript:" in err for err in parser.errors))

        # 4. External remote resources
        parser = HTMLSecurityParser()
        parser.feed('<img src="https://remote.com/image.png">')
        self.assertIn("https://remote.com/image.png", parser.remote_resources_found)

    def test_valid_composition_passes(self):
        task_def = self._sample_task_def()
        comp_input = self._sample_comp_input(["rec-1"])

        comp_data = {
            "recipient_group_id": "team-product",
            "recipient_group_reason": "Regular product monitoring",
            "subject": "[Market Digest] 2026-09-04 New Cards",
            "html_path": "email.html",
            "text_path": "email.txt",
            "included_record_ids": ["rec-1"]
        }
        (self.stage_out / "composition-result.json").write_text(json.dumps(comp_data))
        (self.stage_out / "email.html").write_text('<!DOCTYPE html><html><body data-local-date="2026-09-04"><div data-record-id="rec-1">Content</div></body></html>')
        (self.stage_out / "email.txt").write_text("Plain text content")

        comp_res, html_body, text_body, hashes = self.validator.validate_composition(
            compose_output_dir=self.stage_out,
            comp_input=comp_input,
            task_def=task_def
        )
        self.assertEqual(comp_res.recipient_group_id, "team-product")
        self.assertIn("Content", html_body)

    def test_record_parity_violation_raises_error(self):
        task_def = self._sample_task_def()
        comp_input = self._sample_comp_input(["rec-1", "rec-2"])

        # Compose dropped rec-2
        comp_data = {
            "recipient_group_id": "team-product",
            "recipient_group_reason": "Product team",
            "subject": "Digest",
            "html_path": "email.html",
            "text_path": "email.txt",
            "included_record_ids": ["rec-1"]  # rec-2 missing!
        }
        (self.stage_out / "composition-result.json").write_text(json.dumps(comp_data))
        (self.stage_out / "email.html").write_text("<!DOCTYPE html><html><body>Content</body></html>")
        (self.stage_out / "email.txt").write_text("Content")

        with self.assertRaises(ValidationError) as ctx:
            self.validator.validate_composition(
                compose_output_dir=self.stage_out,
                comp_input=comp_input,
                task_def=task_def
            )
        self.assertTrue(any("silent drop forbidden" in err for err in ctx.exception.errors))

    def test_forbidden_group_rejection(self):
        task_def = self._sample_task_def()
        comp_input = self._sample_comp_input(["rec-1"])

        comp_data = {
            "recipient_group_id": "unknown-unapproved-group",
            "recipient_group_reason": "Unauthorized group",
            "subject": "Valid Subject",
            "html_path": "email.html",
            "text_path": "email.txt",
            "included_record_ids": ["rec-1"]
        }
        (self.stage_out / "composition-result.json").write_text(json.dumps(comp_data))
        (self.stage_out / "email.html").write_text("<!DOCTYPE html><html><body>Content</body></html>")
        (self.stage_out / "email.txt").write_text("Content")

        with self.assertRaises(ValidationError) as ctx:
            self.validator.validate_composition(
                compose_output_dir=self.stage_out,
                comp_input=comp_input,
                task_def=task_def
            )
        self.assertTrue(any("allowlist" in err for err in ctx.exception.errors))

    def test_subject_crlf_rejection(self):
        task_def = self._sample_task_def()
        comp_input = self._sample_comp_input(["rec-1"])

        comp_data = {
            "recipient_group_id": "team-product",
            "recipient_group_reason": "Product team",
            "subject": "Subject with\r\nCRLF injection",
            "html_path": "email.html",
            "text_path": "email.txt",
            "included_record_ids": ["rec-1"]
        }
        (self.stage_out / "composition-result.json").write_text(json.dumps(comp_data))
        (self.stage_out / "email.html").write_text("<!DOCTYPE html><html><body>Content</body></html>")
        (self.stage_out / "email.txt").write_text("Content")

        with self.assertRaises(ValidationError) as ctx:
            self.validator.validate_composition(
                compose_output_dir=self.stage_out,
                comp_input=comp_input,
                task_def=task_def
            )
        self.assertTrue(any("does not match" in err or "newline" in err for err in ctx.exception.errors))


if __name__ == "__main__":
    unittest.main()
