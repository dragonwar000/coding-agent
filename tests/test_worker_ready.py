"""Dispatching a worker only once its agent is ready: the screen checks, the trust dialog, and the worktree-clean prompt under bypass."""

import json
from pathlib import Path

import pytest

from coding_agent import events, orca_cli
from test_hooks import call, manifest  # the hook subprocess helpers used by the hook tests

# Screens measured on Claude Code 2.1 under Orca on Windows (`terminal read --screen`).
TRUST_DIALOG = [
    " Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open",
    " source project, or work from your team). If not, take a moment to review what's in this folder first.",
    "",
    " ❯\xa01.\xa0Yes, I trust this folder",
    "   2. No, exit",
    "",
    " Enter to confirm · Esc to cancel",
]
EMPTY_PROMPT = ["─" * 40, '❯\xa0Try "refactor cli.py"', "─" * 40, "  ? for shortcuts"]
EMPTY_PROMPT_BARE = ["─" * 40, "❯\xa0", "─" * 40]
AGENT_WORKING = [
    "✶ Ideating…  (21s · ↓ 1.2k tokens · esc to interrupt)",
    "─" * 40,
    "❯\xa0",
    "─" * 40,
]
HOOKS_RUNNING = ["✻ Running… (running UserPromptSubmit hooks… 3/4 · 0s)", "❯ "]


def screen(lines: list[str]) -> str:
    return "\n".join(lines)


# --- _composer_idle and _trust_dialog ---------------------------------------------------------------------------------

def test_the_trust_dialog_is_recognised_and_is_not_an_idle_prompt():
    assert orca_cli._trust_dialog(screen(TRUST_DIALOG))
    assert not orca_cli._composer_idle(screen(TRUST_DIALOG))


def test_an_empty_prompt_with_a_no_break_space_is_idle():
    for lines in (EMPTY_PROMPT, EMPTY_PROMPT_BARE):
        text = screen(lines)
        assert "\xa0" in text
        assert orca_cli._composer_idle(text)
        assert not orca_cli._trust_dialog(text)


def test_a_working_agent_is_not_idle():
    for lines in (AGENT_WORKING, HOOKS_RUNNING, ["✶ Ideating…  (21s", "❯ "]):
        assert not orca_cli._composer_idle(screen(lines))
        assert not orca_cli._trust_dialog(screen(lines))


def test_a_prompt_holding_text_or_no_prompt_is_not_idle():
    assert not orca_cli._composer_idle("❯ some typed text")
    assert not orca_cli._composer_idle("Welcome to Claude Code")
    assert not orca_cli._composer_idle("")


# --- _start_in_new_worktree --------------------------------------------------------------------------------------------

class FakeOrca:
    """Stands in for `_call` and `_call_tool`: the worktree create answer, a scripted sequence of screens, and the calls made."""

    def __init__(self, screens: list[list[str]], terminal: str | None = "term_agent") -> None:
        self.screens = screens
        self.terminal = terminal
        self.calls: list[tuple[str, ...]] = []
        self.tool_calls: list[tuple[str, ...]] = []

    def call(self, *args: str) -> dict:
        self.calls.append(args)
        return {"ok": True, "result": {"dispatchId": "ctx_1", "state": "ready"}}

    def call_tool(self, *args: str) -> dict:
        self.tool_calls.append(args)
        if args[:2] == ("worktree", "create"):
            result = {"worktree": {"path": "/wt/fix-login"}}
            if self.terminal:
                result["agentTerminalHandle"] = self.terminal
            return {"ok": True, "result": result}
        if args[:2] == ("terminal", "read"):
            lines = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
            return {"ok": True, "result": {"terminal": {"tail": lines}}}
        raise AssertionError(f"unexpected orca call {args}")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def install(screens: list[list[str]], terminal: str | None = "term_agent") -> FakeOrca:
        orca = FakeOrca(screens, terminal)
        monkeypatch.setattr(orca_cli, "_call", orca.call)
        monkeypatch.setattr(orca_cli, "_call_tool", orca.call_tool)
        monkeypatch.setattr(orca_cli, "_repo_path", lambda root: tmp_path)
        monkeypatch.setattr(orca_cli, "_base_args", lambda root, base=None: [])
        monkeypatch.setattr(orca_cli, "AGENT_READY_POLL_S", 0)
        monkeypatch.setenv("CODING_AGENT_SHARE_TRUST", "off")  # a Claude worker sharing folder trust gets its own terminal
        return orca
    return install


def start(tmp_path: Path) -> dict:
    answer, path = orca_cli._start_in_new_worktree(tmp_path, "task_1", "claude", "fix-login", "Fix login", "run_1")
    assert path == "/wt/fix-login"
    return answer


def test_the_worktree_is_created_with_its_agent_and_the_task_dispatched_to_that_terminal_once_ready(fake, tmp_path):
    orca = fake([AGENT_WORKING, HOOKS_RUNNING, EMPTY_PROMPT_BARE])
    answer = start(tmp_path)
    assert answer["result"]["state"] == "ready"
    assert orca.tool_calls[0] == ("worktree", "create", "--name", "fix-login", "--repo", f"path:{tmp_path}", "--no-parent", "--agent", "claude")
    assert [c for c in orca.tool_calls if c[:2] == ("terminal", "read")] == [("terminal", "read", "--terminal", "term_agent", "--screen")] * 3
    assert orca.calls == [("worker-start", "--task", "task_1", "--terminal", "term_agent", "--worktree", "path:/wt/fix-login",
                           "--display-name", "Fix login", "--run", "run_1")]


def test_the_trust_dialog_stops_the_dispatch(fake, tmp_path):
    orca = fake([AGENT_WORKING, TRUST_DIALOG])
    with pytest.raises(orca_cli.OrcaError, match="trust this folder"):
        start(tmp_path)
    assert orca.calls == []  # nothing typed into the dialog: Enter there picks `No, exit`


def test_an_agent_never_ready_stops_the_dispatch_and_shows_the_screen(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(orca_cli, "AGENT_READY_TIMEOUT_S", 0)
    orca = fake([AGENT_WORKING])
    with pytest.raises(orca_cli.OrcaError, match="not ready for input") as raised:
        start(tmp_path)
    assert "esc to interrupt" in str(raised.value)
    assert orca.calls == []


def test_a_worktree_without_an_agent_terminal_falls_back_to_worker_start_agent(fake, tmp_path):
    orca = fake([EMPTY_PROMPT_BARE], terminal=None)
    start(tmp_path)
    assert not any(c[:2] == ("terminal", "read") for c in orca.tool_calls)
    assert orca.calls == [("worker-start", "--task", "task_1", "--agent", "claude", "--worktree", "path:/wt/fix-login",
                           "--display-name", "Fix login", "--run", "run_1")]


# --- coordinator-guard: worktree-clean under each permission mode -----------------------------------------------------

CLEAN = "python3 -m coding_agent.cli worktree-clean --task-id t1 --yes"


def guard(repo: Path, extra: dict) -> tuple[object, dict]:
    payload = {"session_id": "pm", "cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": CLEAN}, **extra}
    result = call(repo, "coordinator-guard", payload, mode="enforce")
    last = json.loads(events.log_path(repo).read_text(encoding="utf-8").splitlines()[-1])
    return result, last


@pytest.fixture
def coordinator_repo(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    manifest(repo, verify=[])
    return repo


def test_worktree_clean_is_not_asked_when_the_session_bypasses_permissions(coordinator_repo):
    result, last = guard(coordinator_repo, {"permission_mode": "bypassPermissions"})
    assert result.returncode == 0 and result.stdout == ""
    assert last["kind"] == "confirm-skipped-bypass" and last["applied"] is False


@pytest.mark.parametrize("extra", [{"permission_mode": "default"}, {"permission_mode": "acceptEdits"}, {}], ids=["default", "acceptEdits", "missing"])
def test_worktree_clean_is_asked_in_any_other_mode_or_when_the_mode_is_missing(coordinator_repo, extra):
    result, last = guard(coordinator_repo, extra)
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert last["kind"] == "confirm-requested"
