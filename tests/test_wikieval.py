"""`wikieval`: the wiki goldens eval suite. Defaults resolve against the project (`--root`), never against
where coding-agent is installed."""

import json
from pathlib import Path

import pytest

from coding_agent import cli
from coding_agent.wiki import evaluate

GOLDEN = """---
type: eval
id: {id}
input: "q"
expected: "{expected}"
asserts:
  - 'contains:Origin'
  - 'regex:(?i)required'
---
# {id}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    evals = tmp_path / "llmwiki" / "wiki" / "sources" / "evals"
    evals.mkdir(parents=True)
    (evals / "origin.md").write_text(GOLDEN.format(id="origin", expected="Origin section is required."), encoding="utf-8")
    (evals / "origin-2.md").write_text(GOLDEN.format(id="origin-2", expected="Origin is required here."), encoding="utf-8")
    (evals / "README.md").write_text("---\ninput: skipped\n---\n", encoding="utf-8")
    (tmp_path / "harness").mkdir()
    return tmp_path


def outputs(root: Path, mapping: dict) -> str:
    path = root / "outputs.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return str(path)


def run(root: Path, *args: str) -> int:
    return cli.main(["wikieval", "--root", str(root), *args])


GOOD = {"origin": "The Origin section is required.", "origin-2": "Origin: required."}


def test_baseline_is_written_inside_the_project(project, capsys):
    assert run(project, "--outputs", outputs(project, GOOD), "--write-baseline") == 0
    baseline = json.loads((project / "harness" / "metrics" / "eval-baseline.json").read_text(encoding="utf-8"))
    assert baseline["schema"] == "wikieval-baseline/v1"
    assert baseline["evals_dir"] == "llmwiki/wiki/sources/evals"
    assert baseline["summary"] == {"goldens": 2, "decided": 2, "passed": 2}
    assert baseline["goldens"]["origin"]["asserts"] == ["contains:Origin", "regex:(?i)required"]
    assert "PASS  origin" in capsys.readouterr().out


def test_check_passes_then_fails_on_a_regression(project, capsys):
    assert run(project, "--outputs", outputs(project, GOOD), "--write-baseline") == 0
    assert run(project, "--outputs", outputs(project, GOOD), "--check") == 0
    assert "OK — no metric below baseline" in capsys.readouterr().out
    broken = {**GOOD, "origin": "nothing useful"}
    assert run(project, "--outputs", outputs(project, broken), "--check") == 2
    assert "FAIL  origin: pass True -> False" in capsys.readouterr().out


def test_check_without_baseline_is_a_usage_error(project, capsys):
    assert run(project, "--outputs", outputs(project, GOOD), "--check") == 1
    assert "no baseline" in capsys.readouterr().err


def test_baseline_in_an_unknown_schema_fails_closed(project, capsys):
    path = project / "harness" / "metrics" / "eval-baseline.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"per_golden": {}}), encoding="utf-8")
    assert run(project, "--outputs", outputs(project, GOOD), "--check") == 1
    assert "goldens" in capsys.readouterr().err


def test_empty_run_never_overwrites_a_real_baseline(project, capsys):
    assert run(project, "--outputs", outputs(project, GOOD), "--write-baseline") == 0
    path = project / "harness" / "metrics" / "eval-baseline.json"
    before = path.read_text(encoding="utf-8")
    assert run(project, "--write-baseline") == 1
    assert "TỪ CHỐI" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_self_test_checks_goldens_against_their_own_expected(project, capsys):
    assert run(project, "--self-test") == 0
    assert "SELFTEST: ALL PASS" in capsys.readouterr().out
    (project / "llmwiki" / "wiki" / "sources" / "evals" / "bad.md").write_text(
        GOLDEN.format(id="bad", expected="no such word"), encoding="utf-8")
    assert run(project, "--self-test") == 1


def test_self_test_without_goldens_uses_a_hermetic_fixture(tmp_path, capsys):
    assert run(tmp_path, "--self-test") == 0
    assert "hermetic engine-fixture" in capsys.readouterr().out


def test_fdk_wiki_wins_over_llmwiki(tmp_path):
    (tmp_path / "llmwiki" / "wiki" / "sources" / "evals").mkdir(parents=True)
    (tmp_path / "fdk" / "wiki" / "sources" / "evals").mkdir(parents=True)
    assert evaluate._default_evals_dir(tmp_path) == tmp_path / "fdk" / "wiki" / "sources" / "evals"


def test_duplicate_golden_ids_are_refused(project, capsys):
    (project / "llmwiki" / "wiki" / "sources" / "evals" / "dup.md").write_text(
        GOLDEN.format(id="origin", expected="Origin required"), encoding="utf-8")
    assert run(project, "--self-test") == 1
    assert "duplicate golden id" in capsys.readouterr().err


@pytest.mark.parametrize(("spec", "output", "passed"), [
    ("equals: bar ", "bar", True),
    ("contains:Foo", "a Foo b", True),
    ("icontains:foo", "a FOO b", True),
    ("not-contains:foo", "bar", True),
    ("regex:^a:b$", "a:b", True),
    ("regex:(", "anything", False),
    ("is-json", '{"a": 1}', True),
    ("is-json", "{nope", False),
    ("is-sql-ish", "SELECT a FROM t", True),
    ("is-sql-ish", "select me please", False),
    ("bogus:x", "x", False),
])
def test_tier1_asserts(spec, output, passed):
    assert evaluate.eval_assert(spec, output)[0] is passed


def test_golden_without_asserts_needs_the_judge_unless_difflib_is_configured(project):
    golden = {"id": "g", "expected": "hello world", "asserts": []}
    assert evaluate.run_golden(golden, "hello world", {})["status"] == "needs-judge"
    result = evaluate.run_golden(golden, "hello world", {"embedding": {"backend": "difflib", "threshold": 0.8}})
    assert result["pass"] is True and result["decided_by"] == "tier2-lexical-difflib"
