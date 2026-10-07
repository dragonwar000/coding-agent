import json
import sys
from pathlib import Path

import pytest

from conftest import requires_zm
from coding_agent import cli, events
from coding_agent.hooks import handlers
from coding_agent.manifest import Loop


def test_report_summarises_applied_and_total_decisions(repo: Path, capsys):
    events.record(repo, guard="loop-guard", kind="remind-repeat", mode="shadow", applied=False)
    events.record(repo, guard="loop-guard", kind="stop-repeat", mode="enforce", applied=True)
    events.record(repo, guard="stop-gate", kind="skipped", mode="shadow", applied=False)
    assert cli.main(["--root", str(repo), "report"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"loop-guard": {"decisions": 2, "applied": 1}, "stop-gate": {"decisions": 1, "applied": 0}}


def test_report_on_a_fresh_project_is_empty(repo: Path, capsys):
    assert cli.main(["--root", str(repo), "report"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_a_broken_manifest_stops_the_memory_commands(repo: Path, capsys):
    (repo / "integration.yaml").write_text("schema: 2\n", encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        cli.main(["--root", str(repo), "stats"])
    assert "schema must be 1" in str(stop.value)


@requires_zm
def test_recall_stats_and_forget_through_the_cli(repo: Path, zm_home: Path, capsys, monkeypatch):
    from coding_agent.memory import zeromem

    store = zeromem.store_for(repo, embedder="hash")
    zeromem.spool(store, [{"session_id": "cli-1", "speaker": "user", "text": "Retries use jittered backoff.", "ts": 1790000000, "uuid": "x:1"}])
    assert cli.main(["--root", str(repo), "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["turns"] == 1
    assert cli.main(["--root", str(repo), "recall", "jittered backoff", "-k", "3"]) == 0
    assert "jittered" in capsys.readouterr().out
    assert cli.main(["--root", str(repo), "forget", "cli-1"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted_turns"] == 1


@pytest.mark.parametrize("loop, count, calls, expected", [
    (Loop(remind_at=3, stop_at=6, max_tool_calls_per_turn=400, max_denials_per_turn=5), 1, 1, None),
    (Loop(remind_at=3, stop_at=6, max_tool_calls_per_turn=400, max_denials_per_turn=5), 3, 10, "remind-repeat"),
    (Loop(remind_at=3, stop_at=6, max_tool_calls_per_turn=400, max_denials_per_turn=5), 6, 10, "stop-repeat"),
    (Loop(remind_at=3, stop_at=6, max_tool_calls_per_turn=400, max_denials_per_turn=5), 2, 401, "stop-budget"),
    (Loop(remind_at=0, stop_at=0, max_tool_calls_per_turn=0, max_denials_per_turn=0), 99, 999, None),
])
def test_loop_policy_decisions(loop, count, calls, expected):
    assert handlers.decide_loop(loop, count, calls) == expected


def test_denial_budget_stops_retries_of_refused_calls():
    loop = Loop(remind_at=0, stop_at=0, max_tool_calls_per_turn=0, max_denials_per_turn=5)
    assert handlers.decide_loop(loop, 1, 1, denials=4) is None
    assert handlers.decide_loop(loop, 1, 1, denials=5) == "stop-denials"


def py(code: str) -> str:
    """A verify command that runs under the shell of any platform: this interpreter with one statement."""
    return f'"{sys.executable}" -c "{code}"'


def test_verify_timeout_counts_as_a_failed_check(tmp_path):
    checks = handlers._verify((py("import time; time.sleep(5)"),), cwd=str(tmp_path), timeout=1)
    assert checks[0]["exit"] is None and "timed out" in checks[0]["output"]


def test_verify_stops_at_the_first_failure(tmp_path):
    ok, bad = py("raise SystemExit(0)"), py("raise SystemExit(1)")
    checks = handlers._verify((ok, bad, "echo never"), cwd=str(tmp_path), timeout=10)
    assert [c["command"] for c in checks] == [ok, bad]
