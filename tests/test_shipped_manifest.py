"""The manifest this repository ships: unverified, every hook in shadow, and the generated commands say so (FR-010)."""

from pathlib import Path

from coding_agent import gen
from coding_agent.manifest import load

SHIPPED = Path(__file__).resolve().parents[1] / "integration.yaml"


def test_the_shipped_manifest_is_unverified_and_enforces_only_the_gates():
    manifest = load(SHIPPED)
    assert manifest.verified is False
    enforcing = {hook.id for hook in manifest.hooks if hook.mode == "enforce"}
    assert enforcing == {"orca-guard", "stop-gate", "coordinator-guard"}
    assert {hook.mode for hook in manifest.hooks if hook.id not in enforcing} == {"shadow"}


def test_the_generated_commands_carry_the_manifest_mode():
    manifest = load(SHIPPED)
    rendered = gen.render(manifest)
    commands = [h["command"] for event in rendered["claude_code"]["hooks"].values() for entry in event for h in entry["hooks"]]
    assert any("CODING_AGENT_MODE_ORCA_GUARD=enforce " in command for command in commands)
    assert any("CODING_AGENT_MODE_STOP_GATE=enforce " in command for command in commands)
    assert all("=enforce " in command or "=shadow " in command for command in commands)
    assert "PreCompact" in rendered["claude_code"]["hooks"] and "SessionStart" in rendered["claude_code"]["hooks"]
    assert "PreCompact" not in rendered["codex"]["hooks"]
