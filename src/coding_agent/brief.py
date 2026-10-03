"""The brief an agent receives for one PLAN task (SC-005): Global constraints, then the task's Files and Interfaces.

The PLAN is markdown with a `## Global constraints` section and `### Task N` sections. A task's
`**Files:**` and `**Interfaces:**` blocks run from their label to the next `**` label or the next heading.
"""

from __future__ import annotations

import re

HEADING = re.compile(r"^(#{2,3}) (.+?)\s*$")
TASK_HEADING = re.compile(r"^Task (\d+)\b")
LABEL = re.compile(r"^\*\*(Files|Interfaces):\*\*")


class BriefError(ValueError):
    """The PLAN has no such task, or no Global constraints section."""


def _sections(text: str) -> list[tuple[str, str, list[str]]]:
    """(level, heading, body lines) for each `##` or `###` heading, in file order."""
    found: list[tuple[str, str, list[str]]] = []
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            found.append((match.group(1), match.group(2), []))
        elif found:
            found[-1][2].append(line)
    return found


def _block(body: list[str], label: str) -> list[str]:
    """The lines under one `**Label:**`, up to the next bold label."""
    out: list[str] = []
    active = False
    for line in body:
        if LABEL.match(line):
            active = line.startswith(f"**{label}:**")
            if active:
                out.append(line)
            continue
        if active:
            if line.startswith("**"):
                active = False
                continue
            out.append(line)
    return [line for line in out if line.strip()]


def brief(plan: str, task_no: int) -> str:
    """The brief text for one task. Raises BriefError when the PLAN lacks the task or its constraints."""
    sections = _sections(plan)
    constraints = next((body for level, heading, body in sections if level == "##" and heading.strip().lower() == "global constraints"), None)
    if constraints is None:
        raise BriefError("the PLAN has no '## Global constraints' section")
    for level, heading, body in sections:
        match = TASK_HEADING.match(heading)
        if level != "###" or not match or int(match.group(1)) != task_no:
            continue
        parts = ["## Global constraints", *[line for line in constraints if line.strip()], f"## Task {task_no}", *_block(body, "Files"), *_block(body, "Interfaces")]
        return "\n".join(parts) + "\n"
    raise BriefError(f"the PLAN has no Task {task_no}")
