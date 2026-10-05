"""What Windows needs from the package: a configurable interpreter, a shell check in verify, and two bootstraps on one pin."""

import re
from pathlib import Path

import pytest

from coding_agent import gen, project_install
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
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None)
    assert project_install.default_python() == "python3"
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert project_install.default_python() == "python"


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
