"""The auto-decide hook as Claude Code runs it: a subprocess per call, JSON on stdin (window and Fable faked)."""

import json
import os
import subprocess
import sys
from pathlib import Path

from coding_agent import autodecide, events
from coding_agent.manifest import AutoDecide

SRC = Path(__file__).resolve().parents[1] / "src"
MANIFEST = """schema: 1
verified: false
python_src: src
hooks:
  - id: auto-decide
    host: [claude_code]
    event: PreToolUse
    matcher: AskUserQuestion
    mode: shadow
    timeout_s: 4215
    assumption: "a question nobody answers in time can be decided for the user"
"""
ASK = {"questions": [{"question": "Xoá nhánh cũ?", "header": "Dọn", "multiSelect": False,
                      "options": [{"label": "Giữ lại", "description": ""}, {"label": "Xoá (Recommended)", "description": ""}]}]}


def setup(repo: Path, *, enabled: bool = True, fable: bool = False) -> None:
    (repo / "integration.yaml").write_text(MANIFEST, encoding="utf-8")
    autodecide.save(repo, AutoDecide(enabled=enabled, timeout_s=10, fable=fable, fable_timeout_s=30))


def call(repo: Path, *, tool: str = "AskUserQuestion", fake: str = "timeout", **env: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo),
                   "CODING_AGENT_MODE_AUTO_DECIDE": "shadow", autodecide.FAKE_ENV: fake, **env}
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ASK}
    return subprocess.run([sys.executable, "-m", "coding_agent.hooks", "auto-decide"], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", env=environment, check=False, timeout=60)


def last_event(repo: Path) -> dict:
    return json.loads(events.log_path(repo).read_text(encoding="utf-8").splitlines()[-1])


def test_turned_off_it_leaves_the_question_to_the_host(repo):
    setup(repo, enabled=False)
    done = call(repo)
    assert done.returncode == 0 and done.stdout.strip() == ""


def test_other_tools_are_ignored(repo):
    setup(repo)
    done = call(repo, tool="Bash")
    assert done.returncode == 0 and done.stdout.strip() == ""


def test_a_timeout_picks_the_suggested_option_and_flags_a_sensitive_question(repo):
    setup(repo)
    done = call(repo, fake="timeout")
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Xoá (Recommended)"}
    assert out["updatedInput"]["questions"] == ASK["questions"]
    assert "sau 10 giây" in out["additionalContext"] and "[sensitive]" in out["additionalContext"]
    event = last_event(repo)
    assert event["kind"] == "auto-decided" and event["applied"] is True
    assert event["detail"]["by"] == "suggested" and event["detail"]["sensitive"] is True


def test_the_users_choice_wins(repo):
    setup(repo)
    done = call(repo, fake="choice:1")
    assert json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Giữ lại"}
    assert last_event(repo)["kind"] == "answered-by-user"


def test_a_window_that_cannot_open_falls_back_to_asking(repo):
    setup(repo)
    done = call(repo, fake="error")
    assert done.returncode == 0 and done.stdout.strip() == ""
    assert last_event(repo)["kind"] == "auto-decide-fallback"


def test_a_nested_fable_session_is_left_alone(repo):
    setup(repo)
    done = call(repo, **{autodecide.NESTED_ENV: "1"})
    assert done.stdout.strip() == ""


def test_fable_decides_when_the_wait_runs_out(repo):
    setup(repo, fable=True)
    fable = json.dumps({"result": '{"answers": {"Xoá nhánh cũ?": "Giữ lại"}, "reason": "nhánh chưa gộp"}'})
    done = call(repo, fake="timeout", **{autodecide.FABLE_FAKE_ENV: fable})
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Giữ lại"}
    assert "người chọn: Fable" in out["additionalContext"] and "nhánh chưa gộp" in out["additionalContext"]
    assert last_event(repo)["detail"]["by"] == "fable"


def test_a_failing_fable_falls_back_to_the_suggested_option(repo):
    setup(repo, fable=True)
    done = call(repo, fake="timeout", **{autodecide.FABLE_FAKE_ENV: "error"})
    assert json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"]["answers"] == {"Xoá nhánh cũ?": "Xoá (Recommended)"}
    assert last_event(repo)["detail"]["by"] == "suggested"
