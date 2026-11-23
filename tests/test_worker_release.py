"""Finished workers: bytecode is not work, idle sessions are released, and cleanup runs without a coordinator."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import yaml

from coding_agent import cleanup, coordinator, events, gen, orca_cli, release, resume, watcher
from coding_agent.hooks import handlers
from coding_agent.manifest import load
from test_hooks import manifest as write_manifest

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"
SRC = Path(__file__).resolve().parents[1] / "src"
GIT = ["-c", "user.email=t@t", "-c", "user.name=t"]
SESSION = "11111111-2222-3333-4444-555555555555"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *GIT, *args], check=True, capture_output=True, text=True).stdout


class World:
    """A repository, the fake Orca, and workers that meet every release condition until a test changes one."""

    def __init__(self, root: Path, state: Path):
        self.root, self.state = root, state
        self.count = 0

    def data(self) -> dict[str, Any]:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def edit(self, change: Callable[[dict[str, Any]], None]) -> None:
        data = self.data()
        change(data)
        self.state.write_text(json.dumps(data), encoding="utf-8")

    def worker(self, name: str, status: str = "completed", run: str = "run_a", owner: Path | None = None) -> SimpleNamespace:
        self.count += 1
        repo = owner or self.root
        task, dispatch, handle, pty = f"task_{self.count}", f"ctx_{self.count}", f"term_{self.count}", f"pty_{self.count}"
        tree = repo.parent / name
        _git(repo, "worktree", "add", "-q", "-b", name, str(tree))
        orca_cli.record_worker(repo, worktree=str(tree), task_id=task, dispatch=dispatch, run=run)
        resolved = str(tree.resolve())
        report = {"id": f"msg_{task}", "type": "worker_done", "subject": "done", "body": "x", "created_at": "2026-11-23T01:00:00Z",
                  "payload": json.dumps({"taskId": task, "dispatchId": dispatch, "outcome": "succeeded"})}

        def add(data: dict[str, Any]) -> None:
            data["tasks"][task] = {"id": task, "title": name, "status": status, "run": run, "deps": "[]"}
            data.setdefault("dispatches", {})[dispatch] = {"task": task, "assignee": handle, "creator": "term_coordinator",
                                                           "incarnation": f"{pty}:inc", "status": "completed"}
            data.setdefault("terminals", {})[handle] = {"handle": handle, "worktreePath": resolved, "ptyId": pty, "incarnationId": "inc",
                                                        "agentIdentity": "claude", "connected": True,
                                                        "lastOutputAt": int((time.time() - 3600) * 1000)}
            data.setdefault("acked_messages", []).append(report)
        self.edit(add)
        return SimpleNamespace(tree=tree, task=task, dispatch=dispatch, handle=handle, pty=pty, name=name, report=report)

    def closed(self) -> list[str]:
        return self.data().get("closed_terminals", [])

    def released(self) -> list[str]:
        return self.data().get("released", [])

    def removed(self) -> list[str]:
        return [item["path"] for item in self.data().get("removed_worktrees", [])]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    state = tmp_path / "orca.json"
    state.write_text(json.dumps({"tasks": {}}), encoding="utf-8")
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv(orca_cli.RUN_ENV, "run_a")
    for name in (cleanup.AUTO_ENV, release.ENV, release.IDLE_ENV, "ORCA_TERMINAL_HANDLE", "FAKE_ORCA_SCREEN", "FAKE_ORCA_FAIL", "CODING_AGENT_ROLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(release, "_server_running", lambda pty, agent, processes: False)
    monkeypatch.setattr(watcher, "_overloaded", lambda: False)
    return World(root, state)


def _commit(tree: Path, name: str = "work.txt") -> None:
    (tree / name).write_text("work\n", encoding="utf-8")
    _git(tree, "add", name)
    _git(tree, "commit", "-q", "-m", name)


def _events(root: Path, kind: str) -> list[dict]:
    path = events.log_path(root)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [row for row in rows if row["kind"] == kind]


def _bytecode(tree: Path) -> None:
    cache = tree / "harness" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.cpython-314.pyc").write_bytes(b"\0")
    (tree / "loose.pyc").write_bytes(b"\0")


# --- bug 1: bytecode is not work --------------------------------------------------------------------------------------


def test_untracked_bytecode_does_not_make_a_worktree_dirty(world: World) -> None:
    item = world.worker("w1")
    _bytecode(item.tree)
    assert cleanup._dirty(item.tree) == 0


def test_a_real_file_beside_bytecode_is_still_work(world: World) -> None:
    item = world.worker("w1")
    _bytecode(item.tree)
    (item.tree / "harness" / "notes.py").write_text("x = 1\n", encoding="utf-8")
    assert cleanup._dirty(item.tree) == 1


def test_a_tracked_pyc_that_changed_still_counts(world: World) -> None:
    item = world.worker("w1")
    (item.tree / "shipped.pyc").write_bytes(b"\1")
    _git(item.tree, "add", "-f", "shipped.pyc")
    _git(item.tree, "commit", "-q", "-m", "ship")
    assert cleanup._dirty(item.tree) == 0
    (item.tree / "shipped.pyc").write_bytes(b"\2")
    assert cleanup._dirty(item.tree) == 1


def test_a_staged_new_pyc_still_counts(world: World) -> None:
    item = world.worker("w1")
    (item.tree / "new.pyc").write_bytes(b"\1")
    _git(item.tree, "add", "-f", "new.pyc")
    assert cleanup._dirty(item.tree) == 1


def test_a_merged_worktree_with_bytecode_is_removed(world: World) -> None:
    item = world.worker("w1")
    _commit(item.tree)
    _git(world.root, "merge", "-q", item.name)
    _bytecode(item.tree)
    assert [outcome for _i, outcome in cleanup.auto_clean(world.root)] == ["removed"]
    assert not item.tree.exists()


def test_an_orphan_with_bytecode_matches_its_merged_commit(world: World) -> None:
    item = world.worker("w1")
    _commit(item.tree)
    _git(world.root, "merge", "-q", item.name)
    head = _git(item.tree, "rev-parse", "HEAD").strip()
    _bytecode(item.tree)
    common = orca_cli._git_common_dir(world.root)
    assert cleanup._matches_commit(common, item.tree, head)
    (item.tree / "real.txt").write_text("work\n", encoding="utf-8")
    assert not cleanup._matches_commit(common, item.tree, head)


def test_a_leftover_of_bytecode_counts_as_gone_and_a_real_file_does_not(tmp_path: Path) -> None:
    left = tmp_path / "left"
    (left / "pkg" / "__pycache__").mkdir(parents=True)
    (left / "pkg" / "__pycache__" / "a.cpython-314.pyc.123").write_bytes(b"\0")
    (left / ".DS_Store").write_bytes(b"\0")
    assert cleanup._only_junk(left)
    cleanup._remove_leftover(left)
    assert not left.exists()
    kept = tmp_path / "kept"
    (kept / "__pycache__").mkdir(parents=True)
    (kept / "main.py").write_text("x\n", encoding="utf-8")
    assert not cleanup._only_junk(kept)


def test_a_worktree_under_a_directory_named_pycache_is_not_junk(tmp_path: Path) -> None:
    tree = tmp_path / "__pycache__"
    tree.mkdir()
    (tree / "work.txt").write_text("x\n", encoding="utf-8")
    assert not cleanup._only_junk(tree)


MANIFEST = """schema: 1
verified: false
python_src: src
hooks:
  - id: prompt-reset
    host: [claude_code]
    event: UserPromptSubmit
    mode: shadow
    assumption: "a new prompt starts a new turn"
"""


def test_generated_hooks_write_no_bytecode_and_an_older_install_still_passes_the_gate(tmp_path: Path) -> None:
    (tmp_path / "integration.yaml").write_text(MANIFEST, encoding="utf-8")
    loaded = load(tmp_path / "integration.yaml")
    gen.write(loaded, apply=True, hosts=["claude_code"])
    path = tmp_path / ".claude" / "settings.json"
    text = path.read_text(encoding="utf-8")
    assert "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=" in text
    assert gen.drift(loaded, ["claude_code"]) == []

    path.write_text(text.replace("PYTHONDONTWRITEBYTECODE=1 ", ""), encoding="utf-8")
    assert gen.drift(loaded, ["claude_code"]) == []  # an install from before the change
    gen.write(loaded, apply=True, hosts=["claude_code"])
    assert "PYTHONDONTWRITEBYTECODE=1" in path.read_text(encoding="utf-8")  # upgraded by the next generation

    path.write_text(text.replace("prompt-reset", "other-hook"), encoding="utf-8")
    assert gen.drift(loaded, ["claude_code"]) != []  # real drift is still found


# --- bug 2: the release step ------------------------------------------------------------------------------------------


def test_a_finished_acknowledged_idle_worker_is_released_and_keeps_its_work(world: World) -> None:
    item = world.worker("w1")
    _commit(item.tree)  # not merged
    (item.tree / "draft.txt").write_text("uncommitted\n", encoding="utf-8")
    world.edit(lambda d: d.update(release_answer={"state": "retained", "reason": "external_terminal"}))
    resume.remember_transcript(item.tree, {"task_id": item.task, "dispatch": item.dispatch}, f"/tmp/{SESSION}.jsonl", SESSION)

    outcome = release.sweep(world.root)

    assert [entry.task_id for entry in outcome.released] == [item.task]
    assert world.closed() == [item.handle]
    assert (item.tree / "draft.txt").exists() and _git(world.root, "rev-parse", "--verify", item.name).strip()
    assert world.removed() == []
    assert len(_events(world.root, "worker-session-released")) == 1

    again = release.sweep(world.root)  # recorded: nothing is read or tried again
    assert (again.released, again.waiting, again.kept) == ([], [], [])
    assert world.closed() == [item.handle] and world.released() == [item.dispatch]

    remaining = cleanup.candidates(world.root)
    lines = release.board_lines(world.root, remaining)
    assert len(lines) == 2 and f"cd {item.tree.resolve()} && claude --resume {SESSION}" in lines[1]


def test_a_managed_terminal_is_released_by_orca_and_not_closed_by_hand(world: World) -> None:
    item = world.worker("w1")
    release.sweep(world.root)
    assert world.released() == [item.dispatch] and world.closed() == []


def _unread(world: World, item: SimpleNamespace) -> None:
    def change(data: dict) -> None:
        data["acked_messages"].remove(item.report)
        data.setdefault("messages", []).append(item.report)
    world.edit(change)


def _no_report(world: World, item: SimpleNamespace) -> None:
    world.edit(lambda d: d["acked_messages"].remove(item.report))


def _question(world: World, item: SimpleNamespace) -> None:
    world.edit(lambda d: d["acked_messages"].append({
        "id": "q1", "type": "question", "subject": "?", "created_at": "2026-11-23T02:00:00Z",
        "payload": json.dumps({"taskId": item.task, "dispatchId": item.dispatch, "question": "which one?"})}))


def _set_dispatch(**values: Any) -> Callable[[World, SimpleNamespace], None]:
    return lambda world, item: world.edit(lambda d: d["dispatches"][item.dispatch].update(values))


def _set_terminal(**values: Any) -> Callable[[World, SimpleNamespace], None]:
    return lambda world, item: world.edit(lambda d: d["terminals"][item.handle].update(values))


def _newer_dispatch(world: World, item: SimpleNamespace) -> None:
    world.edit(lambda d: d["dispatches"].update(ctx_new={"task": item.task, "assignee": "term_new", "status": "dispatched"}))


GATES: dict[str, Callable[[World, SimpleNamespace], None]] = {
    "unread report": _unread,
    "no report at all": _no_report,
    "open question": _question,
    "unread message for the worker": lambda w, i: w.edit(lambda d: d.update(terminal_mail={i.handle: [{"id": "m", "type": "status"}]})),
    "working": _set_dispatch(activity="working"),
    "waiting on a prompt": _set_dispatch(observation={"status": "live", "exactWorker": True, "agentWait": {"kind": "permission"}}),
    "wait never checked": _set_dispatch(observation={"status": "live", "exactWorker": True}),
    "identity not proven": _set_dispatch(observation={"status": "live", "exactWorker": False, "agentWait": None}),
    "output inside the grace period": _set_terminal(lastOutputAt=int(time.time() * 1000)),
    "no output time": _set_terminal(lastOutputAt=None),
    "another process in the pane": _set_terminal(incarnationId="other"),
    "terminal in another directory": _set_terminal(worktreePath="/somewhere/else"),
    "no recorded incarnation": _set_dispatch(incarnation=None),
    "coordinator's terminal": _set_dispatch(creator="term_1"),
    "a newer dispatch": _newer_dispatch,
}


@pytest.mark.parametrize("gate", list(GATES))
def test_each_condition_keeps_the_worker(world: World, gate: str) -> None:
    item = world.worker("w1")
    GATES[gate](world, item)
    outcome = release.sweep(world.root)
    assert outcome.released == [] and world.closed() == [] and world.released() == []
    assert item.tree.exists()


@pytest.mark.parametrize("status", ["dispatched", "running", "ready", "pending", "blocked"])
def test_a_task_not_settled_is_not_touched(world: World, status: str) -> None:
    world.worker("w1", status=status)
    assert release.sweep(world.root).released == [] and world.closed() == [] and world.released() == []


def test_a_screen_with_a_spinner_keeps_the_worker(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    monkeypatch.setenv("FAKE_ORCA_SCREEN", "✶ Thinking… (3s)\n\n❯ ")
    assert release.sweep(world.root).released == [] and world.closed() == []


def test_the_terminal_running_this_code_is_never_closed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = world.worker("w1")
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", item.handle)
    assert release.sweep(world.root).released == [] and world.closed() == []


@pytest.mark.parametrize("answer", [True, None], ids=["port open", "cannot check"])
def test_a_listening_process_or_an_unknown_answer_keeps_the_worker(world: World, monkeypatch: pytest.MonkeyPatch, answer: bool | None) -> None:
    world.worker("w1")
    monkeypatch.setattr(release, "_server_running", lambda pty, agent, processes: answer)
    assert release.sweep(world.root).released == [] and world.closed() == []


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_listening_sockets_are_read_from_the_real_process_tree() -> None:
    code = "import socket,sys,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print('up',flush=True); time.sleep(30)"
    server = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    quiet = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert server.stdout is not None and server.stdout.readline().strip() == "up"
        processes = release._Processes()
        processes._pids = {"busy": server.pid, "calm": quiet.pid}
        assert release._server_running("busy", "claude", processes) is True
        assert release._server_running("calm", "claude", processes) is False
        assert release._server_running("unknown", "claude", processes) is None
        program = release._program(next(name for pid, name in processes.tree("busy")))  # the agent's own socket does not count
        assert release._server_running("busy", program, processes) is False
    finally:
        server.kill()
        quiet.kill()
        server.wait()
        quiet.wait()


def test_a_per_task_acknowledgement_counts(world: World) -> None:
    item = world.worker("w1")
    _unread(world, item)
    path = world.root / orca_cli.ACKED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": item.report["id"], "task_id": item.task}) + "\n", encoding="utf-8")
    assert [entry.task_id for entry in release.sweep(world.root).released] == [item.task]


def test_the_grace_period_is_configurable(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = world.worker("w1")
    world.edit(lambda d: d["terminals"][item.handle].update(lastOutputAt=int((time.time() - 300) * 1000)))
    assert release.sweep(world.root).released == []
    monkeypatch.setenv(release.IDLE_ENV, "4")
    assert len(release.sweep(world.root).released) == 1


@pytest.mark.parametrize("value", ["abc", "0", "-3", "nan", "inf"])
def test_a_bad_grace_value_means_the_default(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(release.IDLE_ENV, value)
    assert release.idle_seconds() == release.IDLE_MINUTES * 60


def test_a_session_that_already_exited_is_recorded_and_nothing_is_closed(world: World) -> None:
    item = world.worker("w1")
    _unread(world, item)
    _set_dispatch(observation={"status": "exited", "exactWorker": True})(world, item)
    assert len(release.sweep(world.root).released) == 1
    assert world.closed() == [] and world.released() == []
    assert release.marks(world.root)[item.dispatch]["state"] == "ended"


def test_a_terminal_kept_for_another_reason_is_respected(world: World) -> None:
    item = world.worker("w1")
    world.edit(lambda d: d.update(release_answer={"state": "retained", "reason": "user_retained"}))
    outcome = release.sweep(world.root)
    assert outcome.released == [] and world.closed() == []
    assert release.marks(world.root)[item.dispatch]["state"] == "retained"
    release.sweep(world.root)
    assert world.released() == [item.dispatch]  # not asked again


def test_an_uncertain_release_never_becomes_a_close_and_stops_after_three_tries(world: World) -> None:
    item = world.worker("w1")
    world.edit(lambda d: d.update(release_answer={"state": "release_pending"}))
    warnings: list[str] = []
    for _ in range(5):
        release.sweep(world.root, warnings=warnings)
    assert world.closed() == [] and world.released() == [item.dispatch] * release.MAX_ATTEMPTS
    assert release.marks(world.root)[item.dispatch]["state"] == "gave-up" and len(warnings) == release.MAX_ATTEMPTS


def test_output_during_the_checks_stops_the_close(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = world.worker("w1")
    real = orca_cli.release_worker
    monkeypatch.setattr(orca_cli, "release_worker", lambda *a, **k: pytest.fail("released a terminal that just wrote output"))
    reads = {"count": 0}
    detail = orca_cli.terminal_detail

    def moving(handle: str) -> dict | None:
        reads["count"] += 1
        found = detail(handle)
        if found is not None and reads["count"] > 1:
            found = {**found, "lastOutputAt": found["lastOutputAt"] + 1}
        return found

    monkeypatch.setattr(orca_cli, "terminal_detail", moving)
    assert [entry.task_id for entry in release.sweep(world.root).waiting] == [item.task]
    monkeypatch.setattr(orca_cli, "release_worker", real)


def test_a_dry_run_ends_nothing_and_writes_nothing(world: World) -> None:
    item = world.worker("w1")
    assert [entry.task_id for entry in release.sweep(world.root, dry_run=True).released] == [item.task]
    assert world.closed() == [] and world.released() == [] and release.marks(world.root) == {}


def test_the_off_switch_makes_no_orca_call(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    monkeypatch.setenv(release.ENV, "off")
    monkeypatch.setattr(orca_cli, "list_tasks", lambda run=None: pytest.fail("read Orca with the switch off"))
    outcome = release.sweep(world.root)
    assert (outcome.released, outcome.waiting, outcome.kept) == ([], [], []) and not release.pending(world.root)


def test_an_older_runs_record_is_read_in_that_run(world: World) -> None:
    item = world.worker("w1", run="run_old")
    assert [entry.task_id for entry in release.sweep(world.root, "run_a").released] == [item.task]


def test_a_worker_of_another_repository_in_roots_is_covered(world: World, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "commit", "-q", "--allow-empty", "-m", "init")
    assert orca_cli.register_root(world.root, other)
    item = world.worker("other-w1", owner=other)
    assert [entry.task_id for entry in release.sweep(world.root).released] == [item.task]
    assert release.marks(other)[item.dispatch]["state"] == "released"


def test_the_pass_is_bounded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "MAX_CLOSES", 2)
    for index in range(4):
        world.worker(f"w{index}")
    outcome = release.sweep(world.root)
    assert len(outcome.released) == 2 and len(outcome.waiting) == 2
    assert len(release.sweep(world.root).released) == 2


# --- removal never ends a session that is still needed ---------------------------------------------------------------


def _merged(world: World, name: str = "w1") -> SimpleNamespace:
    item = world.worker(name)
    _commit(item.tree)
    _git(world.root, "merge", "-q", item.name)
    return item


@pytest.mark.parametrize("strict", [False, True], ids=["board", "watcher"])
@pytest.mark.parametrize("gate", ["unread report", "open question", "working", "waiting on a prompt"])
def test_a_merged_worktree_stays_while_its_worker_needs_the_session(world: World, gate: str, strict: bool) -> None:
    item = _merged(world)
    GATES[gate](world, item)
    warnings: list[str] = []
    assert cleanup.auto_clean(world.root, warnings=warnings, strict=strict) == []
    assert item.tree.exists() and world.removed() == [] and warnings


@pytest.mark.parametrize("strict", [False, True], ids=["board", "watcher"])
def test_another_agents_terminal_keeps_a_merged_worktree(world: World, strict: bool) -> None:
    item = _merged(world)
    world.edit(lambda d: d["terminals"].update(term_human={"handle": "term_human", "worktreePath": str(item.tree.resolve()), "agentIdentity": "claude", "ptyId": "pty_h"}))
    assert cleanup.auto_clean(world.root, strict=strict) == []
    assert item.tree.exists() and "term_human" not in world.closed()


def test_a_shell_running_a_program_keeps_a_merged_worktree_in_the_watcher(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _merged(world)
    world.edit(lambda d: d["terminals"].update(term_sh={"handle": "term_sh", "worktreePath": str(item.tree.resolve()), "ptyId": "pty_sh"}))
    monkeypatch.setattr(release._Processes, "tree", lambda self, pty: [(10, "zsh"), (11, "npm")])
    assert cleanup.auto_clean(world.root, strict=True) == []
    monkeypatch.setattr(release._Processes, "tree", lambda self, pty: [(10, "login"), (11, "zsh")])
    assert [outcome for _i, outcome in cleanup.auto_clean(world.root, strict=True)] == ["removed"]


def test_the_coordinators_terminal_is_never_closed_with_a_worktree(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _merged(world)
    world.edit(lambda d: d["terminals"].update(term_me={"handle": "term_me", "worktreePath": str(item.tree.resolve())}))
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "term_me")
    assert cleanup.auto_clean(world.root) == []
    closed, _warnings = orca_cli.close_worktree_terminals(str(item.tree))
    assert "term_me" not in closed


# --- bug 3: without a coordinator -------------------------------------------------------------------------------------


def _watch(world: World, **options: Any) -> list[str]:
    lines: list[str] = []
    options.setdefault("sleep", lambda seconds: None)
    assert watcher.watch(world.root, "run_a", out=lines.append, housekeep_s=watcher.HOUSEKEEP_INTERVAL_S, **options) == 0
    return lines


def test_the_watcher_cleans_and_releases_then_exits(world: World) -> None:
    merged = _merged(world, "merged")
    open_work = world.worker("unmerged")
    _commit(open_work.tree, "other.txt")
    _watch(world, scans=None)
    assert not merged.tree.exists() and open_work.tree.exists()
    assert open_work.dispatch in world.released()
    assert not (world.root / watcher.ALIVE_FILE).exists()


def test_the_watcher_never_removes_real_work(world: World) -> None:
    dirty = _merged(world, "dirty")
    (dirty.tree / "unsaved.txt").write_text("work\n", encoding="utf-8")
    unmerged = world.worker("unmerged")
    _commit(unmerged.tree, "other.txt")
    _watch(world, scans=1)
    assert dirty.tree.exists() and (dirty.tree / "unsaved.txt").exists() and unmerged.tree.exists() and world.removed() == []


def test_the_watcher_waits_for_an_acknowledgement_with_a_bound(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "LINGER_PASSES", 3)
    item = world.worker("w1")
    _unread(world, item)
    pauses: list[float] = []
    lines = _watch(world, scans=None, sleep=pauses.append)
    assert world.closed() == [] and world.released() == []
    assert sum(pauses) == 3 * watcher.HOUSEKEEP_INTERVAL_S and max(pauses) <= watcher.WAKE_POLL_S
    assert any("no unsettled worker left" in line for line in lines)


def test_the_watcher_releases_once_the_report_is_acknowledged(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = world.worker("w1")
    _unread(world, item)

    def acknowledge(_seconds: float) -> None:
        world.edit(lambda d: (d["messages"].clear(), d["acked_messages"].append(item.report)))

    _watch(world, scans=None, sleep=acknowledge)
    assert world.released() == [item.dispatch]


def test_an_unreachable_orca_removes_and_closes_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    merged = _merged(world, "merged")
    idle = world.worker("idle")
    missing = world.worker("missing")
    _git(world.root, "worktree", "remove", "--force", str(missing.tree))
    before = world.state.read_text(encoding="utf-8")
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")

    _watch(world, scans=3)

    assert world.state.read_text(encoding="utf-8") == before
    assert merged.tree.exists() and idle.tree.exists()
    assert str(missing.tree.resolve()) in orca_cli.worker_worktrees(world.root)  # the record was not marked removed
    assert release.marks(world.root) == {}


def test_a_watcher_gives_up_on_an_orca_that_stays_down(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    monkeypatch.setenv("FAKE_ORCA_FAIL", "1")
    monkeypatch.setattr(watcher, "GIVE_UP_SCANS", 2)
    monkeypatch.setattr(watcher, "FAILED_SCANS", 99)
    lines = _watch(world, scans=None)
    assert any("stopping after 2 failed scans" in line for line in lines)


def test_one_watcher_per_project(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    held = watcher._lock(world.root)
    assert held is not None
    try:
        monkeypatch.setattr(resume, "scan", lambda *a, **k: pytest.fail("a second watcher scanned"))
        lines = _watch(world)
        assert "another watcher" in lines[0]
        assert watcher._requested(world.root)["run"] == "run_a"
        monkeypatch.setenv(watcher.WATCH_ENV, "on")
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("started a second watcher"))
        assert watcher.ensure_running(world.root, "run_b") == "running"
        assert watcher._requested(world.root)["run"] == "run_b"
    finally:
        watcher._unlock(held)
    assert watcher._lock(world.root) is not None  # released when its holder closes


def test_a_running_watcher_follows_a_new_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    scanned: list[str | None] = []
    real = resume.scan

    def scan(root: Path, run: str | None = None, **options: Any) -> list:
        scanned.append(run)
        return real(root, run, **options)

    monkeypatch.setattr(resume, "scan", scan)
    monkeypatch.setattr(watcher, "housekeep", lambda *a, **k: True)
    monkeypatch.setattr(watcher, "LINGER_PASSES", 1)

    def ask(_seconds: float) -> None:
        watcher._request(world.root, "run_b")

    _watch(world, scans=None, sleep=ask)
    assert "run_b" in scanned


@pytest.mark.parametrize("switch", [cleanup.AUTO_ENV, release.ENV])
def test_each_off_switch_holds_in_the_watcher(world: World, monkeypatch: pytest.MonkeyPatch, switch: str) -> None:
    merged = _merged(world, "merged")
    open_work = world.worker("unmerged")
    _commit(open_work.tree, "other.txt")
    monkeypatch.setenv(switch, "off")
    _watch(world, scans=1)
    if switch == cleanup.AUTO_ENV:
        assert merged.tree.exists() and world.removed() == []
        assert open_work.dispatch in world.released()  # the other step still runs
    else:
        assert not merged.tree.exists()
        assert open_work.dispatch not in world.released() and world.closed() == []


def test_with_both_switches_off_the_watcher_does_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _merged(world)
    monkeypatch.setenv(cleanup.AUTO_ENV, "off")
    monkeypatch.setenv(release.ENV, "off")
    assert watcher.housekeep(world.root, "run_a") is False
    assert item.tree.exists() and world.released() == [] and world.closed() == []
    monkeypatch.setenv(watcher.WATCH_ENV, "on")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("started a watcher with both switches off"))
    assert watcher.after_session(world.root) is None


def test_the_watcher_leaves_an_active_task_alone(world: World) -> None:
    item = world.worker("w1", status="dispatched")
    _commit(item.tree)
    _git(world.root, "merge", "-q", item.name)
    _watch(world, scans=1)
    assert item.tree.exists() and world.closed() == [] and world.released() == [] and world.removed() == []


def test_an_overloaded_machine_skips_the_pass(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    monkeypatch.setattr(watcher, "_overloaded", lambda: True)
    monkeypatch.setattr(cleanup, "candidates", lambda *a, **k: pytest.fail("worked while overloaded"))
    assert watcher.housekeep(world.root, "run_a") is True


def test_the_command_runs_to_the_end_as_a_process(world: World) -> None:
    item = world.worker("w1")
    env = {**__import__("os").environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    # The machine's real load must not decide this test: an overloaded pass correctly waits five minutes.
    driver = "import sys; from coding_agent import cli, watcher; watcher._overloaded = lambda: False; sys.exit(cli.main(sys.argv[1:]))"
    done = subprocess.run([sys.executable, "-c", driver, "--root", str(world.root), "worker-watch", "--run", "run_a"],
                          env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert world.released() == [item.dispatch] and not item.tree.exists()


@pytest.mark.parametrize("dispatch", ["orca", "subagent"])
def test_session_end_starts_the_watcher_for_a_coordinator(world: World, monkeypatch: pytest.MonkeyPatch, dispatch: str) -> None:
    write_manifest(world.root, verify=[])
    path = world.root / "integration.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["coordinator"] = {"dispatch": dispatch}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    item = world.worker("w1")
    started: list[tuple[Path, str | None]] = []
    monkeypatch.setenv(watcher.WATCH_ENV, "on")
    monkeypatch.setattr(watcher, "ensure_running", lambda root, run=None, *, session="cli": started.append((root, run)) or "started")
    note = lambda **kw: None

    def end(cwd: Path) -> None:
        handlers.subagent_ledger(SimpleNamespace(payload={"hook_event_name": "SessionEnd", "session_id": "s", "cwd": str(cwd), "reason": "other"},
                                                 root=world.root, session="s", manifest=load(path), note=note))

    end(item.tree)  # a worker's session
    assert started == []
    end(world.root)
    assert started == [(world.root, "run_a")]


def test_session_end_without_a_run_follows_the_newest_record(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1", run="run_old")
    monkeypatch.delenv(orca_cli.RUN_ENV)
    monkeypatch.setenv(watcher.WATCH_ENV, "on")
    started: list[str | None] = []
    monkeypatch.setattr(watcher, "ensure_running", lambda root, run=None, *, session="cli": started.append(run) or "started")
    watcher.after_session(world.root)
    assert started == ["run_old"]


def test_the_board_does_not_restart_a_watcher_that_just_finished(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.worker("w1")
    monkeypatch.setenv(watcher.WATCH_ENV, "on")
    started: list[str | None] = []
    monkeypatch.setattr(watcher, "ensure_running", lambda root, run=None, *, session="cli": started.append(run) or "started")
    watcher._note_exit(world.root)
    assert watcher.ensure_for_cleanup(world.root) is None and started == []
    assert watcher.after_session(world.root) == "started"  # a session end is the last chance, so it is not held back


def test_the_board_shows_a_released_worker_with_unfinished_work(world: World) -> None:
    item = world.worker("w1")
    _commit(item.tree)
    release.sweep(world.root)
    lines, error = coordinator.read_board(world.root)
    text = "\n".join(lines)
    assert error is None and "worker đã đóng phiên" in text and "claude --resume" in text
