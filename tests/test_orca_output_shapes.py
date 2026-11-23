"""Orca 1.4.271 prints a concise view unless `--full` is passed; older Orca refuses that flag. Both must work."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coding_agent import orca_cli, plan, roots

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
SHAPES = {"1.4.271 concise": {"FAKE_ORCA_CONCISE": "1"}, "older, no --full": {"FAKE_ORCA_NO_FULL": "1"}, "plain": {}}


@pytest.fixture(params=list(SHAPES))
def shape(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca.json"
    state.write_text(json.dumps({"tasks": {}}), encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_a")
    for name, value in SHAPES[request.param].items():
        monkeypatch.setenv(name, value)
    return state


def _data(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def _create(root: Path, spec: str = "do it", t_id: str = "T-1") -> str:
    return orca_cli.create_task(root, project="demo", t_id=t_id, spec=spec, title="A task")


def test_a_task_is_created_and_listed_with_its_fields(shape: Path, tmp_path: Path) -> None:
    task = _create(tmp_path)
    assert task == "task_0001" and len(_data(shape)["tasks"]) == 1
    listed = orca_cli.list_tasks()
    assert listed[0]["id"] == task and listed[0]["status"] == "ready"


def test_reports_keep_their_payload(shape: Path) -> None:
    payload = json.dumps({"taskId": "task_1", "dispatchId": "ctx_1", "outcome": "succeeded"})
    data = _data(shape)
    data["messages"] = [{"id": "m1", "type": "worker_done", "subject": "done", "body": "b", "payload": payload}]
    shape.write_text(json.dumps(data), encoding="utf-8")
    if "FAKE_ORCA_CONCISE" in __import__("os").environ:
        assert "payload" not in orca_cli._run_orca_once("orchestration", "check", "--peek", "--run", "run_a")["result"]["messages"][0]
    assert orca_cli.unread_reports("run_a")[0]["task_id"] == "task_1"


def test_worker_observation_is_read(shape: Path) -> None:
    data = _data(shape)
    data["dispatches"] = {"ctx_1": {"task": "task_1", "observation": {"status": "live", "exactWorker": True, "agentWait": None}, "activity": "done"}}
    shape.write_text(json.dumps(data), encoding="utf-8")
    assert orca_cli.worker_observation("ctx_1") == {"status": "live", "exact": True, "wait_known": True, "agent_wait": None, "activity": "done"}


def test_the_bound_run_is_read(shape: Path) -> None:
    data = _data(shape)
    data["bound_run"] = {"id": "run_bound"}
    shape.write_text(json.dumps(data), encoding="utf-8")
    assert orca_cli.bound_run() == "run_bound"


def test_worker_list_rows_keep_their_worktree(shape: Path) -> None:
    data = _data(shape)
    data["workers"] = [{"dispatchId": "ctx_1", "taskId": "task_1", "workerState": "succeeded", "agentTerminalHandle": "t", "terminalState": "retained",
                        "resource": {"worktreeId": "repo1::/tmp/w1"}, "projection": {"workspace": {"id": "repo1::/tmp/w1"}}}]
    shape.write_text(json.dumps(data), encoding="utf-8")
    assert roots.task_worktrees("run_a") == {"task_1": [("repo1", "/tmp/w1")]}


@pytest.mark.parametrize("answer,expected", [
    ({"result": {"task": {"id": "task_a"}}}, "task_a"),
    ({"result": {"taskId": "task_b", "status": "ready"}}, "task_b"),
])
def test_task_id_is_read_from_both_shapes(answer: dict, expected: str) -> None:
    assert orca_cli.task_id_of(answer) == expected


@pytest.mark.parametrize("answer", [{}, {"result": {}}, {"id": "uuid-of-the-envelope", "result": {"status": "ready"}}, None])
def test_the_envelope_id_is_never_taken_for_a_task_id(answer: object) -> None:
    with pytest.raises(orca_cli.OrcaError, match="no result.task.id"):
        orca_cli.task_id_of(answer)


def test_a_create_whose_id_cannot_be_read_is_replayed_not_duplicated(shape: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ORCA_UNREADABLE_CREATE", "1")
    for _ in range(2):
        with pytest.raises(orca_cli.OrcaError, match="may exist"):
            _create(tmp_path)
    assert len(_data(shape)["tasks"]) == 1
    monkeypatch.delenv("FAKE_ORCA_UNREADABLE_CREATE")
    assert _create(tmp_path) == "task_0001"
    assert len(_data(shape)["tasks"]) == 1 and _data(shape)["replays"] == 2
    assert orca_cli._creates(tmp_path) == {}


def test_a_retry_keeps_the_durable_id_of_the_first_attempt(shape: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ORCA_UNREADABLE_CREATE", "1")
    with pytest.raises(orca_cli.OrcaError):
        _create(tmp_path, t_id="T-first")
    monkeypatch.delenv("FAKE_ORCA_UNREADABLE_CREATE")
    assert _create(tmp_path, t_id="T-second") == "task_0001"  # `delegate` makes a new t_id on each call
    assert len(_data(shape)["tasks"]) == 1 and "t_id: T-first" in _data(shape)["tasks"]["task_0001"]["spec"]


def test_after_a_confirmed_create_the_same_request_is_a_new_task(shape: Path, tmp_path: Path) -> None:
    assert _create(tmp_path) == "task_0001"
    assert _create(tmp_path) == "task_0002"


def test_plan_apply_makes_no_duplicate_after_an_unreadable_answer(shape: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    nodes = [plan.Node(id="a", title="A", spec="spec a", deps=(), agent=None)]
    monkeypatch.setenv("FAKE_ORCA_UNREADABLE_CREATE", "1")
    with pytest.raises(orca_cli.OrcaError):
        plan.apply(repo, nodes)
    monkeypatch.delenv("FAKE_ORCA_UNREADABLE_CREATE")
    assert plan.apply(repo, nodes) == [("a", "task_0001", True)]
    assert plan.apply(repo, nodes) == [("a", "task_0001", False)]
    assert len(_data(shape)["tasks"]) == 1


def test_a_refusal_that_applied_nothing_starts_fresh(shape: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(orca_cli.RUN_ENV)
    with pytest.raises(orca_cli.OrcaRefused, match="run_required"):
        _create(tmp_path)
    assert orca_cli._creates(tmp_path) == {}


def test_an_orca_that_crashes_keeps_the_request_for_the_retry(shape: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    with pytest.raises(orca_cli.OrcaError):
        _create(tmp_path)
    kept = list(orca_cli._creates(tmp_path).values())
    assert len(kept) == 1
    monkeypatch.delenv("FAKE_ORCA_FAIL")
    assert _create(tmp_path) == "task_0001" and len(_data(shape)["tasks"]) == 1


def test_a_run_whose_id_cannot_be_read_is_not_created_twice(shape: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_ORCA_UNREADABLE_CREATE", "1")
    with pytest.raises(orca_cli.OrcaError, match="no run id"):
        orca_cli.run_init(tmp_path, "objective")
    monkeypatch.delenv("FAKE_ORCA_UNREADABLE_CREATE")
    assert orca_cli.run_init(tmp_path, "objective") == "run_created"
    assert _data(shape)["run_creates"] == 1


def test_a_cli_without_retry_request_is_never_sent_a_blind_second_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def old_cli(*args: str) -> dict:
        calls.append(args)
        if "--retry-request" in args:
            raise orca_cli.OrcaRefused("orca task-create answered ok:false: invalid_argument: Unknown flag --retry-request for command")
        return {"ok": True, "result": {"accepted": True}}

    monkeypatch.setattr(orca_cli, "_call", old_cli)
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_a")
    with pytest.raises(orca_cli.OrcaError, match="may exist"):
        _create(tmp_path)
    plain = len([call for call in calls if "--retry-request" not in call])
    with pytest.raises(orca_cli.OrcaError, match="cannot replay"):
        _create(tmp_path)
    assert plain == 1 and len([call for call in calls if "--retry-request" not in call]) == 1


def test_full_is_asked_for_once_per_process_on_an_older_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "orca.json"
    state.write_text(json.dumps({"tasks": {}}), encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("FAKE_ORCA_NO_FULL", "1")
    real = orca_cli._run_orca_once
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(orca_cli, "_run_orca_once", lambda *w, timeout=60: (sent.append(w), real(*w, timeout=timeout))[1])
    for _ in range(2):
        orca_cli.list_tasks("run_a")
    assert [("--full" in words) for words in sent] == [True, False, False]


def test_commands_outside_orchestration_get_no_full_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(orca_cli, "_run_orca_once", lambda *w, timeout=60: sent.append(w) or {"ok": True})
    orca_cli._run_orca("terminal", "show", "--terminal", "t")
    assert "--full" not in sent[0]
