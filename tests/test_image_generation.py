"""Native image-helper bounds and staging without a model or network call."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from researchops.runners.image_generation import generate_images
from researchops.runners.result_submission import stage_submission_helper


class NativeImageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.briefs = [{'filename': 'first.png', 'prompt': 'Synthetic first diagram'},
                       {'filename': 'second.png', 'prompt': 'Synthetic second diagram'}]

    def binary(self, mode='success'):
        path = self.root / ('stub-' + mode)
        path.write_text('''#!/usr/bin/python3
import json,os,pathlib,sys,time
request=json.loads(sys.stdin.read().split('Request:\\n',1)[1])
root=pathlib.Path(request['output_directory'])
mode=''' + repr(mode) + '''
if mode=='timeout':
    time.sleep(20)
for brief in request['images']:
    p=root/brief['filename']
    if mode=='symlink':p.symlink_to('/etc/hosts')
    else:p.write_bytes(b'\\x89PNG\\r\\n\\x1a\\nfixture')
(root/'invocation.json').write_text(json.dumps({'argv':sys.argv,'has_key':'OPENAI_API_KEY' in os.environ}))
sys.exit(1 if mode=='failed' else 0)
''')
        path.chmod(0o700)
        return str(path)

    def test_two_images_and_manifest_without_task_model_override(self):
        result = generate_images(self.briefs, artifact_root=self.root, codex_binary=self.binary())
        self.assertEqual(result['status'], 'available')
        self.assertEqual(len(result['images']), 2)
        for image in result['images']:
            self.assertEqual(image['mime_type'], 'image/png')
            self.assertTrue(Path(image['path']).is_relative_to(self.root))
            self.assertEqual(len(image['sha256']), 64)
        invocation = json.loads((Path(result['images'][0]['path']).parent/'invocation.json').read_text())
        self.assertNotIn('--model', invocation['argv'])
        self.assertFalse(invocation['has_key'])

    def test_nonzero_exit_cannot_be_success_even_with_images(self):
        result = generate_images(self.briefs, artifact_root=self.root, codex_binary=self.binary('failed'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['images'], [])

    def test_linked_images_are_rejected(self):
        result = generate_images(self.briefs, artifact_root=self.root, codex_binary=self.binary('symlink'))
        self.assertEqual(result['status'], 'failed')

    def test_timeout_returns_failure_and_reaps_process(self):
        result = generate_images(self.briefs, artifact_root=self.root, codex_binary=self.binary('timeout'), timeout_seconds=1)
        self.assertEqual(result['reason_code'], 'image_generation_timeout')

    def test_paths_and_duplicate_names_are_rejected_before_spawn(self):
        for bad in ('../escape.png', '/escape.png', 'a/b.png'):
            with self.assertRaises(ValueError):
                generate_images([{'filename': bad, 'prompt': 'Synthetic'}], artifact_root=self.root)
        with self.assertRaises(ValueError):
            generate_images(self.briefs[:1] * 2, artifact_root=self.root)
        self.assertFalse(list(self.root.glob('imagegen-*')))

    def test_helper_is_not_staged_for_offline_or_compose_by_default(self):
        offline = self.root / 'offline'
        offline.mkdir()
        stage_submission_helper(offline)
        self.assertFalse((offline / '.researchops-submit/researchops/runners/image_generation.py').exists())
        research = self.root / 'research'
        research.mkdir()
        stage_submission_helper(research, include_imagegen=True)
        self.assertTrue((research / '.researchops-submit/researchops/runners/image_generation.py').is_file())


if __name__ == '__main__':
    unittest.main()
