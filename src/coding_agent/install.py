"""Safe install and removal (Task 6, FR-004, SC-003).

The harness owns a directory only when it carries a marker file whose digest still matches the
directory's contents. A directory without the marker, or one edited after install, is never
replaced or removed. Replacement always keeps a backup first.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

MARKER = ".coding-agent-owned"


class NotOwned(RuntimeError):
    """The directory is not a harness-owned install, or it was edited after install."""


def _tracked(path: Path) -> bool:
    """Source files count toward the digest; bytecode caches do not, since running the package writes them."""
    return path.is_file() and path.name != MARKER and "__pycache__" not in path.parts and path.suffix != ".pyc"


def tree_digest(root: Path) -> str:
    """sha256 over every source file's relative path and bytes, marker and bytecode caches excluded."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if _tracked(p)):
        digest.update(str(path.relative_to(root)).encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def mark_owned(root: Path) -> None:
    (root / MARKER).write_text(tree_digest(root) + "\n", encoding="utf-8")


def is_owned_intact(root: Path) -> bool:
    marker = root / MARKER
    if not marker.is_file():
        return False
    return marker.read_text(encoding="utf-8").strip() == tree_digest(root)


def _require_owned(target: Path) -> None:
    if not target.exists():
        return
    if not target.is_dir():
        raise NotOwned(f"{target} exists and is not a directory")
    if not is_owned_intact(target):
        raise NotOwned(f"{target} has no intact harness marker; it is left in place")


def backup(target: Path, backup_root: Path) -> Path:
    """Copy `target` aside before it is changed. The name carries a timestamp and, when needed, a counter, so repeated installs never collide."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = backup_root / f"{target.name}-{stamp}"
    counter = 1
    while destination.exists():
        counter += 1
        destination = backup_root / f"{target.name}-{stamp}-{counter}"
    shutil.copytree(target, destination)
    return destination


def safe_replace(target: Path, source: Path, *, backup_root: Path) -> Path | None:
    """Install `source` at `target`. An existing owned install is backed up first; anything else is refused."""
    _require_owned(target)
    kept: Path | None = None
    if target.exists():
        kept = backup(target, backup_root)
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    mark_owned(target)
    return kept


def safe_remove(target: Path, *, backup_root: Path) -> Path | None:
    """Remove an owned install, keeping a backup. Refuses anything not owned."""
    if not target.exists():
        return None
    _require_owned(target)
    kept = backup(target, backup_root)
    shutil.rmtree(target)
    return kept
