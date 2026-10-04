"""The coordinator role (FR-019 to FR-021): roles per worktree, the guard, the contract and board injection, and delegation to workers."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, events, orca_cli
from coding_agent.hooks import context_for
from coding_agent.hooks import handlers
from test_hooks import call, manifest  # the hook subprocess helpers used by the hook tests

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    return state


def committed_repo(repo: Path) -> Path:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return repo


def linked_worktree(repo: Path, tmp_path: Path) -> Path:
    worktree = tmp_path / "worker-tree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "worker-branch", str(worktree)], check=True)
    return worktree


def test_the_main_worktree_is_the_coordinator_and_a_linked_one_is_a_worker(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    committed_repo(repo)
    worktree = linked_worktree(repo, tmp_path)
    assert coordinator.role_for(repo) == "coordinator"
    assert coordinator.role_for(worktree) == "worker"


def test_a_directory_outside_git_is_the_coordinator(tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    assert coordinator.role_for(tmp_path) == "coordinator"
    assert coordinator.role_for(None) == "coordinator"


def test_the_role_environment_variable_overrides_detection(repo, monkeypatch):
    committed_repo(repo)
    monkeypatch.setenv("CODING_AGENT_ROLE", "worker")
    assert coordinator.role_for(repo) == "worker"
    monkeypatch.setenv("CODING_AGENT_ROLE", "nonsense")
    assert coordinator.role_for(repo) == "coordinator"


@pytest.mark.parametrize("tool, tool_input, blocked", [
    ("Write", {"file_path": "a.py"}, True),
    ("Edit", {"file_path": "a.py"}, True),
    ("MultiEdit", {"file_path": "a.py"}, True),
    ("Bash", {"command": "sed -i 's/a/b/' a.py"}, True),
    ("Bash", {"command": "echo hi > out.txt"}, True),
    ("Bash", {"command": "git commit -m x"}, True),
    ("Bash", {"command": "rm -rf build"}, True),
    ("Bash", {"command": "pytest -q 2>&1"}, False),
    ("Bash", {"command": "git status && git log --oneline"}, False),
    ("Bash", {"command": "ls -la > /dev/null"}, False),
    ("Bash", {"command": "PYTHONPATH=harness/src python3 -m coding_agent.cli delegate --title t --spec 'x > y'"}, False),
    ("Bash", {"command": "python3 -m coding_agent.cli delegate --title t --spec x && rm -rf build"}, True),
    ("Read", {"file_path": "a.py"}, False),
])
def test_the_guard_reason_covers_writes_and_mutating_shell(tool, tool_input, blocked):
    assert (coordinator.guard_reason(tool, tool_input) is not None) is blocked


def test_the_coordinator_guard_blocks_a_write_in_enforce_and_lets_a_worker_write(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    worktree = linked_worktree(repo, tmp_path)
    payload = {"session_id": "c1", "cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": "a.py"}}
    blocked = call(repo, "coordinator-guard", payload, mode="enforce")
    assert blocked.returncode == 2 and "delegate" in blocked.stderr
    worker = call(repo, "coordinator-guard", {**payload, "cwd": str(worktree)}, mode="enforce")
    assert worker.returncode == 0


def test_the_coordinator_guard_in_shadow_nudges_and_never_blocks(repo, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-guard", {"session_id": "c2", "cwd": str(repo), "tool_name": "Edit", "tool_input": {}}, mode="shadow")
    assert result.returncode == 0
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "PreToolUse"
    assert "Quyết định là của coordinator" in body["additionalContext"]
    last = json.loads(events.log_path(repo).read_text(encoding="utf-8").splitlines()[-1])
    assert last["guard"] == "coordinator-guard" and last["kind"] == "nudged" and last["applied"] is False


def test_session_start_injects_the_contract_and_the_board(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    orca_cli.delegate(repo, title="Ship", spec="ship it", t_id="T-30", agent="claude")
    result = call(repo, "coordinator-context", {"session_id": "c3", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}, mode="shadow")
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "SessionStart"
    assert "Bạn là coordinator" in body["additionalContext"] and "delegate" in body["additionalContext"]
    assert "bảng việc hiện tại" in body["additionalContext"] and "Ship" in body["additionalContext"]


def test_each_prompt_gets_the_board_but_not_the_contract(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-board", {"session_id": "c4", "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "tiến độ?"}, mode="shadow")
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit"
    assert "Bạn là coordinator" not in body["additionalContext"]
    assert "bảng việc" in body["additionalContext"]


def test_a_worker_session_gets_no_coordinator_context(repo, tmp_path, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    worktree = linked_worktree(repo, tmp_path)
    result = call(repo, "coordinator-context", {"session_id": "c5", "cwd": str(worktree), "hook_event_name": "SessionStart"}, mode="shadow")
    assert result.returncode == 0 and result.stdout == ""


def test_an_unreadable_orca_is_reported_in_the_context_not_hidden(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-board", {"session_id": "c6", "cwd": str(repo), "hook_event_name": "UserPromptSubmit"}, mode="shadow")
    text = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "không đọc được từ Orca" in text and "run_required" in text


def test_delegate_creates_a_task_and_starts_a_worker_in_its_own_worktree(repo, fake_orca, monkeypatch, capsys):
    committed_repo(repo)
    task_id, dispatch = orca_cli.delegate(repo, title="Refactor", spec="split the module", t_id="T-31", agent="codex")
    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    assert state["dispatches"][dispatch] == {"task": task_id, "agent": "codex", "worktree": "new-child"}
    assert state["tasks"][task_id]["status"] == "dispatched"
    assert cli.main(["--root", str(repo), "delegate", "--title", "Docs", "--spec", "write docs", "--t-id", "T-32"]) == 0
    assert json.loads(capsys.readouterr().out)["dispatch"].startswith("dsp_")


def test_delegate_without_a_run_fails_clearly(repo, fake_orca, monkeypatch):
    committed_repo(repo)
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    with pytest.raises(orca_cli.OrcaError, match="run_required"):
        orca_cli.delegate(repo, title="x", spec="y", t_id="T-33", agent="claude")


def test_a_worker_start_answer_without_a_dispatch_id_is_an_error():
    with pytest.raises(orca_cli.OrcaError, match="no dispatch id"):
        orca_cli.dispatch_id_of({"ok": True, "result": {}})


def test_the_board_groups_only_this_repositorys_tasks():
    tasks = [
        {"id": "task_1", "status": "dispatched", "task_title": "Mine"},
        {"id": "task_2", "status": "dispatched", "task_title": "Theirs"},
    ]
    lines = coordinator.board_lines(tasks, {"task_1": "demo-repo", "task_2": "other"}, "demo-repo")
    assert any("task_1" in line for line in lines) and not any("task_2" in line for line in lines)
    assert coordinator.board_lines([], {}, "demo-repo") == ["bảng việc: chưa có task nào của repo này"]


def test_the_board_is_capped_so_it_cannot_flood_a_prompt():
    tasks = [{"id": f"task_{n}", "status": "dispatched", "task_title": f"job {n}"} for n in range(40)]
    lines = coordinator.board_lines(tasks, {f"task_{n}": "demo-repo" for n in range(40)}, "demo-repo")
    assert len(lines) == coordinator.BOARD_LIMIT + 1 and lines[-1] == "  ..."


def test_the_context_error_text_tells_the_coordinator_not_to_guess():
    text = coordinator.board_context([], "run_required")
    assert "Hỏi người dùng" in text
