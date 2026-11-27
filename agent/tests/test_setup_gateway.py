"""Unit tests for agent/scripts/setup_gateway.py (tasks 1.3 and 2.7).

botocore's Stubber stands in for AWS: no request leaves the process, and every
request is checked against the real service model, so a mistyped parameter
fails here rather than in phase 2.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

from contracts import names

SCRIPT = Path(__file__).resolve().parents[2] / names.GATEWAY_SETUP_SCRIPT


def _load():
    spec = importlib.util.spec_from_file_location("setup_gateway", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sg = _load()

# A stand-in for the organisation key. The tests assert it never reaches output.
FAKE_KEY = "fake-org-key-for-unit-tests-0123456789"
NOW = dt.datetime(2026, 11, 26, 22, 0, tzinfo=dt.UTC)
ACCOUNT = names.ACCOUNT_ID
REGION = names.REGION
PROVIDER_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default/"
    f"apikeycredentialprovider/{names.GATEWAY_CREDENTIAL_PROVIDER}"
)
SECRET_ARN = (
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:"
    f"bedrock-agentcore-identity!default/apikey/{names.GATEWAY_CREDENTIAL_PROVIDER}-AbCdEf"
)
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{names.GATEWAY_ROLE}"
GATEWAY_ID = f"{names.GATEWAY_NAME}-abcde12345"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{GATEWAY_ID}"
GATEWAY_URL = f"https://{GATEWAY_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com/mcp"
TARGET_ID = "TGT1234567"
ADMIN_TARGET_ID = "ADM1234567"
ADMIN_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{names.FLAGSMITH_ADMIN_FUNCTION}"


def _clients():
    session = boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name=REGION
    )
    return sg.Clients(
        sts=session.client("sts"),
        secrets=session.client("secretsmanager"),
        iam=session.client("iam"),
        control=session.client("bedrock-agentcore-control"),
    )


class Stubs:
    """One Stubber per client, activated together, all asserted drained at the end."""

    def __init__(self, clients):
        self.clients = clients
        self.sts = Stubber(clients.sts)
        self.secrets = Stubber(clients.secrets)
        self.iam = Stubber(clients.iam)
        self.control = Stubber(clients.control)
        self.all = (self.sts, self.secrets, self.iam, self.control)

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


def _caller(stubs, account=ACCOUNT):
    stubs.sts.add_response(
        "get_caller_identity",
        {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/x/y", "UserId": "u"},
        {},
    )


def _provider_missing(stubs):
    stubs.control.add_client_error(
        "get_api_key_credential_provider",
        service_error_code="ResourceNotFoundException",
        http_status_code=404,
        expected_params={"name": names.GATEWAY_CREDENTIAL_PROVIDER},
    )


def _provider_found(stubs):
    stubs.control.add_response(
        "get_api_key_credential_provider",
        {
            "apiKeySecretArn": {"secretArn": SECRET_ARN},
            "name": names.GATEWAY_CREDENTIAL_PROVIDER,
            "credentialProviderArn": PROVIDER_ARN,
            "createdTime": NOW,
            "lastUpdatedTime": NOW,
        },
        {"name": names.GATEWAY_CREDENTIAL_PROVIDER},
    )


def _role_missing(stubs):
    stubs.iam.add_client_error(
        "get_role",
        service_error_code="NoSuchEntity",
        http_status_code=404,
        expected_params={"RoleName": names.GATEWAY_ROLE},
    )


def _role_body():
    return {
        "Path": "/",
        "RoleName": names.GATEWAY_ROLE,
        "RoleId": "AROAEXAMPLEEXAMPLE12",
        "Arn": ROLE_ARN,
        "CreateDate": NOW,
        "AssumeRolePolicyDocument": json.dumps(sg.trust_policy()),
    }


def _role_found(stubs, trust=None):
    body = _role_body()
    if trust is not None:
        body["AssumeRolePolicyDocument"] = json.dumps(trust)
    stubs.iam.add_response("get_role", {"Role": body}, {"RoleName": names.GATEWAY_ROLE})


def _put_role_policy(stubs):
    stubs.iam.add_response(
        "put_role_policy",
        {},
        {
            "RoleName": names.GATEWAY_ROLE,
            "PolicyName": names.GATEWAY_ROLE,
            "PolicyDocument": json.dumps(sg.role_policy(PROVIDER_ARN, SECRET_ARN), sort_keys=True),
        },
    )


def _gateway_body(status="READY", auth=names.GATEWAY_INBOUND_AUTH, role=ROLE_ARN):
    return {
        "gatewayArn": GATEWAY_ARN,
        "gatewayId": GATEWAY_ID,
        "gatewayUrl": GATEWAY_URL,
        "createdAt": NOW,
        "updatedAt": NOW,
        "status": status,
        "name": names.GATEWAY_NAME,
        "roleArn": role,
        "protocolType": names.GATEWAY_PROTOCOL,
        "authorizerType": auth,
    }


def _gateway_listed(stubs, present=True):
    items = []
    if present:
        items = [
            {
                "gatewayId": GATEWAY_ID,
                "name": names.GATEWAY_NAME,
                "status": "READY",
                "createdAt": NOW,
                "updatedAt": NOW,
                "authorizerType": names.GATEWAY_INBOUND_AUTH,
                "protocolType": names.GATEWAY_PROTOCOL,
            }
        ]
    stubs.control.add_response("list_gateways", {"items": items}, {})


def _get_gateway(stubs, **kwargs):
    stubs.control.add_response(
        "get_gateway", _gateway_body(**kwargs), {"gatewayIdentifier": GATEWAY_ID}
    )


def _target_config():
    return {"mcp": {"mcpServer": {"endpoint": names.FLAGSMITH_MCP_URL}}}


def _credential_config(prefix=names.GATEWAY_API_KEY_PREFIX):
    return [
        {
            "credentialProviderType": "API_KEY",
            "credentialProvider": {
                "apiKeyCredentialProvider": {
                    "providerArn": PROVIDER_ARN,
                    "credentialParameterName": names.GATEWAY_API_KEY_HEADER,
                    "credentialPrefix": prefix,
                    "credentialLocation": "HEADER",
                }
            },
        }
    ]


def _target_body(status="READY", prefix=names.GATEWAY_API_KEY_PREFIX):
    return {
        "gatewayArn": GATEWAY_ARN,
        "targetId": TARGET_ID,
        "createdAt": NOW,
        "updatedAt": NOW,
        "status": status,
        "name": names.GATEWAY_TARGET_FLAGSMITH,
        "targetConfiguration": _target_config(),
        "credentialProviderConfigurations": _credential_config(prefix),
        "lastSynchronizedAt": NOW,
    }


def _targets_listed(stubs, present=True):
    items = []
    if present:
        items = [
            {
                "targetId": TARGET_ID,
                "name": names.GATEWAY_TARGET_FLAGSMITH,
                "status": "READY",
                "createdAt": NOW,
                "updatedAt": NOW,
            }
        ]
    stubs.control.add_response(
        "list_gateway_targets", {"items": items}, {"gatewayIdentifier": GATEWAY_ID}
    )


def _get_target(stubs, **kwargs):
    stubs.control.add_response(
        "get_gateway_target",
        _target_body(**kwargs),
        {"gatewayIdentifier": GATEWAY_ID, "targetId": TARGET_ID},
    )


def _admin_target_body(status="READY", configuration=None, credentials=None):
    return {
        "gatewayArn": GATEWAY_ARN,
        "targetId": ADMIN_TARGET_ID,
        "createdAt": NOW,
        "updatedAt": NOW,
        "status": status,
        "name": names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
        "targetConfiguration": configuration or sg.admin_target_configuration(),
        "credentialProviderConfigurations": credentials
        or [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        "lastSynchronizedAt": NOW,
    }


def _admin_targets_listed(stubs, present=True):
    """The listing the admin target's lookup reads: flagsmith, and the admin target if any."""
    items = [
        {
            "targetId": TARGET_ID,
            "name": names.GATEWAY_TARGET_FLAGSMITH,
            "status": "READY",
            "createdAt": NOW,
            "updatedAt": NOW,
        }
    ]
    if present:
        items.append(
            {
                "targetId": ADMIN_TARGET_ID,
                "name": names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
                "status": "READY",
                "createdAt": NOW,
                "updatedAt": NOW,
            }
        )
    stubs.control.add_response(
        "list_gateway_targets", {"items": items}, {"gatewayIdentifier": GATEWAY_ID}
    )


def _get_admin_target(stubs, **kwargs):
    stubs.control.add_response(
        "get_gateway_target",
        _admin_target_body(**kwargs),
        {"gatewayIdentifier": GATEWAY_ID, "targetId": ADMIN_TARGET_ID},
    )


def _found_up_to_the_admin_target(stubs):
    """Every piece found, as on a second run, up to the admin target's lookup."""
    _caller(stubs)
    _provider_found(stubs)
    _role_found(stubs)
    _put_role_policy(stubs)
    _gateway_listed(stubs)
    _get_gateway(stubs)
    _targets_listed(stubs)
    _get_target(stubs)


def _run(clients, **kwargs):
    lines = []
    summary = sg.run(clients, out=lines.append, sleep=lambda _s: None, **kwargs)
    return summary, "\n".join(lines)


def test_creates_every_piece_once_when_nothing_exists():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_missing(stubs)
        stubs.secrets.add_response(
            "get_secret_value",
            {"SecretString": FAKE_KEY, "Name": names.SECRET_FLAGSMITH_API_KEY},
            {"SecretId": names.SECRET_FLAGSMITH_API_KEY},
        )
        # The key goes only into this field of this one call.
        stubs.control.add_response(
            "create_api_key_credential_provider",
            {
                "apiKeySecretArn": {"secretArn": SECRET_ARN},
                "name": names.GATEWAY_CREDENTIAL_PROVIDER,
                "credentialProviderArn": PROVIDER_ARN,
            },
            {"name": names.GATEWAY_CREDENTIAL_PROVIDER, "apiKey": FAKE_KEY},
        )
        _role_missing(stubs)
        stubs.iam.add_response(
            "create_role",
            {"Role": _role_body()},
            {
                "RoleName": names.GATEWAY_ROLE,
                "AssumeRolePolicyDocument": json.dumps(sg.trust_policy(), sort_keys=True),
                "Description": ANY,
            },
        )
        _put_role_policy(stubs)
        _gateway_listed(stubs, present=False)
        stubs.control.add_response(
            "create_gateway",
            _gateway_body(status="CREATING"),
            {
                "name": names.GATEWAY_NAME,
                "description": ANY,
                "roleArn": ROLE_ARN,
                "protocolType": names.GATEWAY_PROTOCOL,
                "authorizerType": names.GATEWAY_INBOUND_AUTH,
            },
        )
        _get_gateway(stubs, status="READY")
        _targets_listed(stubs, present=False)
        stubs.control.add_response(
            "create_gateway_target",
            _target_body(status="CREATING"),
            {
                "gatewayIdentifier": GATEWAY_ID,
                "name": names.GATEWAY_TARGET_FLAGSMITH,
                "description": ANY,
                "targetConfiguration": _target_config(),
                "credentialProviderConfigurations": _credential_config(),
            },
        )
        _get_target(stubs, status="READY")
        _admin_targets_listed(stubs, present=False)
        stubs.control.add_response(
            "create_gateway_target",
            _admin_target_body(status="CREATING"),
            {
                "gatewayIdentifier": GATEWAY_ID,
                "name": names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
                "description": ANY,
                "targetConfiguration": sg.admin_target_configuration(),
                "credentialProviderConfigurations": [
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
            },
        )
        _get_admin_target(stubs, status="READY")

        summary, output = _run(clients)
        stubs.assert_done()

    assert summary["created"] == [
        names.GATEWAY_CREDENTIAL_PROVIDER,
        names.GATEWAY_ROLE,
        names.GATEWAY_NAME,
        names.GATEWAY_TARGET_FLAGSMITH,
        names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
    ]
    assert summary["admin_target"]["status"] == "READY"
    assert summary["admin_target"]["lambdaArn"] == ADMIN_ARN
    assert summary["gateway"]["url"] == GATEWAY_URL
    assert summary["target"]["status"] == "READY"
    assert FAKE_KEY not in output
    assert FAKE_KEY not in json.dumps(summary, default=str)


def test_second_run_creates_nothing_and_never_reads_the_secret():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs)
        _put_role_policy(stubs)  # the inline policy is set to its value on every run
        _gateway_listed(stubs)
        _get_gateway(stubs)
        _targets_listed(stubs)
        _get_target(stubs)
        _admin_targets_listed(stubs)
        _get_admin_target(stubs)

        summary, _ = _run(clients)
        stubs.assert_done()  # no get_secret_value and no create_* call was stubbed

    assert summary["created"] == []


def test_plan_makes_no_writes():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_missing(stubs)
        _role_missing(stubs)
        _gateway_listed(stubs, present=False)

        summary, output = _run(clients, plan_only=True)
        stubs.assert_done()

    assert summary["created"] == []
    assert summary["would_create"] == [
        names.GATEWAY_CREDENTIAL_PROVIDER,
        names.GATEWAY_ROLE,
        names.GATEWAY_NAME,
        names.GATEWAY_TARGET_FLAGSMITH,
        names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
    ]
    assert "plan" in output


def test_refuses_any_other_account_before_touching_anything():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs, account="111122223333")
        with pytest.raises(sg.SetupError, match=ACCOUNT):
            _run(clients)
        stubs.assert_done()


def test_found_gateway_with_other_inbound_auth_stops_before_any_target_work():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs)
        _put_role_policy(stubs)
        _gateway_listed(stubs)
        _get_gateway(stubs, auth="NONE")
        with pytest.raises(sg.SetupError, match=names.GATEWAY_INBOUND_AUTH):
            _run(clients)
        stubs.assert_done()


def test_found_role_with_another_trust_policy_stops():
    clients = _clients()
    other = {"Version": "2012-10-17", "Statement": []}
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs, trust=other)
        with pytest.raises(sg.SetupError, match="trust policy"):
            _run(clients)
        stubs.assert_done()


def test_found_target_with_the_prefix_stored_trimmed_counts_as_the_same():
    # On the first create, Api-Key with its trailing space came back as "Api-Key".
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs)
        _put_role_policy(stubs)
        _gateway_listed(stubs)
        _get_gateway(stubs)
        _targets_listed(stubs)
        _get_target(stubs, prefix=names.GATEWAY_API_KEY_PREFIX.strip())
        _admin_targets_listed(stubs)
        _get_admin_target(stubs)
        summary, _ = _run(clients)
        stubs.assert_done()
    assert summary["created"] == []


def test_found_target_with_another_prefix_stops():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs)
        _put_role_policy(stubs)
        _gateway_listed(stubs)
        _get_gateway(stubs)
        _targets_listed(stubs)
        _get_target(stubs, prefix="Bearer ")
        with pytest.raises(sg.SetupError, match="credential"):
            _run(clients)
        stubs.assert_done()


def test_failed_target_sync_is_reported_not_hidden():
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs)
        _put_role_policy(stubs)
        _gateway_listed(stubs)
        _get_gateway(stubs)
        _targets_listed(stubs)
        _get_target(stubs, status="FAILED")
        with pytest.raises(sg.SetupError, match="FAILED"):
            _run(clients)
        stubs.assert_done()


# Secrets Manager refuses an empty SecretString, so "blank" here means whitespace only.
@pytest.mark.parametrize("value", ["  ", '{"key": "x"}', "two words"])
def test_unusable_secret_values_stop_without_echoing_them(value):
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_missing(stubs)
        stubs.secrets.add_response(
            "get_secret_value",
            {"SecretString": value, "Name": names.SECRET_FLAGSMITH_API_KEY},
            {"SecretId": names.SECRET_FLAGSMITH_API_KEY},
        )
        with pytest.raises(sg.SetupError) as raised:
            _run(clients)
        stubs.assert_done()
    if value.strip():
        assert value not in str(raised.value)


def test_trust_policy_lets_only_this_accounts_rm_gateway_assume_the_role():
    statement = sg.trust_policy()["Statement"][0]
    assert statement["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    gateways = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{names.GATEWAY_NAME}-*"
    assert statement["Condition"]["ArnLike"] == {"aws:SourceArn": gateways}


def test_role_policy_grants_only_the_api_key_path_and_the_admin_function():
    policy = sg.role_policy(PROVIDER_ARN, SECRET_ARN)
    actions = {
        action: statement["Resource"]
        for statement in policy["Statement"]
        for action in statement["Action"]
    }
    assert set(actions) == {
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetResourceApiKey",
        "secretsmanager:GetSecretValue",
        "lambda:InvokeFunction",
    }
    assert actions["lambda:InvokeFunction"] == [ADMIN_ARN]
    assert actions["secretsmanager:GetSecretValue"] == [SECRET_ARN]
    assert PROVIDER_ARN in actions["bedrock-agentcore:GetResourceApiKey"]
    directory = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default"
    assert actions["bedrock-agentcore:GetWorkloadAccessToken"] == [
        directory,
        f"{directory}/workload-identity/{names.GATEWAY_NAME}-*",
    ]
    assert all(statement["Effect"] == "Allow" for statement in policy["Statement"])


# ---------------------------------------------------------------------------
# The flagsmith-admin target (task 2.7)
# ---------------------------------------------------------------------------
def test_the_admin_tools_are_the_functions_with_their_descriptions_and_arguments():
    definitions = sg.admin_tool_definitions()
    assert [d["name"] for d in definitions] == list(names.FLAGSMITH_ADMIN_TOOLS)
    for definition in definitions:
        tool = definition["name"]
        assert definition["description"] == sg.admin.TOOL_DESCRIPTIONS[tool] != ""
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        assert schema["required"] == list(sg.admin.TOOL_ARGUMENTS[tool])
        assert set(schema["properties"]) == set(sg.admin.TOOL_ARGUMENTS[tool])
    types = {k: v["type"] for d in definitions for k, v in d["inputSchema"]["properties"].items()}
    assert types == {"environment_api_key": "string", "experiment_id": "integer"}
    configuration = sg.admin_target_configuration()["mcp"]["lambda"]
    assert configuration["lambdaArn"] == ADMIN_ARN
    assert configuration["toolSchema"]["inlinePayload"] == definitions


def test_the_docstring_gives_the_prefix_as_api_key():
    assert f"prefix ``{names.GATEWAY_API_KEY_PREFIX}``" in sg.__doc__
    assert "``Api-Key ``" not in sg.__doc__


def _lambda(arn=ADMIN_ARN, tools=None):
    """A Lambda target's configuration as GetGatewayTarget returns it."""
    tools = sg.admin_tool_definitions() if tools is None else tools
    return {
        "configuration": {
            "mcp": {"lambda": {"lambdaArn": arn, "toolSchema": {"inlinePayload": tools}}}
        }
    }


def _experiment_id_as_string():
    tools = sg.admin_tool_definitions()
    for tool in tools:
        if "experiment_id" in tool["inputSchema"]["properties"]:
            tool["inputSchema"]["properties"]["experiment_id"]["type"] = "string"
    return tools


@pytest.mark.parametrize(
    "change, said",
    [
        (_lambda(arn=ADMIN_ARN + "-other"), "function"),
        (_lambda(tools=sg.admin_tool_definitions()[:4]), "tools"),
        (_lambda(tools=_experiment_id_as_string()), "tools"),
        ({"credentials": [{"credentialProviderType": "API_KEY"}]}, "outbound auth"),
    ],
    ids=["other-function", "a-tool-missing", "an-argument-retyped", "other-auth"],
)
def test_a_found_admin_target_that_differs_stops(change, said):
    clients = _clients()
    with Stubs(clients) as stubs:
        _found_up_to_the_admin_target(stubs)
        _admin_targets_listed(stubs)
        _get_admin_target(stubs, **change)
        with pytest.raises(sg.SetupError, match=said):
            _run(clients)
        stubs.assert_done()


def test_a_failed_admin_target_sync_is_reported_not_hidden():
    clients = _clients()
    with Stubs(clients) as stubs:
        _found_up_to_the_admin_target(stubs)
        _admin_targets_listed(stubs)
        _get_admin_target(stubs, status="FAILED")
        with pytest.raises(
            sg.SetupError, match=f"{names.GATEWAY_TARGET_FLAGSMITH_ADMIN} is FAILED"
        ):
            _run(clients)
        stubs.assert_done()


def test_plan_on_the_spikes_gateway_would_add_only_the_admin_target_and_the_policy():
    """2.7's live run: 1.3 made the provider, role, Gateway and flagsmith target."""
    clients = _clients()
    with Stubs(clients) as stubs:
        _caller(stubs)
        _provider_found(stubs)
        _role_found(stubs)  # no put_role_policy: a plan writes nothing
        _gateway_listed(stubs)
        _get_gateway(stubs)
        _targets_listed(stubs)
        _get_target(stubs)
        _admin_targets_listed(stubs, present=False)
        summary, output = _run(clients, plan_only=True)
        stubs.assert_done()
    assert summary["created"] == []
    assert summary["would_create"] == [names.GATEWAY_TARGET_FLAGSMITH_ADMIN]
    assert summary["admin_target"] is None
    assert f"would set inline policy {names.GATEWAY_ROLE}" in output


def test_the_role_may_invoke_rm_flagsmith_admin_and_no_other_function():
    policy = sg.role_policy(PROVIDER_ARN, SECRET_ARN)
    invoke = [s for s in policy["Statement"] if "lambda:InvokeFunction" in s["Action"]]
    assert invoke == [
        {
            "Sid": "InvokeFlagsmithAdmin",
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": [ADMIN_ARN],
        }
    ]


# CreateGatewayTarget's refusal one second after the role's policy was set (task 2.7's live run).
NOT_YET = (
    "Gateway execution role lacks permission to invoke Lambda function "
    f"{ADMIN_ARN}. Update the permission and retry"
)


def _admin_create_params():
    return {
        "gatewayIdentifier": GATEWAY_ID,
        "name": names.GATEWAY_TARGET_FLAGSMITH_ADMIN,
        "description": ANY,
        "targetConfiguration": sg.admin_target_configuration(),
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }


def _refused(stubs, message=NOT_YET):
    stubs.control.add_client_error(
        "create_gateway_target",
        service_error_code="ValidationException",
        service_message=message,
        http_status_code=400,
        expected_params=_admin_create_params(),
    )


def _run_counting_sleeps(clients):
    sleeps: list[float] = []
    summary = sg.run(clients, out=lambda _line: None, sleep=sleeps.append)
    return summary, sleeps


def test_the_admin_target_is_created_once_the_roles_new_policy_applies():
    clients = _clients()
    with Stubs(clients) as stubs:
        _found_up_to_the_admin_target(stubs)
        _admin_targets_listed(stubs, present=False)
        _refused(stubs)
        stubs.control.add_response(
            "create_gateway_target", _admin_target_body(status="CREATING"), _admin_create_params()
        )
        _get_admin_target(stubs, status="READY")
        summary, sleeps = _run_counting_sleeps(clients)
        stubs.assert_done()
    assert summary["created"] == [names.GATEWAY_TARGET_FLAGSMITH_ADMIN]
    assert sleeps[0] == sg.POLL_SECONDS


def test_a_policy_that_never_applies_stops_after_the_propagation_wait():
    clients = _clients()
    tries = sg.ROLE_PROPAGATION_SECONDS // sg.POLL_SECONDS + 1
    with Stubs(clients) as stubs:
        _found_up_to_the_admin_target(stubs)
        _admin_targets_listed(stubs, present=False)
        for _ in range(tries):
            _refused(stubs)
        with pytest.raises(Exception, match="lacks permission to invoke Lambda"):
            _run_counting_sleeps(clients)
        stubs.assert_done()


def test_any_other_refusal_of_the_admin_target_is_not_retried():
    clients = _clients()
    with Stubs(clients) as stubs:
        _found_up_to_the_admin_target(stubs)
        _admin_targets_listed(stubs, present=False)
        _refused(stubs, message="1 validation error detected: inlinePayload")
        with pytest.raises(Exception, match="inlinePayload"):
            _run_counting_sleeps(clients)
        stubs.assert_done()
