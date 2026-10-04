"""Hook entry point: `python3 -m coding_agent.hooks <hook-id>` reads the host's JSON payload on stdin.

Fail-open is the rule for every hook: an exception, a malformed payload, a missing manifest, or
an unwritable log exits 0 and records a telemetry line. Only a handler that returns exit 2 blocks,
and a handler only does that in `enforce` mode.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from coding_agent import events
from coding_agent.manifest import Manifest, ManifestError, load


@dataclass
class HookResult:
    code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Context:
    hook_id: str
    mode: str
    root: Path
    session: str
    manifest: Manifest | None
    payload: dict[str, Any] = field(default_factory=dict)
    # Why `integration.yaml` exists but cannot be used, so the hook reports it instead of going quiet.
    manifest_error: str | None = None

    def note(self, *, guard: str, kind: str, applied: bool, detail: dict[str, Any] | None = None) -> None:
        events.record(self.root, guard=guard, kind=kind, mode=self.mode, applied=applied, session=self.session, detail=detail)


def _root(payload: dict[str, Any]) -> Path:
    for candidate in (os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd"), os.getcwd()):
        if candidate:
            return Path(candidate)
    return Path.cwd()


def _manifest(root: Path) -> tuple[Manifest | None, str | None]:
    """The manifest and None, or None and the reason it cannot be used.

    A hook never breaks the session over configuration. It reports the reason as an event and a warning,
    and `coding_agent.gate` exits 2 on the same reason.
    """
    path = root / "integration.yaml"
    if not path.exists():
        return None, None
    try:
        return load(path.resolve()), None
    except ImportError as error:
        return None, f"PyYAML is not importable by this python3 ({error}); install pyyaml into the interpreter that runs the hooks"
    except (ManifestError, OSError, ValueError) as error:
        return None, str(error)


def context_for(hook_id: str, payload: dict[str, Any]) -> Context:
    """The hook's context. The mode comes from the generated command's env var; without one, the manifest's mode applies, and an unknown hook is `off`."""
    env_name = "CODING_AGENT_MODE_" + hook_id.upper().replace("-", "_")
    root = _root(payload)
    manifest, manifest_error = _manifest(root)
    declared = manifest.hook(hook_id) if manifest is not None else None
    return Context(
        hook_id=hook_id,
        mode=os.environ.get(env_name, declared.mode if declared is not None else "off"),
        root=root,
        session=str(payload.get("session_id") or "default"),
        manifest=manifest,
        payload=payload,
        manifest_error=manifest_error,
    )


def run(hook_id: str, payload: dict[str, Any], handler: Callable[[Context], HookResult]) -> HookResult:
    """Run one handler. A hook in mode `off` does nothing; any exception becomes exit 0 with a telemetry line."""
    ctx = context_for(hook_id, payload)
    if ctx.mode == "off":
        return HookResult()
    warning = ""
    if ctx.manifest_error is not None:
        warning = f"[{hook_id}] integration.yaml cannot be used, so manifest rules are not applied: {ctx.manifest_error}"
        ctx.note(guard=hook_id, kind="manifest-error", applied=False, detail={"error": ctx.manifest_error})
    try:
        result = handler(ctx)
        return replace(result, stderr="\n".join(part for part in (warning, result.stderr) if part)) if warning else result
    except Exception as error:  # fail-open: a hook never breaks the session
        try:
            ctx.note(guard=hook_id, kind="hook-error", applied=False, detail={"error": repr(error), "trace": traceback.format_exc()[-800:]})
        except OSError:
            pass
        return HookResult(code=0)


def _read_payload() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    from coding_agent.hooks import handlers

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 0
    hook_id = args[0]
    handler = handlers.REGISTRY.get(hook_id)
    if handler is None:
        return 0
    result = run(hook_id, _read_payload(), handler)
    if result.stdout:
        sys.stdout.write(result.stdout + "\n")
    if result.stderr:
        sys.stderr.write(result.stderr + "\n")
    return result.code
