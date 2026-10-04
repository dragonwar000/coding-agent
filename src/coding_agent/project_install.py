"""Install coding-agent into a project: copy the package, write the manifest, render the hook files, and gate them.

`python3 -m coding_agent.project_install --project DIR` copies this package to `DIR/<python_src>/coding_agent`.
An existing copy is replaced only when it still carries the harness marker (FR-004), and a backup is kept.
The manifest is written only when the project has none; an existing `integration.yaml` is never overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import coding_agent
from coding_agent import gen
from coding_agent import install as safe_install
from coding_agent.manifest import ManifestError, load

PACKAGE = Path(coding_agent.__file__).resolve().parent
TEMPLATE = PACKAGE / "template.yaml"
DEFAULT_PYTHON_SRC = "harness/src"
DEFAULT_VERIFY = "python3 -m pytest -q"


class InstallError(RuntimeError):
    """The project cannot take the install as requested."""


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


def install(project: Path, *, python_src: str = DEFAULT_PYTHON_SRC, verify: str = DEFAULT_VERIFY) -> list[str]:
    """Install into `project` and return one report line per step. Raises InstallError or NotOwned before changing anything it cannot undo."""
    if not project.is_dir():
        raise InstallError(f"{project} is not a directory")
    if python_src.startswith("/") or ".." in Path(python_src).parts:
        raise InstallError("python_src must be a relative path inside the project")
    report: list[str] = []
    backups = project / ".coding-agent" / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    kept = safe_install.safe_replace(project / python_src / "coding_agent", PACKAGE, backup_root=backups)
    report.append(f"package: {python_src}/coding_agent" + (f" (previous copy kept at {kept})" if kept else ""))

    manifest_path = project / "integration.yaml"
    if manifest_path.exists():
        report.append("manifest: kept existing integration.yaml")
    else:
        manifest_path.write_text(manifest_text(python_src, verify), encoding="utf-8")
        report.append("manifest: wrote integration.yaml (verified: false, every hook in shadow except the two enforced gates)")

    try:
        manifest = load(manifest_path.resolve())
    except ImportError as error:
        raise InstallError(f"PyYAML is not importable here ({error}); install pyyaml, then run the installer again") from error
    except ManifestError as error:
        raise InstallError(f"integration.yaml is invalid: {error}") from error
    written = gen.write(manifest, apply=True)
    report.extend(f"hooks: wrote {path.relative_to(project)}" for path in written)
    problems = gen.drift(manifest)
    if problems:
        raise InstallError("; ".join(problems))
    report.append("gate: live hook files match integration.yaml")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.project_install", description="Install coding-agent into a project.")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--python-src", default=DEFAULT_PYTHON_SRC, help="package location inside the project (default: harness/src)")
    parser.add_argument("--verify", default=DEFAULT_VERIFY, help="verify command written to a new manifest (default: python3 -m pytest -q)")
    args = parser.parse_args(argv)
    try:
        report = install(args.project.resolve(), python_src=args.python_src, verify=args.verify)
    except (InstallError, safe_install.NotOwned) as error:
        print(f"project-install: {error}", file=sys.stderr)
        return 2
    for line in report:
        print(line)
    print("next: check the hook python3 has pyyaml and that the verify command runs; then start a session")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
