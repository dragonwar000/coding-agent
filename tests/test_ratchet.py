"""Ratchet: keep-or-revert over verified attempts, snapshots through a temporary git index."""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from coding_agent import events, ratchet, state
from coding_agent.manifest import Ratchet

SRC = Path(__file__).resolve().parents[1] / "src"
METRIC = "python3 -c \"print(open('score.txt').read().strip())\""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def seed(repo: Path, score: str = "1") -> None:
    (repo / "score.txt").write_text(score, encoding="utf-8")
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")


def config(**overrides) -> Ratchet:
    return Ratchet(**{"mode": "enforce", "assumption": "a", "metric": METRIC, "no_improve_stop": 2, "max_trials_per_turn": 20, **overrides})


def test_parse_score_reads_the_last_line():
    assert ratchet.parse_score("warming up\n0.75\n") == 0.75
    assert ratchet.parse_score("1e3") == 1000.0
    assert ratchet.parse_score("score: 3") is None
    assert ratchet.parse_score("") is None
    assert ratchet.parse_score("nan") is None


def test_baseline_kept_reverted_and_stop(repo):
    seed(repo)
    rs: dict = {}
    first = ratchet.step(repo, "s1", rs, config(), apply=True)
    assert [o.decision for o in first] == ["baseline"] and first[-1].steer and "[ratchet] baseline" in first[-1].steer

    (repo / "score.txt").write_text("5", encoding="utf-8")
    second = ratchet.step(repo, "s1", rs, config(), apply=True)
    assert [o.decision for o in second] == ["kept"] and rs["best"] == 5.0

    (repo / "score.txt").write_text("2", encoding="utf-8")
    (repo / "new.py").write_text("tmp\n", encoding="utf-8")
    third = ratchet.step(repo, "s1", rs, config(), apply=True)
    assert [o.decision for o in third] == ["reverted"]
    assert (repo / "score.txt").read_text(encoding="utf-8") == "5" and not (repo / "new.py").exists()

    (repo / "score.txt").write_text("4", encoding="utf-8")
    fourth = ratchet.step(repo, "s1", rs, config(), apply=True)
    assert [o.decision for o in fourth] == ["reverted", "stopped"] and fourth[-1].steer is None
    assert fourth[-1].detail["reason"] == "no-improve" and rs["stopped"]
    assert (repo / "score.txt").read_text(encoding="utf-8") == "5"
    assert ratchet.step(repo, "s1", rs, config(), apply=True) == []
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/coding-agent/ratchet/s1").split()
    assert "refs/coding-agent/ratchet/s1/kept" in refs and len(refs) == 5


def test_min_direction_keeps_a_lower_score(repo):
    seed(repo, "9")
    rs: dict = {}
    ratchet.step(repo, "s2", rs, config(direction="min"), apply=True)
    (repo / "score.txt").write_text("3", encoding="utf-8")
    assert [o.decision for o in ratchet.step(repo, "s2", rs, config(direction="min"), apply=True)] == ["kept"]


def test_a_failing_metric_is_a_crash_that_restores(repo):
    seed(repo)
    rs: dict = {}
    ratchet.step(repo, "s3", rs, config(), apply=True)
    (repo / "score.txt").write_text("not a number", encoding="utf-8")
    outcome = ratchet.step(repo, "s3", rs, config(), apply=True)
    assert outcome[0].decision == "crash" and (repo / "score.txt").read_text(encoding="utf-8") == "1"


def test_a_baseline_without_a_number_stops_the_ratchet(repo):
    seed(repo, "oops")
    rs: dict = {}
    outcome = ratchet.step(repo, "s4", rs, config(), apply=True)
    assert [o.decision for o in outcome] == ["crash"] and rs["stopped"] and outcome[0].steer is None


def test_shadow_records_but_never_restores_or_steers(repo):
    seed(repo)
    rs: dict = {}
    assert ratchet.step(repo, "s5", rs, config(mode="shadow"), apply=False)[0].steer is None
    (repo / "score.txt").write_text("0", encoding="utf-8")
    outcome = ratchet.step(repo, "s5", rs, config(mode="shadow"), apply=False)
    assert outcome[0].decision == "reverted" and outcome[0].detail["restored"] is False
    assert (repo / "score.txt").read_text(encoding="utf-8") == "0"


def test_no_change_since_the_kept_state_stops(repo):
    seed(repo)
    rs: dict = {}
    ratchet.step(repo, "s6", rs, config(), apply=True)
    outcome = ratchet.step(repo, "s6", rs, config(), apply=True)
    assert [o.decision for o in outcome] == ["stopped"] and outcome[0].detail["reason"] == "no-change"


def test_max_trials_stops(repo):
    seed(repo)
    rs: dict = {}
    ratchet.step(repo, "s7", rs, config(max_trials_per_turn=2), apply=True)
    (repo / "score.txt").write_text("3", encoding="utf-8")
    outcome = ratchet.step(repo, "s7", rs, config(max_trials_per_turn=2), apply=True)
    assert [o.decision for o in outcome] == ["kept", "stopped"] and outcome[-1].detail["reason"] == "max-trials"


def test_outside_git_is_unavailable(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    rs: dict = {}
    outcome = ratchet.step(folder, "s8", rs, config(), apply=True)
    assert [o.decision for o in outcome] == ["unavailable"] and rs["stopped"]


def test_user_index_head_and_harness_state_are_untouched(repo):
    seed(repo)
    head = git(repo, "rev-parse", "HEAD")
    (repo / "staged.py").write_text("s\n", encoding="utf-8")
    git(repo, "add", "staged.py")
    index_before = git(repo, "diff", "--cached", "--name-only")
    rs: dict = {}
    ratchet.step(repo, "s9", rs, config(), apply=True)
    (repo / ".coding-agent").mkdir(exist_ok=True)
    (repo / ".coding-agent" / "events.jsonl").write_text("line\n", encoding="utf-8")
    (repo / "score.txt").write_text("0", encoding="utf-8")
    ratchet.step(repo, "s9", rs, config(), apply=True)
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "diff", "--cached", "--name-only") == index_before
    assert (repo / ".coding-agent" / "events.jsonl").read_text(encoding="utf-8") == "line\n"
    assert (repo / "staged.py").exists()


def test_same_size_edit_is_seen(repo):
    """A copied index keeps its mtime, so a same-size edit right after the snapshot is not missed."""
    seed(repo, "1")
    rs: dict = {}
    ratchet.step(repo, "s10", rs, config(), apply=True)
    (repo / "score.txt").write_text("7", encoding="utf-8")
    assert ratchet.step(repo, "s10", rs, config(), apply=True)[0].decision == "kept"


def manifest(repo: Path, *, verify: list[str], ratchet_mode: str = "enforce", guard: str | None = None) -> None:
    hooks = [
        {"id": "prompt-reset", "host": ["claude_code"], "event": "UserPromptSubmit", "mode": "shadow", "assumption": "a"},
        {"id": "stop-gate", "host": ["claude_code"], "event": "Stop", "mode": "enforce", "assumption": "a"},
    ]
    if guard:
        hooks.append({"id": "coordinator-guard", "host": ["claude_code"], "event": "PreToolUse", "matcher": "Write", "mode": guard, "assumption": "a"})
    doc = {
        "schema": 1, "verified": False, "python_src": "src",
        "verify": {"commands": verify, "timeout_s": 30, "max_continuations": 2, "when": "always"},
        "memory": {"ingest": "conversation", "embedder": "hash", "change_tools": ["Write", "Edit"], "transient_markers": [], "max_episode_chars": 6000},
        "loop": {"remind_at": 3, "stop_at": 6, "max_tool_calls_per_turn": 400},
        "ratchet": {"mode": ratchet_mode, "assumption": "a", "metric": METRIC, "no_improve_stop": 2},
        "hooks": hooks,
    }
    (repo / "integration.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def stop(repo: Path, tmp_path: Path, session: str, role: str = "worker") -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo), "CODING_AGENT_ROLE": role,
           "CODING_AGENT_ZEROMEM_HOME": str(tmp_path / "zm")}
    return subprocess.run([sys.executable, "-m", "coding_agent.hooks", "stop-gate"], input=json.dumps({"session_id": session, "cwd": str(repo)}),
                          capture_output=True, text=True, env=env, check=False, timeout=120)


def test_stop_gate_continues_with_the_ratchet_in_enforce(repo, tmp_path):
    seed(repo)
    manifest(repo, verify=["true"])
    first = stop(repo, tmp_path, "h1")
    assert first.returncode == 2 and "[ratchet] baseline" in first.stderr
    (repo / "score.txt").write_text("0", encoding="utf-8")
    second = stop(repo, tmp_path, "h1")
    assert second.returncode == 2 and "[ratchet] reverted" in second.stderr
    assert (repo / "score.txt").read_text(encoding="utf-8") == "1"
    kinds = [json.loads(line)["kind"] for line in events.log_path(repo).read_text(encoding="utf-8").splitlines() if '"ratchet"' in line]
    assert kinds == ["baseline", "reverted"]


def test_stop_gate_skips_the_ratchet_for_a_bound_coordinator(repo, tmp_path):
    seed(repo)
    manifest(repo, verify=["true"], guard="enforce")
    result = stop(repo, tmp_path, "h2", role="coordinator")
    assert result.returncode == 0 and "ratchet" not in result.stderr


def test_a_red_turn_that_gives_up_returns_to_the_kept_state(repo, tmp_path):
    seed(repo)
    manifest(repo, verify=["python3 -c \"import sys; sys.exit(0 if open('keep.py').read() == 'x = 1\\n' else 1)\""])
    assert stop(repo, tmp_path, "h3").returncode == 2
    (repo / "keep.py").write_text("broken\n", encoding="utf-8")
    assert [stop(repo, tmp_path, "h3").returncode for _ in range(3)] == [2, 2, 0]
    assert (repo / "keep.py").read_text(encoding="utf-8") == "x = 1\n"
    assert state.load(repo, "h3")["ratchet"]["stopped"]
