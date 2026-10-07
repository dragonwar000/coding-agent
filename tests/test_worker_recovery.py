"""Recovering a worker Orca placed but did not see start: ok:true answers with exit 1, the kick-off, `worker-kick`, `worker-settle`, and the inbox hint."""

import json
import subprocess
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, events, orca_cli

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    for name in ("KICK_WAIT_S", "KICK_POLL_S", "KICK_SETTLE_S"):
        monkeypatch.setattr(orca_cli, name, 0)
    return state


def orca_state(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


@pytest.fixture
def committed(repo: Path) -> Path:
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def placed_worktree(repo: Path, tmp_path: Path, name: str) -> Path:
    """The worktree Orca creates for a worker-start whose answer carries no `effects`: on branch `name`, where the fake would put it."""
    path = tmp_path / "worktrees" / name
    path.parent.mkdir(exist_ok=True)
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    return path


def event_kinds(repo: Path) -> list[dict]:
    return [json.loads(line) for line in events.log_path(repo).read_text(encoding="utf-8").splitlines()]


# --- _run_orca --------------------------------------------------------------------------------------------------------

def test_an_ok_true_answer_is_returned_even_when_the_cli_exits_non_zero(repo, fake_orca, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_EXIT", "1")
    answer = orca_cli._call("task-list", "--run", "run_test")
    assert answer["ok"] is True and answer[orca_cli.EXIT_KEY] == 1
    assert orca_cli.list_tasks() == []


def test_an_ok_false_answer_is_still_an_error_when_the_cli_exits_non_zero(repo, fake_orca, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_EXIT", "1")
    with pytest.raises(orca_cli.OrcaError, match="ok:false"):
        orca_cli.set_status(repo, task_id="task_missing", requested="blocked", basis=None)


def test_a_clean_exit_adds_no_exit_key(repo, fake_orca):
    assert orca_cli.EXIT_KEY not in orca_cli._call("task-list", "--run", "run_test")


def test_turn_unobserved_reads_the_measured_shapes():
    assert orca_cli.turn_unobserved({"ok": True, "result": {"turnStart": "unobserved"}})
    assert orca_cli.turn_unobserved({"ok": True, "result": {"stage": "turn_start_unobserved"}})
    assert orca_cli.turn_unobserved({"ok": True, "result": {}, orca_cli.EXIT_KEY: 1})
    assert not orca_cli.turn_unobserved({"ok": True, "result": {"dispatchId": "ctx_1", "effects": []}})


# --- worker_start when the turn start was not observed -----------------------------------------------------------------

def test_worker_start_records_the_worktree_and_kicks_the_worker_when_the_turn_was_not_seen(committed, tmp_path, fake_orca, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_TURN_UNOBSERVED", "1")
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-k1", spec="x", title="Fix login bug")
    path = placed_worktree(committed, tmp_path, "fix-login-bug")

    dispatch = orca_cli.worker_start(committed, task_id=task_id, agent="claude")

    assert dispatch.startswith("dsp_")
    assert orca_cli.worker_worktrees(committed) == {str(path.resolve())}
    assert coordinator.role_for(path) == "worker"
    sent = orca_state(fake_orca)["sent"]
    assert len(sent) == 1
    assert sent[0]["terminal"] == f"term_{task_id}" and sent[0]["enter"] is True and sent[0]["wait_submit"] == "20"
    assert task_id in sent[0]["text"] and dispatch in sent[0]["text"] and f"dispatch-show --task {task_id} --preamble --json" in sent[0]["text"]
    assert ">" not in sent[0]["text"] and "\n" not in sent[0]["text"]
    log = event_kinds(committed)
    assert [e["kind"] for e in log[-2:]] == ["delegated", "kicked"]
    assert log[-2]["detail"]["turn_start"] == "unobserved" and Path(log[-2]["detail"]["worktree"]).resolve() == path.resolve()
    assert log[-1]["applied"] is True and log[-1]["detail"] == {"task_id": task_id, "dispatch": dispatch, "terminal": f"term_{task_id}", "attempts": 1, "seen": True, "error": None}


def test_worker_start_does_not_kick_when_the_turn_was_seen(committed, fake_orca):
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-k2", spec="x", title="x")
    orca_cli.worker_start(committed, task_id=task_id, agent="claude")
    assert "sent" not in orca_state(fake_orca)
    assert [e["kind"] for e in event_kinds(committed)] == ["delegated"]


def test_a_kick_the_worker_never_shows_is_resent_then_reported_without_raising(committed, tmp_path, fake_orca, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_ORCA_TURN_UNOBSERVED", "1")
    monkeypatch.setenv("FAKE_ORCA_DEAF", "1")
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-k3", spec="x", title="Deaf worker")
    placed_worktree(committed, tmp_path, "deaf-worker")

    dispatch = orca_cli.worker_start(committed, task_id=task_id, agent="claude")

    assert len(orca_state(fake_orca)["sent"]) == orca_cli.KICK_ATTEMPTS
    kicked = event_kinds(committed)[-1]
    assert kicked["kind"] == "kicked" and kicked["applied"] is False and kicked["detail"]["attempts"] == orca_cli.KICK_ATTEMPTS
    err = capsys.readouterr().err
    assert "may not have received its task" in err and f"orca terminal send --terminal term_{task_id} --enter --wait-submit 20 --text" in err
    assert dispatch in err


def test_find_worktree_reads_git_by_branch_then_by_directory_name(committed, tmp_path):
    by_branch = placed_worktree(committed, tmp_path, "on-branch")
    other = tmp_path / "worktrees" / "by-dir"
    git(committed, "worktree", "add", "-q", "-b", "some-other-branch", str(other))
    assert Path(orca_cli.find_worktree(committed, "on-branch")).resolve() == by_branch.resolve()
    assert Path(orca_cli.find_worktree(committed, "by-dir")).resolve() == other.resolve()
    assert orca_cli.find_worktree(committed, "nowhere") is None
    assert orca_cli.find_worktree(tmp_path / "not-git", "x") is None


def test_the_kickoff_text_is_one_line_without_a_redirect_character():
    text = orca_cli.kickoff_text("task_1", "ctx_1")
    assert "\n" not in text and ">" not in text
    assert text.startswith(orca_cli.KICK_MARK) and "REPORT-task_1.md" in text and "AskUserQuestion" in text


# --- worker-kick --------------------------------------------------------------------------------------------------------

def test_worker_kick_adopts_the_worktree_and_sends_the_kickoff(committed, tmp_path, fake_orca, capsys):
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-k4", spec="x", title="Kick me")
    orca_cli._call("worker-start", "--task", task_id, "--agent", "claude", "--worktree", "new-child", "--name", "kick-me", "--run", "run_test")
    path = placed_worktree(committed, tmp_path, "kick-me")
    assert coordinator.role_for(path) == "coordinator"

    assert cli.main(["--root", str(committed), "worker-kick", "--task-id", task_id]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["kicked"] is True and out["recorded"] is True and Path(out["worktree"]).resolve() == path.resolve()
    assert coordinator.role_for(path) == "worker"
    assert orca_state(fake_orca)["sent"][0]["terminal"] == f"term_{task_id}"
    assert cli.main(["--root", str(committed), "worker-kick", "--task-id", task_id]) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is False
    assert len(orca_cli.worker_records(committed)) == 1


def test_worker_kick_fails_clearly_for_a_task_without_a_dispatch(committed, fake_orca, capsys):
    assert cli.main(["--root", str(committed), "worker-kick", "--task-id", "task_nope"]) == 1
    assert "dispatch_not_found" in capsys.readouterr().err


# --- worker-settle ------------------------------------------------------------------------------------------------------

def test_settle_abandons_the_active_dispatch_then_completes_the_task(committed, fake_orca):
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-s1", spec="x", title="x")
    dispatch = orca_cli.worker_start(committed, task_id=task_id, agent="claude")

    outcome = orca_cli.settle(committed, task_id=task_id, basis="verifier", artifacts=("REPORT.md",))

    assert {k: v for k, v in outcome.items() if k != "reason"} == {"task_id": task_id, "dispatch": dispatch, "abandoned": True, "decision": "completed", "sent": True}
    state = orca_state(fake_orca)
    assert state["abandoned"] == [dispatch] and state["tasks"][task_id]["status"] == "completed"
    assert json.loads(state["tasks"][task_id]["result"]) == {"basis": "verifier", "artifacts": ["REPORT.md"]}
    assert event_kinds(committed)[-1]["kind"] == "settled"


def test_settle_leaves_a_settled_dispatch_alone(committed, fake_orca):
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-s2", spec="x", title="x")
    dispatch = orca_cli.worker_start(committed, task_id=task_id, agent="claude")
    orca_cli._call("worker-abandon", "--dispatch", dispatch)
    assert orca_cli.dispatch_active(dispatch) is False

    outcome = orca_cli.settle(committed, task_id=task_id, basis="predicate")

    assert outcome["abandoned"] is False and outcome["decision"] == "completed"
    assert orca_state(fake_orca)["abandoned"] == [dispatch]


def test_settle_without_a_dispatch_just_applies_the_status(committed, fake_orca):
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-s3", spec="x", title="x")
    outcome = orca_cli.settle(committed, task_id=task_id, basis="human")
    assert outcome["dispatch"] is None and outcome["abandoned"] is False and outcome["decision"] == "completed"


def test_the_settle_command_requires_proof(committed, fake_orca, capsys):
    task_id = orca_cli.create_task(committed, project="demo-repo", t_id="T-s4", spec="x", title="x")
    orca_cli.worker_start(committed, task_id=task_id, agent="claude")
    with pytest.raises(SystemExit):
        cli.main(["--root", str(committed), "worker-settle", "--task-id", task_id, "--basis", "agentReported"])
    assert "abandoned" not in orca_state(fake_orca)
    assert cli.main(["--root", str(committed), "worker-settle", "--task-id", task_id, "--basis", "verifier", "--artifact", "a.md"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["abandoned"] is True and out["decision"] == "completed"


# --- inbox hint ----------------------------------------------------------------------------------------------------------

def test_a_rejected_worker_done_gets_the_settle_hint_in_the_inbox(repo, fake_orca, capsys):
    data = orca_state(fake_orca) if fake_orca.exists() else {"tasks": {}}
    data["messages"] = [
        {"id": "m1", "type": "worker_done", "subject": "Rejected worker_done: capability missing", "body": "done",
         "payload": json.dumps({"taskId": "task_7", "dispatchId": "ctx_7", "outcome": "succeeded"})},
        {"id": "m2", "type": "worker_done", "subject": "DONE", "body": "done", "payload": json.dumps({"taskId": "task_8", "outcome": "succeeded"})},
    ]
    fake_orca.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["--root", str(repo), "inbox"]) == 0
    out = capsys.readouterr().out
    assert "task_7: succeeded — Rejected worker_done: capability missing" in out
    assert "worker-settle --task-id task_7 --basis verifier" in out
    assert "worker-settle --task-id task_8" not in out


def test_the_contract_names_both_recovery_commands():
    text = coordinator.contract("src")
    assert "worker-kick --task-id" in text and "worker-settle --task-id" in text and "Rejected worker_done" in text
