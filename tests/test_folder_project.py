"""Delegating from a folder project: a root outside git that holds several repositories, where the worker's worktree is created in a named one."""

import json
import subprocess
from pathlib import Path

import pytest

from coding_agent import cleanup, cli, coordinator, orca_cli, plan

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.setenv("FAKE_ORCA_REAL_WORKTREE", "1")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    return state


def orca_state(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout.strip()


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path.resolve()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A folder project: not a repository itself, with the child repositories `vgps/app` and `tools`."""
    root = tmp_path / "sdk"
    new_repo(root / "vgps" / "app")
    new_repo(root / "tools")
    return root.resolve()


def test_a_folder_root_with_repo_starts_the_worker_through_worktree_create(folder, fake_orca, capsys):
    app = folder / "vgps" / "app"
    assert cli.main(["--root", str(folder), "delegate", "--title", "Fix login", "--spec", "x", "--t-id", "T-f1", "--repo", "vgps/app"]) == 0
    out = json.loads(capsys.readouterr().out)
    state = orca_state(fake_orca)
    assert state["created_worktrees"] == [{"name": "fix-login", "repo": f"path:{app}", "agent": "claude"}]
    # The worktree starts from the commit the target repository has checked out, and is nobody's child.
    assert state["create_calls"] == [{"base": git(app, "rev-parse", "HEAD"), "no_parent": True}]
    dispatch = state["dispatches"][out["dispatch"]]
    assert dispatch["task"] == out["task_id"] and dispatch["worktree"].startswith("path:")
    worktree = Path(dispatch["worktree"][5:]).resolve()
    assert orca_cli.worker_worktrees(app) == {str(worktree)}
    assert coordinator.role_for(worktree) == "worker" and coordinator.role_for(app) == "coordinator"
    # The coordinator root sees the same worker, with the repository it belongs to.
    assert orca_cli.worker_records(folder) == [{"worktree": str(worktree), "task_id": out["task_id"], "dispatch": out["dispatch"],
                                                "base": git(app, "rev-parse", "HEAD"), "repo": str(app)}]
    assert orca_cli.workers_ledger(folder) == folder / ".coding-agent" / "workers.jsonl"


def test_a_repository_orca_does_not_know_is_registered_and_the_creation_tried_once_more(folder, fake_orca, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_UNKNOWN_REPO", "1")
    tools = folder / "tools"
    orca_cli.delegate(folder, title="Lint", spec="x", t_id="T-f2", agent="claude", repo=str(tools))
    state = orca_state(fake_orca)
    assert state["refused_creates"] == [str(tools)] and state["repos"] == [str(tools)]
    assert state["created_worktrees"] == [{"name": "lint", "repo": f"path:{tools}", "agent": "claude"}]
    assert len(orca_cli.worker_worktrees(tools)) == 1


def test_another_worktree_create_error_is_not_retried(folder, fake_orca, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    with pytest.raises(orca_cli.OrcaError, match="daemon not running"):
        orca_cli._create_worktree(folder / "tools", "x", [])


def test_a_folder_root_without_repo_fails_before_any_task_is_created(folder, fake_orca, capsys):
    new_repo(folder / "node_modules" / "dep")
    new_repo(folder / ".hidden" / "repo")
    new_repo(folder / "a" / "b" / "c" / "too-deep")
    assert cli.main(["--root", str(folder), "delegate", "--title", "Fix", "--spec", "x"]) == 1
    err = capsys.readouterr().err
    assert "pass --repo" in err and "Git repositories found under it: tools, vgps/app." in err
    assert not fake_orca.exists()
    assert not (folder / ".coding-agent" / "links.jsonl").exists()


def test_a_named_path_that_is_not_a_repository_fails_before_any_task_is_created(folder, fake_orca):
    (folder / "docs").mkdir()
    with pytest.raises(orca_cli.OrcaError, match="--repo docs: not a git repository"):
        orca_cli.delegate(folder, title="x", spec="y", t_id="T-f3", agent="claude", repo="docs")
    assert not fake_orca.exists()


def test_a_git_root_creates_the_worktree_from_its_own_repository(repo, fake_orca):
    # Never `new-child`: Orca refuses it for a terminal of a folder project, and it types the dispatch before the agent is ready.
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    task_id, dispatch = orca_cli.delegate(repo, title="Models", spec="x", t_id="T-f4", agent="claude")
    state = orca_state(fake_orca)
    assert state["created_worktrees"] == [{"name": "models", "repo": f"path:{repo.resolve()}", "agent": "claude"}]
    assert state["create_calls"] == [{"base": git(repo, "rev-parse", "HEAD"), "no_parent": True}]
    worktree = Path(state["dispatches"][dispatch]["worktree"][5:]).resolve()
    assert orca_cli.worker_worktrees(repo) == {str(worktree)} and coordinator.role_for(worktree) == "worker"


def test_a_base_ref_is_passed_to_the_new_worktree(repo, folder, fake_orca, capsys):
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    git(repo, "branch", "release")
    assert cli.main(["--root", str(repo), "delegate", "--title", "On main", "--spec", "x", "--base-branch", "release"]) == 0
    assert orca_state(fake_orca)["create_calls"] == [{"base": "release", "no_parent": True}]
    app = folder / "vgps" / "app"
    git(app, "branch", "old")
    task_id = orca_cli.create_task(folder, project="sdk", t_id="T-f5", spec="x", title="From old")
    assert cli.main(["--root", str(folder), "worker-start", "--task-id", task_id, "--repo", str(app), "--base-branch", "old"]) == 0
    assert orca_state(fake_orca)["create_calls"][1:] == [{"base": "old", "no_parent": True}]


def test_worker_start_on_a_folder_root_without_repo_names_the_repositories(folder, fake_orca, capsys):
    task_id = orca_cli.create_task(folder, project="sdk", t_id="T-f6", spec="x", title="x")
    assert cli.main(["--root", str(folder), "worker-start", "--task-id", task_id]) == 1
    assert "tools, vgps/app" in capsys.readouterr().err
    assert "dispatches" not in orca_state(fake_orca)


FOLDER_PLAN = """tasks:
  - id: app
    title: App screen
    spec: Build it
    repo: vgps/app
    base: old
  - id: tools
    title: Tool script
    spec: Write it
    repo: tools
"""


def test_plan_nodes_carry_repo_and_base_to_the_worker(folder, fake_orca):
    nodes = plan.parse(__import__("yaml").safe_load(FOLDER_PLAN))
    assert [(node.id, node.repo, node.base) for node in nodes] == [("app", "vgps/app", "old"), ("tools", "tools", None)]
    app, tools = folder / "vgps" / "app", folder / "tools"
    git(app, "branch", "old")
    plan.apply(folder, nodes)
    started = plan.dispatch_ready(folder, nodes, default_agent="claude")
    state = orca_state(fake_orca)
    assert len(started) == 2
    assert state["created_worktrees"] == [{"name": "app-screen", "repo": f"path:{app}", "agent": "claude"}, {"name": "tool-script", "repo": f"path:{tools}", "agent": "claude"}]
    assert state["create_calls"] == [{"base": "old", "no_parent": True}, {"base": git(tools, "rev-parse", "HEAD"), "no_parent": True}]
    assert len(orca_cli.worker_worktrees(app)) == 1 and len(orca_cli.worker_worktrees(tools)) == 1 and len(orca_cli.worker_records(folder)) == 2


def test_a_plan_without_repo_in_a_folder_project_creates_no_task(folder, tmp_path, fake_orca, capsys):
    path = tmp_path / "plan.yaml"
    path.write_text("tasks:\n  - id: ok\n    title: Ok\n    spec: x\n    repo: tools\n  - id: lost\n    title: Lost\n    spec: x\n", encoding="utf-8")
    assert cli.main(["--root", str(folder), "plan-apply", str(path)]) == 1
    err = capsys.readouterr().err
    assert "task 'lost'" in err and "pass --repo" in err and "tools, vgps/app" in err
    assert not fake_orca.exists() and plan.ledger(folder) == {}


@pytest.mark.parametrize("field", ["repo", "base"])
def test_a_blank_repo_or_base_is_refused(field):
    with pytest.raises(plan.PlanError, match=rf"tasks\[a\]\.{field}"):
        plan.parse({"tasks": [{"id": "a", "title": "A", "spec": "x", field: " "}]})


def test_the_folder_root_board_and_cleanup_see_a_worker_of_a_child_repository(folder, fake_orca, capsys):
    app = folder / "vgps" / "app"
    task_id, _dispatch = orca_cli.delegate(folder, title="Fix login", spec="x", t_id="T-f7", agent="claude", repo="vgps/app")
    worktree = Path(orca_cli.worker_records(folder)[0]["worktree"])
    assert cleanup.candidates(folder) == []
    orca_cli.set_status(folder, task_id=task_id, requested="completed", basis="predicate")
    # The worker's commits are compared with what the child repository has checked out, not with the folder.
    assert cleanup.candidates(folder)[0].safe is True
    git(worktree, "commit", "-q", "--allow-empty", "-m", "the worker's commit")
    found = cleanup.candidates(folder)
    assert found[0].unmerged == 1 and found[0].describe() == "1 commit chưa gộp"
    lines, error = coordinator.read_board(folder)
    assert error is None and any("chờ dọn" in line for line in lines) and any("fix-login" in line for line in lines)
    assert cli.main(["--root", str(folder), "worktree-clean", "--task-id", task_id, "--yes"]) == 1
    assert worktree.is_dir()
    git(app, "merge", "-q", "fix-login")
    capsys.readouterr()
    assert cli.main(["--root", str(folder), "worktree-clean", "--task-id", task_id, "--yes"]) == 0
    assert "removed: fix-login" in capsys.readouterr().out
    assert not worktree.exists()
    assert orca_cli.worker_records(folder) == [] and orca_cli.worker_worktrees(app) == set()


def test_child_repos_skips_hidden_dependency_and_deep_directories(folder):
    new_repo(folder / ".venv" / "lib")
    new_repo(folder / "web" / "node_modules" / "pkg")
    new_repo(folder / "build" / "out")
    new_repo(folder / "one" / "two" / "three")
    new_repo(folder / "one" / "two" / "sub" / "four")
    # A repository is listed, not entered: one nested inside it is not a separate target.
    new_repo(folder / "tools" / "vendored")
    assert orca_cli.child_repos(folder) == ["one/two/three", "tools", "vgps/app"]


def test_the_contract_tells_a_folder_project_coordinator_to_name_the_repo():
    text = coordinator.contract("src")
    assert "Folder project" in text and "`--repo <đường dẫn repo>`" in text and "`repo:`" in text
    assert coordinator.delegate_command("src") == 'PYTHONPATH=src python3 -m coding_agent.cli delegate --title "..." --spec "..."'
    assert coordinator.guard_reason("Bash", {"command": 'python3 -m coding_agent.cli delegate --title "a" --spec "b" --repo vgps/app --base-branch main'}) is None
