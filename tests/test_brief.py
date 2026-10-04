"""The brief an agent receives for one PLAN task (SC-005): Global constraints, then that task's Files and Interfaces."""

import pytest

from coding_agent import cli
from coding_agent.brief import BriefError, brief

PLAN = """# PLAN

## Global constraints
- Không dùng đường dẫn tuyệt đối.
- Chạy test trước khi báo xong.

## Plan
### Task 1 — Manifest
**Files:** `src/coding_agent/manifest.py`, `tests/test_manifest_gen.py`
**Interfaces:**
```python
def load(path: Path) -> Manifest
```
**Verify:** run the manifest tests

### Task 2 — Generator
**Files:** `src/coding_agent/gen.py`
**Interfaces:**
```python
def render(manifest: Manifest) -> dict
```
**Verify:** run the generator tests
"""


def test_the_brief_carries_the_constraints_and_only_its_own_files_and_interfaces():
    text = brief(PLAN, 2)
    assert "## Global constraints" in text and "- Không dùng đường dẫn tuyệt đối." in text
    assert "`src/coding_agent/gen.py`" in text and "def render(manifest: Manifest) -> dict" in text
    assert "manifest.py" not in text and "**Verify:**" not in text


def test_a_missing_task_is_an_error():
    with pytest.raises(BriefError, match="no Task 9"):
        brief(PLAN, 9)


def test_a_plan_without_constraints_is_an_error():
    with pytest.raises(BriefError, match="Global constraints"):
        brief("### Task 1 — x\n**Files:** `a.py`\n", 1)


def test_the_cli_prints_the_brief(tmp_path, capsys):
    plan = tmp_path / "PLAN.md"
    plan.write_text(PLAN, encoding="utf-8")
    assert cli.main(["brief", "--plan", str(plan), "--task", "1"]) == 0
    assert "`src/coding_agent/manifest.py`" in capsys.readouterr().out
    assert cli.main(["brief", "--plan", str(plan), "--task", "7"]) == 1
