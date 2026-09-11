"""Append-only application evidence, published by atomic directory rename."""
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import tempfile
from jsonschema import Draft202012Validator, FormatChecker
from researchops.errors import ValidationError
from researchops.workspace.security import read_safe_bytes, open_safe_file, safe_file_info


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


class RunArchive:
    def __init__(self, settings, run):
        self.settings, self.run = settings, run
        self.parent = settings.paths.run_archive_dir / run.task_id
        self.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.final = self.parent / run.run_id
        if self.final.exists():
            raise ValidationError("Run already has an immutable archive; use a new run/revision")
        self.staging = Path(tempfile.mkdtemp(prefix=f".{run.run_id}.pending-", dir=self.parent))
        self.import_warnings = []

    def write(self, name, raw):
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValidationError("Unsafe archive path")
        path = self.staging / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def json(self, name, value):
        self.write(name, canonical_json(value))

    def capture_log(self, name, source, max_bytes):
        """Keep raw runner bytes, including an incomplete UTF-8 tail."""
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValidationError("Unsafe archive log path")
        target = self.staging / relative
        if source == target:
            safe_file_info(source, self.staging, max_bytes)
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open_safe_file(source, source.parent, max_bytes) as (stream, size):
            with target.open("xb") as out:
                os.chmod(target, 0o600)
                remaining = size
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise ValidationError("Runner log ended before its recorded size")
                    out.write(chunk)
                    remaining -= len(chunk)
                out.flush()
                os.fsync(out.fileno())

    def capture(self, source, prefix):
        """Read only safe regular files; retain refusal metadata, never follow links."""
        if not source.exists():
            return
        total, count = 0, 0
        for directory, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(dirs)
            for entry in list(dirs):
                path = Path(directory) / entry
                if path.is_symlink():
                    self.import_warnings.append(f"Rejected symlink directory: {prefix}/{path.relative_to(source)}")
                    dirs.remove(entry)
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(source).as_posix()
                try:
                    count += 1
                    if count > 1000:
                        raise ValidationError("Evidence file count limit exceeded")
                    raw = read_safe_bytes(path, source, 50_000_000)
                    total += len(raw)
                    if total > 100_000_000:
                        raise ValidationError("Evidence byte limit exceeded")
                    self.write(f"{prefix}/{relative}", raw)
                except Exception as exc:
                    self.import_warnings.append(f"Rejected {prefix}/{relative}: {exc}")
                    if count > 1000 or total > 100_000_000:
                        return

    def finish(self, manifest, *, before_commit=None):
        manifest["warnings"].extend(self.import_warnings)
        run_schema = json.loads((self.settings.paths.schemas_dir / "run-manifest.schema.json").read_text())
        Draft202012Validator(run_schema, format_checker=FormatChecker()).validate(manifest)
        raw = canonical_json(manifest)
        self.write("run-manifest.json", raw)
        roles = {"result.json": "research_result", "composition-input.json": "composition_input",
                 "composition-result.json": "composition_result", "email.html": "composed_html",
                 "email.txt": "composed_text", "delivery-request.json": "delivery_request",
                 "validation-report.json": "validation_report"}
        artifacts = []
        for path in sorted(self.staging.rglob("*")):
            if not path.is_file() or path == self.staging / "run-manifest.json":
                continue
            name = path.relative_to(self.staging).as_posix()
            limit = max(100_000_000, self.settings.runner.trace_max_bytes) if name.startswith("logs/") else 100_000_000
            digest, size = safe_file_info(path, self.staging, limit)
            artifacts.append({"relative_path": name, "role": roles.get(name, "evidence"),
                              "sha256": digest, "size_bytes": size,
                              "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
        index = {"task_id": self.run.task_id, "run_id": self.run.run_id, "artifacts": artifacts,
                 "run_manifest": {"relative_path": "run-manifest.json", "sha256": hashlib.sha256(raw).hexdigest()}}
        schema = json.loads((self.settings.paths.schemas_dir / "artifact-manifest.schema.json").read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(index)
        self.json("artifact-manifest.json", index)
        for directory, _, _ in os.walk(self.staging, topdown=False):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if before_commit is not None:
            before_commit()
        if self.final.exists():
            raise ValidationError("Archive collision; existing evidence is immutable")
        os.rename(self.staging, self.final)
        fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
