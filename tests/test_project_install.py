"""Installing into a project (FR-004, FR-001): owned copies are replaced with a backup, unowned ones are refused, and the result passes the gate."""

import shutil
from pathlib import Path

import pytest

from coding_agent import gate, install, project_install
from coding_agent.manifest import load

ROOT = Path(__file__).resolve().parents[1]


def test_the_template_is_the_shipped_manifest():
    assert project_install.TEMPLATE.read_text(encoding="utf-8") == (ROOT / "integration.yaml").read_text(encoding="utf-8")


def test_a_fresh_project_gets_a_package_a_manifest_and_matching_hooks(tmp_path: Path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    report = project_install.install(project)
    assert (project / "harness" / "src" / "coding_agent" / "gate.py").exists()
    assert not list((project / "harness" / "src").rglob("__pycache__"))
    assert install.is_owned_intact(project / "harness" / "src" / "coding_agent")
    manifest = load(project / "integration.yaml")
    assert manifest.python_src == "harness/src" and manifest.verified is False
    assert (project / ".claude" / "settings.json").exists() and (project / ".codex" / "hooks.json").exists()
    assert any(line.startswith("gate: live hook files match") for line in report)
    assert gate.main(["--manifest", str(project / "integration.yaml")]) == 0


def test_an_existing_manifest_is_never_overwritten(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "integration.yaml").write_text((ROOT / "integration.yaml").read_text(encoding="utf-8").replace("python_src: src", "python_src: vendored/src"), encoding="utf-8")
    report = project_install.install(project)
    assert "manifest: kept existing integration.yaml" in report
    assert load(project / "integration.yaml").python_src == "vendored/src"


def test_a_second_install_replaces_the_owned_copy_and_keeps_a_backup(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    project_install.install(project)
    report = project_install.install(project)
    assert any("previous copy kept at" in line for line in report)
    assert list((project / ".coding-agent" / "backups").iterdir())


def test_an_unowned_copy_is_refused_and_left_in_place(tmp_path: Path):
    project = tmp_path / "project"
    (project / "harness" / "src" / "coding_agent").mkdir(parents=True)
    mine = project / "harness" / "src" / "coding_agent" / "mine.py"
    mine.write_text("keep me", encoding="utf-8")
    with pytest.raises(install.NotOwned):
        project_install.install(project)
    assert mine.read_text(encoding="utf-8") == "keep me"
    assert not (project / "integration.yaml").exists()


def test_a_custom_location_and_verify_command_reach_the_manifest(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    project_install.install(project, python_src="tools/src", verify="make test")
    manifest = load(project / "integration.yaml")
    assert manifest.python_src == "tools/src" and manifest.verify_commands == ("make test",)
    assert (project / "tools" / "src" / "coding_agent" / "cli.py").exists()


@pytest.mark.parametrize("python_src", ["/abs/src", "../escape"])
def test_a_location_outside_the_project_is_refused(tmp_path: Path, python_src):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(project_install.InstallError, match="relative path"):
        project_install.install(project, python_src=python_src)


def test_a_missing_project_directory_is_refused(tmp_path: Path):
    with pytest.raises(project_install.InstallError, match="not a directory"):
        project_install.install(tmp_path / "nowhere")


def test_the_cli_reports_errors_with_exit_two(tmp_path: Path, capsys):
    assert project_install.main(["--project", str(tmp_path / "nowhere")]) == 2
    assert "not a directory" in capsys.readouterr().err
    project = tmp_path / "project"
    project.mkdir()
    assert project_install.main(["--project", str(project)]) == 0
    assert "gate: live hook files match" in capsys.readouterr().out


def test_a_template_without_the_expected_lines_is_refused(tmp_path: Path, monkeypatch):
    broken = tmp_path / "template.yaml"
    broken.write_text("schema: 1\nverified: false\npython_source: src\nhooks: []\n", encoding="utf-8")
    monkeypatch.setattr(project_install, "TEMPLATE", broken)
    with pytest.raises(project_install.InstallError, match="cannot set it"):
        project_install.manifest_text("harness/src", "true")
