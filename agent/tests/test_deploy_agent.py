"""Unit tests for agent/scripts/deploy_agent.py (tasks.md 2.7).

botocore's Stubber stands in for STS, the AgentCore control plane and SSM, so every request is
checked against the service model and none leaves the process. A fake shell stands in for git,
the AgentCore CLI and the CDK CLI. Its `agentcore build` writes a real CodeZip from
agent/runtime, so the zip check runs as it does for real.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import zipfile
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from contracts import names

ROOT = Path(__file__).resolve().parents[2]


def _load():
    path = ROOT / "agent" / "scripts" / "deploy_agent.py"
    spec = importlib.util.spec_from_file_location("deploy_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dep = _load()
check = dep.check_runtime_zip

NOW = dt.datetime(2026, 11, 27, 9, 0, tzinfo=dt.UTC)
COMMIT = "0123456789abcdef0123456789abcdef01234567"
RUNTIME_ID = f"{names.AGENT_RUNTIME_DEPLOYED_NAME}-5Izilk9O73"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{names.REGION}:{names.ACCOUNT_ID}:runtime/{RUNTIME_ID}"
ROLE_ARN = (
    f"arn:aws:iam::{names.ACCOUNT_ID}:role/AgentCore-rm-default-ApplicationRuntimeRmAgentRunti-x"
)
ASSET_HASH = "e9b72f9c72f8bf6e304ef32a7837c012715cd8824bcd48987efaae4cd131f8ab"
BUCKET = f"cdk-hnb659fds-assets-{names.ACCOUNT_ID}-{names.REGION}"
DIFF = "Stack AgentCore-rm-default\nResources\n[~] AWS::BedrockAgentCore::Runtime ...\n"
DEPLOYED = "deployed project 'rm' to target 'default'"


# ---------------------------------------------------------------------------
# The fake shell
# ---------------------------------------------------------------------------
class Shell:
    """git, agentcore and cdk, answering from canned results; every call is recorded."""

    def __init__(self, cdk_out: Path, **answers):
        self.cdk_out = cdk_out
        self.calls: list[tuple[list[str], Path, dict]] = []
        self.answers = {
            "version": dep.Result(0, "1.3.0\n"),
            "status": dep.Result(0, ""),
            "diff": dep.Result(0, ""),
            "build": dep.Result(0, '{"message": "built project \'rm\'"}\n'),
            "cdk": dep.Result(0, DIFF),
            "deploy": dep.Result(0, DEPLOYED),
            **answers,
        }
        self.drop: str | None = None  # a file the build leaves out of the zip

    def __call__(self, cmd, cwd, env):
        cmd = list(cmd)
        self.calls.append((cmd, Path(cwd), dict(env)))
        if cmd[:2] == ["git", "rev-parse"]:
            return dep.Result(0, COMMIT + "\n")
        if cmd[:2] == ["git", "status"]:
            return self.answers["status"]
        if cmd[:2] == ["git", "diff"]:
            return self.answers["diff"]
        if cmd[0] == names.AGENTCORE_CLI_COMMAND and cmd[1] == "--version":
            return self.answers["version"]
        if cmd[0] == names.AGENTCORE_CLI_COMMAND and cmd[1] in ("build", "deploy"):
            self._write_assembly()
            return self.answers[cmd[1]]
        if cmd[0] == str(dep.CDK_BIN):
            return self.answers["cdk"]
        raise AssertionError(f"unexpected command {cmd}")

    def _write_assembly(self):
        self.cdk_out.mkdir(exist_ok=True)
        zip_name = f"asset.{ASSET_HASH}.zip"
        with zipfile.ZipFile(self.cdk_out / zip_name, "w") as archive:
            for name in check.required_files(check.CODE):
                if name != self.drop:
                    archive.writestr(name, (check.CODE / name).read_bytes())
        files = {ASSET_HASH: {"source": {"path": zip_name, "packaging": "file"}}}
        assets = self.cdk_out / f"{names.AGENT_STACK}.assets.json"
        assets.write_text(json.dumps({"files": files}))

    def commands(self) -> list[list[str]]:
        return [cmd for cmd, _, _ in self.calls]


# ---------------------------------------------------------------------------
# The stubbed clients
# ---------------------------------------------------------------------------
class Stubs:
    def __init__(self):
        session = boto3.Session(
            aws_access_key_id="testing", aws_secret_access_key="testing", region_name=names.REGION
        )
        self.clients = dep.Clients(
            sts=session.client("sts"),
            control=session.client("bedrock-agentcore-control"),
            ssm=session.client("ssm"),
        )
        self.sts = Stubber(self.clients.sts)
        self.control = Stubber(self.clients.control)
        self.ssm = Stubber(self.clients.ssm)
        self.all = (self.sts, self.control, self.ssm)

    def __enter__(self):
        for stub in self.all:
            stub.activate()
        return self

    def __exit__(self, *exc):
        for stub in self.all:
            stub.deactivate()

    def assert_done(self):
        for stub in self.all:
            stub.assert_no_pending_responses()

    def caller(self, account=names.ACCOUNT_ID):
        self.sts.add_response(
            "get_caller_identity",
            {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/x/y", "UserId": "u"},
            {},
        )

    def runtime(self, version="4", status="READY", prefix=f"{ASSET_HASH}.zip", arn=RUNTIME_ARN):
        runtime_id = arn.rsplit("/", 1)[1]
        self.control.add_response(
            "list_agent_runtimes",
            {
                "agentRuntimes": [
                    {
                        "agentRuntimeArn": f"arn:aws:bedrock-agentcore:{names.REGION}:"
                        f"{names.ACCOUNT_ID}:runtime/other_runtime-AbCdEfGhIj",
                        "agentRuntimeId": "other_runtime-AbCdEfGhIj",
                        "agentRuntimeVersion": "1",
                        "agentRuntimeName": "other_runtime",
                        "description": "a runtime",
                        "lastUpdatedAt": NOW,
                        "status": "READY",
                    },
                    {
                        "agentRuntimeArn": arn,
                        "agentRuntimeId": runtime_id,
                        "agentRuntimeVersion": version,
                        "agentRuntimeName": names.AGENT_RUNTIME_DEPLOYED_NAME,
                        "description": "a runtime",
                        "lastUpdatedAt": NOW,
                        "status": status,
                    },
                ]
            },
            {},
        )
        self.control.add_response(
            "get_agent_runtime",
            {
                "agentRuntimeArn": arn,
                "agentRuntimeName": names.AGENT_RUNTIME_DEPLOYED_NAME,
                "agentRuntimeId": runtime_id,
                "agentRuntimeVersion": version,
                "createdAt": NOW,
                "lastUpdatedAt": NOW,
                "roleArn": ROLE_ARN,
                "status": status,
                "lifecycleConfiguration": {},
                "environmentVariables": {"SOME_SETTING": "env-value-never-recorded"},
                "agentRuntimeArtifact": {
                    "codeConfiguration": {
                        "code": {"s3": {"bucket": BUCKET, "prefix": prefix}},
                        "runtime": "PYTHON_3_13",
                        "entryPoint": ["opentelemetry-instrument", names.AGENT_ENTRYPOINT],
                    }
                },
            },
            {"agentRuntimeId": runtime_id},
        )

    def no_runtime(self):
        self.control.add_response("list_agent_runtimes", {"agentRuntimes": []}, {})

    def parameter(self, value=RUNTIME_ARN, version=1):
        self.ssm.add_response(
            "get_parameter",
            {
                "Parameter": {
                    "Name": names.AGENT_RUNTIME_ARN_PARAMETER,
                    "Type": "String",
                    "Value": value,
                    "Version": version,
                    "LastModifiedDate": NOW,
                }
            },
            {"Name": names.AGENT_RUNTIME_ARN_PARAMETER},
        )

    def no_parameter(self):
        self.ssm.add_client_error(
            "get_parameter",
            service_error_code="ParameterNotFound",
            http_status_code=400,
            expected_params={"Name": names.AGENT_RUNTIME_ARN_PARAMETER},
        )

    def put_parameter(self, value=RUNTIME_ARN, version=2):
        self.ssm.add_response(
            "put_parameter",
            {"Version": version, "Tier": "Standard"},
            {
                "Name": names.AGENT_RUNTIME_ARN_PARAMETER,
                "Value": value,
                "Type": "String",
                "Overwrite": True,
            },
        )


def _run(stubs, shell, tmp_path, **kwargs):
    lines: list[str] = []
    record: dict = {}
    try:
        dep.run(
            stubs.clients,
            shell,
            record=record,
            out=lines.append,
            sleep=lambda _s: None,
            cdk_out=shell.cdk_out,
            **kwargs,
        )
    finally:
        stubs.assert_done()
    return record, "\n".join(lines)


def _before(stubs):
    """The reads before the deploy: the runtime and the parameter as 1.3 left them."""
    stubs.caller()
    stubs.runtime(version="3")
    stubs.parameter()


def _deploy_command():
    return [
        names.AGENTCORE_CLI_COMMAND,
        "deploy",
        "--target",
        names.AGENTCORE_TARGET_NAME,
        "--json",
    ]


# ---------------------------------------------------------------------------
# The plan and the deploy
# ---------------------------------------------------------------------------
def test_plan_builds_checks_and_diffs_but_never_deploys_or_writes(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)  # no put_parameter queued: a call to it would fail here
        record, output = _run(stubs, shell, tmp_path, plan_only=True)
    assert _deploy_command() not in shell.commands()
    assert record["plan"] is True
    assert record["commit"] == COMMIT
    assert record["zip_before"]["checked"] == len(check.required_files(check.CODE))
    assert record["cdk_diff"] == DIFF
    assert record["runtime_before"]["version"] == "3"
    assert record["parameter_before"]["value"] == RUNTIME_ARN
    assert "deployed" not in record
    assert "no deploy and no put-parameter" in output


def test_deploy_writes_the_arn_and_records_the_version_and_the_commit(tmp_path):
    shell = Shell(tmp_path / "cdk.out", diff=dep.Result(0, '-    "aws-cdk": "~2.1126.0",\n'))
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="4")
        stubs.put_parameter()
        stubs.parameter(version=2)
        record, _ = _run(stubs, shell, tmp_path)

    assert shell.commands() == [
        [names.AGENTCORE_CLI_COMMAND, "--version"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain", "--", *dep.CODE_PATHS],
        ["git", "diff", "--", dep.PACKAGE_JSON],
        [names.AGENTCORE_CLI_COMMAND, "build", "--json"],
        [
            str(dep.CDK_BIN),
            "diff",
            "--app",
            str(shell.cdk_out),
            "--method",
            "template",
            "--no-color",
            names.AGENT_STACK,
        ],
        _deploy_command(),
        ["git", "diff", "--", dep.PACKAGE_JSON],
        ["git", "status", "--porcelain", "--", dep.CLI_PROJECT_DIR],
    ]
    assert record["package_json_diff_before"] == '-    "aws-cdk": "~2.1126.0",\n'
    assert record["runtime_before"]["version"] == "3"
    assert record["runtime_after"]["version"] == "4"
    assert record["parameter_after"]["value"] == RUNTIME_ARN
    deployed = record["deployed"]
    assert (deployed["commit"], deployed["runtime_version"]) == (COMMIT, "4")
    assert deployed["runtime_arn"] == RUNTIME_ARN
    assert record["deploy_output"] == DEPLOYED


def test_the_cli_runs_in_agent_without_telemetry_and_the_cdk_in_its_project(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        _run(stubs, shell, tmp_path, plan_only=True)
    for cmd, cwd, env in shell.calls:
        if cmd[0] == names.AGENTCORE_CLI_COMMAND:
            assert (cwd, env) == (dep.AGENT_DIR, {"AGENTCORE_TELEMETRY_DISABLED": "1"})
        elif cmd[0] == str(dep.CDK_BIN):
            assert cwd == dep.CDK_DIR
            assert "--fail" not in cmd and "change-set" not in " ".join(cmd)
        else:
            assert cwd == dep.ROOT
    assert "--yes" not in " ".join(" ".join(cmd) for cmd, _, _ in shell.calls)


def test_the_first_write_creates_the_parameter(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        stubs.caller()
        stubs.runtime(version="3")
        stubs.no_parameter()
        stubs.runtime(version="4")
        stubs.put_parameter(version=1)
        stubs.parameter(version=1)
        record, _ = _run(stubs, shell, tmp_path)
    assert record["parameter_before"] is None
    assert record["parameter_after"]["version"] == 1


def test_waits_while_the_runtime_settles_after_the_deploy(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="4", status="UPDATING")
        stubs.runtime(version="4", status="READY")
        stubs.put_parameter()
        stubs.parameter(version=2)
        record, _ = _run(stubs, shell, tmp_path)
    assert record["runtime_after"]["status"] == "READY"


# ---------------------------------------------------------------------------
# Every stop
# ---------------------------------------------------------------------------
def test_refuses_another_account_before_any_command(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        stubs.caller(account="111122223333")
        with pytest.raises(dep.DeployError, match=names.ACCOUNT_ID):
            _run(stubs, shell, tmp_path)
    assert shell.calls == []


@pytest.mark.parametrize("version", ["1.2.9", "2.0.0", "unknown"])
def test_refuses_a_cli_outside_1x_from_1_3_0(tmp_path, version):
    shell = Shell(tmp_path / "cdk.out", version=dep.Result(0, version + "\n"))
    with Stubs() as stubs:
        stubs.caller()
        with pytest.raises(dep.DeployError, match="want 1.x"):
            _run(stubs, shell, tmp_path)
    assert shell.commands() == [[names.AGENTCORE_CLI_COMMAND, "--version"]]


def test_refuses_uncommitted_code_but_a_plan_reports_it(tmp_path):
    dirty = dep.Result(0, " M agent/runtime/rm_agent/state.py\n")
    with Stubs() as stubs:
        stubs.caller()
        with pytest.raises(dep.DeployError, match="uncommitted"):
            _run(stubs, Shell(tmp_path / "a", status=dirty), tmp_path)
    with Stubs() as stubs:
        _before(stubs)
        record, _ = _run(stubs, Shell(tmp_path / "b", status=dirty), tmp_path, plan_only=True)
    assert record["uncommitted_code"] == [" M agent/runtime/rm_agent/state.py"]


def test_a_zip_missing_main_py_stops_before_the_deploy(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    shell.drop = names.AGENT_ENTRYPOINT
    with Stubs() as stubs:
        stubs.caller()
        with pytest.raises(dep.DeployError, match="zip check.*main"):
            _run(stubs, shell, tmp_path)
    assert _deploy_command() not in shell.commands()


def test_a_failed_cdk_diff_stops_before_the_deploy(tmp_path):
    shell = Shell(tmp_path / "cdk.out", cdk=dep.Result(1, "Unable to resolve AWS account"))
    with Stubs() as stubs:
        stubs.caller()
        with pytest.raises(dep.DeployError, match="exited 1"):
            _run(stubs, shell, tmp_path)
    assert _deploy_command() not in shell.commands()


def test_a_failed_deploy_writes_no_parameter(tmp_path):
    shell = Shell(tmp_path / "cdk.out", deploy=dep.Result(1, "UPDATE_ROLLBACK_COMPLETE"))
    with Stubs() as stubs:
        _before(stubs)  # nothing after the deploy is queued
        with pytest.raises(dep.DeployError, match="UPDATE_ROLLBACK_COMPLETE"):
            _run(stubs, shell, tmp_path)


def test_code_other_than_the_checked_zip_writes_no_parameter(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="4", prefix="0000.zip")
        with pytest.raises(dep.DeployError, match="not the checked zip"):
            _run(stubs, shell, tmp_path)


def test_a_failed_update_writes_no_parameter(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="4", status="UPDATE_FAILED")
        with pytest.raises(dep.DeployError, match="UPDATE_FAILED"):
            _run(stubs, shell, tmp_path)


def test_a_changed_arn_is_written_and_then_reported(tmp_path):
    new_arn = RUNTIME_ARN.replace("5Izilk9O73", "NewIdXyz12")
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="1", arn=new_arn)
        stubs.put_parameter(value=new_arn)
        stubs.parameter(value=new_arn, version=2)
        record: dict = {}
        with pytest.raises(dep.DeployError, match="never changes: tell the human"):
            dep.run(
                stubs.clients,
                shell,
                record=record,
                out=lambda _l: None,
                sleep=lambda _s: None,
                cdk_out=shell.cdk_out,
            )
        stubs.assert_done()
    assert record["parameter_after"]["value"] == new_arn  # callers keep working


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
def test_the_record_never_holds_the_runtimes_environment_variables(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="4")
        stubs.put_parameter()
        stubs.parameter(version=2)
        record, _ = _run(stubs, shell, tmp_path)
    text = json.dumps(record, default=str)
    assert "env-value-never-recorded" not in text and "SOME_SETTING" not in text
    assert record["runtime_after"]["entryPoint"] == ["opentelemetry-instrument", "main.py"]


def test_main_writes_the_record_even_when_a_step_fails(tmp_path, monkeypatch, capsys):
    stubs = Stubs()
    stubs.caller(account="111122223333")
    monkeypatch.setattr(dep, "_clients", lambda: stubs.clients)
    path = tmp_path / "record.json"
    with stubs:
        assert dep.main(["--plan", "--record", str(path)]) == 1
    written = json.loads(path.read_text())
    assert written["plan"] is True and names.ACCOUNT_ID in written["error"]
    assert "error:" in capsys.readouterr().err


def test_asset_key_is_the_zip_name_without_its_asset_prefix():
    assert dep.asset_key(f"asset.{ASSET_HASH}.zip") == f"{ASSET_HASH}.zip"


def test_a_parameter_that_reads_back_another_value_is_an_error(tmp_path):
    shell = Shell(tmp_path / "cdk.out")
    with Stubs() as stubs:
        _before(stubs)
        stubs.runtime(version="4")
        stubs.put_parameter()
        stubs.parameter(value=RUNTIME_ARN + "-stale", version=2)
        with pytest.raises(dep.DeployError, match="reads back"):
            _run(stubs, shell, tmp_path)
