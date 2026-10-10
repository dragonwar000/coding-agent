"""Remove a worker's worktree after its task settled.

A worktree is a candidate when coding-agent started a worker in it (the workers ledger) and its Orca task is
`completed` or `failed`. `clean` releases the worker's terminal and removes the worktree through Orca, which also
deletes its branch.

A worktree is safe to remove when it holds no work that exists nowhere else: no uncommitted files, and no commits
that the coordinator's current branch does not contain. For a worker of another repository than the root (a child
repository of a folder project), that branch is the one the worker's own repository has checked out. Safe
worktrees, and records whose directory is already gone, are removed without asking anyone: `auto_clean` runs on the
coordinator's board and after the CLI commands the coordinator runs each turn (`CODING_AGENT_AUTO_CLEAN=off` turns
that off). A worker that reported but whose branch the coordinator has not merged yet keeps its worktree, so the
coordinator can still check it; once the branch is merged into the coordinator's HEAD, the next run removes it.

Only `discard=True` removes a worktree that is not safe, and that loses the work. The user confirms that: the
coordinator asks in the conversation, the coordinator-guard hook answers `ask` for `worktree-clean --discard`
(except when the session's `permission_mode` is `bypassPermissions`), and the CLI refuses `--discard` without `--yes`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import events, orca_cli

SETTLED = ("completed", "failed")
AUTO_ENV = "CODING_AGENT_AUTO_CLEAN"
WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class Candidate:
    task_id: str
    dispatch: str
    worktree: str
    status: str
    title: str
    exists: bool
    dirty: int
    unmerged: int

    @property
    def safe(self) -> bool:
        """True when removing the worktree loses no work."""
        return self.dirty == 0 and self.unmerged == 0

    def describe(self) -> str:
        if not self.exists:
            return "thư mục không còn"
        if self.safe:
            return "sạch, đã gộp"
        parts = []
        if self.unmerged == UNKNOWN or self.dirty == UNKNOWN:
            parts.append("không kiểm được bằng git")
        if self.unmerged > 0:
            parts.append(f"{self.unmerged} commit chưa gộp")
        if self.dirty > 0:
            parts.append(f"{self.dirty} file chưa commit")
        return ", ".join(parts)


def _git(cwd: Path, *args: str) -> str | None:
    try:
        run = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 else None


UNKNOWN = -1


# Files the harness itself writes into a worktree (hooks and CLI runtime state), relative to the worktree root. They
# are not the worker's work, so they never make a worktree dirty. A trailing `/` marks a directory. `.coding-agent/`
# as a whole is not listed: `.coding-agent/plan.yaml` is written by the coordinator and is real work.
HARNESS_RUNTIME = (
    ".coding-agent/events.jsonl",    # events.LOG_FILE
    ".coding-agent/state/",          # state.py: per-session state, permission-mode
    ".coding-agent/orca-run",        # orca_cli.RUN_FILE
    ".coding-agent/links.jsonl",     # orca_cli / orca.py link log
    ".coding-agent/workers.jsonl",   # orca_cli.FOLDER_LEDGER
    ".coding-agent/plan.json",       # plan.LEDGER: plan id -> Orca task id
    ".coding-agent/installed.json",  # gate.INSTALLED / project_install record
    ".coding-agent/backups/",        # project_install package backups
)


def _harness_owned(path: str) -> bool:
    return any(path.startswith(item) if item.endswith("/") else path == item for item in HARNESS_RUNTIME)


def _status_args(windows: bool) -> list[str]:
    """`git status` arguments for `_dirty`.

    On Windows, mode-only changes are ignored (`core.fileMode=false`): a repo cloned by WSL/Linux git keeps
    `core.fileMode=true` and files stored as 100755, NTFS has no executable bit, so Git for Windows reads them as
    100644 and reports every such file modified with no content change. On Linux/macOS a `chmod +x` that is not
    committed is real work, so the repo's setting stands. `-uall` lists untracked files one by one (not a collapsed
    `.coding-agent/` directory) so harness runtime files can be told apart; `-z` keeps names with spaces exact.
    """
    return [*(["-c", "core.fileMode=false"] if windows else []), "status", "--porcelain", "-z", "--untracked-files=all"]


def _dirty(worktree: Path) -> int:
    """Changed or untracked files in the worktree, or UNKNOWN when git cannot say.

    Not counted: mode-only changes on Windows (see `_status_args`) and the harness's own runtime files
    (`HARNESS_RUNTIME`), which hooks write into every worker's worktree whether or not the worker did anything.
    """
    out = _git(worktree, *_status_args(WINDOWS))
    if out is None:
        return UNKNOWN
    fields = out.split("\0")
    count = 0
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            index += 1  # the next field is the rename/copy source
        if not _harness_owned(entry[3:]):
            count += 1
    return count


def _unmerged(root: Path, worktree: Path, base: str) -> int:
    """Commits the worker added that the coordinator's HEAD does not contain, or UNKNOWN when git cannot say.

    The worker's commits are those after `base`, the commit the worktree started from. Without a recorded base,
    every commit the coordinator lacks counts, which overstates when the two started from different branches.
    """
    head = (_git(worktree, "rev-parse", "HEAD") or "").strip()
    if not head:
        return UNKNOWN
    out = _git(root, "rev-list", "--count", head, "--not", "HEAD", *([base] if base else []))
    return int(out.strip()) if out and out.strip().isdigit() else UNKNOWN


def candidates(root: Path, run: str | None = None) -> list[Candidate]:
    """Worker worktrees whose task settled, oldest first, with what removing each one would lose."""
    tasks = {str(task.get("id")): task for task in orca_cli.list_tasks(run)}
    found: list[Candidate] = []
    for record in orca_cli.worker_records(root):
        task = tasks.get(record["task_id"])
        if task is None or task.get("status") not in SETTLED:
            continue
        path = Path(record["worktree"])
        exists = path.is_dir()
        found.append(Candidate(
            task_id=record["task_id"],
            dispatch=record["dispatch"],
            worktree=record["worktree"],
            status=str(task.get("status")),
            title=str(task.get("task_title") or task.get("title") or ""),
            exists=exists,
            dirty=_dirty(path) if exists else 0,
            unmerged=_unmerged(orca_cli.repo_of(root, record), path, record.get("base", "")) if exists else 0,
        ))
    return found


def clean(root: Path, chosen: list[Candidate], *, discard: bool = False, session: str = "cli",
          kind: str = "worktree-removed") -> list[tuple[Candidate, str]]:
    """Remove each chosen worktree. Returns (candidate, outcome) with outcome `removed`, `kept: <why>`, or `failed: <why>`."""
    results: list[tuple[Candidate, str]] = []
    for item in chosen:
        if item.exists and not item.safe and not discard:
            results.append((item, f"kept: {item.describe()}"))
            continue
        try:
            if item.dispatch and item.dispatch != "adopted":
                try:
                    orca_cli.release_worker(item.dispatch)
                except orca_cli.OrcaError:
                    pass  # already released, or Orca no longer tracks it; the worktree removal below is what matters
            if item.exists:
                orca_cli.remove_worktree(item.worktree, force=discard and not item.safe)
        except orca_cli.OrcaError as error:
            results.append((item, f"failed: {str(error)[:200]}"))
            continue
        orca_cli.mark_removed(root, item.worktree)
        events.record(root, guard="coordinator", kind=kind, mode="enforce", applied=True, session=session,
                      detail={"task_id": item.task_id, "worktree": item.worktree, "discarded": discard and not item.safe})
        results.append((item, "removed"))
    return results


def auto_enabled() -> bool:
    """False when `CODING_AGENT_AUTO_CLEAN` is `off` (or `0`, `false`, `no`); on by default."""
    return os.environ.get(AUTO_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def auto_clean(root: Path, run: str | None = None, *, session: str = "cli",
               found: list[Candidate] | None = None) -> list[tuple[Candidate, str]]:
    """Remove every settled worker worktree that loses no work, without asking. Returns `clean`'s (candidate, outcome).

    Removed: safe candidates, and records whose directory no longer exists (only marked removed). Never touched:
    worktrees with unmerged commits, uncommitted files, or a state git cannot report. Never forced. An Orca error on
    one worktree is reported as its `failed:` outcome and does not stop the others; an error listing the tasks raises
    `orca_cli.OrcaError`. `found` reuses candidates the caller already listed.

    Limits: only tasks Orca lists for `run` are seen. With `run` None that is the Run the CLI is bound to
    (`CODING_AGENT_ORCA_RUN`, or the stored or bound Run); `orca orchestration task-list` has no option to list
    every Run, so a worker worktree whose task belongs to an older Run is never a candidate here and stays until
    it is removed by hand. A worktree recorded by `worker-adopt` without a real task id has no task and is not seen either.
    """
    if not auto_enabled():
        return []
    if found is None:
        if not orca_cli.worker_records(root):
            return []
        found = candidates(root, run)
    chosen = [item for item in found if item.safe or not item.exists]
    return clean(root, chosen, session=session, kind="worktree-auto-removed") if chosen else []


def board_lines(found: list[Candidate]) -> list[str]:
    """Lines for the coordinator's board. Empty when no worktree is left.

    Merged, clean worktrees are removed by `auto_clean` before the board is built, so what is listed here normally
    holds work that is not merged or not committed.
    """
    if not found:
        return []
    lines = [f"worktree của worker đã xong, còn việc chưa gộp hoặc chưa commit ({len(found)}):"]
    for item in found[:10]:
        lines.append(f"  - {Path(item.worktree).name} ({item.task_id}, {item.status}): {item.describe()}")
    lines.append("  việc cần làm: worktree đã gộp và sạch tự được xoá, không cần hỏi ai. Với các worktree trên: kiểm rồi gộp nhánh vào HEAD (lượt sau nó tự được dọn); "
                 "nếu muốn bỏ việc đó thì hỏi người dùng trước, rồi chạy `worktree-clean --task-id <id> --discard --yes` (mất code chưa gộp). "
                 "Worktree ghi 'sạch, đã gộp' chỉ còn khi tự dọn bị tắt (CODING_AGENT_AUTO_CLEAN=off): xoá bằng `worktree-clean --task-id <id>`, không cần --yes.")
    return lines
