"""The board follows the Run the coordinator terminal is bound to, reads every repository the coordinator delegated
into, keeps finished-but-unmerged worktrees in view whatever the inbox says, and `inbox --ack --task-id` acknowledges
only the named tasks' reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent import cleanup, cli, coordinator, events, orca, orca_cli
from test_auto_clean import _commit, _git, _worker, orca as orca_fake, repo  # noqa: F401  (fixtures)

FENCED = "orca check answered ok:false: consumer_fenced: This coordinator terminal is bound to run_new, not run_old"


def _events(root: Path) -> list[dict]:
    path = events.log_path(root)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _store_run(root: Path, run: str) -> None:
    (root / orca_cli.RUN_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / orca_cli.RUN_FILE).write_text(run + "\n", encoding="utf-8")


@pytest.fixture
def other(tmp_path: Path) -> Path:
    """A second repository the coordinator delegates into with `--root`."""
    root = tmp_path / "other"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    return root


# --- 1. the Run bound to the terminal ---------------------------------------------------------------------------


def test_board_follows_the_bound_run_without_rewriting_the_stored_one(repo: Path, orca_fake: dict,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    _store_run(repo, "run_old")
    seen: list[str | None] = []
    monkeypatch.setattr(orca_cli, "bound_run", lambda: "run_new")
    monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: seen.append(orca_cli.run_id(run)) or [])
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])

    for _ in range(3):
        lines, error = coordinator.read_board(repo, session="s1")

    assert error is None
    assert lines[0] == "bảng theo Run run_new của terminal này (orca-run lưu run_old)"
    assert set(seen) == {"run_new"}
    assert orca_cli.stored_run(repo) == "run_old"
    assert [(e["kind"], e["detail"]) for e in _events(repo)] == [("run-differs", {"stored": "run_old", "terminal": "run_new"})]


def test_board_says_nothing_when_the_bound_run_is_the_stored_one(repo: Path, orca_fake: dict,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _store_run(repo, "run_a")
    monkeypatch.setattr(orca_cli, "bound_run", lambda: "run_a")
    monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: [])
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])

    lines, error = coordinator.read_board(repo)

    assert error is None
    assert not any("theo Run" in line for line in lines)
    assert _events(repo) == []


def test_bound_run_is_stored_when_none_is(repo: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orca_cli, "bound_run", lambda: "run_new")

    assert orca_cli.sync_bound_run(repo) is None
    assert orca_cli.stored_run(repo) == "run_new"


def test_two_sessions_bound_to_different_runs_share_the_stored_file(repo: Path, orca_fake: dict,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Two coordinator terminals of one repository, each bound to its own Run, each see their own tasks and leave
    `.coding-agent/orca-run` alone."""
    _store_run(repo, "run_a")
    tasks = {"run_a": [{"id": "task_a", "status": "ready", "title": "việc A"}],
             "run_b": [{"id": "task_b", "status": "ready", "title": "việc B"}]}
    for task_id in ("task_a", "task_b"):
        orca.link(repo, t_id=f"T-{task_id}", task_id=task_id, project=orca.project_name(repo))
    bound = {"run": "run_a"}
    monkeypatch.setattr(orca_cli, "bound_run", lambda: bound["run"])
    monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: tasks[orca_cli.run_id(run)])
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])

    def session(run: str, name: str) -> str:
        # Each turn is a fresh hook process: no Run inherited from the other session.
        monkeypatch.delenv(orca_cli.RUN_ENV, raising=False)
        monkeypatch.setattr(orca_cli, "_adopted", None)
        bound["run"] = run
        lines, error = coordinator.read_board(repo, session=name)
        assert error is None
        return "\n".join(lines)

    for _ in range(2):
        a, b = session("run_a", "sa"), session("run_b", "sb")
        assert "task_a" in a and "task_b" not in a and "theo Run" not in a
        assert "task_b" in b and "task_a" not in b and "bảng theo Run run_b của terminal này (orca-run lưu run_a)" in b
        assert orca_cli.stored_run(repo) == "run_a"
    assert [e["kind"] for e in _events(repo)] == ["run-differs"]


def test_cli_without_run_uses_the_terminal_run(repo: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_run(repo, "run_a")
    seen: list[str | None] = []
    monkeypatch.setattr(orca_cli, "bound_run", lambda: "run_b")
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: seen.append(orca_cli.run_id(run)) or [])

    assert cli.main(["--root", str(repo), "inbox"]) == 0

    assert seen == ["run_b"]
    assert orca_cli.stored_run(repo) == "run_a"


def test_run_init_still_stores_the_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_run(repo, "run_a")
    monkeypatch.setattr(orca_cli, "_call", lambda *args, **kwargs: {"ok": True, "result": {"run": {"id": "run_fresh"}}})

    assert orca_cli.run_init(repo, "mục tiêu") == "run_fresh"
    assert orca_cli.stored_run(repo) == "run_fresh"


def test_board_follows_a_fenced_inbox(repo: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_run(repo, "run_old")
    monkeypatch.setattr(orca_cli, "bound_run", lambda: None)

    def unread(run=None):
        if orca_cli.run_id(run) != "run_new":
            raise orca_cli.OrcaError(FENCED)
        return [{"task_id": "task_9", "outcome": "succeeded", "type": "worker_done", "subject": "Standalone xong"}]

    monkeypatch.setattr(orca_cli, "unread_reports", unread)

    lines, error = coordinator.read_board(repo)

    text = "\n".join(lines)
    assert error is None
    assert "bảng theo Run run_new của terminal này (orca-run lưu run_old)" in text
    assert "task_9: succeeded — Standalone xong" in text
    assert "không đọc được" not in text
    assert orca_cli.stored_run(repo) == "run_old"


def test_a_run_set_by_the_user_is_kept(repo: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _store_run(repo, "run_old")
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_user")
    monkeypatch.setattr(orca_cli, "bound_run", lambda: "run_new")

    assert orca_cli.sync_bound_run(repo) is None
    assert orca_cli.run_id(None) == "run_user"
    assert orca_cli.stored_run(repo) == "run_old"


def test_fenced_run_is_parsed() -> None:
    assert orca_cli.fenced_run(orca_cli.OrcaError(FENCED)) == "run_new"
    assert orca_cli.fenced_run(orca_cli.OrcaError("run_required: No Run is bound")) is None


# --- 2. repositories delegated into with --root -----------------------------------------------------------------


def test_delegate_with_another_root_registers_it_once(repo: Path, other: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.setattr(orca_cli, "delegate", lambda root, **kwargs: ("task_x", "ctx_x"))

    for _ in range(2):
        assert cli.main(["--root", str(other), "delegate", "--title", "t", "--spec", "s"]) == 0
    assert cli.main(["--root", str(repo), "delegate", "--title", "t", "--spec", "s"]) == 0

    data = json.loads((repo / orca_cli.ROOTS_FILE).read_text(encoding="utf-8"))
    assert data == [str(other.resolve())]
    assert orca_cli.all_roots(repo) == [repo.resolve(), other.resolve()]


def test_board_reads_tasks_reports_and_worktrees_of_registered_roots(repo: Path, other: Path, orca_fake: dict,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    orca_cli.register_root(repo, other)
    orca.link(other, t_id="T-1", task_id="task_fe", project=orca.project_name(other))
    orca.link(repo, t_id="T-2", task_id="task_home", project=orca.project_name(repo))
    orca_fake["tasks"]["task_home"] = "dispatched"
    (other / ".coding-agent" / "plan.json").write_text(json.dumps({"fe": "task_fe"}), encoding="utf-8")
    _commit(_worker(other, "w-fe", "task_fe", orca_fake), "fe.txt")
    monkeypatch.setattr(orca_cli, "unread_reports",
                        lambda run=None: [{"task_id": "task_fe", "outcome": "succeeded", "type": "worker_done", "subject": "fe xong"}])

    lines, error = coordinator.read_board(repo)

    text = "\n".join(lines)
    assert error is None
    assert "task_fe [completed] task_fe (repo other)" in text
    assert "task_home [dispatched] task_home\n" in text + "\n"
    assert "node fe (task_fe) [repo other]: succeeded — fe xong" in text
    assert "w-fe (task_fe, completed, repo other): 1 commit chưa gộp" in text
    assert orca_fake["removed"] == []


def test_auto_clean_removes_merged_worktree_of_a_registered_root(repo: Path, other: Path, orca_fake: dict) -> None:
    orca_cli.register_root(repo, other)
    tree = _worker(other, "w-fe", "task_fe", orca_fake)
    _commit(tree, "fe.txt")
    _git(other, "merge", "-q", "w-fe")

    results = cleanup.auto_clean(repo)

    assert [(item.task_id, outcome) for item, outcome in results] == [("task_fe", "removed")]
    assert orca_cli.worker_records(other) == []


def test_runtime_files_are_not_work() -> None:
    assert cleanup._harness_owned(".coding-agent/roots.json")
    assert cleanup._harness_owned(".coding-agent/acked.jsonl")


# --- 3. the safety net: finished, unmerged worktrees stay on the board ------------------------------------------


@pytest.mark.parametrize("inbox", ["acked", "unreadable"])
def test_unmerged_finished_worktree_stays_whatever_the_inbox(repo: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch, inbox: str) -> None:
    _commit(_worker(repo, "w-open", "task_1", orca_fake), "a.txt")

    def unread(run=None):
        if inbox == "unreadable":
            raise orca_cli.OrcaError("orca check exited 1: boom")
        return []

    monkeypatch.setattr(orca_cli, "unread_reports", unread)

    lines, error = coordinator.read_board(repo)

    text = "\n".join(lines)
    assert error is None
    assert "xong, worktree còn việc chưa gộp hoặc chưa commit (1):" in text
    assert "w-open (task_1, completed): 1 commit chưa gộp" in text


# --- 4. inbox --ack --task-id -----------------------------------------------------------------------------------


@pytest.fixture
def inbox(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Orca's `check`: unread worker messages in one delivery batch, read by `--peek`, acknowledged by `--ack`."""
    def message(mid: str, task: str, subject: str) -> dict:
        return {"id": mid, "type": "worker_done", "subject": subject,
                "payload": json.dumps({"taskId": task, "dispatchId": f"ctx_{task}", "outcome": "succeeded"})}

    fake: dict = {"unread": [message("m1", "task_a", "A xong"), message("m2", "task_b", "B xong")], "acks": []}

    def call(*args: str, accept_ok_exit: bool = False) -> dict:
        if args[0] == "run-current":
            return {"ok": True, "result": {"run": None}}
        if args[0] != "check":
            return {"ok": True, "result": {}}
        if "--ack" in args:
            fake["acks"].append(args[args.index("--ack") + 1])
            fake["unread"] = []
            return {"ok": True, "result": {}}
        return {"ok": True, "result": {"deliveryId": "d1" if "--peek" not in args else None, "messages": list(fake["unread"])}}

    monkeypatch.setattr(orca_cli, "_call", call)
    return fake


def test_ack_by_task_leaves_other_reports_unread(repo: Path, inbox: dict, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", str(repo), "inbox", "--ack", "--task-id", "task_a"]) == 0
    out = capsys.readouterr().out
    assert f"task_a [repo ?]: succeeded — A xong" in out and "task_b" not in out
    assert "inbox: acknowledged 1 report(s) of task_a" in out
    assert inbox["acks"] == []

    assert cli.main(["--root", str(repo), "inbox"]) == 0
    out = capsys.readouterr().out
    assert "task_b: succeeded — B xong" in out and "task_a" not in out

    assert cli.main(["--root", str(repo), "inbox", "--ack", "--task-id", "task_b"]) == 0
    assert "inbox: acknowledged 1 report(s) of task_b" in capsys.readouterr().out
    assert inbox["acks"] == ["d1"]


def test_ack_without_task_id_acknowledges_everything(repo: Path, inbox: dict, capsys: pytest.CaptureFixture[str]) -> None:
    orca.link(repo, t_id="T-a", task_id="task_a", project=orca.project_name(repo))

    assert cli.main(["--root", str(repo), "inbox", "--ack"]) == 0

    out = capsys.readouterr().out
    assert f"task_a [repo {orca.project_name(repo)}]: succeeded — A xong" in out
    assert "task_b [repo ?]: succeeded — B xong" in out
    assert "inbox: acknowledged 2 report(s)" in out
    assert inbox["acks"] == ["d1"]


def test_task_id_needs_ack(repo: Path, inbox: dict) -> None:
    assert cli.main(["--root", str(repo), "inbox", "--task-id", "task_a"]) == 2


def test_contract_acks_the_merged_task_only() -> None:
    assert "inbox --ack --task-id <id>" in coordinator.contract("src")
