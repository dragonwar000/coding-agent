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
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from coding_agent import events, notify, orca_cli, resume

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


def _mark_alive(root: Path, run: str | None, pid: int | None = None) -> None:
    path = root / ALIVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": pid or os.getpid(), "run": orca_cli.run_id(run) or "", "ts": time.time()}), encoding="utf-8")
    temporary.replace(path)


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


def watch(root: Path, run: str | None = None, *, task_ids: list[str] | None = None, interval_s: float = WATCH_INTERVAL_S,
          scans: int | None = None, confirm: int = DEAD_CONFIRM, sleep: Callable[[float], None] = time.sleep,
          out: Callable[[str], None] | None = None) -> int:
    """Scan every `interval_s` seconds, resume and remind as `resume.scan` does, and report each death once. Returns 0.

    Ends when no unsettled, unreported dispatch is left, or after `scans` scans (for tests). Prints one line when a
    worker's state changes and one per report. Any `Exception` in a scan or in one worker's report is printed once and
    the loop goes on; `FAILED_SCANS` failed scans in a row send one escalation (`_Failures`).
    """
    say = out or (lambda line: print(line, flush=True))
    suspects: dict[str, int] = {}
    shown: dict[str, str] = {}
    delivered: set[str] = set()
    failures = _Failures(root, run, say)

    def handle(item: resume.Finding, after: float | None, asking: set[str] | None) -> None:
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
                sleep(interval_s)
            done += 1
            clean = True
            try:
                _mark_alive(root, run)
                found = resume.scan(root, run, task_ids=task_ids, session="worker-watch")
                after = resume.silent_after_s()
                due = [item for item in found if item.state == STUCK and _stuck_key(item) not in delivered and not stuck_reported(root, item)]
                asking = _asking(run) if due else set()
                for item in found:
                    try:
                        handle(item, after, asking)
                    except Exception as error:  # noqa: BLE001 - one worker's report must not cost the others theirs
                        clean = False
                        failures.note(error, f"{item.task_id}: ")
                left = [item for item in found if _key(item) not in delivered and not reported(root, item)]
            except Exception as error:  # noqa: BLE001 - the watcher outlives a bad scan; KeyboardInterrupt is not an Exception
                failures.note(error)
                failures.scan_failed()
                continue
            if clean:
                failures.scan_ok()
            else:
                failures.scan_failed()
            if not left:
                say("worker-watch: no unsettled worker left to watch" + (" (the dead ones were reported)" if found else ""))
                return 0
    finally:
        _clear_alive(root)
    return 0


def running(root: Path, run: str | None = None, now: float | None = None) -> bool:
    """True when a watch loop for this Run wrote its alive file less than `STALE_AFTER_S` ago."""
    try:
        record: Any = json.loads((root / ALIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    now = time.time() if now is None else now
    return record.get("run") == (orca_cli.run_id(run) or "") and now - float(record.get("ts") or 0) < STALE_AFTER_S


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
            return "running"
        chosen = orca_cli.run_id(run)
        package_parent = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(part for part in (package_parent, env.get("PYTHONPATH", "")) if part)
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
