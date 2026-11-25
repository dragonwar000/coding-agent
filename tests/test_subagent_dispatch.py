"""`coordinator.dispatch: subagent`: the setting, the guard, the refusals, the ledger and the board.

Payload shapes follow what Claude Code 2.1.330 sent to a logging hook (see the `subagents` module docstring).
"""

import json
from pathlib import Path

import pytest
import yaml

from coding_agent import cli, coordinator, gen, subagents
from coding_agent.manifest import ManifestError, coordinator_from, load
from test_hooks import call, manifest

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
GUARD = {"id": "coordinator-guard", "host": ["claude_code"], "event": "PreToolUse",
         "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash|Agent|Task", "mode": "enforce", "assumption": "graph is mandatory"}
PLAN = {"tasks": [
    {"id": "first", "title": "First", "spec": "Do the first thing", "model": "sonnet", "verify": ["pytest -q"]},
    {"id": "second", "title": "Second", "spec": "Do the second thing", "model": "opus", "deps": ["first"]},
]}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("CODING_AGENT_ROLE", "CODING_AGENT_COORDINATOR_GIT", subagents.MINUTES_ENV):
        monkeypatch.delenv(name, raising=False)


def project(repo: Path, dispatch: str | None = "subagent") -> Path:
    manifest(repo, verify=[])
    path = repo / "integration.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["hooks"].append(GUARD)
    doc.pop("coordinator", None)
    if dispatch is not None:
        doc["coordinator"] = {"dispatch": dispatch}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return repo


def tool(repo: Path, name: str, tool_input: dict, **extra) -> dict:
    return {"session_id": "main", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": name,
            "tool_use_id": "toolu_x", "tool_input": tool_input, **extra}


SUBAGENT = {"agent_id": "a123", "agent_type": "general-purpose"}


def assigned(use_id: str, description: str = "job", session: str = "main", **tool_input) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": session, "tool_use_id": use_id,
            "transcript_path": f"/nonexistent/{session}.jsonl",
            "tool_input": {"description": description, "subagent_type": "general-purpose", "model": "sonnet", **tool_input}}


def started(agent: str, session: str = "main") -> dict:
    return {"hook_event_name": "SubagentStart", "session_id": session, "agent_id": agent, "agent_type": "general-purpose"}


def stopped(agent: str, session: str = "main") -> dict:
    return {"hook_event_name": "SubagentStop", "session_id": session, "agent_id": agent, "agent_type": "general-purpose",
            "last_assistant_message": "done"}


def result(use_id: str, agent: str, status: str) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Agent", "session_id": "main", "tool_use_id": use_id,
            "tool_response": {"status": status, "agentId": agent}}


def feed(repo: Path, *payloads, start: float = 1000.0) -> None:
    for offset, payload in enumerate(payloads):
        subagents.record_event(repo, payload, now=start + offset)


def only(repo: Path) -> subagents.Subagent:
    entries = subagents.fold(subagents.read(repo))
    assert len(entries) == 1
    return entries[0]


# The setting


def test_dispatch_defaults_to_subagent_and_is_validated(repo):
    assert load(project(repo, None) / "integration.yaml").coordinator.dispatch == "subagent"
    assert subagents.dispatch_mode(repo) == "subagent"
    assert coordinator_from(None).dispatch == coordinator_from({}).dispatch == "subagent"
    assert coordinator_from({"dispatch": "orca"}).dispatch == "orca"
    assert load(project(repo, "orca") / "integration.yaml").coordinator.dispatch == "orca"
    for bad in ({"dispatch": "threads"}, {"dispatch": "orca", "other": 1}, "subagent"):
        with pytest.raises(ManifestError, match="coordinator"):
            coordinator_from(bad)


def test_the_shipped_files_default_to_subagent_and_declare_the_ledger_hooks():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "integration.yaml", root / "src" / "coding_agent" / "template.yaml"):
        shipped = load(path)
        assert shipped.coordinator.dispatch == "subagent"
        events = {hook.id: (hook.event, hook.matcher) for hook in shipped.hooks if hook.id.startswith("subagent-")}
        assert events == {
            "subagent-assign": ("PreToolUse", "Agent|Task"),
            "subagent-started": ("SubagentStart", None),
            "subagent-stopped": ("SubagentStop", None),
            "subagent-result": ("PostToolUse", "Agent|Task|TaskStop"),
            "subagent-failed": ("PostToolUseFailure", "Agent|Task"),
            "subagent-session-end": ("SessionEnd", None),
        }


def test_the_setting_is_read_at_run_time_and_not_baked_into_the_hook_command(repo):
    orca_mode = gen.render(load(project(repo, "orca") / "integration.yaml"))
    subagent_mode = gen.render(load(project(repo, "subagent") / "integration.yaml"))
    assert orca_mode == subagent_mode
    assert "subagent" not in json.dumps(orca_mode["claude_code"]["hooks"]["PreToolUse"][0])


def test_a_broken_manifest_counts_as_orca(repo):
    (repo / "integration.yaml").write_text("schema: [", encoding="utf-8")
    assert subagents.dispatch_mode(repo) == "orca"
    assert subagents.dispatch_mode(repo / "absent") == "orca"


# The guard


@pytest.mark.parametrize("name,tool_input", [
    ("Write", {"file_path": "x.py", "content": "x"}),
    ("Edit", {"file_path": "x.py", "old_string": "a", "new_string": "b"}),
    ("Bash", {"command": "rm -rf build"}),
    ("Bash", {"command": "echo hi > out.txt"}),
])
def test_the_main_session_still_may_not_write(repo, name, tool_input):
    blocked = call(project(repo), "coordinator-guard", tool(repo, name, tool_input), mode="enforce")
    assert blocked.returncode == 2 and "tool Agent" in blocked.stderr and "delegate" not in blocked.stderr


def test_a_main_session_started_with_agent_is_still_the_main_session(repo):
    payload = tool(project(repo), "Write", {"file_path": "x.py", "content": "x"}, agent_type="foo")
    assert call(repo, "coordinator-guard", payload, mode="enforce").returncode == 2


def test_the_main_session_may_write_the_plan_file(repo):
    payload = tool(project(repo), "Write", {"file_path": ".coding-agent/plan.yaml", "content": "tasks: []"})
    assert call(repo, "coordinator-guard", payload, mode="enforce").returncode == 0


@pytest.mark.parametrize("name,tool_input", [
    ("Write", {"file_path": "x.py", "content": "x"}),
    ("Edit", {"file_path": "x.py", "old_string": "a", "new_string": "b"}),
    ("Bash", {"command": "rm -rf build && echo hi > out.txt"}),
])
def test_a_subagent_may_write(repo, name, tool_input):
    allowed = call(project(repo), "coordinator-guard", tool(repo, name, tool_input, **SUBAGENT), mode="enforce")
    assert allowed.returncode == 0 and allowed.stdout == ""


def test_agent_is_allowed_only_in_subagent_mode(repo):
    payload = tool(project(repo), "Agent", {"description": "job", "prompt": "do it"})
    assert call(repo, "coordinator-guard", payload, mode="enforce").returncode == 0
    project(repo, "orca")
    refused = call(repo, "coordinator-guard", payload, mode="enforce")
    assert refused.returncode == 2 and "delegate" in refused.stderr


def test_orca_mode_ignores_agent_id(repo):
    payload = tool(project(repo, "orca"), "Write", {"file_path": "x.py", "content": "x"}, **SUBAGENT)
    refused = call(repo, "coordinator-guard", payload, mode="enforce")
    assert refused.returncode == 2 and "delegate" in refused.stderr


@pytest.mark.parametrize("extra", [{}, SUBAGENT])
def test_the_destructive_git_rule_binds_both(repo, monkeypatch, extra):
    project(repo)
    payload = tool(repo, "Bash", {"command": "git reset --hard HEAD~1"}, **extra)
    assert call(repo, "coordinator-guard", payload, mode="enforce").returncode == 0
    monkeypatch.setenv(coordinator.COORDINATOR_GIT_ENV, "safe")
    blocked = call(repo, "coordinator-guard", payload, mode="enforce")
    assert blocked.returncode == 2 and "git reset --hard" in blocked.stderr


def test_a_subagent_editing_the_hook_config_still_asks_the_user(repo):
    payload = tool(project(repo), "Write", {"file_path": str(repo / "integration.yaml"), "content": "x"}, **SUBAGENT)
    answer = json.loads(call(repo, "coordinator-guard", payload, mode="enforce").stdout)
    assert answer["hookSpecificOutput"]["permissionDecision"] == "ask"


# delegate and plan-next


@pytest.fixture
def no_orca(tmp_path, monkeypatch):
    """A fake Orca whose state file appears on its first call, so a test can tell it was never called."""
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    return state


@pytest.mark.parametrize("argv", [
    ["delegate", "--title", "t", "--spec", "s", "--model", "sonnet"],
    ["worker-start", "--task-id", "task_1", "--model", "sonnet"],
    ["orca-create", "--t-id", "T-1", "--title", "t", "--spec", "s"],
])
def test_commands_that_create_orca_work_are_refused(repo, no_orca, monkeypatch, capsys, argv):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project(repo)))
    assert cli.main(["--root", str(repo), *argv]) == 2
    err = capsys.readouterr().err
    assert f"{argv[0]}: refused" in err and "Agent tool" in err and "dispatch: subagent" in err
    assert not no_orca.exists()


def test_plan_apply_is_refused(repo, no_orca, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project(repo)))
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump(PLAN), encoding="utf-8")
    assert cli.main(["--root", str(repo), "plan-apply", str(plan)]) == 2
    assert "plan-apply: refused" in capsys.readouterr().err and not no_orca.exists()


def test_the_coordinators_own_setting_refuses_a_delegate_into_another_root(repo, no_orca, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project(repo)))
    other = tmp_path / "other"
    other.mkdir()
    assert cli.main(["--root", str(other), "delegate", "--title", "t", "--spec", "s", "--model", "sonnet"]) == 2
    assert "refused" in capsys.readouterr().err and not no_orca.exists()


def test_plan_next_walks_the_graph_from_the_ledger_without_orca(repo, no_orca, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project(repo)))
    plan = tmp_path / "plan.yaml"
    plan.write_text(yaml.safe_dump(PLAN), encoding="utf-8")

    def next_output() -> str:
        assert cli.main(["--root", str(repo), "plan-next", str(plan)]) == 0
        return capsys.readouterr().out

    out = next_output()
    assert "ready first: First [sonnet]" in out and "spec: Do the first thing" in out and "verify: pytest -q" in out
    assert "ready second" not in out and "[pending] second: Second (chờ: first)" in out

    feed(repo, assigned("u1", "first: do it"), started("a1"))
    out = next_output()
    assert "ready first" not in out and "[running] first: First" in out

    feed(repo, stopped("a1"), result("u1", "a1", "completed"), start=1100.0)
    assert "ready second: Second [opus]" in next_output()
    assert cli.main(["--root", str(repo), "plan-status", str(plan)]) == 0
    assert "[completed] first: First" in capsys.readouterr().out
    assert not no_orca.exists()


def test_a_failed_node_is_ready_again(repo):
    nodes = cli.plan_graph.parse(PLAN)
    feed(repo, assigned("u1", "first: do it"),
         {"hook_event_name": "PostToolUseFailure", "tool_name": "Agent", "session_id": "main", "tool_use_id": "u1", "error": "boom"})
    assert subagents.node_states(repo, nodes)["first"] == "pending"


def test_a_node_id_is_not_matched_by_a_longer_id(repo):
    nodes = cli.plan_graph.parse(PLAN)
    feed(repo, assigned("u1", "first2: another node"))
    assert subagents.node_states(repo, nodes)["first"] == "pending"


def test_orca_mode_still_delegates(repo, no_orca, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project(repo, "orca")))
    monkeypatch.setattr(cli.orca_cli, "delegate", lambda *a, **k: ("task_1", "ctx_1"))
    monkeypatch.setattr(cli, "_start_watch", lambda *a: None)
    monkeypatch.setattr(cli, "_auto_clean", lambda *a: None)
    assert cli.main(["--root", str(repo), "delegate", "--title", "t", "--spec", "s", "--model", "sonnet"]) == 0
    assert "task_1" in capsys.readouterr().out


# The ledger


def test_an_assignment_keeps_what_the_board_needs(repo):
    feed(repo, assigned("u1", "write parser", run_in_background=True, isolation="worktree"))
    entry = only(repo)
    assert (entry.id, entry.description, entry.subagent_type, entry.model, entry.session, entry.ts) == \
        ("u1", "write parser", "general-purpose", "sonnet", "main", 1000.0)
    assert entry.background and entry.isolation == "worktree" and entry.is_open


def test_a_foreground_subagent_closes(repo):
    feed(repo, assigned("u1"), started("a1"), stopped("a1"), result("u1", "a1", "completed"))
    entry = only(repo)
    assert (entry.agent_id, entry.outcome, entry.result) == ("a1", "completed", "done")


def test_a_background_launch_does_not_close_the_record(repo):
    feed(repo, assigned("u1", run_in_background=True), started("a1"), result("u1", "a1", "async_launched"))
    assert only(repo).is_open and only(repo).agent_id == "a1"
    feed(repo, stopped("a1"), start=1100.0)
    assert only(repo).outcome == "completed"


def test_the_hosts_response_says_whether_it_runs_in_the_background(repo):
    feed(repo, assigned("u1"), started("a1"))
    assert not only(repo).background
    feed(repo, result("u1", "a1", "async_launched"), start=1100.0)
    assert only(repo).background


def test_task_stop_closes_a_background_subagent(repo):
    stop = {"hook_event_name": "PostToolUse", "tool_name": "TaskStop", "session_id": "main", "tool_use_id": "u9",
            "tool_input": {"task_id": "a1"}, "tool_response": {"task_type": "local_agent"}}
    feed(repo, assigned("u1", run_in_background=True), started("a1"), result("u1", "a1", "async_launched"), stop)
    assert only(repo).outcome == "stopped"


def test_task_stop_of_another_task_changes_nothing(repo):
    stop = {"hook_event_name": "PostToolUse", "tool_name": "TaskStop", "session_id": "main", "tool_use_id": "u9",
            "tool_input": {"task_id": "shell_7"}, "tool_response": {}}
    feed(repo, assigned("u1"), started("a1"), stop)
    assert only(repo).is_open


def test_a_failed_launch_closes_with_the_error(repo):
    feed(repo, assigned("u1"), {"hook_event_name": "PostToolUseFailure", "tool_name": "Agent", "session_id": "main",
                                "tool_use_id": "u1", "error": "Agent type 'x' not found"})
    entry = only(repo)
    assert entry.outcome == "failed" and "not found" in entry.result


def test_a_start_written_before_its_assignment_still_pairs(repo):
    subagents.record_event(repo, started("a1"), now=1000.0)
    subagents.record_event(repo, assigned("u1"), now=1000.1)
    assert only(repo).agent_id == "a1"


def test_a_start_skips_a_denied_assignment(repo):
    feed(repo, assigned("denied", "denied job"), assigned("real", "real job"), started("a1"))
    by_id = {entry.id: entry for entry in subagents.fold(subagents.read(repo))}
    assert by_id["real"].agent_id == "a1" and by_id["denied"].agent_id is None


def test_the_hosts_link_corrects_a_crossed_pairing(repo):
    feed(repo, assigned("u1", "one"), assigned("u2", "two"), started("a1"), started("a2"),
         stopped("a1"), result("u1", "a1", "completed"))
    by_id = {entry.id: entry for entry in subagents.fold(subagents.read(repo))}
    assert (by_id["u1"].agent_id, by_id["u1"].outcome) == ("a1", "completed")
    assert by_id["u2"].agent_id == "a2" and by_id["u2"].is_open


def test_a_torn_ledger_line_is_skipped(repo):
    feed(repo, assigned("u1"))
    with (repo / subagents.LEDGER).open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "clos\n')
    assert only(repo).id == "u1"


def test_the_ledger_hook_records_only_in_subagent_mode(repo):
    payload = {**assigned("u1"), "cwd": str(repo)}
    assert call(project(repo, "orca"), "subagent-assign", payload, mode="shadow").returncode == 0
    assert not (repo / subagents.LEDGER).exists()
    project(repo, "subagent")
    assert call(repo, "subagent-assign", payload, mode="shadow").returncode == 0
    assert only(repo).description == "job"


# The board


def install_ledger(repo: Path) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "settings.json").write_text('{"c": "python3 -m coding_agent.hooks subagent-assign"}', encoding="utf-8")


def test_the_board_lists_open_subagents_with_their_age(repo):
    install_ledger(repo)
    feed(repo, assigned("u1", "write parser", run_in_background=True), started("a1"))
    text = "\n".join(subagents.board_lines(repo, now=1000.0 + 5 * 60))
    assert "subagent đang mở (1)" in text and "write parser [general-purpose, sonnet, nền]: 5 phút (a1)" in text
    assert "QUÁ" not in text


def test_an_overdue_subagent_is_marked_with_advice(repo):
    install_ledger(repo)
    feed(repo, assigned("u1"), started("a1"))
    text = "\n".join(subagents.board_lines(repo, now=1000.0 + 31 * 60))
    assert "QUÁ 30 PHÚT" in text and "TaskStop task_id=a1" in text


def test_the_overdue_limit_comes_from_the_environment(repo, monkeypatch):
    install_ledger(repo)
    feed(repo, assigned("u1"), started("a1"))
    monkeypatch.setenv(subagents.MINUTES_ENV, "5")
    assert "QUÁ 5 PHÚT" in "\n".join(subagents.board_lines(repo, now=1000.0 + 6 * 60))
    monkeypatch.setenv(subagents.MINUTES_ENV, "off")
    assert "QUÁ" not in "\n".join(subagents.board_lines(repo, now=1000.0 + 600 * 60))
    monkeypatch.setenv(subagents.MINUTES_ENV, "soon")
    assert subagents.overdue_minutes() == subagents.DEFAULT_MINUTES


def test_a_closed_subagent_leaves_the_board(repo):
    install_ledger(repo)
    feed(repo, assigned("u1"), started("a1"), stopped("a1"))
    assert subagents.board_lines(repo, now=1100.0) == ["bảng việc: không có subagent nào đang mở"]


def test_the_board_says_when_the_ledger_hooks_are_not_installed(repo):
    assert "sổ subagent chưa bật" in subagents.board_lines(repo, now=1000.0)[0]


def test_the_board_of_a_subagent_project_does_not_call_orca(repo, no_orca):
    install_ledger(project(repo))
    assert coordinator.read_board(repo) == (["bảng việc: không có subagent nào đang mở"], None)
    assert not no_orca.exists()


# Stale records


def test_session_start_marks_an_ended_sessions_subagent_lost(repo):
    install_ledger(repo)
    feed(repo, assigned("u1", "bg job", session="old", run_in_background=True), started("a1", "old"),
         {"hook_event_name": "SessionEnd", "session_id": "old", "reason": "other"})
    closed = subagents.sweep(repo, "new", "startup", now=1100.0)
    assert [(entry.id, entry.outcome) for entry in closed] == [("u1", "lost")]
    assert "mất dấu" in "\n".join(subagents.board_lines(repo, now=1200.0))
    assert subagents.sweep(repo, "new", "startup", now=1300.0) == []


def test_a_real_stop_after_lost_replaces_it(repo):
    feed(repo, assigned("u1", session="old"), started("a1", "old"),
         {"hook_event_name": "SessionEnd", "session_id": "old", "reason": "other"})
    subagents.sweep(repo, "new", "startup", now=1100.0)
    feed(repo, stopped("a1", "old"), start=1200.0)
    assert only(repo).outcome == "completed"


def test_a_compaction_leaves_the_sessions_own_subagents_open(repo):
    feed(repo, assigned("u1"), started("a1"))
    assert subagents.sweep(repo, "main", "compact", now=1100.0) == []
    assert only(repo).is_open


def test_a_resume_closes_the_records_of_the_process_that_died(repo):
    feed(repo, assigned("u1"), started("a1"))
    assert [entry.outcome for entry in subagents.sweep(repo, "main", "resume", now=1100.0)] == ["lost"]


def test_a_clear_does_not_end_the_record(repo):
    feed(repo, assigned("u1"), started("a1"), {"hook_event_name": "SessionEnd", "session_id": "main", "reason": "clear"})
    assert subagents.sweep(repo, "after-clear", "clear", now=1100.0) == []


def test_a_new_assignment_after_a_resume_is_not_ended_by_the_old_session_end(repo):
    feed(repo, {"hook_event_name": "SessionEnd", "session_id": "main", "reason": "other"}, assigned("u1"), started("a1"))
    assert subagents.sweep(repo, "other", "startup", now=1100.0) == []
    assert only(repo).is_open


def test_another_live_session_is_not_marked_lost(repo, tmp_path):
    live = tmp_path / "live.jsonl"
    live.write_text("{}\n", encoding="utf-8")
    payload = {**assigned("u1", session="live"), "transcript_path": str(live)}
    now = live.stat().st_mtime + subagents.LOST_QUIET_S - 10
    feed(repo, payload, started("a1", "live"), start=live.stat().st_mtime - 5)
    assert subagents.sweep(repo, "new", "startup", now=now) == []
    assert [entry.outcome for entry in subagents.sweep(repo, "new", "startup", now=now + 20)] == ["lost"]


def test_an_assignment_that_never_started_is_closed(repo):
    feed(repo, assigned("u1"))
    closed = subagents.sweep(repo, "main", "compact", now=1000.0 + subagents.NOT_STARTED_S + 1)
    assert [entry.outcome for entry in closed] == ["not-started"]


def test_a_start_after_a_long_permission_prompt_reopens_the_record(repo):
    feed(repo, assigned("u1"))
    subagents.sweep(repo, "main", "compact", now=1000.0 + subagents.NOT_STARTED_S + 1)
    feed(repo, started("a1"), start=1000.0 + subagents.NOT_STARTED_S + 2)
    assert only(repo).is_open and only(repo).agent_id == "a1"


# The contract and the hooks together


def test_the_contract_of_subagent_mode(repo):
    text = coordinator.contract("src", dispatch="subagent")
    for needle in ("tool Agent", "không sửa file", "dùng chung working tree", "song song không được đụng cùng file",
                   'isolation: "worktree"', "đọc diff và chạy test", "CODING_AGENT_SUBAGENT_MINUTES"):
        assert needle in text
    assert "delegate --title" not in text


def test_both_contracts_plan_for_quality_not_time():
    for text in (coordinator.contract("src", dispatch="subagent"), coordinator.contract("src")):
        assert "- " + coordinator.QUALITY_RULE in text
        assert "thời gian hoàn thành không phải tiêu chí" in text and "chất lượng kết quả là tiêu chí duy nhất" in text
        assert "Không ghi ước lượng thời gian vào plan" in text
        assert text.count(coordinator.QUALITY_RULE) == 1


def test_the_orca_contract_is_unchanged_by_default():
    assert coordinator.contract("src") == coordinator.contract("src", dispatch="orca")
    assert "worker Orca" in coordinator.contract("src") and "dispatch: subagent" not in coordinator.contract("src")


def test_session_start_injects_the_subagent_contract_and_sweeps(repo, no_orca):
    install_ledger(project(repo))
    feed(repo, assigned("u1", "old job", session="old"), started("a1", "old"),
         {"hook_event_name": "SessionEnd", "session_id": "old", "reason": "other"})
    payload = {"session_id": "new", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}
    out = call(repo, "coordinator-context", payload, mode="shadow")
    text = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "dispatch: subagent" in text and "mất dấu" in text and "old job" in text
    assert only(repo).outcome == "lost" and not no_orca.exists()


def test_a_subagents_write_is_not_recorded_as_a_direct_change(repo):
    project(repo)
    from coding_agent import events, state

    state.save(repo, "main", {"dirty_at_prompt": []})
    (repo / "written-by-subagent.txt").write_text("x", encoding="utf-8")
    assert call(repo, "stop-gate", {"session_id": "main", "cwd": str(repo)}, mode="enforce").returncode == 0
    log = events.log_path(repo).read_text(encoding="utf-8")
    assert "direct-change" not in log
