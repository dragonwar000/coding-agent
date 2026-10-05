#!/usr/bin/env bash
# install.sh — cài coding-agent vào một dự án bằng một lệnh, như install.sh của harness/setup.
#
#   bash install.sh <project_root> [--vendor claude,codex] [--python-src harness/coding-agent/src]
#                   [--verify "python3 -m pytest -q"] [--no-verify] [--no-ci] [--clean]
#   bash install.sh <project_root> --uninstall [--keep-core]
#
# Idempotent. Mọi việc thực hiện bởi `python3 -m coding_agent.project_install` (xem docstring của module).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# python3 where it exists; Git Bash on Windows usually has only `python`.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else echo "[install] cần Python 3.11 trở lên (python3 hoặc python) trong PATH" >&2; exit 1
fi
PROJECT="${1:-.}"
[ $# -gt 0 ] && shift

if ! "$PY" -c 'import yaml' 2>/dev/null; then
  echo "[install] thiếu pyyaml → pip install pyyaml"
  "$PY" -m pip install --quiet pyyaml || { echo "[install] không cài được pyyaml; cài tay: $PY -m pip install pyyaml" >&2; exit 1; }
fi

PYTHONPATH="$HERE/src" exec "$PY" -m coding_agent.project_install --project "$PROJECT" "$@"
