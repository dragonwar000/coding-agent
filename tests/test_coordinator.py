"""The coordinator role (FR-019 to FR-021): roles per worktree, the guard, the contract and board injection, and delegation to workers."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, events, orca_cli
from coding_agent.hooks import context_for
from coding_agent.hooks import handlers
from test_hooks import call, manifest  # the hook subprocess helpers used by the hook tests

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    # A Claude worker that shares folder trust gets a terminal of its own; these tests follow `worktree create --agent`.
    monkeypatch.setenv("CODING_AGENT_SHARE_TRUST", "off")
    return state


def committed_repo(repo: Path) -> Path:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return repo


def linked_worktree(repo: Path, tmp_path: Path, *, worker: bool = True, name: str = "worker-tree") -> Path:
    """A linked git worktree. With `worker`, it is recorded the way worker-start records one."""
    worktree = tmp_path / name
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", name, str(worktree)], check=True)
    if worker:
        orca_cli.record_worker(repo, worktree=str(worktree), task_id="task_x", dispatch="ctx_x")
    return worktree


def test_only_a_worktree_started_by_worker_start_is_a_worker(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    committed_repo(repo)
    worker = linked_worktree(repo, tmp_path)
    workspace = linked_worktree(repo, tmp_path, worker=False, name="orca-workspace")
    assert coordinator.role_for(repo) == "coordinator"
    assert coordinator.role_for(worker) == "worker"
    assert coordinator.role_for(workspace) == "coordinator"


def test_the_workers_ledger_is_shared_by_every_worktree(repo, tmp_path):
    committed_repo(repo)
    worker = linked_worktree(repo, tmp_path)
    assert orca_cli.worker_worktrees(worker) == orca_cli.worker_worktrees(repo) == {str(worker.resolve())}
    # Outside git (a folder project) the ledger is a file under the root's .coding-agent directory.
    assert orca_cli.workers_ledger(tmp_path) == tmp_path / ".coding-agent" / "workers.jsonl" and orca_cli.worker_worktrees(tmp_path) == set()


def test_a_directory_outside_git_is_the_coordinator(tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    assert coordinator.role_for(tmp_path) == "coordinator"
    assert coordinator.role_for(None) == "coordinator"


def test_the_role_environment_variable_overrides_detection(repo, monkeypatch):
    committed_repo(repo)
    monkeypatch.setenv("CODING_AGENT_ROLE", "worker")
    assert coordinator.role_for(repo) == "worker"
    monkeypatch.setenv("CODING_AGENT_ROLE", "nonsense")
    assert coordinator.role_for(repo) == "coordinator"


@pytest.mark.parametrize("tool, tool_input, blocked", [
    ("Write", {"file_path": "a.py"}, True),
    ("Edit", {"file_path": "a.py"}, True),
    ("MultiEdit", {"file_path": "a.py"}, True),
    ("Bash", {"command": "sed -i 's/a/b/' a.py"}, True),
    ("Bash", {"command": "echo hi > out.txt"}, True),
    ("Bash", {"command": "git commit -m x"}, False),
    ("Bash", {"command": "git add -A && git merge worker-branch"}, False),
    ("Bash", {"command": "git reset --hard HEAD~1"}, True),
    ("Bash", {"command": "git checkout -- src/app.py"}, True),
    ("Bash", {"command": "rm -rf build"}, True),
    ("Bash", {"command": "pytest -q 2>&1"}, False),
    ("Bash", {"command": "git status && git log --oneline"}, False),
    ("Bash", {"command": "ls -la > /dev/null"}, False),
    ("Bash", {"command": "PYTHONPATH=harness/src python3 -m coding_agent.cli delegate --title t --spec 'x > y'"}, False),
    ("Bash", {"command": "python3 -m coding_agent.cli delegate --title t --spec x && rm -rf build"}, True),
    ("Bash", {"command": 'echo "$d | $b -> $a"'}, False),
    ("Bash", {"command": "echo '> out.txt and rm -rf build'"}, False),
    ("Bash", {"command": 'git commit -m "move a -> b; rm old"'}, False),
    ("Bash", {"command": 'echo "$(rm -rf build)"'}, True),
    ("Bash", {"command": 'echo "`touch x`"'}, True),
    ("Bash", {"command": 'echo "a" > out.txt'}, True),
    ("Bash", {"command": "python3 -m coding_agent.cli delegate --title t --spec 'a && b'"}, False),
    ("Read", {"file_path": "a.py"}, False),
    ("Agent", {"description": "do it all"}, True),
    ("Task", {"description": "do it all"}, True),
])
def test_the_guard_reason_covers_writes_and_mutating_shell(tool, tool_input, blocked):
    assert (coordinator.guard_reason(tool, tool_input) is not None) is blocked


def test_the_coordinator_guard_blocks_a_write_in_enforce_and_lets_a_worker_write(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    worktree = linked_worktree(repo, tmp_path)
    payload = {"session_id": "c1", "cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": "a.py"}}
    blocked = call(repo, "coordinator-guard", payload, mode="enforce")
    assert blocked.returncode == 2 and "plan-apply" in blocked.stderr
    worker = call(repo, "coordinator-guard", {**payload, "cwd": str(worktree)}, mode="enforce")
    assert worker.returncode == 0


def test_the_coordinator_guard_in_shadow_nudges_and_never_blocks(repo, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-guard", {"session_id": "c2", "cwd": str(repo), "tool_name": "Edit", "tool_input": {}}, mode="shadow")
    assert result.returncode == 0
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "PreToolUse"
    assert "plan-apply" in body["additionalContext"] and "shadow" in body["additionalContext"]
    last = json.loads(events.log_path(repo).read_text(encoding="utf-8").splitlines()[-1])
    assert last["guard"] == "coordinator-guard" and last["kind"] == "nudged" and last["applied"] is False


def test_session_start_injects_the_contract_and_the_board(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    orca_cli.delegate(repo, title="Ship", spec="ship it", t_id="T-30", agent="claude")
    result = call(repo, "coordinator-context", {"session_id": "c3", "cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}, mode="shadow")
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "SessionStart"
    assert "Bạn là coordinator" in body["additionalContext"] and "delegate" in body["additionalContext"]
    assert "bảng việc hiện tại" in body["additionalContext"] and "Ship" in body["additionalContext"]


def test_each_prompt_gets_the_board_but_not_the_contract(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-board", {"session_id": "c4", "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "tiến độ?"}, mode="shadow")
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit"
    assert "Bạn là coordinator" not in body["additionalContext"]
    assert "bảng việc" in body["additionalContext"]


def test_a_worker_session_gets_no_coordinator_context(repo, tmp_path, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    worktree = linked_worktree(repo, tmp_path)
    assert coordinator.role_for(worktree) == "worker"
    result = call(repo, "coordinator-context", {"session_id": "c5", "cwd": str(worktree), "hook_event_name": "SessionStart"}, mode="shadow")
    assert result.returncode == 0 and result.stdout == ""


def test_an_unreadable_orca_is_reported_in_the_context_not_hidden(repo, fake_orca, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-board", {"session_id": "c6", "cwd": str(repo), "hook_event_name": "UserPromptSubmit"}, mode="shadow")
    text = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "không đọc được từ Orca" in text and "run_required" in text


def test_delegate_creates_a_task_and_starts_a_worker_in_its_own_worktree(repo, fake_orca, monkeypatch, capsys):
    committed_repo(repo)
    task_id, dispatch = orca_cli.delegate(repo, title="Refactor", spec="split the module", t_id="T-31", agent="codex")
    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    # The worktree starts from the coordinator's commit, not from the repository's default base, with its agent already
    # running; the task is dispatched to that agent's terminal.
    assert state["created_worktrees"][0] == {"name": "refactor", "repo": f"path:{orca_cli._repo_path(repo)}", "agent": "codex"}
    assert state["create_calls"][0] == {"base": head, "no_parent": True}
    path = os.path.join(os.path.dirname(str(fake_orca)), "worktrees", "refactor")
    assert state["dispatches"][dispatch] == {"task": task_id, "agent": "codex", "terminal": "term_wt_refactor", "worktree": f"path:{path}", "name": "refactor",
                                             "display": "Refactor", "base": None}
    assert len(orca_cli.worker_worktrees(repo)) == 1
    assert state["tasks"][task_id]["status"] == "dispatched"
    assert cli.main(["--root", str(repo), "delegate", "--title", "Docs", "--spec", "write docs", "--t-id", "T-32"]) == 0
    assert json.loads(capsys.readouterr().out)["dispatch"].startswith("dsp_")


def test_delegate_without_a_run_fails_clearly(repo, fake_orca, monkeypatch):
    committed_repo(repo)
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    with pytest.raises(orca_cli.OrcaError, match="run_required"):
        orca_cli.delegate(repo, title="x", spec="y", t_id="T-33", agent="claude")


def test_a_worker_start_answer_without_a_dispatch_id_is_an_error():
    with pytest.raises(orca_cli.OrcaError, match="no dispatch id"):
        orca_cli.dispatch_id_of({"ok": True, "result": {}})


def test_the_board_groups_only_this_repositorys_tasks():
    tasks = [
        {"id": "task_1", "status": "dispatched", "task_title": "Mine"},
        {"id": "task_2", "status": "dispatched", "task_title": "Theirs"},
    ]
    lines = coordinator.board_lines(tasks, {"task_1": "demo-repo", "task_2": "other"}, "demo-repo")
    assert any("task_1" in line for line in lines) and not any("task_2" in line for line in lines)
    assert coordinator.board_lines([], {}, "demo-repo") == ["bảng việc: chưa có task nào của repo này"]


def test_the_board_is_capped_so_it_cannot_flood_a_prompt():
    tasks = [{"id": f"task_{n}", "status": "dispatched", "task_title": f"job {n}"} for n in range(40)]
    lines = coordinator.board_lines(tasks, {f"task_{n}": "demo-repo" for n in range(40)}, "demo-repo")
    assert len(lines) == coordinator.BOARD_LIMIT + 1 and lines[-1] == "  ..."


def test_the_context_error_text_tells_the_coordinator_not_to_guess():
    text = coordinator.board_context([], "daemon not running")
    assert "Không bịa trạng thái task" in text and "hỏi người dùng" in text


def test_without_a_run_the_coordinator_is_told_to_run_run_init_itself():
    text = coordinator.board_context([], "run_required: No Run is bound.")
    assert "tự chạy" in text and "run-init" in text and "không nhờ người dùng chạy" in text
    assert "Không bịa trạng thái task" in text and "hỏi người dùng" not in text.lower()


def test_the_coordinator_may_write_only_the_plan_file(repo, monkeypatch):
    assert coordinator.guard_reason("Write", {"file_path": ".coding-agent/plan.yaml"}, repo) is None
    assert coordinator.guard_reason("Write", {"file_path": str(repo / ".coding-agent" / "plan.yaml")}, repo) is None
    assert coordinator.guard_reason("Write", {"file_path": "src/app.py"}, repo) is not None
    assert coordinator.guard_reason("Write", {"file_path": ".coding-agent/../src/app.py"}, repo) is not None


def test_an_in_process_subagent_is_blocked_in_the_coordinator(repo, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    result = call(repo, "coordinator-guard", {"session_id": "c9", "cwd": str(repo), "tool_name": "Agent", "tool_input": {"description": "x"}}, mode="enforce")
    assert result.returncode == 2 and "subagent" in result.stderr


def test_a_worker_start_without_a_name_is_what_orca_refuses(repo, fake_orca):
    committed_repo(repo)
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-40", spec="x", title="x")
    with pytest.raises(orca_cli.OrcaError, match="require --name"):
        orca_cli._call("worker-start", "--task", task_id, "--agent", "claude", "--worktree", "new-child", "--run", "run_test")


def test_task_records_have_parsed_deps_and_result():
    tasks = orca_cli.tasks_of({"result": {"tasks": [{"id": "t", "deps": '["a"]', "result": '{"provenance":"worker_report"}'}, {"id": "u", "deps": "[]", "result": None}]}})
    assert tasks[0]["deps"] == ["a"] and tasks[0]["result"] == {"provenance": "worker_report"}
    assert tasks[1]["deps"] == [] and tasks[1]["result"] is None


def test_a_worker_reported_completion_is_unverified_on_the_board():
    tasks = orca_cli.tasks_of([{"id": "task_1", "status": "completed", "task_title": "A", "result": '{"provenance":"worker_report","outcome":"succeeded"}'}])
    lines = coordinator.board_lines(tasks, {"task_1": "demo-repo"}, "demo-repo")
    assert lines[0].startswith("báo xong nhưng chưa có bằng chứng")


def test_stop_gate_records_files_the_coordinator_tree_gained_during_the_turn(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    text = (repo / "integration.yaml").read_text(encoding="utf-8")
    import yaml
    doc = yaml.safe_load(text)
    doc["hooks"].append({"id": "coordinator-guard", "host": ["claude_code"], "event": "PreToolUse", "mode": "enforce", "assumption": "graph is mandatory"})
    (repo / "integration.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (repo / ".gitignore").write_text(".coding-agent/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    committed_repo(repo)
    call(repo, "prompt-reset", {"session_id": "c20", "prompt": "do it", "cwd": str(repo)}, mode="shadow")
    (repo / "sneaked.py").write_text("x = 1\n", encoding="utf-8")
    call(repo, "stop-gate", {"session_id": "c20", "cwd": str(repo)}, mode="shadow", zm_base=tmp_path / "zm")
    found = [json.loads(line) for line in events.log_path(repo).read_text(encoding="utf-8").splitlines()]
    change = [e for e in found if e["kind"] == "direct-change"]
    assert change and change[0]["detail"] == {"paths": ["sneaked.py"], "count": 1}


def test_dirty_paths_is_none_outside_git(tmp_path):
    assert coordinator.dirty_paths(tmp_path) is None


def test_worker_start_falls_back_to_worker_start_agent_when_the_worktree_has_no_agent_terminal(repo, fake_orca, monkeypatch):
    committed_repo(repo)
    monkeypatch.setenv("FAKE_ORCA_NO_AGENT_TERMINAL", "1")
    task_id, dispatch = orca_cli.delegate(repo, title="Models", spec="x", t_id="T-50", agent="claude")
    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    assert [(w["name"], w["repo"], w["agent"]) for w in state["created_worktrees"]] == [("models", f"path:{repo.resolve()}", "claude")]
    record = state["dispatches"][dispatch]
    assert record["worktree"].startswith("path:") and record["task"] == task_id and record["agent"] == "claude" and record["terminal"] is None
    assert "sent" not in state  # nothing typed into a terminal
    assert len(orca_cli.worker_worktrees(repo)) == 1


def test_a_worker_start_error_is_raised_and_not_retried(repo, fake_orca):
    committed_repo(repo)
    with pytest.raises(orca_cli.OrcaError, match="unknown_task"):
        orca_cli.worker_start(repo, task_id="task_nope", agent="claude")
    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    assert len(state["created_worktrees"]) == 1 and "dispatches" not in state


def test_worker_adopt_records_a_worktree_started_elsewhere(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    committed_repo(repo)
    worktree = linked_worktree(repo, tmp_path, worker=False, name="by-hand")
    assert coordinator.role_for(worktree) == "coordinator"
    assert cli.main(["--root", str(repo), "worker-adopt", "--worktree", str(worktree)]) == 0
    assert coordinator.role_for(worktree) == "worker"
    assert cli.main(["--root", str(repo), "worker-adopt", "--worktree", str(tmp_path / "missing")]) == 1


def test_the_cli_starts_a_worker_on_an_existing_task(repo, fake_orca, capsys):
    committed_repo(repo)
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-51", spec="x", title="x")
    assert cli.main(["--root", str(repo), "worker-start", "--task-id", task_id, "--agent", "codex"]) == 0
    assert json.loads(capsys.readouterr().out)["dispatch"].startswith("dsp_")


@pytest.mark.parametrize("title, expected", [
    ("3D: model chó, chủ nhà, người trộm, xe máy có animation", "3d-model-cho-chu-nha-nguoi-trom-xe-may"),
    ("Đổi phím & toàn màn hình", "doi-phim-toan-man-hinh"),
    ("Fix login bug", "fix-login-bug"),
    ("  ", "task-abc123"),
    ("???", "task-abc123"),
    ("Supercalifragilisticexpialidociousandevenlongerthanthat", "supercalifragilisticexpialidociousandeve"),
])
def test_the_worktree_name_is_read_from_the_title(title, expected):
    name = orca_cli.worktree_name(title, "task_abc123")
    assert name == expected and len(name) <= orca_cli.NAME_LIMIT


def test_a_worker_started_by_task_id_gets_its_name_from_the_title_orca_stores(repo, fake_orca):
    committed_repo(repo)
    task_id = orca_cli.create_task(repo, project="demo-repo", t_id="T-60", spec="x", title="Vật thể tương tác, ánh sáng")
    dispatch = orca_cli.worker_start(repo, task_id=task_id, agent="claude")
    record = json.loads(fake_orca.read_text(encoding="utf-8"))["dispatches"][dispatch]
    assert record["name"] == "vat-the-tuong-tac-anh-sang" and record["display"] == "Vật thể tương tác, ánh sáng"
    assert not record["name"].startswith("ca-")


def test_the_contract_tells_the_coordinator_to_delegate_without_asking():
    text = coordinator.contract("harness/coding-agent/src")
    assert "Chỉ worker được ghi" in text and "không viết lại lệnh bị chặn" in text
    assert "giao ngay trong cùng lượt" in text and "Không xin phép người dùng" in text and "không kết thúc lượt" in text
    assert "Chưa có Orca Run: tự chạy `run-init" in text and "Không nhờ người dùng chạy" in text
    assert "PYTHONPATH=harness/coding-agent/src python3 -m coding_agent.cli delegate" in text


@pytest.mark.parametrize("tool, tool_input", [
    ("Write", {"file_path": "a.py"}),
    ("Agent", {"description": "do it all"}),
    ("Bash", {"command": "rm -rf build"}),
])
def test_every_refusal_says_to_delegate_now(tool, tool_input):
    generic = coordinator.guard_reason(tool, tool_input)
    assert "không hỏi người dùng" in generic and "không viết lại lệnh" in generic and "giao việc này ngay" in generic
    assert "`python3 -m coding_agent.cli delegate --title" in generic and coordinator.HOW in generic
    located = coordinator.guard_reason(tool, tool_input, python_src="harness/coding-agent/src")
    assert "`PYTHONPATH=harness/coding-agent/src python3 -m coding_agent.cli delegate --title" in located


def test_the_coordinator_guard_names_the_delegate_command_with_the_manifest_path(repo, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    payload = {"session_id": "c40", "cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
    blocked = call(repo, "coordinator-guard", payload, mode="enforce")
    python_src = context_for("coordinator-guard", payload).manifest.python_src
    assert blocked.returncode == 2 and "giao việc này ngay" in blocked.stderr
    assert f"PYTHONPATH={python_src} python3 -m coding_agent.cli delegate" in blocked.stderr


def test_the_contract_asks_for_short_summary_titles():
    text = coordinator.contract("harness/coding-agent/src")
    assert "tóm tắt việc cần làm" in text and "40 ký tự" in text


def test_the_quote_aware_view_keeps_substitutions_and_drops_strings():
    assert ">" not in coordinator.code_only('echo "$b -> $a"')
    assert "rm -rf x" in coordinator.code_only('echo "$(rm -rf x)"')
    assert coordinator.code_only("echo 'a > b' > out") == "echo '' > out"


@pytest.mark.parametrize("command", [
    'bash -c "rm -rf src"',
    "sh -c 'rm -rf src'",
    "/bin/sh -e -c 'echo x > out.txt'",
    'eval "rm -rf src"',
    "ls | xargs -I{} sh -c 'rm {}'",
])
def test_a_script_handed_to_a_shell_is_checked_inside_its_quotes(command):
    assert coordinator.guard_reason("Bash", {"command": command}) is not None


@pytest.mark.parametrize("command", [
    'echo "a -> b"',
    'git commit -m "rm old"',
    "./run.sh 'a > b'",
    'bash -c "git status"',
    "grep -c 'rm ' notes.txt",
])
def test_quoted_text_that_no_shell_runs_is_not_a_write(command):
    assert coordinator.guard_reason("Bash", {"command": command}) is None


@pytest.mark.parametrize("file_path", [".claude/settings.json", ".claude/settings.local.json", "integration.yaml"])
def test_editing_the_hook_config_asks_the_user_instead_of_blocking(repo, monkeypatch, file_path):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    payload = {"session_id": "c30", "cwd": str(repo), "tool_name": "Edit", "tool_input": {"file_path": str(repo / file_path)}}
    result = call(repo, "coordinator-guard", payload, mode="enforce")
    assert result.returncode == 0
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["permissionDecision"] == "ask" and file_path.split("/")[-1] in body["permissionDecisionReason"]


def test_another_file_in_claude_is_still_blocked(repo, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    committed_repo(repo)
    payload = {"session_id": "c31", "cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(repo / ".claude" / "hooks.py")}}
    assert call(repo, "coordinator-guard", payload, mode="enforce").returncode == 2


def test_a_maintainer_session_is_let_through_and_logged(repo, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_ROLE", "maintainer")
    manifest(repo, verify=[])
    committed_repo(repo)
    assert coordinator.role_for(repo) == "maintainer"
    result = call(repo, "coordinator-guard", {"session_id": "c32", "cwd": str(repo), **{"tool_name": "Bash", "tool_input": {"command": "git stash"}}}, mode="enforce")
    assert result.returncode == 0 and result.stdout == ""
    last = json.loads(events.log_path(repo).read_text(encoding="utf-8").splitlines()[-1])
    assert last["kind"] == "maintainer-allowed" and last["applied"] is False


def test_a_maintainer_is_still_asked_before_a_worktree_is_removed(repo, monkeypatch):
    monkeypatch.setenv("CODING_AGENT_ROLE", "maintainer")
    manifest(repo, verify=[])
    command = "python3 -m coding_agent.cli worktree-clean --task-id t1 --yes"
    result = call(repo, "coordinator-guard", {"session_id": "c33", "cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": command}}, mode="enforce")
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
