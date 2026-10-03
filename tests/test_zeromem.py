import json
from pathlib import Path

import pytest

from conftest import requires_zm
from coding_agent.memory import zeromem

pytestmark = requires_zm


def sample(session: str, base_uuid: str) -> list[dict]:
    return [
        {"session_id": session, "speaker": "user", "text": "Retries use jittered backoff, cap 30s.", "ts": 1790000200, "uuid": f"{base_uuid}:1"},
        {"session_id": session, "speaker": "assistant", "text": "Noted: jittered backoff with a 30s cap.", "ts": 1790000210, "uuid": f"{base_uuid}:2"},
    ]


def test_store_is_per_project_root(tmp_path: Path, repo: Path, zm_home: Path):
    other = tmp_path / "other-repo"
    other.mkdir()
    a = zeromem.store_for(repo, embedder="hash", base=zm_home)
    b = zeromem.store_for(other, embedder="hash", base=zm_home)
    assert a.home != b.home and a.home.parent == zm_home / "workspaces"


def test_spooled_records_are_recalled_and_duplicates_do_not_double_count(repo: Path, zm_home: Path):
    store = zeromem.store_for(repo, embedder="hash", base=zm_home)
    zeromem.spool(store, sample("sA", "coding-agent:sA"))
    found = zeromem.recall(store, "backoff cap", top_k=3)
    assert {e["speaker"] for e in found} == {"user", "assistant"}
    zeromem.spool(store, sample("sA", "coding-agent:sA"))
    assert zeromem.stats(store)["turns"] == 2
    zeromem.forget_session(store, "sA")


def test_recall_can_leave_the_current_session_out(repo: Path, zm_home: Path):
    store = zeromem.store_for(repo, embedder="hash", base=zm_home)
    zeromem.spool(store, sample("sB", "coding-agent:sB"))
    assert zeromem.recall(store, "backoff", exclude_session="sB") == []
    zeromem.forget_session(store, "sB")


def test_forget_removes_the_session(repo: Path, zm_home: Path):
    store = zeromem.store_for(repo, embedder="hash", base=zm_home)
    zeromem.spool(store, sample("sC", "coding-agent:sC"))
    assert zeromem.stats(store)["turns"] == 2
    assert zeromem.forget_session(store, "sC")["deleted_turns"] == 2
    assert zeromem.stats(store)["turns"] == 0


def test_spool_files_appear_whole_and_never_as_temporary_names(repo: Path, zm_home: Path):
    store = zeromem.store_for(repo, embedder="hash", base=zm_home)
    final = zeromem.spool(store, sample("sD", "coding-agent:sD"))
    assert final is not None and final.suffix == ".jsonl" and not list(final.parent.glob(".spool-*"))
    lines = [json.loads(line) for line in final.read_text(encoding="utf-8").splitlines()]
    assert [r["uuid"] for r in lines] == ["coding-agent:sD:1", "coding-agent:sD:2"]
    zeromem.forget_session(store, "sD")


def test_an_empty_spool_writes_nothing(repo: Path, zm_home: Path):
    store = zeromem.store_for(repo, embedder="hash", base=zm_home)
    assert zeromem.spool(store, []) is None


def test_a_missing_binary_is_a_clear_error(repo: Path, zm_home: Path, monkeypatch):
    store = zeromem.store_for(repo, embedder="hash", base=zm_home, zm="/nonexistent/zm")
    with pytest.raises(zeromem.ZeromemError, match="cannot run"):
        zeromem.stats(store)


def test_the_default_embedder_needs_a_model_directory(repo: Path, zm_home: Path, monkeypatch):
    monkeypatch.delenv("CODING_AGENT_ZM_MODELS", raising=False)
    store = zeromem.store_for(repo, embedder="default", base=zm_home)
    with pytest.raises(zeromem.ZeromemError, match="CODING_AGENT_ZM_MODELS"):
        zeromem.ensure_models(store)
