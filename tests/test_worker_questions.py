"""A worker blocked on `orca orchestration ask` shows on the board (and in `inbox`) as waiting, not running, with the
command that answers it; a dispatched task whose worker went silent says for how long."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coding_agent import cli, coordinator, orca, orca_cli
from coding_agent.orca_cli import worker_activity as real_worker_activity
from test_auto_clean import orca as orca_fake, repo  # noqa: F401  (fixtures)

RUN = "run_8c7da24a71fb"


def _msg(mid: str, created: str, seq: int, *, type: str = "heartbeat", frm: str = "term_w", to: str = f"run:{RUN}",
         thread: str | None = None, payload: dict | None = None, subject: str = "alive", body: str = "") -> dict:
    return {"id": mid, "run_id": RUN, "from_handle": frm, "to_handle": to, "type": type, "subject": subject, "body": body,
            "thread_id": thread, "payload": json.dumps(payload) if payload is not None else None,
            "created_at": created, "sequence": seq}


QUESTION_PAYLOAD = {"taskId": "task_q", "dispatchId": "ctx_q", "question": "Can quyet 3 diem. Dong y?",
                    "options": ["dong y de xuat", "bo qua", "khac (ghi ro)"]}


def _question() -> dict:
    # The shape Orca stored for msg_431126d660e1: sent from the dispatch handle, its own id as thread.
    return _msg("msg_q", "2026-10-11T12:42:56Z", 240, type="question", frm="dispatch:ctx_q", thread="msg_q",
                payload=QUESTION_PAYLOAD, subject="Question", body="Can quyet 3 diem. Dong y?")


def _heartbeat(mid: str, created: str, seq: int, task: str = "task_q") -> dict:
    return _msg(mid, created, seq, payload={"taskId": task, "dispatchId": f"ctx_{task[5:]}", "phase": "implementing"})


# --- orca_cli.open_questions ------------------------------------------------------------------------------------


def test_a_question_nobody_answered_is_open() -> None:
    questions = orca_cli.open_questions([_question()], [])

    assert [(q["id"], q["task_id"], q["dispatch"], q["options"]) for q in questions] == [
        ("msg_q", "task_q", "ctx_q", ["dong y de xuat", "bo qua", "khac (ghi ro)"])]
    assert questions[0]["question"] == "Can quyet 3 diem. Dong y?"
    assert questions[0]["run"] == RUN


def test_heartbeat_and_a_plain_send_to_the_dispatch_do_not_answer_it() -> None:
    # 2026-10-11: the coordinator sent an unrelated follow-up to the dispatch and the worker kept heartbeating while
    # its ask had timed out; the question was still unanswered.
    follow_up = _msg("msg_f", "2026-10-11T12:49:48Z", 244, type="status", frm="term_coord", to="dispatch:ctx_q", subject="dung ghi de")
    run_messages = [_heartbeat("msg_h", "2026-10-11T12:54:04Z", 245), _question()]

    assert [q["id"] for q in orca_cli.open_questions(run_messages, [follow_up])] == ["msg_q"]


def test_a_reply_in_its_thread_answers_it() -> None:
    reply = _msg("msg_r", "2026-10-11T12:56:16Z", 246, type="status", frm=f"run:{RUN}", to="dispatch:ctx_q", thread="msg_q",
                 subject="Re: Question")

    assert orca_cli.open_questions([_question()], [reply]) == []


def test_a_later_report_of_the_task_answers_it() -> None:
    done = _msg("msg_d", "2026-10-11T12:57:38Z", 247, type="worker_done", payload={"taskId": "task_q", "dispatchId": "ctx_q"})

    assert orca_cli.open_questions([done, _question()], []) == []


def test_a_later_question_of_the_dispatch_replaces_the_earlier_one() -> None:
    second = _msg("msg_q2", "2026-10-11T13:10:00Z", 250, type="question", frm="dispatch:ctx_q", thread="msg_q2",
                  payload={**QUESTION_PAYLOAD, "question": "Con mot diem nua?"})

    assert [q["id"] for q in orca_cli.open_questions([second, _question()], [])] == ["msg_q2"]


def test_an_escalation_is_a_question_too() -> None:
    escalation = _msg("msg_e", "2026-10-11T12:00:00Z", 10, type="escalation", subject="Blocked: no access", body="need a token",
                      payload={"taskId": "task_e", "dispatchId": "ctx_e"})

    assert [(q["id"], q["type"], q["question"]) for q in orca_cli.open_questions([escalation], [])] == [("msg_e", "escalation", "need a token")]


def test_last_heard_is_the_latest_message_of_each_task() -> None:
    run_messages = [_heartbeat("m2", "2026-10-11T12:30:00Z", 2), _heartbeat("m1", "2026-10-11T12:00:00Z", 1),
                    _heartbeat("m3", "2026-10-11T11:00:00Z", 3, task="task_other")]

    assert orca_cli.last_heard(run_messages) == {"task_q": "2026-10-11T12:30:00Z", "task_other": "2026-10-11T11:00:00Z"}


def test_worker_activity_reads_the_run_and_the_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    reply = _msg("msg_r", "2026-10-11T12:56:16Z", 246, type="status", frm=f"run:{RUN}", to="dispatch:ctx_q", thread="msg_q")

    def call(*args: str, accept_ok_exit: bool = False) -> dict:
        calls.append(args)
        if args[0] == "check":
            return {"ok": True, "result": {"messages": [_question()], "runId": RUN}}
        raise orca_cli.OrcaError("orca inbox exited 1: boom")

    monkeypatch.setattr(orca_cli, "_call", call)
    questions, heard = real_worker_activity(RUN)
    assert [q["id"] for q in questions] == ["msg_q"]
    assert heard == {"task_q": "2026-10-11T12:42:56Z"}
    assert calls[0] == ("check", "--all", "--run", RUN)
    assert calls[1][:2] == ("inbox", "--full")

    monkeypatch.setattr(orca_cli, "_call", lambda *args, **kwargs: {"ok": True, "result": {"messages": [_question()] if args[0] == "check" else [reply]}})
    assert real_worker_activity(RUN)[0] == []


# --- the board --------------------------------------------------------------------------------------------------


NOW = datetime(2026, 10, 11, 13, 40, tzinfo=timezone.utc)


def _tasks() -> list[dict]:
    return [{"id": "task_q", "status": "dispatched", "title": "Nang cap harness"},
            {"id": "task_run", "status": "dispatched", "title": "Dang chay"},
            {"id": "task_quiet", "status": "dispatched", "title": "Im lang"}]


def test_board_lists_the_waiting_worker_first_and_not_as_running() -> None:
    owners = {"task_q": "home", "task_run": "home", "task_quiet": "home"}
    heard = {"task_q": "2026-10-11T12:42:56Z", "task_run": "2026-10-11T13:35:00Z", "task_quiet": "2026-10-11T13:05:00Z"}
    questions = orca_cli.open_questions([_question()], [])

    lines = coordinator.board_lines(_tasks(), owners, "home", questions=questions, heard=heard, now=NOW, run=RUN)

    assert lines[0] == "worker đang hỏi, chờ trả lời (1):"
    assert lines[1] == "  - task_q: Can quyet 3 diem. Dong y?"
    assert lines[2] == "    lựa chọn: dong y de xuat | bo qua | khac (ghi ro)"
    assert lines[3] == f'    msg msg_q; trả lời: `orca orchestration reply --id msg_q --run {RUN} --body "..."`'
    running = lines[lines.index("đang chạy (2):") + 1:]
    assert running[:2] == ["  - task_run [dispatched] Dang chay", "  - task_quiet [dispatched] Im lang (không có tin 35 phút)"]
    assert not any(line.startswith("  - task_q [") for line in lines)


def test_board_cuts_a_long_question_and_names_the_repo() -> None:
    long = {**_question(), "payload": json.dumps({**QUESTION_PAYLOAD, "question": "x" * 500})}
    questions = orca_cli.open_questions([long], [])

    lines = coordinator.board_lines(_tasks(), {"task_q": "other", "task_run": "home"}, "home", {"other"}, questions, now=NOW)

    assert lines[1] == "  - task_q (repo other): " + "x" * 200 + "…"


def test_board_leaves_out_questions_of_another_coordinators_task() -> None:
    questions = orca_cli.open_questions([_question()], [])

    lines = coordinator.board_lines(_tasks(), {"task_run": "home"}, "home", questions=questions, now=NOW)

    assert "worker đang hỏi" not in "\n".join(lines)


def test_read_board_shows_the_question(repo: Path, orca_fake: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    orca.link(repo, t_id="T-q", task_id="task_q", project=orca.project_name(repo))
    orca_fake["tasks"]["task_q"] = "dispatched"
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    monkeypatch.setattr(orca_cli, "worker_activity", lambda run=None: (orca_cli.open_questions([_question()], []), {}))

    lines, error = coordinator.read_board(repo)

    text = "\n".join(lines)
    assert error is None
    assert "worker đang hỏi, chờ trả lời (1):\n  - task_q: Can quyet 3 diem. Dong y?" in text
    assert "đang chạy" not in text


def test_inbox_prints_open_questions(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(orca_cli, "_call", lambda *args, **kwargs: {"ok": True, "result": {"run": None}})
    monkeypatch.setattr(orca_cli, "unread_reports", lambda run=None: [])
    monkeypatch.setattr(orca_cli, "worker_activity", lambda run=None: (orca_cli.open_questions([_question()], []), {}))

    assert cli.main(["--root", str(repo), "inbox"]) == 0

    out = capsys.readouterr().out
    assert "worker đang hỏi, chờ trả lời (1):" in out
    assert "orca orchestration reply --id msg_q" in out
    assert "inbox: no worker reports" in out


def test_contract_says_to_answer_and_to_ask_the_user_first() -> None:
    text = coordinator.contract("src")

    assert "worker đang hỏi, chờ trả lời" in text
    assert "orca orchestration reply --id <msg> --run <run>" in text
    assert "hỏi người dùng trong hội thoại trước" in text
