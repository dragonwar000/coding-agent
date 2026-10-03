import json
from pathlib import Path

import pytest

from coding_agent import gen
from coding_agent.manifest import ManifestError, load

BASE = """schema: 1
verified: false
python_src: src
hooks:
  - id: orca-guard
    host: [claude_code, codex]
    event: PreToolUse
    matcher: Bash
    mode: enforce
    assumption: "a wrong status is wrong"
  - id: prompt-reset
    host: [claude_code]
    event: UserPromptSubmit
    mode: shadow
    assumption: "a new prompt starts a new turn"
"""


def commands(document: dict) -> list[str]:
    """Every command string in a rendered host document."""
    return [h["command"] for event in document["hooks"].values() for entry in event for h in entry["hooks"]]


def write(tmp: Path, text: str) -> Path:
    path = tmp / "integration.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_the_shipped_manifest():
    manifest = load(Path(__file__).resolve().parents[1] / "integration.yaml")
    assert manifest.python_src == "src"
    assert manifest.hook("stop-gate").mode == "enforce"
    assert manifest.hook("prompt-reset").mode == "shadow"
    assert manifest.verified is False


BAD = {
    "absolute src": ("schema: 1\nverified: false\npython_src: /abs/src\nhooks: []\n", "relative path"),
    "escaping src": ("schema: 1\nverified: false\npython_src: ../escape\nhooks: []\n", "relative path"),
    "wrong schema": ("schema: 2\nverified: false\npython_src: src\nhooks: []\n", "schema must be 1"),
    "unknown host": ("schema: 1\nverified: false\npython_src: src\nhooks:\n  - {id: x, host: [vim], event: Stop}\n", "host must name only"),
    "guard without assumption": ("schema: 1\nverified: false\npython_src: src\nhooks:\n  - {id: x, host: [codex], event: Stop, mode: enforce}\n", "needs a non-blank assumption"),
    "unknown mode": ("schema: 1\nverified: false\npython_src: src\nhooks:\n  - {id: x, host: [codex], event: Stop, mode: maybe, assumption: a}\n", "mode must be one of"),
    "duplicate id": ("schema: 1\nverified: false\npython_src: src\nhooks:\n  - {id: x, host: [codex], event: Stop, mode: 'off'}\n  - {id: x, host: [codex], event: Stop, mode: 'off'}\n", "duplicate hook id"),
    "verified missing": ("schema: 1\npython_src: src\nhooks: []\n", "verified must be true or false"),
    "verified not a boolean": ("schema: 1\nverified: maybe\npython_src: src\nhooks: []\n", "verified must be true or false"),
    "unquoted off": ("schema: 1\nverified: false\npython_src: src\nhooks:\n  - {id: x, host: [codex], event: Stop, mode: off}\n", "quote 'off' in YAML"),
    "default mode needs an assumption": ("schema: 1\nverified: false\npython_src: src\nhooks:\n  - {id: x, host: [codex], event: Stop}\n", "runs in mode 'shadow' and needs a non-blank assumption"),
    "negative denial budget": ("schema: 1\nverified: false\npython_src: src\nloop: {max_denials_per_turn: -1}\nhooks: []\n", "max_denials_per_turn must be an integer >= 0"),
    "episode limit below the fixed lines": ("schema: 1\nverified: false\npython_src: src\nmemory: {max_episode_chars: 10}\nhooks: []\n", "max_episode_chars must be at least"),
    "loop thresholds inverted": ("schema: 1\nverified: false\npython_src: src\nloop: {remind_at: 6, stop_at: 3}\nhooks: []\n", "remind_at must be less"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_rejects_bad_config_at_load(tmp_path, case):
    text, message = BAD[case]
    with pytest.raises(ManifestError, match=message):
        load(write(tmp_path, text))


def test_renders_relative_commands_for_both_hosts(tmp_path):
    manifest = load(write(tmp_path, BASE))
    rendered = gen.render(manifest)
    claude = commands(rendered["claude_code"])
    codex = commands(rendered["codex"])
    assert any(c.startswith("CODING_AGENT_MODE_ORCA_GUARD=enforce") and '"$CLAUDE_PROJECT_DIR"/src' in c for c in claude)
    assert any('"$(git rev-parse --show-toplevel)"/src' in c for c in codex)
    assert all("prompt-reset" not in c for c in codex)  # claude_code only
    assert not any("/Users/" in c for c in claude + codex)


def test_write_proposes_by_default_and_applies_on_request(tmp_path):
    manifest = load(write(tmp_path, BASE))
    proposed = gen.write(manifest, apply=False)
    assert {p.name for p in proposed} == {"settings.proposed.json", "hooks.proposed.json"}
    assert not (tmp_path / ".claude" / "settings.json").exists()
    applied = gen.write(manifest, apply=True)
    assert (tmp_path / ".claude" / "settings.json") in applied


def test_check_reports_drift_and_passes_when_in_sync(tmp_path):
    manifest = load(write(tmp_path, BASE))
    problems = gen.drift(manifest)
    assert len(problems) == 2 and all("missing" in p for p in problems)
    gen.write(manifest, apply=True)
    assert gen.drift(manifest) == []
    settings = tmp_path / ".claude" / "settings.json"
    edited = json.loads(settings.read_text(encoding="utf-8"))
    edited["hooks"]["PreToolUse"][0]["matcher"] = "Bash|Write"
    settings.write_text(json.dumps(edited), encoding="utf-8")
    assert any("claude_code" in p for p in gen.drift(manifest))


def test_main_check_exit_codes(tmp_path, capsys):
    manifest = write(tmp_path, BASE)
    assert gen.main(["--manifest", str(manifest), "--check"]) == 1
    assert gen.main(["--manifest", str(manifest), "--apply"]) == 0
    assert gen.main(["--manifest", str(manifest), "--check"]) == 0
    assert gen.main(["--manifest", str(tmp_path / "missing.yaml")]) == 2
