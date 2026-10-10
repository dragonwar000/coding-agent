"""The handlers the manifest binds: orca-guard, loop-guard, prompt-reset, stop-gate, pre-compact, session-restore.

Each handler returns a HookResult. Blocking is exit 2 with the reason on stderr, and happens only
when the hook's mode is `enforce`. Every decision is logged with `applied` (FR-003).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from coding_agent import coordinator, events, orca, state, transcript
from coding_agent.hooks import Context, HookResult
from coding_agent.manifest import Loop
from coding_agent.memory import record, zeromem

TASK_UPDATE = re.compile(r"orca\s+orchestration\s+task-update\b")
TASK_CREATE = re.compile(r"orca\s+orchestration\s+task-create\b")
STATUS_ARG = re.compile(r"--status[=\s]+([^\s'\"]+)")
WRITE_TOOLS_FOR_LOOP = ("Write", "Edit", "MultiEdit", "Bash")
TAIL_CHARS = 2000
RESTORE_PROMPTS = 3
RESTORE_CHARS = 1000


def _inject(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, ensure_ascii=False)


def orca_guard(ctx: Context) -> HookResult:
    """Block `task-update --status` with a value outside the enum; hint about `result.task.id` after `task-create`."""
    if ctx.payload.get("tool_name") != "Bash":
        return HookResult()
    command = str((ctx.payload.get("tool_input") or {}).get("command") or "")

    if TASK_UPDATE.search(command):
        match = STATUS_ARG.search(command)
        if match and match.group(1) not in orca.ORCA_STATUSES:
            reason = (
                f"[orca-guard] '--status {match.group(1)}' is not a valid Orca status; Orca would answer ok:false silently. "
                f"Valid statuses: {', '.join(sorted(orca.ORCA_STATUSES))}."
            )
            blocked = ctx.mode == "enforce"
            ctx.note(guard="orca-guard", kind="invalid-status", applied=blocked, detail={"status": match.group(1)})
            if blocked:
                return HookResult(code=2, stderr=reason)
            return HookResult()

    if TASK_CREATE.search(command):
        ctx.note(guard="orca-guard", kind="task-create-hint", applied=False)
        return HookResult(stdout=_inject(
            "PreToolUse",
            "[orca-guard] task-create returns two ids: the envelope id (a uuid) and result.task.id (task_xxxx). "
            "Use result.task.id in every later command, never the envelope id.",
        ))
    return HookResult()


def _signature(tool: str, tool_input: Any) -> str:
    canonical = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{tool}\n{canonical}".encode("utf-8")).hexdigest()


def decide_loop(loop: Loop, count: int, calls_this_turn: int, denials: int = 0) -> str | None:
    """`stop`, `remind`, or None for one tool call. Zero thresholds are off."""
    if loop.max_tool_calls_per_turn and calls_this_turn > loop.max_tool_calls_per_turn:
        return "stop-budget"
    if loop.max_denials_per_turn and denials >= loop.max_denials_per_turn:
        return "stop-denials"
    if loop.stop_at and count >= loop.stop_at:
        return "stop-repeat"
    if loop.remind_at and count == loop.remind_at:
        return "remind-repeat"
    return None


def loop_guard(ctx: Context) -> HookResult:
    """Count identical tool calls within a turn. Remind at `remind_at`, stop at `stop_at` or past the turn budget."""
    if ctx.manifest is None or ctx.payload.get("tool_name") not in WRITE_TOOLS_FOR_LOOP:
        return HookResult()
    tool = str(ctx.payload.get("tool_name"))
    data = state.load(ctx.root, ctx.session)
    counts: dict[str, int] = data.get("sig_counts") or {}
    calls = int(data.get("calls_this_turn", 0)) + 1
    signature = _signature(tool, ctx.payload.get("tool_input"))
    count = counts.get(signature, 0) + 1
    counts[signature] = count
    data["sig_counts"] = counts
    data["calls_this_turn"] = calls
    state.save(ctx.root, ctx.session, data)

    denials = _denials(ctx) if ctx.manifest.loop.max_denials_per_turn else 0
    decision = decide_loop(ctx.manifest.loop, count, calls, denials)
    if decision is None:
        return HookResult()
    enforce = ctx.mode == "enforce"
    ctx.note(guard="loop-guard", kind=decision, applied=enforce, detail={"tool": tool, "repeats": count, "calls_this_turn": calls, "denials": denials})
    if decision == "stop-denials" and enforce:
        return HookResult(code=2, stderr=f"[loop-guard] {denials} tool calls in this turn were refused or failed. Stop retrying the refused action and report what is blocked.")
    if decision.startswith("stop") and enforce:
        return HookResult(code=2, stderr=f"[loop-guard] {tool} has run {count} times with identical input this turn ({calls} tool calls). Change the approach or finish with the evidence already gathered.")
    if decision.startswith("remind") and enforce:
        return HookResult(stdout=_inject("PreToolUse", f"[loop-guard] {tool} with the same input has run {count} times. Check the last result before repeating it."))
    return HookResult()


def _denials(ctx: Context) -> int:
    """Error tool results in the current turn, read from the transcript the host passes in the payload."""
    path = ctx.payload.get("transcript_path")
    if not path or ctx.manifest is None:
        return 0
    return transcript.read_turn(_path(path), ctx.manifest.memory.change_tools).denials


def prompt_reset(ctx: Context) -> HookResult:
    """A new human prompt starts a new turn: counters reset, and the prompt is kept for seven days (FR-022 retention)."""
    data = state.load(ctx.root, ctx.session)
    prompt = str(ctx.payload.get("prompt") or "").strip()
    prompts = list(data.get("prompts") or [])
    if prompt:
        prompts.append({"ts": int(time.time()), "text": prompt[:4000]})
    data = state.expire_prompts({**data, "prompts": prompts[-20:]})
    data.update({"sig_counts": {}, "calls_this_turn": 0, "continuations": 0})
    # Taken for every role: stop-gate compares against it to tell a turn that changed nothing.
    data["dirty_at_prompt"] = coordinator.dirty_paths(ctx.root)
    data["tree_at_prompt"] = coordinator.tree_fingerprint(ctx.root)
    state.save(ctx.root, ctx.session, data)
    return HookResult()


def _verify(commands: tuple[str, ...], cwd: str, timeout: int) -> list[dict[str, Any]]:
    """Run the verification commands in order and stop at the first failure. A timeout counts as a failure."""
    checks: list[dict[str, Any]] = []
    for command in commands:
        try:
            run = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
            output = (run.stdout + run.stderr)[-TAIL_CHARS:]
            checks.append({"command": command, "exit": run.returncode, "output": output})
        except subprocess.TimeoutExpired as error:
            tail = ((error.stdout or b"").decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or ""))[-TAIL_CHARS:]
            checks.append({"command": command, "exit": None, "output": tail + "\n[timed out]"})
        if checks[-1]["exit"] != 0:
            break
    return checks


def _turn_changed(ctx: Context, turn: transcript.Turn, data: dict[str, Any], has_transcript: bool) -> bool:
    """Whether the turn changed files. Unknown counts as changed, so verification is never skipped on a guess.

    Evidence, in order: a successful change tool in the transcript; the dirty paths of the root against the
    snapshot prompt-reset took; the tree fingerprint against its snapshot, which also sees a change committed
    during the turn and a second edit of an already dirty file. Outside git, a readable transcript with no
    change is the only evidence.
    """
    if turn.changes:
        return True
    now = coordinator.dirty_paths(ctx.root)
    before = data.get("dirty_at_prompt")
    if now is not None:
        if not isinstance(before, list):
            return True
        # The harness writes its own state during the turn; that is not a change the turn made.
        if {p for p in now if not p.startswith(".coding-agent")} != {p for p in before if not p.startswith(".coding-agent")}:
            return True
        fingerprint = data.get("tree_at_prompt")
        return not isinstance(fingerprint, str) or coordinator.tree_fingerprint(ctx.root) != fingerprint
    return not has_transcript


def _changed_repos(root: Path, paths: list[str]) -> list[Path]:
    """The git repositories under `root` that hold `paths`: the nearest directory with a `.git`, up to `root`."""
    root = root.resolve()
    repos: set[Path] = set()
    for value in paths:
        target = Path(value)
        target = (target if target.is_absolute() else root / target).resolve()
        if root not in target.parents:
            continue
        for parent in target.parents:
            if (parent / ".git").exists():
                repos.add(parent)
                break
            if parent == root:
                break
    return sorted(repos)


def _verify_turn(ctx: Context, turn: transcript.Turn) -> list[dict[str, Any]]:
    """Run the verification commands where the manifest's scope says: the root, or each changed repository."""
    manifest = ctx.manifest
    assert manifest is not None
    places = _changed_repos(ctx.root, list(turn.changes)) if manifest.verify_scope == "changed-repos" else []
    if not places:
        return _verify(manifest.verify_commands, str(ctx.root), manifest.verify_timeout_s)
    checks: list[dict[str, Any]] = []
    for place in places:
        found = _verify(manifest.verify_commands, str(place), manifest.verify_timeout_s)
        label = place.relative_to(ctx.root.resolve()).as_posix()
        checks.extend({**check, "command": f"{check['command']} (in {label})"} for check in found)
        if found and found[-1]["exit"] != 0:
            break
    return checks


def stop_gate(ctx: Context) -> HookResult:
    """Verify at the end of a turn; in enforce mode a failing turn continues up to the budget; record the turn to memory."""
    if ctx.manifest is None:
        ctx.note(guard="stop-gate", kind="no-manifest", applied=False)
        return HookResult()
    manifest = ctx.manifest
    path = ctx.payload.get("transcript_path")
    turn = transcript.read_turn(_path(path), manifest.memory.change_tools) if path else transcript.Turn()
    lines = _line_count(path)
    data = state.load(ctx.root, ctx.session)

    if not manifest.verify_commands:
        checks, verdict = [], "skipped"
    elif manifest.verify_when == "changed" and not _turn_changed(ctx, turn, data, bool(path)):
        checks, verdict = [], "no-change"
    else:
        checks = _verify_turn(ctx, turn)
        verdict = "ok" if checks and all(c["exit"] == 0 for c in checks) else "not-ok"

    continuations = int(data.get("continuations", 0))
    result = HookResult()

    if verdict == "not-ok":
        failing = next((c for c in checks if c["exit"] != 0), checks[-1] if checks else {"command": "", "output": ""})
        reason = f"[stop-gate] verification failed: {failing['command']}\n{failing['output']}"
        if ctx.mode == "enforce" and continuations < manifest.max_continuations:
            data["continuations"] = continuations + 1
            state.save(ctx.root, ctx.session, data)
            ctx.note(guard="stop-gate", kind="blocked", applied=True, detail={"command": failing["command"], "continuation": continuations + 1})
            return HookResult(code=2, stderr=reason)
        kind = "budget-exhausted" if ctx.mode == "enforce" else "not-ok"
        ctx.note(guard="stop-gate", kind=kind, applied=False, detail={"command": failing["command"], "continuations": continuations})
    else:
        if continuations:
            data["continuations"] = 0
            state.save(ctx.root, ctx.session, data)
        ctx.note(guard="stop-gate", kind=verdict, applied=False, detail={"commands": len(checks)})

    if manifest.loop.max_denials_per_turn and turn.denials >= manifest.loop.max_denials_per_turn:
        ctx.note(guard="stop-gate", kind="denial-budget", applied=False, detail={"denials": turn.denials, "budget": manifest.loop.max_denials_per_turn})

    _note_direct_changes(ctx, data)
    _remember(ctx, turn, verdict, lines)
    return result


def _note_direct_changes(ctx: Context, data: dict[str, Any]) -> None:
    """Record files the coordinator's tree gained during the turn while the graph was mandatory.

    Detection only: a change is already made when the turn ends. It also counts files the user changed by hand in that time.
    """
    guard = ctx.manifest.hook("coordinator-guard") if ctx.manifest is not None else None
    before = data.get("dirty_at_prompt")
    if guard is None or guard.mode != "enforce" or not isinstance(before, list) or coordinator.role_for(_cwd(ctx)) != "coordinator":
        return
    now = coordinator.dirty_paths(ctx.root)
    new = sorted(set(now or []) - set(before))
    if new:
        ctx.note(guard="coordinator-guard", kind="direct-change", applied=False, detail={"paths": new[:20], "count": len(new)})


def pre_compact(ctx: Context) -> HookResult:
    """Record a compaction before the host drops context. The prompts already sit in the session state, so nothing is copied here."""
    data = state.load(ctx.root, ctx.session)
    ctx.note(guard="pre-compact", kind="compaction", applied=False, detail={"prompts": len(data.get("prompts") or [])})
    return HookResult()


def session_restore(ctx: Context) -> HookResult:
    """After a compaction, put the most recent prompts back into the new context (FR-022 restore)."""
    if ctx.payload.get("source") != "compact":
        return HookResult()
    prompts = [str(p.get("text", ""))[:RESTORE_CHARS] for p in (state.load(ctx.root, ctx.session).get("prompts") or [])[-RESTORE_PROMPTS:] if isinstance(p, dict)]
    if not prompts:
        ctx.note(guard="session-restore", kind="nothing-to-restore", applied=False)
        return HookResult()
    ctx.note(guard="session-restore", kind="restored", applied=False, detail={"prompts": len(prompts)})
    body = "\n".join(f"- {text}" for text in prompts)
    return HookResult(stdout=_inject("SessionStart", f"[session-restore] Context was compacted. The user's most recent prompts, oldest first:\n{body}"))


def _remember(ctx: Context, turn: transcript.Turn, verdict: str, lines: int) -> None:
    """Spool the turn to Zero-Mem. A memory failure is logged and never stops the session."""
    manifest = ctx.manifest
    if manifest is None or (turn.prompt is None and turn.response == ""):
        return
    try:
        store = zeromem.store_for(ctx.root, embedder=manifest.memory.embedder)
        zeromem.ensure_models(store)
        records = record.records_for_turn(ctx.session, turn, verdict_ok=verdict == "ok", verdict_line=lines, memory=manifest.memory)
        spooled = zeromem.spool(store, records)
        ctx.note(guard="memory", kind="spooled", applied=False, detail={"records": len(records), "file": spooled.name if spooled else None})
    except (OSError, zeromem.ZeromemError) as error:
        ctx.note(guard="memory", kind="memory-error", applied=False, detail={"error": str(error)[:400]})


def _path(value: Any):
    from pathlib import Path

    return Path(str(value)).expanduser()


def _line_count(value: Any) -> int:
    if not value:
        return 0
    try:
        return sum(1 for _ in _path(value).read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _cwd(ctx: Context) -> Path:
    return Path(str(ctx.payload.get("cwd") or ctx.root))


def coordinator_guard(ctx: Context) -> HookResult:
    """In the coordinator's session, block direct writes and mutating shell commands; workers are never affected.

    A maintainer session is let through, and each call the guard would have blocked is logged as `maintainer-allowed`.
    """
    role = coordinator.role_for(_cwd(ctx))
    if role not in ("coordinator", "maintainer"):
        return HookResult()
    tool = str(ctx.payload.get("tool_name") or "")
    question = coordinator.needs_user_confirmation(tool, ctx.payload.get("tool_input"), ctx.root)
    if question is not None:
        # Asked in every mode but `off`: this is the user's confirmation of a deletion, not a guard decision.
        ctx.note(guard="coordinator-guard", kind="confirm-requested", applied=True, detail={"tool": tool})
        return HookResult(stdout=json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask", "permissionDecisionReason": question,
        }}, ensure_ascii=False))
    python_src = ctx.manifest.python_src if ctx.manifest is not None else None
    reason = coordinator.guard_reason(tool, ctx.payload.get("tool_input"), ctx.root, python_src=python_src)
    if reason is None:
        return HookResult()
    if role == "maintainer":
        ctx.note(guard="coordinator-guard", kind="maintainer-allowed", applied=False, detail={"tool": tool})
        return HookResult()
    enforce = ctx.mode == "enforce"
    ctx.note(guard="coordinator-guard", kind="blocked" if enforce else "nudged", applied=enforce, detail={"tool": tool})
    if enforce:
        return HookResult(code=2, stderr=reason)
    return HookResult(stdout=_inject("PreToolUse", reason + " (shadow: chỉ nhắc, lệnh vẫn chạy.)"))


def coordinator_context(ctx: Context) -> HookResult:
    """Inject the coordinator contract at session start and the task board on every prompt. Workers get nothing."""
    if coordinator.role_for(_cwd(ctx)) != "coordinator" or ctx.manifest is None:
        return HookResult()
    event = str(ctx.payload.get("hook_event_name") or "SessionStart")
    board, error = coordinator.read_board(ctx.root)
    text = coordinator.board_context(board, error)
    if event == "SessionStart":
        text = coordinator.contract(ctx.manifest.python_src) + "\n" + text
    ctx.note(guard="coordinator-context", kind="injected", applied=False, detail={"event": event, "board_lines": len(board), "error": error})
    return HookResult(stdout=_inject(event, text))


REGISTRY: dict[str, Callable[[Context], HookResult]] = {
    "orca-guard": orca_guard,
    "loop-guard": loop_guard,
    "prompt-reset": prompt_reset,
    "stop-gate": stop_gate,
    "pre-compact": pre_compact,
    "session-restore": session_restore,
    "coordinator-guard": coordinator_guard,
    "coordinator-context": coordinator_context,
    "coordinator-board": coordinator_context,
}
