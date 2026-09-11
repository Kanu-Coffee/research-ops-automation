#!/usr/bin/env python3
"""Check release inventory, local Markdown links, versions and source hashes.

Run from a source checkout or extracted sdist. --write refreshes only the generated
FILE_TREE.txt and CHECKSUMS.sha256 after the remaining validation passes.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
TREES = {"researchops", "schemas", "examples", "tasks", "tests", "deploy",
         "templates", "bin", "docs", "scripts", ".github"}
TOP_FILES = {"README.md", "START_HERE.md", "AGENTS.md", "CHANGELOG.md", "SECURITY.md",
             "CONTRIBUTING.md", "PACK_MANIFEST.md", "FILE_TREE.txt", "CHECKSUMS.sha256",
             "pyproject.toml", "uv.lock", "VERSION", "MANIFEST.in", "setup.cfg", ".gitignore", ".gitattributes"}
GENERATED = {"FILE_TREE.txt", "CHECKSUMS.sha256"}
LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")


def inventory() -> list[str]:
    paths = set(TOP_FILES - GENERATED)
    for tree in TREES:
        base = ROOT / tree
        if base.is_symlink() or not base.is_dir():
            raise ValueError(f"Required source directory missing: {tree}")
        for path in base.rglob("*"):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink():
                raise ValueError(f"Source symlink is forbidden: {path.relative_to(ROOT)}")
            if path.is_file() and path.suffix not in {".pyc", ".pyo"}:
                paths.add(path.relative_to(ROOT).as_posix())
    if (ROOT / ".git").exists():
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                                 capture_output=True).stdout.decode().split("\0")
        unexpected = set(filter(None, tracked)) - paths - GENERATED
        if unexpected:
            raise ValueError("Tracked files outside release inventory: " + ", ".join(sorted(unexpected)))
    for name in paths | GENERATED:
        path = ROOT / name
        if name in GENERATED and not path.exists() and not path.is_symlink():
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or unsafe source: {name}")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"Source must be a regular non-hardlinked file: {name}")
        basename = path.name.lower()
        example = basename.endswith(".example.yaml") or basename == ".env.example"
        private_name = (basename.startswith((".env", "credentials", "delivery_config"))
                        or basename in {"settings.yaml", "settings.yml", "auth.json", "hosts.yml"})
        if (private_name and not example or
                path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".pem", ".key", ".p12", ".pfx"}
                or re.search(r"\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$", basename)):
            raise ValueError(f"Private/runtime file in release: {name}")
        if any(char in name for char in "\r\n\\"):
            raise ValueError("Nonportable source filename")
        if any(part in {"exports", "run-archive", "task-workspaces", "evidence", "backups", "secrets"}
               for part in Path(name).parts[:-1]):
            raise ValueError(f"Private/runtime directory in release: {name}")
    return sorted(paths | GENERATED)


def validate_versions() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = [p["version"] for p in lock["package"] if p["name"] == "researchops"]
    module = ast.parse((ROOT / "researchops/__init__.py").read_text())
    imported = [ast.literal_eval(node.value) for node in module.body
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__"
                                                       for t in node.targets)]
    if locked != [project] or imported != [project] or (ROOT / "VERSION").read_text().strip() != project:
        raise ValueError("Version mismatch between pyproject, uv.lock, VERSION and package")
    return project


def validate_links(paths: list[str]) -> int:
    count = 0
    for name in paths:
        if not name.endswith(".md"):
            continue
        path = ROOT / name
        # Code examples may contain Markdown link syntax as a demonstration.
        content = re.sub(r"```.*?```", "", path.read_text(), flags=re.DOTALL)
        for match in LINK.finditer(content):
            target = match.group(1).strip().split(' "', 1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                raise ValueError(f"Broken or nonportable local link in {name}: {target}")
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        names = inventory()
        version = validate_versions()
        links = validate_links(names)
        tree = "\n".join(names) + "\n"
        entries = []
        for name in names:
            if name == "CHECKSUMS.sha256":
                continue
            data = tree.encode() if name == "FILE_TREE.txt" else (ROOT / name).read_bytes()
            entries.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        checksums = "".join(entries)
        expected = {"FILE_TREE.txt": tree, "CHECKSUMS.sha256": checksums}
        for name, value in expected.items():
            path = ROOT / name
            if args.write:
                with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(value)
                temporary.replace(path)
            elif not path.exists() or path.read_text() != value:
                raise ValueError(f"{name} is stale; review changes and run scripts/release_check.py --write")
        print(f"ResearchOps {version}: {len(names)} source files, {links} local links, versions and hashes OK")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Release check failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
