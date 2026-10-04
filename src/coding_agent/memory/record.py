"""Turn → spool records (FR-019, memory section of integration.yaml).

Conversation records are the human prompt and the final response. An episode is added when the
turn is verified (see episodes.refusal). Every UUID is derived from the transcript line that
produced it, so recording the same turn again gives the same keys and zm stores it once.
"""

from __future__ import annotations

import time
from typing import Any

from coding_agent.manifest import Memory
from coding_agent.memory import episodes
from coding_agent.transcript import Turn


def _record(session: str, speaker: str, text: str, ts: int, uuid: str) -> dict[str, Any]:
    return {"session_id": session, "speaker": speaker, "text": text, "ts": ts, "uuid": uuid}


def records_for_turn(session: str, turn: Turn, *, verdict_ok: bool, verdict_line: int, memory: Memory, now: int | None = None) -> list[dict[str, Any]]:
    """Spool records for one completed turn. `verdict_line` is the transcript's last line when verification ran."""
    stamp = now if now is not None else int(time.time())
    out: list[dict[str, Any]] = []

    if memory.ingest in ("conversation", "both"):
        if turn.prompt and turn.prompt_line is not None:
            out.append(_record(session, "user", turn.prompt, turn.prompt_ts or stamp, f"coding-agent:{session}:{turn.prompt_line}"))
        if turn.response and turn.response_line is not None:
            out.append(_record(session, "assistant", turn.response, turn.response_ts or stamp, f"coding-agent:{session}:{turn.response_line}"))

    if memory.ingest in ("episodes", "both") and turn.prompt and turn.response and turn.response_line is not None:
        changes = turn.change_list()
        if episodes.refusal(changes, has_response=True, verdict_ok=verdict_ok) is None:
            text = episodes.content(turn.prompt, turn.response, memory.transient_markers, memory.max_episode_request_chars, memory.max_episode_outcome_chars)
            body = episodes.render(turn.turn, turn.response_line, verdict_line, changes, text, memory.max_episode_chars)
            if body is not None:
                out.append(_record(
                    session, "assistant", body, turn.response_ts or stamp,
                    f"coding-agent:episode:{session}:{turn.turn}:{turn.response_line}",
                ))
    return out
