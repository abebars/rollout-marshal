"""Build the app's three Lambda zips in app/build/ (task 3.5). Run through app/build.sh.

`make build` runs app/build.sh before every plan and apply (design.md section 2).

Each zip keeps the repo's paths, so a handler is its module's path plus the function
(design.md section 5, contract change c3):

- rm-answer.zip, for rm-answer-current and rm-answer-cheaper: app/__init__.py, app/answer/,
  app/bedrock_fields.py and the two contracts files. app/bedrock_fields.py is in it since
  task 3.3 moved the field helpers there.
- rm-router.zip: app/__init__.py, app/router/, the two contracts files, and the app
  dependency group, the flagsmith SDK and its dependencies, as uv.lock pins them.
- rm-judge.zip: app/__init__.py, app/judge/, app/bedrock_fields.py, app/prices.json, the
  two contracts files and the app group (the lead's decision for 3.5).

boto3 and botocore come from the python3.13 runtime. Task 1.2 ran Converse there with the
runtime's boto3 1.42.97, and nothing else the functions call is newer: Lambda Invoke,
DynamoDB GetItem and Secrets Manager GetSecretValue.

The app group is installed from manylinux aarch64 wheels for CPython 3.13, with the hashes
uv.lock pins, as task 1.2's build did. A zip is repeatable: sorted entries, and fixed times
and modes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # contracts, when run as a script

from contracts import names  # noqa: E402

BUILD_DIR = ROOT / "app" / "build"
DEPS_DIR = BUILD_DIR / "deps"
PLATFORM = "aarch64-manylinux2014"  # glibc 2.17 wheels; Lambda's python3.13 is Amazon Linux 2023
CONTRACTS = ("contracts/__init__.py", "contracts/names.py")
ZIP_TIME = (2026, 11, 26, 0, 0, 0)  # fixed, so an unchanged package gives the same zip

# What each zip holds from the repository, and whether the app group goes in with it.
ZIPS: dict[str, dict] = {
    "rm-answer.zip": {
        "files": (
            "app/__init__.py",
            "app/answer/__init__.py",
            "app/answer/handler.py",
            "app/bedrock_fields.py",
            *CONTRACTS,
        ),
        "dependencies": False,
    },
    "rm-router.zip": {
        "files": (
            "app/__init__.py",
            "app/router/__init__.py",
            "app/router/handler.py",
            *CONTRACTS,
        ),
        "dependencies": True,
    },
    "rm-judge.zip": {
        "files": (
            "app/__init__.py",
            "app/judge/__init__.py",
            "app/judge/handler.py",
            "app/bedrock_fields.py",
            names.PRICES_FILE,
            *CONTRACTS,
        ),
        "dependencies": True,
    },
}
# Left out of the installed dependencies: uv's lock file, console scripts and bytecode, and
# each wheel's RECORD. RECORD hashes the console scripts, whose shebangs hold the build's
# absolute Python path, so two checkouts would give different zips, and every plan would
# show the functions changing (PR #27's review). importlib.metadata.version, which the SDK
# uses, reads only METADATA.
DEPENDENCY_SKIPS = (".lock", "bin/", "__pycache__/", ".dist-info/RECORD")


def install_dependencies(deps_dir: Path = DEPS_DIR) -> Path:
    """The app dependency group, as uv.lock pins it, for Lambda's python3.13 on arm64."""
    shutil.rmtree(deps_dir, ignore_errors=True)
    deps_dir.mkdir(parents=True)
    requirements = deps_dir.parent / "requirements.txt"
    subprocess.run(
        [
            "uv", "export", "--project", str(ROOT), "--frozen", "--only-group", "app",
            "--no-emit-project", "--format", "requirements-txt", "-o", str(requirements),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )  # fmt: skip
    subprocess.run(
        [
            "uv", "pip", "install", "--quiet", "--target", str(deps_dir),
            "--python-platform", PLATFORM, "--python-version", names.PYTHON_VERSION,
            "--only-binary", ":all:", "-r", str(requirements),
        ],
        check=True,
    )  # fmt: skip
    check_platform(deps_dir)
    return deps_dir


def check_platform(deps_dir: Path) -> None:
    """Every compiled module is a Linux aarch64 build, and every wheel's tag fits."""
    wrong = []
    for so in sorted(deps_dir.rglob("*.so")):
        kind = subprocess.run(["file", str(so)], capture_output=True, text=True).stdout
        if "ARM aarch64" not in kind:
            wrong.append(f"not an aarch64 ELF: {so.relative_to(deps_dir)}")
    for wheel in sorted(deps_dir.glob("*.dist-info/WHEEL")):
        for line in wheel.read_text().splitlines():
            if line.startswith("Tag:"):
                tag = line.split(":", 1)[1].strip()
                if tag != "py3-none-any" and not ("manylinux" in tag and "aarch64" in tag):
                    wrong.append(f"{wheel.parent.name}: wheel tag {tag}")
    if wrong:
        raise SystemExit("build: wrong platform in the package:\n  " + "\n  ".join(wrong))


def dependency_files(deps_dir: Path) -> dict[str, Path]:
    """Each installed dependency file, by its path in the zip."""
    found = {}
    for path in sorted(deps_dir.rglob("*")):
        relative = path.relative_to(deps_dir).as_posix()
        if path.is_file() and not any(skip in relative for skip in DEPENDENCY_SKIPS):
            found[relative] = path
    return found


def write_zip(target: Path, entries: dict[str, Path]) -> Path:
    """A repeatable zip of entries (path in the zip to file on disk)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16  # rw-r--r--: Lambda's runtime reads every file
            archive.writestr(info, entries[name].read_bytes())
    return target


def build(build_dir: Path = BUILD_DIR, *, deps_dir: Path | None = None) -> dict[str, Path]:
    """Every zip in ZIPS, written to build_dir. deps_dir, when given, is used as the
    installed app group instead of installing it."""
    if deps_dir is None and any(spec["dependencies"] for spec in ZIPS.values()):
        deps_dir = install_dependencies(build_dir / "deps")
    built = {}
    for zip_name, spec in ZIPS.items():
        entries = {path: ROOT / path for path in spec["files"]}
        if spec["dependencies"]:
            clashes = set(entries) & set(dependency_files(deps_dir))
            if clashes:
                raise SystemExit(f"build: dependencies would replace {sorted(clashes)}")
            entries.update(dependency_files(deps_dir))
        built[zip_name] = write_zip(build_dir / zip_name, entries)
    return built


def main() -> int:
    for path in build().values():
        print(f"built {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
