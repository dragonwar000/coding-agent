"""The coordinator decides each task's tests and each worker's model (user request, 2026-11-04).

- stop-gate: a coordinator session runs no verification (`skipped`, reason `coordinator decides tests`); a worker
  session runs its task's own `verify` commands; no commands is `skipped` and never blocks; pytest's exit 5 is `skipped`.
- model: `delegate --model`, a plan node's `model:`; claude takes `--model`, codex `-m`, another agent is logged as
  not applied; the model is kept in the workers ledger, the events, and on the board.
- tier (user request, 2026-11-12): a claude worker cannot start without a model, `sonnet` or `opus` by the rule; a
  full id is taken too; `CODING_AGENT_WORKER_MODEL` supplies one when none is given; codex is unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coding_agent import cli, coordinator, events, orca_cli, plan
from coding_agent.hooks import handlers, run as run_hook

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
GUARD = {"id": "coordinator-guard", "host": ["claude_code"], "event": "PreToolUse", "matcher": "Write|Edit|Bash",
         "mode": "enforce", "assumption": "the coordinator writes through the graph"}


def _events(root: Path, guard: str) -> list[dict]:
    path = events.log_path(root)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [item for item in map(json.loads, lines) if item["guard"] == guard]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)


@pytest.fixture
def repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    monkeypatch.delenv(orca_cli.PERMISSION_ENV, raising=False)
    return repo


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state_path))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.setenv("CODING_AGENT_SHARE_TRUST", "off")  # so only the model decides whether the agent gets its own terminal
    monkeypatch.delenv("FAKE_ORCA_SCREEN", raising=False)
    monkeypatch.setattr(orca_cli, "AGENT_READY_POLL_S", 0)
    return state_path


def _orca(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _task(repo: Path) -> str:
    return orca_cli.create_task(repo, project="demo-repo", t_id="T-vm", spec="x", title="Fix the thing")


# --- agent_command -------------------------------------------------------------------------------------------------


def test_claude_takes_the_model_flag():
    assert orca_cli.agent_command("claude", model="sonnet", windows=False) == "CODING_AGENT_ROLE=worker claude --model sonnet"
    assert orca_cli.agent_command("claude", "acceptEdits", "opus", windows=True) == "claude --permission-mode acceptEdits --model opus"


def test_codex_takes_dash_m():
    assert orca_cli.agent_command("codex", model="gpt-5.1-codex", windows=False) == "CODING_AGENT_ROLE=worker codex -m gpt-5.1-codex"
    assert orca_cli.agent_command("codex", model="o3", windows=True) == "codex -m o3"


def test_another_agent_takes_no_model_flag():
    assert orca_cli.agent_command("gemini", model="sonnet", windows=False) == "CODING_AGENT_ROLE=worker gemini"


def test_no_model_keeps_the_old_command():
    assert orca_cli.agent_command("claude", windows=False) == "CODING_AGENT_ROLE=worker claude"
    assert orca_cli.agent_command("claude", "plan", None, windows=True) == "claude --permission-mode plan"


def test_a_model_with_glob_characters_is_quoted_for_the_shell():
    assert orca_cli.agent_command("claude", model="opus[1m]", windows=False) == "CODING_AGENT_ROLE=worker claude --model 'opus[1m]'"


@pytest.mark.parametrize("bad", ["opus; rm -rf .", "son net", "$(id)", "-model"])
def test_a_model_name_with_shell_characters_is_refused(bad):
    with pytest.raises(orca_cli.OrcaError, match="not a model name"):
        orca_cli.check_model(bad)


# --- worker_start / delegate with a model and verify -----------------------------------------------------------------


def test_a_claude_worker_with_a_model_starts_in_its_own_terminal(repo: Path, fake_orca: Path) -> None:
    dispatch = orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="Fix the thing", model="haiku",
                                     verify=("python3 -m pytest -q tests/test_a.py",))
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] is None
    assert orca["created_terminals"][0]["command"] == orca_cli.agent_command("claude", None, "haiku")
    record = orca_cli.worker_records(repo)[0]
    assert record["model"] == "haiku" and record["verify"] == ["python3 -m pytest -q tests/test_a.py"] and record["dispatch"] == dispatch
    delegated = [e for e in _events(repo, "coordinator") if e["kind"] == "delegated"][-1]["detail"]
    assert delegated["model"] == "haiku" and delegated["verify"] == ["python3 -m pytest -q tests/test_a.py"]
    started = [e for e in _events(repo, "permission-mode") if e["kind"] == "worker-started"][-1]["detail"]
    assert started["model"] == "haiku"


def test_a_codex_worker_gets_dash_m(repo: Path, fake_orca: Path) -> None:
    orca_cli.worker_start(repo, task_id=_task(repo), agent="codex", title="x", model="o3")
    assert _orca(fake_orca)["created_terminals"][0]["command"] == orca_cli.agent_command("codex", None, "o3")


def test_another_agent_ignores_the_model_and_logs_it(repo: Path, fake_orca: Path) -> None:
    orca_cli.worker_start(repo, task_id=_task(repo), agent="gemini", title="x", model="sonnet")
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] == "gemini" and "created_terminals" not in orca
    skipped = [e for e in _events(repo, "model") if e["kind"] == "not-applied"]
    assert skipped and skipped[0]["detail"]["agent"] == "gemini" and skipped[0]["detail"]["model"] == "sonnet"
    assert "model" not in orca_cli.worker_records(repo)[0]
    assert [e for e in _events(repo, "coordinator") if e["kind"] == "delegated"][-1]["detail"]["model"] == "agent-default"


def test_a_codex_worker_without_a_model_keeps_the_agent_start(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(orca_cli.WORKER_MODEL_ENV, "opus")  # names a Claude model: not read for codex
    orca_cli.worker_start(repo, task_id=_task(repo), agent="codex", title="x")
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] == "codex" and "created_terminals" not in orca
    record = orca_cli.worker_records(repo)[0]
    assert "model" not in record and "verify" not in record
    assert [e for e in _events(repo, "permission-mode") if e["kind"] == "worker-started"][-1]["detail"]["model"] == "agent-default"


def test_delegate_refuses_a_bad_model_before_any_task_exists(repo: Path, fake_orca: Path) -> None:
    with pytest.raises(orca_cli.OrcaError, match="not a model name"):
        orca_cli.delegate(repo, title="x", spec="y", t_id="T-bad", agent="claude", model="a b")
    assert not fake_orca.exists() or "tasks" not in _orca(fake_orca)


def test_the_head_record_keeps_model_and_verify(repo: Path, fake_orca: Path) -> None:
    orca_cli.worker_start(repo, task_id=_task(repo), agent="claude", title="x", model="opus", verify=("make test",))
    orca_cli.record_head(repo, orca_cli.worker_records(repo)[0], "abc123")
    record = orca_cli.worker_records(repo)[0]
    assert record["head"] == "abc123" and record["model"] == "opus" and record["verify"] == ["make test"]


def test_the_cli_passes_model_and_verify_to_delegate(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    seen: dict = {}

    def fake_delegate(root, **kwargs):
        seen.update(kwargs)
        return "task_1", "ctx_1"

    monkeypatch.setattr(orca_cli, "delegate", fake_delegate)
    monkeypatch.setenv("CODING_AGENT_AUTO_CLEAN", "off")
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.setattr(orca_cli, "sync_bound_run", lambda root, session="cli": None)
    monkeypatch.setattr(orca_cli, "register_root", lambda coordinator, root: False)
    code = cli.main(["--root", str(repo), "delegate", "--title", "t", "--spec", "s", "--model", "sonnet",
                     "--verify", "go test ./pkg/...", "--verify", "npm test -- web"])
    assert code == 0
    assert seen["model"] == "sonnet" and seen["verify"] == ("go test ./pkg/...", "npm test -- web")


# --- plan.yaml -----------------------------------------------------------------------------------------------------


def _plan(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "plan.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_plan_node_reads_model_and_verify(tmp_path: Path) -> None:
    nodes = plan.load(_plan(tmp_path, "tasks:\n  - id: a\n    title: t\n    spec: s\n    model: haiku\n"
                                      "    verify:\n      - go test ./pkg/...\n      - npm test\n  - {id: b, title: t, spec: s, agent: codex}\n"))
    assert (nodes[0].model, nodes[0].verify) == ("haiku", ("go test ./pkg/...", "npm test"))
    assert (nodes[1].model, nodes[1].verify) == (None, ())


@pytest.mark.parametrize("text, message", [
    ("tasks:\n  - {id: a, title: t, spec: s, verify: [1]}\n", "verify must be a list of commands"),
    ("tasks:\n  - {id: a, title: t, spec: s, verify: ['']}\n", "verify must be a list of commands"),
    ("tasks:\n  - {id: a, title: t, spec: s, model: 'a; b'}\n", "not a model name"),
])
def test_a_malformed_model_or_verify_is_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(plan.PlanError, match=message):
        plan.load(_plan(tmp_path, text))


def test_plan_next_passes_the_node_model_and_verify(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = plan.parse({"tasks": [{"id": "a", "title": "t", "spec": "s", "model": "opus", "verify": ["make check"]}]})
    monkeypatch.setattr(plan, "ledger", lambda root: {"a": "task_a"})
    monkeypatch.setattr(plan, "states", lambda root, nodes, run=None: {"a": "pending"})
    calls: list[dict] = []
    monkeypatch.setattr(orca_cli, "worker_start", lambda root, **kwargs: calls.append(kwargs) or "ctx_a")
    assert plan.dispatch_ready(repo, nodes, default_agent="claude") == [("a", "task_a", "ctx_a", "opus")]
    assert calls[0]["model"] == "opus" and calls[0]["verify"] == ("make check",)


# --- the tier is required for a claude worker ------------------------------------------------------------------------


def test_delegate_without_a_model_is_refused_before_any_task_exists(repo: Path, fake_orca: Path) -> None:
    with pytest.raises(orca_cli.OrcaError) as refused:
        orca_cli.delegate(repo, title="x", spec="y", t_id="T-none", agent="claude")
    message = str(refused.value)
    assert "worker claude phải có model" in message and "`sonnet`" in message and "`opus`" in message
    assert "`--model sonnet|opus`" in message and "`model: sonnet|opus`" in message and orca_cli.WORKER_MODEL_ENV in message
    assert not fake_orca.exists() or "tasks" not in _orca(fake_orca)


@pytest.mark.parametrize("argv", [
    ["delegate", "--title", "t", "--spec", "s"],
    ["delegate", "--title", "t", "--spec", "s", "--agent", "claude", "--model", " "],
    ["worker-start", "--task-id", "task_0001"],
])
def test_the_cli_refuses_a_claude_worker_without_a_model(repo: Path, fake_orca: Path, capsys, argv: list[str]) -> None:
    assert cli.main(["--root", str(repo), *argv]) == 1
    err = capsys.readouterr().err
    assert "worker claude phải có model" in err and "--model sonnet|opus" in err
    assert not fake_orca.exists() or not {"tasks", "created_worktrees", "dispatches"} & set(_orca(fake_orca))


def test_worker_start_without_a_model_creates_no_worktree(repo: Path, fake_orca: Path) -> None:
    task_id = _task(repo)
    with pytest.raises(orca_cli.OrcaError, match="worker claude phải có model"):
        orca_cli.worker_start(repo, task_id=task_id, agent="claude", title="x")
    assert "created_worktrees" not in _orca(fake_orca) and orca_cli.worker_records(repo) == []


@pytest.mark.parametrize("node", [
    {"id": "a", "title": "t", "spec": "s"},
    {"id": "a", "title": "t", "spec": "s", "agent": "claude"},
])
def test_a_claude_plan_node_without_a_model_is_refused_by_name(node: dict) -> None:
    with pytest.raises(plan.PlanError, match=r"tasks\[a\]\.model: worker claude phải có model.*`model: sonnet\|opus`"):
        plan.parse({"tasks": [{"id": "first", "title": "t", "spec": "s", "model": "opus"}, node]})


def test_a_plan_without_a_model_creates_no_task(repo: Path, tmp_path: Path, fake_orca: Path, capsys) -> None:
    path = _plan(tmp_path, "tasks:\n  - {id: a, title: t, spec: s}\n")
    for command in ("plan-apply", "plan-next", "plan-status"):
        assert cli.main(["--root", str(repo), command, str(path)]) == 1
        assert "tasks[a].model: worker claude phải có model" in capsys.readouterr().err
    assert not fake_orca.exists()


@pytest.mark.parametrize("model", ["sonnet", "opus", "claude-opus-5-5", "haiku"])
def test_a_tier_a_full_id_or_another_alias_is_accepted(repo: Path, fake_orca: Path, model: str) -> None:
    task_id, _dispatch = orca_cli.delegate(repo, title="x", spec="y", t_id="T-ok", agent="claude", model=model)
    assert _orca(fake_orca)["created_terminals"][0]["command"] == orca_cli.agent_command("claude", None, model)
    assert coordinator.worker_models([repo]) == {task_id: model}
    assert [node.model for node in plan.parse({"tasks": [{"id": "a", "title": "t", "spec": "s", "model": model}]})] == [model]


def test_the_env_model_is_used_when_none_is_given(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(orca_cli.WORKER_MODEL_ENV, " claude-sonnet-5-5 ")
    assert orca_cli.worker_model("claude") == "claude-sonnet-5-5" and orca_cli.worker_model("claude", "opus") == "opus"
    assert cli.main(["--root", str(repo), "delegate", "--title", "t", "--spec", "s"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"] == "claude-sonnet-5-5"
    assert _orca(fake_orca)["created_terminals"][0]["command"] == orca_cli.agent_command("claude", None, "claude-sonnet-5-5")
    assert orca_cli.worker_records(repo)[0]["model"] == "claude-sonnet-5-5"
    # A plan node without `model:` is taken as it is, and gets the same model when its worker starts.
    assert [node.model for node in plan.parse({"tasks": [{"id": "a", "title": "t", "spec": "s"}]})] == [None]


def test_a_malformed_env_model_is_refused_by_name(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(orca_cli.WORKER_MODEL_ENV, "a; b")
    with pytest.raises(orca_cli.OrcaError, match=f"{orca_cli.WORKER_MODEL_ENV}: .*not a model name"):
        orca_cli.delegate(repo, title="x", spec="y", t_id="T-env", agent="claude")
    assert not fake_orca.exists() or "tasks" not in _orca(fake_orca)


def test_codex_and_other_agents_need_no_model(repo: Path, fake_orca: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    assert orca_cli.worker_model("codex") is None and orca_cli.worker_model("gemini") is None
    assert orca_cli.shown_model("codex") == "agent-default" and orca_cli.shown_model("codex", "o3") == "o3"
    assert orca_cli.shown_model("gemini", "sonnet") == "agent-default"
    nodes = plan.parse({"tasks": [{"id": "a", "title": "t", "spec": "s", "agent": "codex"}]})
    assert (nodes[0].agent, nodes[0].model) == ("codex", None)
    assert plan.parse({"tasks": [{"id": "a", "title": "t", "spec": "s"}]}, default_agent="codex")[0].model is None
    monkeypatch.setenv("CODING_AGENT_WORKER_AGENT", "codex")
    assert plan.parse({"tasks": [{"id": "a", "title": "t", "spec": "s"}]})[0].model is None
    assert cli.main(["--root", str(repo), "delegate", "--title", "t", "--spec", "s"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "agent-default"
    orca = _orca(fake_orca)
    assert orca["created_worktrees"][0]["agent"] == "codex" and "created_terminals" not in orca


def test_delegate_and_worker_start_print_the_model(repo: Path, fake_orca: Path, capsys) -> None:
    assert cli.main(["--root", str(repo), "delegate", "--title", "Hard one", "--spec", "s", "--model", "opus"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "opus"
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-ws", spec="x", title="Easy one")
    assert cli.main(["--root", str(repo), "worker-start", "--task-id", task_id, "--model", "sonnet"]) == 0
    assert json.loads(capsys.readouterr().out) == {"task_id": task_id, "dispatch": orca_cli.worker_records(repo)[-1]["dispatch"], "model": "sonnet"}


def test_plan_next_prints_each_started_worker_model(repo: Path, tmp_path: Path, fake_orca: Path, capsys) -> None:
    path = _plan(tmp_path, "tasks:\n  - {id: easy, title: Rename it, spec: s, model: sonnet}\n"
                           "  - {id: hard, title: Auth guard, spec: s, model: opus}\n"
                           "  - {id: other, title: Codex job, spec: s, agent: codex}\n")
    assert cli.main(["--root", str(repo), "plan-apply", str(path)]) == 0
    capsys.readouterr()
    assert cli.main(["--root", str(repo), "plan-next", str(path), "--max", "3"]) == 0
    started = [line for line in capsys.readouterr().out.splitlines() if line.startswith("started ")]
    assert [(line.split()[1], line.rsplit(" ", 1)[1]) for line in started] == [("easy", "[sonnet]"), ("hard", "[opus]"), ("other", "[agent-default]")]


# --- board ---------------------------------------------------------------------------------------------------------


def test_the_board_shows_the_worker_model(repo: Path, fake_orca: Path) -> None:
    task_id = _task(repo)
    orca_cli.worker_start(repo, task_id=task_id, agent="claude", title="Fix the thing", model="sonnet")
    models = coordinator.worker_models([repo])
    assert models == {task_id: "sonnet"}
    lines = coordinator.board_lines([{"id": task_id, "status": "dispatched", "title": "Fix the thing"}], {task_id: "demo-repo"},
                                    "demo-repo", models=models)
    assert f"  - {task_id} [dispatched] Fix the thing [sonnet]" in lines


def test_a_task_without_a_model_has_no_tag() -> None:
    lines = coordinator.board_lines([{"id": "task_1", "status": "dispatched", "title": "T"}], {"task_1": "demo-repo"}, "demo-repo", models={})
    assert "  - task_1 [dispatched] T" in lines


# --- stop-gate -----------------------------------------------------------------------------------------------------


def _manifest(root: Path, verify: list[str], *, guard: bool) -> None:
    doc = {
        "schema": 1, "verified": False, "python_src": "src",
        "verify": {"commands": verify, "timeout_s": 60, "max_continuations": 2, "when": "always"},
        "memory": {"ingest": "both", "embedder": "hash", "change_tools": ["Write", "Edit", "MultiEdit"],
                   "transient_markers": ["for now"], "max_episode_chars": 6000},
        "hooks": [{"id": "stop-gate", "host": ["claude_code"], "event": "Stop", "mode": "enforce",
                   "assumption": "a verification command is the evidence"}] + ([GUARD] if guard else []),
    }
    (root / "integration.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def _stop(root: Path, monkeypatch: pytest.MonkeyPatch, session: str = "s1"):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("CODING_AGENT_MODE_STOP_GATE", "enforce")
    return run_hook("stop-gate", {"session_id": session, "cwd": str(root)}, handlers.stop_gate)


def _verdicts(root: Path) -> list[dict]:
    return [e for e in _events(root, "stop-gate") if e["kind"] in ("ok", "not-ok", "skipped", "no-change", "blocked")]


def test_the_coordinator_session_runs_no_verification(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _manifest(repo, ["false"], guard=True)
    result = _stop(repo, monkeypatch)
    assert result.code == 0
    verdict = _verdicts(repo)[-1]
    assert verdict["kind"] == "skipped" and verdict["applied"] is False
    assert verdict["detail"]["reason"] == "coordinator decides tests" and verdict["detail"]["source"] == "coordinator"


@pytest.fixture
def worker_tree(repo: Path, tmp_path: Path) -> Path:
    tree = tmp_path / "worker-tree"
    _git(repo, "worktree", "add", "-q", "-b", "worker", str(tree))
    return tree


def _worker(repo: Path, tree: Path, verify: tuple[str, ...]) -> None:
    orca_cli.record_worker(repo, worktree=str(tree), task_id="task_w", dispatch="ctx_w", verify=verify)
    _manifest(tree, ["false"], guard=True)


def test_a_worker_runs_its_task_verify_and_a_failure_blocks(repo: Path, worker_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, worker_tree, ("sh -c 'echo task check broke >&2; exit 1'",))
    assert coordinator.role_for(worker_tree) == "worker"
    result = _stop(worker_tree, monkeypatch)
    assert result.code == 2 and "task check broke" in result.stderr


def test_a_worker_whose_task_verify_passes_is_ok(repo: Path, worker_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, worker_tree, ("true", "sh -c 'exit 0'"))
    result = _stop(worker_tree, monkeypatch)
    assert result.code == 0
    verdict = _verdicts(worker_tree)[-1]
    assert verdict["kind"] == "ok" and verdict["detail"] == {"commands": 2, "source": "task"}


def test_a_worker_task_without_verify_is_skipped_not_blocked(repo: Path, worker_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _worker(repo, worker_tree, ())
    result = _stop(worker_tree, monkeypatch)
    assert result.code == 0
    verdict = _verdicts(worker_tree)[-1]
    assert verdict["kind"] == "skipped" and verdict["detail"]["reason"] == "task declares no verify"


def test_empty_manifest_commands_are_skipped_not_blocked(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_ROLE", "maintainer")
    _manifest(repo, [], guard=True)
    result = _stop(repo, monkeypatch)
    assert result.code == 0
    verdict = _verdicts(repo)[-1]
    assert verdict["kind"] == "skipped" and verdict["detail"]["reason"] == "verify.commands is empty"


def test_pytest_exit_5_is_skipped_not_a_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_ROLE", "maintainer")
    _manifest(repo, [f"{sys.executable} -m pytest -q -p no:cacheprovider"], guard=False)
    result = _stop(repo, monkeypatch)
    assert result.code == 0
    verdict = _verdicts(repo)[-1]
    assert verdict["kind"] == "skipped" and verdict["detail"]["reason"] == "pytest ran no tests (exit 5)"


def test_exit_5_of_another_command_still_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_ROLE", "maintainer")
    _manifest(repo, ["sh -c 'exit 5'"], guard=False)
    assert _stop(repo, monkeypatch).code == 2


def test_pytest_exit_5_does_not_hide_a_later_failure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_ROLE", "maintainer")
    _manifest(repo, [f"{sys.executable} -m pytest -q -p no:cacheprovider", "false"], guard=False)
    assert _stop(repo, monkeypatch).code == 2


def test_verdict_rules():
    no_tests = {"command": "pytest", "exit": 5, "skipped": True}
    assert handlers._verdict([no_tests]) == "skipped"
    assert handlers._verdict([no_tests, {"command": "true", "exit": 0}]) == "ok"
    assert handlers._verdict([{"command": "x", "exit": None}]) == "not-ok"


# --- contract and shipped manifest ---------------------------------------------------------------------------------


def test_the_contract_states_both_rules_and_the_delegate_form():
    text = coordinator.contract("src")
    assert "Test: bạn quyết định." in text and "`go test ./pkg/...`" in text
    assert "- " + coordinator.MODEL_RULE in text
    rule = coordinator.MODEL_RULE
    assert "với MỌI task" in rule and "`sonnet` cho việc máy móc hoặc spec rõ" in rule and "`opus` cho việc khó/rủi ro cao" in rule
    assert "Chưa chắc → `opus`" in rule and "BẮT BUỘC với worker claude" in rule and "`--model sonnet|opus` / `model: sonnet|opus`" in rule
    assert "haiku" not in rule and "model mặc định" not in rule
    assert 'delegate --title "..." --spec "..." --model sonnet|opus [--agent claude|codex] [--verify CMD]' in text
    assert "(tasks: id, title, spec, deps, model; tuỳ chọn verify)" in text


def test_the_shipped_manifest_runs_no_default_verify():
    from coding_agent.manifest import load

    assert load(Path(__file__).resolve().parents[1] / "integration.yaml").verify_commands == ()


def test_a_worker_with_no_ledger_record_falls_back_to_the_manifest(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT_ROLE", "worker")
    _manifest(repo, ["false"], guard=True)
    assert _stop(repo, monkeypatch).code == 2
