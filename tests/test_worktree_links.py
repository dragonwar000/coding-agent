"""A worker worktree gets the coordinator's untracked install, so the worker's session runs the same hooks.

The install (`harness/`, `integration.yaml`, `.claude/settings.json`, `.codex/hooks.json`) is untracked in the
coordinator's checkout, and a worktree created from a commit lacks it. `worktree_links` links it in before the agent
starts, hides it from git through the shared `info/exclude`, and removes only the links when the worktree goes.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent import cleanup, cli, coordinator, gate, gen, orca_cli, worktree_links
from coding_agent.manifest import load

SHIPPED = Path(__file__).resolve().parents[1] / "integration.yaml"
FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_orca.py"


def git(cwd: Path, *args: str) -> str:
    run = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True)
    return run.stdout


def install_into(checkout: Path) -> None:
    """An untracked install the way `install.sh --vendor claude` leaves one: package, manifest, host files, record."""
    package = checkout / "harness" / "coding-agent" / "src" / "coding_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# parent copy\n", encoding="utf-8")
    text = SHIPPED.read_text(encoding="utf-8").replace("python_src: src", "python_src: harness/coding-agent/src", 1)
    (checkout / "integration.yaml").write_text(text, encoding="utf-8")
    gen.write(load(checkout / "integration.yaml"), apply=True)
    (checkout / ".claude" / "settings.json.bak").write_text("{}\n", encoding="utf-8")
    (checkout / ".coding-agent").mkdir(exist_ok=True)
    (checkout / ".coding-agent" / "installed.json").write_text(json.dumps({"hosts": ["claude_code", "codex"]}), encoding="utf-8")


@pytest.fixture
def parent(repo: Path) -> Path:
    """The coordinator's checkout: one commit, and an install nobody committed."""
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    install_into(repo)
    return repo


def add_worktree(repo: Path, tmp_path: Path, name: str = "wt") -> Path:
    path = tmp_path / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    return path


def snapshot(root: Path) -> dict[str, str]:
    """Every file of the install under `root`, with its content, to compare before and after."""
    out = {}
    for rel in (*worktree_links.LINKED, ".claude/settings.json.bak"):
        path = root / rel
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else []
        for file in files:
            out[file.relative_to(root).as_posix()] = file.read_text(encoding="utf-8")
    return out


EXCLUDE_LINES = ["/harness", "/integration.yaml", "/.claude/settings.json", "/.codex/hooks.json", "/.coding-agent/installed.json",
                 "/.claude/settings.json.bak", "/.coding-agent/worktree-links.json"]


# --- linking ------------------------------------------------------------------------------------------------------


def test_every_untracked_install_path_is_linked_into_the_worktree(parent, tmp_path):
    wt = add_worktree(parent, tmp_path)
    results = worktree_links.link_install(parent, wt)
    assert {item["path"]: item["action"] for item in results} == {rel: "symlink" for rel in worktree_links.LINKED}
    for rel in worktree_links.LINKED:
        assert (wt / rel).is_symlink() and (wt / rel).resolve() == (parent / rel).resolve()
    assert (wt / "harness" / "coding-agent" / "src" / "coding_agent" / "__init__.py").read_text(encoding="utf-8") == "# parent copy\n"
    record = json.loads((wt / worktree_links.RECORD).read_text(encoding="utf-8"))
    assert [item["path"] for item in record["links"]] == list(worktree_links.LINKED)
    assert worktree_links.linked_paths(wt) == list(worktree_links.LINKED)


def test_an_existing_file_in_the_worktree_is_not_overwritten(parent, tmp_path):
    wt = add_worktree(parent, tmp_path)
    (wt / ".claude").mkdir()
    (wt / ".claude" / "settings.json").write_text('{"own": true}\n', encoding="utf-8")
    (wt / "harness").mkdir()
    results = {item["path"]: item["action"] for item in worktree_links.link_install(parent, wt)}
    assert results[".claude/settings.json"] == "exists" and results["harness"] == "exists"
    assert (wt / ".claude" / "settings.json").read_text(encoding="utf-8") == '{"own": true}\n'
    assert not (wt / ".claude" / "settings.json").is_symlink() and not any((wt / "harness").iterdir())
    assert results["integration.yaml"] == "symlink"


def test_a_tracked_path_is_never_linked(parent, tmp_path):
    # The repository commits its own manifest; a worker that deleted it must not get the coordinator's back.
    git(parent, "add", "-f", "integration.yaml")
    git(parent, "commit", "-q", "-m", "track the manifest")
    wt = add_worktree(parent, tmp_path)
    assert (wt / "integration.yaml").is_file() and not (wt / "integration.yaml").is_symlink()
    (wt / "integration.yaml").unlink()
    results = {item["path"]: item["action"] for item in worktree_links.link_install(parent, wt)}
    assert results["integration.yaml"] == "tracked" and not os.path.lexists(wt / "integration.yaml")


def test_a_checkout_without_an_install_links_nothing(repo, tmp_path):
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wt = add_worktree(repo, tmp_path)
    assert worktree_links.prepare_worker(wt, repo) == {"source": "", "links": [], "excluded": []}
    assert not (wt / worktree_links.RECORD).exists()


def test_the_coordinator_checkout_is_preferred_over_the_main_repository(parent, tmp_path):
    # The coordinator works in a linked worktree (like an Orca workspace) that holds the install; the main checkout does not.
    coordinator_wt = add_worktree(parent, tmp_path, "coordinator")
    install_into(coordinator_wt)
    shutil.rmtree(parent / "harness")
    (parent / "integration.yaml").unlink()
    wt = add_worktree(parent, tmp_path, "worker")
    prepared = worktree_links.prepare_worker(wt, coordinator_wt, parent)
    assert prepared["source"] == str(coordinator_wt)
    assert (wt / "integration.yaml").resolve() == (coordinator_wt / "integration.yaml").resolve()


# --- git exclusion ------------------------------------------------------------------------------------------------


def test_install_paths_go_to_the_shared_info_exclude_and_git_add_all_commits_none(parent, tmp_path):
    wt = add_worktree(parent, tmp_path)
    prepared = worktree_links.prepare_worker(wt, parent)
    assert prepared["excluded"] == EXCLUDE_LINES
    common = Path(git(wt, "rev-parse", "--git-common-dir").strip())
    exclude = (common if common.is_absolute() else wt / common) / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert worktree_links.EXCLUDE_MARK in lines and all(line in lines for line in EXCLUDE_LINES)
    assert git(wt, "status", "--porcelain", "--untracked-files=all") == ""
    git(wt, "add", "-A")
    assert git(wt, "diff", "--cached", "--name-only") == ""
    # the coordinator's checkout reads the same file, so its own install stops showing as untracked too
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == ""


def test_exclusion_is_added_once_and_skips_tracked_paths(parent, tmp_path):
    git(parent, "add", "-f", "integration.yaml")
    git(parent, "commit", "-q", "-m", "track the manifest")
    wt = add_worktree(parent, tmp_path)
    first = worktree_links.exclude_install(wt)
    assert "/integration.yaml" not in first and "/harness" in first
    assert worktree_links.exclude_install(wt) == []
    exclude = Path(git(parent, "rev-parse", "--absolute-git-dir").strip()) / "info" / "exclude"
    assert exclude.read_text(encoding="utf-8").count(worktree_links.EXCLUDE_MARK) == 1


# --- cleanup ------------------------------------------------------------------------------------------------------


def test_unlink_removes_only_the_links_and_leaves_the_parent_install_intact(parent, tmp_path):
    before = snapshot(parent)
    wt = add_worktree(parent, tmp_path)
    worktree_links.prepare_worker(wt, parent)
    removed = worktree_links.unlink_install(wt)
    assert removed == list(worktree_links.LINKED)
    assert not any(os.path.lexists(wt / rel) for rel in worktree_links.LINKED)
    assert not (wt / worktree_links.RECORD).exists() and not (wt / ".claude").exists() and not (wt / ".codex").exists()
    assert snapshot(parent) == before


def test_unlink_leaves_a_path_that_is_no_longer_a_link(parent, tmp_path):
    wt = add_worktree(parent, tmp_path)
    worktree_links.link_install(parent, wt)
    (wt / "integration.yaml").unlink()
    (wt / "integration.yaml").write_text("worker's own\n", encoding="utf-8")
    assert "integration.yaml" not in worktree_links.unlink_install(wt)
    assert (wt / "integration.yaml").read_text(encoding="utf-8") == "worker's own\n"


@pytest.fixture
def fake_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "orca-state.json"
    monkeypatch.setenv("CODING_AGENT_ORCA", str(FAKE))
    monkeypatch.setenv("FAKE_ORCA_STATE", str(state))
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "run_test")
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    monkeypatch.delenv("FAKE_ORCA_SCREEN", raising=False)
    monkeypatch.setattr(orca_cli, "AGENT_READY_POLL_S", 0)
    return state


def test_worktree_cleanup_removes_the_links_first_and_the_parent_install_survives(parent, tmp_path, fake_orca, monkeypatch):
    before = snapshot(parent)
    task_id = orca_cli.create_task(parent, project="demo-repo", t_id="T-clean", spec="x", title="clean")
    wt = add_worktree(parent, tmp_path)
    orca_cli.record_worker(parent, worktree=str(wt), task_id=task_id, dispatch="ctx_clean")
    orca_cli.link_worker_install(parent, wt, parent, task_id=task_id)
    orca_cli.set_status(parent, task_id=task_id, requested="completed", basis="predicate")
    seen_links: list[bool] = []

    def remove_for_real(path: str, *, force: bool = False) -> None:
        seen_links.append(any(os.path.lexists(Path(path) / rel) for rel in worktree_links.LINKED))
        subprocess.run(["git", "-C", str(parent), "worktree", "remove", "--force", path], check=True, capture_output=True)

    monkeypatch.setattr(orca_cli, "remove_worktree", remove_for_real)
    found = cleanup.candidates(parent)
    assert [(item.task_id, item.safe) for item in found] == [(task_id, True)]
    assert [outcome for _item, outcome in cleanup.clean(parent, found)] == ["removed"]
    assert seen_links == [False] and not wt.exists()
    assert snapshot(parent) == before


def test_links_never_make_a_worktree_dirty_even_without_the_exclude(parent, tmp_path):
    wt = add_worktree(parent, tmp_path)
    worktree_links.link_install(parent, wt)  # no exclude_install: as if info/exclude could not be written
    assert cleanup._dirty(wt) == 0
    (wt / "work.py").write_text("x = 1\n", encoding="utf-8")
    assert cleanup._dirty(wt) == 1


# --- Windows without the symlink privilege ------------------------------------------------------------------------


def test_without_symlinks_files_are_copied_and_cleanup_deletes_only_the_copies(parent, tmp_path, monkeypatch):
    before = snapshot(parent)
    wt = add_worktree(parent, tmp_path)

    def refuse(*_args, **_kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(worktree_links.os, "symlink", refuse)
    monkeypatch.setattr(worktree_links.sys, "platform", "win32")
    results = {item["path"]: item["action"] for item in worktree_links.link_install(parent, wt)}
    assert results["integration.yaml"] == "copy" and results[".claude/settings.json"] == "copy"
    assert results["harness"] in ("junction", "copy")
    assert (wt / "integration.yaml").read_text(encoding="utf-8") == (parent / "integration.yaml").read_text(encoding="utf-8")
    monkeypatch.setattr(worktree_links.sys, "platform", sys.platform)
    assert set(worktree_links.unlink_install(wt)) == set(worktree_links.LINKED)
    assert not any(os.path.lexists(wt / rel) for rel in worktree_links.LINKED)
    assert snapshot(parent) == before


# --- the hooks in the worktree ------------------------------------------------------------------------------------


def test_the_gate_inside_the_worktree_checks_the_worktree_files(parent, tmp_path, monkeypatch, capsys):
    wt = add_worktree(parent, tmp_path)
    worktree_links.prepare_worker(wt, parent)
    monkeypatch.chdir(wt)
    assert gate.main(["--manifest", "integration.yaml"]) == 0
    assert "match integration.yaml" in capsys.readouterr().out
    # without the worktree's own settings link, the gate fails: it read the worktree, not the coordinator's checkout
    (wt / ".claude" / "settings.json").unlink()
    assert gate.main(["--manifest", "integration.yaml"]) == 1
    assert (parent / ".claude" / "settings.json").is_file()


def test_a_linked_worker_worktree_is_a_worker_and_writes_there(parent, tmp_path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ROLE", raising=False)
    wt = add_worktree(parent, tmp_path)
    worktree_links.prepare_worker(wt, parent)
    assert coordinator.role_for(wt) == "coordinator"  # linked but not recorded and no role in the environment
    monkeypatch.setenv("CODING_AGENT_ROLE", "worker")
    assert coordinator.role_for(wt) == "worker"
    monkeypatch.delenv("CODING_AGENT_ROLE")
    orca_cli.record_worker(parent, worktree=str(wt), task_id="t", dispatch="d")
    assert coordinator.role_for(wt) == "worker"


# --- dispatch paths -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_worker_start_links_the_install_before_the_agent_starts(parent, tmp_path, fake_orca, monkeypatch, agent):
    monkeypatch.setenv("FAKE_ORCA_REAL_WORKTREE", "1")
    calls: list[tuple[str, ...]] = []
    linked_at_start: list[bool] = []
    real = orca_cli._call_tool

    def watch(*args: str):
        calls.append(args)
        if args[:2] == ("terminal", "create"):
            path = Path(args[args.index("--worktree") + 1][5:])
            linked_at_start.append(all((path / rel).is_symlink() for rel in worktree_links.LINKED))
        return real(*args)

    monkeypatch.setattr(orca_cli, "_call_tool", watch)
    task_id = orca_cli.create_task(parent, project="demo-repo", t_id="T-start", spec="x", title="Fix it")
    orca_cli.worker_start(parent, task_id=task_id, agent=agent, title="Fix it")
    state = json.loads(fake_orca.read_text(encoding="utf-8"))
    assert state["created_worktrees"][0]["agent"] is None  # every agent takes the separate path when there is an install
    assert linked_at_start == [True]
    assert state["created_terminals"][0]["command"] == orca_cli.agent_command(agent)
    wt = Path(orca_cli.worker_records(parent)[0]["worktree"])
    assert git(wt, "status", "--porcelain", "--untracked-files=all") == ""
    events = [json.loads(line) for line in (parent / ".coding-agent" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in events if e["guard"] == "worker-install"] == ["linked"]


def test_agent_command_marks_the_worker_role_except_on_windows():
    assert orca_cli.agent_command("claude", windows=False) == "CODING_AGENT_ROLE=worker claude"
    assert orca_cli.agent_command("claude", "acceptEdits", windows=False) == "CODING_AGENT_ROLE=worker claude --permission-mode acceptEdits"
    assert orca_cli.agent_command("claude", "acceptEdits", windows=True) == "claude --permission-mode acceptEdits"


def test_worker_adopt_links_the_install_into_an_outside_worktree(parent, tmp_path, capsys):
    wt = add_worktree(parent, tmp_path)
    assert cli.main(["--root", str(parent), "worker-adopt", "--worktree", str(wt)]) == 0
    out = capsys.readouterr().out
    assert "worker-adopt: linked harness, integration.yaml" in out
    assert (wt / ".claude" / "settings.json").is_symlink() and coordinator.role_for(wt) == "worker"


def test_a_worker_gets_a_coordinator_install_whose_package_is_a_live_link(parent, tmp_path):
    # The coordinator installed with `--link`: its package is a symlink to a live checkout, not a copy.
    live = tmp_path / "live" / "src" / "coding_agent"
    live.mkdir(parents=True)
    (live / "__init__.py").write_text("# live checkout\n", encoding="utf-8")
    package = parent / "harness" / "coding-agent" / "src" / "coding_agent"
    shutil.rmtree(package)
    os.symlink(live, package, target_is_directory=True)
    wt = add_worktree(parent, tmp_path)
    results = {item["path"]: item["action"] for item in worktree_links.link_install(parent, wt)}
    assert results["harness"] == "symlink"
    worker_package = wt / "harness" / "coding-agent" / "src" / "coding_agent"
    assert worker_package.resolve() == live.resolve()
    assert (worker_package / "__init__.py").read_text(encoding="utf-8") == "# live checkout\n"
    (live / "probe.py").write_text("edited\n", encoding="utf-8")
    assert (worker_package / "probe.py").read_text(encoding="utf-8") == "edited\n"
    worktree_links.unlink_install(wt)
    assert not os.path.lexists(wt / "harness")
    assert package.is_symlink() and sorted(p.name for p in live.iterdir()) == ["__init__.py", "probe.py"]


def test_a_worker_linked_straight_to_a_package_symlink_reaches_the_live_checkout(tmp_path):
    # When the linked path is itself the package symlink, the worker's link points at its resolved target.
    live = tmp_path / "live" / "coding_agent"
    live.mkdir(parents=True)
    (live / "__init__.py").write_text("# live\n", encoding="utf-8")
    via = tmp_path / "coordinator_package"
    os.symlink(live, via, target_is_directory=True)
    worker = tmp_path / "worker_package"
    assert worktree_links._make_link(via.resolve(), worker) == "symlink"
    assert worker.resolve() == live.resolve() and (worker / "__init__.py").is_file()
    worktree_links.remove_link(worker)
    assert not os.path.lexists(worker) and (live / "__init__.py").is_file() and via.is_symlink()
