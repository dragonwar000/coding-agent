"""Worker liveness without heartbeats: the no-heartbeat instruction, liveness read from the terminal, the death watcher
(`worker-watch`) and its one escalation per dispatch, and the coordinator hook that drops heartbeat-only wake-ups."""

import json
import subprocess
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, events, hooks, orca_cli, resume, watcher
from coding_agent.hooks import handlers

from test_resume import BLOCKED, FOOTER, STALLED, WORKING, Workers

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
RUN = "run_test"
POINTER = f"\nYou have 2 orchestration messages. Run `orca orchestration check --run {RUN}`.\n"

IDLE = """⏺ Da sua xong ham parse va chay test.

✻ Worked for 4m 2s · done 9:30 PM
""" + FOOTER

# Claude Code has exited: the shell prompt sits under the last frame.
EXITED = IDLE + "\nuser@host worktree % "


@pytest.fixture
def clock(monkeypatch):
    now = [100_000.0]
    monkeypatch.setattr(resume.time, "time", lambda: now[0])
    monkeypatch.setattr(watcher.time, "time", lambda: now[0])
    return now


class Terminals:
    """`orca_cli.terminal_state` per handle: alive by default, `gone` (closed) or `no-agent` (agent exited), counting reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.kind: dict[str, str] = {}
        self.reads: list[str] = []

        def state(handle: str) -> dict:
            self.reads.append(handle)
            kind = self.kind.get(handle, "alive")
            if kind == "gone":
                return {"exists": False, "code": "terminal_handle_stale"}
            if kind == "error":
                raise orca_cli.OrcaError("orca terminal exited 3: daemon not running")
            return {"exists": True, "connected": True, "orphaned": False, "agent": None if kind == "no-agent" else "claude",
                    "last_output_ms": 1}

        monkeypatch.setattr(orca_cli, "terminal_state", state)


class Escalations:
    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.sent: list[dict] = []
        self.fail = False

        def escalate(*, run, subject, body, task_id, dispatch):
            if self.fail:
                raise orca_cli.OrcaError("orca send exited 1")
            self.sent.append({"run": run, "subject": subject, "body": body, "task_id": task_id, "dispatch": dispatch})

        monkeypatch.setattr(orca_cli, "escalate", escalate)


# --- part 1: workers are told not to send heartbeats -------------------------------------------------------------------

def test_the_kickoff_tells_the_worker_not_to_send_heartbeats_and_keeps_worker_done_once():
    text = orca_cli.kickoff_text("task_1", "ctx_1")
    assert "KHONG gui heartbeat" in text and "coding-agent tu theo doi terminal" in text
    assert "worker_done DUNG MOT LAN cuoi cung" in text and "escalation" in text
    assert "\n" not in text and ">" not in text and not set(text) & set('`$"\\')


def test_every_task_spec_ends_with_the_no_heartbeat_instruction(repo, tmp_path, monkeypatch):
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-h1", spec="build the thing\n", title="Build", run=RUN)
    spec = json.loads(state.read_text(encoding="utf-8"))["tasks"][task_id]["spec"]
    assert spec == "project: demo-repo\nt_id: T-h1\n\nbuild the thing\n\n" + orca_cli.NO_HEARTBEAT
    assert "do NOT send `heartbeat`" in spec and "worker_done exactly once" in spec and "escalation" in spec


# --- part 2: liveness comes from the terminal ---------------------------------------------------------------------------

def test_a_closed_terminal_or_an_exited_agent_is_dead_and_a_quiet_idle_worker_is_alive(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": IDLE, "task_3": EXITED, "task_4": WORKING, "task_5": ""})
    terminals = Terminals(monkeypatch)
    terminals.kind.update({"term_task_2": "gone", "term_task_3": "no-agent", "term_task_4": "gone", "term_task_5": "gone"})
    found = {item.task_id: item for item in resume.stalled_workers(repo)}
    assert {task: item.state for task, item in found.items()} == {
        "task_1": "idle", "task_2": "dead", "task_3": "dead", "task_4": "working", "task_5": "dead"}
    assert "terminal is gone" in found["task_2"].error and "agent process exited" in found["task_3"].error
    assert not found["task_1"].attention and found["task_1"].title == "Viec task_1"
    # A working screen is alive by itself: Orca is not asked about its terminal.
    assert "term_task_4" not in terminals.reads


def test_an_orca_that_cannot_be_read_is_not_a_death(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": IDLE})
    Terminals(monkeypatch).kind["term_task_1"] = "error"
    assert [item.state for item in resume.stalled_workers(repo)] == ["idle"]


def test_terminal_state_reads_orca_terminal_show(tmp_path, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(tmp_path / "orca-state.json"))
    monkeypatch.setenv("FAKE_ORCA_GONE_TERMINALS", "term_gone")
    monkeypatch.setenv("FAKE_ORCA_NO_AGENT_TERMINALS", "term_shell")
    assert orca_cli.terminal_state("term_gone") == {"exists": False, "code": "terminal_handle_stale"}
    assert orca_cli.terminal_state("term_shell")["agent"] is None
    live = orca_cli.terminal_state("term_live")
    assert live["exists"] and live["agent"] == "claude" and live["last_output_ms"] == 1794568264920


def test_the_board_measures_quiet_on_the_screen_not_on_messages(repo, monkeypatch, clock):
    """With reminders off a quiet worker stays in the running group, and its note comes from the screen's idle time."""
    monkeypatch.setenv(resume.SILENT_ENV, "off")
    workers = Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": WORKING})
    Terminals(monkeypatch)
    coordinator.read_board(repo, run=RUN)
    clock[0] += 25 * 60
    (repo / resume.SCAN_FILE).unlink()
    lines, error = coordinator.read_board(repo, run=RUN)
    text = "\n".join(lines)
    assert error is None and "task_1 [dispatched] Viec task_1 (màn hình đứng yên 25 phút)" in text
    assert "task_2 [dispatched] Viec task_2\n" in text + "\n" and "không có tin" not in text
    assert workers.sent == []


def test_the_board_lists_dead_workers_first(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": STALLED})
    Terminals(monkeypatch).kind["term_task_1"] = "gone"
    lines, _ = coordinator.read_board(repo, run=RUN)
    assert lines[0] == "worker chết, terminal hoặc agent đã mất (1):"
    assert lines[1].startswith("  - task_1 [dispatched] Viec task_1: worker chết, cần người xử lý — terminal is gone")
    assert any(line.startswith("worker bị dừng vì lỗi API/mạng") for line in lines)


def test_the_contract_names_the_watcher_and_no_heartbeats():
    text = coordinator.contract("src")
    assert "worker-watch" in text and "không gửi heartbeat" in text and "worker dead:" in text and "màn hình đứng yên" in text
    assert "không có tin" not in text


# --- part 3: the death watcher ------------------------------------------------------------------------------------------

def finding(**fields) -> resume.Finding:
    base = {"task_id": "task_1", "dispatch": "ctx_1", "terminal": "term_1", "state": "idle"}
    return resume.Finding(**{**base, **fields})


@pytest.mark.parametrize("fields, dead", [
    ({"state": "dead", "error": "terminal is gone (Orca: terminal_handle_stale)"}, "terminal is gone"),
    ({"state": "blocked", "action": "blocked", "error": "API Error: 401"}, "retry does not fix"),
    ({"state": "stalled", "action": "limit", "error": "API Error: Connection lost"}, f"after {resume.MAX_RESUMES} resumes"),
    ({"state": "stalled", "action": "already-resumed", "error": "API Error: x"}, "did not change"),
    ({"state": "silent", "action": "nudge-limit", "idle_s": resume.SILENT_AFTER_S}, f"after {resume.MAX_NUDGES} reminders"),
    ({"state": "silent", "action": "nudge-limit", "idle_s": 30}, None),
    ({"state": "silent", "action": "nudged", "idle_s": resume.SILENT_AFTER_S}, None),
    ({"state": "stalled", "action": "resumed"}, None),
    ({"state": "stalled", "action": "waiting-network"}, None),
    ({"state": "stalled", "action": "cooldown"}, None),
    ({"state": "idle", "idle_s": 99_999}, None),
    ({"state": "working"}, None),
    ({"state": "unreadable"}, None),
])
def test_only_what_no_resume_or_reminder_can_fix_is_a_death(fields, dead):
    said = watcher.verdict(finding(**fields), resume.SILENT_AFTER_S)
    assert (said is None) if dead is None else (dead in said)


def test_the_report_body_is_short_and_carries_the_ids_and_the_screen():
    screen = "\n".join(f"line {n}" for n in range(40)) + FOOTER
    body = watcher.death_body(finding(state="dead", title="Fix parse"), "terminal is gone", screen)
    lines = body.splitlines()
    assert len(lines) <= watcher.BODY_LINES
    assert lines[:4] == ["task: task_1", "dispatch: ctx_1", "terminal: term_1", "reason: terminal is gone"]
    assert lines[4] == "last screen lines:" and lines[-1] == "  line 39" and "line 29" not in body
    assert "  (the terminal cannot be read)" in watcher.death_body(finding(), "x", "")


def test_a_dead_worker_is_reported_once_after_two_scans_and_the_watch_ends(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": WORKING})
    Terminals(monkeypatch).kind["term_task_1"] = "gone"
    escalations = Escalations(monkeypatch)
    said: list[str] = []
    naps: list[float] = []
    assert watcher.watch(repo, RUN, interval_s=5, scans=3, sleep=naps.append, out=said.append) == 0
    assert len(escalations.sent) == 1
    sent = escalations.sent[0]
    assert sent["subject"] == "worker dead: Viec task_1" and sent["task_id"] == "task_1" and sent["dispatch"] == "ctx_task_1"
    assert "reason: terminal is gone" in sent["body"] and sent["run"] == RUN
    assert any("task_1 reported dead to the coordinator" in line for line in said)
    # A later watch, or another loop, never reports the same dispatch again.
    watcher.watch(repo, RUN, scans=3, sleep=lambda s: None, out=said.append)
    assert len(escalations.sent) == 1
    assert [record["kind"] for record in resume.history(repo) if record.get("kind") == watcher.DEATH] == ["death"]
    assert "reported" in [entry["kind"] for entry in map(json.loads, events.log_path(repo).read_text(encoding="utf-8").splitlines())
                          if entry["guard"] == "worker-watch"]


def test_one_odd_scan_is_not_a_death(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": IDLE})
    terminals = Terminals(monkeypatch)
    escalations = Escalations(monkeypatch)
    terminals.kind["term_task_1"] = "no-agent"

    def nap(seconds):
        terminals.kind["term_task_1"] = "alive"

    watcher.watch(repo, RUN, scans=3, sleep=nap, out=lambda line: None)
    assert escalations.sent == []


def test_a_report_orca_refused_is_sent_on_the_next_scan(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": BLOCKED})
    Terminals(monkeypatch)
    escalations = Escalations(monkeypatch)
    escalations.fail = True

    def nap(seconds):
        escalations.fail = False

    watcher.watch(repo, RUN, scans=3, sleep=nap, out=lambda line: None, confirm=1)
    assert len(escalations.sent) == 1 and "retry does not fix" in escalations.sent[0]["body"]
    assert [record["sent"] for record in resume.history(repo) if record.get("kind") == "death"] == [False, True]


def test_a_stuck_stall_is_reported_only_after_the_resumes_ran_out(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    Terminals(monkeypatch)
    escalations = Escalations(monkeypatch)
    step = [0]

    def nap(seconds):
        clock[0] += resume.RESUME_GAP_S
        step[0] += 1
        workers.screens["task_1"] = STALLED.replace("3h 5m", f"{step[0]}s")

    watcher.watch(repo, RUN, scans=resume.MAX_RESUMES + 3, sleep=nap, out=lambda line: None)
    assert len(workers.sent) == resume.MAX_RESUMES
    assert len(escalations.sent) == 1 and f"after {resume.MAX_RESUMES} resumes" in escalations.sent[0]["body"]


def test_a_silent_worker_is_reported_after_the_last_reminder(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})
    Terminals(monkeypatch)
    escalations = Escalations(monkeypatch)
    watcher.watch(repo, RUN, interval_s=resume.SILENT_AFTER_S, scans=6, sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                  out=lambda line: None)
    assert len(workers.sent) == resume.MAX_NUDGES
    assert len(escalations.sent) == 1 and "reminders, no worker_done" in escalations.sent[0]["body"]


def test_the_watch_ends_at_once_when_no_worker_is_unsettled(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": IDLE}, statuses={"task_1": "completed"})
    said: list[str] = []
    assert watcher.watch(repo, RUN, scans=5, sleep=lambda s: pytest.fail("slept"), out=said.append) == 0
    assert said == ["worker-watch: no unsettled worker left to watch"]
    assert not (repo / watcher.ALIVE_FILE).exists()


def test_worker_watch_escalates_through_orca_to_the_run(repo, tmp_path, monkeypatch, capsys):
    """End to end against the fake CLI: `terminal show` says the terminal is closed, and the report goes to `run:<id>`."""
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", RUN)
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-d1", spec="x", title="Sua parser")
    data = json.loads(state.read_text(encoding="utf-8"))
    data["tasks"][task_id]["status"] = "dispatched"
    data["dispatches"] = {"dsp_0001": {"task": task_id, "status": "ready"}}
    state.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("FAKE_ORCA_SCREEN", IDLE)
    monkeypatch.setenv("FAKE_ORCA_GONE_TERMINALS", f"term_{task_id}")
    monkeypatch.setattr(watcher.time, "sleep", lambda seconds: None)

    assert cli.main(["--root", str(repo), "worker-watch", "--interval", "1"]) == 0
    out = capsys.readouterr().out
    assert f"{task_id} reported dead to the coordinator: terminal is gone" in out and "no unsettled worker left" in out
    sent = json.loads(state.read_text(encoding="utf-8"))["orchestration_sent"]
    assert len(sent) == 1
    assert {key: sent[0][key] for key in ("to", "type", "subject", "task-id", "dispatch-id", "run")} == {
        "to": f"run:{RUN}", "type": "escalation", "subject": "worker dead: Sua parser", "task-id": task_id,
        "dispatch-id": "dsp_0001", "run": RUN}
    assert f"task: {task_id}\ndispatch: dsp_0001" in sent[0]["body"]


def test_the_death_report_is_not_listed_as_a_worker_question():
    report = {"id": "msg_d", "type": "escalation", "subject": "worker dead: Sua parser", "body": "task: task_1",
              "created_at": "2026-11-13T11:00:00Z", "payload": json.dumps({"taskId": "task_1", "dispatchId": "ctx_1"})}
    asked = {**report, "id": "msg_e", "subject": "Blocked: need a token"}
    assert [question["id"] for question in orca_cli.open_questions([report, asked], [])] == ["msg_e"]


def test_the_watcher_is_started_once_per_run_and_can_be_turned_off(repo, monkeypatch, clock):
    started: list[list[str]] = []

    class Process:
        pid = 4242

    def popen(argv, **kwargs):
        started.append(argv)
        return Process()

    monkeypatch.setattr(watcher.subprocess, "Popen", popen)
    assert watcher.ensure_running(repo, RUN) is None and started == []
    monkeypatch.setenv(watcher.WATCH_ENV, "on")
    assert watcher.ensure_running(repo, RUN) == "started"
    assert started[0][-4:] == ["--root", str(repo), "worker-watch", "--run", RUN][-4:] and "coding_agent.cli" in started[0]
    assert watcher.ensure_running(repo, RUN) == "running" and len(started) == 1
    clock[0] += watcher.STALE_AFTER_S
    assert watcher.ensure_running(repo, RUN) == "started" and len(started) == 2
    assert watcher.ensure_running(repo, "run_other") == "started"


def test_delegate_starts_the_watcher(repo, monkeypatch, capsys):
    calls: list[tuple] = []
    monkeypatch.setattr(orca_cli, "delegate", lambda root, **kwargs: ("task_9", "ctx_9"))
    monkeypatch.setattr(watcher, "ensure_running", lambda root, run=None, **kwargs: calls.append((root, run)) or "started")
    assert cli.main(["--root", str(repo), "delegate", "--title", "x", "--spec", "y", "--model", "sonnet", "--run", RUN]) == 0
    assert calls and calls[0][1] == RUN and "worker-watch: started in the background" in capsys.readouterr().err


# --- part 4: heartbeat-only wake-ups do not reach the coordinator's model -----------------------------------------------

def message(mid: str, kind: str, task: str = "task_1") -> dict:
    return {"id": mid, "type": kind, "subject": "alive" if kind == "heartbeat" else kind, "created_at": f"2026-11-13T11:0{mid[-1]}:00Z",
            "payload": json.dumps({"taskId": task, "dispatchId": f"ctx_{task}"})}


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    """The fake Orca with a Run mailbox; returns a function that sets the unread messages and one that reads the state."""
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))

    def put(*messages: dict) -> None:
        state.write_text(json.dumps({"tasks": {}, "messages": list(messages)}), encoding="utf-8")

    def unread() -> list[dict]:
        return json.loads(state.read_text(encoding="utf-8")).get("messages", [])

    return put, unread


def run_hook(repo: Path, monkeypatch, prompt: str, mode: str = "enforce") -> hooks.HookResult:
    monkeypatch.setenv("CODING_AGENT_MODE_COORDINATOR_HEARTBEAT", mode)
    payload = {"session_id": "c1", "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": prompt}
    return hooks.run("coordinator-heartbeat", payload, handlers.coordinator_heartbeat)


def test_a_heartbeat_only_wakeup_is_acked_and_blocked(repo, monkeypatch, mailbox):
    put, unread = mailbox
    put(message("m1", "heartbeat"), message("m2", "heartbeat", task="task_2"))
    result = run_hook(repo, monkeypatch, POINTER)
    assert result.code == 0 and json.loads(result.stdout) == {"decision": "block", "reason": "heartbeat only, acked (2)"}
    assert unread() == []


@pytest.mark.parametrize("other", ["worker_done", "question", "escalation", "status", "merge_ready"])
def test_a_batch_with_anything_but_heartbeats_passes_unchanged(repo, monkeypatch, mailbox, other):
    put, unread = mailbox
    put(message("m1", "heartbeat"), message("m2", other))
    result = run_hook(repo, monkeypatch, POINTER)
    assert result.stdout == "" and result.code == 0
    assert [item["id"] for item in unread()] == ["m1", "m2"] and not any(item.get("delivery") for item in unread())


def test_a_report_already_acked_per_task_counts_as_handled(repo, monkeypatch, mailbox):
    put, unread = mailbox
    put(message("m1", "heartbeat"), message("m2", "worker_done"))
    (repo / orca_cli.ACKED_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / orca_cli.ACKED_FILE).write_text(json.dumps({"key": "m2", "task_id": "task_1"}) + "\n", encoding="utf-8")
    assert json.loads(run_hook(repo, monkeypatch, POINTER).stdout)["decision"] == "block" and unread() == []


def test_the_hook_lets_everything_else_through(repo, monkeypatch, mailbox):
    put, unread = mailbox
    put(message("m1", "heartbeat"))
    # The user's own text typed together with Orca's line, a prompt of the user's, an empty mailbox, shadow mode.
    assert run_hook(repo, monkeypatch, "sua cai nay" + POINTER).stdout == ""
    assert run_hook(repo, monkeypatch, "tiến độ?").stdout == ""
    assert run_hook(repo, monkeypatch, POINTER, mode="shadow").stdout == "" and len(unread()) == 1
    put()
    assert run_hook(repo, monkeypatch, POINTER).stdout == ""
    kinds = [entry["kind"] for entry in map(json.loads, events.log_path(repo).read_text(encoding="utf-8").splitlines())
             if entry["guard"] == "coordinator-heartbeat"]
    assert kinds == ["would-block", "passed"]


def test_the_hook_fails_open_when_orca_cannot_be_read(repo, monkeypatch, mailbox):
    put, _ = mailbox
    put(message("m1", "heartbeat"))
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    result = run_hook(repo, monkeypatch, POINTER)
    assert result.stdout == "" and result.code == 0


def test_the_hook_leaves_a_worker_session_alone(repo, monkeypatch, mailbox):
    put, unread = mailbox
    put(message("m1", "heartbeat"))
    monkeypatch.setenv("CODING_AGENT_ROLE", "worker")
    assert run_hook(repo, monkeypatch, POINTER).stdout == "" and len(unread()) == 1


def test_the_hook_runs_from_the_shipped_manifest(repo, monkeypatch, mailbox):
    """End to end: the manifest declares the hook on UserPromptSubmit in enforce, and the generated command blocks."""
    from coding_agent import gen
    from coding_agent.manifest import load

    from test_hooks import call

    manifest = load(Path(__file__).resolve().parents[1] / "integration.yaml")
    hook = manifest.hook("coordinator-heartbeat")
    assert hook is not None and hook.event == "UserPromptSubmit" and hook.mode == "enforce"
    rendered = gen.render(manifest)["claude_code"]["hooks"]["UserPromptSubmit"]
    assert any("coordinator-heartbeat" in h["command"] for entry in rendered for h in entry["hooks"])
    put, unread = mailbox
    put(message("m1", "heartbeat"))
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    result = call(repo, "coordinator-heartbeat", {"session_id": "c2", "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": POINTER})
    assert result.returncode == 0 and json.loads(result.stdout)["decision"] == "block" and unread() == []
