#!/usr/bin/env bash
# Verified operations; restore is offline-only and retention defaults to dry-run.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TASK_PYTHON="${RESEARCHOPS_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$TASK_PYTHON" ]]; then
    TASK_PYTHON="$(command -v python3)"
fi
export TZ=Asia/Seoul
export RESEARCHOPS_CONFIG="${RESEARCHOPS_CONFIG:-/etc/researchops/settings.yaml}"
cd "$REPO_ROOT"
exec env -u PYTHONPATH -u PYTHONSTARTUP "$TASK_PYTHON" -m researchops.operations cleanup "$@"
