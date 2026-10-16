"""`wiki-sync`: the code→wiki anchor, ported from setup's wiki-sync-test.sh, wiki-sync-subdir-test.sh
and wiki-sync-flags-failopen-test.sh. Every case runs in a temporary git repository."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from coding_agent import cli

CORE_PAGE = "---\ntype: concept\n---\n# core\nXem `src/app.py`.\n"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.hooksPath=/dev/null",
                    "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True)


def commit(cwd: Path, message: str) -> None:
    git(cwd, "add", "-A")
    git(cwd, "commit", "--no-verify", "-qm", message)


def write(path: Path, text: str, *, later: bool = False) -> None:
    """Write a file; `later` moves its mtime forward so tree mode sees a same-size rewrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = path.stat().st_mtime_ns if path.exists() else None
    path.write_text(text, encoding="utf-8")
    if later and stamp is not None:
        os.utime(path, ns=(stamp + 5_000_000_000, stamp + 5_000_000_000))


def init(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    return root


def mkproj(root: Path) -> Path:
    write(root / "llmwiki" / "wiki" / "concepts" / "core.md", CORE_PAGE)
    write(root / "src" / "app.py", "v1\n")
    return root


@pytest.fixture
def sync(monkeypatch, capsys):
    """Run `cli wiki-sync ARGS` from a directory; return (exit code, stdout)."""
    def run(cwd: Path, *args: str) -> tuple[int, str]:
        monkeypatch.chdir(cwd)
        capsys.readouterr()
        code = cli.main(["wiki-sync", *args])
        return code, capsys.readouterr().out
    return run


def anchor(wiki: Path) -> dict:
    return json.loads((wiki / ".last-sync.json").read_text(encoding="utf-8"))


def stale(wiki: Path) -> dict:
    return json.loads((wiki / "stale.json").read_text(encoding="utf-8"))


# ── wiki-sync-test.sh: a project at the repository root ──

def test_anchor_lifecycle_at_repo_root(tmp_path, sync):
    root = init(tmp_path / "repo")
    wiki = root / "llmwiki" / "wiki"
    write(wiki / "concepts" / "app-core.md", "---\ntype: concept\n---\n# app-core\nXử lý chính nằm ở `src/app.py`.\n")
    write(root / "src" / "app.py", "print('v1')\n")
    commit(root, "init")

    assert sync(root, "--check")[0] == 2, "no anchor yet"
    assert sync(root, "--mark-synced")[0] == 0
    assert (wiki / ".last-sync.json").is_file()
    assert "gitHead" in anchor(wiki) and "mode" not in anchor(wiki), "repo-root anchor keeps the old format"
    assert sync(root, "--check")[0] == 0, "nothing changed: no-op"

    write(wiki / "concepts" / "app-core.md", (wiki / "concepts" / "app-core.md").read_text(encoding="utf-8") + "- ghi chú\n")
    commit(root, "docs: wiki only")
    assert sync(root, "--check")[0] == 0, "a wiki-only change is not code drift"

    write(root / "src" / "app.py", "print('v2')\n")
    commit(root, "feat: change app")
    code, out = sync(root, "--check", "--json")
    report = json.loads(out)
    assert code == 3 and report["status"] == "drift" and "src/app.py" in report["changed"]
    assert "concepts/app-core.md" in report["suspects"]
    assert stale(wiki)["concepts/app-core.md"]["action"] == "code-drift"

    assert sync(root, "--mark-synced")[0] == 0
    assert not any(v.get("action") == "code-drift" for v in stale(wiki).values()), "mark-synced clears code-drift flags"
    assert sync(root, "--check")[0] == 0


def test_archive_pages_and_ambiguous_basenames_raise_no_flag(tmp_path, sync):
    root = init(tmp_path / "repo")
    wiki = root / "llmwiki" / "wiki"
    write(wiki / "concepts" / "app-core.md", "# app-core\nXem `src/app.py`.\n")
    write(wiki / "sources" / "draft" / "archive" / "old-plan.md", "# old-plan\nKế hoạch cũ, có nhắc `src/app.py`.\n")
    write(wiki / "concepts" / "prose.md", '# prose\nTrang này nói "we serve users".\n')
    for i in range(1, 10):
        write(wiki / "concepts" / f"common{i}.md", f"# common{i}\nTham chiếu SHARED.md trong văn xuôi.\n")
    write(root / "src" / "app.py", "v1\n")
    write(root / "bin" / "serve", "#!/bin/sh\n")
    write(root / "pkg-a" / "SHARED.md", "x\n")
    commit(root, "init")
    assert sync(root, "--mark-synced")[0] == 0

    write(root / "src" / "app.py", "v3\n")
    write(root / "bin" / "serve", "#!/bin/sh -e\n")
    write(root / "pkg-a" / "SHARED.md", "y\n")
    commit(root, "feat: đổi cả ba")
    code, out = sync(root, "--check", "--json")
    suspects = json.loads(out)["suspects"]
    assert code == 3
    assert not any(page.startswith("sources/draft/archive/") for page in suspects), "archive pages are history, never flagged"
    assert "concepts/prose.md" not in suspects, "a basename without an extension is not a needle"
    assert not any(page.startswith("concepts/common") for page in suspects), "a basename over the page cap is not a needle"
    assert "concepts/app-core.md" in suspects, "a full path still flags"


# ── wiki-sync-subdir-test.sh: the project is a subdirectory of a parent repository ──

@pytest.fixture
def parent(tmp_path):
    root = init(tmp_path / "parent")
    write(root / "top.txt", "x\n")
    mkproj(root / "tracked")
    mkproj(root / "loose")
    write(root / ".gitignore", "loose/\n")
    commit(root, "init")
    return root


def test_tracked_subdirectory_uses_git_mode_scoped_to_the_project(parent, sync):
    tracked = parent / "tracked"
    assert sync(tracked, "--mark-synced", "--root", ".")[0] == 0, "regression: used to crash on the parent's .stale.lock"
    assert (tracked / "llmwiki" / "wiki" / ".last-sync.json").is_file() and not (parent / "llmwiki").exists()
    assert anchor(tracked / "llmwiki" / "wiki")["subpath"] == "tracked"
    assert sync(tracked, "--check", "--root", ".")[0] == 0

    write(parent / "top.txt", "x\ny\n")
    git(parent, "commit", "-qam", "đổi ngoài dự án")
    assert sync(tracked, "--check", "--root", ".")[0] == 0, "a change outside the project is not drift"

    write(tracked / "src" / "app.py", "v2\n")
    code, out = sync(tracked, "--check", "--root", ".", "--json")
    assert code == 3 and "src/app.py" in json.loads(out)["changed"], "paths are relative to the project"
    assert sync(tracked / "src", "--check")[0] == 3, "from a deeper directory the project is found walking up"


def test_ignored_subdirectory_uses_tree_mode(parent, sync):
    loose = parent / "loose"
    wiki = loose / "llmwiki" / "wiki"
    assert sync(parent, "--mark-synced", "--root", "loose")[0] == 0
    assert anchor(wiki)["mode"] == "tree"
    sidecar = json.loads((wiki / ".last-sync-tree.json").read_text(encoding="utf-8"))
    assert "src/app.py" in sidecar and anchor(wiki)["treeFiles"] == len(sidecar)
    assert sync(parent, "--check", "--root", "loose")[0] == 0
    assert sync(parent, "--mark-synced", "--root", "loose")[0] == 0, "marking again is idempotent"

    write(wiki / "concepts" / "core.md", CORE_PAGE + "wiki edit\n")
    assert sync(parent, "--check", "--root", "loose")[0] == 0, "a wiki edit is not code drift"
    write(loose / "src" / "app.py", "v2\n", later=True)
    assert sync(parent, "--check", "--root", "loose")[0] == 3
    assert "code-drift" in (wiki / "stale.json").read_text(encoding="utf-8")
    assert sync(parent, "--mark-synced", "--root", "loose")[0] == 0
    assert sync(parent, "--check", "--root", "loose")[0] == 0
    assert "code-drift" not in (wiki / "stale.json").read_text(encoding="utf-8")

    init(loose / "llmwiki")
    assert sync(loose / "llmwiki", "--check")[0] == 0, "inside a nested llmwiki repository the parent project's anchor is used"


def test_tree_mode_ignores_runtime_build_and_gitignored_files(parent, sync):
    loose = parent / "loose"
    audit = loose / "llmwiki" / ".claude" / "audit" / "2026-10-12.jsonl"
    write(audit, '{"e":1}\n')
    write(loose / ".gitignore", "*.tmp\n")
    init(loose / "svc")
    write(loose / "svc" / ".gitignore", "gen/\n")
    write(loose / "svc" / "src" / "m.py", "s1\n")
    assert sync(parent, "--mark-synced", "--root", "loose")[0] == 0

    write(audit, '{"e":1}\n{"e":2}\n', later=True)
    assert sync(parent, "--check", "--root", "loose")[0] == 0, "a session audit log is not drift"
    write(loose / ".claude" / "session.jsonl", "x\n")
    for rel in ("node_modules/p/i.js", "dist-web/a.js", "build/o", "svc/target/c", "src/__pycache__/a.pyc",
                "svc/gen/x.js", ".coding-agent/s.json", "scratch.tmp"):
        write(loose / rel, "")
    assert sync(parent, "--check", "--root", "loose")[0] == 0, "build, vendor, runtime and .gitignore'd files (nested repo too)"

    write(loose / "svc" / "src" / "m.py", "s2\n", later=True)
    code, out = sync(parent, "--check", "--root", "loose", "--json")
    assert code == 3 and json.loads(out)["changed"] == ["svc/src/m.py"]


def test_untracked_project_applies_the_parent_gitignore(parent, sync):
    untr = mkproj(parent / "untr")
    write(parent / ".gitignore", "loose/\n*.log\n")
    assert sync(parent, "--mark-synced", "--root", "untr")[0] == 0
    assert anchor(untr / "llmwiki" / "wiki")["mode"] == "tree"
    write(untr / "src" / "run.log", "")
    write(untr / "llmwiki" / ".claude" / "audit" / "a.jsonl", "a\n")
    assert sync(parent, "--check", "--root", "untr")[0] == 0
    write(untr / "src" / "app.py", "v2\n", later=True)
    assert sync(parent, "--check", "--root", "untr")[0] == 3


# ── wiki-sync-flags-failopen-test.sh: the read path (/query) sees flags and never breaks ──

@pytest.fixture
def flags(tmp_path, sync):
    wiki = tmp_path / "wiki"
    write(wiki / "concepts" / "alpha.md", "noop\n")

    def run(pages: str, wiki_dir: Path = wiki) -> tuple[int, str]:
        return sync(tmp_path, "--wiki-dir", str(wiki_dir), "--flags-for", pages)
    return wiki, run


def test_flagged_page_warns_on_the_read_path(flags):
    wiki, run = flags
    write(wiki / "stale.json", json.dumps({"concepts/alpha.md": {"by": "harness/scripts/foo.py", "action": "code-drift",
                                                                 "ts": "2026-07-19T10:00:00", "session": None}}))
    for pages in ("alpha", "concepts/alpha.md"):
        code, out = run(pages)
        assert code == 0 and "CẢNH BÁO DRIFT" in out and "foo.py" in out
    assert run("beta") == (0, ""), "a clean page stays silent"


@pytest.mark.parametrize("content", ["{ this is not json", '["mảng chứ không phải object"]', None])
def test_broken_stale_json_never_breaks_the_read_path(flags, content):
    wiki, run = flags
    if content is not None:
        write(wiki / "stale.json", content)
    assert run("alpha") == (0, "")


def test_missing_wiki_or_empty_page_list_exits_zero(flags, tmp_path):
    _wiki, run = flags
    assert run("alpha", tmp_path / "khong-ton-tai")[0] == 0
    assert run("")[0] == 0


def test_flags_for_outside_any_project_exits_zero(tmp_path, sync):
    assert sync(tmp_path, "--flags-for", "alpha")[0] == 0
