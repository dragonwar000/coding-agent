"""The coordinator role: the main session plans, delegates to Orca workers, and stays free to answer the user.

Role is decided per session. The main worktree of the repository is the coordinator; a linked worktree
(where `worker-start --worktree new-child` puts a worker) is a worker. `CODING_AGENT_ROLE=coordinator|worker|maintainer`
overrides the detection; a maintainer session is not blocked, and what the guard would have blocked is still logged. A worker never gets the coordinator contract, and the coordinator guard does
nothing in a worker.

The guard blocks direct file writes and mutating shell commands in the coordinator, so the work goes to a
worker. The contract and the task board are injected at session start and on every prompt, so the
coordinator keeps the user's question in view while workers run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from coding_agent import orca, orca_cli

ROLE_ENV = "CODING_AGENT_ROLE"
ROLES = ("coordinator", "worker", "maintainer")
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
BOARD_LIMIT = 20

# Shell forms that change file content or rewrite repository state. Read-only forms (git status/log/diff, ls, cat, grep),
# the coding-agent CLI, and `git add/commit/merge` are not matched: integrating worker branches is the coordinator's job.
# A heuristic: what it misses is recorded by stop-gate as `direct-change`.
MUTATING_BASH = re.compile(
    r"(\bsed\s+-i\b|\brm\s|\bmv\s|\bcp\s|\bmkdir\b|\btouch\b|\btee\b|\bchmod\b|\bln\s|"
    r"\bgit\s+(push|reset|checkout|clean|rebase|stash|apply|restore|rm)\b|"
    r"\b(pip3?|npm|pnpm|yarn)\s+(install|add|i)\b|"
    r"(?<![0-9&])>{1,2}\s*(?![&]|/dev/null)\S)"
)


def code_only(command: str) -> str:
    """`command` with quoted text removed, so a `>` or `rm` inside a string is not read as shell.

    Single-quoted text is dropped whole. Double-quoted text is dropped except `$(...)` and backtick
    substitutions, because the shell runs those. Quotes stay as empty pairs so word boundaries hold.
    """
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        char = command[i]
        if char == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
        elif char == "'":
            end = command.find("'", i + 1)
            i = n if end < 0 else end + 1
            out.append("''")
        elif char == '"':
            i += 1
            kept: list[str] = []
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                elif command.startswith("$(", i):
                    depth, start = 0, i
                    i += 1
                    while i < n:
                        depth += {"(": 1, ")": -1}.get(command[i], 0)
                        i += 1
                        if depth == 0:
                            break
                    kept.append(command[start:i])
                elif command[i] == "`":
                    end = command.find("`", i + 1)
                    stop = n if end < 0 else end + 1
                    kept.append(command[i:stop])
                    i = stop
                else:
                    i += 1
            i += 1
            out.append('"' + " ".join(kept) + '"')
        else:
            out.append(char)
            i += 1
    return "".join(out)


# A shell given its script as an argument (`sh -c '...'`, `eval "..."`) runs the quoted text, so it is checked whole.
SHELL_STRING = re.compile(r"(?:^|[\s;&|(])(?:(?:\S*/)?(?:bash|sh|zsh|dash|ksh)\s+(?:-\w+\s+)*-\w*c\b|eval\s)")


def checked_shell(command: str) -> str:
    """The text of `command` that the guard matches mutating forms against.

    Quoted text is not shell, so it is removed (`code_only`), unless the command hands a quoted script to a
    shell interpreter or to `eval`; then the whole command is checked, because the shell runs that text.
    """
    shell = code_only(command)
    return command if SHELL_STRING.search(shell) else shell


def _git(cwd: Path, *args: str) -> str | None:
    try:
        run = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.strip() if run.returncode == 0 and run.stdout.strip() else None


def role_for(cwd: Path | None) -> str:
    """`coordinator`, `worker`, or `maintainer` for a session working in `cwd`.

    A session is a worker only in a worktree that coding-agent started a worker in (the workers ledger of the
    worktree's own repository).
    Any other session, in the main worktree or in a linked one such as an Orca workspace, is the coordinator.
    `maintainer` comes only from the environment: a person starts the session that way for work that is not
    code (pulling repositories, generating docs). It is not a file the agent could write to lift the guard.
    """
    override = os.environ.get(ROLE_ENV)
    if override in ROLES:
        return override
    if cwd is None:
        return "coordinator"
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return "coordinator"
    return "worker" if str(Path(top).resolve()) in orca_cli.worker_worktrees(cwd) else "coordinator"


PLAN_FILE = Path(".coding-agent") / "plan.yaml"
DELEGATING_TOOLS = frozenset({"Agent", "Task"})
HOW = (
    "Chia việc theo graph: (1) ghi plan vào .coding-agent/plan.yaml (tasks: id, title, spec, deps); "
    "(2) `python3 -m coding_agent.cli run-init --objective \"...\"` nếu chưa có Run; "
    "(3) `python3 -m coding_agent.cli plan-apply .coding-agent/plan.yaml`; "
    "(4) `python3 -m coding_agent.cli plan-next .coding-agent/plan.yaml`. Xem graph: `plan-status`."
)
RECOVER = (
    "Worker không nhận đề bài (Orca báo turn_start_unobserved): `plan-next` tự gửi kick-off; nếu worker vẫn im, chạy "
    "`python3 -m coding_agent.cli worker-kick --task-id <id>`. Báo cáo 'Rejected worker_done' là worker thiếu token: kiểm kết quả rồi "
    "`python3 -m coding_agent.cli worker-settle --task-id <id> --basis verifier [--artifact FILE]` để chốt task."
)
REJECTED_PREFIX = "Rejected worker_done"


def delegate_command(python_src: str | None = None) -> str:
    """The command that hands one job to a worker. Without `python_src` it is the generic form."""
    prefix = f"PYTHONPATH={python_src} " if python_src else ""
    return f'{prefix}python3 -m coding_agent.cli delegate --title "..." --spec "..."'


def delegate_now(python_src: str | None = None) -> str:
    """The instruction closing every refusal: the refused work goes to a worker in this turn."""
    return (
        "Không dừng lượt, không hỏi người dùng, không viết lại lệnh để lách guard: giao việc này ngay bằng "
        f"`{delegate_command(python_src)}`."
    )


def is_plan_file(root: Path, file_path: Any) -> bool:
    """True when `file_path` is the coordinator's plan file, the one file it writes itself."""
    if not isinstance(file_path, str) or not file_path:
        return False
    target = Path(file_path)
    target = target if target.is_absolute() else root / target
    return target.resolve() == (root / PLAN_FILE).resolve()


# The coordinator's own delegation command, run alone. A chained or substituted command is not allowed through.
DELEGATE_CALL = re.compile(r"^\s*(PYTHONPATH=\S+\s+)?python3?\s+-m\s+coding_agent\.cli\s")
# Removing a worker's worktree deletes its directory and branch, so the host asks the user before this command runs
# (unless the session bypasses permissions).
CLEAN_CALL = re.compile(r"coding_agent\.cli\s+(--root\s+\S+\s+)?worktree-clean\b")


# The files that set the hooks' modes. Writing them directly is how a person lifts a guard, so the host asks
# instead of refusing: refusing would leave the guard unable to be turned off from inside the session.
HOOK_CONFIG_FILES = (Path(".claude") / "settings.json", Path(".claude") / "settings.local.json", Path("integration.yaml"))


def is_hook_config(root: Path, file_path: Any) -> bool:
    """True when `file_path` is one of the files that set the hooks' modes in `root`."""
    if not isinstance(file_path, str) or not file_path:
        return False
    target = Path(file_path)
    target = (target if target.is_absolute() else root / target).resolve()
    return any(target == (root / name).resolve() for name in HOOK_CONFIG_FILES)


def needs_user_confirmation(tool: str, tool_input: Any, root: Path | None = None) -> str | None:
    """The question the host should put to the user before this call runs, or None when no confirmation is needed."""
    if not isinstance(tool_input, dict):
        return None
    if tool in WRITE_TOOLS and root is not None:
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if is_hook_config(root, file_path):
            return f"coding-agent: {tool} sửa cấu hình hook ({file_path}), có thể đổi mode của guard. Cho phép?"
        return None
    if tool != "Bash":
        return None
    command = str(tool_input.get("command") or "")
    if CLEAN_CALL.search(command):
        return "coding-agent sắp xoá worktree và nhánh của worker đã xong. Lệnh: " + command.strip()[:300]
    return None


CHAINING = re.compile(r"(&&|\|\||;|\||`|\$\()")


def guard_reason(tool: str, tool_input: Any, root: Path | None = None, *, python_src: str | None = None) -> str | None:
    """Why the coordinator must not do this call itself, or None when the call is allowed.

    Allowed: read-only shell, a single `coding_agent.cli` command, and writing the plan file.
    Refused: any other file write, a mutating shell command, and an in-process subagent, which would bypass the graph.
    Every refusal ends with the delegate command, with `python_src` in it when the caller knows the path.
    """
    data = tool_input if isinstance(tool_input, dict) else {}
    how = f"{HOW} {delegate_now(python_src)}"
    if tool in WRITE_TOOLS:
        if root is not None and is_plan_file(root, data.get("file_path") or data.get("notebook_path")):
            return None
        return f"[coordinator] {tool} là sửa trực tiếp. {how}"
    if tool in DELEGATING_TOOLS:
        return f"[coordinator] subagent nội bộ không qua graph, nên bảng việc không thấy nó. {how}"
    if tool == "Bash":
        command = str(data.get("command") or "")
        shell = checked_shell(command)
        if DELEGATE_CALL.search(command) and not CHAINING.search(shell):
            return None
        if MUTATING_BASH.search(shell):
            return f"[coordinator] lệnh shell này làm thay đổi file hoặc trạng thái repo. {how}"
    return None


def contract(python_src: str) -> str:
    """The coordinator contract injected at session start and after a compaction."""
    return "\n".join([
        "[coordinator] Bạn là coordinator của repo này. Vai trò: nhận yêu cầu, lập kế hoạch, giao việc, và trả lời người dùng.",
        "- Mọi thay đổi file đi qua graph: bạn viết plan, worker Orca làm từng node trong worktree riêng. Ở mode enforce, hook chặn sửa trực tiếp, lệnh shell ghi file, và subagent nội bộ.",
        "- Bạn vẫn đọc file, chạy lệnh đọc, ghi đúng một file (.coding-agent/plan.yaml), và gộp kết quả: `git add`, `git commit`, `git merge <nhánh worker>`.",
        "- Worker tách nhánh từ commit hiện tại: commit việc đang dở trước khi `plan-next`, nếu không worker sẽ không thấy nó.",
        "- Chỉ worker được ghi: mọi lần ghi file, script tạm, hay lệnh shell đổi trạng thái là việc của worker. Bạn không tự làm, và không viết lại lệnh bị chặn để lách guard.",
        "- Khi việc cần ghi, hoặc khi hook chặn một lệnh: giao ngay trong cùng lượt, bằng lệnh delegate (một việc nhỏ) hoặc graph (nhiều việc). Không xin phép người dùng, không kết thúc lượt để chờ. Chỉ hỏi người dùng khi quyết định thật sự là của họ (phạm vi, thao tác phá huỷ hoặc ra bên ngoài), không hỏi \"có giao việc không\".",
        "- Chưa có Orca Run: tự chạy `run-init --objective \"...\"` với mục tiêu một dòng, rồi giao việc. Không nhờ người dùng chạy.",
        f"- Giao việc bằng: {delegate_command(python_src)} [--agent claude|codex]",
        "- Folder project (root không phải repo git, chứa nhiều repo con): mọi lệnh delegate phải có `--repo <đường dẫn repo>`, và mỗi node trong plan đặt `repo:`. Worker tách nhánh từ HEAD của repo đó; đổi bằng `--base-branch` / `base:`.",
        "- " + HOW + " Chỉ node đã xong hết phụ thuộc mới được giao.",
        "- `title` của mỗi node là tóm tắt việc cần làm, ngắn và súc tích, khoảng 3 đến 6 từ, không tiền tố chung. Nó thành tên worktree, tên nhánh (cắt ở 40 ký tự, bỏ dấu) và nhãn của worker trong Orca. Chi tiết để trong `spec`.",
        "- Khi bảng việc ghi 'worker vừa báo': kiểm kết quả, gộp nhánh nếu đạt, chạy `plan-next`, rồi `inbox --ack`.",
        "- " + RECOVER,
        "- Khi bảng việc ghi 'worktree chờ dọn': hỏi người dùng có xoá không, rồi chạy `worktree-clean --task-id <id> --yes`. Host hỏi người dùng xác nhận lệnh đó, trừ khi phiên đang bypass permissions; dù vậy bạn vẫn phải hỏi người dùng trong hội thoại trước khi chạy với `--yes`. Worktree còn việc chưa gộp thì gộp trước; lệnh từ chối xoá nó.",
        "- Task `completed` do worker tự báo là chưa có bằng chứng: kiểm kết quả (đọc diff của worktree, chạy verify) trước khi báo người dùng là xong.",
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
        if "run_required" in error:
            return (
                f"[coordinator] bảng việc không đọc được từ Orca: {error}. Chưa có Orca Run: tự chạy "
                "`python3 -m coding_agent.cli run-init --objective \"...\"` với mục tiêu một dòng rồi giao việc; không nhờ người dùng chạy. "
                "Không bịa trạng thái task."
            )
        return f"[coordinator] bảng việc không đọc được từ Orca: {error}. Không bịa trạng thái task; hỏi người dùng trước khi nói về tiến độ."
    return "[coordinator] bảng việc hiện tại:\n" + "\n".join(board)


def read_board(root: Path, run: str | None = None) -> tuple[list[str], str | None]:
    """The board lines for this repository, or the reason Orca could not be read."""
    orca_cli.use_stored_run(root)
    try:
        tasks = orca_cli.list_tasks(run)
    except orca_cli.OrcaError as error:
        return [], str(error)[:300]
    lines = board_lines(tasks, orca_cli.links(root), orca.project_name(root))
    try:
        reports = orca_cli.unread_reports(run)
    except orca_cli.OrcaError as error:
        return lines + [f"hộp thư worker không đọc được: {str(error)[:160]}"], None
    lines = lines + report_lines(reports, _plan_ids(root))
    try:
        from coding_agent import cleanup

        lines = lines + cleanup.board_lines(cleanup.candidates(root, run))
    except orca_cli.OrcaError as error:
        lines.append(f"danh sách worktree chờ dọn không đọc được: {str(error)[:160]}")
    return lines, None


def _plan_ids(root: Path) -> dict[str, str]:
    """Orca task id to plan node id, from the plan ledger."""
    path = root / ".coding-agent" / "plan.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {task_id: node for node, task_id in data.items() if isinstance(node, str) and isinstance(task_id, str)} if isinstance(data, dict) else {}


def report_lines(reports: list[dict[str, Any]], plan_ids: dict[str, str]) -> list[str]:
    """Lines for the unacknowledged worker reports, with the next step. Empty when there are none."""
    if not reports:
        return []
    lines = [f"worker vừa báo, chưa xử lý ({len(reports)}):"]
    for report in reports[:10]:
        task_id = str(report.get("task_id") or "?")
        node = plan_ids.get(task_id)
        name = f"node {node} ({task_id})" if node else task_id
        lines.append(f"  - {name}: {report.get('outcome') or report.get('type')} — {report.get('subject', '')[:80]}")
        if str(report.get("subject", "")).startswith(REJECTED_PREFIX):
            lines.append(f"    worker_done bị Orca từ chối (thiếu token); kiểm kết quả rồi `worker-settle --task-id {task_id} --basis verifier`")
    lines.append("  việc cần làm: kiểm kết quả của worker, gộp nhánh nếu đạt, chạy `plan-next` để giao node kế, rồi `inbox --ack`.")
    return lines


def dirty_paths(root: Path) -> list[str] | None:
    """Paths git reports as changed or untracked in `root`, or None outside git. Ignored files are not listed."""
    out = _git_raw(root, "status", "--porcelain")
    if out is None:
        return None
    return sorted({line[3:].split(" -> ")[-1].strip('"') for line in out.splitlines() if len(line) > 3})


def tree_fingerprint(root: Path) -> str | None:
    """A value that differs whenever the work tree or the checked-out commit of `root` differs, or None outside git.

    It holds the commit `HEAD` names and, for every path git reports as changed or untracked, its status code,
    size, and modification time. The set of dirty paths alone misses a change that was committed during the
    turn and a second edit of a file that was already dirty. The harness's own `.coding-agent` state is left out.
    """
    status = _git_raw(root, "-c", "core.quotePath=false", "status", "--porcelain", "-uall")
    if status is None:
        return None
    parts = [(_git_raw(root, "rev-parse", "HEAD") or "").strip()]
    for line in sorted(status.splitlines()):
        if len(line) <= 3:
            continue
        name = line[3:].split(" -> ")[-1].strip('"')
        if name.startswith(".coding-agent"):
            continue
        try:
            stat = (root / name).stat()
            parts.append(f"{line[:2]}|{name}|{stat.st_size}|{stat.st_mtime_ns}")
        except OSError:
            parts.append(f"{line[:2]}|{name}|gone")
    return "\n".join(parts)


def _git_raw(cwd: Path, *args: str) -> str | None:
    try:
        run = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 else None
