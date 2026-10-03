from coding_agent.manifest import Memory
from coding_agent.memory import episodes
from coding_agent.memory.record import records_for_turn
from coding_agent.transcript import Turn

MARKERS = ("this session", "for now", "tạm thời")


def test_transient_sentences_are_dropped_case_insensitively():
    text = "Added retry. For now, lint is skipped! Tests pass.\nTạm thời bỏ qua.\n\nDone?"
    assert episodes.filter_transient(text, MARKERS) == "Added retry. Tests pass.\nDone?"


def test_refusal_reasons_follow_the_distill_rule():
    assert episodes.refusal([], True, True) == "no-changes"
    assert episodes.refusal([("a.py", 3)], False, True) == "no-response"
    assert episodes.refusal([("a.py", 3)], True, False) == "verdict-not-ok"
    assert episodes.refusal([("a.py", 3)], True, True) is None


def test_render_keeps_identifiers_and_cuts_the_request_then_the_outcome():
    text = episodes.Content(summary="x", request="r" * 500, outcome="o" * 500)
    fixed = episodes.render(1, 4, 9, [("src/a.py", 3)], episodes.Content("x", "", ""), 10_000)
    body = episodes.render(1, 4, 9, [("src/a.py", 3)], text, len(fixed) + 120)
    assert body is not None and len(body) <= len(fixed) + 120
    assert "Final response line: 4" in body and "- src/a.py — successful change at line 3" in body
    assert " …" in body


def test_render_refuses_a_limit_below_the_fixed_lines():
    small = len("\n".join(episodes._lines(1, 4, 9, [("a.py", 3)], "", "")))
    assert episodes.render(1, 4, 9, [("a.py", 3)], episodes.Content("x", "r", "o"), small - 1) is None
    assert episodes.render(1, 4, 9, [("a.py", 3)], episodes.Content("x", "r", "o"), small) is not None


def test_records_for_a_verified_turn_have_stable_keys():
    turn = Turn(turn=2, prompt="Add retry to the client.", prompt_line=1, response="Added three attempts.", response_line=6,
                changes={"src/retry.py": 4})
    memory = Memory(ingest="both", embedder="hash", change_tools=("Write",), transient_markers=MARKERS,
                    max_episode_request_chars=1000, max_episode_outcome_chars=2000, max_episode_chars=6000)
    records = records_for_turn("s1", turn, verdict_ok=True, verdict_line=7, memory=memory, now=1790000000)
    assert [r["uuid"] for r in records] == [
        "coding-agent:s1:1", "coding-agent:s1:6", "coding-agent:episode:s1:2:6",
    ]
    assert records[2]["text"].startswith("# Verified episode")
    again = records_for_turn("s1", turn, verdict_ok=True, verdict_line=7, memory=memory, now=1790000000)
    assert [r["uuid"] for r in again] == [r["uuid"] for r in records]


def test_unverified_turns_store_conversation_only():
    turn = Turn(turn=1, prompt="Add retry.", prompt_line=1, response="Added.", response_line=3, changes={"a.py": 2})
    memory = Memory(ingest="both", embedder="hash", change_tools=("Write",), transient_markers=(),
                    max_episode_request_chars=1000, max_episode_outcome_chars=2000, max_episode_chars=6000)
    records = records_for_turn("s2", turn, verdict_ok=False, verdict_line=4, memory=memory, now=1)
    assert [r["speaker"] for r in records] == ["user", "assistant"]
    episodes_only = Memory(ingest="episodes", embedder="hash", change_tools=("Write",), transient_markers=(),
                           max_episode_request_chars=1000, max_episode_outcome_chars=2000, max_episode_chars=6000)
    assert records_for_turn("s2", turn, verdict_ok=True, verdict_line=4, memory=episodes_only, now=1)[0]["uuid"].startswith("coding-agent:episode:")
