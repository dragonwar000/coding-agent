"""Give a worker worktree the coordinator's coding-agent install, so the worker's session runs the same hooks.

A project installs coding-agent without committing it: `harness/`, `integration.yaml`, `.claude/settings.json` and
`.codex/hooks.json` stay untracked (or gitignored) in the checkout the installer ran in. Orca creates a worker's
worktree from a commit, so those files are not in it, and a worker session runs no hooks at all: no stop-gate, no
verify, no ratchet, no evaluator.

`link_install` runs right after the worktree exists and before the worker's agent starts. For each path in `LINKED`
that the source checkout has, the worktree does not have, and git does not track in the worktree, it puts a link in
the worktree that points at the source: a symlink on macOS and Linux; on Windows a symlink when the account may make
one, else a directory junction (`harness/`) or a copy (a file). An existing file, a tracked path, or a path the source
lacks is never touched. One copy of the install serves every worker, so an upgrade of the coordinator's install
reaches them too. What was made is recorded in the worktree (`RECORD`), which is what `unlink_install` removes before
the worktree is deleted; it removes only links it made (and copies it made), never anything they point to.

`exclude_install` adds the same paths, the installer's `.claude/settings.json.bak` and the record to
`info/exclude` in the worktree's git common directory, which every worktree of the repository reads (the
coordinator's checkout too, when it is one of them). So `git status` shows none of them and `git add -A` in a worker
never commits them. A path git tracks is left out: ignore rules do not apply to tracked files.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Paths, relative to the checkout root, that make up an untracked install. `harness` is the default `python_src`
# parent (`harness/coding-agent/src`); `.coding-agent/installed.json` tells the gate which hosts the installer wrote.
LINKED = ("harness", "integration.yaml", ".claude/settings.json", ".codex/hooks.json", ".coding-agent/installed.json")
RECORD = Path(".coding-agent") / "worktree-links.json"
EXCLUDED = (*LINKED, ".claude/settings.json.bak", RECORD.as_posix())
EXCLUDE_MARK = "# coding-agent: install linked into worker worktrees (worktree_links.py)"
MANIFEST = "integration.yaml"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def tracked(checkout: Path, rel: str) -> bool:
    """True when git tracks `rel` (or a file under it) in `checkout`, or when git cannot say (then nothing is touched)."""
    run = _git(checkout, "ls-files", "--", rel)
    return run is None or run.returncode != 0 or bool(run.stdout.strip())


def source_for(*candidates: Path | None) -> Path | None:
    """The first checkout among `candidates` that holds an install (`integration.yaml`), or None."""
    for candidate in candidates:
        if candidate is not None and (candidate / MANIFEST).is_file():
            return candidate
    return None


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_junction(path: Path) -> bool:
    check = getattr(os.path, "isjunction", None)
    return bool(check and check(path))


def is_link(path: Path) -> bool:
    """True when `path` is a symlink or a Windows junction, dangling or not."""
    return path.is_symlink() or _is_junction(path)


def remove_link(path: Path) -> None:
    """Remove the symlink or junction at `path` itself; what it points to is never entered."""
    if _is_junction(path):
        os.rmdir(path)  # removes the junction itself, not the directory it points to
    elif path.is_symlink():
        (os.rmdir if sys.platform == "win32" and path.is_dir() else os.unlink)(path)
    else:
        raise OSError(f"{path} is not a link")


def _make_link(source: Path, target: Path) -> str:
    """Link `target` to `source`; returns the kind made: `symlink`, `junction` or `copy`."""
    try:
        os.symlink(source, target, target_is_directory=source.is_dir())
        return "symlink"
    except OSError:
        if sys.platform != "win32":
            raise
    # Windows without the symlink privilege (no Developer Mode): a junction needs none, a file is copied.
    if source.is_dir():
        try:
            import _winapi  # type: ignore[import-not-found]

            _winapi.CreateJunction(str(source), str(target))
            return "junction"
        except (ImportError, AttributeError, OSError):
            shutil.copytree(source, target, symlinks=True)
            return "copy"
    shutil.copy2(source, target)
    return "copy"


def link_install(source: Path, worktree: Path) -> list[dict[str, str]]:
    """Link the install of checkout `source` into `worktree`. Returns one `{path, action}` per path in `LINKED`.

    `action` is `symlink`, `junction` or `copy` for a link made, else why the path was left alone: `missing` (the
    source has no such path), `exists` (the worktree already has a file, directory or link there), `tracked` (git
    tracks it in the worktree) or `failed: <error>`. Nothing is done when `source` has no `integration.yaml` or is the
    worktree itself.
    """
    source, worktree = Path(os.path.abspath(source)), Path(os.path.abspath(worktree))
    if source_for(source) is None or source.resolve() == worktree.resolve():
        return []
    results: list[dict[str, str]] = []
    made: list[dict[str, str]] = []
    created_dirs: list[str] = []
    for rel in LINKED:
        origin, target = source / rel, worktree / rel
        if not origin.exists():
            results.append({"path": rel, "action": "missing"})
            continue
        if _lexists(target):
            results.append({"path": rel, "action": "exists"})
            continue
        if tracked(worktree, rel):
            results.append({"path": rel, "action": "tracked"})
            continue
        try:
            for parent in reversed(target.relative_to(worktree).parents[:-1]):
                directory = worktree / parent
                if not directory.exists():
                    directory.mkdir()
                    created_dirs.append(parent.as_posix())
            kind = _make_link(origin.resolve(), target)
        except OSError as error:
            results.append({"path": rel, "action": f"failed: {str(error)[:200]}"})
            continue
        made.append({"path": rel, "kind": kind, "source": str(origin.resolve())})
        results.append({"path": rel, "action": kind})
    if made:
        try:
            _save_record(worktree, source, made, created_dirs)
        except OSError:
            pass  # without the record, unlink_install finds nothing to remove; git removes the links themselves
    return results


def _save_record(worktree: Path, source: Path, made: list[dict[str, str]], created_dirs: list[str]) -> None:
    path = worktree / RECORD
    previous = _load_record(worktree)
    links = [*previous.get("links", []), *made]
    dirs = [*previous.get("created_dirs", []), *created_dirs]
    if not (worktree / RECORD.parent).exists():
        (worktree / RECORD.parent).mkdir(parents=True)
    path.write_text(json.dumps({"source": str(source), "links": links, "created_dirs": dirs}, indent=2) + "\n", encoding="utf-8")


def _load_record(worktree: Path) -> dict[str, Any]:
    try:
        record = json.loads((worktree / RECORD).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def linked_paths(worktree: Path) -> list[str]:
    """The paths `link_install` made in `worktree`, from its record."""
    return [str(item["path"]) for item in _load_record(worktree).get("links", []) if isinstance(item, dict) and item.get("path")]


def unlink_install(worktree: Path) -> list[str]:
    """Remove the links `link_install` made in `worktree`, and the record. Returns the paths removed.

    A symlink or junction is removed as a link: what it points to is never entered. A copy is deleted only when it is
    still a plain file or directory inside the worktree. A path that is no longer what was made is left alone.
    """
    worktree = Path(os.path.abspath(worktree))
    record = _load_record(worktree)
    removed: list[str] = []
    for item in record.get("links", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        target = worktree / item["path"]
        kind = item.get("kind")
        try:
            if (kind == "symlink" and target.is_symlink()) or (kind == "junction" and _is_junction(target)):
                remove_link(target)
            elif kind == "copy" and _lexists(target) and not target.is_symlink() and not _is_junction(target):
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            else:
                continue
        except OSError:
            continue
        removed.append(item["path"])
    for rel in sorted(record.get("created_dirs", []), key=lambda d: -d.count("/")):
        try:
            os.rmdir(worktree / rel)  # only when empty
        except OSError:
            pass
    try:
        (worktree / RECORD).unlink()
    except OSError:
        pass
    return removed


def exclude_install(checkout: Path) -> list[str]:
    """Add the install paths to `info/exclude` in the git common directory of `checkout`. Returns the lines added.

    Every worktree of the repository reads that file. Patterns are anchored at the worktree root and carry no
    trailing `/`, so they match a symlink or junction too. Paths git tracks in `checkout` are skipped. Lines already
    there are not added again.
    """
    source = checkout
    run = _git(source, "rev-parse", "--git-common-dir")
    if run is None or run.returncode != 0 or not run.stdout.strip():
        return []
    common = Path(run.stdout.strip())
    common = common if common.is_absolute() else (source / common)
    exclude = common / "info" / "exclude"
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    except OSError:
        return []
    present = {line.strip() for line in text.splitlines()}
    wanted = [f"/{rel}" for rel in EXCLUDED if not tracked(source, rel)]
    missing = [line for line in wanted if line not in present]
    if not missing:
        return []
    block = ([] if EXCLUDE_MARK in present else [EXCLUDE_MARK]) + missing
    prefix = "" if not text or text.endswith("\n") else "\n"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(block) + "\n")
    return missing


def prepare_worker(worktree: Path, *candidates: Path | None) -> dict[str, Any]:
    """Link the install of the first candidate checkout that has one into `worktree`, and exclude it from git.

    Returns `{source, links, excluded}`; `source` is "" when no candidate has an install, and then nothing is done.
    """
    source = source_for(*candidates)
    if source is None:
        return {"source": "", "links": [], "excluded": []}
    try:
        excluded = exclude_install(worktree)
    except OSError:
        excluded = []  # the links still work; cleanup does not count them as work either way
    links = link_install(source, worktree)
    return {"source": str(source), "links": links, "excluded": excluded}


def made_links(result: dict[str, Any]) -> bool:
    """True when `prepare_worker` made at least one link."""
    return any(item.get("action") in ("symlink", "junction", "copy") for item in result.get("links", []))
