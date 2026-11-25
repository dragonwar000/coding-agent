"""Remind the user, at session start, that upstream coding-agent has a newer commit than the installed copy. Never installs.

Install time (`project_install`): `record_source` notes where the copied package came from, in the `source` key of
`.coding-agent/installed.json` (the install record, outside the package, so the package digest of FR-004 is unchanged
and an older installer still owns the copy):

- from a git checkout: the `origin` URL, `HEAD`, whether the tree had uncommitted changes, and the checkout path;
- from `bootstrap.sh` / `bootstrap.ps1`: the repository and ref they downloaded, passed in `CODING_AGENT_SOURCE_REPO`
  and `CODING_AGENT_SOURCE_REF`. A ref that is not a full commit sha is resolved with `git ls-remote` once.

A linked install (`--link`) has no record: it runs the checkout itself. An install without a record (made before this
module, or from a source with no git and no bootstrap ref) never gets a reminder.

Session start (`check`, the `update-check` hook): reads `refs/heads/<branch>` of the recorded repository with
`git ls-remote` under a short timeout, at most once per `interval_h` (cached in `.coding-agent/state/update-check.json`;
a failed read is retried after at most an hour). When that commit is not the installed one, and the installed checkout
does not already contain it, the result carries one line with both short shas and the command to update by hand. The
same upstream commit is announced at most once per interval. Any failure (offline, no git, timeout, unreadable record)
is silent for the session. `CODING_AGENT_UPDATE_CHECK=off` (or 0, false, no) switches the check off for a shell.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from coding_agent import state
from coding_agent.manifest import UpdateCheck

SOURCE_REPO_ENV = "CODING_AGENT_SOURCE_REPO"
SOURCE_REF_ENV = "CODING_AGENT_SOURCE_REF"
OFF_ENV = "CODING_AGENT_UPDATE_CHECK"
OFF_VALUES = ("off", "0", "false", "no")
INSTALLED = Path(".coding-agent") / "installed.json"
CACHE_FILE = Path(".coding-agent") / "state" / "update-check.json"
# A failed read is retried sooner than a successful one is refreshed, but never on every session of an offline machine.
FAILURE_RETRY_S = 3600
RESOLVE_TIMEOUT_S = 10
SHORT = 7
FULL_SHA = re.compile(r"[0-9a-f]{40}")
GITHUB = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
DEFAULT_OWNER = "dragonwar000"
DEFAULT_REPO = "coding-agent"

# (repository URL, ref, timeout in seconds) -> the commit sha of `ref`, or None when it cannot be read.
LsRemote = Callable[[str, str, float], "str | None"]


def _git(args: list[str], *, cwd: Path | None = None, timeout: float = RESOLVE_TIMEOUT_S) -> str | None:
    """`git <args>` stdout, stripped; None on a non-zero exit, a timeout, or no git. Never prompts for credentials."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if "GIT_SSH_COMMAND" not in env:
        env["GIT_SSH_COMMAND"] = f"ssh -o BatchMode=yes -o ConnectTimeout={max(1, int(timeout))}"
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                                stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def parse_ls_remote(text: str, ref: str) -> str | None:
    """The commit `git ls-remote` printed for `ref`: the exact ref, else the branch, else the peeled tag, else the tag."""
    shas: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and FULL_SHA.fullmatch(parts[0].lower()):
            shas.setdefault(parts[1], parts[0].lower())
    for name in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if name in shas:
            return shas[name]
    return None


def ls_remote(repo: str, ref: str, timeout: float) -> str | None:
    """The commit of `ref` in the repository at `repo`, read with `git ls-remote`; None on any failure."""
    out = _git(["ls-remote", "--", repo, ref], timeout=timeout)
    return parse_ls_remote(out, ref) if out else None


def record_source(package: Path, env: Mapping[str, str] | None = None, remote: LsRemote | None = None) -> dict[str, Any] | None:
    """Where the package about to be copied came from, for `installed.json`; None when that cannot be known.

    `package` is the `coding_agent` directory being copied. The bootstrap variables win over the checkout around it.
    """
    env = os.environ if env is None else env
    remote = remote or ls_remote
    repo = env.get(SOURCE_REPO_ENV, "").strip()
    ref = env.get(SOURCE_REF_ENV, "").strip()
    if repo and ref:
        commit = ref.lower() if FULL_SHA.fullmatch(ref.lower()) else remote(repo, ref, RESOLVE_TIMEOUT_S)
        return {"via": "bootstrap", "repo": repo, "ref": ref, "commit": commit}
    checkout = package.resolve().parent.parent
    top = _git(["rev-parse", "--show-toplevel"], cwd=checkout)
    # A package inside some other repository (an installed copy in a project) is not a coding-agent checkout.
    if top is None or Path(top).resolve() != checkout:
        return None
    commit = _git(["rev-parse", "HEAD"], cwd=checkout)
    origin = _git(["remote", "get-url", "origin"], cwd=checkout)
    if not commit or not origin:
        return None
    status = _git(["status", "--porcelain", "--untracked-files=no"], cwd=checkout)
    return {"via": "checkout", "repo": origin, "commit": commit.lower(), "dirty": None if status is None else status != "",
            "checkout": str(checkout)}


def source_line(source: dict[str, Any] | None) -> str:
    """The installer's report line for the recorded source."""
    if not source:
        return "source: unknown (no git checkout with an origin, no bootstrap ref): no update reminder"
    commit = source.get("commit")
    if not commit:
        return f"source: {source.get('repo')} at {source.get('ref')} (commit unresolved): no update reminder"
    dirty = " with uncommitted changes" if source.get("dirty") else ""
    return f"source: {source.get('repo')} at {str(commit)[:SHORT]}{dirty}; session start reminds when its main moves"


def github_slug(url: str) -> tuple[str, str] | None:
    """(owner, repository) of a GitHub URL in https, scp-like ssh or ssh:// form; None for anything else."""
    match = GITHUB.match(url.strip())
    return (match.group(1), match.group(2)) if match else None


def _python_src_flag(python_src: Any) -> list[str]:
    from coding_agent.project_install import DEFAULT_PYTHON_SRC

    return ["--python-src", str(python_src)] if python_src and python_src != DEFAULT_PYTHON_SRC else []


def update_command(source: dict[str, Any], upstream: str, root: Path, branch: str, python_src: Any = None,
                   platform: str = sys.platform) -> str | None:
    """The command that updates this project by hand: the recorded checkout's `install.sh` when that checkout is
    still here, else the README bootstrap line pinned to `upstream`. None when neither applies."""
    extra = _python_src_flag(python_src) + ["--add-new-hooks"]
    checkout = source.get("checkout")
    if source.get("via") == "checkout" and checkout and (Path(checkout) / "install.sh").is_file():
        installer = shlex.quote(str(Path(checkout) / "install.sh"))
        return f"git -C {shlex.quote(checkout)} pull --ff-only && bash {installer} {shlex.quote(str(root))} {shlex.join(extra)}"
    slug = github_slug(str(source.get("repo") or ""))
    if slug is None:
        return None
    owner, repo = slug
    names = {} if slug == (DEFAULT_OWNER, DEFAULT_REPO) else {"CODING_AGENT_OWNER": owner, "CODING_AGENT_REPO": repo}
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
    if platform == "win32":
        assigns = "".join(f"$env:{name}='{value}'; " for name, value in {**names, "CODING_AGENT_REF": upstream}.items())
        return f"{assigns}& ([scriptblock]::Create((irm {base}/bootstrap.ps1))) {' '.join(extra)}"
    assigns = " ".join(f"{name}={value}" for name, value in {**names, "CODING_AGENT_REF": upstream}.items())
    return f"curl -fsSL {base}/bootstrap.sh | {assigns} bash -s -- {shlex.join(extra)}"


def notice_text(source: dict[str, Any], upstream: str, branch: str, command: str | None) -> str:
    """The one line the session gets: installed and upstream short shas, and how to update by hand."""
    slug = github_slug(str(source.get("repo") or ""))
    where = "/".join(slug) if slug else str(source.get("repo"))
    dirty = " (có sửa chưa commit)" if source.get("dirty") else ""
    how = f"Cập nhật bằng tay khi tiện: {command}" if command else f"Cập nhật bằng tay: cài lại từ {source.get('repo')}."
    return (f"[update-check] coding-agent có commit mới trên {where}@{branch}: đang cài {str(source['commit'])[:SHORT]}{dirty}, "
            f"upstream {upstream[:SHORT]}. {how} (Agent: chỉ báo cho người dùng, không tự chạy lệnh này.)")


@dataclass(frozen=True)
class Result:
    """What `check` decided. `message` is set only when the session should see the reminder."""
    kind: str
    message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def switched_off(env: Mapping[str, str] | None = None) -> bool:
    """True when `CODING_AGENT_UPDATE_CHECK` is off, 0, false or no (any case)."""
    value = (os.environ if env is None else env).get(OFF_ENV, "")
    return value.strip().lower() in OFF_VALUES


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _contains(checkout: Any, installed: str, upstream: str) -> bool:
    """True when the recorded checkout knows `upstream` and it is already part of the installed commit (installed ahead)."""
    if not checkout or not Path(str(checkout)).is_dir():
        return False
    path = Path(str(checkout))
    if _git(["cat-file", "-e", f"{upstream}^{{commit}}"], cwd=path) is None:
        return False
    return _git(["merge-base", "--is-ancestor", upstream, installed], cwd=path) is not None


def check(root: Path, settings: UpdateCheck, *, now: float | None = None, remote: LsRemote | None = None,
          env: Mapping[str, str] | None = None, platform: str = sys.platform) -> Result:
    """Decide whether this session gets the update reminder. Never raises for a missing or unreadable file."""
    if switched_off(env):
        return Result("switched-off")
    installed = _read(root / INSTALLED)
    if installed.get("link"):
        return Result("linked")
    source = installed.get("source")
    if not isinstance(source, dict) or not source.get("repo") or not FULL_SHA.fullmatch(str(source.get("commit") or "")):
        return Result("no-source")
    now = time.time() if now is None else now
    remote = remote or ls_remote
    repo, commit, branch = str(source["repo"]), str(source["commit"]), settings.branch
    interval = settings.interval_h * 3600
    cache_path = root / CACHE_FILE
    cache = _read(cache_path)
    if cache.get("repo") != repo or cache.get("branch") != branch:
        cache = {"repo": repo, "branch": branch}
    upstream = cache.get("upstream") if isinstance(cache.get("upstream"), str) else None
    checked_at = cache.get("checked_at")
    fresh = isinstance(checked_at, (int, float)) and now - checked_at < (interval if upstream else min(interval, FAILURE_RETRY_S))
    fetched = False
    if not fresh:
        upstream = remote(repo, f"refs/heads/{branch}", settings.timeout_s)
        cache.update(checked_at=now, upstream=upstream)
        state._write_json(cache_path, cache)
        fetched = True
        if upstream is None:
            return Result("check-failed", detail={"repo": repo, "branch": branch, "fetched": True})
    if upstream is None:
        return Result("cached")
    detail = {"repo": repo, "branch": branch, "installed": commit[:SHORT], "upstream": upstream[:SHORT], "fetched": fetched}
    if upstream == commit:
        return Result("up-to-date", detail=detail)
    if _contains(source.get("checkout"), commit, upstream):
        return Result("installed-ahead", detail=detail)
    notified_at = cache.get("notified_at")
    if cache.get("notified") == upstream and isinstance(notified_at, (int, float)) and now - notified_at < interval:
        return Result("already-notified", detail=detail)
    cache.update(notified=upstream, notified_at=now)
    state._write_json(cache_path, cache)
    command = update_command(source, upstream, root, branch, installed.get("python_src"), platform)
    return Result("update-available", notice_text(source, upstream, branch, command), detail)
