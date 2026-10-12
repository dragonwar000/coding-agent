"""Resuming workers that stopped on a transient API error: sorting a screen, the resume itself, its limits, the board, and `worker-resume`."""

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
])
def test_an_error_a_retry_does_not_fix_is_blocked(line):
    assert resume.classify(line + "\n" + FOOTER).state == "blocked"


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
        self.online = True
        self.tasks = [{"id": task_id, "status": (statuses or {}).get(task_id, "dispatched"), "task_title": f"Viec {task_id}"} for task_id in screens]
        for task_id in screens:
            orca_cli._append(repo / ".coding-agent" / "links.jsonl", {"t_id": f"T-{task_id}", "task_id": task_id, "project": "demo-repo"})
        monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: self.tasks)
        monkeypatch.setattr(orca_cli, "dispatch_of", lambda task_id: {"id": f"ctx_{task_id}", "status": "ready", "assignee_handle": f"term_{task_id}"})
        monkeypatch.setattr(orca_cli, "screen_of", lambda handle: self.screens[handle.removeprefix("term_")])
        monkeypatch.setattr(orca_cli, "send_line", lambda handle, text: self.sent.append((handle, text)))
        monkeypatch.setattr(resume, "network_up", lambda timeout_s=0.0: self.online)


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
