from pathlib import Path

import pytest

from coding_agent import install


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_an_unmarked_scripts_directory_is_never_removed(tmp_path: Path):
    project = make_tree(tmp_path / "project", {"harness/scripts/mine.py": "print('mine')"})
    backups = tmp_path / "backups"
    backups.mkdir()
    with pytest.raises(install.NotOwned, match="no intact harness marker"):
        install.safe_remove(project / "harness" / "scripts", backup_root=backups)
    assert (project / "harness" / "scripts" / "mine.py").exists()


def test_an_owned_install_is_replaced_and_backed_up(tmp_path: Path):
    source = make_tree(tmp_path / "source", {"tool.py": "v2"})
    target = tmp_path / "installed"
    make_tree(target, {"tool.py": "v1"})
    install.mark_owned(target)
    backups = tmp_path / "backups"
    backups.mkdir()
    kept = install.safe_replace(target, source, backup_root=backups)
    assert kept is not None and (kept / "tool.py").read_text(encoding="utf-8") == "v1"
    assert (target / "tool.py").read_text(encoding="utf-8") == "v2"
    assert install.is_owned_intact(target)


def test_an_owned_install_edited_after_install_is_refused(tmp_path: Path):
    source = make_tree(tmp_path / "source", {"tool.py": "v2"})
    target = make_tree(tmp_path / "installed", {"tool.py": "v1"})
    install.mark_owned(target)
    (target / "tool.py").write_text("someone edited this", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    with pytest.raises(install.NotOwned):
        install.safe_replace(target, source, backup_root=backups)
    assert (target / "tool.py").read_text(encoding="utf-8") == "someone edited this"


def test_a_fresh_install_needs_no_backup(tmp_path: Path):
    source = make_tree(tmp_path / "source", {"tool.py": "v1"})
    target = tmp_path / "new"
    backups = tmp_path / "backups"
    backups.mkdir()
    assert install.safe_replace(target, source, backup_root=backups) is None
    assert install.is_owned_intact(target)


def test_removal_of_an_owned_install_keeps_a_backup(tmp_path: Path):
    target = make_tree(tmp_path / "owned", {"tool.py": "v1"})
    install.mark_owned(target)
    backups = tmp_path / "backups"
    backups.mkdir()
    kept = install.safe_remove(target, backup_root=backups)
    assert kept is not None and not target.exists()
