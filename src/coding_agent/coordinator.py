"""The coordinator role: the main session plans, delegates to Orca workers, and stays free to answer the user.

Role is decided per session. The main worktree of the repository is the coordinator; a linked worktree
(where `worker-start --worktree new-child` puts a worker) is a worker. `CODING_AGENT_ROLE=coordinator|worker|maintainer`
overrides the detection; a maintainer session is not blocked, and what the guard would have blocked is still logged. A worker never gets the coordinator contract, and the coordinator guard does
nothing in a worker.

The guard blocks direct file writes and shell commands that change files in the coordinator, so the work goes
to a worker. git commands are the coordinator's to run; `CODING_AGENT_COORDINATOR_GIT=safe` blocks the destructive forms. The contract and the task board are injected at session start and on every prompt, so the
coordinator keeps the user's question in view while workers run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent import orca, orca_cli, roots as root_discovery

ROLE_ENV = "CODING_AGENT_ROLE"
ROLES = ("coordinator", "worker", "maintainer")
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
BOARD_LIMIT = 20
QUESTION_CHARS = 200
# A dispatched task whose worker sent nothing (heartbeat, report, question) for longer than this is flagged on the board.
SILENT_MINUTES = 20

# Shell forms that change file content. Read-only forms (ls, cat, grep) and the coding-agent CLI are not matched.
# git commands are not matched here: the coordinator runs any git command (in `safe` mode, all but the destructive forms below).
# A heuristic: what it misses is recorded by stop-gate as `direct-change`.
MUTATING_BASH = re.compile(
    r"(\bsed\s+-i\b|\brm\s|\bmv\s|\bcp\s|\bmkdir\b|\btouch\b|\btee\b|\bchmod\b|\bln\s|"
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


# One git invocation: `git` as a word of its own, up to the next separator or redirect.
GIT_CALL = re.compile(r"(?<![^\s;&|(`'\"])git(?=\s|$)([^;&|\n()`>]*)")
# `CODING_AGENT_COORDINATOR_GIT=safe` blocks the destructive git forms. Unset or `all` (the default), every git command runs.
COORDINATOR_GIT_ENV = "CODING_AGENT_COORDINATOR_GIT"
# Global options of git that take a value as the next word.
GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _short_flags(args: list[str], letter: str) -> bool:
    """True when a short-flag cluster in `args` (`-f`, `-fd`, `-xdf`) contains `letter`."""
    return any(re.fullmatch(r"-[A-Za-z]+", arg) and letter in arg for arg in args)


def _destructive_form(sub: str, args: list[str]) -> str | None:
    """The destructive git form `git <sub> <args>` is, or None when it loses nothing that cannot be recovered."""
    if sub == "push":
        if any(arg in ("--force", "--force-with-lease") or arg.startswith("--force-with-lease=") for arg in args):
            return "git push --force"
        if _short_flags(args, "f"):
            return "git push -f"
        if any(arg.startswith("+") for arg in args):
            return "git push +refspec"
    elif sub == "reset" and "--hard" in args:
        return "git reset --hard"
    elif sub == "clean" and ("--force" in args or _short_flags(args, "f")):
        return "git clean -f"
    elif sub == "checkout" and ("--" in args or "." in args):
        return "git checkout -- <path>"
    elif sub == "restore" and ("--staged" not in args and not _short_flags(args, "S")
                               or "--worktree" in args or _short_flags(args, "W")):
        return "git restore (bỏ thay đổi trong working tree)"
    elif sub == "stash" and args[:1] in (["drop"], ["clear"]):
        return f"git stash {args[0]}"
    elif sub == "branch" and (_short_flags(args, "D") or ("--delete" in args or _short_flags(args, "d"))
                              and ("--force" in args or _short_flags(args, "f"))):
        return "git branch -D"
    return None


def destructive_git(shell: str) -> str | None:
    """The first git form in `shell` that destroys data beyond recovery, or None.

    Force pushes, `reset --hard`, `clean -f`, discarding working-tree changes (`checkout -- <path>`, `checkout .`,
    `restore` without `--staged`), `stash drop/clear`, and `branch -D`. Every other git command is allowed.
    """
    for match in GIT_CALL.finditer(shell):
        words = [word.strip("'\"") for word in match.group(1).split()]
        words = [word for word in words if word]
        i = 0
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in GIT_OPTIONS_WITH_VALUE else 1
        if i < len(words):
            form = _destructive_form(words[i], words[i + 1:])
            if form:
                return form
    return None


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
# `worktree-clean --discard` deletes a worker's worktree and branch with work that is merged nowhere, so the host asks
# the user before this command runs (unless the session bypasses permissions). Removing a merged, clean worktree loses
# nothing and is not asked.
CLEAN_CALL = re.compile(r"coding_agent\.cli\s+(--root\s+\S+\s+)?worktree-clean\b[^\n]*\s--discard\b")


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
        return "coding-agent sắp xoá worktree và nhánh của worker, kể cả việc chưa gộp (mất code). Lệnh: " + command.strip()[:300]
    return None


CHAINING = re.compile(r"(&&|\|\||;|\||`|\$\()")


def guard_reason(tool: str, tool_input: Any, root: Path | None = None, *, python_src: str | None = None) -> str | None:
    """Why the coordinator must not do this call itself, or None when the call is allowed.

    Allowed: read-only shell, a single `coding_agent.cli` command, and writing the plan file.
    Refused: any other file write, a shell command that changes files, a destructive git form (only with
    `CODING_AGENT_COORDINATOR_GIT=safe`), and an in-process subagent, which would bypass the graph.
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
        if os.environ.get(COORDINATOR_GIT_ENV) == "safe":
            form = destructive_git(shell)
            if form:
                return (
                    f"[coordinator] `{form}` là dạng git phá huỷ: làm mất dữ liệu không lấy lại được, nên bị chặn "
                    f"vì phiên đặt {COORDINATOR_GIT_ENV}=safe. Muốn cho coordinator chạy cả dạng này: khởi động phiên "
                    f"không đặt biến đó (hoặc {COORDINATOR_GIT_ENV}=all). {how}"
                )
        if MUTATING_BASH.search(GIT_CALL.sub("git", shell)):
            return f"[coordinator] lệnh shell này làm thay đổi file. {how}"
    return None


AUTO_DECIDE_LINE = (
    "- Auto-decide đang bật: khi cần người dùng quyết (kể cả câu hỏi worker leo thang), hỏi bằng tool AskUserQuestion, "
    "không hỏi bằng văn bản rồi kết thúc lượt. Đặt phương án bạn gợi ý lên đầu, nhãn kết thúc bằng \"(Recommended)\". "
    "Người dùng không trả lời trong thời gian chờ thì hệ thống tự chọn và báo lại; hãy nói lại lựa chọn đó cho người dùng "
    "rồi làm tiếp (với câu hỏi của worker: trả lời worker theo lựa chọn đó)."
)


def contract(python_src: str, auto_decide: bool = False) -> str:
    """The coordinator contract injected at session start and after a compaction."""
    lines = [
        "[coordinator] Bạn là coordinator của repo này. Vai trò: nhận yêu cầu, lập kế hoạch, giao việc, và trả lời người dùng.",
        "- Mọi thay đổi file đi qua graph: bạn viết plan, worker Orca làm từng node trong worktree riêng. Ở mode enforce, hook chặn sửa trực tiếp, lệnh shell ghi file, và subagent nội bộ.",
        "- Bạn vẫn đọc file, chạy lệnh đọc, ghi đúng một file (.coding-agent/plan.yaml), và được chạy mọi lệnh git (gộp nhánh worker, push, pull, checkout, rebase, stash, tag...). Phiên đặt `CODING_AGENT_COORDINATOR_GIT=safe` thì chặn các dạng phá huỷ: `git push --force`/`-f`/`+refspec`, `git reset --hard`, `git clean -f`, `git checkout -- <path>`/`git checkout .`, `git restore` không có `--staged`, `git stash drop/clear`, `git branch -D`.",
        "- Worker tách nhánh từ commit hiện tại: commit việc đang dở trước khi `plan-next`, nếu không worker sẽ không thấy nó.",
        "- Chỉ worker được ghi: mọi lần ghi file, script tạm, hay lệnh shell đổi trạng thái là việc của worker. Bạn không tự làm, và không viết lại lệnh bị chặn để lách guard.",
        "- Khi việc cần ghi, hoặc khi hook chặn một lệnh: giao ngay trong cùng lượt, bằng lệnh delegate (một việc nhỏ) hoặc graph (nhiều việc). Không xin phép người dùng, không kết thúc lượt để chờ. Chỉ hỏi người dùng khi quyết định thật sự là của họ (phạm vi, thao tác phá huỷ hoặc ra bên ngoài), không hỏi \"có giao việc không\".",
        "- Chưa có Orca Run: tự chạy `run-init --objective \"...\"` với mục tiêu một dòng, rồi giao việc. Không nhờ người dùng chạy.",
        f"- Giao việc bằng: {delegate_command(python_src)} [--agent claude|codex]",
        "- Folder project (root không phải repo git, chứa nhiều repo con): mọi lệnh delegate phải có `--repo <đường dẫn repo>`, và mỗi node trong plan đặt `repo:`. Worker tách nhánh từ HEAD của repo đó; đổi bằng `--base-branch` / `base:`.",
        "- " + HOW + " Chỉ node đã xong hết phụ thuộc mới được giao.",
        "- `title` của mỗi node là tóm tắt việc cần làm, ngắn và súc tích, khoảng 3 đến 6 từ, không tiền tố chung. Nó thành tên worktree, tên nhánh (cắt ở 40 ký tự, bỏ dấu) và nhãn của worker trong Orca. Chi tiết để trong `spec`.",
        "- Khi bảng việc ghi 'worker vừa báo': kiểm kết quả, gộp nhánh nếu đạt, rồi `inbox --ack --task-id <id>` cho đúng task vừa gộp (không ack cả hộp thư: báo cáo chưa xem sẽ mất khỏi bảng), rồi `plan-next`.",
        "- Task giao bằng `delegate`/`plan-next` với `--root <repo khác>` vẫn hiện trên bảng (kèm tên repo) và được tự dọn như task của repo này: repo trong cây thư mục của bạn được tự tìm (có .coding-agent/links.jsonl), repo ngoài cây được ghi vào .coding-agent/roots.json. Task giao trước khi có roots.json được tự tìm lại repo từ Orca; nếu bảng vẫn thiếu, chạy `roots-add <path>`, xem bằng `roots-list`.",
        "- " + RECOVER,
        "- Worker dừng vì lỗi API tạm thời (mất mạng, quá tải, rate limit) được tự resume khi dựng bảng việc, sau khi kiểm thấy mạng đã có lại; bảng việc ghi rõ worker nào đã resume, đang chờ mạng, hay bị chặn cần người xử lý. Khi còn worker đang chạy mà bạn không có lượt mới, chạy nền `worker-resume --watch` để chúng được resume ngay khi mạng trở lại.",
        "- Worker im lặng (task còn dispatched, terminal đứng ở ô nhập trống quá 10 phút, chưa có worker_done trong hộp thư) được tự nhắc tối đa 2 lần: việc xong thì commit và gửi worker_done, chưa xong thì làm tiếp. Bảng việc tách riêng nhóm 'worker im lặng, chưa báo' kèm số lần đã nhắc; nhắc đủ 2 lần mà vẫn im thì đọc màn hình terminal và diff của worktree rồi xử lý (gộp và `worker-settle` nếu đạt).",
        "- Worktree của worker đã xong, đã gộp và sạch thì tự được xoá (khi dựng bảng việc và sau `plan-next`, `plan-status`, `inbox`, `delegate`, `worktree-list`), không cần hỏi người dùng. Bảng việc chỉ còn liệt kê worktree có việc chưa gộp hoặc chưa commit: kiểm rồi gộp nhánh vào HEAD, hoặc vào nhánh base của task khi worker được tạo từ nhánh đó (`base:`/`--base-branch`), lượt sau nó tự được dọn. Muốn bỏ việc đó thì hỏi người dùng trong hội thoại trước, rồi chạy `worktree-clean --task-id <id> --discard --yes` (mất code chưa gộp; host cũng hỏi xác nhận, trừ khi phiên đang bypass permissions).",
        "- Khi bảng việc ghi 'worker đang hỏi, chờ trả lời': worker đó đang đứng chờ, không chạy. Trả lời ngay bằng lệnh in trên bảng (`orca orchestration reply --id <msg> --run <run> --body \"...\"`). Câu hỏi cần người dùng quyết (phạm vi, xoá/ghi đè, commit/push, chọn hướng) thì hỏi người dùng trong hội thoại trước rồi mới trả lời worker. Dòng task có '(không có tin X phút)' là worker im lặng lâu: đọc terminal của nó trước khi coi là đang chạy.",
        "- Task `completed` do worker tự báo là chưa có bằng chứng: kiểm kết quả (đọc diff của worktree, chạy verify) trước khi báo người dùng là xong.",
        "- Worker chạy trong worktree riêng và ghi kết quả vào Orca. Sau khi giao, trả lời người dùng ngay; không chờ worker.",
        "- Mỗi lượt, đọc bảng việc bên dưới trước khi nói về tiến độ. Không bịa trạng thái task.",
    ]
    if auto_decide:
        lines.append(AUTO_DECIDE_LINE)
    return "\n".join(lines)


def _minutes_since(when: Any, now: datetime) -> int | None:
    try:
        then = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError:
        return None
    then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
    return int((now - then).total_seconds() // 60)


def question_lines(questions: list[dict[str, Any]], owners: dict[str, str] | None = None, home: str | None = None,
                   run: str | None = None) -> list[str]:
    """Lines for the worker questions nobody answered, each with the command that answers it. Empty when there are none."""
    if not questions:
        return []
    lines = [f"worker đang hỏi, chờ trả lời ({len(questions)}):"]
    for question in questions:
        task = str(question.get("task_id") or "?")
        repo = (owners or {}).get(task)
        where = f" (repo {repo})" if repo and repo != home else ""
        text = " ".join(str(question.get("question") or "").split())
        text = text if len(text) <= QUESTION_CHARS else text[:QUESTION_CHARS] + "…"
        kind = "" if question.get("type") in (None, "question") else f" [{question['type']}]"
        lines.append(f"  - {task}{where}{kind}: {text}")
        if question.get("options"):
            lines.append("    lựa chọn: " + " | ".join(question["options"]))
        target = question.get("run") or run
        lines.append(f"    msg {question.get('id')}; trả lời: `orca orchestration reply --id {question.get('id')}"
                     + (f" --run {target}" if target else "") + ' --body "..."`')
    return lines


def board_lines(tasks: list[dict[str, Any]], owners: dict[str, str], project: str, others: frozenset[str] | set[str] = frozenset(),
                questions: list[dict[str, Any]] | None = None, heard: dict[str, str] | None = None,
                now: datetime | None = None, run: str | None = None, stalled: dict[str, str] | None = None,
                silent: dict[str, str] | None = None) -> list[str]:
    """The task board for this repository, one line per task, grouped by state.

    `others` are the repositories this coordinator delegated into; their tasks are listed too, each with its repository.
    `questions` (`orca_cli.open_questions`) come first: a task waiting on an answer is not listed as running. `heard`
    maps a task to when its worker last sent anything; a running task silent for over `SILENT_MINUTES` says so.
    `stalled` maps a task whose worker stopped on an API error to what was done about it (`resume.board_notes`); those
    tasks are listed next, in a group of their own, and not as running. `silent` does the same for a worker that went
    quiet without reporting (`resume.silent_notes`), in the group after it.
    """
    shown = {project, *others}
    stalled = stalled or {}
    silent = {task: note for task, note in (silent or {}).items() if task not in stalled}
    mine = [{**task, "project": owners[str(task.get("id"))]} for task in tasks if owners.get(str(task.get("id"))) in shown]
    ids = {str(task.get("id")) for task in mine}
    known = {str(task.get("id")) for task in tasks}
    # A question of another coordinator's task stays off this board; one of a task the Run does not list is shown.
    asking = [question for question in questions or []
              if str(question.get("task_id")) in ids or str(question.get("task_id")) not in known]
    waiting = {str(question.get("task_id")) for question in asking}
    lines: list[str] = question_lines(asking, owners, project, run)
    if not mine:
        return lines + ["bảng việc: chưa có task nào của repo này"]

    def row(task: dict[str, Any]) -> str:
        title = task.get("task_title") or task.get("title") or ""
        repo = f" (repo {task['project']})" if task.get("project") != project else ""
        return f"  - {task.get('id')} [{task.get('status')}] {title}".rstrip() + repo

    halted = [task for task in mine if str(task.get("id")) in stalled]
    quiet = [task for task in mine if str(task.get("id")) in silent]
    groups = orca.reconcile([task for task in mine if str(task.get("id")) not in stalled and str(task.get("id")) not in silent])
    groups["running"] = [task for task in groups["running"] if str(task.get("id")) not in waiting]
    now = now or datetime.now(timezone.utc)
    labels = {
        "running": "đang chạy",
        "never_dispatched": "chưa giao worker",
        "stuck": "bị chặn",
        "unverified": "báo xong nhưng chưa có bằng chứng",
        "other": "khác",
    }
    if halted:
        lines.append(f"worker bị dừng vì lỗi API/mạng, không còn chạy ({len(halted)}):")
        lines += [f"{row(task)}: {stalled[str(task.get('id'))]}" for task in halted]
        lines.append("  việc cần làm: worker 'đã tự resume' hoặc 'đang chờ mạng' thì không cần làm gì (chạy nền `worker-resume --watch` để resume ngay khi mạng có lại); "
                     "worker 'bị chặn' thì đọc màn hình terminal của nó và báo người dùng.")
    if quiet:
        lines.append(f"worker im lặng, chưa báo ({len(quiet)}):")
        lines += [f"{row(task)}: {silent[str(task.get('id'))]}" for task in quiet]
        lines.append("  việc cần làm: worker vừa được nhắc hoặc chờ nhắc lại thì không cần làm gì; worker 'cần người xử lý' thì đọc màn hình "
                     "terminal và diff của worktree: việc đã xong thì kiểm, gộp rồi `worker-settle`, chưa xong thì báo người dùng.")
    for key, label in labels.items():
        items = groups.get(key) or []
        if not items:
            continue
        lines.append(f"{label} ({len(items)}):")
        for task in items:
            quiet_for = ""
            if key == "running" and (heard or {}).get(str(task.get("id"))):
                minutes = _minutes_since(heard[str(task.get("id"))], now)
                quiet_for = f" (không có tin {minutes} phút)" if minutes is not None and minutes > SILENT_MINUTES else ""
            lines.append(row(task) + quiet_for)
    return lines[:BOARD_LIMIT] + (["  ..."] if len(lines) > BOARD_LIMIT else [])


# The line Orca types into an idle agent terminal when its mailbox gets mail (Orca 1.4: `\nYou have N orchestration
# message(s). Run `orca orchestration check --run <id>`.\n`, then Enter 500 ms later). Orca does not look at the
# Claude Code input box first, so text the user is typing at that moment is sent together with it, unfinished.
ORCA_POINTER = re.compile(r"You have \d+ orchestration messages?\. Run `[^`\n]*orchestration check[^`\n]*`\.?")


def split_orca_pointer(prompt: str) -> str | None:
    """The user's own text when Orca's mailbox line was typed into the middle of it, else None.

    None also when the prompt is only Orca's line (an ordinary mailbox wake-up) or has no Orca line.
    """
    if not ORCA_POINTER.search(prompt):
        return None
    rest = "\n".join(line.strip() for line in ORCA_POINTER.sub("\n", prompt).splitlines() if line.strip())
    return rest or None


def collision_context(user_text: str) -> str:
    """What the coordinator is told when a prompt holds both the user's text and Orca's mailbox line."""
    return (
        "[coordinator] Prompt này bị dính: Orca tự gõ dòng \"You have N orchestration message(s). Run `orca orchestration check`\" "
        "vào ô nhập đúng lúc người dùng đang gõ, rồi tự bấm Enter. Hai phần là hai tin khác nhau:\n"
        "- Dòng của Orca: chỉ là thông báo hộp thư có tin mới (worker báo/hỏi). Xử lý theo bảng việc bên dưới như mọi lần.\n"
        f"- Phần của người dùng (có thể đang gõ dở, chưa xong): «{user_text}».\n"
        "Nói ngắn cho người dùng biết tin của họ bị gửi sớm do trùng thông báo của Orca. Nếu phần đó đã đủ ý thì làm theo; "
        "nếu có vẻ dở dang thì đừng đoán, nhờ người dùng gõ nốt."
    )


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


def read_board(root: Path, run: str | None = None, session: str = "board") -> tuple[list[str], str | None]:
    """The board lines for this repository, or the reason Orca could not be read.

    The Run read is, in order: `run`, `CODING_AGENT_ORCA_RUN` set by the user, the Run Orca has bound to this terminal
    (`run-current`, or the one a `consumer_fenced` answer names), then the Run stored in `.coding-agent/orca-run`.
    Several coordinator terminals share that file, so the terminal's Run is used for this process only and the file is
    left as it is; when they differ the first line says which Run the board follows (`orca_cli.adopt_run`). Tasks,
    reports and worker worktrees are read for `root` and every repository it delegated into (`orca_cli.all_roots`); a
    task no known repository owns makes `roots.discover` look its repository up from Orca and register it first.
    Worker questions nobody answered come first (`orca_cli.worker_activity`). Workers that stopped on a transient API
    error are resumed (`resume.auto`), and listed in a group of their own; so are workers that went silent without
    reporting, which `resume.auto` reminds.
    Settled worker worktrees that are merged and clean are removed first (`cleanup.auto_clean`), so the board lists
    only the ones that still hold work; that group does not depend on the inbox being readable.
    """
    orca_cli.use_stored_run(root)
    notes: list[str] = []
    if run is None:
        note = orca_cli.sync_bound_run(root, session=session)
        notes += [f"bảng {note}"] if note else []
    lines, error, fenced = _board(root, run, session)
    if fenced and run is None and fenced != orca_cli.run_id(None):
        note = orca_cli.adopt_run(root, fenced, session=session)
        notes = [f"bảng {note}"] if note else []
        lines, error, fenced = _board(root, run, session)
    return notes + lines, error


def _board(root: Path, run: str | None, session: str) -> tuple[list[str], str | None, str | None]:
    """`read_board` for the current Run: (lines, error, the Run Orca says this terminal is bound to when it fenced a call)."""
    try:
        tasks = orca_cli.list_tasks(run)
    except orca_cli.OrcaError as error:
        return [], str(error)[:300], orca_cli.fenced_run(error)
    root_discovery.discover(root, [str(task.get("id") or "") for task in tasks], run, session)
    roots = orca_cli.all_roots(root)
    owners = repo_owners(roots)
    home = orca.project_name(root)
    try:
        questions, heard = orca_cli.worker_activity(run)
        activity_error = None
    except orca_cli.OrcaError as error:
        if orca_cli.fenced_run(error):
            return [], None, orca_cli.fenced_run(error)
        questions, heard, activity_error = [], {}, str(error)[:160]
    from coding_agent import resume

    found = resume.auto(root, tasks, run, session)
    lines = board_lines(tasks, owners, home, {orca.project_name(other) for other in roots[1:]}, questions, heard,
                        run=orca_cli.run_id(run), stalled=resume.board_notes(found), silent=resume.silent_notes(found))
    if activity_error:
        lines.append(f"câu hỏi của worker không đọc được: {activity_error}")
    try:
        reports = orca_cli.drop_acked(root, orca_cli.unread_reports(run))
    except orca_cli.OrcaError as error:
        if orca_cli.fenced_run(error):
            return lines, None, orca_cli.fenced_run(error)
        lines.append(f"hộp thư worker không đọc được: {str(error)[:160]}")
    else:
        lines += report_lines(reports, plan_ids(roots), owners, home)
    try:
        from coding_agent import cleanup

        found = cleanup.candidates(root, run)
        removed = {item.worktree for item, outcome in cleanup.auto_clean(root, run, session=session, found=found) if outcome == "removed"}
        lines = lines + cleanup.board_lines([item for item in found if item.worktree not in removed])
    except orca_cli.OrcaError as error:
        lines.append(f"danh sách worktree chờ dọn không đọc được: {str(error)[:160]}")
    return lines, None, None


def repo_owners(roots: list[Path]) -> dict[str, str]:
    """Orca task id to its repository, from the link ledgers of `roots`; the first root's links win."""
    owners: dict[str, str] = {}
    for root in reversed(roots):
        owners.update(orca_cli.links(root))
    return owners


def plan_ids(roots: list[Path]) -> dict[str, str]:
    """`_plan_ids` of every root; the first root's plan wins."""
    out: dict[str, str] = {}
    for root in reversed(roots):
        out.update(_plan_ids(root))
    return out


def _plan_ids(root: Path) -> dict[str, str]:
    """Orca task id to plan node id, from the plan ledger."""
    path = root / ".coding-agent" / "plan.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {task_id: node for node, task_id in data.items() if isinstance(node, str) and isinstance(task_id, str)} if isinstance(data, dict) else {}


def report_name(report: dict[str, Any], plan_ids: dict[str, str], owners: dict[str, str] | None = None, home: str | None = None,
                always_repo: bool = False) -> str:
    """How a report's task is named: `node <id> (<task>)` or `<task>`, with its repository when it is not `home`
    (or always, with `always_repo`)."""
    task_id = str(report.get("task_id") or "?")
    node = plan_ids.get(task_id)
    name = f"node {node} ({task_id})" if node else task_id
    repo = (owners or {}).get(task_id)
    if always_repo or (repo and repo != home):
        name += f" [repo {repo or '?'}]"
    return name


def report_lines(reports: list[dict[str, Any]], plan_ids: dict[str, str], owners: dict[str, str] | None = None,
                 home: str | None = None) -> list[str]:
    """Lines for the unacknowledged worker reports, with the next step. Empty when there are none."""
    if not reports:
        return []
    lines = [f"worker vừa báo, chưa xử lý ({len(reports)}):"]
    for report in reports[:10]:
        task_id = str(report.get("task_id") or "?")
        name = report_name(report, plan_ids, owners, home)
        lines.append(f"  - {name}: {report.get('outcome') or report.get('type')} — {report.get('subject', '')[:80]}")
        if str(report.get("subject", "")).startswith(REJECTED_PREFIX):
            lines.append(f"    worker_done bị Orca từ chối (thiếu token); kiểm kết quả rồi `worker-settle --task-id {task_id} --basis verifier`")
    lines.append("  việc cần làm: kiểm kết quả của worker, gộp nhánh nếu đạt, rồi `inbox --ack --task-id <id>` cho task đó, rồi `plan-next` để giao node kế.")
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
