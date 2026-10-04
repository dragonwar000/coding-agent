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
PY="${PYTHON:-python3}"
PROJECT="${1:-.}"
[ $# -gt 0 ] && shift

if ! "$PY" -c 'import yaml' 2>/dev/null; then
  echo "[install] thiếu pyyaml → pip install pyyaml"
  "$PY" -m pip install --quiet pyyaml || { echo "[install] không cài được pyyaml; cài tay: $PY -m pip install pyyaml" >&2; exit 1; }
fi

PYTHONPATH="$HERE/src" exec "$PY" -m coding_agent.project_install --project "$PROJECT" "$@"
