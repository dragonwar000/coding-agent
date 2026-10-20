"""Keep-or-revert ratchet over verified attempts (Graph Engineering §II, Karpathy's autoresearch loop).

stop-gate calls `step` at a Stop whose verification passed. The first verified attempt of a turn becomes the
baseline; every later attempt is measured with the manifest's `metric` command and kept only when it beats the
best kept score by more than `min_delta`. A worse attempt, or one whose metric fails, has the work tree restored
to the kept snapshot. The loop stops after `no_improve_stop` consecutive attempts that did not improve, or after
`max_trials_per_turn`, and leaves the tree at the best kept state.

Snapshots are git objects made through a temporary index (`GIT_INDEX_FILE`): HEAD, branches and the user's index
are never touched. Every attempt gets a ref under `refs/coding-agent/ratchet/<session>/`, so a reverted attempt
stays addressable. `.coding-agent/` is left out of every snapshot, so a restore never rolls back the harness's own
state and event log.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.manifest import Ratchet

TAIL_CHARS = 2000
GIT_TIMEOUT_S = 120
REF_PREFIX = "refs/coding-agent/ratchet"
EXCLUDE = ":(exclude).coding-agent"
IDENTITY = {
    "GIT_AUTHOR_NAME": "coding-agent ratchet",
    "GIT_AUTHOR_EMAIL": "ratchet@coding-agent.invalid",
    "GIT_COMMITTER_NAME": "coding-agent ratchet",
    "GIT_COMMITTER_EMAIL": "ratchet@coding-agent.invalid",
}
# Variables that would point git at another repository, index or config than the work tree's own.
CLEARED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
           "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
           "GIT_CEILING_DIRECTORIES", "GIT_PREFIX")
NUMBER = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")


class GitError(RuntimeError):
    """A git command failed; the message names it."""


@dataclass
class Snapshot:
    tree: str
    commit: str
    index: str
    workdir: str

    def dispose(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in CLEARED and not key.startswith("GIT_CONFIG_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    env.update(extra or {})
    return env


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=_env(env), check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"git {args[0]}: {error}") from error
    if done.returncode != 0:
        raise GitError(f"git {args[0]} failed (exit {done.returncode}): {done.stderr.strip()[:400]}")
    return done.stdout


def toplevel(cwd: Path) -> Path | None:
    """The work tree's top directory, or None outside git."""
    try:
        return Path(_git(cwd, "rev-parse", "--show-toplevel").strip())
    except GitError:
        return None


def _head(root: Path) -> str | None:
    try:
        out = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").strip()
    except GitError:
        return None
    return out or None


def snapshot(root: Path, parent: str | None, message: str) -> Snapshot:
    """Record the work tree (tracked and untracked, `.gitignore` respected, `.coding-agent/` left out) as a commit."""
    workdir = tempfile.mkdtemp(prefix="coding-agent-ratchet-")
    index = os.path.join(workdir, "index")
    try:
        source = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        # The copy keeps the source's mtime: git re-reads entries modified in the same second as the index file,
        # and a newer mtime would let a same-size edit pass as unchanged.
        if source.exists():
            shutil.copy2(source, index)
        env = {"GIT_INDEX_FILE": index}
        _git(root, "add", "-A", "--", ".", EXCLUDE, env=env)
        # A copied index may still list .coding-agent paths the user tracks; drop them from the snapshot.
        _git(root, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".coding-agent", env=env)
        tree = _git(root, "write-tree", env=env).strip()
        parents = ["-p", parent] if parent else []
        commit = _git(root, "commit-tree", "--no-gpg-sign", tree, *parents, "-m", message, env=IDENTITY).strip()
        return Snapshot(tree=tree, commit=commit, index=index, workdir=workdir)
    except GitError:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def set_ref(root: Path, ref: str, commit: str) -> None:
    _git(root, "update-ref", ref, commit)


def restore(root: Path, current: Snapshot, kept_tree: str) -> None:
    """Make the work tree equal `kept_tree` for snapshot scope: changed files rewritten, added files removed."""
    _git(root, "read-tree", "-m", "-u", current.tree, kept_tree, env={"GIT_INDEX_FILE": current.index})


def ref_name(session: str, name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)[:80] or "default"
    return f"{REF_PREFIX}/{safe}/{name}"


def parse_score(output: str) -> float | None:
    """The number on the last non-blank line of the metric's stdout, or None."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or not NUMBER.match(lines[-1]):
        return None
    value = float(lines[-1])
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def measure(command: str, cwd: Path, timeout_s: int) -> dict[str, Any]:
    """Run the metric. `score` is None when it fails, times out or prints no number."""
    try:
        run = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as error:
        out = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        return {"score": None, "exit": None, "timed_out": True, "tail": out[-TAIL_CHARS:]}
    score = parse_score(run.stdout) if run.returncode == 0 else None
    return {"score": score, "exit": run.returncode, "timed_out": False, "tail": (run.stdout + run.stderr)[-TAIL_CHARS:]}


def better(score: float, best: float, config: Ratchet) -> bool:
    return score > best + config.min_delta if config.direction == "max" else score < best - config.min_delta


@dataclass
class Outcome:
    """One ratchet decision. `steer` is the continuation text when the turn should go on, None to let it end."""
    decision: str
    detail: dict[str, Any]
    steer: str | None = None


def step(root: Path, session: str, state: dict[str, Any], config: Ratchet, *, apply: bool) -> list[Outcome]:
    """Judge the attempt at this Stop. `state` is the turn's ratchet state and is updated in place.

    With `apply` false (shadow) nothing is restored and nothing steers; the git objects are still recorded.
    Returns the decisions in order; the last one carries the steer, if any.
    """
    if state.get("stopped"):
        return []
    top = toplevel(root)
    if top is None:
        state["stopped"] = True
        return [Outcome("unavailable", {"reason": "not a git work tree"})]
    seq = int(state.get("seq", 0)) + 1
    state["seq"] = seq
    trials = int(state.get("trials", 0))
    kept_commit, kept_tree = state.get("kept_commit"), state.get("kept_tree")
    try:
        current = snapshot(top, kept_commit or _head(top), f"coding-agent ratchet trial {seq}")
    except GitError as error:
        state["stopped"] = True
        return [Outcome("unavailable", {"reason": str(error)[:400]})]
    try:
        set_ref(top, ref_name(session, f"t{seq:04d}"), current.commit)
        if kept_tree is not None and current.tree == kept_tree:
            state["stopped"] = True
            return [Outcome("stopped", {"reason": "no-change", "best": state.get("best"), "commit": kept_commit})]
        trials += 1
        state["trials"] = trials
        metric = measure(config.metric, top, config.metric_timeout_s)
        score = metric["score"]
        base = {"trial": trials, "score": score, "commit": current.commit, "parent": kept_commit,
                "metric_exit": metric["exit"], "metric_timed_out": metric["timed_out"], "metric_tail": metric["tail"][-400:]}
        outcomes: list[Outcome] = []
        if kept_tree is None:
            if score is None:
                state["stopped"] = True
                return [Outcome("crash", {**base, "reason": "baseline metric produced no number"})]
            state.update({"kept_commit": current.commit, "kept_tree": current.tree, "best": score, "no_improve": 0})
            set_ref(top, ref_name(session, "kept"), current.commit)
            outcomes.append(Outcome("baseline", {**base, "best": score}))
        elif score is not None and better(score, float(state["best"]), config):
            state.update({"kept_commit": current.commit, "kept_tree": current.tree, "best": score, "no_improve": 0})
            set_ref(top, ref_name(session, "kept"), current.commit)
            outcomes.append(Outcome("kept", {**base, "best": score}))
        else:
            if apply:
                restore(top, current, kept_tree)
            state["no_improve"] = int(state.get("no_improve", 0)) + 1
            outcomes.append(Outcome("crash" if score is None else "reverted", {**base, "best": state["best"], "restored": apply}))
        reason = None
        if int(state.get("no_improve", 0)) >= config.no_improve_stop:
            reason = "no-improve"
        elif trials >= config.max_trials_per_turn:
            reason = "max-trials"
        if reason is not None:
            state["stopped"] = True
            outcomes.append(Outcome("stopped", {"reason": reason, "best": state["best"], "commit": state.get("kept_commit")}))
            return outcomes
        if apply:
            outcomes[-1].steer = _steer(outcomes[-1].decision, score, float(state["best"]), config, int(state.get("no_improve", 0)))
        return outcomes
    finally:
        current.dispose()


def restore_kept(root: Path, state: dict[str, Any]) -> bool:
    """Put the work tree back at the kept snapshot (used when a turn gives up with verification still red)."""
    kept_tree = state.get("kept_tree")
    top = toplevel(root)
    if kept_tree is None or top is None:
        return False
    current = snapshot(top, state.get("kept_commit"), "coding-agent ratchet give-up")
    try:
        if current.tree != kept_tree:
            restore(top, current, kept_tree)
        state["stopped"] = True
        return True
    finally:
        current.dispose()


def _steer(decision: str, score: float | None, best: float, config: Ratchet, no_improve: int) -> str:
    want = "higher" if config.direction == "max" else "lower"
    left = config.no_improve_stop - no_improve
    if decision in ("baseline", "kept"):
        head = f"[ratchet] {decision}: metric {score:g} is the best kept score."
    elif decision == "reverted":
        head = f"[ratchet] reverted: metric {score:g} did not beat the best kept score {best:g}; the work tree is back at the best kept state."
    else:
        head = f"[ratchet] reverted: the metric command failed or printed no number; the work tree is back at the best kept state (best {best:g})."
    return (f"{head} Make one motivated change that should give a {want} metric (`{config.metric}`), keep the verification green, "
            f"then finish. The loop stops after {left} more attempt(s) without improvement.")
