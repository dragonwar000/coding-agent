"""Remove a worker's worktree after its task settled.

A worktree is a candidate when coding-agent started a worker in it (the workers ledger) and its Orca task is
`completed` or `failed`. `clean` releases the worker's terminal and removes the worktree through Orca, which also
deletes its branch.

A worktree is safe to remove when it holds no work that exists nowhere else: no uncommitted files, and no worker
commits that are missing from both the coordinator's current branch and the task's base branch. The base branch is
the one the worker was started from (`delegate --base-branch`, a plan node's `base:`), with its upstream
remote-tracking branch; a coordinator merges such a worker there rather than into its own branch. Other branches never
count: a branch forked from a worker's branch holds its commits without merging them. A record written before the base
branch was kept, or whose base branch was deleted, is compared with the coordinator's branch alone. For a worker of
another repository than the root (a child repository of a folder project), the coordinator's branch is the one the
worker's own repository has checked out.

Safe worktrees, and records whose directory is already gone, are removed without asking anyone: `auto_clean` runs on
the coordinator's board and after the CLI commands the coordinator runs each turn (`CODING_AGENT_AUTO_CLEAN=off`
turns that off). A worker that reported but whose branch the coordinator has not merged yet keeps its worktree, so
the coordinator can still check it; once the branch is merged into the coordinator's HEAD or the task's base branch,
the next run removes it.

A removal that stopped part-way (`Directory not empty`) can leave the directory without its git registration. A
directory holding nothing but OS junk (`.DS_Store`, `Thumbs.db`, `desktop.ini`, and directories of those) counts as
already gone and is deleted. A full checkout whose `.git` file points to a git directory that no longer exists is an
orphan: it is deleted only when its files provably match a merged commit (`_orphan_is_merged`), and otherwise stays
listed as `đăng ký worktree đã mất, cần kiểm tay`. Deleting never follows a symlink or junction out of the directory.

Only `discard=True` removes a worktree that is not safe, and that loses the work. The user confirms that: the
coordinator asks in the conversation, the coordinator-guard hook answers `ask` for `worktree-clean --discard`
(except when the session's `permission_mode` is `bypassPermissions`), and the CLI refuses `--discard` without `--yes`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import events, orca, orca_cli, roots, worktree_links

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
    root: str = ""  # the repository whose workers ledger lists the worktree; "" for the coordinator's own
    repo: str = ""  # that repository's name, when it is not the coordinator's
    orphan: bool = False  # a checkout whose git registration is gone; safe only when it matches a merged commit

    @property
    def safe(self) -> bool:
        """True when removing the worktree loses no work."""
        return self.dirty == 0 and self.unmerged == 0

    def describe(self) -> str:
        if not self.exists:
            return "thư mục không còn"
        if self.orphan:
            return "đăng ký worktree đã mất, khớp commit đã gộp" if self.safe else "đăng ký worktree đã mất, cần kiểm tay"
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
    ".coding-agent/roots.json",      # orca_cli.ROOTS_FILE: repositories the coordinator delegated into
    ".coding-agent/acked.jsonl",     # orca_cli.ACKED_FILE: reports acknowledged one task at a time
    ".coding-agent/worktree-links.json",  # worktree_links.RECORD: the install links made in a worker worktree
)


def _harness_owned(path: str, links: tuple[str, ...] = ()) -> bool:
    """True for a harness runtime file, or for a path under one of `links` (the install `worktree_links` linked in)."""
    return (any(path.startswith(item) if item.endswith("/") else path == item for item in HARNESS_RUNTIME)
            or any(path == link or path.startswith(link + "/") for link in links))


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

    Not counted: mode-only changes on Windows (see `_status_args`), the harness's own runtime files
    (`HARNESS_RUNTIME`), which hooks write into every worker's worktree whether or not the worker did anything, and
    the coordinator's install linked in by `worktree_links` (normally hidden by `info/exclude` already).
    """
    out = _git(worktree, *_status_args(WINDOWS))
    if out is None:
        return UNKNOWN
    links = tuple(worktree_links.linked_paths(worktree))
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
        if not _harness_owned(entry[3:], links):
            count += 1
    return count


def _merge_targets(root: Path, base_branch: str) -> list[str]:
    """The refs a worker's commits count as merged into: the coordinator's HEAD, and the task's base branch when it
    still exists, with its upstream (for a local branch) or the local branches tracking it (for a remote-tracking one).
    No other branch counts."""
    targets = ["HEAD"]
    if not base_branch or _git(root, "rev-parse", "--verify", "--quiet", f"{base_branch}^{{commit}}") is None:
        return targets
    targets.append(base_branch)
    lines = (_git(root, "for-each-ref", "--format=%(refname) %(upstream)", "refs/heads/") or "").splitlines()
    for line in lines:
        name, _, upstream = line.partition(" ")
        if base_branch.startswith("refs/heads/") and name == base_branch and upstream:
            targets.append(upstream)
        elif base_branch.startswith("refs/remotes/") and upstream == base_branch:
            targets.append(name)
    return [ref for ref in dict.fromkeys(targets)
            if ref == "HEAD" or _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is not None]


def _unmerged(root: Path, head: str, base: str, base_branch: str = "") -> int:
    """Commits the worker added up to `head` that neither the coordinator's HEAD nor the task's base branch contains
    (`_merge_targets`), or UNKNOWN when git cannot say.

    The worker's commits are those after `base`, the commit the worktree started from. Without a recorded base,
    every commit the coordinator lacks counts, which overstates when the two started from different branches.
    """
    if not head:
        return UNKNOWN
    out = _git(root, "rev-list", "--count", head, "--not", *_merge_targets(root, base_branch), *([base] if base else []))
    return int(out.strip()) if out and out.strip().isdigit() else UNKNOWN


def _merged(root: Path, commit: str, base_branch: str) -> bool:
    """True when `commit` exists and is reachable from the coordinator's HEAD or the task's base branch."""
    out = _git(root, "rev-list", "--count", commit, "--not", *_merge_targets(root, base_branch))
    return out is not None and out.strip() == "0"


# Files the OS writes into any folder it shows. A directory holding only these (and directories of these) has no work.
OS_JUNK = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def _plain_dir(path: Path) -> bool:
    """A directory that is not a symlink or a junction: something that can be walked without leaving it."""
    return path.is_dir() and not worktree_links.is_link(path)


def _only_junk(path: Path) -> bool:
    """True for a directory that holds nothing but OS junk files and directories of those (or nothing at all). It is
    not a git worktree (that holds at least its `.git` file): it is what is left when a removal stopped after git
    removed the worktree, and counts as already gone. A symlink or junction anywhere is content."""
    if not _plain_dir(path):
        return False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                inner = Path(entry.path)
                if worktree_links.is_link(inner):
                    return False
                if entry.is_dir(follow_symlinks=False):
                    if not _only_junk(inner):
                        return False
                elif not (entry.name in OS_JUNK and entry.is_file(follow_symlinks=False)):
                    return False
    except OSError:
        return False
    return True


def _lost_gitdir(path: Path) -> Path | None:
    """The git directory a checkout's `.git` file names when that directory no longer exists, else None.

    That is a worktree whose registration was removed while its files stayed (`git worktree remove` stopped part-way).
    """
    marker = path / ".git"
    if worktree_links.is_link(marker) or not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    gitdir = gitdir if gitdir.is_absolute() else path / gitdir
    return None if gitdir.exists() else gitdir


ORPHAN_TIMEOUT_S = 300


def _git_against(common: Path, work_tree: Path, index: Path, *args: str) -> str | None:
    """Run git on `work_tree` with the object store and config of `common` and the separate index file `index`.

    Read-only for `work_tree`: the commands used here write only the index. fsmonitor and the untracked cache are off,
    so nothing outside `index` is started or written for that work tree.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_INDEX_FILE"] = str(index)
    command = ["git", "--git-dir", str(common), "--work-tree", str(work_tree),
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               *(["-c", "core.fileMode=false"] if WINDOWS else []), *args]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=ORPHAN_TIMEOUT_S, check=False, env=env, cwd=str(common))
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout if run.returncode == 0 else None


def _matches_commit(common: Path, path: Path, commit: str) -> bool:
    """True when the files in `path` are exactly `commit`'s, apart from ignored files (`.gitignore`, `info/exclude`),
    OS junk, the harness's runtime files and the coordinator's install links.

    `commit`'s tree is read into a temporary index, which is refreshed against the directory: a tracked file that is
    changed or missing, or an untracked file that is not ignored, is work the commit does not hold.
    """
    with tempfile.TemporaryDirectory(prefix="coding-agent-orphan-") as tmp:
        index = Path(tmp) / "index"
        if _git_against(common, path, index, "read-tree", commit) is None:
            return False
        _git_against(common, path, index, "update-index", "-q", "--refresh")  # exits 1 when files differ; diff-files says which
        changed = _git_against(common, path, index, "diff-files", "--name-only", "-z")
        untracked = _git_against(common, path, index, "ls-files", "--others", "--exclude-standard", "-z")
    if changed is None or untracked is None:
        return False
    links = tuple(worktree_links.linked_paths(path))
    if any(name and not _harness_owned(name, links) for name in changed.split("\0")):
        return False
    return not any(name and not _harness_owned(name, links) and name.rsplit("/", 1)[-1] not in OS_JUNK
                   for name in untracked.split("\0"))


def _orphan_is_merged(repo: Path, path: Path, gitdir: Path, record: dict[str, str]) -> bool:
    """True when removing the orphaned checkout `path` provably loses nothing: its `.git` file points into `repo`'s
    worktrees, and its files match a commit that is merged (`_merged`) — the worker's last commit the cleanup saw
    (`record["head"]`), the tip of the branch named like the directory, or the commit it started from."""
    common = orca_cli._git_common_dir(repo)
    if common is None or gitdir.parent.name != "worktrees" or gitdir.parent.parent.resolve() != common.resolve():
        return False
    branch = (_git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{path.name}^{{commit}}") or "").strip()
    for commit in dict.fromkeys(item for item in (record.get("head", ""), branch, record.get("base", "")) if item):
        if _merged(repo, commit, record.get("base_branch", "")) and _matches_commit(common, path, commit):
            return True
    return False


def candidates(root: Path, run: str | None = None) -> list[Candidate]:
    """Worker worktrees whose task settled, oldest first, with what removing each one would lose.

    Read from `root`'s workers ledger and from that of every repository `root` delegated into (`orca_cli.all_roots`);
    a worktree listed by two of them (repositories sharing one git directory) is listed once. A task no known
    repository owns makes `roots.discover` look its repository up from Orca first.
    """
    tasks = {str(task.get("id")): task for task in orca_cli.list_tasks(run)}
    roots.discover(root, tasks, run)
    home = orca.project_name(root)
    own = root.resolve()
    found: list[Candidate] = []
    seen: set[str] = set()
    for owner in orca_cli.all_roots(root):
        name = orca.project_name(owner) if owner != own else home
        for record in orca_cli.worker_records(owner):
            task = tasks.get(record["task_id"])
            if record["worktree"] in seen or task is None or task.get("status") not in SETTLED:
                continue
            seen.add(record["worktree"])
            path = Path(record["worktree"])
            exists = path.is_dir() and not _only_junk(path)
            repo = orca_cli.repo_of(owner, record)
            gitdir = _lost_gitdir(path) if exists else None
            if not exists:
                dirty = unmerged = 0
            elif gitdir is not None:
                dirty = unmerged = 0 if _orphan_is_merged(repo, path, gitdir, record) else UNKNOWN
            else:
                head = (_git(path, "rev-parse", "HEAD") or "").strip()
                if head and head != record.get("head"):
                    _keep_head(owner, record, head)
                dirty = _dirty(path)
                unmerged = _unmerged(repo, head, record.get("base", ""), record.get("base_branch", ""))
            found.append(Candidate(
                task_id=record["task_id"],
                dispatch=record["dispatch"],
                worktree=record["worktree"],
                status=str(task.get("status")),
                title=str(task.get("task_title") or task.get("title") or ""),
                exists=exists,
                dirty=dirty,
                unmerged=unmerged,
                root="" if owner == own else str(owner),
                repo="" if name == home else name,
                orphan=gitdir is not None,
            ))
    return found


def _keep_head(owner: Path, record: dict[str, str], head: str) -> None:
    """Record the worker's last commit, so the worktree can still be checked if its git registration is lost later."""
    try:
        orca_cli.record_head(owner, record, head)
    except OSError:
        pass  # a read-only ledger only costs that later check


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
                # The install links go first, as links: no removal below ever walks into the coordinator's install.
                worktree_links.unlink_install(Path(item.worktree))
            if item.orphan:
                # Git no longer knows the directory, so neither git nor Orca can remove it.
                _remove_orphan(Path(item.worktree))
            elif item.exists:
                # A safe worktree is removed with force too: `_dirty`/`_unmerged` already found no work in it, and
                # what is left (harness runtime files, Windows mode-only noise) makes `git worktree remove` refuse.
                orca_cli.remove_worktree(item.worktree, force=item.safe or discard)
            else:
                _remove_leftover(Path(item.worktree))
        except (orca_cli.OrcaError, OSError) as error:
            results.append((item, f"failed: {str(error)[:200]}"))
            continue
        orca_cli.mark_removed(Path(item.root) if item.root else root, item.worktree)
        events.record(root, guard="coordinator", kind=kind, mode="enforce", applied=True, session=session,
                      detail={"task_id": item.task_id, "worktree": item.worktree, "discarded": discard and not item.safe})
        results.append((item, "removed"))
    return results


def _remove_leftover(path: Path) -> None:
    """Delete what a half-finished removal left behind when it is only OS junk (`_only_junk`); anything else, and
    anything reached through a symlink or junction, is left alone."""
    if not _only_junk(path):
        return
    try:
        _remove_junk_tree(path)
    except OSError:
        pass  # gone already, or something appeared in it


def _remove_junk_tree(path: Path) -> None:
    with os.scandir(path) as entries:
        for entry in entries:
            inner = Path(entry.path)
            if worktree_links.is_link(inner):
                continue
            if entry.is_dir(follow_symlinks=False):
                _remove_junk_tree(inner)
            elif entry.name in OS_JUNK and entry.is_file(follow_symlinks=False):
                inner.unlink()
    os.rmdir(path)  # fails when anything else is in it


def _remove_orphan(path: Path) -> None:
    """Delete an orphaned checkout's directory tree. Links inside it are removed as links, never entered; a recorded
    path that is itself a link is refused."""
    if not _plain_dir(path):
        raise OSError(f"{path} is not a plain directory; not removed")
    shutil.rmtree(path)


def auto_enabled() -> bool:
    """False when `CODING_AGENT_AUTO_CLEAN` is `off` (or `0`, `false`, `no`); on by default."""
    return os.environ.get(AUTO_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def auto_clean(root: Path, run: str | None = None, *, session: str = "cli",
               found: list[Candidate] | None = None) -> list[tuple[Candidate, str]]:
    """Remove every settled worker worktree that loses no work, without asking. Returns `clean`'s (candidate, outcome).

    Removed: safe candidates (with force, see `clean`); records whose directory no longer exists or holds only OS junk
    (only marked removed, the junk directory deleted); and orphaned checkouts whose files match a merged commit
    (deleted directly, since git no longer knows them). Never touched: worktrees with commits merged into neither the
    coordinator's HEAD nor the task's base branch, uncommitted files, or a state git cannot report — including an
    orphaned checkout that cannot be proven to match a merged commit. An Orca error on one worktree is reported as its `failed:` outcome and does
    not stop the others; an error listing the tasks raises `orca_cli.OrcaError`. `found` reuses candidates the caller
    already listed.

    Limits: only tasks Orca lists for `run` are seen. With `run` None that is the Run the CLI is bound to
    (`CODING_AGENT_ORCA_RUN`, or the stored or bound Run); `orca orchestration task-list` has no option to list
    every Run, so a worker worktree whose task belongs to an older Run is never a candidate here and stays until
    it is removed by hand. A worktree recorded by `worker-adopt` without a real task id has no task and is not seen either.
    Merged means reachable: a squash or rebase merge leaves the worker's own commits unmerged. A worker recorded before
    the base branch was kept is compared with HEAD alone. An orphaned checkout is matched only against the worker's
    last commit the cleanup saw while git still knew it, the branch named like its directory, and its start commit.
    """
    if not auto_enabled():
        return []
    if found is None:
        if not any(orca_cli.worker_records(owner) for owner in orca_cli.all_roots(root)):
            return []
        found = candidates(root, run)
    chosen = [item for item in found if item.safe or not item.exists]
    return clean(root, chosen, session=session, kind="worktree-auto-removed") if chosen else []


def board_lines(found: list[Candidate]) -> list[str]:
    """Lines for the coordinator's board. Empty when no worktree is left.

    Merged, clean worktrees are removed by `auto_clean` before the board is built, so what is listed here normally
    holds work that is not merged or not committed. This group comes from git and the workers ledger, not from the
    inbox: a finished task whose branch is not merged stays listed until it is merged or discarded, even after its
    report was acknowledged.
    """
    if not found:
        return []
    lines = [f"xong, worktree còn việc chưa gộp hoặc chưa commit ({len(found)}):"]
    for item in found[:10]:
        repo = f", repo {item.repo}" if item.repo else ""
        lines.append(f"  - {Path(item.worktree).name} ({item.task_id}, {item.status}{repo}): {item.describe()}")
    lines.append("  việc cần làm: worktree đã gộp và sạch tự được xoá, không cần hỏi ai. Với các worktree trên: kiểm rồi gộp nhánh vào HEAD "
                 "hoặc vào nhánh base của task (nhánh worker được tạo từ đó, `base:`/`--base-branch`; lượt sau nó tự được dọn); "
                 "worktree ghi 'đăng ký worktree đã mất, cần kiểm tay' thì git không còn biết nó: kiểm thư mục bằng tay; "
                 "nếu muốn bỏ việc đó thì hỏi người dùng trước, rồi chạy `worktree-clean --task-id <id> --discard --yes` (mất code chưa gộp). "
                 "Worktree ghi 'sạch, đã gộp' chỉ còn khi tự dọn bị tắt (CODING_AGENT_AUTO_CLEAN=off): xoá bằng `worktree-clean --task-id <id>`, không cần --yes.")
    return lines
