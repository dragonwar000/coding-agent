"""The repositories a coordinator delegated into, read through one list (`orca_cli.all_roots`): the ones in its tree
are found by their link ledger (`orca_cli.tree_roots`); one outside the tree that `roots.json` does not list yet is
found again from Orca's worker records (`roots.discover`, run by the board, the inbox and auto-clean), or registered
by hand with `roots-add`. The layout follows the real case: a coordinator in ctd-rnd-poc (itself a git repository)
delegating into c-core-platform/cde-service, gitops/config (in its tree) and D:/Documents/coding-agent (outside it)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, events, orca, orca_cli, roots
from test_auto_clean import _git, orca as orca_fake, repo  # noqa: F401  (fixtures)


def _events(root: Path) -> list[dict]:
    path = events.log_path(root)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _links(path: Path, *task_ids: str) -> None:
    for task_id in task_ids:
        orca.link(path, t_id=f"T-{task_id}", task_id=task_id, project=orca.project_name(path))


@pytest.fixture
def worker_list(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Orca stand-in for `orchestration worker-list` and `worktree list`: `workers` is task id to worktree id."""
    fake: dict = {"workers": {}, "main": [], "calls": [], "fail": None}

    def call_tool(*args: str, accept_ok_exit: bool = False, timeout: float = 60) -> dict:
        fake["calls"].append(args)
        if fake["fail"]:
            raise orca_cli.OrcaError(fake["fail"])
        if args[:2] == ("orchestration", "worker-list"):
            workers = [{"taskId": task, "resource": {"worktreeId": ident}, "projection": {"workspace": {"id": ident}}}
                       for task, ident in fake["workers"].items()]
            return {"ok": True, "result": {"workers": workers, "page": {"nextCursor": None}}}
        if args[:2] == ("worktree", "list"):
            return {"ok": True, "result": {"worktrees": fake["main"]}}
        raise AssertionError(args)

    monkeypatch.setattr(orca_cli, "_call_tool", call_tool)
    return fake


# --- 1. discovery from Orca ------------------------------------------------------------------------------------


def test_discover_registers_the_repo_of_an_unowned_task(repo: Path, tmp_path: Path, worker_list: dict) -> None:
    upstream = _repo(tmp_path / "coding-agent")
    _links(upstream, "task_up")
    tree = tmp_path / "workspaces" / "coding-agent" / "w1"
    _git(upstream, "worktree", "add", "-q", "-b", "w1", str(tree))
    worker_list["workers"]["task_up"] = f"repo-up::{tree.as_posix()}"

    assert roots.discover(repo, ["task_up"]) == [upstream.resolve()]

    assert orca_cli.all_roots(repo) == [repo.resolve(), upstream.resolve()]
    assert orca_cli.registered_roots(repo) == [upstream.resolve()]
    assert [(e["guard"], e["kind"], e["detail"]["task_id"]) for e in _events(repo)] == [("roots", "registered", "task_up")]


def test_a_repo_in_the_tree_needs_neither_orca_nor_roots_json(repo: Path, worker_list: dict) -> None:
    cde = _repo(repo / "c-core-platform" / "cde-service")
    _links(cde, "task_cde")

    assert orca_cli.all_roots(repo) == [repo.resolve(), cde.resolve()]
    assert roots.discover(repo, ["task_cde"]) == []
    assert worker_list["calls"] == []
    assert not (repo / orca_cli.ROOTS_FILE).exists()


def test_discover_uses_the_main_worktree_orca_lists_when_the_worktree_is_gone(repo: Path, tmp_path: Path, worker_list: dict) -> None:
    fe = _repo(tmp_path / "frontend")
    _links(fe, "task_fe")
    worker_list["workers"]["task_fe"] = f"repo-fe::{(tmp_path / 'gone').as_posix()}"
    worker_list["main"] = [{"repoId": "repo-fe", "path": fe.as_posix(), "isMainWorktree": True},
                           {"repoId": "repo-fe", "path": (tmp_path / "other").as_posix(), "isMainWorktree": False}]

    assert roots.discover(repo, ["task_fe"]) == [fe.resolve()]


def test_discover_skips_a_repo_whose_ledger_does_not_hold_the_task(repo: Path, tmp_path: Path, worker_list: dict) -> None:
    be = _repo(tmp_path / "backend")
    _links(be, "task_other")
    worker_list["workers"]["task_be"] = f"repo-be::{be.as_posix()}"

    assert roots.discover(repo, ["task_be"]) == []
    assert orca_cli.registered_roots(repo) == []


def test_discover_asks_orca_nothing_when_every_task_has_an_owner(repo: Path, worker_list: dict) -> None:
    _links(repo, "task_home")

    assert roots.discover(repo, ["task_home", ""]) == []
    assert worker_list["calls"] == []


def test_discover_logs_an_orca_error_and_retries_only_later(repo: Path, worker_list: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    worker_list["fail"] = "orca worker-list exited 1: boom"

    assert roots.discover(repo, ["task_x"]) == []
    assert roots.discover(repo, ["task_x"]) == []

    assert len(worker_list["calls"]) == 1
    assert [(e["kind"], e["detail"]["error"]) for e in _events(repo)] == [("discover-failed", "orca worker-list exited 1: boom")]
    now = roots.time.time()
    monkeypatch.setattr(roots.time, "time", lambda: now + roots.RETRY_S + 1)
    roots.discover(repo, ["task_x"])
    assert len(worker_list["calls"]) == 2


def test_board_shows_tasks_of_every_repo_delegated_into(repo: Path, tmp_path: Path, orca_fake: dict,
                                                         worker_list: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    cde = _repo(repo / "c-core-platform" / "cde-service")
    config = _repo(repo / "gitops" / "config")
    upstream = _repo(tmp_path / "coding-agent")
    for path, task in ((repo, "task_home"), (cde, "task_cde"), (config, "task_cfg"), (upstream, "task_up")):
        _links(path, task)
        orca_fake["tasks"][task] = "dispatched"
    worker_list["workers"]["task_up"] = f"repo-up::{upstream.as_posix()}"
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])

    lines, error = coordinator.read_board(repo, run="run_x")

    text = "\n".join(lines)
    assert error is None
    assert "task_home [dispatched] task_home\n" in text + "\n"
    for line in ("task_cde [dispatched] task_cde (repo cde-service)", "task_cfg [dispatched] task_cfg (repo config)",
                 "task_up [dispatched] task_up (repo coding-agent)"):
        assert line in text
    # Only the repository outside the tree is written to roots.json.
    assert orca_cli.registered_roots(repo) == [upstream.resolve()]


# --- 2. the tree, roots-add, roots-list -------------------------------------------------------------------------


def _ledger(path: Path) -> None:
    (path / ".coding-agent").mkdir(parents=True)
    (path / ".coding-agent" / "links.jsonl").write_text("", encoding="utf-8")


def test_tree_roots_are_the_link_ledgers_below_the_coordinator(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cde = _repo(repo / "c-core-platform" / "cde-service")
    gitops = _repo(repo / "gitops" / "config")
    for found in (cde, gitops):
        _links(found, "task_" + found.name)
    _links(repo, "task_home")
    _repo(repo / "c-core-platform" / "mobile")              # a child repository nobody delegated into
    _ledger(repo / "web" / "node_modules" / "pkg")          # a dependency directory
    _ledger(repo / "a" / "b" / "c" / "d")                   # deeper than SCAN_DEPTH
    _ledger(cde / "sub")                                    # inside a child repository
    linked = repo / "wt"
    _git(cde, "worktree", "add", "-q", "-b", "wt", str(linked))
    _links(linked, "task_wt")                               # a linked worktree, not a repository

    assert orca_cli.tree_roots(repo) == [cde.resolve(), gitops.resolve()]
    assert orca_cli.all_roots(repo) == [repo.resolve(), cde.resolve(), gitops.resolve()]
    assert orca_cli.registered_roots(repo) == []

    assert cli.main(["--root", str(repo), "roots-list"]) == 0
    out = capsys.readouterr().out
    assert f"  {repo.resolve()} [own] (1 task link(s))" in out
    assert f"  {cde.resolve()} [tree] (1 task link(s))" in out


def test_register_root_leaves_tree_directories_out_of_roots_json(repo: Path, tmp_path: Path) -> None:
    cde = _repo(repo / "c-core-platform" / "cde-service")
    deep = repo / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    outside = _repo(tmp_path / "coding-agent")

    assert orca_cli.register_root(repo, cde) is False
    (cde / "sub").mkdir()
    assert orca_cli.register_root(repo, cde / "sub") is True  # below a child repository: the tree does not see it
    assert orca_cli.register_root(repo, deep) is True
    assert orca_cli.register_root(repo, outside) is True
    assert orca_cli.in_tree(repo, cde) and not orca_cli.in_tree(repo, deep) and not orca_cli.in_tree(repo, outside)


def test_roots_add_registers_by_hand(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    be = _repo(tmp_path / "backend")

    inside = _repo(repo / "gitops" / "config")
    assert cli.main(["--root", str(repo), "roots-add", str(be), str(repo), str(inside)]) == 0
    assert cli.main(["--root", str(repo), "roots-add", str(be), str(tmp_path / "missing")]) == 1

    out = capsys.readouterr().out
    assert f"added: {be.resolve()}" in out and f"own: {repo.resolve()}" in out and f"tree: {inside.resolve()}" in out
    assert f"listed: {be.resolve()}" in out and f"missing: {(tmp_path / 'missing').resolve()}" in out
    assert orca_cli.registered_roots(repo) == [be.resolve()]

    assert cli.main(["--root", str(repo), "roots-list"]) == 0
    assert f"  {be.resolve()} [roots.json] (0 task link(s))" in capsys.readouterr().out
