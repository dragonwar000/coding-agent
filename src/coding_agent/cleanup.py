"""Remove a worker's worktree after its task settled, once the user has confirmed.

A worktree is a candidate when coding-agent started a worker in it (the workers ledger), its Orca task is
`completed` or `failed`, and the directory still exists. `clean` releases the worker's terminal and removes the
worktree through Orca, which also deletes its branch.

Nothing is removed while the worktree holds work that exists nowhere else: uncommitted files, or commits that
the coordinator's current branch does not contain. For a worker of another repository than the root (a child
repository of a folder project), that branch is the one the worker's own repository has checked out. `discard=True` overrides that and loses the work.

The confirmation is not taken here. The coordinator asks the user in the conversation first, and the
coordinator-guard hook answers `ask` for the `worktree-clean` command so the host asks again before it runs,
except when the session's `permission_mode` is `bypassPermissions`: then the hook stays silent, because the user
turned prompts off. The CLI always refuses to run without `--yes`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import events, orca_cli

SETTLED = ("completed", "failed")


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


def _dirty(worktree: Path) -> int:
    """Changed or untracked files in the worktree, or UNKNOWN when git cannot say."""
    out = _git(worktree, "status", "--porcelain")
    return UNKNOWN if out is None else len([line for line in out.splitlines() if line.strip()])


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


def clean(root: Path, chosen: list[Candidate], *, discard: bool = False, session: str = "cli") -> list[tuple[Candidate, str]]:
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
        events.record(root, guard="coordinator", kind="worktree-removed", mode="enforce", applied=True, session=session,
                      detail={"task_id": item.task_id, "worktree": item.worktree, "discarded": discard and not item.safe})
        results.append((item, "removed"))
    return results


def board_lines(found: list[Candidate]) -> list[str]:
    """Lines for the coordinator's board. Empty when no worktree waits for removal."""
    if not found:
        return []
    lines = [f"worktree của worker đã xong, chờ dọn ({len(found)}):"]
    for item in found[:10]:
        lines.append(f"  - {Path(item.worktree).name} ({item.task_id}, {item.status}): {item.describe()}")
    lines.append("  việc cần làm: gộp phần chưa gộp nếu cần giữ, hỏi người dùng có xoá không, rồi chạy `worktree-clean --task-id <id> --yes`. Host hỏi xác nhận lệnh đó trừ khi phiên đang bypass permissions; vẫn phải hỏi người dùng trong hội thoại trước khi chạy với `--yes`.")
    return lines
