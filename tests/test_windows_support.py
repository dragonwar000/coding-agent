"""What Windows needs from the package: a configurable interpreter, a shell check in verify, two bootstraps on one pin, and UTF-8 output on a cp1252 console."""

import io
import re
import sys
from pathlib import Path

import pytest

from coding_agent import cli, gen, hooks, project_install
from coding_agent.hooks import HookResult
from coding_agent.manifest import ManifestError, load

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "schema: 1\nverified: false\n{python}python_src: src\nhooks:\n  - {{id: orca-guard, host: [claude_code], event: PreToolUse, matcher: Bash, mode: shadow, assumption: a}}\n"


def write(tmp_path: Path, python_line: str) -> Path:
    path = tmp_path / "integration.yaml"
    path.write_text(MANIFEST.format(python=python_line), encoding="utf-8")
    return path


def commands(tmp_path: Path, python_line: str) -> list[str]:
    rendered = gen.render(load(write(tmp_path, python_line)))
    return [h["command"] for event in rendered["claude_code"]["hooks"].values() for entry in event for h in entry["hooks"]]


def test_the_hook_interpreter_defaults_to_python3(tmp_path):
    assert load(write(tmp_path, "")).python == "python3"
    assert all(" python3 -m coding_agent.hooks " in command for command in commands(tmp_path, ""))


def test_the_manifest_can_name_the_windows_interpreter(tmp_path):
    assert all(" python -m coding_agent.hooks " in command for command in commands(tmp_path, "python: python\n"))
    assert all(" py -m coding_agent.hooks " in command for command in commands(tmp_path, "python: py\n"))


@pytest.mark.parametrize("value", ["'python3 -X dev'", "'python; rm -rf .'", "''", "'C:\\\\Python312\\\\python.exe'", "3"])
def test_an_interpreter_that_is_not_a_bare_command_is_refused(tmp_path, value):
    with pytest.raises(ManifestError, match="python must be an interpreter command"):
        load(write(tmp_path, f"python: {value}\n"))


def test_the_installer_writes_the_chosen_interpreter_and_a_matching_verify_command(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    project_install.install(project, python="python", run_verify=False, ci=False)
    manifest = load(project / "integration.yaml")
    assert manifest.python == "python" and manifest.verify_commands == ("python -m pytest -q",)
    settings = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert " python -m coding_agent.hooks " in settings and "python3" not in settings


def test_the_default_interpreter_follows_what_the_machine_has(monkeypatch):
    monkeypatch.setattr(project_install, "is_python", lambda name: True)
    assert project_install.default_python() == "python3"
    monkeypatch.setattr(project_install, "is_python", lambda name: name == "py")
    assert project_install.default_python() == "py"
    monkeypatch.setattr(project_install, "is_python", lambda name: False)
    assert project_install.default_python() == "python"


def test_a_command_on_path_that_is_not_python_is_not_taken_for_the_interpreter(tmp_path, monkeypatch):
    """The Microsoft Store alias: `python3` is on PATH, prints a hint, and exits non-zero."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: sys.executable)
    assert project_install.is_python("python3")
    monkeypatch.setattr(project_install.subprocess, "run", lambda *args, **kwargs: project_install.subprocess.CompletedProcess(args, 9009))
    assert not project_install.is_python("python3")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert not project_install.is_python("python3")


def test_verify_names_the_fix_when_the_manifest_interpreter_does_not_run_here(tmp_path, monkeypatch):
    """A manifest written on macOS says `python3`; on Windows that is the Store alias, and every hook would fail."""
    project = tmp_path / "project"
    project.mkdir()
    project_install.install(project, python="python3", run_verify=False, ci=False)
    monkeypatch.setattr(project_install, "posix_shell", lambda: sys.executable)
    monkeypatch.setattr(project_install, "is_python", lambda name: name == "py")
    with pytest.raises(project_install.InstallError, match="python: python3.*Set `python: py`"):
        project_install.verify_wiring(project, project / "integration.yaml", ["claude_code"])


def test_git_bash_is_found_next_to_git_when_bash_is_not_on_path(tmp_path, monkeypatch):
    import shutil

    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    wsl = tmp_path / "Windows" / "System32" / "bash.exe"
    for path in (git, bash, wsl):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
    for name in ("CLAUDE_CODE_GIT_BASH_PATH", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(shutil, "which", lambda name: {"git": str(git), "bash": str(wsl)}.get(name))
    assert Path(project_install._git_bash()) == bash.resolve()
    bash.unlink()
    assert project_install._git_bash() is None, "the WSL launcher under System32 is not a shell for hook commands"
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(git))
    assert project_install._git_bash() == str(git)


def test_verify_says_so_when_no_posix_shell_can_run_the_hooks(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    project_install.install(project, run_verify=False, ci=False)
    monkeypatch.setattr(project_install, "posix_shell", lambda: None)
    report = project_install.verify_wiring(project, project / "integration.yaml", ["claude_code"])
    assert report[0].startswith("verify: gate passed")
    assert "no sh or bash on PATH" in report[1] and "Git for Windows" in report[1]
    assert len(report) == 2


def test_both_bootstraps_pin_the_same_commit():
    sh = re.search(r'^PINNED_REF="([0-9a-f]{40})"', (ROOT / "bootstrap.sh").read_text(encoding="utf-8"), re.M)
    ps = re.search(r"^\$PinnedRef = '([0-9a-f]{40})'", (ROOT / "bootstrap.ps1").read_text(encoding="utf-8"), re.M)
    assert sh and ps and sh.group(1) == ps.group(1)


VIETNAMESE = "Nhiệm vụ: sửa lỗi mã hoá trên Windows"


def cp1252_console(monkeypatch) -> tuple[io.BytesIO, io.BytesIO]:
    """stdout and stderr as a Windows console opens them: text streams over the cp1252 code page.

    Called from the test body, not a fixture: pytest swaps `sys.stdout` between its setup and call phases.
    """
    out, err = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, encoding="cp1252", newline="\n", write_through=True))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err, encoding="cp1252", newline="\n", write_through=True))
    return out, err


def test_a_hook_writes_utf8_on_a_cp1252_console(tmp_path, monkeypatch):
    from coding_agent.hooks import handlers

    out, err = cp1252_console(monkeypatch)
    monkeypatch.setitem(handlers.REGISTRY, "utf8-probe", lambda ctx: HookResult(stdout=VIETNAMESE, stderr=VIETNAMESE))
    monkeypatch.setenv("CODING_AGENT_MODE_UTF8_PROBE", "shadow")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name": "SessionStart", "session_id": "x", "cwd": "%s"}' % tmp_path.as_posix()))
    assert hooks.main(["utf8-probe"]) == 0
    assert out.getvalue().decode("utf-8") == VIETNAMESE + "\n"
    assert err.getvalue().decode("utf-8") == VIETNAMESE + "\n"


def test_a_hook_reads_a_utf8_payload_on_a_cp1252_console(monkeypatch):
    """Hosts send raw UTF-8, not \\u escapes. "ở" holds byte 0x9d, which cp1252 cannot decode at all."""
    raw = ('{"session_id": "x", "prompt": "%s ở đây"}' % VIETNAMESE).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252"))
    assert hooks._read_payload()["prompt"] == VIETNAMESE + " ở đây"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff\xfe not utf-8"), encoding="cp1252"))
    assert hooks._read_payload() == {}


def test_the_cli_writes_utf8_on_a_cp1252_console(tmp_path, monkeypatch):
    out, _ = cp1252_console(monkeypatch)
    plan = tmp_path / "PLAN.md"
    plan.write_text(f"# PLAN\n\n## Global constraints\n- {VIETNAMESE}\n\n## Plan\n### Task 1 — Việc\n**Files:** `a.py`\n", encoding="utf-8")
    assert cli.main(["brief", "--plan", str(plan), "--task", "1"]) == 0
    assert VIETNAMESE in out.getvalue().decode("utf-8")
