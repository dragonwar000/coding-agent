"""The package gate (FR-003, SC-004): fails when a live host file differs from `integration.yaml`.

`python3 -m coding_agent.gate --manifest integration.yaml [--root DIR]` exits 0 when every live file
matches its rendering, 1 on drift or a missing file, and 2 when the manifest itself is invalid.
This package has no fdk-gate of its own, so run this command before a push or in CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coding_agent import gen
from coding_agent.manifest import ManifestError, load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.gate", description="Fail when live hook files drift from integration.yaml.")
    parser.add_argument("--manifest", type=Path, default=Path("integration.yaml"))
    args = parser.parse_args(argv)
    try:
        manifest = load(args.manifest.resolve())
    except (ManifestError, OSError) as error:
        print(f"gate: {error}", file=sys.stderr)
        return 2
    problems = gen.drift(manifest)
    for problem in problems:
        print(f"gate: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("gate: live hook files match integration.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
