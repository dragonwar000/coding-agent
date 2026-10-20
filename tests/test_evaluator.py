"""Evaluator: one fresh claude child judges a verified turn; grounding checks the evidence it cites."""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from coding_agent import evaluator, events, state
from coding_agent.manifest import Evaluator

SRC = Path(__file__).resolve().parents[1] / "src"


def fake_claude(tmp_path: Path, report: dict | str, *, exit_code: int = 0) -> Path:
    """An executable that records argv, stdin and env, and prints a `claude -p --output-format json` envelope."""
    reply = tmp_path / "reply.json"
    body = report if isinstance(report, str) else json.dumps(report)
    reply.write_text(json.dumps({"result": body, "total_cost_usd": 0.001}), encoding="utf-8")
    log = tmp_path / "claude-log.jsonl"
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'nested': os.environ.get('CODING_AGENT_EVALUATOR_NESTED'), 'cwd': os.getcwd()}}) + '\\n')\n"
        f"sys.stdout.write(open({str(reply)!r}).read())\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "claude-log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def report(verdict: str = "ok", *criteria: dict) -> dict:
    return {"verdict": verdict, "reason": "checked", "criteria": list(criteria)}


def crit(cid: str, met: bool, evidence=None, required=None) -> dict:
    item = {"id": cid, "text": f"criterion {cid}", "met": met}
    if evidence is not None:
        item["evidence"] = evidence
    if required is not None:
        item["requiredEvidence"] = required
    return item


def test_prompt_without_grounding_has_no_evidence_fields():
    text = evaluator.prompt("Add retry", ("retries 3 times",), ["pytest -q"], grounding=False)
    assert "c1: retries 3 times" in text and "pytest -q" in text and "evidence" not in text
    grounded = evaluator.prompt("Add retry", (), [], grounding=True)
    assert "requiredEvidence" in grounded and "write 2 to 6 checkable criteria" in grounded


def test_parse_rejects_a_malformed_report():
    assert evaluator.parse(json.dumps({"result": json.dumps(report("ok", crit("c1", True)))}), grounding=False) is not None
    assert evaluator.parse("no json here", grounding=False) is None
    assert evaluator.parse(json.dumps({"result": json.dumps({"verdict": "maybe", "criteria": []})}), grounding=False) is None
    assert evaluator.parse(json.dumps({"result": json.dumps(report("ok", crit("c1", True), crit("c1", False)))}), grounding=False) is None
    assert evaluator.parse(json.dumps({"result": json.dumps(report("ok", crit("c1", True, evidence="x")))}), grounding=True) is None


def test_grounding_resolves_paths_inside_the_root_and_passed_commands(repo):
    (repo / "a.py").write_text("x\n", encoding="utf-8")
    parsed = evaluator.parse(json.dumps(report("ok", crit("c1", True, ["a.py", "pytest -q"]), crit("c2", True, ["missing.py"]),
                                               crit("c3", True, ["../outside"]), crit("c4", True, []), crit("c5", False, []))), grounding=True)
    evaluator.resolve(parsed, repo, ["pytest -q"])
    by_id = {c.id: c for c in parsed.criteria}
    assert by_id["c1"].grounded and not by_id["c2"].grounded and by_id["c2"].unresolved == ["missing.py"]
    assert not by_id["c3"].grounded and not by_id["c4"].grounded and by_id["c5"].grounded
    blocking = evaluator.unmet(parsed, grounding_enforced=True)
    assert [c.id for c in blocking] == ["c2", "c3", "c4", "c5"]
    lines = evaluator.steer(parsed, blocking, 1, 2).splitlines()
    revise = [json.loads(line) for line in lines if line.startswith("{")]
    assert revise[0] == {"decision": "revise", "criterion": "c2", "claim": "criterion c2",
                         "reason": "cited evidence did not resolve: missing.py", "required_evidence": ["missing.py"]}
    assert revise[2]["reason"] == "marked met without evidence" and revise[3]["reason"] == "an evaluator found it unmet"


def test_judge_runs_one_isolated_read_only_child(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(evaluator.CLAUDE_ENV, str(fake_claude(tmp_path, report("ok", crit("c1", True)))))
    config = Evaluator(mode="enforce", assumption="a", rubric=("does it",), model="haiku")
    judgement = evaluator.judge(config, repo, "Do it", ["true"])
    assert judgement.report is not None and judgement.report.verdict == "ok" and judgement.cost_usd == 0.001
    (call,) = calls(tmp_path)
    assert call["nested"] == "1" and Path(call["cwd"]).resolve() == repo.resolve()
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "haiku" and argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv and "Do it" in call["stdin"]


def test_judge_reports_a_child_failure(repo, tmp_path, monkeypatch):
    monkeypatch.setenv(evaluator.CLAUDE_ENV, str(fake_claude(tmp_path, report("ok"), exit_code=1)))
    judgement = evaluator.judge(Evaluator(mode="shadow", assumption="a"), repo, "x", [])
    assert judgement.report is None and "exited 1" in judgement.error
    monkeypatch.setenv(evaluator.CLAUDE_ENV, str(fake_claude(tmp_path, "not a report")))
    assert evaluator.judge(Evaluator(mode="shadow", assumption="a"), repo, "x", []).error == "evaluator answered without a usable report"


def manifest(repo: Path, *, mode: str = "enforce", grounding: str = "off", stop_mode: str = "enforce") -> None:
    doc = {
        "schema": 1, "verified": False, "python_src": "src",
        "verify": {"commands": ["true"], "timeout_s": 30, "max_continuations": 2, "when": "always"},
        "memory": {"ingest": "conversation", "embedder": "hash", "change_tools": ["Write", "Edit"], "transient_markers": [], "max_episode_chars": 6000},
        "loop": {"remind_at": 3, "stop_at": 6, "max_tool_calls_per_turn": 400},
        "evaluator": {"mode": mode, "assumption": "a", "rubric": ["the feature works"], "grounding": grounding},
        "hooks": [
            {"id": "prompt-reset", "host": ["claude_code"], "event": "UserPromptSubmit", "mode": "shadow", "assumption": "a"},
            {"id": "stop-gate", "host": ["claude_code"], "event": "Stop", "mode": stop_mode, "assumption": "a"},
        ],
    }
    (repo / "integration.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def hook(repo: Path, tmp_path: Path, name: str, payload: dict, fake: Path, extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(repo), "CODING_AGENT_ROLE": "worker",
           "CODING_AGENT_ZEROMEM_HOME": str(tmp_path / "zm"), evaluator.CLAUDE_ENV: str(fake), **(extra or {})}
    return subprocess.run([sys.executable, "-m", "coding_agent.hooks", name], input=json.dumps(payload), capture_output=True, text=True, env=env, check=False, timeout=120)


def verdicts(repo: Path) -> list[dict]:
    return [json.loads(line) for line in events.log_path(repo).read_text(encoding="utf-8").splitlines() if '"evaluator"' in line]


def test_enforce_continues_an_unmet_turn_up_to_the_budget(repo, tmp_path):
    manifest(repo)
    fake = fake_claude(tmp_path, report("not-ok", crit("c1", False)))
    assert hook(repo, tmp_path, "prompt-reset", {"session_id": "e1", "prompt": "Make the feature work"}, fake).returncode == 0
    codes = [hook(repo, tmp_path, "stop-gate", {"session_id": "e1", "cwd": str(repo)}, fake) for _ in range(3)]
    assert [c.returncode for c in codes] == [2, 2, 0]
    assert '"decision": "revise"' in codes[0].stderr and "round 1 of 2" in codes[0].stderr
    assert "Make the feature work" in calls(tmp_path)[0]["stdin"]
    assert [e["kind"] for e in verdicts(repo)] == ["evaluator-verdict", "evaluator-verdict", "budget-exhausted"]
    assert hook(repo, tmp_path, "prompt-reset", {"session_id": "e1", "prompt": "next"}, fake).returncode == 0
    assert state.load(repo, "e1")["evaluator_rounds"] == 0


def test_grounding_enforce_blocks_an_ok_without_resolvable_evidence(repo, tmp_path):
    manifest(repo, grounding="enforce")
    (repo / "feature.py").write_text("ok\n", encoding="utf-8")
    ungrounded = fake_claude(tmp_path, report("ok", crit("c1", True, ["nope.py"], [])))
    result = hook(repo, tmp_path, "stop-gate", {"session_id": "e2", "cwd": str(repo)}, ungrounded)
    assert result.returncode == 2 and "cited evidence did not resolve: nope.py" in result.stderr
    grounded = fake_claude(tmp_path, report("ok", crit("c1", True, ["feature.py", "true"], [])))
    assert hook(repo, tmp_path, "stop-gate", {"session_id": "e2", "cwd": str(repo)}, grounded).returncode == 0
    assert verdicts(repo)[-1]["detail"]["effective"] == "ok"


def test_shadow_records_the_verdict_without_blocking(repo, tmp_path):
    manifest(repo, mode="shadow", grounding="shadow")
    fake = fake_claude(tmp_path, report("not-ok", crit("c1", False, [], ["a test"])))
    assert hook(repo, tmp_path, "stop-gate", {"session_id": "e3", "cwd": str(repo)}, fake).returncode == 0
    (event,) = verdicts(repo)
    assert event["applied"] is False and event["detail"]["effective"] == "not-ok" and event["detail"]["criteria"][0]["required_evidence"] == ["a test"]


def test_stop_gate_in_shadow_never_blocks_even_with_an_enforcing_evaluator(repo, tmp_path):
    manifest(repo, stop_mode="shadow")
    fake = fake_claude(tmp_path, report("not-ok", crit("c1", False)))
    result = hook(repo, tmp_path, "stop-gate", {"session_id": "e4", "cwd": str(repo)}, fake, {"CODING_AGENT_MODE_STOP_GATE": "shadow"})
    assert result.returncode == 0 and verdicts(repo)[0]["applied"] is False


def test_the_evaluator_child_session_skips_stop_gate(repo, tmp_path):
    manifest(repo)
    fake = fake_claude(tmp_path, report("not-ok", crit("c1", False)))
    result = hook(repo, tmp_path, "stop-gate", {"session_id": "e5", "cwd": str(repo)}, fake, {evaluator.NESTED_ENV: "1"})
    assert result.returncode == 0 and calls(tmp_path) == [] and not events.log_path(repo).exists()


def test_a_child_failure_is_logged_and_lets_the_turn_end(repo, tmp_path):
    manifest(repo)
    fake = fake_claude(tmp_path, report("ok"), exit_code=3)
    assert hook(repo, tmp_path, "stop-gate", {"session_id": "e6", "cwd": str(repo)}, fake).returncode == 0
    assert verdicts(repo)[0]["kind"] == "evaluator-error"


def test_manifest_validation(tmp_path):
    from coding_agent.manifest import ManifestError, evaluator_from, ratchet_from
    import pytest

    with pytest.raises(ManifestError):
        evaluator_from({"mode": "shadow"})
    with pytest.raises(ManifestError):
        evaluator_from({"mode": "shadow", "assumption": "a", "grounding": "enforce"})
    with pytest.raises(ManifestError):
        evaluator_from({"mode": "enforce", "assumption": "a", "model": "bad model"})
    with pytest.raises(ManifestError):
        ratchet_from({"mode": "enforce", "assumption": "a", "direction": "up"})
    with pytest.raises(ManifestError):
        ratchet_from({"mode": "enforce", "assumption": "a", "min_delta": -1})
    assert not ratchet_from({"mode": "enforce", "assumption": "a", "metric": ""}).active
    assert ratchet_from(None).mode == "off" and evaluator_from(None).mode == "off"
