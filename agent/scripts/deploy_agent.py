"""Deploy rm_agent with the AgentCore CLI and record what shipped (tasks.md 2.7).

The CLI deploys the runtime rm_default_rm_agent through its own CDK stack AgentCore-rm-default
(design.md section 7). This script wraps one deploy, so that every deploy leaves the same
record. Each step stops the run on failure, and nothing after it runs.

Before the deploy:

1. The caller's account is 818928662830 (STS). The CLI is 1.x, from 1.3.0 on.
2. The commit. agent/runtime/, contracts/, agentcore.json and the CDK stack's lib/ (its
   grants ship too) must hold no uncommitted change, so that the commit names what ships.
3. ``git diff agent/agentcore/cdk/package.json``. The CLI rewrites that file to the versions
   it ships.
4. ``agentcore build``, the CodeZip check (check_runtime_zip.py), and the CDK diff of the built
   stack against the deployed one. The diff runs ``cdk diff --method template``: it uses the
   lookup role and makes no change set, so it writes nothing.
5. The runtime as deployed now, found by its name, and /rm/agent-runtime-arn as it is.

The deploy (skipped with --plan):

6. ``agentcore deploy --target default --json``.
7. The runtime again: its ARN, its version, and READY. Its code must be the zip that passed
   the check: the artifact's S3 key is the asset's name.
8. ``put-parameter --overwrite`` of /rm/agent-runtime-arn with the ARN, then ``get-parameter``.
9. The package.json diff and ``git status`` of agent/agentcore again: what the CLI rewrote.

The ARN must not change from one deploy to the next (design.md section 7). If it does, the new
ARN is still written, so callers keep working, and the run ends in an error for the human.

The record is JSON. It holds no secret: it takes named fields from GetAgentRuntime, never the
runtime's environment variables. With --record, it is written even when a step fails or a call
raises, such as an expired token after the minutes-long deploy, or a command's timeout. Run it
with the human's credentials:

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=abisb_IsbAdminsPS-818928662830
    uv run python agent/scripts/deploy_agent.py --plan --record <file>   # reads only
    uv run python agent/scripts/deploy_agent.py --record <file>          # deploys

Sources:
- ListAgentRuntimes, GetAgentRuntime and the runtime statuses:
  docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/.
- PutParameter's Overwrite: docs.aws.amazon.com/systems-manager/latest/APIReference/.
- ``cdk diff --method``: the CDK CLI 2.1126.0's own help.
- The CLI's deploy and its telemetry switch: task 1.3's evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root, for contracts

from agent.scripts import check_runtime_zip  # noqa: E402
from contracts import names  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"  # the CLI project's root: agent/agentcore/agentcore.json
CDK_DIR = AGENT_DIR / "agentcore" / "cdk"
CDK_BIN = CDK_DIR / "node_modules" / ".bin" / "cdk"  # the CDK CLI that package.json pins
PACKAGE_JSON = "agent/agentcore/cdk/package.json"
CLI_PROJECT_DIR = "agent/agentcore/"
CODE_PATHS = (
    names.AGENT_CODE_DIR,
    "contracts/",
    "agent/agentcore/agentcore.json",
    "agent/agentcore/cdk/lib/",  # the stack's own grants ship too
)
CLI_ENV = {"AGENTCORE_TELEMETRY_DISABLED": "1"}  # as in task 1.3
COMMAND_TIMEOUT_SECONDS = 1800
TIMEOUT_EXIT = 124  # as coreutils' timeout reports a timed-out command
NOT_FOUND_EXIT = 127  # as a shell reports a command it cannot find
READY = "READY"
SETTLING = frozenset({"CREATING", "UPDATING"})
WAIT_SECONDS = 300
POLL_SECONDS = 10
PARAMETER_TYPE = "String"


class DeployError(RuntimeError):
    """A step failed or found something that differs from the contract."""


class Result(NamedTuple):
    code: int
    out: str  # stdout and stderr, interleaved as printed


Runner = Callable[[Sequence[str], Path, dict[str, str]], Result]


def run_command(cmd: Sequence[str], cwd: Path, env: dict[str, str]) -> Result:
    try:
        done = subprocess.run(
            list(cmd),
            cwd=cwd,
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return Result(NOT_FOUND_EXIT, f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired as exc:
        # The output so far is bytes whatever text= says, or None when nothing was printed
        # (the subprocess docs, and a run on Python 3.13.3).
        partial = exc.output.decode(errors="replace") if isinstance(exc.output, bytes) else ""
        message = f"{cmd[0]}: no exit after {COMMAND_TIMEOUT_SECONDS} s"
        return Result(TIMEOUT_EXIT, f"{partial}\n{message}" if partial else message)
    return Result(done.returncode, done.stdout)


class Clients(NamedTuple):
    sts: Any
    control: Any  # bedrock-agentcore-control
    ssm: Any


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.strip().split("."))


def cli_version_ok(text: str) -> bool:
    """1.x from names.AGENTCORE_CLI_MIN_VERSION on (design.md section 2)."""
    try:
        found, least = _version(text), _version(names.AGENTCORE_CLI_MIN_VERSION)
    except ValueError:
        return False
    return found[0] == least[0] and found >= least


def runtime_record(found: dict) -> dict:
    """GetAgentRuntime's fields for the record, by name: never its environment variables."""
    code = (found.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}
    s3 = (code.get("code") or {}).get("s3") or {}
    return {
        "arn": found["agentRuntimeArn"],
        "id": found["agentRuntimeId"],
        "name": found["agentRuntimeName"],
        "version": found["agentRuntimeVersion"],
        "status": found["status"],
        "lastUpdatedAt": found["lastUpdatedAt"],
        "roleArn": found["roleArn"],
        "runtime": code.get("runtime"),
        "entryPoint": code.get("entryPoint"),
        "code": {"bucket": s3.get("bucket"), "prefix": s3.get("prefix")},
    }


def asset_key(zip_name: str) -> str:
    """A CDK file asset asset.<hash>.zip is uploaded under the S3 key <hash>.zip."""
    return zip_name.removeprefix("asset.")


class Deploy:
    def __init__(
        self,
        clients: Clients,
        runner: Runner,
        record: dict,
        *,
        plan_only: bool,
        out: Callable[[str], None],
        sleep: Callable[[float], None],
        cdk_out: Path,
        code: Path,
    ) -> None:
        self.c = clients
        self.runner = runner
        self.record = record
        self.plan_only = plan_only
        self.out = out
        self.sleep = sleep
        self.cdk_out = cdk_out
        self.code = code

    def say(self, message: str) -> None:
        self.out(("plan: " if self.plan_only else "") + message)

    def sh(self, cmd: Sequence[str], cwd: Path, env: dict[str, str] | None = None) -> str:
        result = self.runner(cmd, cwd, env or {})
        if result.code != 0:
            raise DeployError(f"`{' '.join(cmd)}` exited {result.code}: {result.out[-2000:]}")
        return result.out

    def cli(self, *args: str) -> str:
        return self.sh([names.AGENTCORE_CLI_COMMAND, *args], AGENT_DIR, CLI_ENV)

    # 1. account and CLI ----------------------------------------------------------

    def check_account(self) -> None:
        account = self.c.sts.get_caller_identity()["Account"]
        if account != names.ACCOUNT_ID:
            raise DeployError(
                f"credentials are for account {account}, not {names.ACCOUNT_ID}: stop and "
                "tell the human"
            )
        self.record["account"] = account
        self.record["region"] = names.REGION

    def check_cli(self) -> None:
        version = (self.cli("--version").strip().splitlines() or [""])[-1].strip()
        if not cli_version_ok(version):
            raise DeployError(
                f"{names.AGENTCORE_CLI_COMMAND} is {version!r}: want 1.x from "
                f"{names.AGENTCORE_CLI_MIN_VERSION} on"
            )
        self.record["cli_version"] = version

    # 2-3. the commit and package.json ---------------------------------------------

    def check_git(self) -> None:
        self.record["commit"] = self.sh(["git", "rev-parse", "HEAD"], ROOT).strip()
        changes = self.sh(["git", "status", "--porcelain", "--", *CODE_PATHS], ROOT)
        self.record["uncommitted_code"] = changes.splitlines()
        if changes.strip() and not self.plan_only:
            raise DeployError(
                f"uncommitted changes under {', '.join(CODE_PATHS)}; commit them first, so "
                "that the recorded commit is the code that ships"
            )
        diff = self.sh(["git", "diff", "--", PACKAGE_JSON], ROOT)
        self.record["package_json_diff_before"] = diff

    # 4. build, zip check, CDK diff -------------------------------------------------

    def build(self) -> None:
        self.say("agentcore build")
        self.record["build"] = self.cli("build", "--json").strip()
        self.record["zip_before"] = self.check_zip()

    def check_zip(self) -> dict:
        try:
            return check_runtime_zip.check_zip(
                check_runtime_zip.find_code_zip(self.cdk_out), self.code
            )
        except check_runtime_zip.ZipError as exc:
            raise DeployError(f"zip check: {exc}") from exc

    def cdk_diff(self) -> None:
        self.say(f"cdk diff {names.AGENT_STACK} (template comparison)")
        command = [
            str(CDK_BIN),
            "diff",
            "--app",
            str(self.cdk_out),
            "--method",
            "template",
            "--no-color",
            names.AGENT_STACK,
        ]
        self.record["cdk_diff"] = self.sh(command, CDK_DIR)

    # 5 and 7. the runtime and the parameter ----------------------------------------

    def runtime(self) -> dict | None:
        token = None
        while True:
            page = self.c.control.list_agent_runtimes(**({"nextToken": token} if token else {}))
            for item in page["agentRuntimes"]:
                if item["agentRuntimeName"] == names.AGENT_RUNTIME_DEPLOYED_NAME:
                    found = self.c.control.get_agent_runtime(agentRuntimeId=item["agentRuntimeId"])
                    return runtime_record(found)
            token = page.get("nextToken")
            if not token:
                return None

    def settled_runtime(self) -> dict:
        waited = 0
        while True:
            found = self.runtime()
            if found is None:
                raise DeployError(f"{names.AGENT_RUNTIME_DEPLOYED_NAME} not found after the deploy")
            if found["status"] not in SETTLING:
                break
            if waited >= WAIT_SECONDS:
                raise DeployError(f"runtime still {found['status']} after {WAIT_SECONDS} s")
            self.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        if found["status"] != READY:
            raise DeployError(f"runtime is {found['status']} after the deploy")
        return found

    def parameter(self) -> dict | None:
        try:
            found = self.c.ssm.get_parameter(Name=names.AGENT_RUNTIME_ARN_PARAMETER)["Parameter"]
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) == "ParameterNotFound":
                return None
            raise
        return {
            "name": found["Name"],
            "type": found["Type"],
            "value": found["Value"],
            "version": found["Version"],
            "lastModifiedDate": found.get("LastModifiedDate"),
        }

    # 6-9. the deploy -------------------------------------------------------------

    def deploy(self) -> None:
        self.say(f"agentcore deploy --target {names.AGENTCORE_TARGET_NAME} (a few minutes)")
        self.record["deploy_output"] = self.cli(
            "deploy", "--target", names.AGENTCORE_TARGET_NAME, "--json"
        )

    def write_parameter(self, arn: str) -> None:
        self.c.ssm.put_parameter(
            Name=names.AGENT_RUNTIME_ARN_PARAMETER,
            Value=arn,
            Type=PARAMETER_TYPE,
            Overwrite=True,
        )
        self.say(f"put-parameter {names.AGENT_RUNTIME_ARN_PARAMETER} --overwrite")

    def after(self) -> None:
        self.record["package_json_diff_after"] = self.sh(["git", "diff", "--", PACKAGE_JSON], ROOT)
        cli_changes = self.sh(["git", "status", "--porcelain", "--", CLI_PROJECT_DIR], ROOT)
        self.record["cli_project_changes_after"] = cli_changes.splitlines()


def run(
    clients: Clients,
    runner: Runner = run_command,
    *,
    plan_only: bool = False,
    record: dict | None = None,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    cdk_out: Path = check_runtime_zip.CDK_OUT,
    code: Path = check_runtime_zip.CODE,
) -> dict:
    """One deploy, or with plan_only every read before it. Fills and returns the record."""
    record = {} if record is None else record
    record["plan"] = plan_only
    step = Deploy(
        clients,
        runner,
        record,
        plan_only=plan_only,
        out=out,
        sleep=sleep,
        cdk_out=cdk_out,
        code=code,
    )
    step.check_account()
    step.check_cli()
    step.check_git()
    step.build()
    step.cdk_diff()
    before = record["runtime_before"] = step.runtime()
    previous = record["parameter_before"] = step.parameter()
    if plan_only:
        step.say("no deploy and no put-parameter")
        return record

    step.deploy()
    record["zip_after"] = step.check_zip()  # the deploy's own build
    found = record["runtime_after"] = step.settled_runtime()
    if found["code"]["prefix"] != asset_key(record["zip_after"]["zip"]):
        raise DeployError(
            f"the runtime's code {found['code']} is not the checked zip "
            f"{record['zip_after']['zip']}: no parameter written"
        )
    step.write_parameter(found["arn"])
    written = record["parameter_after"] = step.parameter()
    if written is None or written["value"] != found["arn"]:
        raise DeployError(f"{names.AGENT_RUNTIME_ARN_PARAMETER} reads back {written}")
    step.after()
    record["deployed"] = {
        "commit": record["commit"],
        "runtime_version": found["version"],
        "runtime_arn": found["arn"],
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    earlier = {x for x in ((previous or {}).get("value"), (before or {}).get("arn")) if x}
    if earlier - {found["arn"]}:
        raise DeployError(
            f"the runtime's ARN changed from {sorted(earlier)} to {found['arn']}. "
            f"{names.AGENT_RUNTIME_ARN_PARAMETER} now holds the new one, but design.md "
            "section 7 says the ARN never changes: tell the human"
        )
    step.say(f"deployed version {found['version']} of {found['arn']} at {record['commit']}")
    return record


def _clients() -> Clients:
    import boto3

    session = boto3.Session(region_name=names.REGION)
    return Clients(
        sts=session.client("sts"),
        control=session.client("bedrock-agentcore-control"),
        ssm=session.client("ssm"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="every read and check; no deploy")
    parser.add_argument("--record", type=Path, help="write the record (JSON) to this file")
    args = parser.parse_args(argv)
    record: dict = {}
    status = 0
    try:
        run(_clients(), plan_only=args.plan, record=record)
    except Exception as exc:  # noqa: BLE001 -- any failure still leaves its record
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"error: {record['error']}", file=sys.stderr)
        status = 1
    text = json.dumps(record, indent=2, default=str)
    if args.record:
        args.record.write_text(text + "\n")
    print(text)
    return status


if __name__ == "__main__":
    sys.exit(main())
