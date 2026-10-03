"""Hooks as a host runs them: a subprocess per call, JSON on stdin, exit codes on the wire."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import requires_zm
from coding_agent import events, state
from coding_agent.hooks import HookResult, run
from coding_agent.memory import zeromem

SRC = Path(__file__).resolve().parents[1] / "src"


def manifest(repo: Path, *, verify: list[str], mode: str = "enforce", loop_stop: int = 6) -> None:
    doc = {
        "schema": 1,
        "verified": False,
        "python_src": "src",
        "verify": {"commands": verify, "timeout_s": 30, "max_continuations": 2},
        "memory": {"ingest": "both", "embedder": "hash", "change_tools": ["Write", "Edit", "MultiEdit"],
                   "transient_markers": ["for now"], "max_episode_chars": 6000},
        "loop": {"remind_at": 3, "stop_at": loop_stop, "max_tool_calls_per_turn": 400},
        "hooks": [
            {"id": "orca-guard", "host": ["claude_code"], "event": "PreToolUse", "matcher": "Bash", "mode": "enforce",
             "assumption": "a wrong status is wrong"},
            {"id": "loop-guard", "host": ["claude_code"], "event": "PreToolUse", "matcher": "Write|Edit|Bash", "mode": mode,
             "assumption": "the same call repeated is stuck"},
            {"id": "prompt-reset", "host": ["claude_code"], "event": "UserPromptSubmit", "mode": "shadow",
             "assumption": "a new prompt starts a new turn"},
            {"id": "stop-gate", "host": ["claude_code"], "event": "Stop", "mode": mode,
             "assumption": "a verification command is the evidence"},
        ],
    }
    (repo / "integration.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def call(repo: Path, hook: str, payload: dict, *, mode: str = "enforce", zm_base: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo), f"CODING_AGENT_MODE_{hook.upper().replace('-', '_')}": mode}
    if zm_base is not None:
        env["CODING_AGENT_ZEROMEM_HOME"] = str(zm_base)
    return subprocess.run([sys.executable, "-m", "coding_agent.hooks", hook], input=json.dumps(payload), capture_output=True, text=True, env=env, check=False, timeout=120)


def transcript(path: Path, *, prompt: str, edited: str, answer: str) -> Path:
    entries = [
        {"type": "user", "message": {"role": "user", "content": "earlier question"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "earlier answer"}]}},
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": edited}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": False}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def bash(command: str) -> dict:
    return {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": command}}


def test_orca_guard_blocks_a_status_outside_the_enum(repo):
    manifest(repo, verify=[])
    blocked = call(repo, "orca-guard", bash("orca orchestration task-update --id task_1 --status done"))
    assert blocked.returncode == 2 and "not a valid Orca status" in blocked.stderr
    allowed = call(repo, "orca-guard", bash("orca orchestration task-update --id task_1 --status completed"))
    assert allowed.returncode == 0
    other = call(repo, "orca-guard", {"tool_name": "Write", "tool_input": {}})
    assert other.returncode == 0 and other.stdout == ""


def test_orca_guard_hints_about_result_task_id_after_create(repo):
    manifest(repo, verify=[])
    hint = call(repo, "orca-guard", bash("orca orchestration task-create --spec x"))
    assert hint.returncode == 0
    body = json.loads(hint.stdout)
    assert "result.task.id" in body["hookSpecificOutput"]["additionalContext"]


def test_a_malformed_payload_never_breaks_the_session(repo):
    manifest(repo, verify=[])
    result = subprocess.run([sys.executable, "-m", "coding_agent.hooks", "orca-guard"], input="{not json", capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo)}, check=False)
    assert result.returncode == 0


def test_loop_guard_is_shadow_by_default_and_records_without_blocking(repo):
    manifest(repo, verify=[], mode="shadow", loop_stop=4)
    payload = {"session_id": "s2", "tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x"}}
    codes = [call(repo, "loop-guard", payload, mode="shadow").returncode for _ in range(5)]
    assert codes == [0, 0, 0, 0, 0]
    logged = [e for e in events.summarize(repo).items()]
    assert ("loop-guard", {"decisions": 3, "applied": 0}) in logged


def test_loop_guard_enforced_stops_the_repeat(repo):
    manifest(repo, verify=[], loop_stop=4)
    payload = {"session_id": "s3", "tool_name": "Write", "tool_input": {"file_path": "a.py", "content": "x"}}
    codes = [call(repo, "loop-guard", payload).returncode for _ in range(5)]
    assert codes[:3] == [0, 0, 0] and codes[3] == 2
    assert events.summarize(repo)["loop-guard"]["applied"] >= 1


def test_prompt_reset_clears_counters_and_keeps_recent_prompts(repo):
    manifest(repo, verify=[])
    state.save(repo, "s4", {"sig_counts": {"x": 5}, "calls_this_turn": 9, "continuations": 2})
    assert call(repo, "prompt-reset", {"session_id": "s4", "prompt": "Add retry please."}).returncode == 0
    data = state.load(repo, "s4")
    assert data["calls_this_turn"] == 0 and data["sig_counts"] == {} and data["continuations"] == 0
    assert data["prompts"][-1]["text"] == "Add retry please."


def test_stop_gate_blocks_a_failing_verification_in_enforce(repo, tmp_path):
    manifest(repo, verify=["sh -c 'echo broken tests >&2; exit 1'"], mode="enforce")
    path = transcript(tmp_path / "t.jsonl", prompt="Add retry.", edited="src/a.py", answer="Done, tests pass.")
    result = call(repo, "stop-gate", {"session_id": "s5", "transcript_path": str(path), "cwd": str(repo)}, zm_base=tmp_path / "zm")
    assert result.returncode == 2
    assert "verification failed" in result.stderr and "broken tests" in result.stderr
    assert state.load(repo, "s5")["continuations"] == 1


def test_stop_gate_gives_up_after_the_continuation_budget(repo, tmp_path):
    manifest(repo, verify=["false"], mode="enforce")
    path = transcript(tmp_path / "t.jsonl", prompt="Add retry.", edited="src/a.py", answer="Done.")
    payload = {"session_id": "s6", "transcript_path": str(path), "cwd": str(repo)}
    codes = [call(repo, "stop-gate", payload, zm_base=tmp_path / "zm").returncode for _ in range(4)]
    assert codes == [2, 2, 0, 0]
    kinds = [e["kind"] for e in _events(repo) if e["guard"] == "stop-gate"]
    assert "budget-exhausted" in kinds


def test_stop_gate_shadow_records_the_failure_and_lets_the_turn_end(repo, tmp_path):
    manifest(repo, verify=["false"], mode="shadow")
    path = transcript(tmp_path / "t.jsonl", prompt="Add retry.", edited="src/a.py", answer="Done.")
    result = call(repo, "stop-gate", {"session_id": "s7", "transcript_path": str(path), "cwd": str(repo)}, mode="shadow", zm_base=tmp_path / "zm")
    assert result.returncode == 0
    assert [e["kind"] for e in _events(repo) if e["guard"] == "stop-gate"] == ["not-ok"]


def test_stop_gate_without_commands_is_skipped_never_ok(repo, tmp_path):
    manifest(repo, verify=[], mode="shadow")
    path = transcript(tmp_path / "t.jsonl", prompt="Add retry.", edited="src/a.py", answer="Done.")
    call(repo, "stop-gate", {"session_id": "s8", "transcript_path": str(path), "cwd": str(repo)}, mode="shadow", zm_base=tmp_path / "zm")
    kinds = [e["kind"] for e in _events(repo) if e["guard"] == "stop-gate"]
    assert kinds == ["skipped"]


def test_a_missing_manifest_is_logged_and_passes(repo):
    result = call(repo, "stop-gate", {"session_id": "s9", "cwd": str(repo)})
    assert result.returncode == 0


def test_run_is_fail_open_when_a_handler_raises(repo, monkeypatch):
    from coding_agent.hooks import context_for

    monkeypatch.setenv("CODING_AGENT_MODE_BROKEN_HOOK", "shadow")

    def broken(_ctx):
        raise RuntimeError("boom")

    result = run("broken-hook", {"cwd": str(repo)}, broken)
    assert isinstance(result, HookResult) and result.code == 0
    assert _events(repo)[-1]["kind"] == "hook-error"
    monkeypatch.delenv("CODING_AGENT_MODE_BROKEN_HOOK")
    assert context_for("broken-hook", {"cwd": str(repo)}).mode == "off"


@requires_zm
def test_stop_gate_records_a_verified_episode_that_recall_returns(repo, tmp_path, zm_home):
    manifest(repo, verify=["true"], mode="shadow")
    path = transcript(tmp_path / "t.jsonl", prompt="Add jittered backoff to the retry client.", edited="src/retry.py",
                      answer="Added jittered backoff with a 30 second cap.")
    result = call(repo, "stop-gate", {"session_id": "s10", "transcript_path": str(path), "cwd": str(repo)}, mode="shadow", zm_base=zm_home)
    assert result.returncode == 0
    store = zeromem.store_for(repo, embedder="hash", base=zm_home)
    evidence = zeromem.recall(store, "jittered backoff retry client", top_k=5)
    texts = [e["text"] for e in evidence]
    assert any(t.startswith("# Verified episode") and "- src/retry.py" in t for t in texts)
    assert any("jittered" in t for t in texts)
    zeromem.forget_session(store, "s10")


def _events(repo: Path) -> list[dict]:
    log = events.log_path(repo)
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _denied_turn(path: Path, count: int) -> None:
    """A transcript whose current turn has `count` Bash calls that all came back as errors."""
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "Dọn các file tạm"}, "timestamp": "2026-10-03T10:00:00Z"})]
    for index in range(count):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"u{index}", "name": "Bash", "input": {"command": f"rm tmp{index}"}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"u{index}", "is_error": True, "content": "denied"}]}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_denial_budget_blocks_retries_in_enforce_and_only_records_in_shadow(repo, tmp_path):
    manifest(repo, verify=[])
    path = tmp_path / "transcript.jsonl"
    _denied_turn(path, 5)
    payload = {"session_id": "s23", "cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "ls"}, "transcript_path": str(path)}

    blocked = call(repo, "loop-guard", payload, mode="enforce")
    assert blocked.returncode == 2 and "refused or failed" in blocked.stderr
    assert _events(repo)[-1]["kind"] == "stop-denials" and _events(repo)[-1]["applied"] is True

    recorded = call(repo, "loop-guard", payload, mode="shadow")
    assert recorded.returncode == 0
    assert _events(repo)[-1]["applied"] is False and _events(repo)[-1]["detail"]["denials"] == 5


def test_pre_compact_records_the_compaction_without_changing_the_session(repo):
    manifest(repo, verify=[])
    call(repo, "prompt-reset", {"session_id": "s24", "prompt": "Sửa bảng giá", "cwd": str(repo)}, mode="shadow")
    result = call(repo, "pre-compact", {"session_id": "s24", "cwd": str(repo), "trigger": "auto"}, mode="shadow")
    assert result.returncode == 0 and result.stdout == ""
    assert _events(repo)[-1]["kind"] == "compaction" and _events(repo)[-1]["detail"] == {"prompts": 1}


def test_session_restore_puts_the_recent_prompts_back_after_a_compaction(repo):
    manifest(repo, verify=[])
    call(repo, "prompt-reset", {"session_id": "s25", "prompt": "Sửa bảng giá", "cwd": str(repo)}, mode="shadow")
    call(repo, "prompt-reset", {"session_id": "s25", "prompt": "Thêm test cho nó", "cwd": str(repo)}, mode="shadow")

    restored = call(repo, "session-restore", {"session_id": "s25", "cwd": str(repo), "source": "compact"}, mode="shadow")
    body = json.loads(restored.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "SessionStart"
    assert "- Sửa bảng giá" in body["additionalContext"] and "- Thêm test cho nó" in body["additionalContext"]

    startup = call(repo, "session-restore", {"session_id": "s25", "cwd": str(repo), "source": "startup"}, mode="shadow")
    assert startup.stdout == ""


def test_orca_guard_in_shadow_reports_an_invalid_status_and_lets_it_through(repo):
    manifest(repo, verify=[])
    payload = {"session_id": "s26", "cwd": str(repo), "tool_name": "Bash",
               "tool_input": {"command": "orca orchestration task-update --id task_1 --status bogus"}}
    result = call(repo, "orca-guard", payload, mode="shadow")
    assert result.returncode == 0
    assert _events(repo)[-1]["kind"] == "invalid-status" and _events(repo)[-1]["applied"] is False


def test_a_hook_in_mode_off_does_nothing(repo):
    manifest(repo, verify=[])
    call(repo, "prompt-reset", {"session_id": "s27", "prompt": "không ghi", "cwd": str(repo)}, mode="off")
    assert not (repo / ".coding-agent" / "state").exists()


def test_without_an_env_mode_the_manifest_mode_applies(repo, monkeypatch):
    from coding_agent.hooks import context_for

    manifest(repo, verify=[])
    monkeypatch.delenv("CODING_AGENT_MODE_ORCA_GUARD", raising=False)
    assert context_for("orca-guard", {"cwd": str(repo)}).mode == "enforce"
    assert context_for("pre-compact", {"cwd": str(repo)}).mode == "off"
