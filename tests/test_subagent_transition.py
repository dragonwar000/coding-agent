"""A project that switched to `dispatch: subagent` keeps the Orca workers it dispatched before: only new Orca work is refused."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coding_agent import cli, coordinator, orca_cli, resume, subagents, watcher
from test_hooks import call, manifest
from test_subagent_dispatch import assigned, feed, project, started, tool

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
PLAN = {"tasks": [
    {"id": "first", "title": "First", "spec": "Do the first thing", "model": "sonnet"},
    {"id": "second", "title": "Second", "spec": "Do the second thing", "model": "opus", "deps": ["first"]},
]}


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    return state


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


@pytest.fixture
def switched(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed repository whose manifest has no `coordinator` block, so it runs on the default."""
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    project(repo, None)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    return repo


def worker(repo: Path, tmp_path: Path, name: str, *, status: str = "dispatched", run: str | None = None) -> tuple[str, Path]:
    """An Orca task with a real linked worktree recorded as its worker's."""
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id=f"T-{name}", spec="x", title=name)
    path = tmp_path / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    orca_cli.record_worker(repo, worktree=str(path), task_id=task_id, dispatch=f"ctx_{name}", run=run)
    orca_cli.set_status(repo, task_id=task_id, requested=status, basis="predicate" if status == "completed" else None)
    return task_id, path


def board(repo: Path) -> str:
    lines, error = coordinator.read_board(repo)
    assert error is None
    return "\n".join(lines)


def status_of(state: Path, task_id: str) -> str:
    return json.loads(state.read_text(encoding="utf-8"))["tasks"][task_id]["status"]


def plan_file(repo: Path, tmp_path: Path, mapping: dict[str, str]) -> Path:
    (repo / ".coding-agent").mkdir(exist_ok=True)
    (repo / ".coding-agent" / "plan.json").write_text(json.dumps(mapping), encoding="utf-8")
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(PLAN), encoding="utf-8")
    return path


def plan_next(repo: Path, plan: Path, capsys) -> str:
    capsys.readouterr()
    assert cli.main(["--root", str(repo), "plan-next", str(plan)]) == 0
    return capsys.readouterr().out


# The board


def test_the_board_still_lists_an_unsettled_orca_worker(switched, tmp_path):
    task_id, _ = worker(switched, tmp_path, "w1")
    text = board(switched)
    assert task_id in text and "[dispatched]" in text and "vẫn theo dõi" in text and "Run run_test" in text


def test_the_board_shows_the_ledger_and_the_old_workers_together(switched, tmp_path):
    task_id, _ = worker(switched, tmp_path, "w1")
    feed(switched, assigned("u1", "new job"), started("a1"))
    text = board(switched)
    assert "new job" in text and task_id in text


def test_an_unreadable_orca_warns_and_keeps_the_ledger(switched, tmp_path, monkeypatch):
    task_id, _ = worker(switched, tmp_path, "w1")
    feed(switched, assigned("u1", "new job"), started("a1"))
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    text = board(switched)
    assert "new job" in text and "không đọc được từ Orca" in text and "không giao lại" in text and task_id not in text


def test_the_watcher_is_kept_running_for_an_old_worker(switched, tmp_path, monkeypatch):
    task_id, _ = worker(switched, tmp_path, "w1")
    started_for: list[str | None] = []
    monkeypatch.setattr(resume, "auto", lambda *a, **k: [resume.Finding(task_id=task_id, dispatch="ctx_w1", terminal="t", state="working")])
    monkeypatch.setattr(watcher, "ensure_running", lambda root, run=None, **k: started_for.append(run))
    board(switched)
    assert started_for == ["run_test"]


def test_a_worker_of_another_run_is_listed_with_how_to_reach_it(switched, tmp_path):
    older, _ = worker(switched, tmp_path, "w1", run="run_older")
    newer, _ = worker(switched, tmp_path, "w2", run="run_test")
    text = board(switched)
    assert newer in text and older in text and "inbox --run run_older" in text


def test_a_project_with_no_worker_records_does_not_call_orca(repo, tmp_path, monkeypatch):
    state = tmp_path / "never-written.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    project(repo, None)
    assert "vẫn theo dõi" not in board(repo) and not state.exists()


# Settling, merging and cleaning


def test_settle_inbox_and_auto_clean_finish_an_old_worker(switched, fake_orca, tmp_path, capsys):
    task_id, path = worker(switched, tmp_path, "w1")
    assert cli.main(["--root", str(switched), "inbox"]) == 0
    assert cli.main(["--root", str(switched), "worker-settle", "--task-id", task_id, "--basis", "predicate"]) == 0
    assert status_of(fake_orca, task_id) == "completed"
    board(switched)
    assert not path.exists() and orca_cli.worker_records(switched) == []
    assert "vẫn theo dõi" not in board(switched)


def test_an_unmerged_old_worker_is_kept_and_listed(switched, tmp_path):
    task_id, path = worker(switched, tmp_path, "w1", status="completed")
    (path / "work.txt").write_text("x", encoding="utf-8")
    git(path, "add", "work.txt")
    git(path, "commit", "-q", "-m", "work")
    text = board(switched)
    assert path.exists() and task_id in text
    git(switched, "merge", "-q", "--no-edit", "w1")
    board(switched)
    assert not path.exists() and (switched / "work.txt").exists()


def test_new_orca_work_is_still_refused_while_an_old_worker_runs(switched, tmp_path, capsys):
    worker(switched, tmp_path, "w1")
    assert cli.main(["--root", str(switched), "delegate", "--title", "t", "--spec", "s", "--model", "sonnet"]) == 2
    assert "delegate: refused" in capsys.readouterr().err


def test_an_old_workers_session_is_not_guarded(switched, tmp_path):
    _, path = worker(switched, tmp_path, "w1")
    payload = tool(path, "Write", {"file_path": "x.py", "content": "x"})
    assert call(switched, "coordinator-guard", payload, mode="enforce").returncode == 0


# The plan graph


def test_plan_next_does_not_give_out_a_node_an_orca_worker_holds(switched, fake_orca, tmp_path, capsys):
    task_id, _ = worker(switched, tmp_path, "w1")
    plan = plan_file(switched, tmp_path, {"first": task_id})
    out = plan_next(switched, plan, capsys)
    assert "ready first" not in out and "[dispatched] first" in out and "ready second" not in out
    orca_cli.set_status(switched, task_id=task_id, requested="completed", basis="predicate")
    out = plan_next(switched, plan, capsys)
    assert "[completed] first" in out and "ready second: Second" in out


def test_plan_next_fails_closed_when_orca_cannot_be_read(switched, tmp_path, monkeypatch, capsys):
    task_id, _ = worker(switched, tmp_path, "w1")
    plan = plan_file(switched, tmp_path, {"first": task_id})
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    out = plan_next(switched, plan, capsys)
    assert "ready first" not in out and "[orca-unreadable] first" in out


def test_an_applied_node_with_no_worker_can_go_to_a_subagent(switched, tmp_path, capsys):
    task_id = orca_cli.create_task(switched, project="demo-repo", t_id="T-free", spec="x", title="free")
    out = plan_next(switched, plan_file(switched, tmp_path, {"first": task_id}), capsys)
    assert "ready first: First" in out


def test_a_stale_mapping_does_not_block_a_new_plan(switched, tmp_path, capsys):
    out = plan_next(switched, plan_file(switched, tmp_path, {"first": "task_gone"}), capsys)
    assert "ready first: First" in out


def test_a_subagents_record_of_a_node_wins_over_its_orca_task(switched, tmp_path, capsys):
    task_id = orca_cli.create_task(switched, project="demo-repo", t_id="T-free", spec="x", title="free")
    feed(switched, assigned("u1", "first: do it"), started("a1"))
    out = plan_next(switched, plan_file(switched, tmp_path, {"first": task_id}), capsys)
    assert "[running] first" in out


# Which manifest decides


def test_an_explicit_orca_coordinator_can_delegate_into_a_repo_with_no_setting(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project(repo, "orca")))
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    project(other, None)
    monkeypatch.setattr(cli.orca_cli, "delegate", lambda *a, **k: ("task_1", "ctx_1"))
    monkeypatch.setattr(cli, "_start_watch", lambda *a: None)
    monkeypatch.setattr(cli, "_auto_clean", lambda *a: None)
    assert cli.main(["--root", str(other), "delegate", "--title", "t", "--spec", "s", "--model", "sonnet"]) == 0
    assert "task_1" in capsys.readouterr().out


def test_a_directory_with_no_manifest_uses_the_roots_setting(repo, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert subagents.command_mode(project(repo, None), bare) == "subagent"
    assert subagents.command_mode(project(repo, "orca"), bare) == "orca"
    manifest(repo, verify=[])
    assert subagents.command_mode(bare, bare) == "orca"
