#!/usr/bin/env bash
# bootstrap.sh — cài coding-agent vào dự án hiện tại bằng một dòng, ghim vào một commit.
#
#   curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/<commit này>/bootstrap.sh | bash
#
# Kèm tham số cài (sau `bash -s --`), đúng như install.sh:
#   ... | bash -s -- --vendor claude --no-verify
#   ... | bash -s -- --uninstall
#
# Biến môi trường:
#   CODING_AGENT_REF     commit cần cài (mặc định: commit ghim bên dưới)
#   CODING_AGENT_OWNER   chủ repo (mặc định: dragonwar000)
#   CODING_AGENT_REPO    tên repo (mặc định: coding-agent)
#   GH_TOKEN             token để tải repo private; repo public không cần
#
# Kiểm nguồn trước khi cài (không tải gì): ... | bash -s -- --print-source
set -euo pipefail

# Commit chứa code đã kiểm (installer, gate, hooks). Tách khỏi commit của chính file này.
PINNED_REF="a0076fa2b4178486eedd27534858c78cdd685fb4"
OWNER="${CODING_AGENT_OWNER:-dragonwar000}"
REPO="${CODING_AGENT_REPO:-coding-agent}"
REF="${CODING_AGENT_REF:-$PINNED_REF}"

say(){ printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

ARGS=()
for a in "$@"; do
  case "$a" in
    --print-source)
      printf 'owner=%s repo=%s ref=%s\narchive=https://codeload.github.com/%s/%s/tar.gz/%s\n' "$OWNER" "$REPO" "$REF" "$OWNER" "$REPO" "$REF"
      exit 0 ;;
    *) ARGS+=("$a") ;;
  esac
done

command -v curl >/dev/null || die "cần curl"
command -v tar >/dev/null || die "cần tar"
TARGET="$PWD"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

AUTH=()
[ -n "${GH_TOKEN:-}" ] && AUTH=(-H "Authorization: token $GH_TOKEN")
say "tải $OWNER/$REPO@$REF"
curl -fsSL ${AUTH[@]+"${AUTH[@]}"} "https://codeload.github.com/$OWNER/$REPO/tar.gz/$REF" | tar -xz -C "$TMP" \
  || die "không tải được $OWNER/$REPO@$REF (repo private cần GH_TOKEN)"

SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -n "$SRC" ] && [ -f "$SRC/install.sh" ] || die "archive không có install.sh"

say "cài vào $TARGET"
bash "$SRC/install.sh" "$TARGET" ${ARGS[@]+"${ARGS[@]}"}
