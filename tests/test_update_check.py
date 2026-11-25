"""The session-start update reminder: the install records its source, upstream is read at most once per interval,
every failure is silent, and the reminder names both commits and the command to update by hand. No network: every
read of upstream is a fake, and the git repositories are local."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coding_agent import events, hooks, install, manifest, project_install, update_check
from coding_agent.hooks import handlers
from coding_agent.manifest import ManifestError, UpdateCheck

ROOT = Path(__file__).resolve().parents[1]
OLD = "a" * 40
NEW = "b" * 40
REPO = "https://github.com/dragonwar000/coding-agent.git"
HOUR = 3600.0


class Remote:
    """A fake `git ls-remote`: answers `sha` (None = failure) and counts the reads."""

    def __init__(self, sha: str | None):
        self.sha = sha
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, repo: str, ref: str, timeout: float) -> str | None:
        self.calls.append((repo, ref, timeout))
        return self.sha


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def checkout(tmp_path: Path, origin: str | None = REPO) -> Path:
    """A coding-agent checkout: a git repository with `src/coding_agent` and one commit."""
    root = tmp_path / "checkout"
    (root / "src" / "coding_agent").mkdir(parents=True)
    (root / "src" / "coding_agent" / "__init__.py").write_text("", encoding="utf-8")
    (root / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    git(tmp_path, "init", "-q", str(root))
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    if origin:
        git(root, "remote", "add", "origin", origin)
    return root


def project(tmp_path: Path, source: dict | None = None, **installed) -> Path:
    root = tmp_path / "project"
    (root / ".coding-agent").mkdir(parents=True)
    record = {"hosts": ["claude_code"], "python_src": project_install.DEFAULT_PYTHON_SRC, **installed}
    if source is not None:
        record["source"] = source
    (root / ".coding-agent" / "installed.json").write_text(json.dumps(record), encoding="utf-8")
    return root


def bootstrap_source(commit: str = OLD) -> dict:
    return {"via": "bootstrap", "repo": "https://github.com/dragonwar000/coding-agent", "ref": commit, "commit": commit}


# --- recording the source at install time -------------------------------------------------------------------------


def test_a_checkout_install_records_origin_head_and_a_clean_tree(tmp_path: Path):
    root = checkout(tmp_path)
    source = update_check.record_source(root / "src" / "coding_agent", env={})
    assert source == {"via": "checkout", "repo": REPO, "commit": git(root, "rev-parse", "HEAD"), "dirty": False, "checkout": str(root.resolve())}


def test_a_checkout_with_uncommitted_changes_is_recorded_as_dirty(tmp_path: Path):
    root = checkout(tmp_path)
    (root / "src" / "coding_agent" / "__init__.py").write_text("# edited\n", encoding="utf-8")
    assert update_check.record_source(root / "src" / "coding_agent", env={})["dirty"] is True


def test_a_checkout_without_origin_or_a_package_inside_another_repository_has_no_source(tmp_path: Path):
    assert update_check.record_source(checkout(tmp_path, origin=None) / "src" / "coding_agent", env={}) is None
    other = tmp_path / "other"
    (other / "harness" / "coding-agent" / "src" / "coding_agent").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(other))
    assert update_check.record_source(other / "harness" / "coding-agent" / "src" / "coding_agent", env={}) is None
    assert update_check.record_source(tmp_path / "nowhere" / "src" / "coding_agent", env={}) is None


def test_bootstrap_passes_the_repository_and_the_commit_it_downloaded(tmp_path: Path):
    remote = Remote(NEW)
    env = {update_check.SOURCE_REPO_ENV: "https://github.com/dragonwar000/coding-agent", update_check.SOURCE_REF_ENV: OLD.upper()}
    assert update_check.record_source(tmp_path, env=env, remote=remote) == bootstrap_source(OLD) | {"ref": OLD.upper()}
    assert remote.calls == []


def test_a_bootstrap_ref_that_is_not_a_commit_is_resolved_once_and_may_stay_unknown(tmp_path: Path):
    env = {update_check.SOURCE_REPO_ENV: "https://github.com/o/r", update_check.SOURCE_REF_ENV: "main"}
    remote = Remote(NEW)
    assert update_check.record_source(tmp_path, env=env, remote=remote)["commit"] == NEW
    assert remote.calls == [("https://github.com/o/r", "main", update_check.RESOLVE_TIMEOUT_S)]
    unresolved = update_check.record_source(tmp_path, env=env, remote=Remote(None))
    assert unresolved["commit"] is None
    assert "commit unresolved" in update_check.source_line(unresolved)


def test_ls_remote_output_is_read_for_the_exact_ref_the_branch_or_the_peeled_tag():
    text = f"{OLD}\trefs/heads/main\n{NEW}\trefs/tags/v1^{{}}\n{'c' * 40}\trefs/tags/v1\n"
    assert update_check.parse_ls_remote(text, "refs/heads/main") == OLD
    assert update_check.parse_ls_remote(text, "main") == OLD
    assert update_check.parse_ls_remote(text, "v1") == NEW
    assert update_check.parse_ls_remote(text, "other") is None
    assert update_check.parse_ls_remote("fatal: not a sha\n", "main") is None


def test_the_installer_records_the_source_outside_the_package_so_ownership_still_holds(tmp_path: Path, monkeypatch):
    target = tmp_path / "demo"
    target.mkdir()
    git(tmp_path, "init", "-q", str(target))
    monkeypatch.setenv(update_check.SOURCE_REPO_ENV, "https://github.com/dragonwar000/coding-agent")
    monkeypatch.setenv(update_check.SOURCE_REF_ENV, OLD)
    report = project_install.install(target, run_verify=False, ci=False)
    record = json.loads((target / ".coding-agent" / "installed.json").read_text(encoding="utf-8"))
    assert record["source"] == bootstrap_source(OLD)
    assert f"source: https://github.com/dragonwar000/coding-agent at {OLD[:7]}; session start reminds when its main moves" in report
    package = target / project_install.DEFAULT_PYTHON_SRC / "coding_agent"
    assert install.is_owned_intact(package)
    assert not any(path.name.startswith(".coding-agent-source") for path in package.iterdir())
    # A second install over the first still owns and replaces the copy.
    project_install.install(target, run_verify=False, ci=False)
    assert install.is_owned_intact(package)


def test_a_linked_install_records_no_source(tmp_path: Path):
    target = tmp_path / "demo"
    target.mkdir()
    git(tmp_path, "init", "-q", str(target))
    project_install.install(target, run_verify=False, ci=False, link=ROOT / "src")
    record = json.loads((target / ".coding-agent" / "installed.json").read_text(encoding="utf-8"))
    assert "link" in record and "source" not in record


# --- the check at session start -----------------------------------------------------------------------------------


def test_an_install_without_a_source_record_or_a_link_is_silent_and_reads_nothing(tmp_path: Path):
    remote = Remote(NEW)
    assert update_check.check(tmp_path, UpdateCheck(), remote=remote, env={}).kind == "no-source"
    assert update_check.check(project(tmp_path), UpdateCheck(), remote=remote, env={}).kind == "no-source"
    unresolved = project(tmp_path / "u", bootstrap_source() | {"commit": None})
    assert update_check.check(unresolved, UpdateCheck(), remote=remote, env={}).kind == "no-source"
    linked = project(tmp_path / "l", bootstrap_source(), link="/somewhere/src/coding_agent")
    assert update_check.check(linked, UpdateCheck(), remote=remote, env={}).kind == "linked"
    assert remote.calls == []


def test_the_same_commit_upstream_is_silent(tmp_path: Path):
    result = update_check.check(project(tmp_path, bootstrap_source(OLD)), UpdateCheck(), remote=Remote(OLD), env={}, now=1000.0)
    assert result.kind == "up-to-date" and result.message is None and result.detail["fetched"] is True


def test_a_new_upstream_commit_gives_one_line_with_both_shas_and_the_pinned_bootstrap_command(tmp_path: Path):
    remote = Remote(NEW)
    result = update_check.check(project(tmp_path, bootstrap_source(OLD)), UpdateCheck(timeout_s=3), remote=remote, env={}, now=1000.0, platform="darwin")
    assert result.kind == "update-available"
    assert remote.calls == [("https://github.com/dragonwar000/coding-agent", "refs/heads/main", 3)]
    assert "\n" not in result.message
    assert f"đang cài {OLD[:7]}" in result.message and f"upstream {NEW[:7]}" in result.message
    assert (f"curl -fsSL https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.sh | CODING_AGENT_REF={NEW} "
            "bash -s -- --add-new-hooks") in result.message
    assert "không tự chạy" in result.message


def test_the_cache_answers_within_the_interval_and_the_reminder_is_not_repeated(tmp_path: Path):
    root = project(tmp_path, bootstrap_source(OLD))
    remote = Remote(NEW)
    settings = UpdateCheck(interval_h=24)
    assert update_check.check(root, settings, remote=remote, env={}, now=1000.0).message
    again = update_check.check(root, settings, remote=remote, env={}, now=1000.0 + 23 * HOUR)
    assert again.kind == "already-notified" and again.message is None and again.detail["fetched"] is False
    assert len(remote.calls) == 1
    # After the interval upstream is read again, and the same commit may be announced once more.
    later = update_check.check(root, settings, remote=remote, env={}, now=1000.0 + 25 * HOUR)
    assert later.kind == "update-available" and len(remote.calls) == 2


def test_a_newer_upstream_commit_found_on_a_later_read_is_announced(tmp_path: Path):
    root = project(tmp_path, bootstrap_source(OLD))
    update_check.check(root, UpdateCheck(interval_h=1), remote=Remote(NEW), env={}, now=0.0)
    newer = "c" * 40
    result = update_check.check(root, UpdateCheck(interval_h=1), remote=Remote(newer), env={}, now=2 * HOUR)
    assert result.kind == "update-available" and f"upstream {newer[:7]}" in result.message


def test_a_cache_for_another_repository_or_branch_is_not_used(tmp_path: Path):
    root = project(tmp_path, bootstrap_source(OLD))
    update_check.check(root, UpdateCheck(), remote=Remote(OLD), env={}, now=0.0)
    remote = Remote(NEW)
    result = update_check.check(root, UpdateCheck(branch="next"), remote=remote, env={}, now=10.0)
    assert remote.calls[0][1] == "refs/heads/next" and result.kind == "update-available" and "@next" in result.message


def test_a_failed_read_is_silent_and_retried_after_an_hour_not_every_session(tmp_path: Path):
    root = project(tmp_path, bootstrap_source(OLD))
    failing = Remote(None)
    result = update_check.check(root, UpdateCheck(), remote=failing, env={}, now=0.0)
    assert result.kind == "check-failed" and result.message is None
    assert update_check.check(root, UpdateCheck(), remote=failing, env={}, now=HOUR - 1).kind == "cached"
    assert len(failing.calls) == 1
    assert update_check.check(root, UpdateCheck(), remote=Remote(NEW), env={}, now=HOUR + 1).kind == "update-available"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["git"], 5), FileNotFoundError("git"), PermissionError("git")])
def test_ls_remote_that_times_out_or_has_no_git_answers_none(monkeypatch, error):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(update_check.subprocess, "run", boom)
    assert update_check.ls_remote(REPO, "refs/heads/main", 5) is None


def test_ls_remote_never_prompts_and_passes_the_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{NEW}\trefs/heads/main\n", stderr="")

    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(update_check.subprocess, "run", fake_run)
    assert update_check.ls_remote(REPO, "refs/heads/main", 4) == NEW
    assert seen["argv"] == ["git", "ls-remote", "--", REPO, "refs/heads/main"] and seen["timeout"] == 4
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and "BatchMode=yes" in seen["env"]["GIT_SSH_COMMAND"]


def test_a_failing_ls_remote_exit_answers_none(monkeypatch):
    monkeypatch.setattr(update_check.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal"))
    assert update_check.ls_remote(REPO, "refs/heads/main", 4) is None


def test_an_unreadable_record_or_cache_is_silent(tmp_path: Path):
    root = project(tmp_path, bootstrap_source(OLD))
    (root / update_check.CACHE_FILE).parent.mkdir(parents=True)
    (root / update_check.CACHE_FILE).write_text("{not json", encoding="utf-8")
    assert update_check.check(root, UpdateCheck(), remote=Remote(OLD), env={}, now=0.0).kind == "up-to-date"
    (root / update_check.INSTALLED).write_text("[1, 2]", encoding="utf-8")
    assert update_check.check(root, UpdateCheck(), remote=Remote(NEW), env={}, now=0.0).kind == "no-source"


@pytest.mark.parametrize("value", ["off", "0", "false", "NO"])
def test_the_environment_switches_the_check_off_without_reading_upstream(tmp_path: Path, value):
    remote = Remote(NEW)
    result = update_check.check(project(tmp_path, bootstrap_source(OLD)), UpdateCheck(), remote=remote, env={update_check.OFF_ENV: value})
    assert result.kind == "switched-off" and remote.calls == []


def test_an_installed_checkout_that_already_contains_upstream_is_not_reminded(tmp_path: Path):
    root_checkout = checkout(tmp_path)
    base = git(root_checkout, "rev-parse", "HEAD")
    (root_checkout / "extra.txt").write_text("x", encoding="utf-8")
    git(root_checkout, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(root_checkout, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "two")
    ahead = git(root_checkout, "rev-parse", "HEAD")
    source = {"via": "checkout", "repo": REPO, "commit": ahead, "dirty": False, "checkout": str(root_checkout)}
    result = update_check.check(project(tmp_path, source), UpdateCheck(), remote=Remote(base), env={}, now=0.0)
    assert result.kind == "installed-ahead" and result.message is None


def test_a_checkout_install_is_told_to_pull_and_rerun_its_install_sh(tmp_path: Path):
    root_checkout = checkout(tmp_path)
    source = update_check.record_source(root_checkout / "src" / "coding_agent", env={}) | {"dirty": True}
    root = project(tmp_path, source)
    result = update_check.check(root, UpdateCheck(), remote=Remote(NEW), env={}, now=0.0)
    assert "(có sửa chưa commit)" in result.message
    assert f"git -C {root_checkout.resolve()} pull --ff-only && bash {root_checkout.resolve() / 'install.sh'} {root} --add-new-hooks" in result.message


def test_the_update_command_keeps_a_custom_location_another_repository_and_windows():
    source = {"via": "bootstrap", "repo": "git@github.com:someone/fork.git", "commit": OLD}
    posix = update_check.update_command(source, NEW, Path("/p"), "main", ".harness/coding-agent/src", platform="linux")
    assert posix == (f"curl -fsSL https://raw.githubusercontent.com/someone/fork/main/bootstrap.sh | CODING_AGENT_OWNER=someone "
                     f"CODING_AGENT_REPO=fork CODING_AGENT_REF={NEW} bash -s -- --python-src .harness/coding-agent/src --add-new-hooks")
    windows = update_check.update_command(bootstrap_source(), NEW, Path("C:/p"), "main", project_install.DEFAULT_PYTHON_SRC, platform="win32")
    assert windows == (f"$env:CODING_AGENT_REF='{NEW}'; & ([scriptblock]::Create((irm "
                       "https://raw.githubusercontent.com/dragonwar000/coding-agent/main/bootstrap.ps1))) --add-new-hooks")
    other = {"via": "bootstrap", "repo": "https://gitlab.example/x/y.git", "commit": OLD}
    assert update_check.update_command(other, NEW, Path("/p"), "main") is None
    assert "cài lại từ https://gitlab.example/x/y.git" in update_check.notice_text(other, NEW, "main", None)


# --- the hook ---------------------------------------------------------------------------------------------------


@pytest.fixture
def hooked(tmp_path: Path, monkeypatch):
    """A project with the shipped manifest and a bootstrap source record; upstream is faked to a newer commit."""
    root = project(tmp_path, bootstrap_source(OLD))
    git(tmp_path, "init", "-q", str(root))
    (root / "integration.yaml").write_text((ROOT / "integration.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    remote = Remote(NEW)
    monkeypatch.setattr(update_check, "ls_remote", remote)
    monkeypatch.delenv(update_check.OFF_ENV, raising=False)
    monkeypatch.delenv("CODING_AGENT_MODE_UPDATE_CHECK", raising=False)
    return root, remote


def run_hook(root: Path, source: str = "startup") -> hooks.HookResult:
    payload = {"session_id": "s1", "cwd": str(root), "hook_event_name": "SessionStart", "source": source}
    return hooks.run("update-check", payload, handlers.update_check_hook)


def test_the_hook_shows_the_reminder_to_the_user_and_the_agent_and_logs_it(hooked):
    root, remote = hooked
    result = run_hook(root)
    output = json.loads(result.stdout)
    assert result.code == 0 and output["systemMessage"] == output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert f"upstream {NEW[:7]}" in output["systemMessage"]
    logged = [e for e in map(json.loads, events.log_path(root).read_text(encoding="utf-8").splitlines()) if e["guard"] == "update-check"]
    assert [e["kind"] for e in logged] == ["update-available"]
    # The next session within the interval reads nothing, says nothing and logs nothing.
    assert run_hook(root, "resume").stdout == "" and len(remote.calls) == 1
    assert len(events.log_path(root).read_text(encoding="utf-8").splitlines()) == 1


def test_the_hook_reads_interval_timeout_and_branch_from_the_manifest(hooked):
    root, remote = hooked
    path = root / "integration.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("  timeout_s: 5 ", "  timeout_s: 2 ").replace("  branch: main", "  branch: release"), encoding="utf-8")
    run_hook(root)
    assert remote.calls == [("https://github.com/dragonwar000/coding-agent", "refs/heads/release", 2)]


def test_the_hook_is_silent_after_a_compaction_in_a_worker_and_when_switched_off(hooked, monkeypatch):
    root, remote = hooked
    assert run_hook(root, "compact").stdout == "" and run_hook(root, "clear").stdout == ""
    monkeypatch.setenv("CODING_AGENT_ROLE", "worker")
    assert run_hook(root).stdout == ""
    monkeypatch.delenv("CODING_AGENT_ROLE")
    monkeypatch.setenv("CODING_AGENT_MODE_UPDATE_CHECK", "off")
    assert run_hook(root).stdout == ""
    monkeypatch.delenv("CODING_AGENT_MODE_UPDATE_CHECK")
    monkeypatch.setenv(update_check.OFF_ENV, "off")
    assert run_hook(root).stdout == ""
    assert remote.calls == []


def test_the_hook_is_silent_when_upstream_cannot_be_read(hooked):
    root, remote = hooked
    remote.sha = None
    result = run_hook(root)
    assert result.code == 0 and result.stdout == "" and result.stderr == ""
    logged = [json.loads(line) for line in events.log_path(root).read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in logged] == ["check-failed"]


def test_the_hook_never_fails_the_session_when_the_check_raises(hooked, monkeypatch):
    root, _ = hooked

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(update_check, "check", broken)
    assert run_hook(root).code == 0


def test_the_hook_runs_as_a_process_from_the_generated_command(hooked):
    root, _ = hooked
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"), "CLAUDE_PROJECT_DIR": str(root), update_check.OFF_ENV: "off"}
    payload = json.dumps({"session_id": "s", "cwd": str(root), "hook_event_name": "SessionStart", "source": "startup"})
    done = subprocess.run([sys.executable, "-m", "coding_agent.hooks", "update-check"], input=payload, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert done.returncode == 0 and done.stdout == ""


# --- the manifest -------------------------------------------------------------------------------------------------


def test_the_shipped_manifest_wires_update_check_at_session_start_for_both_hosts():
    shipped = manifest.load(ROOT / "integration.yaml")
    hook = shipped.hook("update-check")
    assert hook.event == "SessionStart" and hook.matcher == "startup|resume" and hook.mode == "shadow"
    assert set(hook.host) == {"claude_code", "codex"}
    assert shipped.update_check == UpdateCheck(interval_h=24, timeout_s=5, branch="main")


def test_a_manifest_without_the_block_uses_the_defaults():
    assert manifest.update_check_from(None) == UpdateCheck()
    assert manifest.update_check_from({"interval_h": 6}) == UpdateCheck(interval_h=6)


@pytest.mark.parametrize("raw, message", [
    ({"interval_h": 0}, "interval_h must be an integer from 1 to 720"),
    ({"timeout_s": 60}, "timeout_s must be an integer from 1 to 30"),
    ({"timeout_s": True}, "timeout_s must be an integer"),
    ({"branch": "-x"}, "branch must be a branch name"),
    ({"branch": "a..b"}, "branch must be a branch name"),
    ({"enabled": False}, "unknown keys: enabled"),
    ([], "must be a mapping"),
])
def test_a_wrong_update_check_block_fails_at_load(raw, message):
    with pytest.raises(ManifestError, match=message):
        manifest.update_check_from(raw)


def test_a_wrong_block_in_the_file_fails_the_whole_manifest(tmp_path: Path):
    text = yaml.safe_load((ROOT / "integration.yaml").read_text(encoding="utf-8"))
    text["update_check"] = {"interval_h": "daily"}
    path = tmp_path / "integration.yaml"
    path.write_text(yaml.safe_dump(text, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ManifestError, match="update_check.interval_h"):
        manifest.load(path)
