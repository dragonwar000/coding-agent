"""Workers start in the coordinator session's permission mode."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coding_agent import events, orca_cli, state
from coding_agent.hooks import handlers, run as run_hook

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


def _events(root: Path, guard: str) -> list[dict]:
    path = events.log_path(root)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [item for item in map(json.loads, lines) if item["guard"] == guard]


@pytest.fixture
def repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    monkeypatch.delenv(orca_cli.PERMISSION_ENV, raising=False)
    return repo


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state_path))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    monkeypatch.delenv("FAKE_ORCA_SCREEN", raising=False)
    monkeypatch.setattr(orca_cli, "AGENT_READY_POLL_S", 0)
    return state_path


def _task(repo: Path) -> str:
    return orca_cli.create_task(repo, project="demo-repo", t_id="T-pm", spec="x", title="Fix the thing")


def _orca(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


# --- precedence --------------------------------------------------------------------------------------------------


def test_cli_argument_wins_over_env_and_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state.save_permission_mode(repo, "plan", "s1")
    monkeypatch.setenv(orca_cli.PERMISSION_ENV, "acceptEdits")
    assert orca_cli.worker_permission_mode(repo, "bypassPermissions") == ("bypassPermissions", "cli")


def test_env_wins_over_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state.save_permission_mode(repo, "plan", "s1")
    monkeypatch.setenv(orca_cli.PERMISSION_ENV, "acceptEdits")
    assert orca_cli.worker_permission_mode(repo, None) == ("acceptEdits", "env")


def test_session_mode_is_the_fallback(repo: Path) -> None:
    state.save_permission_mode(repo, "bypassPermissions", "s1")
    assert orca_cli.worker_permission_mode(repo, None) == ("bypassPermissions", "session")


def test_nothing_recorded_keeps_the_old_behaviour(repo: Path) -> None:
    assert orca_cli.worker_permission_mode(repo, None) == (None, "none")


def test_unknown_value_is_skipped_and_logged(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state.save_permission_mode(repo, "plan", "s1")
    monkeypatch.setenv(orca_cli.PERMISSION_ENV, "yolo")
    assert orca_cli.worker_permission_mode(repo, "dontAsk") == ("plan", "session")
    ignored = [(e["detail"]["permission_mode"], e["detail"]["source"]) for e in _events(repo, "permission-mode") if e["kind"] == "ignored"]
    assert ignored == [("dontAsk", "cli"), ("yolo", "env")]


# --- recording from the hook payload -----------------------------------------------------------------------------


def _prompt(repo: Path, monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.setenv("CODING_AGENT_MODE_PROMPT_RESET", "shadow")
    monkeypatch.setenv("CODING_AGENT_ROLE", "coordinator")
    result = run_hook("prompt-reset", {"session_id": "sess-1", "cwd": str(repo), "prompt": "hi", **payload}, handlers.prompt_reset)
    assert result.code == 0


def test_prompt_reset_records_the_session_mode(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prompt(repo, monkeypatch, {"permission_mode": "bypassPermissions"})
    assert state.load(repo, "sess-1")["permission_mode"] == "bypassPermissions"
    stored = state.load_permission_mode(repo)
    assert stored["permission_mode"] == "bypassPermissions" and stored["session"] == "sess-1" and isinstance(stored["ts"], int)
    assert not [e for e in _events(repo, "prompt-reset") if e["kind"] == "hook-error"]


def test_latest_prompt_wins(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prompt(repo, monkeypatch, {"permission_mode": "bypassPermissions"})
    _prompt(repo, monkeypatch, {"permission_mode": "default"})
    assert state.load_permission_mode(repo)["permission_mode"] == "default"


def test_payload_without_mode_records_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prompt(repo, monkeypatch, {})
    assert state.load_permission_mode(repo) == {}
    assert "permission_mode" not in state.load(repo, "sess-1")


def test_unknown_payload_mode_is_ignored_and_logged(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prompt(repo, monkeypatch, {"permission_mode": "plan"})
    _prompt(repo, monkeypatch, {"permission_mode": "weird"})
    assert state.load_permission_mode(repo)["permission_mode"] == "plan"
    assert [e["detail"]["permission_mode"] for e in _events(repo, "permission-mode") if e["kind"] == "ignored"] == ["weird"]


# --- starting the worker -----------------------------------------------------------------------------------------


def test_claude_worker_gets_its_own_terminal_with_the_mode(repo: Path, fake_orca: Path) -> None:
    dispatch = orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="Fix the thing", permission_mode="bypassPermissions")
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] is None
    terminal = orca["created_terminals"][0]
    assert terminal["command"] == "claude --permission-mode bypassPermissions"
    assert terminal["worktree"] == orca["dispatches"][dispatch]["worktree"]
    assert orca["dispatches"][dispatch]["terminal"] == terminal["handle"] and orca["dispatches"][dispatch]["agent"] == "claude"
    started = [e for e in _events(repo, "permission-mode") if e["kind"] == "worker-started"]
    assert started[-1]["applied"] is True
    assert started[-1]["detail"]["permission_mode"] == "bypassPermissions" and started[-1]["detail"]["source"] == "cli"


def test_session_mode_reaches_the_worker(repo: Path, fake_orca: Path) -> None:
    state.save_permission_mode(repo, "acceptEdits", "coord")
    orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="x")
    assert _orca(fake_orca)["created_terminals"][0]["command"] == "claude --permission-mode acceptEdits"


def _child_repo(parent: Path) -> Path:
    """A repository nested inside the coordinator's project, like `platform/services/billing`."""
    child = parent / "platform" / "services" / "billing"
    child.mkdir(parents=True)
    for command in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(child), *command], check=True)
    return child


def test_root_inside_the_coordinator_project_reads_the_parent_record(repo: Path, fake_orca: Path) -> None:
    state.save_permission_mode(repo, "bypassPermissions", "coord")
    child = _child_repo(repo)
    assert orca_cli.worker_permission_mode(child, None) == ("bypassPermissions", "session")
    orca_cli.worker_start(child, task_id=_task(child), agent="claude", title="x")
    assert _orca(fake_orca)["created_terminals"][0]["command"] == "claude --permission-mode bypassPermissions"
    detail = [e for e in _events(child, "permission-mode") if e["kind"] == "worker-started"][-1]["detail"]
    assert detail["permission_mode"] == "bypassPermissions" and detail["source"] == "session"
    assert detail["origin"] == "parent" and detail["recorded_by"] == "coord"
    assert detail["state_dir"] == (repo.resolve() / ".coding-agent" / "state").as_posix()


def test_claude_project_dir_is_read_when_root_has_no_record(tmp_path: Path, repo: Path, fake_orca: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    state.save_permission_mode(other, "acceptEdits", "coord")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="x")
    assert _orca(fake_orca)["created_terminals"][0]["command"] == "claude --permission-mode acceptEdits"
    detail = [e for e in _events(repo, "permission-mode") if e["kind"] == "worker-started"][-1]["detail"]
    assert detail["source"] == "session" and detail["origin"] == "CLAUDE_PROJECT_DIR"
    assert detail["state_dir"] == (other.resolve() / ".coding-agent" / "state").as_posix()


def test_lookup_order_root_then_project_dir_then_cwd(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, cwd = tmp_path / "project", tmp_path / "cwd-project"
    project.mkdir(), cwd.mkdir()
    state.save_permission_mode(cwd, "plan", "c")
    monkeypatch.chdir(cwd)
    assert orca_cli.recorded_permission_mode(repo)[1]["origin"] == "cwd"
    state.save_permission_mode(project, "acceptEdits", "p")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert orca_cli.worker_permission_mode(repo, None) == ("acceptEdits", "session")
    state.save_permission_mode(repo, "default", "r")
    assert orca_cli.recorded_permission_mode(repo)[1]["origin"] == "root"


@pytest.mark.parametrize("mode", [None, "default"])
def test_default_or_no_mode_keeps_worktree_create_agent(repo: Path, fake_orca: Path, mode: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_SHARE_TRUST", "off")  # with sharing on, a Claude worker always gets its own terminal
    dispatch = orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="x", permission_mode=mode)
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] == "claude"
    assert "created_terminals" not in orca
    assert orca["dispatches"][dispatch]["terminal"] == "term_wt_x"
    started = [e for e in _events(repo, "permission-mode") if e["kind"] == "worker-started"]
    assert started[-1]["applied"] is False and started[-1]["detail"]["permission_mode"] == "agent-default"


def test_codex_keeps_its_start_and_logs_why(repo: Path, fake_orca: Path) -> None:
    orca_cli.worker_start(repo, task_id=_task(repo), agent="codex", title="x", permission_mode="bypassPermissions")
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] == "codex"
    assert "created_terminals" not in orca
    skipped = [e for e in _events(repo, "permission-mode") if e["kind"] == "not-applied"]
    assert skipped and skipped[0]["detail"]["agent"] == "codex" and "Claude Code" in skipped[0]["detail"]["reason"]


def test_bypass_confirmation_stops_the_dispatch(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = _task(repo)
    monkeypatch.setenv("FAKE_ORCA_SCREEN", "WARNING: Claude Code running in Bypass Permissions mode\n❯ 1. No, exit\n  2. Yes, I accept")
    with pytest.raises(orca_cli.OrcaError, match="Bypass Permissions"):
        orca_cli.worker_start(repo, task_id=task_id, agent="claude", title="x", permission_mode="bypassPermissions")
    orca = _orca(fake_orca)
    assert "dispatches" not in orca and "sent" not in orca
