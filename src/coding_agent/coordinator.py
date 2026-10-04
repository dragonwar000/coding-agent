"""The coordinator role: the main session plans, delegates to Orca workers, and stays free to answer the user.

Role is decided per session. The main worktree of the repository is the coordinator; a linked worktree
(where `worker-start --worktree new-child` puts a worker) is a worker. `CODING_AGENT_ROLE=coordinator|worker`
overrides the detection. A worker never gets the coordinator contract, and the coordinator guard does
nothing in a worker.

The guard blocks direct file writes and mutating shell commands in the coordinator, so the work goes to a
worker. The contract and the task board are injected at session start and on every prompt, so the
coordinator keeps the user's question in view while workers run.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from coding_agent import orca, orca_cli

ROLE_ENV = "CODING_AGENT_ROLE"
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
BOARD_LIMIT = 20

# Shell forms that change files or repository state. Read-only forms (git status/log/diff, ls, cat, grep)
# and the coding-agent CLI itself are not matched. A heuristic: anything it misses is still visible in events.
MUTATING_BASH = re.compile(
    r"(\bsed\s+-i\b|\brm\s|\bmv\s|\bcp\s|\bmkdir\b|\btouch\b|\btee\b|\bchmod\b|\bln\s|"
    r"\bgit\s+(commit|push|reset|checkout|clean|rebase|merge|stash|apply|add|restore|rm)\b|"
    r"\b(pip3?|npm|pnpm|yarn)\s+(install|add|i)\b|"
    r"(?<![0-9&])>{1,2}\s*(?![&]|/dev/null)\S)"
)


def _git(cwd: Path, *args: str) -> str | None:
    try:
        run = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.strip() if run.returncode == 0 and run.stdout.strip() else None


def role_for(cwd: Path | None) -> str:
    """`coordinator` or `worker` for a session working in `cwd`. Outside git, the session is the coordinator."""
    override = os.environ.get(ROLE_ENV)
    if override in ("coordinator", "worker"):
        return override
    if cwd is None:
        return "coordinator"
    git_dir = _git(cwd, "rev-parse", "--git-dir")
    common = _git(cwd, "rev-parse", "--git-common-dir")
    if git_dir is None or common is None:
        return "coordinator"
    same = Path(cwd, git_dir).resolve() == Path(cwd, common).resolve()
    return "coordinator" if same else "worker"


# The coordinator's own delegation command, run alone. A chained or substituted command is not allowed through.
DELEGATE_CALL = re.compile(r"^\s*(PYTHONPATH=\S+\s+)?python3?\s+-m\s+coding_agent\.cli\s")
CHAINING = re.compile(r"(&&|\|\||;|\||`|\$\()")


def guard_reason(tool: str, tool_input: Any) -> str | None:
    """Why the coordinator must not do this call itself, or None when the call is allowed."""
    if tool in WRITE_TOOLS:
        return (
            f"[coordinator] {tool} is a direct change. The coordinator delegates it: "
            "run `coding_agent.cli delegate --title ... --spec ...` and answer the user while the worker runs."
        )
    if tool == "Bash" and isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "")
        if DELEGATE_CALL.search(command) and not CHAINING.search(command):
            return None
        if MUTATING_BASH.search(command):
            return (
                "[coordinator] this shell command changes files or repository state. "
                "The coordinator delegates it: run `coding_agent.cli delegate --title ... --spec ...`."
            )
    return None


def contract(python_src: str) -> str:
    """The coordinator contract injected at session start and after a compaction."""
    return "\n".join([
        "[coordinator] Bạn là coordinator của repo này. Vai trò: nhận yêu cầu, lập kế hoạch, giao việc, và trả lời người dùng.",
        "- Bạn tự quyết định: việc nhỏ, một bước, hoặc cần kết quả ngay thì tự làm; việc lớn, nhiều bước, hoặc chạy lâu thì giao worker.",
        "- Hook chỉ nhắc khi bạn sửa file hay chạy lệnh làm thay đổi trạng thái, không chặn. Ghi ngắn lý do làm trực tiếp hoặc giao việc trong câu trả lời.",
        f"- Giao việc bằng: PYTHONPATH={python_src} python3 -m coding_agent.cli delegate --title \"...\" --spec \"...\" [--agent claude|codex]",
        "- Việc nhiều phần phụ thuộc nhau: viết plan YAML (tasks: id, title, spec, deps) rồi `plan-apply FILE` và `plan-next FILE`; `plan-status FILE` xem graph. Chỉ task đã xong hết phụ thuộc mới được giao.",
        "- Worker chạy trong worktree riêng và ghi kết quả vào Orca. Sau khi giao, trả lời người dùng ngay; không chờ worker.",
        "- Mỗi lượt, đọc bảng việc bên dưới trước khi nói về tiến độ. Không bịa trạng thái task.",
    ])


def board_lines(tasks: list[dict[str, Any]], owners: dict[str, str], project: str) -> list[str]:
    """The task board for this repository, one line per task, grouped by state."""
    mine = [{**task, "project": owners[str(task.get("id"))]} for task in tasks if owners.get(str(task.get("id"))) == project]
    if not mine:
        return ["bảng việc: chưa có task nào của repo này"]
    groups = orca.reconcile(mine, project=project)
    labels = {
        "running": "đang chạy",
        "never_dispatched": "chưa giao worker",
        "stuck": "bị chặn",
        "unverified": "báo xong nhưng chưa có bằng chứng",
        "other": "khác",
    }
    lines: list[str] = []
    for key, label in labels.items():
        items = groups.get(key) or []
        if not items:
            continue
        lines.append(f"{label} ({len(items)}):")
        for task in items:
            title = task.get("task_title") or task.get("title") or ""
            lines.append(f"  - {task.get('id')} [{task.get('status')}] {title}".rstrip())
    return lines[:BOARD_LIMIT] + (["  ..."] if len(lines) > BOARD_LIMIT else [])


def board_context(board: list[str], error: str | None) -> str:
    """The board block injected on each prompt. An unavailable Orca is reported, never hidden."""
    if error is not None:
        hint = " Chưa có Orca Run: chạy `python3 -m coding_agent.cli run-init --objective \"...\"` một lần trong terminal này." if "run_required" in error else ""
        return f"[coordinator] bảng việc không đọc được từ Orca: {error}.{hint} Hỏi người dùng trước khi nói về tiến độ."
    return "[coordinator] bảng việc hiện tại:\n" + "\n".join(board)


def read_board(root: Path, run: str | None = None) -> tuple[list[str], str | None]:
    """The board lines for this repository, or the reason Orca could not be read."""
    orca_cli.use_stored_run(root)
    try:
        tasks = orca_cli.list_tasks(run)
    except orca_cli.OrcaError as error:
        return [], str(error)[:300]
    return board_lines(tasks, orca_cli.links(root), orca.project_name(root)), None
