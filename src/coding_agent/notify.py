"""Tell a person when the worker watcher reports something, through a command the user chose. Off by default.

`notify` in `integration.yaml`: `mode` (`off` or `on`), `command` (an argv list) and `timeout_s`. The watcher calls `fire`
when it reports a death, a stuck worker or its own failure. The command runs with no shell, and the event reaches it
only through environment variables (`ENV_*`), so a task title holding `$(...)` or quotes stays text.

Each event runs the command once, whether the command or the escalation succeeded: a retried escalation does not
notify twice, and a broken command is not run every scan. The run is recorded in the resumes ledger with `kind: notify`.
`fire` never raises. A command that fails, cannot start or outlives `timeout_s` (its whole process group is killed) is
recorded as a `notify-failed` event, and the watcher goes on.

Measured on macOS 26 with curl 8.7.1 (2026-11-23), for the README's examples: AppleScript's `system attribute` garbles
UTF-8 (`Sửa` reads as `S·ª≠a`), and `osascript -l JavaScript` with `$.getenv` keeps it; `curl --variable %NAME
--expand-data '{{NAME:json}}'` sends quotes, newlines and Vietnamese as valid JSON, and accepts an empty variable.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from coding_agent import events, manifest, orca_cli, resume

LEDGER_KIND = "notify"
ENV_KIND = "CODING_AGENT_NOTIFY_KIND"
ENV_TASK = "CODING_AGENT_NOTIFY_TASK_ID"
ENV_DISPATCH = "CODING_AGENT_NOTIFY_DISPATCH"
ENV_TERMINAL = "CODING_AGENT_NOTIFY_TERMINAL"
ENV_TITLE = "CODING_AGENT_NOTIFY_TITLE"
ENV_REASON = "CODING_AGENT_NOTIFY_REASON"
VALUE_CHARS = 1000       # longest value passed in one variable
KILL_WAIT_S = 2.0        # how long a killed command gets to disappear


def _settings(root: Path) -> manifest.Notify:
    """The `notify` block of `root`'s manifest; no manifest means off. A malformed manifest raises."""
    path = root / "integration.yaml"
    if not path.exists():
        return manifest.Notify()
    # Absolute, not resolved: a worker worktree's `integration.yaml` can link to the coordinator's (`worktree_links`).
    return manifest.load(Path(os.path.abspath(path))).notify


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Kill the command and what it started: it runs in its own process group on POSIX."""
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        pass


def _run(command: tuple[str, ...], env: dict[str, str], cwd: Path, timeout_s: int) -> str | None:
    """Run `command` without a shell. None when it exits 0, else why it did not."""
    group: dict[str, Any] = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt"
                             else {"start_new_session": True})
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(list(command), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=errors, **group)
        try:
            code = process.wait(timeout_s)
        except subprocess.TimeoutExpired:
            _kill(process)
            return f"timed out after {timeout_s}s"
        if code == 0:
            return None
        errors.seek(0)
        return f"exit {code}: {errors.read()[-200:].decode('utf-8', errors='replace').strip()}"


def _already(root: Path, mark: str) -> bool:
    return any(record.get("kind") == LEDGER_KIND and record.get("notify_key") == mark for record in resume.history(root))


def _note(root: Path, kind: str, applied: bool, session: str, detail: dict[str, Any]) -> None:
    try:
        events.record(root, guard="worker-watch", kind=kind, mode="enforce", applied=applied, session=session, detail=detail)
    except Exception:  # noqa: BLE001 - an event log that cannot be written must not stop the watcher
        pass


def fire(root: Path, kind: str, key: str, *, task_id: str = "", dispatch: str = "", terminal: str = "", title: str = "",
         reason: str = "", session: str = "worker-watch", seen: set[str] | None = None) -> str:
    """Run the notify command once for event `key` of `kind` (`death`, `stuck`, `watch-failing`). Never raises.

    `seen` is the loop's memory, which holds when the ledger cannot be written. Returns `off`, `already-sent`, `sent` or
    `failed`.
    """
    mark = f"{kind}:{key}"
    memo = f"{LEDGER_KIND}:{mark}"  # `seen` is the watcher's set of delivered reports too, whose keys look like `mark`
    detail: dict[str, Any] = {"notify_kind": kind, "task_id": task_id, "dispatch": dispatch}
    try:
        settings = _settings(root)
        if settings.mode != "on":
            return "off"
        if (seen is not None and memo in seen) or _already(root, mark):
            return "already-sent"
        if seen is not None:
            seen.add(memo)
        values = {ENV_KIND: kind, ENV_TASK: task_id, ENV_DISPATCH: dispatch, ENV_TERMINAL: terminal, ENV_TITLE: title, ENV_REASON: reason}
        env = {**os.environ, **{name: value.replace("\0", "")[:VALUE_CHARS] for name, value in values.items()}}
        error = _run(settings.command, env, root, settings.timeout_s)
    except Exception as failure:  # noqa: BLE001 - a command that cannot start, or a manifest that cannot be read
        error = f"{type(failure).__name__}: {failure}"
        if seen is not None:
            seen.add(memo)
    try:
        orca_cli._append(root / resume.LEDGER, {"ts": int(time.time()), "kind": LEDGER_KIND, "notify_key": mark, "task_id": task_id,
                                                "dispatch": dispatch, "sent": error is None})
    except Exception as failure:  # noqa: BLE001 - `seen` still holds it for this loop
        detail["ledger_error"] = f"{type(failure).__name__}: {failure}"[:200]
    _note(root, "notified" if error is None else "notify-failed", error is None, session, {**detail, "error": error and error[:300]})
    return "sent" if error is None else "failed"
