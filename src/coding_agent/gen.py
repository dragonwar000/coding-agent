"""Render the host hook files from `integration.yaml` (FR-001 to FR-003).

Commands use a path relative to the repository root, never an absolute path of one machine:
Claude Code exports `$CLAUDE_PROJECT_DIR`, and Codex is resolved through `git rev-parse`.

By default the generator writes `*.proposed.json` next to the live files, so a manifest
change reaches a repository only when a person applies it (`--apply`). `--check` compares the
live files with what the manifest renders and exits 1 on drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from coding_agent.manifest import Hook, Manifest, load

CLAUDE_ROOT = '"$CLAUDE_PROJECT_DIR"'
CODEX_ROOT = '"$(git rev-parse --show-toplevel)"'

CLAUDE_FILE = Path(".claude/settings.json")
CODEX_FILE = Path(".codex/hooks.json")


def command_for(manifest: Manifest, hook: Hook, root_expr: str) -> str:
    """The shell command a host runs for one hook."""
    src = f"{root_expr}/{manifest.python_src}"
    return f"{hook.env_name}={hook.mode} PYTHONPATH={src} python3 -m coding_agent.hooks {hook.id}"


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


def render(manifest: Manifest) -> dict[str, dict[str, Any]]:
    """Both host documents, keyed by host name."""
    return {
        "claude_code": _render_host(manifest, "claude_code", CLAUDE_ROOT),
        "codex": _render_host(manifest, "codex", CODEX_ROOT),
    }


def _target(root: Path, host: str) -> Path:
    return root / (CLAUDE_FILE if host == "claude_code" else CODEX_FILE)


def write(manifest: Manifest, *, apply: bool) -> list[Path]:
    """Write the rendered documents. Without `apply`, the file gets a `.proposed.json` suffix."""
    written: list[Path] = []
    for host, document in render(manifest).items():
        target = _target(manifest.root, host)
        if not apply:
            target = target.with_name(target.stem + ".proposed" + target.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    return written


def drift(manifest: Manifest) -> list[str]:
    """Hosts whose live file is missing or differs from the manifest. Empty when in sync."""
    problems: list[str] = []
    for host, document in render(manifest).items():
        target = _target(manifest.root, host)
        if not target.exists():
            problems.append(f"{host}: {target} is missing")
            continue
        try:
            live = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{host}: {target} is unreadable ({error})")
            continue
        if live != document:
            problems.append(f"{host}: {target} differs from integration.yaml; run gen --apply")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.gen", description="Render hook files from integration.yaml.")
    parser.add_argument("--manifest", type=Path, default=Path("integration.yaml"))
    parser.add_argument("--check", action="store_true", help="exit 1 when a live file drifts from the manifest")
    parser.add_argument("--apply", action="store_true", help="write the live files instead of *.proposed.json")
    args = parser.parse_args(argv)

    try:
        manifest = load(args.manifest.resolve())
    except Exception as error:  # ManifestError and YAML errors both fail the same way
        print(f"gen: {error}", file=sys.stderr)
        return 2

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
