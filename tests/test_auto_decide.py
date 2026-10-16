"""Auto-decide: configuration, the CLI switch, the choice rules, the window and Fable (no GUI is opened)."""

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent import autodecide, cli, coordinator, gen
from coding_agent.manifest import AutoDecide, ManifestError, load

BASE = """schema: 1
verified: false
python_src: src
hooks:
  - id: auto-decide
    host: [claude_code]
    event: PreToolUse
    matcher: AskUserQuestion
    mode: shadow
    timeout_s: 4215
    assumption: "a question nobody answers in time can be decided for the user"
"""
CLEAN = [{"question": "Dùng thư viện nào?", "header": "Lib", "multiSelect": False,
          "options": [{"label": "requests", "description": ""}, {"label": "httpx (Recommended)", "description": ""}]}]
MULTI = [{"question": "Bật gì?", "header": "Bật", "multiSelect": True,
          "options": [{"label": "a"}, {"label": "b (Khuyến nghị)"}, {"label": "c (Recommended)"}]}]


def write(root: Path, text: str) -> Path:
    path = root / "integration.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_the_manifest_has_no_auto_decide(tmp_path):
    assert load(write(tmp_path, BASE)).auto_decide == AutoDecide(enabled=False, timeout_s=60, fable=True, fable_timeout_s=90)


@pytest.mark.parametrize("block, message", [
    ("auto_decide: {timeout_s: 5}\n", "auto_decide.timeout_s must be an integer from 10 to 3600"),
    ("auto_decide: {timeout_s: 3601}\n", "auto_decide.timeout_s must be an integer from 10 to 3600"),
    ("auto_decide: {fable_timeout_s: 601}\n", "auto_decide.fable_timeout_s must be an integer from 10 to 600"),
    ("auto_decide: {enabled: yes-please}\n", "auto_decide.enabled must be true or false"),
    ("auto_decide: [1, 2]\n", "auto_decide must be a mapping"),
])
def test_rejects_a_bad_auto_decide_block(tmp_path, block, message):
    with pytest.raises(ManifestError, match=re.escape(message)):
        load(write(tmp_path, BASE + block))


def test_the_hook_timeout_is_rendered(tmp_path):
    rendered = gen.render(load(write(tmp_path, BASE)), ["claude_code"])
    entry = rendered["claude_code"]["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "AskUserQuestion"
    assert entry["hooks"][0]["timeout"] == 4215


def test_the_shipped_manifest_waits_long_enough_for_the_longest_wait():
    manifest = load(Path(__file__).resolve().parents[1] / "integration.yaml")
    assert manifest.auto_decide == AutoDecide()
    assert manifest.hook("auto-decide").timeout_s > 3600 + 600


def test_the_cli_turns_it_on_changes_the_wait_and_turns_it_off(tmp_path, capsys):
    write(tmp_path, BASE + "auto_decide: {timeout_s: 60}\n")
    assert cli.main(["--root", str(tmp_path), "auto-decide", "on", "--timeout", "90"]) == 0
    assert json.loads(capsys.readouterr().out)["timeout_s"] == 90
    base = load(tmp_path / "integration.yaml").auto_decide
    assert autodecide.effective(tmp_path, base) == AutoDecide(enabled=True, timeout_s=90, fable=True, fable_timeout_s=90)
    assert cli.main(["--root", str(tmp_path), "auto-decide", "--no-fable"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": True, "timeout_s": 90, "fable": False, "fable_timeout_s": 90}
    assert cli.main(["--root", str(tmp_path), "auto-decide", "off"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_the_cli_refuses_a_wait_out_of_range_and_writes_nothing(tmp_path, capsys):
    write(tmp_path, BASE)
    assert cli.main(["--root", str(tmp_path), "auto-decide", "on", "--timeout", "5"]) == 1
    assert "from 10 to 3600" in capsys.readouterr().err
    assert not (tmp_path / autodecide.STATE_FILE).exists()


def test_a_broken_state_file_falls_back_to_the_manifest(tmp_path):
    (tmp_path / autodecide.STATE_FILE).parent.mkdir(parents=True)
    (tmp_path / autodecide.STATE_FILE).write_text('{"enabled": true, "timeout_s": 2}', encoding="utf-8")
    assert autodecide.effective(tmp_path, AutoDecide()) == AutoDecide()


def test_suggested_takes_the_recommended_option_else_the_first():
    assert autodecide.suggested(CLEAN) == {"Dùng thư viện nào?": "httpx (Recommended)"}
    assert autodecide.suggested(MULTI) == {"Bật gì?": "b (Khuyến nghị), c (Recommended)"}
    plain = [{"question": "Q", "options": [{"label": "x"}, {"label": "y"}]}]
    assert autodecide.suggested(plain) == {"Q": "x"}


def test_valid_answer_refuses_a_label_outside_the_list():
    assert autodecide.valid_answer(CLEAN, {"Dùng thư viện nào?": "requests"})
    assert not autodecide.valid_answer(CLEAN, {"Dùng thư viện nào?": "aiohttp"})
    assert not autodecide.valid_answer(CLEAN, {})
    assert autodecide.valid_answer(MULTI, {"Bật gì?": "a, c (Recommended)"})


def test_sensitive_questions_are_flagged_not_blocked():
    assert not autodecide.is_sensitive(CLEAN)
    assert autodecide.is_sensitive([{"question": "Xoá nhánh cũ?", "options": [{"label": "Có"}]}])
    assert autodecide.is_sensitive([{"question": "Tiếp theo?", "options": [{"label": "git push lên main"}]}])


def test_the_window_reports_a_missing_tkinter_with_exit_3(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "tkinter", None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"questions": CLEAN, "timeout_s": 10})))
    assert autodecide.window_main() == 3
    assert "auto-decide window:" in capsys.readouterr().err


def test_the_window_refuses_bad_input_with_exit_2(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert autodecide.window_main() == 2


@pytest.mark.parametrize("stdout, outcome", [
    ('{"answers": {"Dùng thư viện nào?": "requests"}}', "answered"),
    ('{"timeout": true}', "timeout"),
    ('{"dismissed": true}', "error"),
    ('{"answers": {"Dùng thư viện nào?": "aiohttp"}}', "error"),
])
def test_ask_window_reads_what_the_window_printed(monkeypatch, stdout, outcome):
    monkeypatch.delenv(autodecide.FAKE_ENV, raising=False)
    monkeypatch.setattr(autodecide.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr=""))
    assert autodecide.ask_window(CLEAN, 10).outcome == outcome


def test_parse_fable_reads_the_claude_json_envelope():
    envelope = json.dumps({"type": "result", "result": 'Đây: {"answers": {"Dùng thư viện nào?": "requests"}, "reason": "đã có sẵn"}'})
    assert autodecide.parse_fable(envelope) == ({"Dùng thư viện nào?": "requests"}, "đã có sẵn")
    assert autodecide.parse_fable("không có json") is None


def test_ask_fable_refuses_a_label_outside_the_list(monkeypatch):
    monkeypatch.setenv(autodecide.FABLE_FAKE_ENV, json.dumps({"result": '{"answers": {"Dùng thư viện nào?": "aiohttp"}, "reason": "x"}'}))
    assert autodecide.ask_fable(CLEAN, "", 30) is None
    monkeypatch.setenv(autodecide.FABLE_FAKE_ENV, "error")
    assert autodecide.ask_fable(CLEAN, "", 30) is None


def test_the_prompt_lists_the_options_and_the_context():
    prompt = autodecide.fable_prompt(CLEAN, "user muốn giảm dependency")
    assert "httpx (Recommended)" in prompt and "user muốn giảm dependency" in prompt and '"answers"' in prompt


def test_the_contract_asks_through_ask_user_question_only_when_auto_decide_is_on():
    assert coordinator.contract("src") == coordinator.contract("src", auto_decide=False)
    assert "AskUserQuestion" not in coordinator.contract("src")
    on = coordinator.contract("src", auto_decide=True)
    assert on.endswith(coordinator.AUTO_DECIDE_LINE)
    assert "AskUserQuestion" in on and "(Recommended)" in on
