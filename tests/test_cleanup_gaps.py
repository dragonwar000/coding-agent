"""Two gaps in removing settled workers' worktrees on their own.

1. A worker started from a base branch is merged into that branch, not into the coordinator's checked-out branch: that
   counts as merged, but merged into some other branch does not.
2. A removal that stopped part-way leaves a directory git no longer knows: only OS junk counts as gone, and a full
   checkout is removed only when its files provably match a merged commit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent import cleanup, orca_cli

GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *GIT, *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("node_modules/\n.coding-agent/\n", encoding="utf-8")
    (root / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
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


def _worker(root: Path, name: str, task: str, orca: dict, *, base: str | None = None) -> Path:
    tree = root.parent / name
    _git(root, "worktree", "add", "-q", "-b", name, str(tree), *([base] if base else []))
    orca_cli.record_worker(root, worktree=str(tree), task_id=task, dispatch=f"ctx_{task}", base_ref=base)
    orca["tasks"][task] = "completed"
    return tree


def _commit(tree: Path, file: str, text: str = "x\n") -> str:
    (tree / file).write_text(text, encoding="utf-8")
    _git(tree, "add", file)
    _git(tree, "commit", "-q", "-m", file)
    return _git(tree, "rev-parse", "HEAD").strip()


@pytest.fixture
def feature_base(repo: Path) -> Path:
    """The coordinator sits on its own branch; tasks start from (and merge into) `team/feat-zalo`."""
    _git(repo, "branch", "team/feat-zalo")
    _git(repo, "switch", "-q", "-c", "feat/ctd-update")
    _commit(repo, "coordinator.txt")
    return repo


# --- GAP 1: merged into the task's base branch ------------------------------------------------------------------


def test_base_branch_is_recorded_as_a_ref(feature_base: Path, orca: dict) -> None:
    _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    record = orca_cli.worker_records(feature_base)[0]
    assert record["base_branch"] == "refs/heads/team/feat-zalo"
    assert record["base"] == _git(feature_base, "rev-parse", "team/feat-zalo").strip()


def test_a_commit_as_base_records_no_branch(feature_base: Path, orca: dict) -> None:
    commit = _git(feature_base, "rev-parse", "team/feat-zalo").strip()
    _worker(feature_base, "w1", "task_1", orca, base=commit)
    assert orca_cli.worker_records(feature_base)[0]["base_branch"] == ""


def test_merged_into_the_base_branch_is_removed(feature_base: Path, orca: dict) -> None:
    tree = _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    _commit(tree, "a.txt")
    assert cleanup.candidates(feature_base)[0].unmerged == 1

    _git(feature_base, "branch", "-f", "team/feat-zalo", "w1")  # fast-forward the base branch to the worker

    found = cleanup.candidates(feature_base)
    assert found[0].unmerged == 0 and found[0].safe
    assert [outcome for _item, outcome in cleanup.auto_clean(feature_base)] == ["removed"]
    assert orca["removed"] == [(str(tree.resolve()), True)]


def test_merged_into_the_base_branchs_upstream_counts(feature_base: Path, orca: dict) -> None:
    tree = _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    worker_head = _commit(tree, "a.txt")
    _git(feature_base, "remote", "add", "origin", str(feature_base.parent / "remote.git"))
    _git(feature_base, "update-ref", "refs/remotes/origin/team/feat-zalo", worker_head)
    _git(feature_base, "config", "branch.team/feat-zalo.remote", "origin")
    _git(feature_base, "config", "branch.team/feat-zalo.merge", "refs/heads/team/feat-zalo")

    assert cleanup.candidates(feature_base)[0].unmerged == 0


def test_merged_only_into_an_unrelated_branch_is_kept(feature_base: Path, orca: dict) -> None:
    tree = _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    _commit(tree, "a.txt")
    _git(feature_base, "branch", "elsewhere", "w1")  # another branch holds it, the base branch and HEAD do not

    assert cleanup.candidates(feature_base)[0].unmerged == 1
    assert cleanup.auto_clean(feature_base) == []
    assert orca["removed"] == []


def test_a_branch_forked_from_another_worker_does_not_make_it_merged(feature_base: Path, orca: dict) -> None:
    first = _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    _commit(first, "a.txt")
    second = _worker(feature_base, "w2", "task_2", orca, base="w1")  # starts from the first worker's branch
    _commit(second, "b.txt")

    unmerged = {item.task_id: item.unmerged for item in cleanup.candidates(feature_base)}
    assert unmerged["task_1"] == 1


def test_a_deleted_base_branch_falls_back_to_head(feature_base: Path, orca: dict) -> None:
    tree = _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    _commit(tree, "a.txt")
    _git(feature_base, "branch", "-f", "team/feat-zalo", "w1")
    _git(feature_base, "branch", "-D", "team/feat-zalo")

    assert cleanup.candidates(feature_base)[0].unmerged == 1
    _git(feature_base, "merge", "-q", "w1")
    assert cleanup.candidates(feature_base)[0].unmerged == 0


def test_an_old_record_without_base_branch_compares_with_head_only(feature_base: Path, orca: dict) -> None:
    tree = _worker(feature_base, "w1", "task_1", orca)
    _commit(tree, "a.txt")
    _git(feature_base, "branch", "-f", "team/feat-zalo", "w1")
    assert orca_cli.worker_records(feature_base)[0]["base_branch"] == ""
    assert cleanup.candidates(feature_base)[0].unmerged == 1


def test_the_board_names_the_base_branch_as_a_merge_target(feature_base: Path, orca: dict) -> None:
    tree = _worker(feature_base, "w1", "task_1", orca, base="team/feat-zalo")
    _commit(tree, "a.txt")
    text = "\n".join(cleanup.board_lines(cleanup.candidates(feature_base)))
    assert "nhánh base của task" in text


# --- GAP 2: leftovers of a removal that stopped part-way -------------------------------------------------------


def _drop_registration(repo: Path, tree: Path) -> None:
    """What `git worktree remove` leaves when it fails part-way: the registration is gone, the directory is not."""
    gitdir = Path(_git(tree, "rev-parse", "--absolute-git-dir").strip())
    shutil.rmtree(gitdir)
    assert (tree / ".git").is_file()


@pytest.mark.parametrize("junk", [[".DS_Store"], ["Thumbs.db", "desktop.ini"], [".DS_Store", "sub/.DS_Store"]])
def test_a_directory_holding_only_os_junk_counts_as_gone(repo: Path, orca: dict, junk: list[str]) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _git(repo, "worktree", "remove", str(tree))
    for name in junk:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_bytes(b"\0junk")

    found = cleanup.candidates(repo)
    assert found[0].exists is False and found[0].describe() == "thư mục không còn"
    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]
    assert orca["removed"] == [] and orca_cli.worker_records(repo) == [] and not tree.exists()


def test_junk_beside_a_real_file_is_kept(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _git(repo, "worktree", "remove", str(tree))
    tree.mkdir()
    (tree / ".DS_Store").write_bytes(b"junk")
    (tree / "notes.txt").write_text("x\n", encoding="utf-8")

    assert cleanup.auto_clean(repo) == []
    assert (tree / "notes.txt").exists() and (tree / ".DS_Store").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlink_among_junk_is_content_and_never_followed(repo: Path, orca: dict, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".DS_Store").write_bytes(b"keep me")
    tree = _worker(repo, "w1", "task_1", orca)
    _git(repo, "worktree", "remove", str(tree))
    tree.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)

    assert cleanup.auto_clean(repo) == []
    assert (outside / ".DS_Store").exists() and (tree / "link").is_symlink()


def _merged_orphan(repo: Path, orca: dict) -> Path:
    """A worker that committed, was merged, then lost its registration with its whole checkout still on disk."""
    tree = _worker(repo, "w1", "task_1", orca)
    _commit(tree, "feature.txt", "done\n")
    _git(repo, "merge", "-q", "w1")
    assert cleanup.candidates(repo)[0].safe  # the board saw the worktree alive, and recorded its head
    _drop_registration(repo, tree)
    (tree / "node_modules" / "pkg").mkdir(parents=True)
    (tree / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    (tree / ".DS_Store").write_bytes(b"junk")
    (tree / ".coding-agent").mkdir(exist_ok=True)
    (tree / ".coding-agent" / "events.jsonl").write_text("{}\n", encoding="utf-8")
    return tree


def test_an_orphaned_checkout_matching_a_merged_commit_is_removed(repo: Path, orca: dict) -> None:
    tree = _merged_orphan(repo, orca)
    (tree / ".DS_Store").unlink()  # OS junk beside the checkout is the next test

    found = cleanup.candidates(repo)
    assert found[0].orphan and found[0].safe, found[0].describe()
    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]
    assert orca["removed"] == [] and not tree.exists() and orca_cli.worker_records(repo) == []
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"


def test_os_junk_in_an_orphaned_checkout_is_not_work(repo: Path, orca: dict) -> None:
    tree = _merged_orphan(repo, orca)
    assert cleanup.candidates(repo)[0].safe
    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]
    assert not tree.exists()


def test_an_orphan_without_a_recorded_head_matches_its_merged_branch(repo: Path, orca: dict) -> None:
    tree = _worker(repo, "w1", "task_1", orca)
    _commit(tree, "feature.txt", "done\n")
    _git(repo, "merge", "-q", "w1")
    _drop_registration(repo, tree)  # no board run in between: only the branch named like the directory is left

    assert orca_cli.worker_records(repo)[0]["head"] == ""
    assert cleanup.candidates(repo)[0].safe
    assert [outcome for _item, outcome in cleanup.auto_clean(repo)] == ["removed"]


@pytest.mark.parametrize("change", ["untracked", "modified", "deleted", "unmerged"])
def test_an_orphaned_checkout_holding_work_is_kept(repo: Path, orca: dict, change: str) -> None:
    tree = _merged_orphan(repo, orca)
    if change == "untracked":
        (tree / "new.txt").write_text("wip\n", encoding="utf-8")
    elif change == "modified":
        (tree / "feature.txt").write_text("changed\n", encoding="utf-8")
    elif change == "deleted":
        (tree / "app.txt").unlink()
    else:
        _git(repo, "reset", "-q", "--hard", "HEAD~1")  # the merge is undone: the worker's commit is merged nowhere

    found = cleanup.candidates(repo)
    assert found[0].orphan and not found[0].safe
    assert found[0].describe() == "đăng ký worktree đã mất, cần kiểm tay"
    assert cleanup.auto_clean(repo) == []
    assert tree.exists() and (tree / "node_modules" / "pkg" / "index.js").exists()
    assert len(orca_cli.worker_records(repo)) == 1


def test_an_orphan_whose_git_file_points_elsewhere_is_kept(repo: Path, orca: dict, tmp_path: Path) -> None:
    tree = _merged_orphan(repo, orca)
    (tree / ".git").write_text(f"gitdir: {tmp_path / 'nowhere' / 'worktrees' / 'w1'}\n", encoding="utf-8")
    (tree / ".DS_Store").unlink()

    found = cleanup.candidates(repo)
    assert not found[0].safe and found[0].describe() == "đăng ký worktree đã mất, cần kiểm tay"


def test_checking_an_orphan_writes_nothing_into_it(repo: Path, orca: dict) -> None:
    tree = _merged_orphan(repo, orca)
    (tree / "new.txt").write_text("wip\n", encoding="utf-8")
    before = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))
    cleanup.candidates(repo)
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*")) == before


def test_discard_removes_an_unproven_orphan(repo: Path, orca: dict) -> None:
    tree = _merged_orphan(repo, orca)
    (tree / "new.txt").write_text("wip\n", encoding="utf-8")
    assert [outcome for _item, outcome in cleanup.clean(repo, cleanup.candidates(repo), discard=True)] == ["removed"]
    assert not tree.exists() and orca["removed"] == []
