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


@pytest.fixture(autouse=True)
def claude_config_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test reads and writes Claude Code's config in a temporary CLAUDE_CONFIG_DIR, never the real ~/.claude.json."""
    config_dir = tmp_path_factory.mktemp("claude-config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    monkeypatch.delenv("CODING_AGENT_SHARE_TRUST", raising=False)
    return config_dir


@pytest.fixture(autouse=True)
def no_session_project(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test sees the real session's project: no CLAUDE_PROJECT_DIR, and an empty working directory."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test opens a connection to the API host: the probe says the network is down unless a test patches it."""
    from coding_agent import resume

    monkeypatch.setattr(resume, "network_up", lambda timeout_s=0.0: False)
    monkeypatch.delenv("CODING_AGENT_AUTO_RESUME", raising=False)
    monkeypatch.delenv("CODING_AGENT_API_HOST", raising=False)


@pytest.fixture(autouse=True)
def no_real_orca(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the real `orca` CLI (or the Run this machine's terminal is bound to): a test that needs Orca
    patches `orca_cli` or points `CODING_AGENT_ORCA` at the fake."""
    monkeypatch.setenv("CODING_AGENT_ORCA", "orca-not-available-in-tests")
    # The code under test sets CODING_AGENT_ORCA_RUN in os.environ directly (a stored or bound Run); recording it here
    # makes monkeypatch restore it after each test, so a Run never leaks into the next one.
    monkeypatch.setenv("CODING_AGENT_ORCA_RUN", "unset")
    monkeypatch.delenv("CODING_AGENT_ORCA_RUN")
    from coding_agent import orca_cli

    monkeypatch.setattr(orca_cli, "_adopted", None)
