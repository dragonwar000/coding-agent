"""The task graph (FR-022): a validated plan with dependencies, applied to Orca once, dispatched only when dependencies are completed."""

import json
from pathlib import Path

import pytest

from coding_agent import cli, orca_cli, plan

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"

PLAN = """tasks:
  - id: web
    title: Web client
    spec: Build the client
    deps: [protocol, server]
  - id: protocol
    title: Wire protocol
    spec: Define messages
  - id: server
    title: Server
    spec: Implement rooms
    deps: [protocol]
    agent: codex
"""


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    return state


def plan_file(tmp_path: Path, text: str = PLAN) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def orca_state(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def test_nodes_come_back_in_dependency_order(tmp_path):
    assert [node.id for node in plan.load(plan_file(tmp_path))] == ["protocol", "server", "web"]


BAD = {
    "empty": ("tasks: []\n", "non-empty"),
    "duplicate id": ("tasks:\n  - {id: a, title: t, spec: s}\n  - {id: a, title: t, spec: s}\n", "duplicate task id"),
    "unknown dep": ("tasks:\n  - {id: a, title: t, spec: s, deps: [zz]}\n", "unknown task 'zz'"),
    "self dep": ("tasks:\n  - {id: a, title: t, spec: s, deps: [a]}\n", "depends on itself"),
    "cycle": ("tasks:\n  - {id: a, title: t, spec: s, deps: [b]}\n  - {id: b, title: t, spec: s, deps: [a]}\n", "dependency cycle among: a, b"),
    "blank spec": ("tasks:\n  - {id: a, title: t, spec: ''}\n", "spec must be a non-blank string"),
    "deps not a list": ("tasks:\n  - {id: a, title: t, spec: s, deps: b}\n", "deps must be a list"),
    "not yaml": ("tasks: [\n", "not valid YAML"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_a_malformed_plan_is_refused(tmp_path, case):
    text, message = BAD[case]
    with pytest.raises(plan.PlanError, match=message):
        plan.load(plan_file(tmp_path, text))


def test_apply_creates_each_task_once_with_its_orca_dependencies(repo, tmp_path, fake_orca):
    nodes = plan.load(plan_file(tmp_path))
    first = plan.apply(repo, nodes)
    assert [(node_id, created) for node_id, _task, created in first] == [("protocol", True), ("server", True), ("web", True)]
    ids = plan.ledger(repo)
    tasks = orca_state(fake_orca)["tasks"]
    assert json.loads(tasks[ids["protocol"]]["deps"]) == []
    assert json.loads(tasks[ids["server"]]["deps"]) == [ids["protocol"]]
    assert json.loads(tasks[ids["web"]]["deps"]) == [ids["protocol"], ids["server"]]
    assert [tasks[ids[n]]["status"] for n in ("protocol", "server", "web")] == ["ready", "pending", "pending"]
    assert all(not created for _n, _t, created in plan.apply(repo, nodes))
    assert len(orca_state(fake_orca)["tasks"]) == 3


def test_only_nodes_with_completed_dependencies_are_dispatched(repo, tmp_path, fake_orca):
    nodes = plan.load(plan_file(tmp_path))
    plan.apply(repo, nodes)
    ids = plan.ledger(repo)

    started = plan.dispatch_ready(repo, nodes, default_agent="claude")
    assert [node_id for node_id, _t, _d in started] == ["protocol"]
    assert plan.dispatch_ready(repo, nodes, default_agent="claude") == []

    orca_cli.set_status(repo, task_id=ids["protocol"], requested="completed", basis="predicate")
    started = plan.dispatch_ready(repo, nodes, default_agent="claude")
    assert [node_id for node_id, _t, _d in started] == ["server"]
    dispatches = orca_state(fake_orca)["dispatches"]
    assert [d["agent"] for d in dispatches.values()] == ["claude", "codex"]

    orca_cli.set_status(repo, task_id=ids["server"], requested="failed", basis=None)
    assert plan.dispatch_ready(repo, nodes, default_agent="claude") == []


def test_the_limit_caps_workers_started_at_once(repo, tmp_path, fake_orca):
    text = "tasks:\n" + "".join(f"  - {{id: n{i}, title: t{i}, spec: s}}\n" for i in range(4))
    nodes = plan.load(plan_file(tmp_path, text))
    plan.apply(repo, nodes)
    assert len(plan.dispatch_ready(repo, nodes, default_agent="claude", limit=3)) == 3
    assert len(plan.dispatch_ready(repo, nodes, default_agent="claude", limit=3)) == 1


def test_status_lines_show_state_and_open_dependencies(repo, tmp_path, fake_orca):
    nodes = plan.load(plan_file(tmp_path))
    assert plan.states(repo, nodes) == {"protocol": "unapplied", "server": "unapplied", "web": "unapplied"}
    plan.apply(repo, nodes)
    lines = plan.status_lines(nodes, plan.states(repo, nodes))
    assert lines == [
        "[ready] protocol: Wire protocol",
        "[pending] server: Server (chờ: protocol)",
        "[pending] web: Web client (chờ: protocol, server)",
    ]


def test_the_cli_applies_dispatches_and_shows_the_graph(repo, tmp_path, fake_orca, capsys):
    path = plan_file(tmp_path)
    assert cli.main(["--root", str(repo), "plan-apply", str(path)]) == 0
    assert "created protocol -> task_0001" in capsys.readouterr().out
    assert cli.main(["--root", str(repo), "plan-next", str(path)]) == 0
    out = capsys.readouterr().out
    assert "started protocol -> task_0001" in out and "[dispatched] protocol" in out
    assert cli.main(["--root", str(repo), "plan-next", str(path)]) == 0
    assert "no node is ready" in capsys.readouterr().out
    assert cli.main(["--root", str(repo), "plan-status", str(tmp_path / "missing.yaml")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_run_init_stores_the_run_and_later_commands_use_it(repo, fake_orca, monkeypatch, capsys):
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    assert cli.main(["--root", str(repo), "board"]) == 1
    assert "run_required" in capsys.readouterr().err
    assert cli.main(["--root", str(repo), "run-init", "--objective", "build it"]) == 0
    assert json.loads(capsys.readouterr().out) == {"run": "run_created"}
    assert (repo / ".coding-agent" / "orca-run").read_text(encoding="utf-8").strip() == "run_created"
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN", raising=False)
    assert cli.main(["--root", str(repo), "board"]) == 0


def test_an_environment_run_wins_over_the_stored_one(repo, monkeypatch):
    (repo / ".coding-agent").mkdir()
    (repo / ".coding-agent" / "orca-run").write_text("run_stored\n", encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_env")
    orca_cli.use_stored_run(repo)
    assert orca_cli.run_id(None) == "run_env"
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    orca_cli.use_stored_run(repo)
    assert orca_cli.run_id(None) == "run_stored"
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")


def test_the_board_hints_at_run_init_when_no_run_is_bound():
    from coding_agent import coordinator

    assert "run-init" in coordinator.board_context([], "orca task-list answered ok:false: run_required: No Run is bound.")
    assert "run-init" not in coordinator.board_context([], "daemon not running")
