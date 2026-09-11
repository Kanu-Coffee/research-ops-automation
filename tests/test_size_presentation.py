"""Human-readable sizes preserve raw evidence and agree across SSR and JS."""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from researchops.web.formatting import SIZE_SCRIPT, format_size, render_size


class TestSizePresentation(unittest.TestCase):
    def test_units_rounding_invalid_and_exact_accessible_values(self):
        cases = [(0,'0 B'), (999,'999 B'), (1000,'1 KB'), (1050,'1.1 KB'),
                 (1536,'1.5 KB'), (65536,'65.5 KB'), (999949,'999.9 KB'),
                 (999950,'1 MB'), (10**6,'1 MB'), (10**9,'1 GB'), (10**12,'1 TB'),
                 (None,'—'), (-1,'—'), (True,'—'), ('1024','—')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(format_size(raw), expected)
        self.assertIn('title="65,536 B"', render_size(65536))
        self.assertIn('sr-only">65.5 KB (정확히 65,536 B)', render_size(65536))
        self.assertEqual(render_size(None), '—')

    def test_exact_size_is_a_non_submit_disclosure_with_distinct_targets(self):
        class Elements(HTMLParser):
            def __init__(self, text):
                super().__init__(); self.buttons=[]; self.spans=[]; self.feed(text)
            def handle_starttag(self, tag, attrs):
                if tag == 'button':self.buttons.append(dict(attrs))
                if tag == 'span':self.spans.append(dict(attrs))
        controls = Elements(render_size(65536) + render_size(65536))
        targets = [button['aria-controls'] for button in controls.buttons]
        self.assertEqual(len(set(targets)),2)
        for button in controls.buttons:
            self.assertEqual(button['type'],'button')
            self.assertEqual(button['aria-expanded'],'false')
            target = next(span for span in controls.spans if span.get('id') == button['aria-controls'])
            self.assertIn('hidden',target)
        self.assertNotIn('<button',render_size(999))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is not installed')
    def test_browser_formatter_matches_server_at_unit_boundaries(self):
        values = [0,999,1000,1050,1499,1500,65536,999949,999950,10**6,10**9,10**12,1500*10**12]
        source = 'const window={};\n' + SIZE_SCRIPT + '\nprocess.stdout.write(JSON.stringify(' + json.dumps(values) + '.map(window.researchopsFormatSize)));'
        result = subprocess.run(['node','-e',source],capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(json.loads(result.stdout), [format_size(value) for value in values])

    def test_timeline_sizes_are_humanized_without_changing_counters(self):
        from researchops.web.timeline_views import _summary_items
        values = _summary_items({'stdout_bytes':65536,'mime_bytes':2000000,'record_count':1050,'exit_code':-9})
        self.assertTrue(any('65.5 KB' in value and '65,536 B' in value for value in values))
        self.assertTrue(any('메일 MIME 용량:' in value and '2 MB' in value for value in values))
        self.assertIn('조사 record: 1,050',values)
        self.assertIn('종료 코드: -9',values)
