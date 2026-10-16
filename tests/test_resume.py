"""Resuming workers that stopped on a transient API error and reminding silent ones: sorting a screen, the resume and the
reminder, their limits, the board, and `worker-resume`."""

import json
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, events, orca_cli, resume

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"

FOOTER = """
────────────────────────────────────────────────────────────────
❯\xa0
────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)"""

# The screen of the incident: the worker lost the network mid-task and sits at an empty prompt.
STALLED = """⏺ Bash(python3 -m pytest -q)
  ⎿  412 passed in 58.11s

⏺ API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)

⏺ API Error: Connection lost mid-response

✻ Worked for 3h 5m · done 9:24 PM
""" + FOOTER

WORKING = """⏺ API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)

❯ Mang da co lai. Ban bi dung vi loi API tam thoi.

⏺ Bash(git status)
  ⎿  Running…

✶ Ideating… (21s · ↓ 1.2k tokens · esc to interrupt)
""" + FOOTER

MOVED_ON = """⏺ API Error: Connection lost mid-response

✻ Worked for 12m 3s · done 9:24 PM

❯ lam tiep di

⏺ Da commit xong va gui worker_done.

✻ Worked for 2m 10s · done 9:40 PM
""" + FOOTER

BLOCKED = """⏺ API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}

✻ Worked for 4s · done 9:24 PM
""" + FOOTER

# The screens of 2026-10-15/16 (Bimgiamecco): the turn was cut after the e2e run passed, before the commit and worker_done.
CUT_STOPPED = """⏺ Bash(npm run test:e2e)
  ⎿  14 passed (1.2m)

⏺ API Error: The response stopped arriving. The response above may be incomplete.

✻ Worked for 47m 12s · done 11:02 PM
""" + FOOTER

CUT_SLEEP = """⏺ Bash(python3 -m pytest -q)
  ⎿  88 passed in 9.40s

API Error: Your computer went to sleep mid-response. The response above may be incomplete.

✻ Worked for 1h 2m · done 1:15 AM
""" + FOOTER

IDLE = """⏺ Da xong, cho lenh tiep theo.

✻ Worked for 4s · done 9:24 PM
""" + FOOTER


# --- classify ---------------------------------------------------------------------------------------------------------

def test_the_incident_screen_is_stalled_with_its_last_error_line():
    seen = resume.classify(STALLED)
    assert seen.state == "stalled" and seen.error == "API Error: Connection lost mid-response" and seen.fingerprint


@pytest.mark.parametrize("line", [
    "⏺ API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)",
    "⏺ API Error: Can’t reach the API server",
    "API Error: Connection lost mid-response",
    "● API Error: fetch failed (ECONNRESET)",
    "⏺ API Error: getaddrinfo EAI_AGAIN api.anthropic.com",
    "⏺ API Error: request ETIMEDOUT",
    '⏺ API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    "⏺ API Error: 503 Service Unavailable",
    "⏺ API Error: 502 Bad Gateway",
    '⏺ API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
    "  ⎿  API Error: Rate limit reached",
    # Turns cut off mid-stream (2026-10-15/16): the first two word for word from the workers' screens.
    "API Error: The response stopped arriving. The response above may be incomplete.",
    "⏺ API Error: The response stopped arriving. The response above may be incomplete.",
    "API Error: Your computer went to sleep mid-response. The response above may be incomplete.",
    "⏺ API Error: Your computer went to sleep mid-response. The response above may be incomplete.",
    "⏺ API Error: Request timed out.",
    "⏺ API Error: socket hang up",
    "⏺ API Error: Network error",
    "⏺ API Error: terminated",
    "⏺ API Error: TypeError: terminated",
    "⏺ API Error: stream ended unexpectedly",
    "⏺ API Error: Stream error: connection reset",
    "⏺ API Error: UND_ERR_SOCKET other side closed",
    "⏺ API Error: fetch failed (UND_ERR_CONNECT_TIMEOUT)",
    "⏺ API Error: Premature close",
])
def test_each_transient_error_line_is_stalled(line):
    assert resume.classify(line + "\n\n✻ Worked for 1m · done 9:24 PM\n" + FOOTER).state == "stalled"


@pytest.mark.parametrize("line", [
    '⏺ API Error: 401 {"type":"error","error":{"type":"authentication_error"}}',
    "⏺ API Error: Invalid API key · Please run /login",
    "⏺ Invalid API key · Please run /login",
    "⏺ Credit balance is too low",
    "⏺ API Error: Your credit balance is too low to access the API",
    "⏺ API Error: 400 input length and max_tokens exceed context length",
    "⏺ Prompt is too long",
    '⏺ API Error: 400 {"type":"error","error":{"type":"invalid_request_error"}}',
    # A permanent error stays permanent whatever turn-cutting words come with it.
    "⏺ API Error: 401 network error",
    "⏺ API Error: 403 Forbidden (socket hang up)",
    "⏺ API Error: Your credit balance is too low (stream ended)",
    "⏺ API Error: authentication failed: Request timed out",
    "⏺ API Error: 400 input length exceeds context length · Premature close",
    "⏺ API Error: The response stopped arriving · Please run /login",
    # The marks are tight: words that only look like them are an unknown error, and unknown is blocked.
    "⏺ API Error: Request was terminated by policy",
    "⏺ API Error: the session was terminated",
    "⏺ API Error: streaming is not supported for this model",
    "⏺ API Error: response stopped",
    "⏺ API Error: Your computer went to sleep",
    "⏺ API Error: UNDERR",
])
def test_an_error_a_retry_does_not_fix_is_blocked(line):
    assert resume.classify(line + "\n" + FOOTER).state == "blocked"


def test_the_screens_of_the_cut_turns_are_stalled_not_blocked():
    """2026-10-15/16: both screens were `blocked` for hours; a retry is what they needed."""
    stopped, slept = resume.classify(CUT_STOPPED), resume.classify(CUT_SLEEP)
    assert stopped.state == "stalled" and stopped.error == "API Error: The response stopped arriving. The response above may be incomplete."
    assert slept.state == "stalled" and slept.error.startswith("API Error: Your computer went to sleep mid-response.")


def test_a_cut_turn_quoted_by_a_tool_is_not_an_error():
    quoted = '⏺ Read(src/coding_agent/resume.py)\n  ⎿  r"response stopped arriving", r"socket hang up",\n' + FOOTER
    assert resume.classify(quoted).state == "idle"


def test_a_working_worker_is_not_stalled_even_with_an_error_on_screen():
    assert resume.classify(WORKING).state == "working"
    assert resume.classify(STALLED.replace("❯\xa0", "❯\xa0mot dong dang go")).state == "working"


def test_a_spinner_past_a_minute_or_without_a_timer_is_working():
    """Measured: `✽ Crystallizing… (6m 41s · ↓ 35.1k tokens)`, with no `esc to interrupt`."""
    resumed = STALLED.replace(FOOTER, "") + "❯ " + resume.resume_text("task_1") + "\n"
    assert resume.classify(resumed + "✽ Crystallizing… (6m 41s · ↓ 35.1k tokens)" + FOOTER).state == "working"
    assert resume.classify(resumed + "✻ Thinking…" + FOOTER).state == "working"
    assert not orca_cli._composer_idle("✽ Crystallizing… (1h 2m 3s · ↓ 35.1k tokens)" + FOOTER)
    assert orca_cli._composer_idle("✻ Worked for 3h 5m · done 9:24 PM" + FOOTER)


def test_a_worker_that_took_a_new_turn_after_the_error_is_idle():
    assert resume.classify(MOVED_ON).state == "idle"
    assert resume.classify(IDLE).state == "idle"


def test_a_worker_that_fails_again_after_a_resume_is_stalled_with_another_fingerprint():
    again = STALLED.replace(FOOTER, "") + "\n❯ " + resume.resume_text("task_1") + "\n\n⏺ API Error: fetch failed\n\n✻ Worked for 2s · done 9:31 PM\n" + FOOTER
    seen = resume.classify(again)
    assert seen.state == "stalled" and seen.error == "API Error: fetch failed"
    assert seen.fingerprint != resume.classify(STALLED).fingerprint


def test_an_error_older_than_the_window_or_printed_by_a_tool_is_ignored():
    noise = "\n".join(f"  line {n}" for n in range(resume.WINDOW_LINES + 1))
    assert resume.classify("⏺ API Error: fetch failed\n" + noise + "\n" + FOOTER).state == "idle"
    assert resume.classify('⏺ Read(tests/test_resume.py)\n  ⎿  STALLED = "⏺ API Error: fetch failed"\n     "API Error: 529"\n' + FOOTER).state == "idle"


def test_a_wrapped_error_line_is_read_with_its_continuation():
    wrapped = "⏺ API Error: Can't reach the API server — check your internet or DNS\n  (ENOTFOUND)\n" + FOOTER
    seen = resume.classify(wrapped)
    assert seen.state == "stalled" and seen.error.endswith("(ENOTFOUND)")


def test_an_empty_screen_is_unreadable():
    assert resume.classify("").state == "unreadable"


def test_the_resume_text_is_one_plain_line():
    text = resume.resume_text("task_1")
    assert text.isascii() and "\n" not in text and not set(text) & set('`$"\\>')
    assert text.startswith(resume.RESUME_MARK) and "task_1" in text and "worker_done" in text and "commit" in text


def test_the_api_host_comes_from_the_environment(monkeypatch):
    assert resume.api_endpoint() == ("api.anthropic.com", 443)
    monkeypatch.setenv("CODING_AGENT_API_HOST", "proxy.local:8443")
    assert resume.api_endpoint() == ("proxy.local", 8443)
    monkeypatch.setenv("CODING_AGENT_API_HOST", "example.test")
    assert resume.api_endpoint() == ("example.test", 443)


# --- scanning and resuming, with Orca patched ---------------------------------------------------------------------------

class Workers:
    """Stands in for Orca: tasks, one dispatch each, a screen per terminal, and the lines sent."""

    def __init__(self, repo: Path, monkeypatch: pytest.MonkeyPatch, screens: dict[str, str], statuses: dict[str, str] | None = None):
        self.screens = screens
        self.sent: list[tuple[str, str]] = []
        self.messages: list[dict[str, str]] = []
        self.reports: list[dict[str, str]] = []
        self.online = True
        self.tasks = [{"id": task_id, "status": (statuses or {}).get(task_id, "dispatched"), "task_title": f"Viec {task_id}"} for task_id in screens]
        for task_id in screens:
            orca_cli._append(repo / ".coding-agent" / "links.jsonl", {"t_id": f"T-{task_id}", "task_id": task_id, "project": "demo-repo"})
        monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: self.tasks)
        monkeypatch.setattr(orca_cli, "dispatch_of", lambda task_id: {"id": f"ctx_{task_id}", "status": "ready", "assignee_handle": f"term_{task_id}"})
        monkeypatch.setattr(orca_cli, "screen_of", lambda handle: self.screens[handle.removeprefix("term_")])
        monkeypatch.setattr(orca_cli, "send_line", lambda handle, text: self.sent.append((handle, text)))
        monkeypatch.setattr(resume, "network_up", lambda timeout_s=0.0: self.online)
        monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: self.reports)

        def message(dispatch, *, subject, body, task_id, run=None):
            self.messages.append({"dispatch": dispatch, "subject": subject, "body": body, "task_id": task_id})

        monkeypatch.setattr(orca_cli, "send_message", message)


def kinds(repo: Path) -> list[str]:
    path = events.log_path(repo)
    logged = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [entry["kind"] for entry in logged if entry["guard"] == "worker-resume"]


def test_only_the_stalled_worker_is_resumed(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": WORKING, "task_3": MOVED_ON, "task_4": BLOCKED, "task_5": STALLED},
                      statuses={"task_5": "completed"})
    found = {item.task_id: item for item in resume.scan(repo)}
    assert workers.sent == [("term_task_1", resume.resume_text("task_1"))]
    assert {task: item.state for task, item in found.items()} == {"task_1": "stalled", "task_2": "working", "task_3": "idle", "task_4": "blocked"}
    assert found["task_1"].action == "resumed" and found["task_1"].attempt == 1 and found["task_4"].action == "blocked"
    record = resume.history(repo)[-1]
    assert record["dispatch"] == "ctx_task_1" and record["sent"] is True and record["fingerprint"] == found["task_1"].fingerprint
    assert kinds(repo) == ["resumed"]


def test_a_task_of_another_coordinator_is_left_alone_unless_named(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    (repo / ".coding-agent" / "links.jsonl").unlink()
    assert resume.scan(repo) == [] and workers.sent == []
    assert [item.action for item in resume.scan(repo, task_ids=["task_1"])] == ["resumed"]


def test_the_same_screen_is_never_resumed_twice(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    assert [item.action for item in resume.scan(repo)] == ["resumed"]
    assert [item.action for item in resume.scan(repo)] == ["already-resumed"]
    assert len(workers.sent) == 1


def test_a_new_stall_waits_for_the_gap_and_stops_at_the_limit(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    clock = [1000.0]
    monkeypatch.setattr(resume.time, "time", lambda: clock[0])
    actions = []
    for n in range(resume.MAX_RESUMES + 1):
        workers.screens["task_1"] = STALLED.replace("3h 5m", f"{n}s")
        actions.append(resume.scan(repo)[0].action)
        if n == 0:
            workers.screens["task_1"] = STALLED.replace("3h 5m", "again")
            assert resume.scan(repo)[0].action == "cooldown"
        clock[0] += resume.RESUME_GAP_S
    assert actions == ["resumed"] * resume.MAX_RESUMES + ["limit"]
    assert len(workers.sent) == resume.MAX_RESUMES


def test_nothing_is_sent_while_the_network_is_down(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    workers.online = False
    assert [item.action for item in resume.scan(repo)] == ["waiting-network"]
    assert workers.sent == [] and resume.history(repo) == [] and kinds(repo) == []
    workers.online = True
    assert [item.action for item in resume.scan(repo)] == ["resumed"]
    assert len(workers.sent) == 1


def test_a_scan_out_of_time_defers_the_rest_to_the_next_scan(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": STALLED})
    found = resume.resume_stalled(repo, resume.stalled_workers(repo), budget_s=0)
    assert [item.action for item in found] == ["deferred", "deferred"] and workers.sent == []
    assert "lượt quét sau sẽ gửi" in resume.board_notes(found)["task_1"]
    assert [item.action for item in resume.scan(repo)] == ["resumed", "resumed"]


def test_a_send_orca_refuses_is_recorded_and_tried_again(repo, monkeypatch):
    Workers(repo, monkeypatch, {"task_1": STALLED})

    def refuse(handle, text):
        raise orca_cli.OrcaError("terminal gone")

    monkeypatch.setattr(orca_cli, "send_line", refuse)
    assert [item.action for item in resume.scan(repo)] == ["send-failed"]
    assert [item.action for item in resume.scan(repo)] == ["send-failed"]
    assert kinds(repo) == ["resume-failed", "resume-failed"] and [record["sent"] for record in resume.history(repo)] == [False, False]


# --- the board ---------------------------------------------------------------------------------------------------------

def test_the_board_resumes_and_lists_stalled_workers_apart_from_running_ones(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": WORKING, "task_4": BLOCKED})
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    lines, error = coordinator.read_board(repo, run="run_test")
    assert error is None and len(workers.sent) == 1
    text = "\n".join(lines)
    assert lines[0] == "worker bị dừng vì lỗi API/mạng, không còn chạy (2):"
    assert f"task_1 [dispatched] Viec task_1: đã tự resume (lần 1/{resume.MAX_RESUMES}) — API Error: Connection lost mid-response" in text
    assert "task_4 [dispatched] Viec task_4: bị chặn, cần người xử lý: lỗi không tạm thời" in text
    assert "đang chạy (1):\n  - task_2 [dispatched]" in text
    assert kinds(repo) == ["resumed", "blocked"]


def test_the_board_scan_is_skipped_within_the_gap_but_keeps_its_findings(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    workers.online = False
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    first, _ = coordinator.read_board(repo, run="run_test")
    assert "đang chờ mạng" in first[1]
    workers.online = True
    again, _ = coordinator.read_board(repo, run="run_test")
    assert again[:2] == first[:2] and workers.sent == [] and kinds(repo) == ["waiting-network"]
    saved = json.loads((repo / resume.SCAN_FILE).read_text(encoding="utf-8"))
    (repo / resume.SCAN_FILE).write_text(json.dumps({**saved, "ts": saved["ts"] - resume.SCAN_GAP_S}), encoding="utf-8")
    later, _ = coordinator.read_board(repo, run="run_test")
    assert "đã tự resume" in later[1] and len(workers.sent) == 1


def test_auto_resume_off_or_broken_never_breaks_the_board(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED})
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    monkeypatch.setenv("CODING_AGENT_AUTO_RESUME", "off")
    lines, error = coordinator.read_board(repo, run="run_test")
    assert error is None and lines[0] == "đang chạy (1):" and workers.sent == []
    monkeypatch.delenv("CODING_AGENT_AUTO_RESUME")

    def broken(handle):
        raise RuntimeError("boom")

    monkeypatch.setattr(orca_cli, "screen_of", broken)
    lines, error = coordinator.read_board(repo, run="run_test")
    assert error is None and lines[0] == "đang chạy (1):" and kinds(repo) == ["error"]


def test_the_contract_names_the_resume_and_the_watch_command():
    assert "worker-resume --watch" in coordinator.contract("src")


# --- the CLI -----------------------------------------------------------------------------------------------------------

def test_worker_resume_dry_run_prints_and_sends_nothing(repo, monkeypatch, capsys):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": WORKING, "task_4": BLOCKED})
    assert cli.main(["--root", str(repo), "worker-resume", "--dry-run", "--run", "run_test"]) == 0
    out = capsys.readouterr().out
    assert "worker-resume: task_1 stalled, would be resumed (dry run, nothing sent): API Error: Connection lost mid-response" in out
    assert "worker-resume: task_4 BLOCKED" in out and "task_2" not in out
    assert workers.sent == [] and resume.history(repo) == []


def test_worker_resume_names_one_task_and_exits_zero_with_nothing_to_do(repo, monkeypatch, capsys):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": WORKING, "task_3": STALLED})
    assert cli.main(["--root", str(repo), "worker-resume", "--task-id", "task_2", "--run", "run_test"]) == 0
    assert "worker-resume: no stalled worker (1 worker(s) read)" in capsys.readouterr().out and workers.sent == []
    assert cli.main(["--root", str(repo), "worker-resume", "--task-id", "task_3", "--run", "run_test"]) == 0
    assert f"task_3 stalled, resumed (attempt 1/{resume.MAX_RESUMES})" in capsys.readouterr().out
    assert [handle for handle, _ in workers.sent] == ["term_task_3"]


def test_watch_resumes_when_the_network_returns_and_prints_only_changes(repo, monkeypatch):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": WORKING})
    workers.online = False
    said: list[str] = []
    naps: list[float] = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            workers.online = True
        if len(naps) == 4:
            workers.screens["task_1"] = WORKING

    resume.watch(repo, "run_test", interval_s=7, scans=5, sleep=nap, out=said.append)
    assert naps == [7] * 4 and len(workers.sent) == 1
    assert [line.split(" ", 2)[2].split(",")[0].split(":")[0] for line in said] == ["stalled", "stalled", "stalled", "working again"]
    assert "waiting for the network (api.anthropic.com:443" in said[0] and "resumed (attempt 1/" in said[1] and "needs a person" in said[2]


def test_worker_resume_sends_through_orca_terminal_send(repo, tmp_path, monkeypatch, capsys):
    """End to end against the fake CLI: the send carries the worker's terminal, Enter and `--wait-submit`."""
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-r1", spec="x", title="Mat mang")
    data = json.loads(state.read_text(encoding="utf-8"))
    data["tasks"][task_id]["status"] = "dispatched"
    data["dispatches"] = {"dsp_0001": {"task": task_id, "status": "ready"}}
    state.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("FAKE_ORCA_SCREEN", STALLED)
    monkeypatch.setattr(resume, "network_up", lambda timeout_s=0.0: True)

    assert cli.main(["--root", str(repo), "worker-resume"]) == 0
    assert f"{task_id} stalled, resumed" in capsys.readouterr().out
    sent = json.loads(state.read_text(encoding="utf-8"))["sent"]
    assert sent == [{"terminal": f"term_{task_id}", "text": resume.resume_text(task_id), "enter": True, "wait_submit": "20"}]


# --- reminding silent workers ------------------------------------------------------------------------------------------

AFTER = resume.SILENT_AFTER_S

# A worker that answered the reminder with one more turn and went quiet again without sending worker_done.
ANSWERED = """⏺ Da xong, cho lenh tiep theo.

❯ Nhac viec: task task_1 chua co worker_done ma terminal da im.

⏺ Con thieu mot buoc, dang lam tiep. Xong roi.

✻ Worked for 1m 2s · done 9:50 PM
""" + FOOTER


@pytest.fixture
def clock(monkeypatch):
    """`time.time` under the test's control, so no test sleeps."""
    now = [100_000.0]
    monkeypatch.setattr(resume.time, "time", lambda: now[0])
    return now


def test_a_worker_idle_past_the_threshold_without_a_report_is_reminded_twice_at_most(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})
    inbox_reads = []
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: inbox_reads.append(run) or [])
    assert [(item.state, item.action) for item in resume.scan(repo)] == [("idle", "")]
    clock[0] += AFTER - 1
    assert [(item.state, item.idle_s) for item in resume.scan(repo)] == [("idle", AFTER - 1)]
    assert inbox_reads == [] and workers.sent == []

    clock[0] += 1
    first = resume.scan(repo)[0]
    assert (first.state, first.action, first.attempt, first.idle_s) == ("silent", "nudged", 1, AFTER)
    assert workers.messages == [{"dispatch": "ctx_task_1", "subject": "Nhac viec: chua co worker_done", "body": resume.nudge_text("task_1"),
                                 "task_id": "task_1"}]
    assert workers.sent == [("term_task_1", resume.nudge_text("task_1"))]
    record = resume.history(repo)[-1]
    assert (record["kind"], record["dispatch"], record["sent"], record["message"], record["idle_s"]) == ("nudge", "ctx_task_1", True, True, AFTER)
    assert kinds(repo) == ["nudged"]

    workers.screens["task_1"] = ANSWERED
    clock[0] += 60
    again = resume.scan(repo)[0]
    assert (again.state, again.action, again.attempt) == ("silent", "nudge-wait", 1) and len(workers.sent) == 1
    clock[0] += AFTER
    assert [(item.action, item.attempt) for item in resume.scan(repo)] == [("nudged", 2)]
    clock[0] += 10 * AFTER
    assert [(item.action, item.attempt) for item in resume.scan(repo)] == [("nudge-limit", 2)]
    assert len(workers.sent) == 2 and len(workers.messages) == 2


def test_the_nudge_text_is_one_plain_line_that_asks_for_the_report():
    text = resume.nudge_text("task_1")
    assert text.isascii() and "\n" not in text and not set(text) & set('`$"\\>')
    assert text.startswith(resume.NUDGE_MARK) and "task_1" in text and "commit" in text and "worker_done" in text and "lam tiep" in text


def test_a_worker_that_reported_is_not_reminded(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": IDLE})
    workers.reports = [{"task_id": "task_1", "dispatch": "ctx_task_1", "type": "worker_done", "outcome": "succeeded"},
                       {"task_id": "task_2", "dispatch": "ctx_old_attempt", "type": "worker_done", "outcome": "failed"}]
    resume.scan(repo)
    clock[0] += AFTER
    found = {item.task_id: item for item in resume.scan(repo)}
    assert found["task_1"].state == "idle" and found["task_1"].action == ""
    assert found["task_2"].action == "nudged" and [handle for handle, _ in workers.sent] == ["term_task_2"]


def test_only_a_dispatched_task_with_an_unchanged_idle_screen_is_silent(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": IDLE, "task_3": WORKING}, statuses={"task_2": "ready"})
    resume.scan(repo)
    clock[0] += AFTER - 60
    workers.screens["task_1"] = MOVED_ON
    workers.screens["task_3"] = IDLE
    resume.scan(repo)
    clock[0] += 60
    found = {item.task_id: (item.state, item.idle_s) for item in resume.scan(repo)}
    assert found == {"task_1": ("idle", 60), "task_2": ("idle", AFTER), "task_3": ("idle", 60)}
    assert workers.sent == [] and workers.messages == []


def test_an_unreadable_screen_keeps_the_clock_and_a_working_one_resets_it(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})
    resume.scan(repo)
    clock[0] += AFTER / 2
    workers.screens["task_1"] = ""
    resume.scan(repo)
    clock[0] += AFTER / 2
    workers.screens["task_1"] = IDLE
    assert resume.scan(repo)[0].action == "nudged"
    workers.screens["task_1"] = WORKING
    resume.scan(repo)
    workers.screens["task_1"] = IDLE
    clock[0] += 10 * AFTER
    assert [(item.action, item.idle_s) for item in resume.scan(repo)] == [("nudge-wait", 0)]
    clock[0] += AFTER
    assert [(item.action, item.attempt) for item in resume.scan(repo)] == [("nudged", 2)]


def test_the_threshold_comes_from_the_environment(repo, monkeypatch, clock):
    assert resume.silent_after_s() == 600
    monkeypatch.setenv("CODING_AGENT_SILENT_MINUTES", "2.5")
    assert resume.silent_after_s() == 150
    monkeypatch.setenv("CODING_AGENT_SILENT_MINUTES", "nonsense")
    assert resume.silent_after_s() == 600
    monkeypatch.setenv("CODING_AGENT_SILENT_MINUTES", "1")
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})
    resume.scan(repo)
    clock[0] += 60
    assert resume.scan(repo)[0].action == "nudged"
    for off in ("off", "0"):
        monkeypatch.setenv("CODING_AGENT_SILENT_MINUTES", off)
        assert resume.silent_after_s() is None
        clock[0] += 100 * AFTER
        assert [(item.state, item.action) for item in resume.scan(repo)] == [("idle", "")]
    assert len(workers.sent) == 1


def test_reminders_and_resumes_have_their_own_limits(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})
    for _ in range(resume.MAX_NUDGES):
        resume.scan(repo)
        clock[0] += AFTER
        workers.screens["task_1"] += " "
        resume.scan(repo)
        clock[0] += AFTER
        workers.screens["task_1"] = IDLE + "\n⏺ lan " + str(clock[0]) + "\n" + FOOTER
    workers.screens["task_1"] = STALLED
    assert [(item.action, item.attempt) for item in resume.scan(repo)] == [("resumed", 1)]
    assert [record["kind"] for record in resume.history(repo)] == ["nudge"] * resume.MAX_NUDGES + ["resume"]


def test_a_reminder_goes_to_the_terminal_even_when_orca_refuses_the_message(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})

    def refuse(*args, **kwargs):
        raise orca_cli.OrcaError("consumer_fenced")

    monkeypatch.setattr(orca_cli, "send_message", refuse)
    resume.scan(repo)
    clock[0] += AFTER
    assert [(item.action, item.attempt) for item in resume.scan(repo)] == [("nudged", 1)] and len(workers.sent) == 1
    assert resume.history(repo)[-1]["message"] is False

    monkeypatch.setattr(orca_cli, "send_line", refuse)
    workers.screens["task_1"] = ANSWERED
    resume.scan(repo)
    clock[0] += AFTER
    assert [(item.action, item.attempt) for item in resume.scan(repo)] == [("nudge-failed", 1)]
    assert kinds(repo) == ["nudged", "nudge-failed"]


def test_nothing_is_reminded_while_the_inbox_cannot_be_read(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})

    def unreadable(run=None):
        raise orca_cli.OrcaError("orca check exited 3")

    monkeypatch.setattr(orca_cli, "unread_reports", unreadable)
    resume.scan(repo)
    clock[0] += AFTER
    assert [(item.state, item.action) for item in resume.scan(repo)] == [("silent", "inbox-unreadable")] and workers.sent == []


def test_the_board_lists_silent_workers_apart_from_api_errors_with_their_reminders(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": IDLE, "task_3": WORKING})
    first, _ = coordinator.read_board(repo, run="run_test")
    assert not any("im lặng" in line for line in first)
    clock[0] += AFTER
    lines, error = coordinator.read_board(repo, run="run_test")
    text = "\n".join(lines)
    assert error is None and workers.sent[-1] == ("term_task_2", resume.nudge_text("task_2"))
    assert lines[0] == "worker bị dừng vì lỗi API/mạng, không còn chạy (1):" and "task_2" not in lines[1]
    assert "worker im lặng, chưa báo (1):\n  - task_2 [dispatched] Viec task_2: im 10 phút, chưa gửi worker_done, đã nhắc 1/2 lần: vừa tự nhắc" in text
    assert "đang chạy (1):\n  - task_3 [dispatched]" in text
    clock[0] += 10 * AFTER
    later, _ = coordinator.read_board(repo, run="run_test")
    assert "đã nhắc 1/2 lần: vừa tự nhắc" not in "\n".join(later)


def test_worker_resume_dry_run_lists_stalled_and_silent_workers(repo, monkeypatch, capsys, clock):
    workers = Workers(repo, monkeypatch, {"task_1": STALLED, "task_2": IDLE, "task_3": WORKING})
    assert cli.main(["--root", str(repo), "worker-resume", "--dry-run", "--run", "run_test"]) == 0
    assert "task_2" not in capsys.readouterr().out
    clock[0] += AFTER
    assert cli.main(["--root", str(repo), "worker-resume", "--dry-run", "--run", "run_test"]) == 0
    out = capsys.readouterr().out
    assert "worker-resume: task_1 stalled, would be resumed (dry run, nothing sent)" in out
    assert "worker-resume: task_2 silent for 10 min without worker_done, would be reminded (dry run, nothing sent)" in out
    assert "task_3" not in out and workers.sent == [] and workers.messages == [] and resume.history(repo) == []


def test_watch_reminds_a_worker_that_stays_silent_and_prints_only_changes(repo, monkeypatch, clock):
    workers = Workers(repo, monkeypatch, {"task_1": IDLE})
    said: list[str] = []
    resume.watch(repo, "run_test", interval_s=AFTER, scans=6, sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds), out=said.append)
    assert len(workers.sent) == resume.MAX_NUDGES
    assert said == ["worker-resume: task_1 silent for 10 min without worker_done, reminded (reminder 1/2)",
                    "worker-resume: task_1 silent for 20 min without worker_done, reminded (reminder 2/2)",
                    "worker-resume: task_1 silent for 30 min without worker_done, 2 reminders sent already: needs a person"]


def test_a_reminder_goes_through_orca_orchestration_send(repo, tmp_path, monkeypatch, capsys, clock):
    """End to end against the fake CLI: the message is addressed to the dispatch and carries task and dispatch ids."""
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-n1", spec="x", title="Im lang")
    data = json.loads(state.read_text(encoding="utf-8"))
    data["tasks"][task_id]["status"] = "dispatched"
    data["dispatches"] = {"dsp_0001": {"task": task_id, "status": "ready"}}
    state.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("FAKE_ORCA_SCREEN", IDLE)

    assert cli.main(["--root", str(repo), "worker-resume"]) == 0
    clock[0] += AFTER
    assert cli.main(["--root", str(repo), "worker-resume"]) == 0
    assert f"{task_id} silent for 10 min without worker_done, reminded (reminder 1/2)" in capsys.readouterr().out
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["orchestration_sent"] == [{"to": "dispatch:dsp_0001", "type": "status", "subject": "Nhac viec: chua co worker_done",
                                           "body": resume.nudge_text(task_id), "task-id": task_id, "dispatch-id": "dsp_0001", "run": "run_test"}]
    assert data["sent"] == [{"terminal": f"term_{task_id}", "text": resume.nudge_text(task_id), "enter": True, "wait_submit": "20"}]
