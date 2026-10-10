"""Settled workers' worktrees that are merged and clean are removed without asking; anything holding work stays."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent import cleanup, cli, coordinator, events, orca_cli
from coding_agent import plan as plan_graph

GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *GIT, *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    monkeypatch.delenv(cleanup.AUTO_ENV, raising=False)
    monkeypatch.delenv(orca_cli.RUN_ENV, raising=False)
    return root


@pytest.fixture
def orca(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Orca stand-in: tasks by id, and the worktrees it was asked to remove (removed with git, as Orca does)."""
    fake: dict = {"tasks": {}, "removed": [], "released": []}

    def remove(path: str, *, force: bool = False) -> None:
        fake["removed"].append((path, force))
        shutil.rmtree(path, ignore_errors=True)

    monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: [{"id": k, "status": v, "title": k} for k, v in fake["tasks"].items()])
    monkeypatch.setattr(orca_cli, "remove_worktree", remove)
    monkeypatch.setattr(orca_cli, "release_worker", lambda dispatch: fake["released"].append(dispatch))
    return fake


def _worker(root: Path, name: str, task: str, orca: dict, status: str = "completed") -> Path:
    tree = root.parent / name
    _git(root, "worktree", "add", "-q", "-b", name, str(tree))
    orca_cli.record_worker(root, worktree=str(tree), task_id=task, dispatch=f"ctx_{task}")
    orca["tasks"][task] = status
    return tree


def _commit(tree: Path, file: str) -> None:
    (tree / file).write_text("x\n", encoding="utf-8")
    _git(tree, "add", file)
    _git(tree, "commit", "-q", "-m", file)


def _events(root: Path) -> list[dict]:
    path = events.log_path(root)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# --- auto_clean ------------------------------------------------------------------------------------------------


def test_merged_clean_worktree_is_removed(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _commit(tree, "a.txt")
    _git(repo, "merge", "-q", "w1")

    results = cleanup.auto_clean(repo)

    assert [(item.task_id, outcome) for item, outcome in results] == [("task_1", "removed")]
    assert orca["removed"] == [(str(tree.resolve()), False)]
    assert orca_cli.worker_records(repo) == []
    assert [e["kind"] for e in _events(repo)] == ["worktree-auto-removed"]


def test_unmerged_commit_is_kept_until_merged(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _commit(tree, "a.txt")

    assert cleanup.auto_clean(repo) == []
    assert orca["removed"] == [] and len(orca_cli.worker_records(repo)) == 1

    _git(repo, "merge", "-q", "w1")
    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]


def test_uncommitted_file_is_kept(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    (tree / "draft.txt").write_text("wip\n", encoding="utf-8")

    assert cleanup.auto_clean(repo) == []
    assert orca["removed"] == []


def _executable_on_disk_lost(repo: Path, tree: Path) -> None:
    """A file committed as 100755 whose checkout has no executable bit, as NTFS shows it to Git for Windows."""
    _git(repo, "config", "core.fileMode", "true")
    (tree / "run.sh").write_text("echo hi\n", encoding="utf-8")
    _git(tree, "add", "run.sh")
    _git(tree, "update-index", "--chmod=+x", "run.sh")
    _git(tree, "commit", "-q", "-m", "run.sh")
    _git(repo, "merge", "-q", tree.name)
    (tree / "run.sh").chmod(0o644)  # no-op on Windows, where the bit never reaches the disk


@pytest.mark.parametrize("windows", [True, False])
def test_mode_only_change_counts_only_off_windows(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch, windows: bool) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _executable_on_disk_lost(repo, tree)
    monkeypatch.setattr(cleanup, "WINDOWS", windows)

    if windows:
        assert cleanup._dirty(tree) == 0
        assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]
    else:
        assert cleanup._dirty(tree) == 1
        assert cleanup.auto_clean(repo) == []


def test_status_args_ignore_file_mode_only_on_windows() -> None:
    assert cleanup._status_args(True)[:2] == ["-c", "core.fileMode=false"]
    assert "core.fileMode=false" not in cleanup._status_args(False)


def test_harness_runtime_files_are_not_work(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    (tree / ".coding-agent" / "state").mkdir(parents=True)
    (tree / ".coding-agent" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (tree / ".coding-agent" / "state" / "x").write_text("{}\n", encoding="utf-8")
    (tree / ".coding-agent" / "state" / "a session.json").write_text("{}\n", encoding="utf-8")

    assert cleanup._dirty(tree) == 0
    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]


@pytest.mark.parametrize("path", [".coding-agent/plan.yaml", "notes.txt", "with space.txt"])
def test_other_uncommitted_files_beside_runtime_files_are_work(repo: Path, orca: dict, path: str) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    (tree / ".coding-agent").mkdir()
    (tree / ".coding-agent" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (tree / path).write_text("tasks: []\n", encoding="utf-8")

    assert cleanup._dirty(tree) == 1
    assert cleanup.auto_clean(repo) == []
    assert orca["removed"] == []


def test_running_task_is_not_a_candidate(repo: Path, orca: dict) -> None:
    _worker(repo, "w1", "task_1", orca, status="running")
    assert cleanup.auto_clean(repo) == []


def test_missing_directory_is_only_marked_removed(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _git(repo, "worktree", "remove", str(tree))

    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]
    assert orca["removed"] == [] and orca_cli.worker_records(repo) == []


def test_orca_error_on_one_worktree_does_not_stop_the_others(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _worker(repo, "w1", "task_1", orca), _worker(repo, "w2", "task_2", orca)

    def remove(path: str, *, force: bool = False) -> None:
        if path == str(first.resolve()):
            raise orca_cli.OrcaError("boom")
        orca["removed"].append((path, force))

    monkeypatch.setattr(orca_cli, "remove_worktree", remove)
    outcomes = {item.task_id: outcome for item, outcome in cleanup.auto_clean(repo)}
    assert outcomes["task_1"].startswith("failed") and outcomes["task_2"] == "removed"
    assert orca["removed"] == [(str(second.resolve()), False)]


def test_auto_clean_off(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, "w1", "task_1", orca)
    monkeypatch.setenv(cleanup.AUTO_ENV, "off")
    assert cleanup.auto_clean(repo) == []
    assert orca["removed"] == []


def test_board_lists_only_what_was_not_removed(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, "w-clean", "task_1", orca)
    _commit(_worker(repo, "w-open", "task_2", orca), "b.txt")
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])

    lines, error = coordinator.read_board(repo)

    assert error is None
    text = "\n".join(lines)
    assert "w-open" in text and "1 commit chưa gộp" in text
    assert "w-clean" not in text
    assert len(orca["removed"]) == 1


# --- worktree-clean and the confirmation hook ---------------------------------------------------------------


def test_worktree_clean_all_without_yes_removes_only_safe(repo: Path, orca: dict, capsys: pytest.CaptureFixture[str]) -> None:
    _worker(repo, "w-clean", "task_1", orca)
    _commit(_worker(repo, "w-open", "task_2", orca), "b.txt")

    assert cli.main(["--root", str(repo), "worktree-clean", "--all"]) == 0
    out = capsys.readouterr().out
    assert "removed: w-clean (task_1)" in out and "kept: 1 commit chưa gộp: w-open (task_2)" in out
    assert [Path(path).name for path, force in orca["removed"]] == ["w-clean"]


def test_worktree_clean_discard_needs_yes(repo: Path, orca: dict) -> None:
    _commit(_worker(repo, "w-open", "task_2", orca), "b.txt")
    assert cli.main(["--root", str(repo), "worktree-clean", "--all", "--discard"]) == 2
    assert orca["removed"] == []


def test_confirmation_only_for_discard() -> None:
    base = "PYTHONPATH=harness/coding-agent/src python3 -m coding_agent.cli --root ../x worktree-clean --task-id t1"
    assert coordinator.needs_user_confirmation("Bash", {"command": base}) is None
    assert coordinator.needs_user_confirmation("Bash", {"command": base.replace("--root ../x ", "") + " --all"}) is None
    assert coordinator.needs_user_confirmation("Bash", {"command": base + " --discard --yes"}) is not None
    assert coordinator.needs_user_confirmation("Bash", {"command": base + " --yes --discard"}) is not None


# --- the commands that run auto_clean -------------------------------------------------------------------------


@pytest.fixture
def auto_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def fake(root: Path, run: str | None = None, **kwargs) -> list:
        calls.append((root, run))
        return []

    monkeypatch.setattr(cleanup, "auto_clean", fake)
    return calls


def test_inbox_runs_auto_clean(repo: Path, auto_calls: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    monkeypatch.setattr(orca_cli, "ack_reports", lambda run=None: [])
    assert cli.main(["--root", str(repo), "inbox"]) == 0
    assert cli.main(["--root", str(repo), "inbox", "--ack", "--run", "r1"]) == 0
    assert auto_calls == [(repo.resolve(), None), (repo.resolve(), "r1")]


def test_plan_next_runs_auto_clean(repo: Path, auto_calls: list, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_graph, "load", lambda path: [])
    monkeypatch.setattr(plan_graph, "dispatch_ready", lambda *a, **k: [])
    monkeypatch.setattr(plan_graph, "states", lambda *a, **k: {})
    monkeypatch.setattr(plan_graph, "status_lines", lambda *a, **k: [])
    assert cli.main(["--root", str(repo), "plan-next", "plan.yaml"]) == 0
    assert auto_calls == [(repo.resolve(), None)]


def test_auto_clean_error_is_only_a_warning(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(*a, **k):
        raise orca_cli.OrcaError("run_required")

    monkeypatch.setattr(cleanup, "auto_clean", broken)
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    assert cli.main(["--root", str(repo), "inbox"]) == 0
    assert "auto-clean: warning: run_required" in capsys.readouterr().err


def test_cli_prints_removed_worktrees(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _worker(repo, "w1", "task_1", orca)
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    assert cli.main(["--root", str(repo), "inbox"]) == 0
    assert "auto-clean: removed w1 (task_1)" in capsys.readouterr().out
