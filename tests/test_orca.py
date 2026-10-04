from pathlib import Path

import pytest

from coding_agent import orca


@pytest.mark.parametrize("requested, basis, expected", [
    ("completed", "predicate", ("completed", "basis predicate")),
    ("completed", "verifier", ("completed", "basis verifier")),
    ("completed", "human", ("completed", "basis human")),
    ("completed", "agentReported", ("unverified", "basis agentReported is not proof")),
    ("completed", None, ("unverified", "no basis was recorded")),
    ("completed", "guessed", ("unverified", "no basis was recorded")),
    ("dispatched", None, ("dispatched", "status is not a completion")),
])
def test_completion_needs_proof(requested, basis, expected):
    assert orca.completion(requested, basis) == expected


def test_project_name_comes_from_the_git_root(repo):
    nested = repo / "pkg"
    nested.mkdir()
    assert orca.project_name(nested) == "demo-repo"


def test_project_name_outside_git_uses_the_directory(tmp_path):
    folder = tmp_path / "plain-folder"
    folder.mkdir()
    assert orca.project_name(folder) == "plain-folder"


def test_claim_is_refused_across_repositories():
    task = {"id": "task_1", "project": "other-repo"}
    with pytest.raises(orca.ClaimRefused, match="belongs to other-repo"):
        orca.claim_allowed(task, "demo-repo")
    orca.claim_allowed({"id": "task_2", "project": "demo-repo"}, "demo-repo")


def test_a_task_without_project_is_refused_because_its_owner_is_unknown():
    with pytest.raises(orca.ClaimRefused, match="has no project"):
        orca.claim_allowed({"id": "task_3"}, "demo-repo")


def test_link_appends_the_durable_to_ephemeral_mapping(tmp_path):
    orca.link(tmp_path, t_id="T-261003-01", task_id="task_xyz", project="demo-repo")
    orca.link(tmp_path, t_id="T-261003-02", task_id="task_abc", project="demo-repo")
    lines = (tmp_path / ".coding-agent" / "links.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and '"T-261003-01"' in lines[0]


def test_claimed_done_but_absent_is_reported(tmp_path: Path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "ok.txt").write_text("x", encoding="utf-8")
    task = {"id": "task_9", "status": "completed", "artifacts": ["out/ok.txt", "out/missing.txt"]}
    findings = orca.check_artifacts(task, tmp_path)
    assert [(f.code, f.detail) for f in findings] == [("CLAIMED-DONE BUT ABSENT", "missing out/missing.txt")]
    assert orca.check_artifacts({**task, "status": "dispatched"}, tmp_path) == []


def test_reconcile_groups_without_changing_anything():
    tasks = [
        {"id": "a", "status": "dispatched", "project": "demo-repo"},
        {"id": "b", "status": "ready", "dispatch": None, "project": "demo-repo"},
        {"id": "c", "status": "blocked", "project": "demo-repo"},
        {"id": "d", "status": "completed", "basis": "agentReported", "project": "demo-repo"},
        {"id": "e", "status": "dispatched", "project": "other-repo"},
    ]
    groups = orca.reconcile(tasks, project="demo-repo")
    assert [t["id"] for t in groups["running"]] == ["a"]
    assert [t["id"] for t in groups["never_dispatched"]] == ["b"]
    assert [t["id"] for t in groups["stuck"]] == ["c"]
    assert [t["id"] for t in groups["unverified"]] == ["d"]
    assert tasks[0]["status"] == "dispatched"


def test_the_guard_status_enum_matches_the_gate():
    from coding_agent.hooks import handlers

    assert orca.ORCA_STATUSES == {"pending", "ready", "dispatched", "completed", "failed", "blocked"}
    assert handlers.TASK_UPDATE.search("orca orchestration task-update --id x --status done")


def test_a_basis_recorded_in_the_result_counts_as_proof():
    assert orca.basis_of({"basis": "predicate"}) == "predicate"
    assert orca.basis_of({"result": {"basis": "verifier"}}) == "verifier"
    assert orca.basis_of({"result": {"provenance": "worker_report", "outcome": "succeeded"}}) is None
    groups = orca.reconcile([
        {"id": "p", "status": "completed", "result": {"basis": "predicate"}, "project": "demo-repo"},
        {"id": "w", "status": "completed", "result": {"provenance": "worker_report"}, "project": "demo-repo"},
    ], project="demo-repo")
    assert [t["id"] for t in groups["unverified"]] == ["w"]
    assert [t["id"] for t in groups["other"]] == ["p"]
