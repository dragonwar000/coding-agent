"""Resume workers that stopped on a transient API error (lost network, overloaded API).

What happened (2026-10-12): five Claude Code workers stopped for three hours when the network dropped. Each screen
ended with `API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)`, sometimes
`API Error: Connection lost mid-response`, then a summary such as `Worked for 3h 5m · done 9:24 PM` and an empty
prompt. Orca kept the dispatch `ready` and live, the board kept saying `đang chạy`, and nothing woke the workers when
the network came back.

`stalled_workers` reads the screen of every unsettled dispatch and `classify` sorts it: `working`, `idle`, `stalled`
(empty prompt under a transient API error), `blocked` (empty prompt under an API error that a retry does not fix), or
`unreadable`. `resume_stalled` sends a stalled worker one line through `orca terminal send`, but only when the API host
answers on TCP 443, never twice for the same screen, at most `MAX_RESUMES` times per dispatch and `RESUME_GAP_S`
apart. `auto` is the step the coordinator's board runs each turn; `watch` is the loop behind `worker-resume --watch`.

The stalled screen itself was not measured on a live terminal: `classify` lists what was measured and what is assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from coding_agent import events, orca_cli

AUTO_ENV = "CODING_AGENT_AUTO_RESUME"
HOST_ENV = "CODING_AGENT_API_HOST"
API_HOST = "api.anthropic.com"
API_PORT = 443
CONNECT_TIMEOUT_S = 3.0
WINDOW_LINES = 15        # non-empty lines above the prompt that are searched for the error
MAX_RESUMES = 5          # resumes sent to one dispatch at most
RESUME_GAP_S = 60        # shortest time between two resumes of one dispatch
SCAN_GAP_S = 60          # the board's automatic scan is skipped when the last one is more recent
AUTO_BUDGET_S = 20.0     # the board's scan stops sending after this long: it runs inside a hook, and a send can wait 20s
WATCH_INTERVAL_S = 60.0
RESUME_MARK = "Mang da co lai"
LEDGER = Path(".coding-agent") / "resumes.jsonl"
SCAN_FILE = Path(".coding-agent") / "state" / "resume-scan.json"
DONE_STATUSES = frozenset({"completed", "failed"})

# An agent's own error line: `API Error: ...`, bare or behind the turn bullet. Text a tool printed is indented deeper
# or quoted, so it does not match.
ERROR_LINE = re.compile(r"^\s*(?:[⏺●⎿]\s*)?API Error\b", re.IGNORECASE)
# A new assistant turn: a bullet line. One after the last error line means the worker moved on by itself.
TURN_LINE = re.compile(r"^\s*[⏺●]\s*\S")
# The spinner of a turn in progress, such as `✽ Crystallizing…`: one glyph, one capitalised word, an ellipsis. The summary
# of a finished turn (`✻ Worked for 3h 5m · done 9:24 PM`) has no ellipsis.
SPINNER_LINE = re.compile(r"^\s*[^\s\w⏺●⎿❯]\s+[A-Z][a-z]+…")
RULE_LINE = re.compile(r"^[\s─━═╭╮╰╯│-]+$")
# What makes an `API Error` line transient: waiting and trying again can fix it. Extend here.
TRANSIENT_MARKS = (
    r"ENOTFOUND", r"ECONNRESET", r"ECONNREFUSED", r"ETIMEDOUT", r"EAI_AGAIN",
    r"Connection lost", r"Can(?:.?t| ?not) reach the API", r"fetch failed",
    r"overloaded", r"rate.?limit", r"\b(?:429|500|502|503|504|529)\b",
)
# What makes it permanent: a person has to act. Checked first; an `API Error` line that matches neither is blocked too.
PERMANENT_MARKS = (
    r"\b(?:401|403)\b", r"invalid.api.key", r"authentication", r"credit", r"context.length", r"prompt is too long", r"/login",
)
# Permanent errors Claude Code prints without the `API Error` prefix.
PERMANENT_LINE = re.compile(r"^\s*(?:[⏺●⎿]\s*)?(?:Invalid API key|Credit balance is too low|Prompt is too long)", re.IGNORECASE)
TRANSIENT = re.compile("|".join(TRANSIENT_MARKS), re.IGNORECASE)
PERMANENT = re.compile("|".join(PERMANENT_MARKS), re.IGNORECASE)


@dataclass(frozen=True)
class Screen:
    """One screen sorted by `classify`. `error` is the error line; `fingerprint` identifies the lines above the prompt."""

    state: str
    error: str = ""
    fingerprint: str = ""


@dataclass(frozen=True)
class Finding:
    """One unsettled worker: its screen's `state`, and `action`, what `resume_stalled` did or decided about it.

    `action` is empty for a worker that needs nothing. `attempt` counts the resumes sent to the dispatch so far.
    """

    task_id: str
    dispatch: str
    terminal: str
    state: str
    error: str = ""
    fingerprint: str = ""
    action: str = ""
    attempt: int = 0

    @property
    def attention(self) -> bool:
        return self.state in ("stalled", "blocked")


def auto_enabled() -> bool:
    """False when `CODING_AGENT_AUTO_RESUME` is `off` (or `0`, `false`, `no`); on by default."""
    return os.environ.get(AUTO_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def api_endpoint() -> tuple[str, int]:
    """The host and port probed before a resume: `CODING_AGENT_API_HOST` as `host` or `host:port`, else api.anthropic.com:443."""
    value = os.environ.get(HOST_ENV, "").strip()
    host, _, port = value.partition(":")
    return host or API_HOST, int(port) if port.isdigit() else API_PORT


def network_up(timeout_s: float = CONNECT_TIMEOUT_S) -> bool:
    """True when a TCP connection to the API host opens within `timeout_s`."""
    try:
        with socket.create_connection(api_endpoint(), timeout=timeout_s):
            return True
    except OSError:
        return False


def _error_text(lines: list[str], index: int) -> str:
    """The error line at `index` with its wrapped continuation: up to two indented lines that start no new block."""
    parts = [lines[index].strip()]
    for line in lines[index + 1:index + 3]:
        if not line.startswith("  ") or not re.match(r"[\w(]", line.strip()) or ERROR_LINE.match(line):
            break
        parts.append(line.strip())
    return " ".join(parts)


def classify(screen: str) -> Screen:
    """Sort one worker screen into `working`, `idle`, `stalled`, `blocked` or `unreadable`.

    A worker is `stalled` when its prompt is empty and idle (`orca_cli._composer_idle`, read without the earlier
    `❯ text` lines that echo the user's messages) and the last `API Error` line among the `WINDOW_LINES` non-empty
    lines above the prompt is transient (`TRANSIENT_MARKS`), with no new turn (a `⏺` line that is not an error) after
    it. The same with a permanent or unknown error is `blocked`. Anything else with an empty prompt is `idle`.

    Measured on a working Claude Code terminal under Orca on macOS (2026-10-12): a turn's first line starts with `⏺`
    in column 0, the prompt is a bare `❯` between two rule lines with the footer below, and the spinner line is
    `✽ Crystallizing… (6m 41s · ↓ 35.1k tokens)`. A screen with a spinner (`SPINNER_LINE`) is `working` whatever else
    it shows. Assumptions, from the incident's description and not measured on a stalled terminal: the error line
    starts with `API Error`, bare or after the bullet (`⏺`, or `●` on Windows); the user's earlier messages show as
    `❯ text`; a long error line wraps into lines indented by two spaces or more.
    """
    lines = [line.replace("\xa0", " ").rstrip() for line in screen.splitlines()]
    if not any(lines):
        return Screen("unreadable")
    prompts = [index for index, line in enumerate(lines) if line.strip().startswith("❯")]
    composer = prompts[-1] if prompts else len(lines)
    # Earlier `❯ text` lines are the user's messages, and a resume leaves one; only the last is the prompt itself.
    current = "\n".join(line for index, line in enumerate(lines) if index not in prompts[:-1])
    if not orca_cli._composer_idle(current) or any(SPINNER_LINE.match(line) for line in lines[:composer]):
        return Screen("working")
    window = [line for line in lines[:composer] if line.strip() and not RULE_LINE.match(line)][-WINDOW_LINES:]
    fingerprint = hashlib.sha256("\n".join(line.strip() for line in window).encode("utf-8")).hexdigest()[:16]
    found = [index for index, line in enumerate(window) if ERROR_LINE.match(line) or PERMANENT_LINE.match(line)]
    if not found:
        return Screen("idle", fingerprint=fingerprint)
    last = found[-1]
    if any(TURN_LINE.match(line) and not ERROR_LINE.match(line) and not PERMANENT_LINE.match(line) for line in window[last + 1:]):
        return Screen("idle", fingerprint=fingerprint)
    text = _error_text(window, last)
    transient = ERROR_LINE.match(window[last]) and not PERMANENT.search(text) and TRANSIENT.search(text)
    return Screen("stalled" if transient else "blocked", error=text.lstrip("⏺●⎿ ")[:200], fingerprint=fingerprint)


def _known_tasks(root: Path) -> set[str]:
    """The Orca tasks this coordinator created or started a worker for, over every repository it delegated into."""
    known: set[str] = set()
    for place in orca_cli.all_roots(root):
        known.update(orca_cli.links(place))
        known.update(record["task_id"] for record in orca_cli.worker_records(place))
    return known


def stalled_workers(root: Path, run: str | None = None, *, task_ids: list[str] | None = None,
                    tasks: list[dict[str, Any]] | None = None) -> list[Finding]:
    """One Finding per unsettled dispatch of the Run, with its screen sorted by `classify`. Nothing is sent.

    Without `task_ids`, only this coordinator's tasks are read (`_known_tasks`): a Run can hold another repository's
    workers. A task that is completed, failed or still pending, has no dispatch, a settled dispatch or no terminal
    is left out. `tasks` reuses a task list the caller already read.
    """
    tasks = orca_cli.list_tasks(run) if tasks is None else tasks
    wanted = set(task_ids) if task_ids else _known_tasks(root)
    found: list[Finding] = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        if task_id not in wanted or task.get("status") in DONE_STATUSES or (task.get("status") == "pending" and not task_ids):
            continue
        try:
            dispatch = orca_cli.dispatch_of(task_id)
        except orca_cli.OrcaError:
            continue
        if not dispatch["assignee_handle"] or dispatch["status"] in orca_cli.SETTLED_WORKER_STATES:
            continue
        seen = classify(orca_cli.screen_of(dispatch["assignee_handle"]))
        found.append(Finding(task_id=task_id, dispatch=dispatch["id"], terminal=dispatch["assignee_handle"], state=seen.state,
                             error=seen.error, fingerprint=seen.fingerprint))
    return found


def resume_text(task_id: str) -> str:
    """The one line sent to a stalled worker. Like the kick-off: no accents, no newline, nothing a shell expands."""
    return (f"{RESUME_MARK}. Ban bi dung vi loi API tam thoi. Lam tiep task {task_id} tu cho dang do: "
            "kiem lai lenh va shell nen dang chay do truoc khi chay lai, xong thi commit va gui worker_done.")


def history(root: Path) -> list[dict[str, Any]]:
    """Every resume recorded in the ledger, oldest first."""
    try:
        lines = (root / LEDGER).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _held(sent: list[dict[str, Any]], fingerprint: str, now: float) -> str | None:
    """Why a dispatch with these `sent` resumes gets no other one now, or None when it may be resumed."""
    if any(record.get("fingerprint") == fingerprint for record in sent):
        return "already-resumed"
    if len(sent) >= MAX_RESUMES:
        return "limit"
    if sent and now - float(sent[-1].get("ts") or 0) < RESUME_GAP_S:
        return "cooldown"
    return None


def resume_stalled(root: Path, findings: list[Finding], *, dry_run: bool = False, session: str = "cli",
                   budget_s: float | None = None) -> list[Finding]:
    """Decide about each stalled or blocked worker and resume the ones that may be. Returns the findings with `action` set.

    Actions: `resumed`, `would-resume` (dry run), `waiting-network` (the API host does not answer; nothing is sent),
    `already-resumed` (this screen was resumed before and has not changed), `cooldown`, `limit`, `send-failed`,
    `blocked`, `deferred` (`budget_s` seconds were spent; the next scan resumes it). The network is probed once, and
    only when a worker is due a resume. Each send is appended to `.coding-agent/resumes.jsonl` and recorded as an event.
    """
    deadline = None if budget_s is None else time.monotonic() + budget_s
    online: bool | None = None
    out: list[Finding] = []
    for item in findings:
        if item.state == "blocked":
            out.append(replace(item, action="blocked"))
            continue
        if item.state != "stalled":
            out.append(item)
            continue
        key = item.dispatch or item.task_id
        sent = [record for record in history(root) if record.get("dispatch") == key and record.get("sent") is True]
        held = _held(sent, item.fingerprint, time.time())
        if held is not None:
            out.append(replace(item, action=held, attempt=len(sent)))
            continue
        online = network_up() if online is None else online
        if not online:
            out.append(replace(item, action="waiting-network", attempt=len(sent)))
            continue
        if dry_run:
            out.append(replace(item, action="would-resume", attempt=len(sent)))
            continue
        if deadline is not None and time.monotonic() >= deadline:
            out.append(replace(item, action="deferred", attempt=len(sent)))
            continue
        error: str | None = None
        try:
            orca_cli.send_line(item.terminal, resume_text(item.task_id))
        except orca_cli.OrcaError as failure:
            error = str(failure)[:300]
        attempt = len(sent) + (error is None)
        orca_cli._append(root / LEDGER, {"ts": int(time.time()), "task_id": item.task_id, "dispatch": key, "terminal": item.terminal,
                                         "fingerprint": item.fingerprint, "sent": error is None})
        events.record(root, guard="worker-resume", kind="resumed" if error is None else "resume-failed", mode="enforce",
                      applied=error is None, session=session,
                      detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal, "attempt": attempt,
                              "screen_error": item.error, "fingerprint": item.fingerprint, "error": error})
        out.append(replace(item, action="resumed" if error is None else "send-failed", attempt=attempt))
    return out


def scan(root: Path, run: str | None = None, *, task_ids: list[str] | None = None, dry_run: bool = False, session: str = "cli",
         tasks: list[dict[str, Any]] | None = None, budget_s: float | None = None) -> list[Finding]:
    """Read every unsettled worker and resume the stalled ones. Raises `orca_cli.OrcaError` when the tasks cannot be listed."""
    return resume_stalled(root, stalled_workers(root, run, task_ids=task_ids, tasks=tasks), dry_run=dry_run, session=session,
                          budget_s=budget_s)


def _load_scan(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / SCAN_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def auto(root: Path, tasks: list[dict[str, Any]], run: str | None = None, session: str = "board") -> list[Finding]:
    """The board's step: scan and resume, and return the workers that are stalled or blocked. Never raises.

    A scan less than `SCAN_GAP_S` after the last one of the same Run is skipped and that scan's findings are returned,
    so the board keeps showing them. A worker's waiting, blocked or unchanged state is recorded as an event when it
    changes, not on every scan. Sending stops after `AUTO_BUDGET_S` seconds, so a hook is not held up by many stalled
    workers; the rest are resumed by the next scan. Any failure is recorded (`kind: error`) and answers an empty list.
    `CODING_AGENT_AUTO_RESUME=off` turns the step off.
    """
    if not auto_enabled():
        return []
    try:
        current = orca_cli.run_id(run) or ""
        last = _load_scan(root)
        if last.get("run") == current and time.time() - float(last.get("ts") or 0) < SCAN_GAP_S:
            return [Finding(**item) for item in last.get("findings") or []]
        found = [item for item in scan(root, run, session=session, tasks=tasks, budget_s=AUTO_BUDGET_S) if item.attention]
        noted = last.get("noted") if isinstance(last.get("noted"), dict) else {}
        marks = {item.task_id: f"{item.action}|{item.fingerprint}" for item in found}
        for item in found:
            if item.action in ("waiting-network", "blocked", "limit", "already-resumed") and noted.get(item.task_id) != marks[item.task_id]:
                events.record(root, guard="worker-resume", kind=item.action, mode="enforce", applied=False, session=session,
                              detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal,
                                      "screen_error": item.error, "fingerprint": item.fingerprint, "attempt": item.attempt})
        path = root / SCAN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": int(time.time()), "run": current, "findings": [asdict(item) for item in found], "noted": marks},
                                   ensure_ascii=False), encoding="utf-8")
        return found
    except Exception as error:  # noqa: BLE001 - this runs inside the coordinator's hooks, which it must never break
        try:
            events.record(root, guard="worker-resume", kind="error", mode="enforce", applied=False, session=session,
                          detail={"error": f"{type(error).__name__}: {error}"[:300]})
        except OSError:
            pass
        return []


def board_notes(findings: list[Finding]) -> dict[str, str]:
    """Task id to what the board says about a stalled or blocked worker."""
    notes: dict[str, str] = {}
    for item in findings:
        if not item.attention:
            continue
        said = {
            "resumed": f"đã tự resume (lần {item.attempt}/{MAX_RESUMES})",
            "waiting-network": "đang chờ mạng, chưa gửi gì; tự resume khi mạng có lại",
            "cooldown": f"đã resume {item.attempt} lần, lại dừng; tự resume lại sau {RESUME_GAP_S} giây",
            "deferred": "chưa gửi resume vì hết thời gian của lượt quét; lượt quét sau sẽ gửi (hoặc chạy `worker-resume`)",
            "already-resumed": "bị chặn, cần người xử lý: đã gửi resume mà màn hình không đổi",
            "limit": f"bị chặn, cần người xử lý: đã resume {MAX_RESUMES} lần mà vẫn dừng",
            "send-failed": "bị chặn, cần người xử lý: không gửi được resume vào terminal",
            "blocked": "bị chặn, cần người xử lý: lỗi không tạm thời, không tự resume",
        }.get(item.action, "bị dừng")
        notes[item.task_id] = f"{said} — {item.error}" if item.error else said
    return notes


def describe(item: Finding) -> str:
    """One line about a worker for `worker-resume`."""
    host, port = api_endpoint()
    said = {
        "resumed": f"stalled, resumed (attempt {item.attempt}/{MAX_RESUMES})",
        "would-resume": "stalled, would be resumed (dry run, nothing sent)",
        "waiting-network": f"stalled, waiting for the network ({host}:{port} does not answer), nothing sent",
        "cooldown": f"stalled again, last resume less than {RESUME_GAP_S}s ago, will be resumed later",
        "already-resumed": "stalled, a resume was already sent for this screen and it has not changed: needs a person",
        "deferred": "stalled, not resumed yet (this scan ran out of time), the next scan resumes it",
        "limit": f"stalled, {MAX_RESUMES} resumes were sent already: needs a person",
        "send-failed": "stalled, the resume could not be sent to its terminal",
        "blocked": "BLOCKED by an error a retry does not fix, not resumed: needs a person",
    }.get(item.action) or ("working again" if item.state == "working" else f"{item.state}, no API error on screen")
    return f"worker-resume: {item.task_id} {said}" + (f": {item.error}" if item.error and item.attention else "")


def watch(root: Path, run: str | None = None, *, task_ids: list[str] | None = None, dry_run: bool = False, interval_s: float = WATCH_INTERVAL_S,
          scans: int | None = None, sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] | None = None) -> None:
    """Scan every `interval_s` seconds and resume stalled workers, printing one line when a worker's state changes.

    Runs until interrupted; `scans` stops it after that many scans (for tests). A worker that needs nothing prints only
    when it had a line before. An Orca failure is printed once and the loop goes on.
    """
    say = out or (lambda line: print(line, flush=True))
    shown: dict[str, str] = {}
    done = 0
    while scans is None or done < scans:
        if done:
            sleep(interval_s)
        done += 1
        try:
            found = scan(root, run, task_ids=task_ids, dry_run=dry_run, session="watch")
        except orca_cli.OrcaError as error:
            if shown.get("") != str(error):
                say(f"worker-resume: Orca cannot be read: {str(error)[:200]}")
            shown[""] = str(error)
            continue
        shown.pop("", None)
        for item in found:
            mark = f"{item.action}|{item.fingerprint}" if item.attention else ""
            if shown.get(item.task_id, "") != mark:
                say(describe(item))
            shown[item.task_id] = mark
        for gone in set(shown) - {item.task_id for item in found} - {""}:
            if shown.pop(gone):
                say(f"worker-resume: {gone} settled or no longer has a worker")


def main_lines(found: list[Finding]) -> list[str]:
    """What a single `worker-resume` prints: one line per stalled or blocked worker, or that there is none."""
    lines = [describe(item) for item in found if item.attention]
    return lines or [f"worker-resume: no stalled worker ({len(found)} worker(s) read)"]

