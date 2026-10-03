import json
from datetime import datetime, timezone
from pathlib import Path

from coding_agent.transcript import read_turn


def line(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False)


def write(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(line(e) for e in entries) + "\n", encoding="utf-8")
    return path


def test_reads_the_last_turn_with_cited_lines(tmp_path):
    transcript = write(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "first request"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]}},
        {"type": "user", "message": {"role": "user", "content": "Add retry to the client."}, "timestamp": "2026-10-03T10:00:00Z"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "u1", "name": "Edit", "input": {"file_path": "src/retry.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "is_error": False}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Added three attempts."}]}},
    ])
    turn = read_turn(transcript, ("Write", "Edit", "MultiEdit"))
    assert turn.turn == 2
    assert turn.prompt == "Add retry to the client." and turn.prompt_line == 3
    assert turn.response == "Added three attempts." and turn.response_line == 6
    assert turn.change_list() == [("src/retry.py", 5)]
    assert turn.prompt_ts == int(datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc).timestamp())


def test_failed_and_untracked_tools_are_not_changes(tmp_path):
    transcript = write(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "Write", "input": {"file_path": "bad.py"}},
            {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "read.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "is_error": True},
            {"type": "tool_result", "tool_use_id": "b", "is_error": False},
        ]}},
    ])
    assert read_turn(transcript, ("Write", "Edit")).changes == {}


def test_damaged_lines_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"type": "user", "message": {"content": "hi"}}\n{not json\n[1, 2]\n', encoding="utf-8")
    turn = read_turn(path, ("Write",))
    assert turn.prompt == "hi"


def test_missing_transcript_gives_an_empty_turn(tmp_path):
    turn = read_turn(tmp_path / "absent.jsonl", ("Write",))
    assert turn.prompt is None and turn.response == "" and turn.changes == {}
