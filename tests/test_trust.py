"""Workers share the coordinator's Claude Code folder trust. Every test uses a temporary CLAUDE_CONFIG_DIR (conftest)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coding_agent import events, orca_cli, trust

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


def _config(config_dir: Path, projects: dict, **extra: object) -> Path:
    path = config_dir / ".claude.json"
    path.write_text(json.dumps({"numStartups": 3, **extra, "projects": projects}, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _key(path: Path) -> str:
    return trust.config_key(path)


def _events(root: Path) -> list[dict]:
    path = events.log_path(root)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [item for item in map(json.loads, lines) if item["guard"] == "folder-trust"]


def test_config_path_follows_claude_config_dir(claude_config_dir: Path) -> None:
    assert trust.config_path() == claude_config_dir / ".claude.json"
    (claude_config_dir / ".config.json").write_text("{}", encoding="utf-8")
    assert trust.config_path() == claude_config_dir / ".config.json"


def test_config_path_never_points_home_in_tests(claude_config_dir: Path) -> None:
    assert trust.config_path() != Path.home() / ".claude.json"


# --- is_trusted --------------------------------------------------------------------------------------------------


def test_nearer_false_entry_beats_a_trusted_parent(tmp_path: Path, claude_config_dir: Path) -> None:
    parent = tmp_path / "poc"
    blocked = parent / "c-core-platform"
    repo = blocked / "micro-service" / "pm"
    repo.mkdir(parents=True)
    _config(claude_config_dir, {_key(parent): {"hasTrustDialogAccepted": True}, _key(blocked): {"hasTrustDialogAccepted": False}})
    assert trust.is_trusted(parent)
    assert trust.is_trusted(parent / "other")
    assert not trust.is_trusted(blocked)
    assert not trust.is_trusted(repo)


def test_entry_without_the_field_does_not_decide(tmp_path: Path, claude_config_dir: Path) -> None:
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    _config(claude_config_dir, {_key(tmp_path / "a"): {"hasTrustDialogAccepted": True}, _key(child): {"allowedTools": []}})
    assert trust.is_trusted(child)


def test_nothing_recorded_is_untrusted(tmp_path: Path, claude_config_dir: Path) -> None:
    _config(claude_config_dir, {})
    assert not trust.is_trusted(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows keys")
def test_windows_keys_match_backslashes_and_case(tmp_path: Path, claude_config_dir: Path) -> None:
    _config(claude_config_dir, {str(tmp_path.resolve()).upper(): {"hasTrustDialogAccepted": True}})
    assert trust.is_trusted(tmp_path / "x")


# --- share_trust -------------------------------------------------------------------------------------------------


def test_share_creates_entries_and_keeps_other_keys(tmp_path: Path, claude_config_dir: Path) -> None:
    coordinator = tmp_path / "repo"
    worktree = tmp_path / "wt"
    coordinator.mkdir()
    worktree.mkdir()
    config = _config(claude_config_dir,
                     {_key(coordinator): {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]},
                      _key(worktree): {"hasTrustDialogAccepted": False, "lastCost": 1.5, "note": "đã xem"}},
                     oauthAccount={"emailAddress": "x"})
    written = trust.share_trust(coordinator, [coordinator, worktree, tmp_path / "new"], "s1")
    assert written == [_key(worktree), _key(tmp_path / "new")]
    text = config.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["numStartups"] == 3 and data["oauthAccount"] == {"emailAddress": "x"}
    assert data["projects"][_key(coordinator)] == {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]}
    assert data["projects"][_key(worktree)] == {"hasTrustDialogAccepted": True, "lastCost": 1.5, "note": "đã xem"}
    assert data["projects"][_key(tmp_path / "new")]["hasTrustDialogAccepted"] is True
    assert data["projects"][_key(tmp_path / "new")]["allowedTools"] == []
    assert "đã xem" in text and text.startswith('{\n  "numStartups"')
    assert not [p for p in claude_config_dir.iterdir() if p.name.endswith(".tmp")]
    shared = [e for e in _events(coordinator) if e["kind"] == "shared"]
    assert shared and shared[0]["applied"] is True and _key(worktree) in shared[0]["detail"]["paths"]


def test_untrusted_coordinator_writes_nothing(tmp_path: Path, claude_config_dir: Path) -> None:
    coordinator = tmp_path / "repo"
    coordinator.mkdir()
    config = _config(claude_config_dir, {_key(coordinator): {"hasTrustDialogAccepted": False}})
    before = config.read_bytes()
    assert trust.share_trust(coordinator, [tmp_path / "wt"], "s1") == []
    assert config.read_bytes() == before
    skipped = [e for e in _events(coordinator) if e["kind"] == "skipped"]
    assert skipped and "not trusted" in skipped[0]["detail"]["reason"]


def test_broken_json_writes_nothing_and_says_why(tmp_path: Path, claude_config_dir: Path) -> None:
    coordinator = tmp_path / "repo"
    coordinator.mkdir()
    config = claude_config_dir / ".claude.json"
    config.write_text('{"projects": {', encoding="utf-8")
    with pytest.raises(trust.TrustError, match="not valid UTF-8 JSON"):
        trust.share_trust(coordinator, [tmp_path / "wt"], "s1")
    assert config.read_text(encoding="utf-8") == '{"projects": {'
    assert [e["kind"] for e in _events(coordinator)] == ["skipped"]


def test_missing_config_writes_nothing(tmp_path: Path, claude_config_dir: Path) -> None:
    with pytest.raises(trust.TrustError, match="cannot read"):
        trust.share_trust(tmp_path, [tmp_path / "wt"], "s1")
    assert not (claude_config_dir / ".claude.json").exists()


def test_off_switch_writes_nothing(tmp_path: Path, claude_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(trust.SHARE_ENV, "off")
    coordinator = tmp_path / "repo"
    coordinator.mkdir()
    config = _config(claude_config_dir, {_key(coordinator): {"hasTrustDialogAccepted": True}})
    before = config.read_bytes()
    assert trust.share_trust(coordinator, [tmp_path / "wt"], "s1") == []
    assert config.read_bytes() == before
    assert "CODING_AGENT_SHARE_TRUST=off" in _events(coordinator)[0]["detail"]["reason"]


# --- starting a worker -------------------------------------------------------------------------------------------


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


def _orca(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _task(repo: Path) -> str:
    return orca_cli.create_task(repo, project="demo-repo", t_id="T-tr", spec="x", title="Fix")


def _trusted_in(snapshot: Path, path: Path) -> bool:
    projects = json.loads(snapshot.read_text(encoding="utf-8"))["projects"]
    return projects.get(_key(path), {}).get("hasTrustDialogAccepted") is True


@pytest.mark.parametrize("mode", [None, "bypassPermissions"])
def test_worktree_then_trust_then_terminal(repo: Path, fake_orca: Path, claude_config_dir: Path, mode: str | None) -> None:
    _config(claude_config_dir, {_key(repo): {"hasTrustDialogAccepted": True}})
    answer, path = orca_cli._start_in_new_worktree(repo, _task(repo), "claude", "fix", "Fix", "run_test", permission_mode=mode)
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] is None
    assert orca["created_terminals"][0]["command"] == ("claude" if mode is None else f"claude --permission-mode {mode}")
    assert orca["dispatches"][answer["result"]["dispatchId"]]["terminal"] == orca["created_terminals"][0]["handle"]
    # trust was written after the worktree existed and before the agent started
    assert not _trusted_in(Path(f"{fake_orca}.worktree-create.json"), Path(path))
    assert _trusted_in(Path(f"{fake_orca}.terminal-create.json"), Path(path))
    assert _trusted_in(Path(f"{fake_orca}.terminal-create.json"), repo)
    assert [e["kind"] for e in _events(repo)] == ["shared"]


def test_untrusted_coordinator_keeps_the_dialog_error(repo: Path, fake_orca: Path, claude_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(claude_config_dir, {})
    before = config.read_bytes()
    task_id = _task(repo)
    monkeypatch.setenv("FAKE_ORCA_SCREEN", "Quick safety check: Is this a project you created or one you trust?\n❯ 1. Yes, I trust this folder")
    with pytest.raises(orca_cli.OrcaError, match="asking whether to trust this folder.*not trusted"):
        orca_cli.worker_start(repo, task_id=task_id, agent="claude", title="x")
    assert config.read_bytes() == before
    assert "dispatches" not in _orca(fake_orca)
    assert [e["kind"] for e in _events(repo)] == ["skipped"]


def test_off_switch_keeps_the_old_start(repo: Path, fake_orca: Path, claude_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(trust.SHARE_ENV, "off")
    config = _config(claude_config_dir, {_key(repo): {"hasTrustDialogAccepted": True}})
    before = config.read_bytes()
    orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="x")
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] == "claude"
    assert "created_terminals" not in orca
    assert config.read_bytes() == before
    assert [e["kind"] for e in _events(repo)] == ["skipped"]


def test_other_agents_skip_trust(repo: Path, fake_orca: Path, claude_config_dir: Path) -> None:
    config = _config(claude_config_dir, {_key(repo): {"hasTrustDialogAccepted": True}})
    before = config.read_bytes()
    orca_cli.worker_start(repo, task_id=_task(repo), agent="codex", title="x")
    assert _orca(fake_orca)["created_worktrees"][0]["agent"] == "codex"
    assert config.read_bytes() == before
    assert _events(repo) == []
