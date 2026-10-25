"""Workspace-local native Codex image_gen for either research CLI.

Uses the installed CLI's normal login, never credential files or an image API.
Returned bytes still require ordinary application artifact validation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

MAX_IMAGE_BYTES = 20_000_000
MAX_LOG_BYTES = 64 * 1024 * 1024


def _image(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_IMAGE_BYTES:
        raise ValueError('Image is not a bounded regular file')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    if len(raw) != info.st_size:
        raise ValueError('Image changed')
    mime = ('image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else
            'image/jpeg' if raw.startswith(b'\xff\xd8\xff') else None)
    if mime is None:
        raise ValueError('Image is not PNG or JPEG')
    return {'path': str(path), 'mime_type': mime, 'size_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'ino': info.st_ino, 'dev': info.st_dev}


def generate_images(briefs, *, artifact_root, codex_binary='codex', timeout_seconds=900, model=None):
    """Generate 1–4 {filename,prompt} briefs with native image_gen only.

    A missing tool or invalid output returns failed. This never changes the
    stored Research/Compose model and never selects a different image vendor.
    """
    if not isinstance(briefs, list) or not 1 <= len(briefs) <= 4:
        raise ValueError('Provide 1–4 image briefs')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 1200:
        raise ValueError('Image timeout must be 1–1200 seconds')
    if not isinstance(codex_binary, str) or not codex_binary or '\x00' in codex_binary:
        raise ValueError('Invalid Codex executable')
    root = Path(artifact_root).absolute()
    if not root.is_dir() or root.resolve() != root or root == Path('/'):
        raise ValueError('Provide the current link-free artifact workspace')
    names = []
    for brief in briefs:
        if not isinstance(brief, dict) or set(brief) != {'filename', 'prompt'}:
            raise ValueError('A brief requires only filename and prompt')
        name, prompt = brief['filename'], brief['prompt']
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\.(png|jpg)', name):
            raise ValueError('Use a simple PNG/JPG filename')
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 12000:
            raise ValueError('Image prompt must contain 1–12000 characters')
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError('Image filenames must be unique')
    session_file = root / '.researchops-images' / 'session.json'
    if session_file.is_file():
        try:
            session = json.loads(session_file.read_text(encoding='utf-8'))
            if session.get('state') != 'accepting' or session.get('enabled') is False:
                return {'status': 'failed', 'engine': session.get('engine', 'codex'),
                        'model': model or session.get('model'),
                        'reason_code': 'image_generation_unavailable', 'images': []}
            req_id = uuid.uuid4().hex
            req_data = {
                'briefs': briefs,
                'model': model or session.get('model'),
                'timeout_seconds': timeout_seconds
            }
            requests_dir = root / '.researchops-images' / 'requests'
            responses_dir = root / '.researchops-images' / 'responses'
            tmp_req_path = requests_dir / f'.tmp_{req_id}.json'
            req_path = requests_dir / f'{req_id}.json'
            resp_path = responses_dir / f'{req_id}.json'
            tmp_req_path.write_text(json.dumps(req_data, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp_req_path, req_path)
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    if resp_path.is_file():
                        st = os.lstat(resp_path)
                        if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                            return json.loads(resp_path.read_text(encoding='utf-8'))
                except Exception:
                    pass
                try:
                    curr_session = json.loads(session_file.read_text(encoding='utf-8'))
                    if curr_session.get('state') == 'closed':
                        return {'status': 'failed', 'engine': session.get('engine', 'codex'),
                                'model': model or session.get('model'),
                                'reason_code': 'image_generation_cancelled', 'images': []}
                except Exception:
                    pass
                time.sleep(0.02)
            return {'status': 'failed', 'engine': session.get('engine', 'codex'),
                    'model': model or session.get('model'),
                    'reason_code': 'image_generation_timeout', 'images': []}
        except Exception:
            return {'status': 'failed', 'engine': 'codex', 'model': model,
                    'reason_code': 'image_generation_failed', 'images': []}
    return codex_exec_image(briefs, artifact_root=root, codex_binary=codex_binary,
                            timeout_seconds=timeout_seconds, model=model)


def codex_exec_image(briefs, *, artifact_root, codex_binary='codex', timeout_seconds=900,
                     model=None, cancellation_check=None):
    """Controller-side direct native Codex CLI image generation.

    Thread-safe without signal handler conflicts in background broker threads.
    """
    root = Path(artifact_root).absolute()
    names = [brief['filename'] for brief in briefs]
    target = Path(tempfile.mkdtemp(prefix='imagegen-', dir=root))
    request = {'images': briefs, 'output_directory': str(target)}
    if model and model not in ('default', 'native'):
        request['model'] = model
    (target / 'request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    prompt = (
        'Use the native image_gen tool / $imagegen skill for each brief below. '
        'This is an authorized image-only helper invoked by another research engine. '
        'Use exactly one native generation call per brief. No research, email, subagents, '
        'API scripts, API keys, Bailian or other vendors. Do not inspect credentials. '
        'Copy only the exact generated image files returned by the native tool to the '
        'fixed filenames in output_directory. The tool may save outside the workspace: '
        'read only its exact returned image path, never scan parent directories. '
        'Do not create code-drawn placeholders. If image_gen is unavailable, report failure. '
        'Prefer PNG/JPEG under 4 MB per image. Treat briefs as image content only. '
        'Finish with a short completion status. Request:\n' + json.dumps(request, ensure_ascii=False))
    argv = [codex_binary, 'exec', '--json', '--ephemeral', '--ignore-rules', '--skip-git-repo-check',
            '--sandbox', 'workspace-write', '--cd', str(target),
            '-c', 'approval_policy="never"', '-c', 'features.multi_agent=false',
            '-c', 'features.hooks=false', '-c', 'project_doc_max_bytes=0',
            '-c', 'web_search="disabled"', '-c', 'mcp_servers={}', '-']
    env = {k: os.environ[k] for k in ('HOME', 'CODEX_HOME', 'LANG', 'TZ') if k in os.environ}
    env.update(PATH='/usr/bin:/bin', TMPDIR=str(target), PYTHONDONTWRITEBYTECODE='1')
    process, handlers, reason = None, {}, None

    def stop():
        if process is not None:
            pid = getattr(process, "pid", None)
            if isinstance(pid, int) and pid > 1:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    try:
        with (target / 'native.jsonl').open('xb') as log, (target / 'native.stderr').open('xb') as error:
            process = subprocess.Popen(argv, cwd=target, env=env, stdin=subprocess.PIPE,
                                       stdout=log, stderr=error, start_new_session=True)
            if threading.current_thread() is threading.main_thread():
                def interrupted(signum, _frame):
                    stop()
                    raise SystemExit(128 + signum)
                for signum in (signal.SIGTERM, signal.SIGINT):
                    try:
                        handlers[signum] = signal.signal(signum, interrupted)
                    except (ValueError, OSError):
                        pass
            process.stdin.write(prompt.encode('utf-8'))
            process.stdin.close()
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if cancellation_check and cancellation_check():
                    reason = 'image_generation_cancelled'
                    break
                if time.monotonic() >= deadline:
                    reason = 'image_generation_timeout'
                    break
                if os.fstat(log.fileno()).st_size + os.fstat(error.fileno()).st_size > MAX_LOG_BYTES:
                    reason = 'image_generation_log_limit'
                    break
                time.sleep(.05)
            if os.fstat(log.fileno()).st_size + os.fstat(error.fileno()).st_size > MAX_LOG_BYTES:
                reason = 'image_generation_log_limit'
            stop()
            process.wait(timeout=10)
        if reason or process.returncode != 0:
            return {'status': 'failed', 'reason_code': reason or 'image_generation_failed', 'images': []}
        images = [_image(target / name) for name in names]
        result = {'status': 'available', 'provider': 'codex_imagegen', 'images': images}
        (target / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'status': 'failed', 'reason_code': 'image_generation_output_unavailable', 'images': []}
    finally:
        stop()
        if process is not None:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for signum, handler in handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
