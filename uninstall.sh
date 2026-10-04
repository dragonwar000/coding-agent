#!/usr/bin/env bash
# uninstall.sh — gỡ coding-agent khỏi một dự án. Chỉ gỡ đúng phần coding-agent đã thêm; hook và cài đặt của bạn giữ nguyên.
#
#   bash uninstall.sh <project_root> [--keep-core] [--purge] [--remove-manifest]
#     --keep-core        giữ bản copy package, chỉ gỡ hook và CI
#     --purge            xoá thêm các file .bak và thư mục .coding-agent/ (event, state, bản sao lưu)
#     --remove-manifest  xoá thêm integration.yaml
#
# Bộ nhớ Zero-Mem nằm ngoài dự án (~/.coding-agent/zeromem) không bị đụng tới.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${1:-.}"
[ $# -gt 0 ] && shift
exec bash "$HERE/install.sh" "$PROJECT" --uninstall "$@"
