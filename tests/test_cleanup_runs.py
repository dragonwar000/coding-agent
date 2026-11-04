"""Four gaps in removing worker worktrees on their own, each seen on a live Orca (1.4.222).

1. A worktree whose task belongs to an older Run was never a candidate: only the bound Run's tasks were read.
2. A fenced Run (`consumer_fenced`) made the whole cleanup raise, so nothing was removed, not even a clean worktree.
3. A dispatch that failed after its worktree was created left the worktree behind, recorded without a dispatch, with a
   task that never settles.
4. `worker-release` answers `state: retained` for a worker in an ordinary Orca terminal and does nothing, and its
   errors were swallowed: the worker's terminal stayed alive.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent import cleanup, cli, events, orca_cli

GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]
FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
FENCED = "consumer_fenced: This coordinator terminal is no longer bound to Run {run}"


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
    """Orca stand-in with tasks per Run: `task-list` of a Run in `fenced` answers `consumer_fenced`."""
    fake: dict = {"runs": {}, "fenced": set(), "listed": [], "removed": [], "released": [], "release_error": None}

    def list_tasks(run: str | None = None) -> list[dict]:
        key = orca_cli.run_id(run)
        fake["listed"].append(key)
        if key in fake["fenced"]:
            raise orca_cli.OrcaError("orca task-list answered ok:false: " + FENCED.format(run=key))
        return [{"id": k, "status": v, "title": k} for k, v in fake["runs"].get(key, {}).items()]

    def remove(path: str, *, force: bool = False) -> None:
        fake["removed"].append((path, force))
        shutil.rmtree(path, ignore_errors=True)

    def release(dispatch: str, worktree: str | None = None) -> None:
        if fake["release_error"]:
            raise orca_cli.OrcaError(fake["release_error"])
        fake["released"].append(dispatch)

    monkeypatch.setattr(orca_cli, "list_tasks", list_tasks)
    monkeypatch.setattr(orca_cli, "remove_worktree", remove)
    monkeypatch.setattr(orca_cli, "release_worker", release)
    return fake


def _worker(root: Path, name: str, task: str, orca: dict, monkeypatch: pytest.MonkeyPatch, *, run: str | None,
            status: str = "completed", dispatch: str | None = None) -> Path:
    """A worker worktree recorded while `run` is this process's Run (as `worker-start` records it)."""
    tree = root.parent / name
    _git(root, "worktree", "add", "-q", "-b", name, str(tree))
    if run is None:
        monkeypatch.delenv(orca_cli.RUN_ENV, raising=False)
    else:
        monkeypatch.setenv(orca_cli.RUN_ENV, run)
    orca_cli.record_worker(root, worktree=str(tree), task_id=task, dispatch=f"ctx_{task}" if dispatch is None else dispatch)
    orca["runs"].setdefault(run, {})[task] = status
    return tree


def _events(root: Path) -> list[dict]:
    path = events.log_path(root)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _outcomes(results: list) -> dict[str, str]:
    return {item.task_id: outcome for item, outcome in results}


# --- GAP 1: every Run a worker was recorded in ------------------------------------------------------------------


def test_worktree_of_an_older_run_is_removed(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = _worker(repo, "w-old", "task_old", orca, monkeypatch, run="run_old")
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_new")  # the coordinator moved to a new Run

    assert _outcomes(cleanup.auto_clean(repo)) == {"task_old": "removed"}
    assert orca["removed"] == [(str(tree.resolve()), True)]
    assert orca_cli.worker_records(repo) == []


def test_the_run_is_kept_in_the_record(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, "w1", "task_1", orca, monkeypatch, run="run_a")
    assert orca_cli.worker_records(repo)[0]["run"] == "run_a"
    orca_cli.record_worker(repo, worktree=str(repo.parent / "w2"), task_id="task_2", dispatch="ctx_2", run="run_b")
    assert orca_cli.worker_records(repo)[1]["run"] == "run_b"


def test_each_run_is_listed_once(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, "w1", "task_1", orca, monkeypatch, run="run_old")
    _worker(repo, "w2", "task_2", orca, monkeypatch, run="run_old")
    _worker(repo, "w3", "task_3", orca, monkeypatch, run="run_new")

    found = cleanup.candidates(repo)

    assert sorted(item.task_id for item in found) == ["task_1", "task_2", "task_3"]
    assert orca["listed"] == ["run_new", "run_old"]


def test_a_record_without_a_run_uses_the_current_run(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, "w1", "task_1", orca, monkeypatch, run=None)
    assert orca_cli.worker_records(repo)[0]["run"] == ""
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_now")
    orca["runs"]["run_now"] = orca["runs"].pop(None)

    assert _outcomes(cleanup.auto_clean(repo)) == {"task_1": "removed"}
    assert orca["listed"] == ["run_now"]


# --- GAP 2: a fenced Run skips only its own records -------------------------------------------------------------


@pytest.fixture
def fenced(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The bound Run `run_new` is fenced; `run_old` still reads. One clean worktree in each, plus one gone from disk."""
    trees = {
        "old": _worker(repo, "w-old", "task_old", orca, monkeypatch, run="run_old"),
        "new": _worker(repo, "w-new", "task_new", orca, monkeypatch, run="run_new"),
        "gone": _worker(repo, "w-gone", "task_gone", orca, monkeypatch, run="run_new", status="dispatched"),
    }
    _git(repo, "worktree", "remove", str(trees["gone"]))
    orca["fenced"].add("run_new")
    return trees


def test_a_fenced_run_does_not_stop_the_other_runs(repo: Path, orca: dict, fenced: dict[str, Path]) -> None:
    results = cleanup.auto_clean(repo)

    assert _outcomes(results) == {"task_old": "removed", "task_gone": "removed"}
    assert fenced["new"].is_dir() and not fenced["old"].exists()
    assert [record["task_id"] for record in orca_cli.worker_records(repo)] == ["task_new"]
    unread = [e for e in _events(repo) if e["kind"] == "worktree-run-unread"]
    assert len(unread) == 1 and unread[0]["detail"]["run"] == "run_new" and unread[0]["detail"]["records"] == 1
    assert "consumer_fenced" in unread[0]["detail"]["error"]


def test_the_warning_reaches_the_caller(repo: Path, orca: dict, fenced: dict[str, Path]) -> None:
    warnings: list[str] = []
    cleanup.auto_clean(repo, warnings=warnings)
    assert len(warnings) == 1 and "run_new" in warnings[0] and "consumer_fenced" in warnings[0]


def test_cli_removes_what_it_can_and_warns(repo: Path, orca: dict, fenced: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    assert cli.main(["--root", str(repo), "inbox", "--run", "run_new"]) == 0
    captured = capsys.readouterr()
    assert "auto-clean: removed w-old (task_old)" in captured.out
    assert "auto-clean: warning:" in captured.err and "consumer_fenced" in captured.err


def test_a_fenced_mutation_still_removes_and_is_logged(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """`task-list` reads, only `worker-release` is fenced: the worktree is listed and removed, the refusal is a warning."""
    tree = _worker(repo, "w1", "task_1", orca, monkeypatch, run="run_old")
    orca["release_error"] = "orca worker-release answered ok:false: " + FENCED.format(run="run_old")

    assert _outcomes(cleanup.auto_clean(repo)) == {"task_1": "removed"}
    assert not tree.exists()
    failed = [e for e in _events(repo) if e["kind"] == "worker-release-failed"]
    assert len(failed) == 1 and failed[0]["detail"]["dispatch"] == "ctx_task_1" and "consumer_fenced" in failed[0]["detail"]["error"]


# --- GAP 3a: a failed dispatch removes the worktree it just created ---------------------------------------------


@pytest.fixture
def fake_orca(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_test")
    monkeypatch.setenv("FAKE_ORCA_REAL_WORKTREE", "1")
    monkeypatch.setenv("FAKE_ORCA_NO_AGENT_TERMINAL", "1")  # `worker-start --agent` on the new worktree
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    return state


def test_a_refused_worker_start_removes_the_new_worktree(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = orca_cli.create_task(repo, project="repo", t_id="T-1", spec="x", title="Fix it")
    monkeypatch.setenv("FAKE_ORCA_WORKER_START_FENCED", "1")

    with pytest.raises(orca_cli.OrcaError, match="consumer_fenced: worker-start requires"):
        orca_cli.worker_start(repo, task_id=task_id, agent="codex", title="Fix it")

    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    created = fake_orca.parent / "worktrees" / state["created_worktrees"][0]["name"]
    assert state["removed_worktrees"] == [{"path": str(created), "force": True}]
    assert orca_cli.worker_records(repo) == []
    assert not created.exists() and created.name not in _git(repo, "worktree", "list")
    assert [e["kind"] for e in _events(repo) if e["guard"] == "coordinator"] == ["worker-start-failed"]


def test_a_worker_start_that_may_have_run_keeps_the_worktree(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No answer at all (a crash or a timeout): Orca may have started the worker, so nothing is removed here."""
    task_id = orca_cli.create_task(repo, project="repo", t_id="T-1", spec="x", title="Fix it")
    monkeypatch.setenv("FAKE_ORCA_WORKER_START_CRASH", "1")

    with pytest.raises(orca_cli.OrcaError):
        orca_cli.worker_start(repo, task_id=task_id, agent="codex", title="Fix it")

    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    assert state.get("removed_worktrees", []) == []
    assert [record["dispatch"] for record in orca_cli.worker_records(repo)] == [""]


# --- GAP 3b: a recorded worktree whose worker never started -----------------------------------------------------


def test_worktree_of_a_dispatch_that_never_started_is_removed(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup, "UNSTARTED_GRACE_S", 0)
    ready = _worker(repo, "w-ready", "task_ready", orca, monkeypatch, run="run_a", status="ready", dispatch="")
    _worker(repo, "w-lost", "task_lost", orca, monkeypatch, run="run_a", status="ready", dispatch="")
    del orca["runs"]["run_a"]["task_lost"]  # the task is gone from Orca

    assert _outcomes(cleanup.auto_clean(repo)) == {"task_ready": "removed", "task_lost": "removed"}
    assert not ready.exists() and orca_cli.worker_records(repo) == []
    assert orca["released"] == []  # no dispatch to release


@pytest.mark.parametrize("case", ["dispatched", "running", "commit", "dirty", "adopted", "fresh"])
def test_worktree_of_an_unstarted_dispatch_is_kept_unless_provably_empty(repo: Path, orca: dict, monkeypatch: pytest.MonkeyPatch,
                                                                       case: str) -> None:
    if case != "fresh":
        monkeypatch.setattr(cleanup, "UNSTARTED_GRACE_S", 0)
    status = case if case in ("dispatched", "running") else "ready"
    tree = _worker(repo, "w1", "task_1", orca, monkeypatch, run="run_a", status=status, dispatch="adopted" if case == "adopted" else "")
    if case == "commit":
        (tree / "a.txt").write_text("x\n", encoding="utf-8")
        _git(tree, "add", "a.txt")
        _git(tree, "commit", "-q", "-m", "a")
        _git(repo, "merge", "-q", "w1")  # even merged: a worker that never started made no commit of its own
    if case == "dirty":
        (tree / "draft.txt").write_text("wip\n", encoding="utf-8")

    assert cleanup.candidates(repo) == []
    assert cleanup.auto_clean(repo) == [] and tree.is_dir()


# --- GAP 4: a retained worker's terminals are closed ------------------------------------------------------------


class Terminals:
    """Stands in for `_call` and `_call_tool`: `worker-release`, `terminal list` and `terminal close`."""

    def __init__(self, release: dict, terminals: list[dict], close_error: str | None = None) -> None:
        self.release = release
        self.terminals = terminals
        self.close_error = close_error
        self.closed: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def call(self, *args: str) -> dict:
        self.calls.append(args)
        assert args[0] == "worker-release"
        return {"ok": True, "result": self.release}

    def call_tool(self, *args: str, timeout: float = 0) -> dict:
        self.calls.append(args)
        if args[:2] == ("terminal", "list"):
            return {"ok": True, "result": {"terminals": self.terminals, "totalCount": len(self.terminals), "truncated": False}}
        if args[:2] == ("terminal", "close"):
            if self.close_error:
                raise orca_cli.OrcaError(self.close_error)
            self.closed.append(args[args.index("--terminal") + 1])
            return {"ok": True, "result": {"closed": True}}
        raise AssertionError(f"unexpected orca call {args}")


RETAINED = {"state": "retained", "reason": "external_terminal", "processAction": "none"}


@pytest.fixture
def worker_tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A settled worker's clean worktree, and another worktree whose terminal must stay open."""
    tree, other = repo.parent / "w1", repo.parent / "w-other"
    _git(repo, "worktree", "add", "-q", "-b", "w1", str(tree))
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_a")
    orca_cli.record_worker(repo, worktree=str(tree), task_id="task_1", dispatch="ctx_1")
    monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: [{"id": "task_1", "status": "completed", "title": "t"}])
    monkeypatch.setattr(orca_cli, "remove_worktree", lambda path, *, force=False: shutil.rmtree(path))
    return tree, other


def _terminals(monkeypatch: pytest.MonkeyPatch, fake: Terminals) -> Terminals:
    monkeypatch.setattr(orca_cli, "_call", fake.call)
    monkeypatch.setattr(orca_cli, "_call_tool", fake.call_tool)
    return fake


def test_a_retained_worker_has_its_terminals_closed(repo: Path, worker_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    tree, other = worker_tree
    fake = _terminals(monkeypatch, Terminals(RETAINED, [
        {"handle": "term_agent", "worktreePath": str(tree.resolve())},
        {"handle": "term_shell", "worktreePath": str(tree.resolve()) + "/"},
        {"handle": "term_other", "worktreePath": str(other)},
        {"handle": "term_child", "worktreePath": str(tree.resolve() / "sub")},
    ]))

    assert _outcomes(cleanup.auto_clean(repo)) == {"task_1": "removed"}
    assert fake.closed == ["term_agent", "term_shell"]
    closed = [e for e in _events(repo) if e["kind"] == "worker-terminals-closed"]
    assert len(closed) == 1 and closed[0]["detail"]["terminals"] == ["term_agent", "term_shell"]


def test_a_released_worker_needs_no_terminal_closed(repo: Path, worker_tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _terminals(monkeypatch, Terminals({"state": "released"}, [{"handle": "term_agent", "worktreePath": str(worker_tree[0].resolve())}]))
    assert _outcomes(cleanup.auto_clean(repo)) == {"task_1": "removed"}
    assert fake.closed == [] and not any(call[:2] == ("terminal", "list") for call in fake.calls)


def test_a_terminal_that_will_not_close_is_a_warning_and_the_worktree_still_goes(repo: Path, worker_tree: tuple[Path, Path],
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    tree, _other = worker_tree
    _terminals(monkeypatch, Terminals(RETAINED, [{"handle": "term_agent", "worktreePath": str(tree.resolve())}],
                                      close_error="orca terminal answered ok:false: terminal_not_found"))
    warnings: list[str] = []

    assert _outcomes(cleanup.auto_clean(repo, warnings=warnings)) == {"task_1": "removed"}
    assert not tree.exists()
    failed = [e for e in _events(repo) if e["kind"] == "worker-terminal-close-failed"]
    assert len(failed) == 1 and "term_agent" in failed[0]["detail"]["error"] and "terminal_not_found" in failed[0]["detail"]["error"]
    assert any("term_agent" in warning for warning in warnings)


def test_release_worker_closes_only_the_worktrees_terminals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = tmp_path / "wt"
    tree.mkdir()
    fake = _terminals(monkeypatch, Terminals(RETAINED, [
        {"handle": "term_a", "worktreePath": str(tree)},
        {"handle": "term_b", "worktreePath": str(tmp_path / "wt2")},
        {"handle": "term_c"},
    ]))
    outcome = orca_cli.release_worker("ctx_1", str(tree))
    assert fake.closed == ["term_a"]
    assert outcome == orca_cli.Release(state="retained", reason="external_terminal", closed=("term_a",), warnings=())
