"""The agent's Zero-Mem store, reached through the real `zm` binary.

Protocol, matching `packages/experimental/memory-zeromem` in DeepSeek Harness:

- Records go into `<home>/spool/*.jsonl` as `{session_id, speaker, text, ts, uuid}`. A complete
  file appears atomically (written under a temporary name, then renamed to `*.jsonl`).
- `zm mcp --home <home>` drains the spool before it answers a tool call, and skips any record
  whose `uuid` it already stored. Re-spooling the same turn therefore never duplicates it.
- Reads go through JSON-RPC over stdio: initialize, notifications/initialized, tools/call.

Each store is one directory per project root, so two repositories never share recall.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ZM_ENV = "CODING_AGENT_ZM"
ZM_FALLBACK_ENV = "DSH_ZEROMEM_ZM"
HOME_ENV = "CODING_AGENT_ZEROMEM_HOME"
MODELS_ENV = "CODING_AGENT_ZM_MODELS"
DEFAULT_TIMEOUT_S = 120


class ZeromemError(RuntimeError):
    """zm could not answer; the message carries its stderr tail."""


@dataclass(frozen=True)
class Store:
    home: Path
    zm: str
    no_model: bool


def base_home() -> Path:
    """Where stores live: `CODING_AGENT_ZEROMEM_HOME`, else `~/.coding-agent/zeromem`."""
    configured = os.environ.get(HOME_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".coding-agent" / "zeromem"


def zm_binary() -> str:
    return os.environ.get(ZM_ENV) or os.environ.get(ZM_FALLBACK_ENV) or "zm"


def store_for(root: Path, *, embedder: str, base: Path | None = None, zm: str | None = None) -> Store:
    """The store of one project root. `embedder: hash` runs zm with `--no-model`."""
    key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    home = (base or base_home()) / "workspaces" / key
    return Store(home=home, zm=zm or zm_binary(), no_model=embedder == "hash")


def ensure_models(store: Store) -> None:
    """For the default embedder, link the model directory into the store (zm loads it from `<home>/models`)."""
    if store.no_model:
        return
    source = os.environ.get(MODELS_ENV)
    if not source:
        raise ZeromemError(f"embedder default needs {MODELS_ENV} to point at the model directory")
    link = store.home / "models"
    if link.is_symlink() and os.readlink(link) == source:
        return
    link.unlink(missing_ok=True)
    link.symlink_to(source, target_is_directory=True)


def spool(store: Store, records: list[dict[str, Any]]) -> Path | None:
    """Write one spool file with the given records. Empty input writes nothing."""
    if not records:
        return None
    directory = store.home / "spool"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    name = f"{time.time_ns():020d}-{os.getpid()}-ca.jsonl"
    final = directory / name
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".spool-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(body)
        os.replace(tmp, final)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return final


def call(store: Store, tool: str, arguments: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """One MCP tool call. Returns the JSON document zm put in the tool result text."""
    argv = [store.zm]
    if store.no_model:
        argv.append("--no-model")
    argv += ["mcp", "--home", str(store.home)]
    requests = "".join(json.dumps(message) + "\n" for message in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "coding-agent", "version": "0.1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
    ])
    try:
        proc = subprocess.run(argv, input=requests, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ZeromemError(f"cannot run {store.zm}: {error}") from error
    answer = None
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 2:
            answer = message
    if answer is None:
        raise ZeromemError(f"zm gave no answer to {tool}: {proc.stderr.strip()[-400:]}")
    if "error" in answer:
        raise ZeromemError(f"{tool}: {answer['error']}")
    result = answer.get("result", {})
    if result.get("isError"):
        raise ZeromemError(f"{tool} reported an error")
    text = result["content"][0]["text"]
    return json.loads(text)


def recall(store: Store, query: str, *, top_k: int = 5, exclude_session: str | None = None) -> list[dict[str, Any]]:
    """Stored records that match `query`, best first. `exclude_session` leaves one session out."""
    arguments: dict[str, Any] = {"query": query, "top_k": top_k}
    if exclude_session:
        arguments["exclude_session"] = exclude_session
    return list(call(store, "zeromem_recall", arguments).get("evidence", []))


def stats(store: Store) -> dict[str, Any]:
    return call(store, "zeromem_stats", {})


def forget_session(store: Store, session_id: str) -> dict[str, Any]:
    """Delete one session's records from the store."""
    return call(store, "zeromem_forget_session", {"session_id": session_id})
