"""Get or create the AgentCore Gateway pieces of design.md section 7 (tasks 1.3 and 2.7).

Each piece is found by name and created only when missing, in this order:

1. API key credential provider ``rm-flagsmith-api-key``. On create only, the
   organisation key is read from Secrets Manager ``rm/flagsmith/api-key`` with the
   caller's credentials and passed in CreateApiKeyCredentialProvider's ``apiKey``
   field. It is held in memory only and never printed, logged or written.
2. Gateway service role ``rm-gateway-role``, trusted only by this account's
   ``rm-gateway``. Its inline policy (same name) is set to its value on every run: the
   API key's path, and ``lambda:InvokeFunction`` on ``rm-flagsmith-admin`` only.
3. Gateway ``rm-gateway``: protocol MCP, inbound auth AWS_IAM.
4. MCP server target ``flagsmith`` for https://mcp.flagsmith.com, with API-key outbound
   auth in header ``Authorization`` with prefix ``Api-Key`` (the service stores it
   trimmed; that it sends one space before the key is inferred, task 1.3).
5. Lambda target ``flagsmith-admin`` for ``rm-flagsmith-admin`` (task 2.2), with the
   Gateway's service role as its outbound auth, the only kind a Lambda target takes. Its
   tool schema lists the function's five tools, each with its one-line description and
   its arguments, from agent/flagsmith_admin/handler.py. CreateGatewayTarget checks the
   role's invoke permission, so the create is retried while the policy set in step 2
   propagates.

A piece that exists but differs from the contract stops the run with an error for
the human; nothing is changed to make it match. Run it with the human's credentials:

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=abisb_IsbAdminsPS-818928662830
    uv run python agent/scripts/setup_gateway.py --plan   # reads only
    uv run python agent/scripts/setup_gateway.py          # gets or creates

Sources: CreateApiKeyCredentialProvider, CreateGateway, CreateGatewayTarget and
ToolDefinition in docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/; the
service role's trust and permissions from docs.aws.amazon.com/bedrock-agentcore/latest/
devguide/gateway-prerequisites-permissions.html (a Lambda target: lambda:InvokeFunction on
the function, no resource policy in the same account) and gateway-outbound-auth.html; a
Lambda target's GATEWAY_IAM_ROLE credentials from gateway-building-adding-targets-
authorization.html.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root, for contracts

from agent.flagsmith_admin import handler as admin  # noqa: E402
from contracts import names  # noqa: E402

AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
API_KEY_PROVIDER_TYPE = "API_KEY"
API_KEY_LOCATION = "HEADER"
READY = "READY"
FAILED_STATES = frozenset({"FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL", "DELETING"})
WAIT_SECONDS = 300
POLL_SECONDS = 5
ROLE_PROPAGATION_SECONDS = 60  # a new IAM role, or a role's new policy, takes seconds to apply

DESCRIPTION = "Rollout Marshal (design.md section 7): Flagsmith MCP tools for rm_agent"
ADMIN_DESCRIPTION = (
    "Rollout Marshal (design.md section 7): rm-flagsmith-admin's Admin API tools for rm_agent"
)
GATEWAY_IAM_ROLE = "GATEWAY_IAM_ROLE"  # a Lambda target's only outbound auth
# The JSON schema of each argument rm-flagsmith-admin's tools take (handler.TOOL_ARGUMENTS).
ADMIN_ARGUMENT_SCHEMAS = {
    "environment_api_key": {
        "type": "string",
        "description": "The Development environment's API key, from list_environments",
    },
    "experiment_id": {"type": "integer", "description": "The Flagsmith experiment's ID"},
}


class SetupError(RuntimeError):
    """A piece is missing, unusable or differs from the contract. Fix it by hand."""


class Clients(NamedTuple):
    sts: Any
    secrets: Any
    iam: Any
    control: Any  # bedrock-agentcore-control


def _arn(service: str, resource: str) -> str:
    return f"arn:aws:{service}:{names.REGION}:{names.ACCOUNT_ID}:{resource}"


def gateway_arn_pattern() -> str:
    """Gateway IDs are the name plus a 10-character suffix, e.g. rm-gateway-abcde12345."""
    return _arn("bedrock-agentcore", f"gateway/{names.GATEWAY_NAME}-*")


def trust_policy() -> dict:
    """Only bedrock-agentcore, acting for this account's rm-gateway, may assume the role."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GatewayAssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {"Service": AGENTCORE_PRINCIPAL},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": names.ACCOUNT_ID},
                    "ArnLike": {"aws:SourceArn": gateway_arn_pattern()},
                },
            }
        ],
    }


def role_policy(provider_arn: str, secret_arn: str) -> dict:
    """What the Gateway's two targets need for their outbound calls, and nothing else: the API
    key's path for flagsmith, and InvokeFunction on rm-flagsmith-admin for flagsmith-admin."""
    directory = _arn("bedrock-agentcore", "workload-identity-directory/default")
    workload = f"{directory}/workload-identity/{names.GATEWAY_NAME}-*"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GetWorkloadAccessToken",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetWorkloadAccessToken"],
                "Resource": [directory, workload],
            },
            {
                "Sid": "GetResourceApiKey",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetResourceApiKey"],
                "Resource": [
                    directory,
                    workload,
                    _arn("bedrock-agentcore", "token-vault/default"),
                    provider_arn,
                ],
            },
            {
                "Sid": "GetSecretValue",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [secret_arn],
            },
            {
                "Sid": "InvokeFlagsmithAdmin",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": [admin_function_arn()],
            },
        ],
    }


def admin_function_arn() -> str:
    return _arn("lambda", f"function:{names.FLAGSMITH_ADMIN_FUNCTION}")


def admin_tool_definitions() -> list[dict]:
    """One ToolDefinition per tool: name, the one-line description the Gateway requires,
    and its arguments. The function refuses unknown or missing arguments itself."""
    return [
        {
            "name": tool,
            "description": admin.TOOL_DESCRIPTIONS[tool],
            "inputSchema": {
                "type": "object",
                "properties": {
                    argument: dict(ADMIN_ARGUMENT_SCHEMAS[argument])
                    for argument in admin.TOOL_ARGUMENTS[tool]
                },
                "required": list(admin.TOOL_ARGUMENTS[tool]),
            },
        }
        for tool in names.FLAGSMITH_ADMIN_TOOLS
    ]


def admin_target_configuration() -> dict:
    return {
        "mcp": {
            "lambda": {
                "lambdaArn": admin_function_arn(),
                "toolSchema": {"inlinePayload": admin_tool_definitions()},
            }
        }
    }


def admin_credential_configuration() -> list[dict]:
    return [{"credentialProviderType": GATEWAY_IAM_ROLE}]


def target_configuration() -> dict:
    return {"mcp": {"mcpServer": {"endpoint": names.FLAGSMITH_MCP_URL}}}


def credential_configuration(provider_arn: str) -> list[dict]:
    return [
        {
            "credentialProviderType": API_KEY_PROVIDER_TYPE,
            "credentialProvider": {
                "apiKeyCredentialProvider": {
                    "providerArn": provider_arn,
                    "credentialParameterName": names.GATEWAY_API_KEY_HEADER,
                    "credentialPrefix": names.GATEWAY_API_KEY_PREFIX,
                    "credentialLocation": API_KEY_LOCATION,
                }
            },
        }
    ]


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code")


def _as_policy(document: Any) -> Any:
    """IAM returns policy documents URL-encoded on the wire; boto3 usually decodes them."""
    if isinstance(document, str):
        return json.loads(urllib.parse.unquote(document))
    return document


def _usable_key(value: Any) -> str:
    """The key as stored, after a shape check that never repeats the value."""
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"secret {names.SECRET_FLAGSMITH_API_KEY} is empty")
    if value.lstrip().startswith(("{", "[")):
        raise SetupError(
            f"secret {names.SECRET_FLAGSMITH_API_KEY} looks like JSON; it must hold the bare key"
        )
    if any(ch.isspace() for ch in value):
        raise SetupError(
            f"secret {names.SECRET_FLAGSMITH_API_KEY} contains whitespace; "
            "it must hold the bare key without the Api-Key prefix"
        )
    return value


class Setup:
    def __init__(
        self,
        clients: Clients,
        *,
        plan_only: bool,
        out: Callable[[str], None],
        sleep: Callable[[float], None],
    ) -> None:
        self.c = clients
        self.plan_only = plan_only
        self.out = out
        self.sleep = sleep
        self.created: list[str] = []
        self.would_create: list[str] = []

    def say(self, message: str) -> None:
        self.out(("plan: " if self.plan_only else "") + message)

    # 0. account ---------------------------------------------------------------

    def check_account(self) -> None:
        account = self.c.sts.get_caller_identity()["Account"]
        if account != names.ACCOUNT_ID:
            raise SetupError(
                f"caller account is {account}; this script runs only in {names.ACCOUNT_ID}"
            )
        self.say(f"account {account}")

    # 1. credential provider ---------------------------------------------------

    def credential_provider(self) -> dict | None:
        name = names.GATEWAY_CREDENTIAL_PROVIDER
        try:
            found = self.c.control.get_api_key_credential_provider(name=name)
        except Exception as exc:  # noqa: BLE001 - only "not found" is expected
            if _error_code(exc) != "ResourceNotFoundException":
                raise
            found = None
        if found is not None:
            self.say(f"credential provider {name} exists (key not read)")
            return {
                "name": name,
                "arn": found["credentialProviderArn"],
                "secret_arn": found["apiKeySecretArn"]["secretArn"],
            }
        if self.plan_only:
            self.would_create.append(name)
            self.say(
                f"would create credential provider {name} from {names.SECRET_FLAGSMITH_API_KEY}"
            )
            return None
        secret = self.c.secrets.get_secret_value(SecretId=names.SECRET_FLAGSMITH_API_KEY)
        created = self.c.control.create_api_key_credential_provider(
            name=name, apiKey=_usable_key(secret.get("SecretString"))
        )
        del secret  # the key lives only in this function's frame, and only this long
        self.created.append(name)
        self.say(f"created credential provider {name}")
        return {
            "name": name,
            "arn": created["credentialProviderArn"],
            "secret_arn": created["apiKeySecretArn"]["secretArn"],
        }

    # 2. service role ----------------------------------------------------------

    def role(self, provider: dict | None) -> str | None:
        name = names.GATEWAY_ROLE
        try:
            found = self.c.iam.get_role(RoleName=name)["Role"]
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) != "NoSuchEntity":
                raise
            found = None
        if found is not None:
            if _as_policy(found["AssumeRolePolicyDocument"]) != trust_policy():
                raise SetupError(
                    f"role {name} exists with another trust policy; fix it by hand to match "
                    "trust_policy() in this script"
                )
            arn = found["Arn"]
            self.say(f"role {name} exists")
        elif self.plan_only:
            self.would_create.append(name)
            self.say(f"would create role {name}")
            return None
        else:
            arn = self.c.iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(trust_policy(), sort_keys=True),
                Description=DESCRIPTION,
            )["Role"]["Arn"]
            self.created.append(name)
            self.say(f"created role {name}")
        if provider is not None and not self.plan_only:
            self.c.iam.put_role_policy(
                RoleName=name,
                PolicyName=name,
                PolicyDocument=json.dumps(
                    role_policy(provider["arn"], provider["secret_arn"]), sort_keys=True
                ),
            )
            self.say(f"set inline policy {name} on role {name}")
        elif provider is not None:
            self.say(f"would set inline policy {name} on role {name} to role_policy()")
        return arn

    # 3. gateway ---------------------------------------------------------------

    def _find_gateway(self) -> dict | None:
        token = None
        while True:
            page = self.c.control.list_gateways(**({"nextToken": token} if token else {}))
            for item in page["items"]:
                if item["name"] == names.GATEWAY_NAME:
                    return self.c.control.get_gateway(gatewayIdentifier=item["gatewayId"])
            token = page.get("nextToken")
            if not token:
                return None

    def _check_gateway(self, gateway: dict, role_arn: str | None) -> None:
        problems = []
        if gateway.get("protocolType") != names.GATEWAY_PROTOCOL:
            problems.append(
                f"protocol {gateway.get('protocolType')} (want {names.GATEWAY_PROTOCOL})"
            )
        if gateway.get("authorizerType") != names.GATEWAY_INBOUND_AUTH:
            problems.append(
                f"inbound auth {gateway.get('authorizerType')} (want {names.GATEWAY_INBOUND_AUTH})"
            )
        if role_arn and gateway.get("roleArn") != role_arn:
            problems.append(f"role {gateway.get('roleArn')} (want {role_arn})")
        if problems:
            raise SetupError(
                f"gateway {names.GATEWAY_NAME} exists but differs: " + "; ".join(problems)
            )

    def _wait_gateway(self, gateway: dict) -> dict:
        deadline = WAIT_SECONDS
        while gateway["status"] != READY:
            if gateway["status"] in FAILED_STATES:
                raise SetupError(
                    f"gateway {names.GATEWAY_NAME} is {gateway['status']}: "
                    f"{gateway.get('statusReasons')}"
                )
            if deadline <= 0:
                raise SetupError(f"gateway {names.GATEWAY_NAME} not READY after {WAIT_SECONDS} s")
            self.sleep(POLL_SECONDS)
            deadline -= POLL_SECONDS
            gateway = self.c.control.get_gateway(gatewayIdentifier=gateway["gatewayId"])
        return gateway

    def gateway(self, role_arn: str | None) -> dict | None:
        found = self._find_gateway()
        if found is not None:
            self._check_gateway(found, role_arn)
            self.say(f"gateway {names.GATEWAY_NAME} exists ({found['gatewayId']})")
            return self._wait_gateway(found)
        if self.plan_only:
            self.would_create.append(names.GATEWAY_NAME)
            self.say(
                f"would create gateway {names.GATEWAY_NAME} "
                f"({names.GATEWAY_PROTOCOL}, {names.GATEWAY_INBOUND_AUTH})"
            )
            return None
        waited = 0
        while True:
            try:
                created = self.c.control.create_gateway(
                    name=names.GATEWAY_NAME,
                    description=DESCRIPTION,
                    roleArn=role_arn,
                    protocolType=names.GATEWAY_PROTOCOL,
                    authorizerType=names.GATEWAY_INBOUND_AUTH,
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry only a role that is not ready yet
                retryable = _error_code(exc) in ("ValidationException", "AccessDeniedException")
                if (
                    not retryable
                    or "role" not in str(exc).lower()
                    or waited >= ROLE_PROPAGATION_SECONDS
                ):
                    raise
                self.sleep(POLL_SECONDS)
                waited += POLL_SECONDS
        self.created.append(names.GATEWAY_NAME)
        self.say(f"created gateway {names.GATEWAY_NAME} ({created['gatewayId']})")
        return self._wait_gateway(created)

    # 4. target ----------------------------------------------------------------

    def _find_target(
        self, gateway_id: str, name: str = names.GATEWAY_TARGET_FLAGSMITH
    ) -> dict | None:
        token = None
        while True:
            kwargs = {"gatewayIdentifier": gateway_id, **({"nextToken": token} if token else {})}
            page = self.c.control.list_gateway_targets(**kwargs)
            for item in page["items"]:
                if item["name"] == name:
                    return self.c.control.get_gateway_target(
                        gatewayIdentifier=gateway_id, targetId=item["targetId"]
                    )
            token = page.get("nextToken")
            if not token:
                return None

    def _check_target(self, target: dict, provider_arn: str) -> None:
        """Compare only what the contract sets, so fields the service fills in do not count."""
        endpoint = (
            target.get("targetConfiguration", {})
            .get("mcp", {})
            .get("mcpServer", {})
            .get("endpoint")
        )
        if endpoint != names.FLAGSMITH_MCP_URL:
            raise SetupError(
                f"target {names.GATEWAY_TARGET_FLAGSMITH} exists with endpoint {endpoint} "
                f"(want {names.FLAGSMITH_MCP_URL})"
            )
        want = credential_configuration(provider_arn)[0]
        found = (target.get("credentialProviderConfigurations") or [{}])[0]
        want_key = dict(want["credentialProvider"]["apiKeyCredentialProvider"])
        found_key = dict(
            (found.get("credentialProvider") or {}).get("apiKeyCredentialProvider") or {}
        )
        # The service stores the prefix trimmed: "Api-Key " comes back as "Api-Key"
        # (seen on the first create, task 1.3 evidence), so compare it trimmed.
        for key in (want_key, found_key):
            if isinstance(key.get("credentialPrefix"), str):
                key["credentialPrefix"] = key["credentialPrefix"].strip()
        differs = found.get("credentialProviderType") != want["credentialProviderType"] or any(
            found_key.get(field) != value for field, value in want_key.items()
        )
        if differs:
            raise SetupError(
                f"target {names.GATEWAY_TARGET_FLAGSMITH} exists with another credential "
                f"configuration: {target.get('credentialProviderConfigurations')}"
            )

    def _wait_target(
        self, gateway_id: str, target: dict, name: str = names.GATEWAY_TARGET_FLAGSMITH
    ) -> dict:
        deadline = WAIT_SECONDS
        while target["status"] != READY:
            if target["status"] in FAILED_STATES or target["status"].endswith("PENDING_AUTH"):
                raise SetupError(
                    f"target {name} is {target['status']}: {target.get('statusReasons')}"
                )
            if deadline <= 0:
                raise SetupError(f"target {name} not READY after {WAIT_SECONDS} s")
            self.sleep(POLL_SECONDS)
            deadline -= POLL_SECONDS
            target = self.c.control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target["targetId"]
            )
        return target

    def target(self, gateway: dict | None, provider: dict | None) -> dict | None:
        if gateway is None or provider is None:
            self.would_create.append(names.GATEWAY_TARGET_FLAGSMITH)
            self.say(
                f"would create target {names.GATEWAY_TARGET_FLAGSMITH} -> {names.FLAGSMITH_MCP_URL}"
            )
            return None
        gateway_id = gateway["gatewayId"]
        found = self._find_target(gateway_id)
        if found is not None:
            self._check_target(found, provider["arn"])
            self.say(f"target {names.GATEWAY_TARGET_FLAGSMITH} exists ({found['targetId']})")
            return self._wait_target(gateway_id, found)
        if self.plan_only:
            self.would_create.append(names.GATEWAY_TARGET_FLAGSMITH)
            self.say(
                f"would create target {names.GATEWAY_TARGET_FLAGSMITH} -> {names.FLAGSMITH_MCP_URL}"
            )
            return None
        created = self.c.control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=names.GATEWAY_TARGET_FLAGSMITH,
            description=DESCRIPTION,
            targetConfiguration=target_configuration(),
            credentialProviderConfigurations=credential_configuration(provider["arn"]),
        )
        self.created.append(names.GATEWAY_TARGET_FLAGSMITH)
        self.say(f"created target {names.GATEWAY_TARGET_FLAGSMITH} ({created['targetId']})")
        return self._wait_target(gateway_id, created)

    # 5. the Lambda target --------------------------------------------------------

    def _check_admin_target(self, target: dict) -> None:
        """The function, the outbound auth, and each tool's name and arguments; the
        descriptions are words, so a service that rewords one changes nothing here."""
        name = names.GATEWAY_TARGET_FLAGSMITH_ADMIN
        found = (target.get("targetConfiguration") or {}).get("mcp", {}).get("lambda") or {}
        problems = []
        if found.get("lambdaArn") != admin_function_arn():
            problems.append(f"function {found.get('lambdaArn')} (want {admin_function_arn()})")
        tools = (found.get("toolSchema") or {}).get("inlinePayload") or []
        shape = [
            (
                t.get("name"),
                t.get("inputSchema", {}).get("required"),
                {
                    k: v.get("type")
                    for k, v in t.get("inputSchema", {}).get("properties", {}).items()
                },
            )
            for t in tools
        ]
        want = [
            (
                t["name"],
                t["inputSchema"]["required"],
                {k: v["type"] for k, v in t["inputSchema"]["properties"].items()},
            )
            for t in admin_tool_definitions()
        ]
        if shape != want:
            problems.append(f"tools {[s[0] for s in shape]} with other arguments than {want}")
        kinds = [
            c.get("credentialProviderType")
            for c in target.get("credentialProviderConfigurations") or []
        ]
        if kinds != [GATEWAY_IAM_ROLE]:
            problems.append(f"outbound auth {kinds} (want [{GATEWAY_IAM_ROLE}])")
        if problems:
            raise SetupError(f"target {name} exists but differs: " + "; ".join(problems))

    def admin_target(self, gateway: dict | None) -> dict | None:
        name = names.GATEWAY_TARGET_FLAGSMITH_ADMIN
        if gateway is None:
            self.would_create.append(name)
            self.say(f"would create target {name} -> {names.FLAGSMITH_ADMIN_FUNCTION}")
            return None
        gateway_id = gateway["gatewayId"]
        found = self._find_target(gateway_id, name)
        if found is not None:
            self._check_admin_target(found)
            self.say(f"target {name} exists ({found['targetId']})")
            return self._wait_target(gateway_id, found, name)
        if self.plan_only:
            self.would_create.append(name)
            self.say(f"would create target {name} -> {names.FLAGSMITH_ADMIN_FUNCTION}")
            return None
        created = self._create_admin_target(gateway_id)
        self.created.append(name)
        self.say(f"created target {name} ({created['targetId']})")
        return self._wait_target(gateway_id, created, name)

    def _create_admin_target(self, gateway_id: str) -> dict:
        """CreateGatewayTarget checks that the Gateway's role may invoke the function. Step 2
        set that policy moments before, and IAM takes a few seconds to apply it: task 2.7's
        first run was refused this way one second after the policy was set. Retry only that
        refusal, for up to ROLE_PROPAGATION_SECONDS."""
        waited = 0
        while True:
            try:
                return self.c.control.create_gateway_target(
                    gatewayIdentifier=gateway_id,
                    name=names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
                    description=ADMIN_DESCRIPTION,
                    targetConfiguration=admin_target_configuration(),
                    credentialProviderConfigurations=admin_credential_configuration(),
                )
            except Exception as exc:  # noqa: BLE001 - retry only a policy not yet applied
                propagating = _error_code(exc) == "ValidationException" and (
                    "lacks permission to invoke lambda" in str(exc).lower()
                )
                if not propagating or waited >= ROLE_PROPAGATION_SECONDS:
                    raise
                self.say(
                    f"the role's invoke permission is not applied yet; again in {POLL_SECONDS} s"
                )
                self.sleep(POLL_SECONDS)
                waited += POLL_SECONDS


def run(
    clients: Clients,
    *,
    plan_only: bool = False,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Get or create every piece; return a summary that holds no secret."""
    setup = Setup(clients, plan_only=plan_only, out=out, sleep=sleep)
    setup.check_account()
    provider = setup.credential_provider()
    role_arn = setup.role(provider)
    gateway = setup.gateway(role_arn)
    target = setup.target(gateway, provider)
    admin_target = setup.admin_target(gateway)
    return {
        "created": setup.created,
        "would_create": setup.would_create,
        "credential_provider": provider,
        "role_arn": role_arn,
        "gateway": None
        if gateway is None
        else {
            "id": gateway["gatewayId"],
            "arn": gateway["gatewayArn"],
            "url": gateway.get("gatewayUrl"),
            "status": gateway["status"],
            "protocolType": gateway.get("protocolType"),
            "authorizerType": gateway.get("authorizerType"),
            "protocolConfiguration": gateway.get("protocolConfiguration"),
            "workloadIdentityDetails": gateway.get("workloadIdentityDetails"),
        },
        "target": None
        if target is None
        else {
            "id": target["targetId"],
            "name": target["name"],
            "status": target["status"],
            "lastSynchronizedAt": target.get("lastSynchronizedAt"),
            "credentialProviderConfigurations": target.get("credentialProviderConfigurations"),
        },
        "admin_target": None
        if admin_target is None
        else {
            "id": admin_target["targetId"],
            "name": admin_target["name"],
            "status": admin_target["status"],
            "lastSynchronizedAt": admin_target.get("lastSynchronizedAt"),
            "lambdaArn": admin_function_arn(),
            "tools": list(names.FLAGSMITH_ADMIN_TOOLS),
        },
    }


def _clients() -> Clients:
    import boto3

    session = boto3.Session(region_name=names.REGION)
    return Clients(
        sts=session.client("sts"),
        secrets=session.client("secretsmanager"),
        iam=session.client("iam"),
        control=session.client("bedrock-agentcore-control"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plan", action="store_true", help="read only: report what would be created"
    )
    args = parser.parse_args(argv)
    try:
        summary = run(_clients(), plan_only=args.plan)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
