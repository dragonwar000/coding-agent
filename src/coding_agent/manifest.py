"""Load and validate `integration.yaml`, the single source of every hook binding.

Misconfiguration fails at load, not at the first hook call: an unknown host, a duplicate
hook id, an absolute `python_src`, or a guard without an `assumption` raises ManifestError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HOSTS = ("claude_code", "codex")
MODES = ("off", "shadow", "enforce")
# A hook with no `mode` in the manifest runs as `shadow` (FR-010): it records decisions and blocks nothing.
DEFAULT_MODE = "shadow"
INGEST = ("conversation", "episodes", "both")
EMBEDDERS = ("hash", "default")


class ManifestError(ValueError):
    """The manifest is malformed; the message names the field to fix."""


@dataclass(frozen=True)
class Hook:
    id: str
    host: tuple[str, ...]
    event: str
    matcher: str | None
    mode: str | None
    assumption: str

    @property
    def env_name(self) -> str:
        return "CODING_AGENT_MODE_" + self.id.upper().replace("-", "_")


@dataclass(frozen=True)
class Memory:
    ingest: str
    embedder: str
    change_tools: tuple[str, ...]
    transient_markers: tuple[str, ...]
    max_episode_request_chars: int
    max_episode_outcome_chars: int
    max_episode_chars: int


@dataclass(frozen=True)
class Loop:
    remind_at: int
    stop_at: int
    max_tool_calls_per_turn: int
    max_denials_per_turn: int


@dataclass(frozen=True)
class Manifest:
    path: Path
    verified: bool
    python_src: str
    verify_commands: tuple[str, ...]
    verify_timeout_s: int
    max_continuations: int
    memory: Memory
    loop: Loop
    hooks: tuple[Hook, ...] = field(default_factory=tuple)

    @property
    def root(self) -> Path:
        """The repository root: the directory that holds the manifest."""
        return self.path.parent

    def hook(self, hook_id: str) -> Hook | None:
        return next((h for h in self.hooks if h.id == hook_id), None)


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ManifestError(f"{field_name} must be a non-blank string")
    return value


def _require_int(value: Any, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestError(f"{field_name} must be an integer >= {minimum}")
    return value


def _str_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ManifestError(f"{field_name} must be a list of non-blank strings")
    return tuple(value)


def _hook(item: Any, seen: set[str]) -> Hook:
    if not isinstance(item, dict):
        raise ManifestError("each hooks entry must be a mapping")
    hook_id = _require_str(item.get("id"), "hooks[].id")
    if hook_id in seen:
        raise ManifestError(f"duplicate hook id {hook_id!r}")
    seen.add(hook_id)
    host = _str_list(item.get("host"), f"hooks[{hook_id}].host")
    if not host or any(h not in HOSTS for h in host):
        raise ManifestError(f"hooks[{hook_id}].host must name only {', '.join(HOSTS)}")
    event = _require_str(item.get("event"), f"hooks[{hook_id}].event")
    matcher = item.get("matcher")
    if matcher is not None:
        matcher = _require_str(matcher, f"hooks[{hook_id}].matcher")
    mode = item.get("mode", DEFAULT_MODE)
    if mode is False:
        raise ManifestError(f"hooks[{hook_id}].mode is false: quote 'off' in YAML, since an unquoted off parses as false")
    if mode not in MODES:
        raise ManifestError(f"hooks[{hook_id}].mode must be one of {', '.join(MODES)}")
    assumption = item.get("assumption", "")
    if mode != "off" and not (isinstance(assumption, str) and assumption.strip()):
        raise ManifestError(f"hooks[{hook_id}] runs in mode {mode!r} and needs a non-blank assumption")
    return Hook(id=hook_id, host=host, event=event, matcher=matcher, mode=mode, assumption=str(assumption))


def load(path: Path) -> Manifest:
    """Parse and validate one manifest file. PyYAML is imported here so a hook can import this module without it."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ManifestError("schema must be 1")
    verified = raw.get("verified")
    if not isinstance(verified, bool):
        raise ManifestError("verified must be true or false; false marks the values as unconfirmed defaults")

    python_src = _require_str(raw.get("python_src"), "python_src")
    if python_src.startswith("/") or ".." in Path(python_src).parts:
        raise ManifestError("python_src must be a relative path inside the repository")

    verify = raw.get("verify") or {}
    verify_commands = _str_list(verify.get("commands", []), "verify.commands")
    verify_timeout = _require_int(verify.get("timeout_s", 300), "verify.timeout_s", 1)
    max_continuations = _require_int(verify.get("max_continuations", 3), "verify.max_continuations", 0)

    mem = raw.get("memory") or {}
    ingest = mem.get("ingest", "both")
    if ingest not in INGEST:
        raise ManifestError(f"memory.ingest must be one of {', '.join(INGEST)}")
    embedder = mem.get("embedder", "hash")
    if embedder not in EMBEDDERS:
        raise ManifestError(f"memory.embedder must be one of {', '.join(EMBEDDERS)}")
    change_tools = _str_list(mem.get("change_tools", ["Write", "Edit", "MultiEdit"]), "memory.change_tools")
    if not change_tools:
        raise ManifestError("memory.change_tools must name at least one tool")
    markers = _str_list(mem.get("transient_markers", []), "memory.transient_markers")
    memory = Memory(
        ingest=ingest,
        embedder=embedder,
        change_tools=change_tools,
        transient_markers=markers,
        max_episode_request_chars=_require_int(mem.get("max_episode_request_chars", 1000), "memory.max_episode_request_chars", 1),
        max_episode_outcome_chars=_require_int(mem.get("max_episode_outcome_chars", 2000), "memory.max_episode_outcome_chars", 1),
        max_episode_chars=_require_int(mem.get("max_episode_chars", 6000), "memory.max_episode_chars", 1),
    )

    from coding_agent.memory.episodes import fixed_chars

    if memory.max_episode_chars < fixed_chars():
        raise ManifestError(f"memory.max_episode_chars must be at least {fixed_chars()} so an episode keeps its identifiers and verdict")

    loop_raw = raw.get("loop") or {}
    loop = Loop(
        remind_at=_require_int(loop_raw.get("remind_at", 3), "loop.remind_at", 0),
        stop_at=_require_int(loop_raw.get("stop_at", 6), "loop.stop_at", 0),
        max_tool_calls_per_turn=_require_int(loop_raw.get("max_tool_calls_per_turn", 400), "loop.max_tool_calls_per_turn", 0),
        max_denials_per_turn=_require_int(loop_raw.get("max_denials_per_turn", 5), "loop.max_denials_per_turn", 0),
    )
    if loop.remind_at and loop.stop_at and loop.remind_at >= loop.stop_at:
        raise ManifestError("loop.remind_at must be less than loop.stop_at")

    seen: set[str] = set()
    hooks_raw = raw.get("hooks") or []
    if not isinstance(hooks_raw, list):
        raise ManifestError("hooks must be a list")
    hooks = tuple(_hook(item, seen) for item in hooks_raw)

    return Manifest(
        path=path,
        verified=verified,
        python_src=python_src,
        verify_commands=verify_commands,
        verify_timeout_s=verify_timeout,
        max_continuations=max_continuations,
        memory=memory,
        loop=loop,
        hooks=hooks,
    )
