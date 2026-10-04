"""Installing into a project like the setup harness (FR-001, FR-004): one command, detected vendors, merged wiring, verify, uninstall."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent import gate, install, project_install
from coding_agent.manifest import load

ROOT = Path(__file__).resolve().parents[1]


def git_project(tmp_path: Path) -> Path:
    project = tmp_path / "demo-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


def test_the_template_is_the_shipped_manifest():
    assert project_install.TEMPLATE.read_text(encoding="utf-8") == (ROOT / "integration.yaml").read_text(encoding="utf-8")


def test_a_fresh_project_gets_the_package_manifest_hooks_ci_and_a_passing_verify(tmp_path: Path):
    project = git_project(tmp_path)
    report = project_install.install(project)
    assert (project / "harness" / "coding-agent" / "src" / "coding_agent" / "gate.py").exists()
    assert install.is_owned_intact(project / "harness" / "coding-agent" / "src" / "coding_agent")
    manifest = load(project / "integration.yaml")
    assert manifest.python_src == "harness/coding-agent/src" and manifest.verified is False
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "PreToolUse" in settings["hooks"] and "SessionStart" in settings["hooks"]
    assert not (project / ".codex" / "hooks.json").exists()
    assert (project / ".github" / "workflows" / "coding-agent.yml").read_text(encoding="utf-8").startswith(project_install.CI_MARK)
    assert "vendors: claude_code" in report
    assert "verify: orca-guard (enforce) answered exit 2 to an invalid status" in report
    assert "verify: prompt-reset answered exit 0" in report
    assert "verify: coordinator-guard (shadow) answered exit 0 to a direct write" in report
    assert "verify: coordinator-guard leaves worker sessions alone" in report
    assert gate.main(["--manifest", str(project / "integration.yaml")]) == 0


def test_the_installed_hooks_run_from_the_command_the_host_file_contains(tmp_path: Path):
    project = git_project(tmp_path)
    project_install.install(project, run_verify=False)
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for event in settings["hooks"].values() for entry in event for h in entry["hooks"]]
    assert all(c.startswith("CODING_AGENT_MODE_") and '"$CLAUDE_PROJECT_DIR"/harness/coding-agent/src' in c for c in commands)


def test_vendors_are_detected_from_the_project_and_can_be_forced(tmp_path: Path):
    project = git_project(tmp_path)
    (project / ".codex").mkdir()
    assert project_install.detect_hosts(project, None) == ["codex"]
    (project / ".claude").mkdir()
    assert project_install.detect_hosts(project, None) == ["claude_code", "codex"]
    assert project_install.detect_hosts(project, "claude") == ["claude_code"]
    with pytest.raises(project_install.InstallError, match="unknown vendor"):
        project_install.detect_hosts(project, "cursor")


def test_a_bare_project_defaults_to_claude_code(tmp_path: Path):
    project = git_project(tmp_path)
    assert project_install.detect_hosts(project, None) == ["claude_code"]


def test_an_existing_settings_file_keeps_the_users_hooks_and_is_backed_up(tmp_path: Path):
    project = git_project(tmp_path)
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/my-lint"}]}]},
    }), encoding="utf-8")
    project_install.install(project, run_verify=False)
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "opus"
    assert any(h["command"] == "/usr/local/bin/my-lint" for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"])
    assert (project / ".claude" / "settings.json.bak").exists()


def test_a_second_install_is_idempotent(tmp_path: Path):
    project = git_project(tmp_path)
    project_install.install(project)
    first = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    report = project_install.install(project)
    assert (project / ".claude" / "settings.json").read_text(encoding="utf-8") == first
    assert any("previous copy kept at" in line for line in report)
    assert "manifest: kept existing integration.yaml" in report


def test_an_existing_manifest_is_never_overwritten(tmp_path: Path):
    project = git_project(tmp_path)
    (project / "integration.yaml").write_text((ROOT / "integration.yaml").read_text(encoding="utf-8").replace("python_src: src", "python_src: vendored/src"), encoding="utf-8")
    project_install.install(project, python_src="vendored/src", run_verify=False)
    assert load(project / "integration.yaml").python_src == "vendored/src"


def test_an_existing_ci_file_that_is_not_ours_is_kept(tmp_path: Path):
    project = git_project(tmp_path)
    ci = project / ".github" / "workflows" / "coding-agent.yml"
    ci.parent.mkdir(parents=True)
    ci.write_text("name: mine\n", encoding="utf-8")
    report = project_install.install(project, run_verify=False)
    assert ci.read_text(encoding="utf-8") == "name: mine\n"
    assert any("kept existing" in line for line in report)


def test_no_ci_flag_writes_no_workflow(tmp_path: Path):
    project = git_project(tmp_path)
    project_install.install(project, run_verify=False, ci=False)
    assert not (project / ".github").exists()


def test_an_unowned_package_copy_is_refused_and_left_in_place(tmp_path: Path):
    project = git_project(tmp_path)
    mine = project / "harness" / "coding-agent" / "src" / "coding_agent" / "mine.py"
    mine.parent.mkdir(parents=True)
    mine.write_text("keep me", encoding="utf-8")
    with pytest.raises(install.NotOwned):
        project_install.install(project)
    assert mine.read_text(encoding="utf-8") == "keep me"
    assert not (project / "integration.yaml").exists()


def test_a_host_file_that_is_not_json_stops_the_install_without_changes(tmp_path: Path):
    project = git_project(tmp_path)
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(Exception, match="not valid JSON"):
        project_install.install(project, run_verify=False)
    assert (project / ".claude" / "settings.json").read_text(encoding="utf-8") == "{ broken"


def test_a_failing_wiring_check_is_reported(tmp_path: Path):
    project = git_project(tmp_path)
    project_install.install(project, run_verify=False)
    settings = project / ".claude" / "settings.json"
    settings.write_text(settings.read_text(encoding="utf-8").replace("CODING_AGENT_MODE_ORCA_GUARD=enforce", "CODING_AGENT_MODE_ORCA_GUARD=shadow"), encoding="utf-8")
    with pytest.raises(project_install.InstallError, match="differ from integration.yaml"):
        project_install.verify_wiring(project, project / "integration.yaml", ["claude_code"])


def test_a_hook_that_does_not_block_fails_the_wiring_check(tmp_path: Path, monkeypatch):
    project = git_project(tmp_path)
    project_install.install(project, run_verify=False)
    settings = project / ".claude" / "settings.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    for entries in document["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                hook["command"] = hook["command"].replace("CODING_AGENT_MODE_ORCA_GUARD=enforce", "CODING_AGENT_MODE_ORCA_GUARD=shadow")
    settings.write_text(json.dumps(document), encoding="utf-8")
    manifest = load(project / "integration.yaml")
    assert manifest.hook("orca-guard").mode == "enforce"
    monkeypatch.setattr(project_install.gen, "drift", lambda *_args: [])
    with pytest.raises(project_install.InstallError, match="orca-guard answered exit 0, expected 2"):
        project_install.verify_wiring(project, project / "integration.yaml", ["claude_code"])


def test_uninstall_removes_the_wiring_and_the_copy_and_keeps_the_users_hooks(tmp_path: Path):
    project = git_project(tmp_path)
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/my-lint"}]}]}}), encoding="utf-8")
    project_install.install(project, run_verify=False)
    report = project_install.uninstall(project)
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert [h["command"] for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"]] == ["/usr/local/bin/my-lint"]
    assert not (project / "harness" / "coding-agent" / "src" / "coding_agent").exists()
    assert not (project / "harness").exists()
    assert not (project / ".github" / "workflows" / "coding-agent.yml").exists()
    assert not (project / ".coding-agent" / "installed.json").exists()
    assert (project / "integration.yaml").exists()
    assert any("package: removed" in line for line in report)


def test_uninstall_keep_core_leaves_the_package(tmp_path: Path):
    project = git_project(tmp_path)
    project_install.install(project, run_verify=False)
    project_install.uninstall(project, keep_core=True)
    assert (project / "harness" / "coding-agent" / "src" / "coding_agent" / "gate.py").exists()


def test_clean_reinstalls_from_scratch(tmp_path: Path, capsys):
    project = git_project(tmp_path)
    assert project_install.main(["--project", str(project), "--no-verify"]) == 0
    assert project_install.main(["--project", str(project), "--no-verify", "--clean"]) == 0
    out = capsys.readouterr().out
    assert "package: removed" in out and out.count("package: harness/coding-agent/src/coding_agent") == 2


def test_the_cli_reports_errors_with_exit_two(tmp_path: Path, capsys):
    assert project_install.main(["--project", str(tmp_path / "nowhere")]) == 2
    assert "not a directory" in capsys.readouterr().err
    project = git_project(tmp_path)
    assert project_install.main(["--project", str(project), "--vendor", "cursor"]) == 2
    assert "unknown vendor" in capsys.readouterr().err


@pytest.mark.parametrize("python_src", ["/abs/src", "../escape"])
def test_a_location_outside_the_project_is_refused(tmp_path: Path, python_src):
    project = git_project(tmp_path)
    with pytest.raises(project_install.InstallError, match="relative path"):
        project_install.install(project, python_src=python_src)


def test_a_missing_project_directory_is_refused(tmp_path: Path):
    with pytest.raises(project_install.InstallError, match="not a directory"):
        project_install.install(tmp_path / "nowhere")


def test_a_template_without_the_expected_lines_is_refused(tmp_path: Path, monkeypatch):
    broken = tmp_path / "template.yaml"
    broken.write_text("schema: 1\nverified: false\npython_source: src\nhooks: []\n", encoding="utf-8")
    monkeypatch.setattr(project_install, "TEMPLATE", broken)
    with pytest.raises(project_install.InstallError, match="cannot set it"):
        project_install.manifest_text("harness/coding-agent/src", "true")


def test_gitignore_gets_the_state_directory_once(tmp_path: Path):
    project = git_project(tmp_path)
    (project / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    project_install.install(project, run_verify=False)
    project_install.install(project, run_verify=False)
    assert (project / ".gitignore").read_text(encoding="utf-8").splitlines().count(".coding-agent/") == 1


def old_manifest(project: Path) -> None:
    """A manifest from before the coordinator hooks existed."""
    text = (ROOT / "integration.yaml").read_text(encoding="utf-8").replace("python_src: src", "python_src: harness/coding-agent/src")
    (project / "integration.yaml").write_text(text[:text.index("  - id: coordinator-guard")], encoding="utf-8")


def test_upgrading_over_an_older_manifest_passes_and_names_the_new_hooks(tmp_path: Path):
    project = git_project(tmp_path)
    old_manifest(project)
    report = project_install.install(project)
    assert any("hooks your manifest lacks: coordinator-guard, coordinator-context, coordinator-board" in line for line in report)
    assert "verify: coordinator-guard is not in this manifest; skipped" in report
    assert load(project / "integration.yaml").hook("coordinator-guard") is None


def test_add_new_hooks_appends_them_and_keeps_a_backup(tmp_path: Path):
    project = git_project(tmp_path)
    old_manifest(project)
    before = (project / "integration.yaml").read_text(encoding="utf-8")
    report = project_install.install(project, add_new_hooks=True)
    manifest = load(project / "integration.yaml")
    assert manifest.hook("coordinator-guard").mode == "shadow" and manifest.hook("coordinator-board") is not None
    assert (project / "integration.yaml.bak").read_text(encoding="utf-8") == before
    assert "verify: coordinator-guard (shadow) answered exit 0 to a direct write" in report
    assert "coordinator-guard" in (project / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert project_install.missing_hooks(project / "integration.yaml") == []


def test_add_new_hooks_refuses_a_manifest_that_does_not_end_with_hooks(tmp_path: Path):
    project = git_project(tmp_path)
    old_manifest(project)
    path = project / "integration.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "extra_key: 1\n", encoding="utf-8")
    with pytest.raises(project_install.InstallError, match="by hand"):
        project_install.add_hooks(path, ["coordinator-guard"])
    assert not (project / "integration.yaml.bak").exists()
