"""Check the runtime's CodeZip holds rm_agent's code, as this commit has it (tasks.md 2.7).

`agentcore build` (and the build inside `agentcore deploy`) writes the CodeZip into
agent/agentcore/cdk/cdk.out as a CDK file asset. The CLI packages only the runtime's code
folder, agent/runtime (codeLocation "runtime"), which becomes the zip's root, following the
contracts symlink. This check finds that zip and requires, byte for byte, these files:
- main.py;
- every file of rm_agent/, except what the packager skips (EXCLUDED_ENTRIES below);
- contracts/'s modules and schemas.

It also refuses a zip that carries __pycache__.

    uv run python agent/scripts/check_runtime_zip.py      # after: cd agent && agentcore build
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root, for contracts

from contracts import names  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CODE = ROOT / "agent" / names.AGENT_CODE_LOCATION  # agent/runtime, the zip's root
CDK_OUT = ROOT / "agent" / "agentcore" / "cdk" / "cdk.out"
CONTRACT_FILES = ("contracts/__init__.py", "contracts/dynamo.py", "contracts/names.py")
# The CodeZip packager skips an entry with one of these names at any depth, so the zip never
# holds it: @aws/agentcore-cdk 1.3.0, dist/lib/packaging/helpers.js, EXCLUDED_ENTRIES.
EXCLUDED_ENTRIES = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".DS_Store", "node_modules"}
)


class ZipError(RuntimeError):
    """The CodeZip is missing, ambiguous, stale or incomplete."""


def required_files(code: Path = CODE) -> list[str]:
    """The zip's paths that must hold this commit's bytes."""
    files = [names.AGENT_ENTRYPOINT]
    files += sorted(
        str(path.relative_to(code))
        for path in (code / names.AGENT_PACKAGE).rglob("*")
        if path.is_file() and not EXCLUDED_ENTRIES.intersection(path.relative_to(code).parts)
    )
    files += list(CONTRACT_FILES)
    files += sorted(
        f"contracts/schemas/{path.name}" for path in (code / "contracts" / "schemas").glob("*.json")
    )
    return files


def find_code_zip(cdk_out: Path = CDK_OUT) -> Path:
    """The one zip file asset of the CLI's stack."""
    assets = cdk_out / f"{names.AGENT_STACK}.assets.json"
    if not assets.is_file():
        raise ZipError(f"no {assets.name} in {cdk_out}: run `agentcore build` in agent/ first")
    files = json.loads(assets.read_text()).get("files", {})
    zips = sorted(
        {
            entry["source"]["path"]
            for entry in files.values()
            if entry["source"]["path"].endswith(".zip")
        }
    )
    if len(zips) != 1:
        raise ZipError(f"expected one zip asset in {assets.name}, found {zips}")
    return cdk_out / zips[0]


def check_zip(path: Path, code: Path = CODE) -> dict:
    """{"zip", "files", "checked"}; ZipError naming every missing or different file."""
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        wanted = required_files(code)
        missing = [name for name in wanted if name not in members]
        stale = [
            name
            for name in wanted
            if name in members and archive.read(name) != (code / name).read_bytes()
        ]
    cached = sorted(name for name in members if "__pycache__" in name)
    problems = []
    if missing:
        problems.append(f"missing {missing}")
    if stale:
        problems.append(f"not this commit's bytes {stale}: build again")
    if cached:
        problems.append(f"{len(cached)} __pycache__ entries")
    if problems:
        raise ZipError(f"{path.name}: " + "; ".join(problems))
    return {"zip": path.name, "files": len(members), "checked": len(wanted)}


def main() -> int:
    try:
        summary = check_zip(find_code_zip())
    except ZipError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
