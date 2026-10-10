"""Removing a settled worker's worktree: only after confirmation, never while it holds work that exists nowhere else."""

import json
import subprocess
from pathlib import Path

import pytest

from coding_agent import cleanup, cli, coordinator, events, orca_cli
from test_hooks import call, manifest

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


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


def worker(repo: Path, tmp_path: Path, name: str, *, status: str = "completed") -> tuple[str, Path]:
    """A task with a real linked worktree recorded as its worker's, settled to `status`."""
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id=f"T-{name}", spec="x", title=name)
    path = tmp_path / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    orca_cli.record_worker(repo, worktree=str(path), task_id=task_id, dispatch=f"ctx_{name}")
    if status != "ready":
        orca_cli.set_status(repo, task_id=task_id, requested=status, basis="predicate" if status == "completed" else None)
    return task_id, path


@pytest.fixture
def ready_repo(repo: Path) -> Path:
    (repo / ".gitignore").write_text(".coding-agent/\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_only_settled_workers_are_candidates(ready_repo, tmp_path, fake_orca):
    done, _ = worker(ready_repo, tmp_path, "done-one")
    failed, _ = worker(ready_repo, tmp_path, "failed-one", status="failed")
    worker(ready_repo, tmp_path, "still-running", status="ready")
    found = cleanup.candidates(ready_repo)
    assert [(item.task_id, item.status, item.safe) for item in found] == [(done, "completed", True), (failed, "failed", True)]


def test_a_clean_merged_worktree_is_removed_and_stops_being_a_worker(ready_repo, tmp_path, fake_orca):
    task_id, path = worker(ready_repo, tmp_path, "tidy")
    assert coordinator.role_for(path) == "worker"
    results = cleanup.clean(ready_repo, cleanup.candidates(ready_repo))
    assert [(item.task_id, outcome) for item, outcome in results] == [(task_id, "removed")]
    assert not path.exists()
    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    assert state["released"] == ["ctx_tidy"] and state["removed_worktrees"] == [{"path": str(path.resolve()), "force": False}]
    assert cleanup.candidates(ready_repo) == [] and orca_cli.worker_worktrees(ready_repo) == set()
    log = [json.loads(line) for line in events.log_path(ready_repo).read_text(encoding="utf-8").splitlines()]
    assert log[-1]["kind"] == "worktree-removed" and log[-1]["detail"]["discarded"] is False


def test_uncommitted_files_keep_the_worktree(ready_repo, tmp_path, fake_orca):
    _task, path = worker(ready_repo, tmp_path, "dirty")
    (path / "half-done.py").write_text("x = 1\n", encoding="utf-8")
    found = cleanup.candidates(ready_repo)
    assert found[0].dirty == 1 and found[0].describe() == "1 file chưa commit"
    assert [outcome for _item, outcome in cleanup.clean(ready_repo, found)] == ["kept: 1 file chưa commit"]
    assert path.exists() and (path / "half-done.py").exists()


def test_unmerged_commits_keep_the_worktree_until_merged(ready_repo, tmp_path, fake_orca):
    _task, path = worker(ready_repo, tmp_path, "ahead")
    (path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "work")
    found = cleanup.candidates(ready_repo)
    assert found[0].unmerged == 1 and found[0].describe() == "1 commit chưa gộp"
    assert [outcome for _item, outcome in cleanup.clean(ready_repo, found)] == ["kept: 1 commit chưa gộp"]
    git(ready_repo, "merge", "-q", "ahead")
    assert cleanup.candidates(ready_repo)[0].safe
    assert [outcome for _item, outcome in cleanup.clean(ready_repo, cleanup.candidates(ready_repo))] == ["removed"]
    assert not path.exists()


def test_discard_removes_unmerged_work_and_records_that(ready_repo, tmp_path, fake_orca):
    _task, path = worker(ready_repo, tmp_path, "throwaway")
    (path / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    assert [outcome for _item, outcome in cleanup.clean(ready_repo, cleanup.candidates(ready_repo), discard=True)] == ["removed"]
    assert json.loads(fake_orca.read_text(encoding="utf-8"))["removed_worktrees"][0]["force"] is True
    log = [json.loads(line) for line in events.log_path(ready_repo).read_text(encoding="utf-8").splitlines()]
    assert log[-1]["detail"]["discarded"] is True


def test_a_failing_orca_removal_leaves_the_record_in_place(ready_repo, tmp_path, fake_orca, monkeypatch):
    _task, path = worker(ready_repo, tmp_path, "stuck")
    monkeypatch.setenv("FAKE_ORCA_RM_FAIL", "1")
    results = cleanup.clean(ready_repo, cleanup.candidates(ready_repo))
    assert results[0][1].startswith("failed:") and "worktree_dirty" in results[0][1]
    assert path.exists() and len(cleanup.candidates(ready_repo)) == 1


def test_a_worktree_already_gone_is_forgotten_without_calling_remove(ready_repo, tmp_path, fake_orca):
    _task, path = worker(ready_repo, tmp_path, "vanished")
    git(ready_repo, "worktree", "remove", "--force", str(path))
    found = cleanup.candidates(ready_repo)
    assert found[0].exists is False and found[0].describe() == "thư mục không còn"
    assert [outcome for _item, outcome in cleanup.clean(ready_repo, found)] == ["removed"]
    assert "removed_worktrees" not in json.loads(fake_orca.read_text(encoding="utf-8"))


def test_the_cli_removes_a_clean_worktree_without_yes_and_names_what_it_did(ready_repo, tmp_path, fake_orca, capsys, monkeypatch):
    monkeypatch.setenv(cleanup.AUTO_ENV, "off")  # so worktree-list shows it instead of removing it
    task_id, path = worker(ready_repo, tmp_path, "by-cli")
    assert cli.main(["--root", str(ready_repo), "worktree-list"]) == 0
    assert f"by-cli ({task_id}, completed): sạch, đã gộp" in capsys.readouterr().out
    assert cli.main(["--root", str(ready_repo), "worktree-clean", "--task-id", task_id, "--discard"]) == 2
    assert "ask the user to confirm" in capsys.readouterr().err and path.exists()
    assert cli.main(["--root", str(ready_repo), "worktree-clean"]) == 2
    assert "--task-id, or pass --all" in capsys.readouterr().err
    assert cli.main(["--root", str(ready_repo), "worktree-clean", "--task-id", "task_nope"]) == 1
    assert "has no settled worker worktree" in capsys.readouterr().err
    assert cli.main(["--root", str(ready_repo), "worktree-clean", "--task-id", task_id]) == 0
    assert f"removed: by-cli ({task_id})" in capsys.readouterr().out and not path.exists()
    assert cli.main(["--root", str(ready_repo), "worktree-list"]) == 0
    assert "no settled worker worktree" in capsys.readouterr().out


def test_worktree_list_removes_a_merged_clean_worktree_on_its_own(ready_repo, tmp_path, fake_orca, capsys):
    task_id, path = worker(ready_repo, tmp_path, "auto")
    assert cli.main(["--root", str(ready_repo), "worktree-list"]) == 0
    out = capsys.readouterr().out
    assert f"auto-clean: removed auto ({task_id})" in out and "no settled worker worktree left" in out
    assert not path.exists() and orca_cli.worker_records(ready_repo) == []


def test_clean_all_keeps_unsafe_worktrees_and_a_named_one_kept_fails(ready_repo, tmp_path, fake_orca, capsys):
    worker(ready_repo, tmp_path, "ok-one")
    dirty_task, dirty = worker(ready_repo, tmp_path, "dirty-one")
    (dirty / "wip.py").write_text("x\n", encoding="utf-8")
    assert cli.main(["--root", str(ready_repo), "worktree-clean", "--all"]) == 0
    out = capsys.readouterr().out
    assert "removed: ok-one" in out and "kept: 1 file chưa commit: dirty-one" in out
    assert cli.main(["--root", str(ready_repo), "worktree-clean", "--task-id", dirty_task]) == 1
    assert dirty.exists()


def test_the_board_lists_only_worktrees_that_still_hold_work(ready_repo, tmp_path, fake_orca):
    _clean_task, clean_path = worker(ready_repo, tmp_path, "merged")
    task_id, path = worker(ready_repo, tmp_path, "on-board")
    (path / "wip.py").write_text("x\n", encoding="utf-8")
    lines, error = coordinator.read_board(ready_repo)
    text = "\n".join(lines)
    assert error is None and "worktree của worker đã xong, còn việc chưa gộp hoặc chưa commit (1):" in text
    assert f"on-board ({task_id}, completed): 1 file chưa commit" in text and "worktree-clean --task-id <id> --discard --yes" in text
    assert "merged (" not in text and not clean_path.exists()


def test_the_hook_makes_the_host_ask_the_user_before_a_discard(ready_repo, fake_orca):
    manifest(ready_repo, verify=[])
    command = "PYTHONPATH=harness/coding-agent/src python3 -m coding_agent.cli worktree-clean --task-id task_0001 --discard --yes"
    for mode in ("enforce", "shadow"):
        result = call(ready_repo, "coordinator-guard", {"session_id": "w1", "cwd": str(ready_repo), "tool_name": "Bash", "tool_input": {"command": command}}, mode=mode)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert result.returncode == 0 and decision["permissionDecision"] == "ask"
        assert "xoá worktree" in decision["permissionDecisionReason"] and "task_0001" in decision["permissionDecisionReason"]
    off = call(ready_repo, "coordinator-guard", {"session_id": "w1", "cwd": str(ready_repo), "tool_name": "Bash", "tool_input": {"command": command}}, mode="off")
    assert off.stdout == ""


def test_other_commands_need_no_confirmation():
    assert coordinator.needs_user_confirmation("Bash", {"command": "python3 -m coding_agent.cli worktree-list"}) is None
    assert coordinator.needs_user_confirmation("Bash", {"command": "python3 -m coding_agent.cli plan-next plan.yaml"}) is None
    assert coordinator.needs_user_confirmation("Write", {"file_path": "a.py"}) is None
    assert coordinator.needs_user_confirmation("Bash", {"command": "python3 -m coding_agent.cli --root /r worktree-clean --all"}) is None
    assert coordinator.needs_user_confirmation("Bash", {"command": "python3 -m coding_agent.cli --root /r worktree-clean --all --discard --yes"}) is not None


def test_a_worktree_git_cannot_compare_is_kept(ready_repo, tmp_path, fake_orca):
    """A worker directory that is not a worktree of this repository: git cannot say what removing it loses."""
    task_id = orca_cli.create_task(ready_repo, project="demo-repo", t_id="T-foreign", spec="x", title="foreign")
    other = tmp_path / "foreign"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "commit", "-q", "--allow-empty", "-m", "elsewhere")
    orca_cli.record_worker(ready_repo, worktree=str(other), task_id=task_id, dispatch="ctx_foreign")
    orca_cli.set_status(ready_repo, task_id=task_id, requested="completed", basis="predicate")
    found = cleanup.candidates(ready_repo)
    assert found[0].safe is False and found[0].describe() == "không kiểm được bằng git"
    assert [outcome for _item, outcome in cleanup.clean(ready_repo, found)] == ["kept: không kiểm được bằng git"]
    assert other.exists()


def test_only_the_workers_own_commits_count_when_branches_differ(ready_repo, tmp_path, fake_orca):
    """The worktree starts from an older commit than the coordinator: that gap is not the worker's work."""
    git(ready_repo, "branch", "old-base")
    (ready_repo / "newer.py").write_text("x = 1\n", encoding="utf-8")
    git(ready_repo, "add", "-A")
    git(ready_repo, "commit", "-q", "-m", "coordinator moved on")
    git(ready_repo, "switch", "-q", "old-base")
    (ready_repo / "side.py").write_text("y = 1\n", encoding="utf-8")
    git(ready_repo, "add", "-A")
    git(ready_repo, "commit", "-q", "-m", "a commit the coordinator branch lacks")
    task_id = orca_cli.create_task(ready_repo, project="demo-repo", t_id="T-base", spec="x", title="from-other-base")
    path = tmp_path / "from-other-base"
    git(ready_repo, "worktree", "add", "-q", "-b", "from-other-base", str(path), "old-base")
    orca_cli.record_worker(ready_repo, worktree=str(path), task_id=task_id, dispatch="ctx_base")
    orca_cli.set_status(ready_repo, task_id=task_id, requested="completed", basis="predicate")
    git(ready_repo, "switch", "-q", "-")
    assert cleanup.candidates(ready_repo)[0].unmerged == 0
    (path / "worker.py").write_text("z = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "the worker's own commit")
    assert cleanup.candidates(ready_repo)[0].unmerged == 1
