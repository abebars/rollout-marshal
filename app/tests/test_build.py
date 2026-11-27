"""Tests for app/build_zips.py, which app/build.sh runs (task 3.5).

No wheel is downloaded: the dependency tests use a stand-in for the installed app group.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app import build_zips
from contracts import names

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ["contracts/__init__.py", "contracts/names.py"]


def _files(zip_name: str) -> list[str]:
    return list(build_zips.ZIPS[zip_name]["files"])


def test_the_answer_zip_holds_c3s_list_plus_the_field_helpers():
    assert sorted(_files("rm-answer.zip")) == sorted(
        [
            "app/__init__.py",
            "app/answer/__init__.py",
            "app/answer/handler.py",
            "app/bedrock_fields.py",
            *CONTRACTS,
        ]
    )
    assert build_zips.ZIPS["rm-answer.zip"]["dependencies"] is False  # boto3 is the runtime's


def test_the_router_zip_holds_c3s_list_and_the_app_group():
    assert sorted(_files("rm-router.zip")) == sorted(
        ["app/__init__.py", "app/router/__init__.py", "app/router/handler.py", *CONTRACTS]
    )
    assert build_zips.ZIPS["rm-router.zip"]["dependencies"] is True


def test_the_judge_zip_holds_the_leads_list_and_the_app_group():
    assert sorted(_files("rm-judge.zip")) == sorted(
        [
            "app/__init__.py",
            "app/judge/__init__.py",
            "app/judge/handler.py",
            "app/bedrock_fields.py",
            names.PRICES_FILE,
            *CONTRACTS,
        ]
    )
    assert build_zips.ZIPS["rm-judge.zip"]["dependencies"] is True


@pytest.mark.parametrize(
    ("zip_name", "package"),
    [
        ("rm-answer.zip", "app/answer"),
        ("rm-router.zip", "app/router"),
        ("rm-judge.zip", "app/judge"),
    ],
)
def test_every_module_of_the_package_is_in_its_zip_and_no_test(zip_name, package):
    modules = {p.relative_to(ROOT).as_posix() for p in (ROOT / package).glob("*.py")}
    assert modules <= set(_files(zip_name))
    assert not [f for f in _files(zip_name) if "tests" in f.split("/")]


@pytest.mark.parametrize(
    ("zip_name", "handler"),
    [
        ("rm-answer.zip", names.ANSWER_HANDLER),
        ("rm-router.zip", names.ROUTER_HANDLER),
        ("rm-judge.zip", "app.judge.handler.handler"),
    ],
)
def test_each_handler_imports_from_its_zips_files_alone(tmp_path, zip_name, handler):
    """The zip keeps the repo's paths, so the handler is its module's path plus the
    function. Third-party packages come from this environment, as from the zip's app group
    or the runtime's boto3."""
    for relative in _files(zip_name):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    module, function = handler.rsplit(".", 1)
    loaded = f"importlib.import_module({module!r})"
    probe = f"import importlib; print(callable(getattr({loaded}, {function!r})))"
    done = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(tmp_path)!r}); {probe}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True"


def _fake_dependencies(tmp_path: Path) -> Path:
    deps = tmp_path / "deps"
    (deps / "flagsmith").mkdir(parents=True)
    (deps / "flagsmith" / "__init__.py").write_text("# stand-in\n")
    (deps / "bin").mkdir()
    (deps / "bin" / "console").write_text("#!/bin/sh\n")
    (deps / ".lock").write_text("")
    (deps / "flagsmith" / "__pycache__").mkdir()
    (deps / "flagsmith" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    return deps


def test_a_build_is_repeatable_and_leaves_out_what_lambda_needs_not(tmp_path):
    deps = _fake_dependencies(tmp_path)
    first = build_zips.build(tmp_path / "one", deps_dir=deps)
    second = build_zips.build(tmp_path / "two", deps_dir=deps)

    for zip_name in build_zips.ZIPS:
        assert first[zip_name].read_bytes() == second[zip_name].read_bytes(), zip_name
    with zipfile.ZipFile(first["rm-router.zip"]) as archive:
        entries = archive.namelist()
        assert entries == sorted(entries)
        assert "flagsmith/__init__.py" in entries
        assert not [
            e for e in entries if e.startswith("bin/") or e.endswith(".lock") or "__pycache__" in e
        ]
        for info in archive.infolist():
            assert info.date_time == build_zips.ZIP_TIME
            assert (info.external_attr >> 16) & 0o777 == 0o644
    with zipfile.ZipFile(first["rm-answer.zip"]) as archive:
        assert sorted(archive.namelist()) == sorted(_files("rm-answer.zip"))  # no dependencies


def test_a_dependency_may_never_replace_the_apps_files(tmp_path):
    deps = _fake_dependencies(tmp_path)
    (deps / "contracts").mkdir()
    (deps / "contracts" / "names.py").write_text("# a clash\n")
    with pytest.raises(SystemExit, match="contracts/names.py"):
        build_zips.build(tmp_path / "out", deps_dir=deps)


def test_build_sh_runs_the_builder_and_make_build_finds_it():
    script = (ROOT / "app" / "build.sh").read_text()
    assert 'exec uv run --frozen python "$HERE/build_zips.py"' in script
    assert "/spike/" not in "app/build.sh"  # make build skips only the spikes' scripts
    assert (ROOT / "app" / "build.sh").stat().st_mode & 0o111
