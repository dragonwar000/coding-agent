"""Verified episodes: the memory-distill rules, ported to Python (FR-019).

A turn becomes an episode only when it changed at least one file, its final response is
non-empty, and the verification commands passed for it. Temporary sentences are dropped from
the request and the outcome by marker. The text fits `max_episode_chars`: the request and then
the outcome shrink, while the identifiers, the verdict, and the changed files are never cut.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NONE = "(none)"
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def filter_transient(text: str, markers: tuple[str, ...]) -> str:
    """Drop every sentence that contains a marker (case-insensitive), then blank lines."""
    lowered = [marker.lower() for marker in markers]
    kept_lines: list[str] = []
    for line in text.split("\n"):
        sentences = [s for s in _SENTENCE.split(line) if not any(m in s.lower() for m in lowered)]
        joined = " ".join(sentences)
        if joined.strip():
            kept_lines.append(joined)
    return "\n".join(kept_lines).strip()


def clip(text: str, maximum: int) -> str:
    """Cut text to `maximum` characters and mark the cut with ` …`."""
    if len(text) <= maximum:
        return text
    return f"{text[:maximum].rstrip()} …"


@dataclass(frozen=True)
class Content:
    summary: str
    request: str
    outcome: str


def content(request: str | None, outcome: str, markers: tuple[str, ...], max_request: int, max_outcome: int) -> Content:
    filtered_request = filter_transient(request or "", markers)
    filtered_outcome = filter_transient(outcome, markers)
    first = filtered_request.split("\n", 1)[0].strip()
    return Content(
        summary="(no request text)" if first == "" else clip(first, 80),
        request=NONE if filtered_request == "" else clip(filtered_request, max_request),
        outcome=NONE if filtered_outcome == "" else clip(filtered_outcome, max_outcome),
    )


def refusal(changes: list[tuple[str, int]], has_response: bool, verdict_ok: bool) -> str | None:
    """Why a turn is not an episode, or None when it is one. A turn with no changed file is ordinary chat."""
    if not changes:
        return "no-changes"
    if not has_response:
        return "no-response"
    if not verdict_ok:
        return "verdict-not-ok"
    return None


def _lines(turn: int, response: int, verdict_seq: int, changes: list[tuple[str, int]], request: str, outcome: str) -> list[str]:
    files = [f"- {path} — successful change at line {line}" for path, line in changes]
    return [
        "# Verified episode",
        f"Turn: {turn}",
        f"Final response line: {response}",
        f"Verifier: ok at line {verdict_seq}",
        "",
        "## Request",
        "",
        request,
        "",
        "## Outcome",
        "",
        outcome,
        "",
        "## Files changed",
        "",
        *files,
        "",
        "## Verification",
        "",
        f"The verifier accepted the final response at line {response}.",
    ]


def _fit(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    if room < 3:
        return ""
    return f"{text[:room - 2].rstrip()} …"


def fixed_chars() -> int:
    """The fewest characters an episode can take: its fixed lines at the largest line numbers, no request, outcome, or file."""
    largest = 10**15
    return len("\n".join(_lines(largest, largest, largest, [], "", "")))


def render(turn: int, response: int, verdict_seq: int, changes: list[tuple[str, int]], text: Content, max_chars: int) -> str | None:
    """The spool text of one episode, or None when the identifiers and changed files alone exceed `max_chars`."""
    fixed = len("\n".join(_lines(turn, response, verdict_seq, changes, "", "")))
    room = max_chars - fixed
    if room < 0:
        return None
    request = _fit(text.request, room)
    outcome = _fit(text.outcome, room - len(request))
    return "\n".join(_lines(turn, response, verdict_seq, changes, request, outcome))
