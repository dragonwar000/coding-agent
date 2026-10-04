"""The package gate (FR-003, SC-004): drift fails, a sync passes, a broken manifest is a distinct failure."""

import shutil
from pathlib import Path

from coding_agent import gate, gen
from coding_agent.manifest import load

SHIPPED = Path(__file__).resolve().parents[1] / "integration.yaml"


def project(tmp_path: Path) -> Path:
    """A copy of the shipped manifest in a fresh directory, so the gate runs against its own live files."""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy(SHIPPED, root / "integration.yaml")
    (root / "src").mkdir()
    return root


def test_missing_live_files_fail_the_gate(tmp_path, capsys):
    root = project(tmp_path)
    assert gate.main(["--manifest", str(root / "integration.yaml")]) == 1
    assert "is missing" in capsys.readouterr().err


def test_applied_files_pass_the_gate(tmp_path, capsys):
    root = project(tmp_path)
    gen.write(load(root / "integration.yaml"), apply=True)
    assert gate.main(["--manifest", str(root / "integration.yaml")]) == 0
    assert "match integration.yaml" in capsys.readouterr().out


def test_editing_a_live_file_by_hand_fails_the_gate(tmp_path, capsys):
    root = project(tmp_path)
    gen.write(load(root / "integration.yaml"), apply=True)
    live = root / ".claude" / "settings.json"
    live.write_text(live.read_text(encoding="utf-8").replace("shadow", "enforce", 1), encoding="utf-8")
    assert gate.main(["--manifest", str(root / "integration.yaml")]) == 1
    assert "differ from integration.yaml" in capsys.readouterr().err


def test_an_invalid_manifest_is_exit_two(tmp_path, capsys):
    bad = tmp_path / "integration.yaml"
    bad.write_text("schema: 1\npython_src: src\nhooks: []\n", encoding="utf-8")
    assert gate.main(["--manifest", str(bad)]) == 2
    assert "verified must be true or false" in capsys.readouterr().err


def test_a_missing_pyyaml_is_a_clear_exit_two(tmp_path, capsys, monkeypatch):
    def no_yaml(_path):
        raise ImportError("No module named 'yaml'")

    monkeypatch.setattr(gate, "load", no_yaml)
    manifest = tmp_path / "integration.yaml"
    manifest.write_text("schema: 1\n", encoding="utf-8")
    assert gate.main(["--manifest", str(manifest)]) == 2
    assert "PyYAML is not importable" in capsys.readouterr().err
