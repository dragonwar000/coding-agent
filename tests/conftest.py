"""Shared fixtures: a temporary repository root and an isolated Zero-Mem home."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository named `demo-repo`, so `project` is predictable."""
    root = tmp_path / "demo-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.fixture
def zm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated Zero-Mem base directory, so tests never touch ~/.coding-agent."""
    base = tmp_path / "zeromem"
    monkeypatch.setenv("CODING_AGENT_ZEROMEM_HOME", str(base))
    return base


def zm_available() -> bool:
    return shutil.which("zm") is not None or bool(__import__("os").environ.get("CODING_AGENT_ZM"))


requires_zm = pytest.mark.skipif(not zm_available(), reason="zm binary is not installed")
