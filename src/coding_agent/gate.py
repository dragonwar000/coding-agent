"""The package gate (FR-003, SC-004): fails when a live host file differs from `integration.yaml`.

`python3 -m coding_agent.gate --manifest integration.yaml [--root DIR]` exits 0 when every live file
matches its rendering, 1 on drift or a missing file, and 2 when the manifest itself is invalid.
This package has no fdk-gate of its own, so run this command before a push or in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from coding_agent import gen
from coding_agent.manifest import ManifestError, load

INSTALLED = Path(".coding-agent") / "installed.json"


def installed_hosts(root: Path) -> list[str] | None:
    """The hosts the installer wrote for this project, or None when no install record exists (then every host is checked)."""
    path = root / INSTALLED
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    hosts = record.get("hosts") if isinstance(record, dict) else None
    return [host for host in hosts if host in gen.HOSTS] if isinstance(hosts, list) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.gate", description="Fail when live hook files drift from integration.yaml.")
    parser.add_argument("--manifest", type=Path, default=Path("integration.yaml"))
    args = parser.parse_args(argv)
    try:
        manifest = load(args.manifest.resolve())
    except (ManifestError, OSError) as error:
        print(f"gate: {error}", file=sys.stderr)
        return 2
    except ImportError as error:
        print(f"gate: PyYAML is not importable ({error}); install pyyaml into this python3", file=sys.stderr)
        return 2
    problems = gen.drift(manifest, installed_hosts(manifest.root))
    for problem in problems:
        print(f"gate: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("gate: live hook files match integration.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
