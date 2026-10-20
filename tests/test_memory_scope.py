"""Zero-Mem stores are per project: one git repository, shared by all its worktrees, never by two repositories."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from coding_agent import events
from coding_agent.memory import zeromem

SRC = Path(__file__).resolve().parents[1] / "src"


def old_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def init(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], check=True)
    return path


def test_the_main_checkout_keeps_its_old_key(repo):
    init_root = init(repo.parent / "main-repo")
    assert zeromem.store_key(init_root) == old_key(init_root)
    assert zeromem.project_root(init_root) == init_root.resolve()


def test_two_worktrees_of_one_repo_share_a_store(tmp_path):
    main = init(tmp_path / "proj")
    worktree = tmp_path / "proj-worker"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(worktree)], check=True)
    sub = main / "pkg"
    sub.mkdir()
    assert zeromem.store_key(worktree) == zeromem.store_key(main) == zeromem.store_key(sub) == old_key(main)
    assert zeromem.project_root(worktree) == main.resolve()


def test_two_repos_under_one_non_git_folder_get_different_stores(tmp_path):
    folder = tmp_path / "folder-project"
    first = init(folder / "api")
    second = init(folder / "web")
    keys = {zeromem.store_key(first), zeromem.store_key(second), zeromem.store_key(folder)}
    assert len(keys) == 3
    assert zeromem.store_key(folder) == old_key(folder)


def test_a_non_git_directory_is_keyed_by_its_path(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert zeromem.project_root(plain) == plain.resolve() and zeromem.store_key(plain) == old_key(plain)


def manifest(root: Path) -> None:
    doc = {
        "schema": 1, "verified": False, "python_src": "src", "verify": {"commands": []},
        "memory": {"ingest": "conversation", "embedder": "hash", "change_tools": ["Write", "Edit"], "transient_markers": [], "max_episode_chars": 6000},
        "loop": {"remind_at": 3, "stop_at": 6, "max_tool_calls_per_turn": 400},
        "hooks": [{"id": "stop-gate", "host": ["claude_code"], "event": "Stop", "mode": "shadow", "assumption": "a"}],
    }
    (root / "integration.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


def transcript(path: Path) -> Path:
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Explain the retry policy."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "It retries three times."}]}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def stop(project: Path, cwd: Path, tmp_path: Path, session: str) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC), "CLAUDE_PROJECT_DIR": str(project), "CODING_AGENT_ROLE": "worker",
           "CODING_AGENT_ZEROMEM_HOME": str(tmp_path / "zm")}
    payload = {"session_id": session, "cwd": str(cwd), "transcript_path": str(transcript(tmp_path / f"{session}.jsonl"))}
    done = subprocess.run([sys.executable, "-m", "coding_agent.hooks", "stop-gate"], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, check=False, timeout=120)
    assert done.returncode == 0, done.stderr


def spooled(tmp_path: Path, key: str) -> list[Path]:
    return sorted((tmp_path / "zm" / "workspaces" / key / "spool").glob("*.jsonl"))


def test_a_worker_worktree_records_into_its_repository_store(tmp_path):
    main = init(tmp_path / "proj")
    worktree = tmp_path / "proj-worker"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(worktree)], check=True)
    manifest(worktree)
    stop(worktree, worktree, tmp_path, "w1")
    assert spooled(tmp_path, old_key(main)) and not spooled(tmp_path, old_key(worktree))


def test_a_folder_root_session_never_records_a_turn_that_ran_in_a_member_repo(tmp_path):
    folder = tmp_path / "folder-project"
    member = init(folder / "api")
    folder.mkdir(exist_ok=True)
    manifest(folder)
    stop(folder, member, tmp_path, "f1")
    assert not spooled(tmp_path, old_key(folder)) and not spooled(tmp_path, old_key(member))
    kinds = [json.loads(line)["kind"] for line in events.log_path(folder).read_text(encoding="utf-8").splitlines()]
    assert "other-project" in kinds
    stop(folder, folder, tmp_path, "f2")
    assert spooled(tmp_path, old_key(folder))
