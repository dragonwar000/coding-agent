"""`worker-watch`: tell the coordinator when a worker is dead or stuck, with no model and no heartbeat.

Why (2026-11-13): Orca's preamble told every worker to send a `heartbeat` every 5 minutes. Each one cost the worker a
tool call, and Orca then typed "You have N orchestration messages" into the coordinator's terminal, which started a
full coordinator turn that only read "alive". Workers are now told not to send them (`orca_cli.NO_HEARTBEAT`), and
liveness comes from the worker's terminal instead.

`watch` loops every `WATCH_INTERVAL_S` seconds over every unsettled dispatch of the Run. Each scan is `resume.scan`:
Orca CLI reads only (task list, dispatch, screen, and `terminal show` for a screen that is not working), the same
automatic resume of a worker stopped on a transient API error, and the same reminder to a silent worker. Only what
those cannot fix is a death (`verdict`):

- the terminal is closed, or the agent process left it (`resume.terminal_death`);
- the screen shows an API error a retry does not fix (`PERMANENT_MARKS`, or an unknown `API Error`);
- a transient API error is still on screen after `MAX_RESUMES` resumes, or a resume did not change the screen;
- the worker stays silent for `silent_after_s()` after `MAX_NUDGES` reminders.

A death seen on `DEAD_CONFIRM` scans in a row is reported once (`report_death`): one `escalation` to the Run's mailbox,
subject `worker dead: <task title>`, body with task id, dispatch id, terminal, the reason and the last screen lines
(`BODY_LINES` lines at most). The report is appended to the resumes ledger with `kind: death`, so the same dispatch is
never reported twice, by this loop or a later one. A report Orca refused is tried again on the next scan.

A scan that fails never ends the loop (2026-11-23: until then only `OrcaError` was caught, so a damaged state file, a
JSON error or a ledger write error stopped the watcher and only its log said so). Every `Exception` of a scan or of one
worker's report is caught and printed once per distinct error; `KeyboardInterrupt` and `SystemExit` still stop it.
After `FAILED_SCANS` failed scans in a row (`_Failures`) the watcher sends one escalation about itself, subject
`worker-watch failing`, recorded in the ledger with `kind: watch-failing`, so neither this loop nor a restarted one
repeats it until a scan succeeds again (`kind: watch-recovered`). A dispatch whose report reached Orca is kept in memory
too, so a ledger that cannot be written does not repeat the report every scan. Assumption, not measured: Orca accepts an
`escalation` with no task or dispatch id, as `orca orchestration send --help` lists both as optional.

A worker whose spinner runs with no progress is `stuck` (`resume.track_stuck`, which documents the two signals). It is
not a death: `report_stuck` sends one escalation per stretch without progress, subject `worker stuck: <task title>`, with
task id, dispatch id, terminal, minutes, signal and last screen lines, recorded with `kind: stuck` and the episode. The
watcher never interrupts it, never types into its terminal and keeps watching it; when it progresses and sticks again,
that is a new episode and a new report. A worker waiting on an open question is not reported: its silence has a cause
the coordinator already sees.

Each of those three reports can also tell a person through the optional `notify` command of the manifest (`notify.py`):
once per event, also when Orca refused the escalation, since that is when a person most needs to hear it.

The loop ends when no unsettled dispatch is left that has not been reported. `ensure_running` starts it in the
background (after `delegate`, `plan-next`, and from the board while workers are unsettled) unless one already runs for
the Run: the loop writes `ALIVE_FILE` every scan, and a fresh file means it is alive. `CODING_AGENT_WORKER_WATCH=off`
turns the automatic start off; `worker-watch` still runs by hand.

Cleanup without a coordinator (2026-11-23): worktree cleanup and the session release (`release.py`) ran only during a
coordinator turn, so finished workers stayed open once it stopped. The command now also runs `housekeep` every
`HOUSEKEEP_INTERVAL_S`, and the SessionEnd hook starts it (`after_session`). Once no worker is unsettled it stays only
while a later pass can still do something, scanning every `HOUSEKEEP_INTERVAL_S`, for `LINGER_PASSES` passes at most
(6 hours); a run of `GIVE_UP_SCANS` failed scans ends it too. A pass is skipped while the machine is overloaded
(`_overloaded`). An unreachable Orca removes and closes nothing: every step needs a fresh read.

One watcher per project: the loop holds `LOCK_FILE` (an OS lock, released when the process dies). A start for another Run
leaves a request in `WAKE_FILE`, and the running loop follows that Run as well.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from coding_agent import cleanup, events, notify, orca_cli, release, resume

WATCH_ENV = "CODING_AGENT_WORKER_WATCH"
WATCH_INTERVAL_S = resume.WATCH_INTERVAL_S
DEAD_CONFIRM = 2         # scans in a row that must see a death before it is reported: one odd Orca answer is not a death
SCREEN_LINES = 10        # last screen lines in a report
BODY_LINES = 15          # lines in a report's body at most
LINE_CHARS = 160
ALIVE_FILE = Path(".coding-agent") / "state" / "worker-watch.json"
LOG_FILE = Path(".coding-agent") / "worker-watch.log"
STALE_AFTER_S = 3 * WATCH_INTERVAL_S  # an alive file older than this belongs to a loop that is gone
DEATH = "death"
STUCK = "stuck"
FAILED_SCANS = 5         # failed scans in a row before the watcher tells the coordinator about itself
WATCH_FAILING = "watch-failing"
WATCH_RECOVERED = "watch-recovered"
HOUSEKEEP_INTERVAL_S = 300.0  # cleanup and release pass; also the scan interval once only those are left
LINGER_PASSES = 72       # passes (6 hours) a watcher with no unsettled worker waits for an acknowledgement or a grace period
GIVE_UP_SCANS = 360      # failed scans in a row (6 hours at the default interval) after which the process ends
WAKE_POLL_S = 15.0       # how often a lingering loop looks for a request; a file stat, no Orca call
RESTART_GAP_S = 900      # the board does not restart a watcher that found nothing to do this recently
LOCK_FILE = Path(".coding-agent") / "state" / "worker-watch.lock"
WAKE_FILE = Path(".coding-agent") / "state" / "worker-watch-wake.json"
EXIT_FILE = Path(".coding-agent") / "state" / "worker-watch-exit.json"


def verdict(item: resume.Finding, after: float | None = None) -> str | None:
    """Why the worker of `item` is dead or stuck for good, or None while a resume or a reminder may still help.

    `item` is a finding after `resume.scan`, so its `action` says what the resume or the reminder decided. `after` is
    `resume.silent_after_s()`; a silent worker that got every reminder counts once it has been silent that long again.
    """
    if item.state == "dead":
        return item.error or "terminal or agent is gone"
    if item.state == "blocked":
        return f"API error a retry does not fix: {item.error}"
    if item.state == "stalled" and item.action == "limit":
        return f"transient API error still on screen after {resume.MAX_RESUMES} resumes: {item.error}"
    if item.state == "stalled" and item.action == "already-resumed":
        return f"a resume was sent and the screen did not change: {item.error}"
    if item.state == "silent" and item.action == "nudge-limit" and after is not None and item.idle_s >= after:
        return f"silent for {resume._minutes(item.idle_s)} min after {resume.MAX_NUDGES} reminders, no worker_done"
    return None


def _key(item: resume.Finding) -> str:
    return item.dispatch or item.task_id


def reported(root: Path, item: resume.Finding) -> bool:
    """True once a death of this dispatch reached the coordinator."""
    return bool(resume._sent(root, _key(item), DEATH))


def screen_tail(screen: str, limit: int = SCREEN_LINES) -> list[str]:
    """The last `limit` lines of a screen with text, each cut to `LINE_CHARS`. Rules, the empty prompt and Claude Code's
    mode footer (`⏵⏵ bypass permissions on ...`) are left out: they are on every screen and say nothing."""
    lines = [line.replace("\xa0", " ").rstrip() for line in screen.splitlines()]
    kept = [line for line in lines if line.strip() and not resume.RULE_LINE.match(line) and line.strip() != "❯"
            and not line.strip().startswith("⏵⏵")]
    return [line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + "…" for line in kept[-limit:]]


def death_body(item: resume.Finding, reason: str, screen: str) -> str:
    """The report's body: ids, the reason, and the last screen lines, `BODY_LINES` lines at most."""
    head = [f"task: {item.task_id}", f"dispatch: {item.dispatch}", f"terminal: {item.terminal}", f"reason: {reason}"[:LINE_CHARS * 2]]
    tail = screen_tail(screen, BODY_LINES - len(head) - 1)
    return "\n".join(head + ["last screen lines:"] + ([f"  {line}" for line in tail] or ["  (the terminal cannot be read)"]))


def report_death(root: Path, item: resume.Finding, reason: str, run: str | None = None, *, session: str = "worker-watch",
                 delivered: set[str] | None = None) -> str:
    """Tell the coordinator once that a worker is dead. Returns `reported`, `already-reported` or `report-failed`.

    `delivered` is the loop's memory of the reports Orca accepted: the key is added before the ledger is written, so a
    ledger write that raises does not send the same report again on the next scan.
    """
    key = _key(item)
    if (delivered is not None and key in delivered) or reported(root, item):
        return "already-reported"
    body = death_body(item, reason, orca_cli.screen_of(item.terminal) if item.terminal else "")
    subject = f"{orca_cli.DEATH_SUBJECT}{item.title or item.task_id}"[:120]
    error: str | None = None
    try:
        orca_cli.escalate(run=run, subject=subject, body=body, task_id=item.task_id, dispatch=item.dispatch)
    except orca_cli.OrcaError as failure:
        error = str(failure)[:300]
    if error is None and delivered is not None:
        delivered.add(key)
    notify.fire(root, DEATH, key, task_id=item.task_id, dispatch=item.dispatch, terminal=item.terminal, title=item.title, reason=reason,
                session=session, seen=delivered)
    orca_cli._append(root / resume.LEDGER, {"ts": int(time.time()), "kind": DEATH, "task_id": item.task_id, "dispatch": key,
                                            "terminal": item.terminal, "reason": reason, "sent": error is None})
    events.record(root, guard="worker-watch", kind="reported" if error is None else "report-failed", mode="enforce",
                  applied=error is None, session=session,
                  detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal, "state": item.state,
                          "reason": reason, "error": error})
    return "reported" if error is None else "report-failed"


def _mark_alive(root: Path, run: str | None, pid: int | None = None, stale_s: float | None = None) -> None:
    """Write the alive file. `stale_s` is how long it stays fresh, for a loop that scans less often than the default."""
    path = root / ALIVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    record = {"pid": pid or os.getpid(), "run": orca_cli.run_id(run) or "", "ts": time.time(), **({"stale_s": stale_s} if stale_s else {})}
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(path)


def _lock(root: Path) -> Any | None:
    """Take the project's watcher lock without waiting. Returns the open file that holds it, or None when it is held."""
    path = root / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _unlock(handle: Any) -> None:
    """Close the lock's file, which releases it."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()


def _request(root: Path, run: str | None) -> None:
    """Ask the running loop to scan now, and to follow `run` too."""
    path = root / WAKE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"run": orca_cli.run_id(run) or "", "ts": time.time()}), encoding="utf-8")
    temporary.replace(path)


def _requested(root: Path) -> dict[str, Any]:
    try:
        record = json.loads((root / WAKE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _pause(root: Path, seconds: float, since: float, sleep: Callable[[float], None]) -> None:
    """Sleep up to `seconds`, ending early when a start request newer than `since` arrives."""
    left = seconds
    while left > 0:
        step = min(WAKE_POLL_S, left)
        sleep(step)
        left -= step
        if float(_requested(root).get("ts") or 0) > since:
            return


def _overloaded() -> bool:
    """True when the 1-minute load is above twice the CPU count. False where the load cannot be read."""
    try:
        return os.getloadavg()[0] > 2 * (os.cpu_count() or 1)
    except (OSError, AttributeError):
        return False


def housekeep(root: Path, run: str | None = None, *, session: str = "worker-watch",
              say: Callable[[str], None] | None = None, shown: set[str] | None = None) -> bool:
    """One cleanup and release pass. True while a later pass may still have something to do.

    Honors `CODING_AGENT_AUTO_CLEAN=off` and `CODING_AGENT_AUTO_RELEASE=off`. Orca failures become warnings; each
    distinct warning is printed once (`shown`).
    """
    say = say or (lambda line: None)
    if not (cleanup.auto_enabled() or release.enabled()):
        return False
    if _overloaded():
        say("worker-watch: machine overloaded, cleanup pass skipped")
        return True
    warnings: list[str] = []
    waiting = False
    if cleanup.auto_enabled():
        found = cleanup.candidates(root, run, warnings=warnings, session=session)
        results = cleanup.auto_clean(root, run, session=session, found=found, warnings=warnings, strict=True)
        gone = {item.worktree for item, outcome in results if outcome == "removed"}
        for item in found:
            if item.worktree in gone:
                say(f"worker-watch: removed {Path(item.worktree).name} ({item.task_id})")
        waiting = any(item.safe and item.exists and item.worktree not in gone for item in found) or any(item.status == cleanup.RUN_UNREAD for item in found)
    outcome = release.sweep(root, run, session=session, warnings=warnings)
    for item in outcome.released:
        say(f"worker-watch: session of {Path(item.worktree).name} ({item.task_id}) ended: {item.reason}")
    for warning in warnings:
        if shown is None or warning not in shown:
            say(f"worker-watch: warning: {warning}")
            if shown is not None:
                shown.add(warning)
    return waiting or bool(outcome.waiting)


def _clear_alive(root: Path) -> None:
    path = root / ALIVE_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _stuck_key(item: resume.Finding) -> str:
    return f"{STUCK}:{_key(item)}:{item.episode}"


def stuck_reported(root: Path, item: resume.Finding) -> bool:
    """True once this stretch without progress (`item.episode`) reached the coordinator."""
    return any(record.get("episode") == item.episode for record in resume._sent(root, _key(item), STUCK))


def stuck_reason(item: resume.Finding) -> str:
    """How long the worker shows no progress, what measured it, and what that signal cannot tell."""
    measured = ("no write to its Claude Code transcript (a long foreground tool call writes nothing either)"
                if item.signal == "transcript" else "screen unchanged apart from the spinner")
    return f"no progress for {resume._minutes(item.stuck_s)} min; signal: {item.signal}, {measured}; not interrupted, read the terminal, then decide"


def report_stuck(root: Path, item: resume.Finding, run: str | None = None, *, session: str = "worker-watch",
                 delivered: set[str] | None = None) -> str:
    """Tell the coordinator once per episode that a worker looks hung. Only reads its screen; never types into it.

    Returns `reported`, `already-reported` or `report-failed` (tried again on the next scan).
    """
    if (delivered is not None and _stuck_key(item) in delivered) or stuck_reported(root, item):
        return "already-reported"
    reason = stuck_reason(item)
    body = death_body(item, reason, orca_cli.screen_of(item.terminal) if item.terminal else "")
    subject = f"{orca_cli.STUCK_SUBJECT}{item.title or item.task_id}"[:120]
    error: str | None = None
    try:
        orca_cli.escalate(run=run, subject=subject, body=body, task_id=item.task_id, dispatch=item.dispatch)
    except orca_cli.OrcaError as failure:
        error = str(failure)[:300]
    if error is None and delivered is not None:
        delivered.add(_stuck_key(item))
    notify.fire(root, STUCK, f"{_key(item)}:{item.episode}", task_id=item.task_id, dispatch=item.dispatch, terminal=item.terminal,
                title=item.title, reason=reason, session=session, seen=delivered)
    orca_cli._append(root / resume.LEDGER, {"ts": int(time.time()), "kind": STUCK, "task_id": item.task_id, "dispatch": _key(item),
                                            "terminal": item.terminal, "episode": item.episode, "signal": item.signal,
                                            "stuck_s": item.stuck_s, "sent": error is None})
    events.record(root, guard="worker-watch", kind="stuck-reported" if error is None else "report-failed", mode="enforce",
                  applied=error is None, session=session,
                  detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal, "state": item.state,
                          "signal": item.signal, "stuck_s": item.stuck_s, "episode": item.episode, "error": error})
    return "reported" if error is None else "report-failed"


def _asking(run: str | None) -> set[str] | None:
    """The tasks with an open question, or None when Orca cannot say (a stuck report then waits for the next scan)."""
    try:
        return {str(question.get("task_id") or "") for question in orca_cli.worker_activity(run)}
    except orca_cli.OrcaError:
        return None


def _alert_open(root: Path) -> bool:
    """True when the ledger's last word about the watcher's own failure is an escalation that reached the coordinator."""
    try:
        marks = [record for record in resume.history(root) if record.get("kind") in (WATCH_FAILING, WATCH_RECOVERED)]
    except Exception:  # noqa: BLE001 - an unreadable ledger must not stop the loop from starting
        return False
    return bool(marks) and marks[-1].get("kind") == WATCH_FAILING and marks[-1].get("sent") is True


class _Failures:
    """The loop's own failures: one printed line per distinct error, and one escalation per run of failed scans."""

    def __init__(self, root: Path, run: str | None, say: Callable[[str], None], *, session: str = "worker-watch"):
        self.root, self.run, self.say, self.session = root, run, say, session
        self.streak = 0
        self.printed: set[str] = set()
        self.last = ""
        self.alerted = _alert_open(root)
        self.send_failed = False
        self.since = 0.0         # when this run of failed scans started: names it for the notify command
        self.notified: set[str] = set()

    def note(self, error: Exception, where: str = "") -> None:
        """Print `error` unless this run of failed scans already printed it."""
        text = str(error)[:200] if isinstance(error, orca_cli.OrcaError) else f"{type(error).__name__}: {error}"[:200]
        self.last = f"{where}{text}"
        if self.last not in self.printed:
            self.printed.add(self.last)
            self.say(f"worker-watch: Orca cannot be read: {self.last}" if isinstance(error, orca_cli.OrcaError)
                     else f"worker-watch: scan failed, still watching: {self.last}")

    def scan_failed(self) -> None:
        self.since = self.since if self.streak else time.time()
        self.streak += 1
        if self.streak >= FAILED_SCANS and not self.alerted:
            self._alert()

    def scan_ok(self) -> None:
        self.streak = 0
        self.printed.clear()
        self.send_failed = False
        if not self.alerted:
            return
        self.alerted = False
        self.say("worker-watch: scans succeed again")
        try:
            orca_cli._append(self.root / resume.LEDGER, {"ts": int(time.time()), "kind": WATCH_RECOVERED, "sent": True})
            events.record(self.root, guard="worker-watch", kind="recovered", mode="enforce", applied=True, session=self.session, detail={})
        except Exception as error:  # noqa: BLE001 - bookkeeping must not stop the loop
            self.note(error)

    def _alert(self) -> None:
        """One escalation about the watcher itself. Refused by Orca: tried again next scan, with one event per run."""
        body = (f"worker-watch failed {self.streak} scans in a row and keeps trying.\nlast error: {self.last[:300]}\n"
                f"log: {self.root / LOG_FILE}")
        notify.fire(self.root, WATCH_FAILING, str(self.since), title=orca_cli.WATCH_FAILING_SUBJECT,
                    reason=f"{self.streak} scans in a row failed: {self.last[:300]}", session=self.session, seen=self.notified)
        try:
            orca_cli.escalate(run=self.run, subject=orca_cli.WATCH_FAILING_SUBJECT, body=body, task_id="", dispatch="")
        except Exception as failure:  # noqa: BLE001 - the watcher outlives a mailbox it cannot reach
            if not self.send_failed:
                self.send_failed = True
                self.say(f"worker-watch: could not report its own failure, tried again next scan: {str(failure)[:200]}")
                self._record("report-failed", False, str(failure)[:300])
            return
        self.alerted = True
        self.say(f"worker-watch: told the coordinator that {self.streak} scans in a row failed")
        try:
            orca_cli._append(self.root / resume.LEDGER, {"ts": int(time.time()), "kind": WATCH_FAILING, "error": self.last[:300], "sent": True})
        except Exception as error:  # noqa: BLE001 - `alerted` already holds it for this loop
            self.note(error)
        self._record("watch-failing", True, self.last[:300])

    def _record(self, kind: str, applied: bool, error: str) -> None:
        try:
            events.record(self.root, guard="worker-watch", kind=kind, mode="enforce", applied=applied, session=self.session,
                          detail={"streak": self.streak, "error": error})
        except Exception as failure:  # noqa: BLE001 - an event log that cannot be written must not stop the loop
            self.note(failure)


class _ScanFailed(Exception):
    """A Run could not be scanned; its error was already noted."""


def watch(root: Path, run: str | None = None, *, task_ids: list[str] | None = None, interval_s: float = WATCH_INTERVAL_S,
          scans: int | None = None, confirm: int = DEAD_CONFIRM, sleep: Callable[[float], None] = time.sleep,
          out: Callable[[str], None] | None = None, housekeep_s: float | None = None) -> int:
    """Scan every `interval_s` seconds, resume and remind as `resume.scan` does, and report each death once. Returns 0.

    Ends when no unsettled, unreported dispatch is left, or after `scans` scans (for tests). Prints one line when a
    worker's state changes and one per report. Any `Exception` in a scan or in one worker's report is printed once and
    the loop goes on; `FAILED_SCANS` failed scans in a row send one escalation (`_Failures`), and `GIVE_UP_SCANS` end
    the process. With `housekeep_s` (the `worker-watch` command, unless it names tasks) it also runs `housekeep` that
    often and stays while a pass still has something to wait for, `LINGER_PASSES` at most. A second loop for the same
    project leaves a request for the first and returns.
    """
    say = out or (lambda line: print(line, flush=True))
    lock = _lock(root)
    if lock is None:
        _request(root, run)
        say("worker-watch: another watcher runs for this project; it was asked to follow this Run")
        return 0
    try:
        return _watch(root, run, task_ids, interval_s, scans, confirm, sleep, say, None if task_ids else housekeep_s)
    finally:
        _unlock(lock)


def _watch(root: Path, run: str | None, task_ids: list[str] | None, interval_s: float, scans: int | None, confirm: int,
           sleep: Callable[[float], None], say: Callable[[str], None], housekeep_s: float | None) -> int:
    started = time.time()
    followed: list[str | None] = [run]
    lingering = 0
    last_pass: float | None = None
    shown_warnings: set[str] = set()
    suspects: dict[str, int] = {}
    shown: dict[str, str] = {}
    delivered: set[str] = set()
    failures = _Failures(root, run, say)

    def handle(item: resume.Finding, after: float | None, asking: set[str] | None, run: str | None) -> None:
        key = _key(item)
        mark = f"{item.state}|{item.action}|{item.attempt}|{item.episode if item.state == STUCK else ''}" if item.attention else ""
        if shown.get(key, "") != mark:
            say(resume.describe(item).replace("worker-resume:", "worker-watch:", 1))
        shown[key] = mark
        if item.state == STUCK:
            if asking is not None and item.task_id not in asking and _stuck_key(item) not in delivered and not stuck_reported(root, item):
                outcome = report_stuck(root, item, run, delivered=delivered)
                say(f"worker-watch: {item.task_id} " + ("reported stuck to the coordinator" if outcome == "reported"
                                                         else "could not be reported as stuck, tried again next scan"))
            return
        reason = verdict(item, after)
        if reason is None or key in delivered or reported(root, item):
            suspects.pop(key, None)
            return
        suspects[key] = suspects.get(key, 0) + 1
        if suspects[key] < confirm:
            return
        outcome = report_death(root, item, reason, run, delivered=delivered)
        say(f"worker-watch: {item.task_id} " + ("reported dead to the coordinator" if outcome == "reported"
                                                 else "could not be reported, tried again next scan") + f": {reason}")

    done = 0
    try:
        while scans is None or done < scans:
            if done:
                if lingering and housekeep_s:
                    _pause(root, housekeep_s, max(started, float(_requested(root).get("ts") or 0)), sleep)
                else:
                    sleep(interval_s)
            done += 1
            clean = True
            wake = _requested(root)
            if wake.get("run") and float(wake.get("ts") or 0) >= started and wake["run"] not in followed:
                followed.append(str(wake["run"]))
            failures.run = followed[-1]
            try:
                _mark_alive(root, followed[-1], stale_s=3 * housekeep_s if lingering and housekeep_s else None)
                found: list[resume.Finding] = []
                left: list[resume.Finding] = []
                unread_run = False
                for current in list(followed):
                    if current is None and orca_cli.run_id(None) is None and housekeep_s is not None:
                        continue  # a project with no Run left: only the records' own Runs are read, by `housekeep`
                    try:
                        scanned = resume.scan(root, current, task_ids=task_ids, session="worker-watch")
                    except Exception as error:  # noqa: BLE001 - one Run that cannot be read says nothing about the others
                        failures.note(error)
                        unread_run = True
                        continue
                    after = resume.silent_after_s()
                    due = [item for item in scanned if item.state == STUCK and _stuck_key(item) not in delivered and not stuck_reported(root, item)]
                    asking = _asking(current) if due else set()
                    for item in scanned:
                        try:
                            handle(item, after, asking, current)
                        except Exception as error:  # noqa: BLE001 - one worker's report must not cost the others theirs
                            clean = False
                            failures.note(error, f"{item.task_id}: ")
                    open_items = [item for item in scanned if _key(item) not in delivered and not reported(root, item)]
                    found += scanned
                    left += open_items
                    if not open_items and current != followed[-1]:
                        followed.remove(current)
                waiting = False
                if housekeep_s is not None and ((not left and not unread_run) or last_pass is None or time.monotonic() - last_pass >= housekeep_s):
                    last_pass = time.monotonic()
                    waiting = housekeep(root, followed[-1], say=say, shown=shown_warnings)
                if unread_run:  # a Run that cannot be read may still hold workers: never read as "nothing left"
                    raise _ScanFailed
            except Exception as error:  # noqa: BLE001 - the watcher outlives a bad scan; KeyboardInterrupt is not an Exception
                if not isinstance(error, _ScanFailed):
                    failures.note(error)
                failures.scan_failed()
                if failures.streak >= GIVE_UP_SCANS:
                    say(f"worker-watch: stopping after {failures.streak} failed scans in a row; the next board or session end starts it again")
                    _note_exit(root)
                    return 0
                continue
            if clean:
                failures.scan_ok()
            else:
                failures.scan_failed()
            if left:
                lingering = 0
            elif waiting and lingering < LINGER_PASSES:
                lingering += 1
            else:
                say("worker-watch: no unsettled worker left to watch" + (" (the dead ones were reported)" if found else ""))
                _note_exit(root)
                return 0
    finally:
        _clear_alive(root)
    return 0


def _note_exit(root: Path) -> None:
    try:
        path = root / EXIT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _exited_recently(root: Path, now: float | None = None) -> bool:
    try:
        record = json.loads((root / EXIT_FILE).read_text(encoding="utf-8"))
        return (time.time() if now is None else now) - float(record.get("ts") or 0) < RESTART_GAP_S
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def running(root: Path, run: str | None = None, now: float | None = None) -> bool:
    """True when a watch loop for this Run wrote its alive file less than `STALE_AFTER_S` ago."""
    try:
        record: Any = json.loads((root / ALIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    now = time.time() if now is None else now
    return record.get("run") == (orca_cli.run_id(run) or "") and now - float(record.get("ts") or 0) < float(record.get("stale_s") or STALE_AFTER_S)


def enabled() -> bool:
    """False when `CODING_AGENT_WORKER_WATCH` is `off` (or `0`, `false`, `no`); on by default."""
    return os.environ.get(WATCH_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def ensure_running(root: Path, run: str | None = None, *, session: str = "cli") -> str | None:
    """Start `worker-watch` in the background unless one already runs for the Run. Never raises.

    Returns `started`, `running`, or None (turned off, or the start failed; a failure is recorded as an event). The
    process is detached from this one (its own session), so it outlives the hook or command that started it; its
    output goes to `LOG_FILE`.
    """
    if not enabled():
        return None
    try:
        if running(root, run):
            _request(root, run)  # a lingering loop scans at once, so a worker just started is watched without delay
            return "running"
        held = _lock(root)
        if held is None:
            _request(root, run)
            return "running"
        _unlock(held)
        chosen = orca_cli.run_id(run)
        package_parent = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(part for part in (package_parent, env.get("PYTHONPATH", "")) if part)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        log_path = root / LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        detach: dict[str, Any] = ({"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                                  if os.name == "nt" else {"start_new_session": True})
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "coding_agent.cli", "--root", str(root), "worker-watch",
                                        *(["--run", chosen] if chosen else [])],
                                       cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detach)
        _mark_alive(root, run, pid=process.pid)
        events.record(root, guard="worker-watch", kind="started", mode="enforce", applied=True, session=session,
                      detail={"pid": process.pid, "run": chosen})
        return "started"
    except Exception as error:  # noqa: BLE001 - called from hooks and commands that must not fail over the watcher
        try:
            events.record(root, guard="worker-watch", kind="start-failed", mode="enforce", applied=False, session=session,
                          detail={"error": f"{type(error).__name__}: {error}"[:300]})
        except OSError:
            pass
        return None


def _followed_run(root: Path, run: str | None) -> str | None:
    """`run`, else the current Run, else the Run of the newest worker record."""
    chosen = orca_cli.run_id(run)
    if chosen:
        return chosen
    runs = [record["run"] for owner in orca_cli.all_roots(root) for record in orca_cli.worker_records(owner) if record.get("run")]
    return runs[-1] if runs else None


def ensure_for_cleanup(root: Path, run: str | None = None, *, session: str = "cli", throttle: bool = True) -> str | None:
    """Start the watcher when a finished worker may still need cleanup or release. Reads local files only. Never raises.

    `throttle` (the board, every prompt) leaves alone a watcher that ended within `RESTART_GAP_S`.
    """
    try:
        if not (cleanup.auto_enabled() or release.enabled()):
            return None
        # The board cleans in its own turn, so only a session still to release is worth a process. A session end is the
        # last chance to clean what the coordinator just merged.
        if not (release.pending(root) or (not throttle and cleanup.auto_enabled()
                                          and any(orca_cli.worker_records(owner) for owner in orca_cli.all_roots(root)))):
            return None
        if throttle and _exited_recently(root):
            return None
        return ensure_running(root, _followed_run(root, run), session=session)
    except Exception as error:  # noqa: BLE001 - called from hooks that must not fail over the watcher
        try:
            events.record(root, guard="worker-watch", kind="start-failed", mode="enforce", applied=False, session=session,
                          detail={"error": f"{type(error).__name__}: {error}"[:300]})
        except OSError:
            pass
        return None


def after_session(root: Path, session: str = "session-end") -> str | None:
    """The SessionEnd step of a coordinator: keep cleanup and release going after it stops."""
    orca_cli.use_stored_run(root)
    return ensure_for_cleanup(root, session=session, throttle=False)
