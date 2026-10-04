"""The Orca adapter against the fake CLI: T-id links, project claims, completion proof, CLAIMED-DONE checks (FR-006 to FR-008, FR-013, SC-002)."""

import json
from pathlib import Path

import pytest

from coding_agent import cli, events, orca, orca_cli

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    return state


def orca_tasks(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))["tasks"]


def test_create_records_the_t_id_link_and_the_project_in_the_spec(repo, fake_orca):
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-1", spec="build the thing", title="Build")
    assert task_id == "task_0001"
    link = json.loads((repo / ".coding-agent" / "links.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert link == {"t_id": "T-1", "task_id": "task_0001", "project": "demo-repo"}
    assert orca_tasks(fake_orca)["task_0001"]["spec"].startswith("project: demo-repo\nt_id: T-1\n\nbuild the thing")


def test_a_claim_from_another_repository_is_refused_and_logged(repo, fake_orca):
    task_id = orca_cli.create_task(repo, project="other-repo", t_id="T-2", spec="x", title="x")
    with pytest.raises(orca.ClaimRefused):
        orca_cli.claim(repo, task_id=task_id, current_project="demo-repo")
    log = [json.loads(line) for line in events.log_path(repo).read_text(encoding="utf-8").splitlines()]
    assert log[-1]["guard"] == "orca-project" and log[-1]["kind"] == "claim-refused" and log[-1]["applied"] is True


def test_a_claim_from_the_owning_repository_is_allowed(repo, fake_orca):
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-3", spec="x", title="x")
    orca_cli.claim(repo, task_id=task_id, current_project="demo-repo")


def test_a_task_with_no_recorded_owner_is_refused(repo, fake_orca):
    with pytest.raises(orca.ClaimRefused):
        orca_cli.claim(repo, task_id="task_9999", current_project="demo-repo")


def test_completion_without_proof_never_reaches_orca(repo, fake_orca):
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-4", spec="x", title="x")
    outcome = orca_cli.set_status(repo, task_id=task_id, requested="completed", basis="agentReported")
    assert outcome == orca_cli.Transition(decision="unverified", sent=False, reason="basis agentReported is not proof")
    assert orca_tasks(fake_orca)[task_id]["status"] == "pending"


def test_completion_with_proof_is_sent_with_its_artifacts(repo, fake_orca):
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-5", spec="x", title="x")
    outcome = orca_cli.set_status(repo, task_id=task_id, requested="completed", basis="predicate", artifacts=("out/report.md",))
    assert outcome.decision == "completed" and outcome.sent is True
    stored = orca_tasks(fake_orca)[task_id]
    assert stored["status"] == "completed" and stored["result"] == {"basis": "predicate", "artifacts": ["out/report.md"]}


def test_a_non_completion_status_is_sent_as_given(repo, fake_orca):
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-6", spec="x", title="x")
    assert orca_cli.set_status(repo, task_id=task_id, requested="dispatched", basis=None).sent is True
    assert orca_tasks(fake_orca)[task_id]["status"] == "dispatched"


def test_an_ok_false_answer_is_an_error(repo, fake_orca):
    with pytest.raises(orca_cli.OrcaError, match="ok:false"):
        orca_cli.set_status(repo, task_id="task_missing", requested="blocked", basis=None)


def test_a_failing_cli_is_an_error(repo, fake_orca, monkeypatch):
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    with pytest.raises(orca_cli.OrcaError, match="daemon not running"):
        orca_cli.list_tasks()


def test_task_list_accepts_the_envelope_and_a_bare_list():
    assert orca_cli.tasks_of({"ok": True, "result": {"tasks": [{"id": "a"}]}}) == [{"id": "a"}]
    assert orca_cli.tasks_of([{"id": "b"}, "not a task"]) == [{"id": "b"}]
    with pytest.raises(orca_cli.OrcaError):
        orca_cli.tasks_of({"result": {"other": 1}})


def test_claimed_done_but_absent_fails_the_strict_check(repo, fake_orca, tmp_path, capsys):
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-7", spec="x", title="x")
    orca_cli.set_status(repo, task_id=task_id, requested="completed", basis="verifier", artifacts=("out/missing.md",))
    export = tmp_path / "tasks.json"
    export.write_text(json.dumps({"ok": True, "result": {"tasks": list(orca_tasks(fake_orca).values())}}), encoding="utf-8")

    assert cli.main(["--root", str(repo), "orca-check", "--tasks", str(export), "--strict"]) == 1
    assert "CLAIMED-DONE BUT ABSENT: task_0001 (missing out/missing.md)" in capsys.readouterr().out

    (repo / "out").mkdir()
    (repo / "out" / "missing.md").write_text("done", encoding="utf-8")
    assert cli.main(["--root", str(repo), "orca-check", "--tasks", str(export), "--strict"]) == 0


def test_the_cli_create_status_and_claim_commands_run_end_to_end(repo, fake_orca, capsys):
    assert cli.main(["--root", str(repo), "orca-create", "--t-id", "T-8", "--title", "Ship", "--spec", "ship it"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created == {"t_id": "T-8", "task_id": "task_0001", "project": "demo-repo"}

    assert cli.main(["--root", str(repo), "orca-status", "--task-id", "task_0001", "--status", "completed", "--basis", "agentReported"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "unverified"

    assert cli.main(["--root", str(repo), "claim", "--task-id", "task_0001", "--project", "elsewhere"]) == 1
    assert "refused" in capsys.readouterr().err
    assert cli.main(["--root", str(repo), "claim", "--task-id", "task_0001"]) == 0


def test_status_groups_only_this_repositorys_tasks(repo, fake_orca, capsys):
    mine = orca_cli.create_task(repo, project="demo-repo", t_id="T-20", spec="x", title="Mine")
    theirs = orca_cli.create_task(repo, project="other-repo", t_id="T-21", spec="y", title="Theirs")
    orca_cli.set_status(repo, task_id=mine, requested="dispatched", basis=None)
    orca_cli.set_status(repo, task_id=theirs, requested="dispatched", basis=None)
    assert cli.main(["--root", str(repo), "status"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["project"] == "demo-repo"
    assert view["counts"]["running"] == 1
    assert [t["id"] for t in view["tasks"]["running"]] == [mine]
    assert all(t["id"] != theirs for group in view["tasks"].values() for t in group)


def test_status_reports_a_failing_orca_cli(repo, fake_orca, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    assert cli.main(["--root", str(repo), "status"]) == 1
    assert "daemon not running" in capsys.readouterr().err


def test_without_a_run_the_cli_answers_run_required(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    with pytest.raises(orca_cli.OrcaError, match="run_required"):
        orca_cli.list_tasks()
    assert orca_cli.list_tasks(run="run_explicit") == []


def test_an_explicit_run_overrides_the_environment(repo, fake_orca, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_env")
    assert orca_cli.run_id(None) == "run_env" and orca_cli.run_id("run_arg") == "run_arg"
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    assert orca_cli.run_id(None) is None
