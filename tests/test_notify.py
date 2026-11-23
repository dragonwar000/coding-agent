"""The optional `notify` command of the worker watcher: manifest validation, argv and environment only, a failing or slow
command never stops the watcher, and one run per event."""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
import yaml

from coding_agent import events, manifest, notify, orca_cli, resume, watcher

from test_resume import WORKING, Workers
from test_watcher import IDLE, RUN, Escalations, Terminals, failing_scan, transcript_for

ROOT = Path(__file__).resolve().parents[1]
FAKE_COMMAND = """import json, os, sys
with open(sys.argv[1], "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": sys.argv[2:], **{k: v for k, v in os.environ.items() if k.startswith("CODING_AGENT_NOTIFY_")}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_NOTIFY_EXIT", "0")))
"""


@pytest.fixture
def fake(repo, tmp_path):
    """A manifest whose notify command appends what it received to a file; returns a reader of those runs."""
    script = tmp_path / "fake_notify.py"
    script.write_text(FAKE_COMMAND, encoding="utf-8")
    out = tmp_path / "notified.jsonl"
    write_manifest(repo, {"mode": "on", "command": [sys.executable, str(script), str(out)], "timeout_s": 10})

    def runs() -> list[dict]:
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()] if out.exists() else []

    return runs


def write_manifest(repo: Path, block) -> Path:
    path = repo / "integration.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "verified": False, "python_src": "src", "notify": block}), encoding="utf-8")
    return path


def logged(repo: Path) -> list[dict]:
    path = events.log_path(repo)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    return [row for row in rows if row["guard"] == "worker-watch"]


# --- the manifest block --------------------------------------------------------------------------------------------------

def test_a_manifest_without_the_block_is_off(repo):
    path = repo / "integration.yaml"
    path.write_text("schema: 1\nverified: false\npython_src: src\n", encoding="utf-8")
    assert manifest.load(path).notify == manifest.Notify(mode="off", command=(), timeout_s=10)


@pytest.mark.parametrize("name", ["integration.yaml", "src/coding_agent/template.yaml"])
def test_the_shipped_manifests_declare_notify_off(name):
    loaded = manifest.load(ROOT / name).notify
    assert (loaded.mode, loaded.command, loaded.timeout_s) == ("off", (), 10)


def test_a_valid_block_loads(repo):
    path = write_manifest(repo, {"mode": "on", "command": ["notify-me", "--now"], "timeout_s": 30})
    assert manifest.load(path).notify == manifest.Notify(mode="on", command=("notify-me", "--now"), timeout_s=30)


@pytest.mark.parametrize("block, says", [
    ("nope", "must be a mapping"),
    ({"mode": False}, "quote"),
    ({"mode": True, "command": ["x"]}, "quote"),
    ({"mode": "shadow"}, "must be one of off, on"),
    ({"mode": "on"}, "needs a command"),
    ({"mode": "on", "command": []}, "needs a command"),
    ({"mode": "on", "command": "notify-me --now"}, "notify.command"),
    ({"mode": "on", "command": ["ok", " "]}, "notify.command"),
    ({"mode": "on", "command": ["x"], "timeout_s": 0}, "notify.timeout_s"),
    ({"mode": "on", "command": ["x"], "timeout_s": 121}, "notify.timeout_s"),
    ({"mode": "on", "command": ["x"], "timeout_s": "10"}, "notify.timeout_s"),
])
def test_a_wrong_block_fails_at_load(repo, block, says):
    with pytest.raises(manifest.ManifestError, match=says):
        manifest.load(write_manifest(repo, block))


# --- running the command --------------------------------------------------------------------------------------------------

def test_the_event_reaches_the_command_through_the_environment(repo, fake):
    outcome = notify.fire(repo, "stuck", "ctx_1:e1", task_id="task_1", dispatch="ctx_1", terminal="term_1", title="Sửa lỗi", reason="no progress")
    assert outcome == "sent"
    run = fake()[0]
    assert run["argv"] == []
    assert {key.removeprefix("CODING_AGENT_NOTIFY_"): value for key, value in run.items() if key != "argv"} == {
        "KIND": "stuck", "TASK_ID": "task_1", "DISPATCH": "ctx_1", "TERMINAL": "term_1", "TITLE": "Sửa lỗi", "REASON": "no progress"}
    assert [row["kind"] for row in logged(repo)] == ["notified"]


def test_a_hostile_title_stays_text(repo, tmp_path, fake):
    planted = tmp_path / "planted"
    title = f'$(touch {planted}); `touch {planted}` "x\' | touch {planted}'
    assert notify.fire(repo, "death", "k", title=title) == "sent"
    assert fake()[0]["CODING_AGENT_NOTIFY_TITLE"] == title and not planted.exists()


def test_a_nul_byte_in_a_value_does_not_stop_the_command(repo, fake):
    assert notify.fire(repo, "death", "k", title="a\0b") == "sent"
    assert fake()[0]["CODING_AGENT_NOTIFY_TITLE"] == "ab"


def test_off_runs_nothing_and_records_nothing(repo, tmp_path):
    marker = tmp_path / "ran"
    write_manifest(repo, {"mode": "off", "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]})
    assert notify.fire(repo, "death", "k") == "off"
    assert not marker.exists() and resume.history(repo) == [] and logged(repo) == []


def test_no_manifest_is_off(repo):
    assert notify.fire(repo, "death", "k") == "off"


def test_one_event_runs_once_even_for_a_new_loop(repo, fake):
    assert notify.fire(repo, "death", "k", seen=set()) == "sent"
    assert notify.fire(repo, "death", "k", seen=set()) == "already-sent"
    assert notify.fire(repo, "stuck", "k", seen=set()) == "sent"
    assert len(fake()) == 2


def test_a_failing_command_is_recorded_and_not_run_again(repo, fake, monkeypatch):
    monkeypatch.setenv("FAKE_NOTIFY_EXIT", "3")
    assert notify.fire(repo, "death", "k") == "failed"
    assert notify.fire(repo, "death", "k") == "already-sent"
    assert len(fake()) == 1
    failed = [row for row in logged(repo) if row["kind"] == "notify-failed"]
    assert len(failed) == 1 and failed[0]["detail"]["error"].startswith("exit 3")


def test_a_command_that_cannot_start_is_recorded(repo, tmp_path):
    write_manifest(repo, {"mode": "on", "command": [str(tmp_path / "no-such-command")]})
    assert notify.fire(repo, "death", "k") == "failed"
    assert "FileNotFoundError" in logged(repo)[0]["detail"]["error"]


def test_a_malformed_manifest_is_recorded_and_does_not_raise(repo):
    (repo / "integration.yaml").write_text("schema: [", encoding="utf-8")
    assert notify.fire(repo, "death", "k") == "failed"
    assert logged(repo)[0]["kind"] == "notify-failed"


@pytest.mark.skipif(os.name == "nt", reason="process groups")
def test_a_slow_command_and_what_it_started_are_killed_at_the_timeout(repo, tmp_path):
    pid_file = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(child.pid)); time.sleep(60)")
    write_manifest(repo, {"mode": "on", "command": [sys.executable, "-c", code], "timeout_s": 1})
    started = time.monotonic()
    assert notify.fire(repo, "death", "k") == "failed"
    assert time.monotonic() - started < 10
    assert "timed out after 1s" in logged(repo)[0]["detail"]["error"]
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("the command's child outlived the timeout")


def test_a_ledger_that_cannot_be_written_does_not_repeat_the_command(repo, fake, monkeypatch):
    def broken(path, record):
        raise OSError("disk full")

    monkeypatch.setattr(orca_cli, "_append", broken)
    seen: set[str] = set()
    assert notify.fire(repo, "death", "k", seen=seen) == "sent"
    assert notify.fire(repo, "death", "k", seen=seen) == "already-sent"
    assert len(fake()) == 1


# --- the watcher calls it -------------------------------------------------------------------------------------------------

def test_a_death_notifies_once(repo, monkeypatch, fake):
    Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": WORKING})
    Terminals(monkeypatch).kind["term_task_1"] = "gone"
    Escalations(monkeypatch)
    watcher.watch(repo, RUN, scans=4, sleep=lambda s: None, out=lambda line: None)
    runs = fake()
    assert len(runs) == 1
    assert runs[0]["CODING_AGENT_NOTIFY_KIND"] == "death" and runs[0]["CODING_AGENT_NOTIFY_TASK_ID"] == "task_1"
    assert runs[0]["CODING_AGENT_NOTIFY_TITLE"] == "Viec task_1" and "terminal is gone" in runs[0]["CODING_AGENT_NOTIFY_REASON"]


def test_a_death_orca_refused_still_notifies_once(repo, monkeypatch, fake):
    Workers(repo, monkeypatch, {"task_1": IDLE})
    Terminals(monkeypatch).kind["term_task_1"] = "gone"
    Escalations(monkeypatch).fail = True
    watcher.watch(repo, RUN, scans=5, sleep=lambda s: None, out=lambda line: None)
    assert len(fake()) == 1


def test_a_stuck_worker_notifies_once_per_episode(repo, tmp_path, monkeypatch, fake):
    now = [100_000.0]
    monkeypatch.setattr(resume.time, "time", lambda: now[0])
    Workers(repo, monkeypatch, {"task_1": WORKING})
    Escalations(monkeypatch)
    monkeypatch.setattr(orca_cli, "worker_activity", lambda run=None: [])
    path = transcript_for(repo, tmp_path, written=now[0] - 1300)
    watcher.watch(repo, RUN, scans=3, sleep=lambda s: None, out=lambda line: None)
    assert [run["CODING_AGENT_NOTIFY_KIND"] for run in fake()] == ["stuck"]
    assert "signal: transcript" in fake()[0]["CODING_AGENT_NOTIFY_REASON"]
    os.utime(path, (now[0], now[0]))
    watcher.watch(repo, RUN, scans=1, sleep=lambda s: None, out=lambda line: None)
    now[0] += 1300
    watcher.watch(repo, RUN, scans=2, sleep=lambda s: None, out=lambda line: None)
    assert len(fake()) == 2


def test_the_watchers_own_failure_notifies_once_even_when_orca_is_down(repo, monkeypatch, fake):
    failing_scan(monkeypatch, [ValueError("broken") for _ in range(9)])
    Escalations(monkeypatch).fail = True
    watcher.watch(repo, RUN, scans=9, sleep=lambda s: None, out=lambda line: None)
    runs = fake()
    assert len(runs) == 1 and runs[0]["CODING_AGENT_NOTIFY_KIND"] == "watch-failing"
    assert "ValueError: broken" in runs[0]["CODING_AGENT_NOTIFY_REASON"]


@pytest.mark.parametrize("trouble", ["exit", "missing"])
def test_a_broken_command_does_not_stop_the_watcher(repo, tmp_path, monkeypatch, fake, trouble):
    if trouble == "exit":
        monkeypatch.setenv("FAKE_NOTIFY_EXIT", "9")
    else:
        write_manifest(repo, {"mode": "on", "command": [str(tmp_path / "gone")]})
    Workers(repo, monkeypatch, {"task_1": IDLE, "task_2": WORKING})
    Terminals(monkeypatch).kind["term_task_1"] = "gone"
    escalations = Escalations(monkeypatch)
    said: list[str] = []
    assert watcher.watch(repo, RUN, scans=4, sleep=lambda s: None, out=said.append) == 0
    assert len(escalations.sent) == 1
    assert not any("scan failed" in line for line in said)
    assert any(row["kind"] == "notify-failed" for row in logged(repo))


# --- the README's examples --------------------------------------------------------------------------------------------------

def readme_blocks() -> list[dict]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    found = [yaml.safe_load(re.sub(r"^  ", "", body, flags=re.M)) for body in re.findall(r"```yaml\n(.*?)```", text, flags=re.S) if "notify:" in body]
    return [block["notify"] for block in found]


def test_the_readme_examples_are_valid_blocks():
    blocks = readme_blocks()
    assert [block["command"][0] for block in blocks] == ["osascript", "curl"]
    for block in blocks:
        assert manifest.notify_from(block).mode == "on"
