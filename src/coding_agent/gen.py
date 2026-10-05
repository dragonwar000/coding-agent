"""Render the host hook files from `integration.yaml` and merge them into the project's own files (FR-001 to FR-003).

Commands use a path relative to the repository root, never an absolute path of one machine:
Claude Code exports `$CLAUDE_PROJECT_DIR`, and Codex is resolved through `git rev-parse`.

A harness entry is a hook whose command runs `coding_agent.hooks`. `write` replaces only those entries and
keeps every other hook and setting in the file, as the setup harness does. The previous file is kept as
`.bak` when it is applied. Without `apply`, the merged result goes to `*.proposed.json` and nothing else changes.
`drift` compares only the harness entries, so a user's own hooks never count as drift.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

from coding_agent.manifest import Hook, Manifest, load

CLAUDE_ROOT = '"$CLAUDE_PROJECT_DIR"'
CODEX_ROOT = '"$(git rev-parse --show-toplevel)"'
HARNESS_MARK = "coding_agent.hooks"
HOSTS = ("claude_code", "codex")

CLAUDE_FILE = Path(".claude/settings.json")
CODEX_FILE = Path(".codex/hooks.json")


class GenError(RuntimeError):
    """A host file exists but cannot be merged safely; the message names the file."""


def command_for(manifest: Manifest, hook: Hook, root_expr: str) -> str:
    """The shell command a host runs for one hook."""
    src = f"{root_expr}/{manifest.python_src}"
    return f"{hook.env_name}={hook.mode} PYTHONPATH={src} {manifest.python} -m {HARNESS_MARK} {hook.id}"


def _render_host(manifest: Manifest, host: str, root_expr: str) -> dict[str, Any]:
    events: dict[str, list[dict[str, Any]]] = {}
    for hook in manifest.hooks:
        if host not in hook.host:
            continue
        entry: dict[str, Any] = {"hooks": [{"type": "command", "command": command_for(manifest, hook, root_expr)}]}
        if hook.matcher is not None:
            entry = {"matcher": hook.matcher, **entry}
        events.setdefault(hook.event, []).append(entry)
    return {"hooks": events}


def render(manifest: Manifest, hosts: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """The harness hook document for each selected host (all hosts when `hosts` is None)."""
    chosen = HOSTS if hosts is None else tuple(hosts)
    roots = {"claude_code": CLAUDE_ROOT, "codex": CODEX_ROOT}
    return {host: _render_host(manifest, host, roots[host]) for host in chosen}


def _target(root: Path, host: str) -> Path:
    return root / (CLAUDE_FILE if host == "claude_code" else CODEX_FILE)


def _is_harness(entry: Any) -> bool:
    return isinstance(entry, dict) and any(HARNESS_MARK in str((h or {}).get("command") or "") for h in entry.get("hooks") or [])


def harness_only(document: dict[str, Any] | None) -> dict[str, list[Any]]:
    """The harness entries of a host document, by event. Entries with no harness command are not included."""
    hooks = (document or {}).get("hooks")
    if not isinstance(hooks, dict):
        return {}
    out: dict[str, list[Any]] = {}
    for event, entries in hooks.items():
        if isinstance(entries, list):
            kept = [entry for entry in entries if _is_harness(entry)]
            if kept:
                out[event] = kept
    return out


def merged(live: dict[str, Any] | None, rendered: dict[str, Any]) -> dict[str, Any]:
    """The live document with its old harness entries removed and the rendered ones added. Other keys stay as they are."""
    base = dict(live or {})
    live_hooks = base.get("hooks") if isinstance(base.get("hooks"), dict) else {}
    hooks: dict[str, list[Any]] = {}
    for event, entries in live_hooks.items():
        if isinstance(entries, list):
            kept = [entry for entry in entries if not _is_harness(entry)]
            if kept:
                hooks[event] = kept
    for event, entries in rendered["hooks"].items():
        hooks.setdefault(event, []).extend(entries)
    base["hooks"] = hooks
    return base


def _read(target: Path) -> dict[str, Any] | None:
    """The parsed host file, None when absent. A file that is not a JSON object is refused, never overwritten."""
    if not target.exists():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GenError(f"{target} is not valid JSON ({error}); fix or move it, then run again") from error
    if not isinstance(document, dict):
        raise GenError(f"{target} is not a JSON object; fix or move it, then run again")
    return document


def write(manifest: Manifest, *, apply: bool, hosts: Iterable[str] | None = None) -> list[Path]:
    """Merge the harness entries into each selected host file. Without `apply`, write `*.proposed.json` instead."""
    written: list[Path] = []
    for host, rendered in render(manifest, hosts).items():
        target = _target(manifest.root, host)
        document = merged(_read(target), rendered)
        destination = target
        if apply:
            if target.exists():
                shutil.copy2(target, target.with_name(target.name + ".bak"))
        else:
            destination = target.with_name(target.stem + ".proposed" + target.suffix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(destination)
    return written


def drift(manifest: Manifest, hosts: Iterable[str] | None = None) -> list[str]:
    """Hosts whose harness entries are missing or differ from the manifest. Empty when in sync."""
    problems: list[str] = []
    for host, rendered in render(manifest, hosts).items():
        target = _target(manifest.root, host)
        if not target.exists():
            problems.append(f"{host}: {target} is missing")
            continue
        try:
            live = _read(target)
        except GenError as error:
            problems.append(f"{host}: {error}")
            continue
        if harness_only(live) != harness_only(rendered):
            problems.append(f"{host}: harness hooks in {target} differ from integration.yaml; run gen --apply")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.gen", description="Merge harness hooks from integration.yaml into the host files.")
    parser.add_argument("--manifest", type=Path, default=Path("integration.yaml"))
    parser.add_argument("--check", action="store_true", help="exit 1 when a harness entry drifts from the manifest")
    parser.add_argument("--apply", action="store_true", help="write the host files (keeping a .bak) instead of *.proposed.json")
    args = parser.parse_args(argv)

    try:
        manifest = load(args.manifest.resolve())
    except Exception as error:  # ManifestError, YAML errors, and a missing PyYAML all fail the same way
        print(f"gen: {error}", file=sys.stderr)
        return 2

    try:
        if args.check:
            problems = drift(manifest)
            for problem in problems:
                print(f"gen: {problem}", file=sys.stderr)
            if problems:
                return 1
            print("gen: live hook files match integration.yaml")
            return 0
        for path in write(manifest, apply=args.apply):
            print(f"gen: wrote {path}")
    except GenError as error:
        print(f"gen: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
