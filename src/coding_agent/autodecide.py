"""Auto-decide: a question to the user waits `timeout_s` seconds, then the agent decides (FR-001 to FR-010).

The manifest's `auto_decide:` block holds the defaults; `cli auto-decide` writes the user's choice to
`.coding-agent/state/auto-decide.json`, which wins over the manifest and applies from the next question.

The question is shown in a countdown window (`python -m coding_agent.autodecide window`, tkinter). The user's
choice wins; when the wait runs out Fable picks one of the options, and the option the agent suggested is the
fallback. A window that cannot open, or that the user closes, leaves the question to the host.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from coding_agent import state
from coding_agent.manifest import AutoDecide, ManifestError, auto_decide_from, load

STATE_FILE = Path(".coding-agent") / "state" / "auto-decide.json"
FAKE_ENV = "CODING_AGENT_AUTODECIDE_FAKE"
NESTED_ENV = "CODING_AGENT_AUTODECIDE_NESTED"
MULTI_SEP = ", "
RECOMMENDED = re.compile(r"\((?:recommended|khuyến nghị|khuyen nghi)\)", re.IGNORECASE)
# Flags a question about a destructive or outward action. It only marks the decision; it never stops one (FR-008).
SENSITIVE = re.compile(
    r"xoá|xóa|\bxoa\b|delete|remove|\brm\b|ghi đè|overwrite|force|commit|push|merge|deploy|publish|gửi|\bsend\b|drop|reset|discard",
    re.IGNORECASE,
)
FABLE_MODEL = "claude-fable-5-1"
FABLE_FAKE_ENV = "CODING_AGENT_AUTODECIDE_FABLE_FAKE"
CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class Reply:
    """What the window returned: `answered` (with answers), `timeout`, or `error` (with detail)."""

    outcome: str
    answers: dict[str, str] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class Decision:
    """`answers` None means the host asks the user as usual. `by` is user, fable, suggested or fallback."""

    answers: dict[str, str] | None
    by: str
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def effective(root: Path, base: AutoDecide | None = None) -> AutoDecide:
    """`base` (the manifest's settings) overridden by the state file. A state file that fails validation is ignored."""
    base = base or AutoDecide()
    try:
        data: Any = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    merged = {**asdict(base), **{key: value for key, value in data.items() if key in AutoDecide.__dataclass_fields__}}
    try:
        return auto_decide_from(merged, "auto-decide.json")
    except ManifestError:
        return base


def save(root: Path, settings: AutoDecide) -> None:
    state._write_json(root / STATE_FILE, asdict(settings))


def manifest_settings(root: Path) -> AutoDecide:
    """The `auto_decide:` block of `root/integration.yaml`, or the defaults when there is no usable manifest."""
    path = root / "integration.yaml"
    if not path.exists():
        return AutoDecide()
    try:
        return load(path.resolve()).auto_decide
    except (ManifestError, OSError, ImportError, ValueError):
        return AutoDecide()


def questions_of(tool_input: Any) -> list[dict[str, Any]]:
    """The AskUserQuestion questions that carry a question text and at least one labelled option."""
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return [q for q in raw or [] if isinstance(q, dict) and q.get("question") and labels_of(q)]


def labels_of(question: dict[str, Any]) -> list[str]:
    return [str(o["label"]) for o in question.get("options") or [] if isinstance(o, dict) and o.get("label")]


def suggested(questions: list[dict[str, Any]]) -> dict[str, str]:
    """The option the agent suggested for each question: those marked recommended, else the first one."""
    answers: dict[str, str] = {}
    for question in questions:
        labels = labels_of(question)
        picked = [label for label in labels if RECOMMENDED.search(label)]
        if not question.get("multiSelect"):
            picked = picked[:1]
        answers[str(question["question"])] = MULTI_SEP.join(picked or labels[:1])
    return answers


def valid_answer(questions: list[dict[str, Any]], answers: Any) -> bool:
    """True when every question has an answer made only of its own labels."""
    if not isinstance(answers, dict):
        return False
    for question in questions:
        value = answers.get(str(question["question"]))
        if not isinstance(value, str) or not value:
            return False
        chosen = value.split(MULTI_SEP) if question.get("multiSelect") else [value]
        if any(label not in labels_of(question) for label in chosen):
            return False
    return True


def is_sensitive(questions: list[dict[str, Any]]) -> bool:
    for question in questions:
        options = question.get("options") or []
        text = " ".join([str(question.get("question") or "")] + [
            f"{o.get('label', '')} {o.get('description', '')}" for o in options if isinstance(o, dict)])
        if SENSITIVE.search(text):
            return True
    return False


def _fake_reply(fake: str, questions: list[dict[str, Any]]) -> Reply:
    if fake.startswith("choice:"):
        index = max(int(fake.split(":", 1)[1]) - 1, 0)
        answers = {str(q["question"]): labels_of(q)[min(index, len(labels_of(q)) - 1)] for q in questions}
        return Reply("answered", answers)
    if fake in ("timeout", "error"):
        return Reply(fake, detail="fake")
    return Reply("error", detail="dismissed")


def ask_window(questions: list[dict[str, Any]], timeout_s: int) -> Reply:
    """Show the questions with a countdown in a separate process (`python -m coding_agent.autodecide window`)."""
    fake = os.environ.get(FAKE_ENV)
    if fake:
        return _fake_reply(fake, questions)
    request = json.dumps({"questions": questions, "timeout_s": timeout_s}, ensure_ascii=False)
    try:
        done = subprocess.run([sys.executable, "-m", "coding_agent.autodecide", "window"], input=request,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout_s + 5, check=False,
                              # A Windows console decodes stdin and encodes stdout as cp1252 unless told otherwise.
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return Reply("timeout", detail="window did not close in time")
    except OSError as error:
        return Reply("error", detail=str(error)[:300])
    if done.returncode != 0:
        return Reply("error", detail=(done.stderr or f"window exited {done.returncode}").strip()[:300])
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        return Reply("error", detail=f"window printed {done.stdout[:200]!r}")
    if isinstance(data, dict) and data.get("timeout") is True:
        return Reply("timeout")
    if isinstance(data, dict) and data.get("dismissed") is True:
        return Reply("error", detail="dismissed")
    answers = data.get("answers") if isinstance(data, dict) else None
    if valid_answer(questions, answers):
        return Reply("answered", answers)
    return Reply("error", detail=f"window answers not in the options: {str(answers)[:200]}")


def window_main() -> int:
    """The countdown window: one line of JSON on stdout, `answers`, `timeout` or `dismissed`. Exit 3 when no window can open."""
    try:
        # A Windows PowerShell pipe starts the text with a BOM.
        request = json.loads(sys.stdin.read().lstrip("﻿"))
        questions = questions_of(request)
        timeout_s = int(request.get("timeout_s", 60))
    except (ValueError, TypeError, AttributeError):
        return 2
    if not questions:
        return 2
    try:
        import tkinter as tk

        window = tk.Tk()
    except Exception as error:  # ImportError, or TclError when there is no display
        print(f"auto-decide window: {error}", file=sys.stderr)
        return 3

    result: dict[str, Any] = {"timeout": True}
    window.title("coding-agent: auto-decide")
    window.attributes("-topmost", True)
    choices: list[tuple[dict[str, Any], Any]] = []
    for question in questions:
        frame = tk.LabelFrame(window, text=str(question.get("header") or ""), padx=8, pady=6)
        frame.pack(fill="x", padx=10, pady=6)
        tk.Label(frame, text=str(question["question"]), wraplength=480, justify="left", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        options = [o for o in question.get("options") or [] if isinstance(o, dict) and o.get("label")]
        if question.get("multiSelect"):
            checks = []
            for option in options:
                var = tk.BooleanVar(value=False)
                text = str(option["label"]) + (f" — {option['description']}" if option.get("description") else "")
                tk.Checkbutton(frame, text=text, variable=var, wraplength=480, justify="left", anchor="w").pack(anchor="w")
                checks.append((str(option["label"]), var))
            choices.append((question, checks))
        else:
            var = tk.StringVar(value="")
            for option in options:
                text = str(option["label"]) + (f" — {option['description']}" if option.get("description") else "")
                tk.Radiobutton(frame, text=text, value=str(option["label"]), variable=var, wraplength=480, justify="left", anchor="w").pack(anchor="w")
            choices.append((question, var))

    status = tk.Label(window, text="", fg="#b45309")
    status.pack(padx=10, pady=(4, 0))

    def collect() -> dict[str, str]:
        answers: dict[str, str] = {}
        for question, var in choices:
            if question.get("multiSelect"):
                answers[str(question["question"])] = MULTI_SEP.join(label for label, check in var if check.get())
            else:
                answers[str(question["question"])] = var.get()
        return answers

    def submit() -> None:
        answers = collect()
        if not valid_answer(questions, answers):
            status.config(text="Chọn ít nhất một phương án cho mỗi câu hỏi.")
            return
        result.clear()
        result["answers"] = answers
        window.destroy()

    def take_suggested() -> None:
        result.clear()
        result["answers"] = suggested(questions)
        window.destroy()

    def dismiss() -> None:
        result.clear()
        result["dismissed"] = True
        window.destroy()

    buttons = tk.Frame(window)
    buttons.pack(fill="x", padx=10, pady=8)
    tk.Button(buttons, text="Gửi", command=submit, width=12).pack(side="right")
    tk.Button(buttons, text="Chọn gợi ý ngay", command=take_suggested).pack(side="right", padx=6)
    countdown = tk.Label(buttons, text="")
    countdown.pack(side="left")
    window.protocol("WM_DELETE_WINDOW", dismiss)

    deadline = time.monotonic() + timeout_s

    def tick() -> None:
        left = int(deadline - time.monotonic() + 0.999)
        if left <= 0:
            window.destroy()
            return
        countdown.config(text=f"Tự chọn sau {left} giây")
        window.after(250, tick)

    tick()
    window.mainloop()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def fable_prompt(questions: list[dict[str, Any]], context: str) -> str:
    listed = [{"question": q["question"], "multiSelect": bool(q.get("multiSelect")),
               "options": [{"label": o.get("label"), "description": o.get("description", "")}
                           for o in q.get("options") or [] if isinstance(o, dict) and o.get("label")]}
              for q in questions]
    return "\n".join([
        "Bạn quyết định thay một người dùng đang vắng mặt cho một coding agent: họ không trả lời các câu hỏi dưới đây trong thời gian chờ.",
        f"Cho MỖI câu hỏi, chọn đúng một nhãn trong danh sách của nó (câu multiSelect: một hoặc nhiều nhãn, nối bằng '{MULTI_SEP}'). Không bịa nhãn mới.",
        'Chỉ trả về một object JSON: {"answers": {"<câu hỏi>": "<nhãn>"}, "reason": "<một câu lý do>"}.',
        "",
        "Ngữ cảnh của phiên:",
        context[-CONTEXT_CHARS:] or "(không có)",
        "",
        "Câu hỏi:",
        json.dumps(listed, ensure_ascii=False, indent=1),
    ])


def parse_fable(stdout: str) -> tuple[dict[str, str], str] | None:
    """The answers and reason in `claude -p --output-format json` output (or in plain text), or None."""
    try:
        envelope = json.loads(stdout)
        text = envelope.get("result") if isinstance(envelope, dict) else None
    except json.JSONDecodeError:
        text = stdout
    if not isinstance(text, str):
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return None
    return {str(k): str(v) for k, v in data["answers"].items()}, str(data.get("reason") or "")[:300]


def ask_fable(questions: list[dict[str, Any]], context: str, timeout_s: int) -> tuple[dict[str, str], str] | None:
    """Fable's answers and reason, or None when `claude` is missing, fails, times out or answers outside the options."""
    fake = os.environ.get(FABLE_FAKE_ENV)
    if fake is not None:
        if fake == "error":
            return None
        stdout = fake
    else:
        claude = shutil.which("claude")
        if claude is None:
            return None
        try:
            # Run outside the project so its hooks do not load; NESTED_ENV keeps auto-decide out of Fable's own session.
            done = subprocess.run([claude, "-p", "--model", FABLE_MODEL, "--output-format", "json"],
                                  input=fable_prompt(questions, context), capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout_s, cwd=tempfile.gettempdir(), env={**os.environ, NESTED_ENV: "1"}, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0:
            return None
        stdout = done.stdout
    parsed = parse_fable(stdout)
    if parsed is None or not valid_answer(questions, parsed[0]):
        return None
    return parsed


def decide(tool_input: dict[str, Any], settings: AutoDecide, context: str = "") -> Decision:
    """Ask in the window; when the wait runs out, decide for the user. A window that cannot open is a fallback."""
    questions = questions_of(tool_input)
    if not questions:
        return Decision(None, "fallback", detail={"error": "no question with options"})
    asked = [str(q["question"]) for q in questions]
    reply = ask_window(questions, settings.timeout_s)
    if reply.outcome == "error":
        return Decision(None, "fallback", detail={"questions": asked, "error": reply.detail})
    if reply.outcome == "answered":
        note = "[auto-decide] Người dùng đã trả lời trong cửa sổ auto-decide."
        return Decision(reply.answers, "user", note, {"questions": asked, "answers": reply.answers, "by": "user"})
    answers, by, reason = suggested(questions), "suggested", ""
    if settings.fable:
        picked = ask_fable(questions, context, settings.fable_timeout_s)
        if picked is not None:
            answers, reason = picked
            by = "fable"
    sensitive = is_sensitive(questions)
    chosen = "; ".join(f"{question} → {answer}" for question, answer in answers.items())
    who = "Fable" if by == "fable" else "gợi ý của agent"
    note = (f"[auto-decide] Người dùng không trả lời sau {settings.timeout_s} giây; đã tự chọn: {chosen} "
            f"(người chọn: {who}{'; lý do: ' + reason if reason else ''}){' [sensitive]' if sensitive else ''}. "
            "Hãy báo lại lựa chọn này cho người dùng.")
    detail = {"questions": asked, "answers": answers, "by": by, "waited_s": settings.timeout_s,
              "sensitive": sensitive, "reason": reason}
    return Decision(answers, by, note, detail)


def hook_output(tool_input: dict[str, Any], answers: dict[str, str], note: str) -> str:
    """PreToolUse output that lets AskUserQuestion run with the answers filled in."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": note,
        "updatedInput": {**tool_input, "answers": answers},
        "additionalContext": note,
    }}, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["window"]:
        return window_main()
    print("usage: python -m coding_agent.autodecide window", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
