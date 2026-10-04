"""Install coding-agent into a project the way the setup harness does: one command, detected vendors, merged wiring, verify.

`python3 -m coding_agent.project_install --project DIR` does, in order:

1. Checks PyYAML for this interpreter, and tries `pip install pyyaml` when it is missing.
2. Detects the host vendors (`.claude/` → Claude Code, `.codex/` or `AGENTS.md` → Codex; default Claude Code),
   or takes `--vendor claude,codex`.
3. Copies this package to `DIR/<python_src>/coding_agent`. An existing copy is replaced only when it still
   carries the harness marker (FR-004), and a backup is kept.
4. Writes `integration.yaml` only when the project has none. An existing manifest is never overwritten.
5. Merges the harness hooks into the host files (`.claude/settings.json`, `.codex/hooks.json`), keeping the
   user's own hooks and settings, with a `.bak` of each file it changes.
6. Writes a CI gate at `.github/workflows/coding-agent.yml` when none exists.
7. Verifies the wiring: the gate, and the hook commands from the host file run against sample payloads.

`--uninstall` reverses 2 to 6 and keeps `integration.yaml` (the user's own file). `--clean` uninstalls, then installs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import coding_agent
from coding_agent import gate, gen
from coding_agent import install as safe_install
from coding_agent.manifest import ManifestError, load

PACKAGE = Path(coding_agent.__file__).resolve().parent
TEMPLATE = PACKAGE / "template.yaml"
DEFAULT_PYTHON_SRC = "harness/coding-agent/src"
DEFAULT_VERIFY = "python3 -m pytest -q"
CI_FILE = Path(".github/workflows/coding-agent.yml")
CI_MARK = "# managed by coding-agent"
VENDOR_HOSTS = {"claude": "claude_code", "codex": "codex"}
PAYLOAD_TIMEOUT_S = 60


class InstallError(RuntimeError):
    """The project cannot take the install as requested; nothing after the failing step is changed."""


def manifest_text(python_src: str, verify: str) -> str:
    """The shipped template with the project's source path and verify command.

    Raises InstallError when the template no longer has the lines these substitutions need.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    for old, new in (("python_src: src\n", f"python_src: {python_src}\n"), ('    - "python3 -m pytest -q"\n', f'    - "{verify}"\n')):
        if text.count(old) != 1:
            raise InstallError(f"template.yaml has no unique line {old.strip()!r}; the installer cannot set it")
        text = text.replace(old, new)
    return text


def ensure_pyyaml(report: list[str]) -> None:
    """Make sure this interpreter can import PyYAML, installing it with pip when it cannot."""
    try:
        import yaml  # noqa: F401
        return
    except ImportError:
        pass
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pyyaml"], capture_output=True, text=True, check=False)
    try:
        import importlib

        importlib.invalidate_caches()
        importlib.import_module("yaml")
    except ImportError as error:
        raise InstallError(f"PyYAML is missing and pip could not install it ({result.stderr.strip()[:200] or error}); run: {sys.executable} -m pip install pyyaml") from error
    report.append("pyyaml: installed for this interpreter (hooks use `python3` on PATH; check it has pyyaml)")


def detect_hosts(project: Path, vendors: str | None) -> list[str]:
    """Host names for the project: the explicit `--vendor` list, else what the project already uses, else Claude Code."""
    if vendors:
        names = [name.strip() for name in vendors.split(",") if name.strip()]
        unknown = [name for name in names if name not in VENDOR_HOSTS]
        if unknown:
            raise InstallError(f"unknown vendor(s) {', '.join(unknown)}; use claude, codex")
        return list(dict.fromkeys(VENDOR_HOSTS[name] for name in names))
    found: list[str] = []
    if (project / ".claude").is_dir():
        found.append("claude_code")
    if (project / ".codex").is_dir() or (project / "AGENTS.md").exists():
        found.append("codex")
    return found or ["claude_code"]


def ci_text(python_src: str) -> str:
    return f"""{CI_MARK}; `coding-agent` uninstall removes this file only while this line is here.
name: coding-agent gate
on: [push, pull_request]
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install pyyaml
      - run: PYTHONPATH={python_src} python -m coding_agent.gate --manifest integration.yaml
"""


def _hook_command(document: dict[str, Any] | None, hook_id: str) -> str | None:
    """The command the host file runs for `hook_id`, from the harness entries only."""
    for entries in gen.harness_only(document).values():
        for entry in entries:
            for hook in entry.get("hooks") or []:
                command = str((hook or {}).get("command") or "")
                if f"coding_agent.hooks {hook_id}" in command:
                    return command
    return None


def _run_hook(command: str, project: Path, payload: dict[str, Any], role: str | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "PYTHONDONTWRITEBYTECODE": "1"}
    if role is not None:
        env["CODING_AGENT_ROLE"] = role
    return subprocess.run(["sh", "-c", command], input=json.dumps(payload), capture_output=True, text=True, cwd=project, env=env, timeout=PAYLOAD_TIMEOUT_S, check=False)


def verify_wiring(project: Path, manifest_path: Path, hosts: list[str]) -> list[str]:
    """Run the gate, then run the orca-guard and prompt-reset commands from the Claude Code host file."""
    report: list[str] = []
    manifest = load(manifest_path.resolve())
    problems = gen.drift(manifest, hosts)
    if problems:
        raise InstallError("; ".join(problems))
    report.append(f"verify: gate passed for {', '.join(hosts)}")
    if "claude_code" not in hosts:
        return report
    document = gen._read(project / gen.CLAUDE_FILE)
    guard_command = _hook_command(document, "orca-guard")
    reset_command = _hook_command(document, "prompt-reset")
    if guard_command is None or reset_command is None:
        raise InstallError(".claude/settings.json has no coding-agent command for orca-guard or prompt-reset")
    blocked = _run_hook(guard_command, project, {"session_id": "install-check", "cwd": str(project), "tool_name": "Bash", "tool_input": {"command": "orca orchestration task-update --id install-check --status not-a-status"}})
    expected = 2 if manifest.hook("orca-guard").mode == "enforce" else 0
    if blocked.returncode != expected:
        raise InstallError(f"orca-guard answered exit {blocked.returncode}, expected {expected}: {blocked.stderr.strip()[:200]}")
    report.append(f"verify: orca-guard ({manifest.hook('orca-guard').mode}) answered exit {expected} to an invalid status")
    reset = _run_hook(reset_command, project, {"session_id": "install-check", "cwd": str(project), "prompt": "install check"})
    if reset.returncode != 0:
        raise InstallError(f"prompt-reset answered exit {reset.returncode}: {reset.stderr.strip()[:200]}")
    report.append("verify: prompt-reset answered exit 0")

    coordinator_command = _hook_command(document, "coordinator-guard")
    if coordinator_command is None:
        raise InstallError(".claude/settings.json has no coding-agent command for coordinator-guard")
    write = _run_hook(coordinator_command, project, {"session_id": "install-check", "cwd": str(project), "tool_name": "Write", "tool_input": {"file_path": "install-check.txt"}}, role="coordinator")
    expected_write = 2 if manifest.hook("coordinator-guard").mode == "enforce" else 0
    if write.returncode != expected_write:
        raise InstallError(f"coordinator-guard answered exit {write.returncode} to a direct write, expected {expected_write}")
    report.append(f"verify: coordinator-guard ({manifest.hook('coordinator-guard').mode}) answered exit {expected_write} to a direct write")
    worker = _run_hook(coordinator_command, project, {"session_id": "install-check", "cwd": str(project), "tool_name": "Write", "tool_input": {"file_path": "install-check.txt"}}, role="worker")
    if worker.returncode != 0:
        raise InstallError(f"coordinator-guard blocked a worker session (exit {worker.returncode})")
    report.append("verify: coordinator-guard leaves worker sessions alone")
    return report


def _ignore_state(project: Path, report: list[str]) -> None:
    """Keep the harness state out of git when the project uses a .gitignore."""
    gitignore = project / ".gitignore"
    if not gitignore.exists():
        return
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    if ".coding-agent/" not in lines:
        gitignore.write_text("\n".join(lines + [".coding-agent/"]) + "\n", encoding="utf-8")
        report.append("gitignore: added .coding-agent/")


def install(project: Path, *, python_src: str = DEFAULT_PYTHON_SRC, verify: str = DEFAULT_VERIFY, vendors: str | None = None, run_verify: bool = True, ci: bool = True) -> list[str]:
    """Install into `project` and return one report line per step. Raises InstallError or NotOwned."""
    if not project.is_dir():
        raise InstallError(f"{project} is not a directory")
    if python_src.startswith("/") or ".." in Path(python_src).parts:
        raise InstallError("python_src must be a relative path inside the project")
    report: list[str] = []
    ensure_pyyaml(report)
    hosts = detect_hosts(project, vendors)
    report.append(f"vendors: {', '.join(hosts)}")

    backups = project / ".coding-agent" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    kept = safe_install.safe_replace(project / python_src / "coding_agent", PACKAGE, backup_root=backups)
    report.append(f"package: {python_src}/coding_agent" + (f" (previous copy kept at {kept})" if kept else ""))

    manifest_path = project / "integration.yaml"
    if manifest_path.exists():
        report.append("manifest: kept existing integration.yaml")
    else:
        manifest_path.write_text(manifest_text(python_src, verify), encoding="utf-8")
        report.append("manifest: wrote integration.yaml (verified: false; hooks in shadow except orca-guard and stop-gate)")
    try:
        manifest = load(manifest_path.resolve())
    except ManifestError as error:
        raise InstallError(f"integration.yaml is invalid: {error}") from error

    for path in gen.write(manifest, apply=True, hosts=hosts):
        report.append(f"hooks: merged {path.relative_to(project)} (previous file kept as .bak)")
    record = project / ".coding-agent" / "installed.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"hosts": hosts, "python_src": python_src}, indent=2) + "\n", encoding="utf-8")
    _ignore_state(project, report)

    if ci:
        ci_path = project / CI_FILE
        if ci_path.exists() and CI_MARK not in ci_path.read_text(encoding="utf-8"):
            report.append(f"ci: kept existing {CI_FILE} (not managed by coding-agent)")
        else:
            ci_path.parent.mkdir(parents=True, exist_ok=True)
            ci_path.write_text(ci_text(python_src), encoding="utf-8")
            report.append(f"ci: wrote {CI_FILE}")

    if run_verify:
        report.extend(verify_wiring(project, manifest_path, hosts))
    return report


def uninstall(project: Path, *, keep_core: bool = False) -> list[str]:
    """Reverse the install: harness hooks, the CI gate, the install record, and the package copy. The manifest stays."""
    report: list[str] = []
    for host in gen.HOSTS:
        target = gen._target(project, host)
        if not target.exists():
            continue
        document = gen._read(target)
        if not gen.harness_only(document):
            continue
        target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
        cleaned = gen.merged(document, {"hooks": {}})
        if cleaned.get("hooks") == {}:
            cleaned.pop("hooks")
        target.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report.append(f"hooks: removed harness entries from {target.relative_to(project)} (kept as .bak)")
    ci_path = project / CI_FILE
    if ci_path.exists() and CI_MARK in ci_path.read_text(encoding="utf-8"):
        ci_path.unlink()
        report.append(f"ci: removed {CI_FILE}")
    record = project / ".coding-agent" / "installed.json"
    python_src = DEFAULT_PYTHON_SRC
    if record.exists():
        try:
            python_src = json.loads(record.read_text(encoding="utf-8")).get("python_src", DEFAULT_PYTHON_SRC)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        record.unlink()
    package = project / python_src / "coding_agent"
    if not keep_core and package.exists():
        backups = project / ".coding-agent" / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        try:
            kept = safe_install.safe_remove(package, backup_root=backups)
        except safe_install.NotOwned as error:
            raise InstallError(str(error)) from error
        report.append(f"package: removed {python_src}/coding_agent (backup at {kept})")
        for parent in package.parents:
            if parent == project or not parent.is_relative_to(project) or any(parent.iterdir()):
                break
            parent.rmdir()
    if (project / "integration.yaml").exists():
        report.append("manifest: kept integration.yaml (your own file)")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.project_install", description="Install coding-agent into a project.")
    parser.add_argument("--project", type=Path, default=Path("."), help="project root (default: current directory)")
    parser.add_argument("--vendor", help="claude, codex, or claude,codex (default: detected from the project)")
    parser.add_argument("--python-src", default=DEFAULT_PYTHON_SRC, help="package location inside the project (default: harness/coding-agent/src)")
    parser.add_argument("--verify", default=DEFAULT_VERIFY, help="verify command written to a new manifest (default: python3 -m pytest -q)")
    parser.add_argument("--no-verify", action="store_true", help="skip the wiring check after install")
    parser.add_argument("--no-ci", action="store_true", help="do not write the CI gate")
    parser.add_argument("--clean", action="store_true", help="uninstall first, then install")
    parser.add_argument("--uninstall", action="store_true", help="remove the harness wiring and the package copy")
    parser.add_argument("--keep-core", action="store_true", help="with --uninstall: keep the package copy")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    try:
        if args.uninstall:
            lines = uninstall(project, keep_core=args.keep_core)
        else:
            lines = []
            if args.clean:
                lines.extend(uninstall(project))
            lines.extend(install(project, python_src=args.python_src, verify=args.verify, vendors=args.vendor, run_verify=not args.no_verify, ci=not args.no_ci))
    except (InstallError, safe_install.NotOwned, gen.GenError, OSError) as error:
        print(f"project-install: {error}", file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    if not args.uninstall:
        print("next: start a session in the project; `python3 -m coding_agent.cli status --run <run_id>` reads Orca tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
