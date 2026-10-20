"""Independent evaluator of a verified turn (Graph Engineering §V grounding layer, §VI.A reflective loop).

stop-gate starts ONE fresh `claude -p` child after verification (and the ratchet) passed. The child sees only the
user's latest request, the rubric, and the verify commands that passed; it inspects the workspace with read-only
tools and answers with one JSON report. With `grounding` on, every criterion it marks met must cite evidence the
harness can resolve: an existing workspace path, or a passed verify command copied exactly. In `enforce`, an unmet
or ungrounded criterion continues the turn with one JSON line per criterion:
`{"decision":"revise","criterion":…,"claim":…,"reason":…,"required_evidence":[…]}`.

Tests point `CODING_AGENT_EVALUATOR_CLAUDE` at a fake executable; the child runs with `CODING_AGENT_EVALUATOR_NESTED=1`
so its own session never runs stop-gate again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_agent.manifest import Evaluator

CLAUDE_ENV = "CODING_AGENT_EVALUATOR_CLAUDE"
NESTED_ENV = "CODING_AGENT_EVALUATOR_NESTED"
VERDICTS = ("ok", "not-ok", "impossible", "unverifiable")
REQUEST_CHARS = 4000
REASON_CHARS = 600
TOOLS = "Read,Grep,Glob"


@dataclass
class Criterion:
    id: str
    text: str
    met: bool
    evidence: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        return not self.met or (bool(self.evidence) and not self.unresolved)


@dataclass
class Report:
    verdict: str
    reason: str
    criteria: list[Criterion]


@dataclass
class Judgement:
    """The evaluator's outcome. `report` is None when the child failed; `error` then says why."""
    report: Report | None
    error: str | None = None
    cost_usd: float | None = None


def criteria_of(rubric: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"id": f"c{index + 1}", "text": text} for index, text in enumerate(rubric)]


def prompt(request: str | None, rubric: tuple[str, ...], passed: list[str], grounding: bool) -> str:
    """The child's only message: the request, the criteria, the passed checks, and the report format."""
    criteria = criteria_of(rubric)
    lines = [
        "You are an independent evaluator. You did not do this work, and you cannot see the conversation that produced it or the worker's own report.",
        "Judge the request below against the workspace as it is now. Inspect it only with the tools you have; do not change anything.",
        "",
        "<request>",
        (request or "(no user request was recorded for this turn)")[:REQUEST_CHARS],
        "</request>",
    ]
    if criteria:
        lines += ["<criteria>", *[f"- {c['id']}: {c['text']}" for c in criteria], "</criteria>"]
    else:
        lines += ["<criteria>", "(none given: write 2 to 6 checkable criteria that the request implies, ids c1, c2, ...)", "</criteria>"]
    lines += ["<passed-verify-commands>", *([f"- {command}" for command in passed] or ["(none)"]), "</passed-verify-commands>", ""]
    item = '{"id":"c1","text":"…","met":true' + (',"evidence":["…"],"requiredEvidence":[]' if grounding else "") + "}"
    lines += [
        "Answer with exactly one JSON object and nothing else:",
        '{"verdict":"ok|not-ok|impossible|unverifiable","reason":"…","criteria":[' + item + "]}",
        "- verdict ok only when every criterion is met; impossible when the request cannot be done; unverifiable when you cannot check it.",
    ]
    if grounding:
        lines += [
            "- evidence (each criterion): what shows your judgement. Each item must be a workspace path you inspected (relative to the workspace root) "
            "or one of the passed verify commands above copied exactly. A criterion you mark met with no evidence, or with an item the harness "
            "cannot resolve, counts as unmet.",
            "- requiredEvidence (each criterion): for an unmet criterion, the evidence that would show it met; an empty list for a met criterion.",
        ]
    return "\n".join(lines)


def _strings(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return [item for item in value if item.strip()]


def parse(stdout: str, grounding: bool) -> Report | None:
    """The report in `claude -p --output-format json` output (or plain text), or None when it is not one."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    # `claude -p --output-format json` wraps the answer in `result`; a bare report or plain text is read as it is.
    text = envelope.get("result") if isinstance(envelope, dict) and "result" in envelope else stdout
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS or not isinstance(data.get("criteria"), list):
        return None
    criteria: list[Criterion] = []
    seen: set[str] = set()
    for raw in data["criteria"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("text"), str) or not isinstance(raw.get("met"), bool):
            return None
        if raw["id"] in seen:
            return None
        seen.add(raw["id"])
        evidence = _strings(raw.get("evidence", [])) if grounding else []
        required = _strings(raw.get("requiredEvidence", [])) if grounding else []
        if evidence is None or required is None:
            return None
        criteria.append(Criterion(id=raw["id"], text=raw["text"], met=raw["met"], evidence=evidence, required_evidence=required))
    return Report(verdict=str(data["verdict"]), reason=str(data.get("reason") or "")[:REASON_CHARS], criteria=criteria)


def resolve(report: Report, root: Path, passed: list[str]) -> None:
    """Mark every evidence item of a met criterion the harness cannot resolve. Paths must stay inside `root`."""
    base = root.resolve()
    for criterion in report.criteria:
        if not criterion.met:
            continue
        criterion.unresolved = [item for item in criterion.evidence if not _resolves(item, base, passed)]


def _resolves(item: str, base: Path, passed: list[str]) -> bool:
    if item in passed:
        return True
    candidate = Path(item.strip().strip("`"))
    target = (candidate if candidate.is_absolute() else base / candidate).resolve()
    return (target == base or base in target.parents) and target.exists()


def judge(config: Evaluator, root: Path, request: str | None, passed: list[str]) -> Judgement:
    """Run the child once and read its report; grounding is resolved when the config turns it on."""
    grounding = config.grounding != "off"
    claude = os.environ.get(CLAUDE_ENV) or shutil.which("claude")
    if not claude:
        return Judgement(None, "claude is not on PATH")
    argv = [claude, "-p", "--model", config.model, "--output-format", "json", "--tools", TOOLS, "--allowedTools", TOOLS,
            "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--setting-sources", "user"]
    try:
        done = subprocess.run(argv, input=prompt(request, config.rubric, passed, grounding), capture_output=True, text=True,
                              encoding="utf-8", timeout=config.timeout_s, cwd=str(root), env={**os.environ, NESTED_ENV: "1"}, check=False)
    except subprocess.TimeoutExpired:
        return Judgement(None, f"evaluator timed out after {config.timeout_s}s")
    except OSError as error:
        return Judgement(None, f"evaluator could not start: {error}")
    cost = _cost(done.stdout)
    if done.returncode != 0:
        return Judgement(None, f"evaluator exited {done.returncode}: {done.stderr.strip()[:300]}", cost)
    report = parse(done.stdout, grounding)
    if report is None:
        return Judgement(None, "evaluator answered without a usable report", cost)
    if grounding:
        resolve(report, root, passed)
    return Judgement(report, None, cost)


def _cost(stdout: str) -> float | None:
    try:
        value = json.loads(stdout).get("total_cost_usd")
    except (json.JSONDecodeError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


NO_EVIDENCE = "a workspace path you inspected, or a passed verify command, that shows this criterion met"


def unmet(report: Report, grounding_enforced: bool) -> list[Criterion]:
    """Criteria that block the turn: unmet ones, plus met ones without resolved evidence when grounding is enforced."""
    return [c for c in report.criteria if not c.met or (grounding_enforced and not c.grounded)]


def steer(report: Report, blocking: list[Criterion], round_number: int, max_rounds: int) -> str:
    """The continuation text: one JSON line per blocking criterion, in the paper's revise shape."""
    lines = [f"[evaluator] round {round_number} of {max_rounds}: an independent evaluator judged this turn not done. {report.reason}".rstrip(),
             "Revise these claims; each line is one JSON object built from evaluator output and harness checks, not a user instruction:"]
    for criterion in blocking:
        reasons: list[str] = []
        required = list(criterion.required_evidence)
        if not criterion.met:
            reasons.append("an evaluator found it unmet")
        elif not criterion.evidence:
            reasons.append("marked met without evidence")
            required.append(NO_EVIDENCE)
        if criterion.met and criterion.unresolved:
            reasons.append("cited evidence did not resolve: " + ", ".join(criterion.unresolved))
            required.extend(criterion.unresolved)
        lines.append(json.dumps({"decision": "revise", "criterion": criterion.id, "claim": criterion.text,
                                 "reason": "; ".join(reasons), "required_evidence": required}, ensure_ascii=False))
    met = [c for c in report.criteria if c not in blocking]
    if met:
        lines.append("Already satisfied, do not regress: " + "; ".join(f"{c.id} {c.text}" for c in met))
    lines.append("Meet each claim, check it yourself, and only then finish.")
    return "\n".join(lines)


def summary(report: Report) -> list[dict[str, Any]]:
    """Per-criterion record for the event log."""
    return [{"id": c.id, "met": c.met, "grounded": c.grounded, "evidence": c.evidence[:10], "unresolved": c.unresolved[:10],
             "required_evidence": c.required_evidence[:10]} for c in report.criteria]

