"""The runtime's packaging for the AgentCore CLI (tasks.md 2.7; design.md section 7), and the
check that its CodeZip holds this commit's code.

No AWS call and no CLI run: these read the repo's files, and the zip check runs on zips made
here from the source.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tomllib
import zipfile
from pathlib import Path

import pytest

from contracts import names

ROOT = Path(__file__).resolve().parents[2]
CODE = ROOT / "agent" / names.AGENT_CODE_LOCATION


def _load_check():
    path = ROOT / "agent" / "scripts" / "check_runtime_zip.py"
    spec = importlib.util.spec_from_file_location("check_runtime_zip", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load_check()


# ---------------------------------------------------------------------------
# The CLI's project and the code folder
# ---------------------------------------------------------------------------
def test_the_cli_packages_the_runtime_folder_with_main_py():
    project = json.loads((ROOT / "agent/agentcore/agentcore.json").read_text())
    assert project["name"] == names.AGENTCORE_PROJECT_NAME
    (runtime,) = project["runtimes"]
    assert runtime["name"] == names.AGENT_RUNTIME_NAME
    assert runtime["codeLocation"] == names.AGENT_CODE_LOCATION == "runtime"
    assert runtime["entrypoint"] == names.AGENT_ENTRYPOINT == "main.py"
    assert (runtime["build"], runtime["runtimeVersion"]) == ("CodeZip", "PYTHON_3_13")
    assert (CODE / runtime["entrypoint"]).is_file()
    assert (CODE / names.AGENT_PACKAGE / "__init__.py").is_file()


def test_contracts_reaches_the_zip_through_a_symlink_pytest_skips():
    link = CODE / "contracts"
    assert link.is_symlink() and link.resolve() == (ROOT / "contracts").resolve()
    spec = importlib.util.spec_from_file_location("agent_conftest", ROOT / "agent/conftest.py")
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    assert "runtime/contracts" in conftest.collect_ignore


def _locked(lock_path: Path) -> dict[str, str]:
    lock = tomllib.loads(lock_path.read_text())
    return {package["name"]: package["version"] for package in lock["package"]}


def test_the_runtime_pins_the_root_environments_versions():
    """The unit tests run the same Strands, AgentCore and boto3 code as the runtime."""
    project = tomllib.loads((CODE / "pyproject.toml").read_text())
    assert project["tool"]["uv"]["package"] is False
    assert project["project"]["requires-python"] == ">=3.13,<3.14"
    pinned = {}
    for requirement in project["project"]["dependencies"]:
        if "==" in requirement:
            name, version = requirement.split("==")
            pinned[name.split("[")[0]] = version
    root, runtime = _locked(ROOT / "uv.lock"), _locked(CODE / "uv.lock")
    for name in ("bedrock-agentcore", "strands-agents", "boto3", "botocore"):
        assert pinned[name] == root[name] == runtime[name], name
    assert pinned[names.GATEWAY_SIGV4_PROXY_PACKAGE] == runtime[names.GATEWAY_SIGV4_PROXY_PACKAGE]


# ---------------------------------------------------------------------------
# check_runtime_zip
# ---------------------------------------------------------------------------
def _zip(
    tmp_path: Path, *, drop: str | None = None, change: str | None = None, extra=(), code=CODE
) -> Path:
    path = tmp_path / "asset.0123.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in check.required_files(code):
            if name == drop:
                continue
            data = (code / name).read_bytes()
            archive.writestr(name, data + b"# changed\n" if name == change else data)
        archive.writestr("boto3/__init__.py", b"")
        for name in extra:
            archive.writestr(name, b"")
    return path


def test_the_required_files_are_the_runtimes_code():
    required = check.required_files(CODE)
    assert required[0] == names.AGENT_ENTRYPOINT
    for name in (
        "rm_agent/__init__.py",
        "rm_agent/handlers.py",
        "rm_agent/state.py",
        "rm_agent/planner.py",
        "rm_agent/tools/flagsmith.py",
        "contracts/__init__.py",
        "contracts/names.py",
        "contracts/schemas/invoke.json",
        "contracts/schemas/response.json",
    ):
        assert name in required, name
    assert not [name for name in required if check.EXCLUDED_ENTRIES & set(Path(name).parts)]


def test_the_excluded_entries_are_the_packagers():
    """@aws/agentcore-cdk 1.3.0's dist/lib/packaging/helpers.js, EXCLUDED_ENTRIES."""
    assert check.EXCLUDED_ENTRIES == {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".DS_Store",
        "node_modules",
    }


@pytest.mark.parametrize("entry", sorted(check.EXCLUDED_ENTRIES))
def test_what_the_packager_skips_is_never_required(tmp_path, entry):
    """A stray rm_agent/.DS_Store, which the packager drops, must not block a deploy."""
    code = tmp_path / "runtime"
    shutil.copytree(CODE, code, ignore=shutil.ignore_patterns(*check.EXCLUDED_ENTRIES))
    for folder in (code / names.AGENT_PACKAGE, code / names.AGENT_PACKAGE / "tools"):
        stray = folder / entry  # at the package's top, and one level down
        if entry == ".DS_Store":
            stray.write_bytes(b"\0\0\0\1Bud1")
        else:
            (stray / "nested").mkdir(parents=True)
            (stray / "nested" / "module.py").write_text("x = 1\n")
    assert check.required_files(code) == check.required_files(CODE)
    path = _zip(tmp_path, code=code)  # holds only what the packager copies
    assert check.check_zip(path, code)["checked"] == len(check.required_files(CODE))


def test_a_zip_of_this_commits_code_passes(tmp_path):
    summary = check.check_zip(_zip(tmp_path), CODE)
    assert summary["checked"] == len(check.required_files(CODE))


@pytest.mark.parametrize(
    "kind, name",
    [
        ("drop", names.AGENT_ENTRYPOINT),
        ("drop", "rm_agent/handlers.py"),
        ("drop", "contracts/names.py"),
        ("change", "rm_agent/state.py"),
        ("change", "contracts/schemas/invoke.json"),
    ],
)
def test_a_zip_missing_or_stale_on_any_file_fails(tmp_path, kind, name):
    path = _zip(tmp_path, **{kind: name})
    with pytest.raises(check.ZipError, match=name.replace(".", r"\.")):
        check.check_zip(path, CODE)


def test_a_zip_with_pycache_fails(tmp_path):
    path = _zip(tmp_path, extra=["rm_agent/__pycache__/state.cpython-313.pyc"])
    with pytest.raises(check.ZipError, match="__pycache__"):
        check.check_zip(path, CODE)


def _assets(tmp_path: Path, *paths: str) -> Path:
    out = tmp_path / "cdk.out"
    out.mkdir()
    files = {str(n): {"source": {"path": p}} for n, p in enumerate(paths)}
    (out / f"{names.AGENT_STACK}.assets.json").write_text(json.dumps({"files": files}))
    return out


def test_the_code_zip_is_the_stacks_one_zip_asset(tmp_path):
    out = _assets(tmp_path, "asset.abc.zip", f"{names.AGENT_STACK}.template.json")
    assert check.find_code_zip(out) == out / "asset.abc.zip"


@pytest.mark.parametrize("paths", [(), ("asset.a.zip", "asset.b.zip")], ids=["none", "two"])
def test_no_zip_or_two_zips_is_an_error(tmp_path, paths):
    with pytest.raises(check.ZipError):
        check.find_code_zip(_assets(tmp_path, *paths))
    with pytest.raises(check.ZipError, match="agentcore build"):
        check.find_code_zip(tmp_path / "nothing-built")
