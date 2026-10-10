"""Claude Code folder trust, shared from the coordinator with the workers it starts.

Claude Code keeps trust in its global config file, under `projects`: one entry per folder, keyed by the path with
forward slashes on Windows (`D:/a/b`), whose `hasTrustDialogAccepted` says whether the `Quick safety check` dialog
was accepted there. `config_path` picks the file the way Claude Code 2.1.296 does (read from its bundle on
2026-10-10): `<CLAUDE_CONFIG_DIR or ~/.claude>/.config.json` when that legacy file exists, otherwise
`<CLAUDE_CONFIG_DIR or home>/.claude.json` (`.claude-custom-oauth.json` with `CLAUDE_CODE_CUSTOM_OAUTH_URL`).

Measured on this machine: for a git worktree Claude Code checks trust at the main repository folder (the one holding
the shared `.git`), not at the worktree; a `False` entry on a nearer folder wins over a `True` entry on a parent
(`D:/work/poc` True with `D:/work/poc/platform` False leaves a repository inside `platform` untrusted); and `--permission-mode bypassPermissions` does not skip the dialog. `is_trusted`
follows that rule: the nearest folder with an entry that has `hasTrustDialogAccepted` decides.

Claude Code writes the same file while it runs, so `share_trust` reads it again right before writing, writes a
temporary file in the same folder and swaps it in with `os.replace`, keeps every other key, the UTF-8 encoding and
the JSON layout, and writes nothing when the file cannot be read or is not valid JSON (TrustError).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from coding_agent import events

SHARE_ENV = "CODING_AGENT_SHARE_TRUST"
FIELD = "hasTrustDialogAccepted"
# What Claude Code itself writes for a folder it trusts and has no entry for yet.
NEW_ENTRY = {"allowedTools": [], "mcpContextUris": [], "mcpServers": {}, "enabledMcpjsonServers": [], "disabledMcpjsonServers": [],
             "hasTrustDialogAccepted": True, "hasClaudeMdExternalIncludesApproved": False, "hasClaudeMdExternalIncludesWarningShown": False}
GUARD = "folder-trust"


class TrustError(RuntimeError):
    """The Claude Code config file could not be read, parsed, or replaced; nothing was written."""


def config_path() -> Path:
    """The global config file Claude Code reads trust from."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    legacy = Path(config_dir or Path.home() / ".claude") / ".config.json"
    if legacy.exists():
        return legacy
    suffix = "-custom-oauth" if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    return Path(config_dir or Path.home()) / f".claude{suffix}.json"


def sharing_enabled() -> bool:
    """False when `CODING_AGENT_SHARE_TRUST` is `off` (also `0`, `false`, `no`)."""
    return os.environ.get(SHARE_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def config_key(path: str | Path) -> str:
    """The `projects` key Claude Code uses for `path`: absolute, with forward slashes on Windows."""
    text = str(Path(path).resolve())
    return text.replace("\\", "/") if os.name == "nt" else text


def _same(key: str) -> str:
    key = key.replace("\\", "/") if os.name == "nt" else key
    return key.lower() if os.name == "nt" else key


def _load(config: Path) -> tuple[dict[str, Any], str]:
    """The parsed config and its raw text. Raises TrustError when it cannot be read or is not a JSON object."""
    try:
        raw = config.read_bytes()
    except OSError as error:
        raise TrustError(f"cannot read Claude Code config {config}: {error}") from error
    try:
        text = raw.decode("utf-8")
        data = json.loads(text.removeprefix("\ufeff"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustError(f"Claude Code config {config} is not valid UTF-8 JSON, so nothing was written: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        raise TrustError(f"Claude Code config {config} does not hold a JSON object with a `projects` object, so nothing was written")
    return data, text


def _entries(data: dict[str, Any]) -> dict[str, str]:
    """Normalized key -> key as written, for every `projects` entry."""
    return {_same(key): key for key in data.get("projects", {})}


def is_trusted(path: str | Path, config: Path | None = None) -> bool:
    """True when Claude Code would treat `path` as trusted: the nearest folder (itself, then each parent) that has an
    entry with `hasTrustDialogAccepted` decides. False when no folder decides or the config cannot be read."""
    try:
        data, _ = _load(config or config_path())
    except TrustError:
        return False
    projects = data.get("projects", {})
    keys = _entries(data)
    current = Path(config_key(path))
    while True:
        entry = projects.get(keys.get(_same(str(current)), ""))
        if isinstance(entry, dict) and FIELD in entry:
            return entry.get(FIELD) is True
        if current.parent == current:
            return False
        current = current.parent


def _layout(text: str) -> tuple[int | str | None, bool]:
    """The indent of the file's JSON (2 for Claude Code) and whether it ends with a newline."""
    body = text.removeprefix("\ufeff")
    lines = body.splitlines()
    indent: int | str | None = None
    if len(lines) > 1:
        lead = lines[1][: len(lines[1]) - len(lines[1].lstrip())]
        indent = ("\t" if lead.startswith("\t") else len(lead)) if lead else None
    return indent, body.endswith("\n")


def _write(config: Path, data: dict[str, Any], text: str) -> None:
    indent, newline = _layout(text)
    out = json.dumps(data, ensure_ascii=False, indent=indent) + ("\n" if newline else "")
    if text.startswith("\ufeff"):
        out = "\ufeff" + out
    handle, temp = tempfile.mkstemp(prefix=config.name + ".", suffix=".tmp", dir=config.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(out)
        for attempt in range(5):
            try:
                os.replace(temp, config)
                return
            except PermissionError:
                # Windows refuses the swap while another process has the file open; Claude Code holds it briefly.
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except OSError as error:
        raise TrustError(f"cannot replace Claude Code config {config}: {error}") from error
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def share_trust(coordinator_root: str | Path, paths: Iterable[str | Path], session: str | None = None,
                config: Path | None = None) -> list[str]:
    """Mark each of `paths` trusted, but only when `coordinator_root` is trusted itself. Returns the keys written.

    A missing entry is created with `hasTrustDialogAccepted: true`; an existing one keeps every other key. Each write
    is logged as `guard=folder-trust kind=shared` in the coordinator's event log, each refusal as `kind=skipped` with
    the reason. Raises TrustError, after logging it, when the config cannot be read, parsed, or replaced.
    """
    root = Path(coordinator_root)
    config = config or config_path()
    wanted = list(dict.fromkeys(config_key(path) for path in paths))

    def log(kind: str, **detail: Any) -> None:
        events.record(root, guard=GUARD, kind=kind, mode="enforce", applied=bool(detail.get("written")), session=session,
                      detail={"paths": wanted, "config": str(config), **detail})

    if not sharing_enabled():
        log("skipped", reason=f"{SHARE_ENV}=off")
        return []
    try:
        _load(config)
    except TrustError as error:
        log("skipped", reason=str(error))
        raise
    if not is_trusted(root, config):
        log("skipped", reason=f"the coordinator folder {config_key(root)} is not trusted in Claude Code")
        return []
    try:
        data, text = _load(config)  # read again right before writing: Claude Code may have saved meanwhile
        projects = data.setdefault("projects", {})
        keys = _entries(data)
        written = []
        for key in wanted:
            existing = keys.get(_same(key), key)
            entry = projects.get(existing)
            if isinstance(entry, dict) and entry.get(FIELD) is True:
                continue
            projects[existing] = {**entry, FIELD: True} if isinstance(entry, dict) else json.loads(json.dumps(NEW_ENTRY))
            written.append(existing)
        if written:
            _write(config, data, text)
    except TrustError as error:
        log("skipped", reason=str(error))
        raise
    log("shared", written=written)
    return written
