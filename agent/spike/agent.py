"""rm_agent, task 1.3 spike: a hello-world Strands agent that reaches Flagsmith through rm-gateway.

The runtime answers one JSON payload per invocation. ``action`` picks what it does:

- ``hello``: one model call through Strands, showing the runtime reaches Bedrock.
- ``tools``: the Gateway's MCP tool list, with names, descriptions and input schemas.
- ``setup``: gets or creates the spike's Flagsmith objects by calling MCP tools by name:
  the feature ``feature_key`` (``assistant_model_rspike<HHMM>``, value ``current``, one
  option keyed ``cheaper`` at 50 percent), metric ``spike_mcp_metric`` (mean of
  ``spike_event``, higher is better), one experiment (rollout 100 percent, the metric
  expected "should not decrease") and boolean feature ``spike_kill_switch``. With
  ``omit_rollout`` it sends the experiment without a rollout and returns the reply.
- ``probe``: while the experiment runs, tries to change its feature (then puts back what
  was accepted) and toggles ``spike_kill_switch`` off and on again.
- ``read``: one read-only MCP tool (``get_*`` or ``list_*``).

The model is used only for ``hello``. Every state change is code that calls a tool by
name, never the model's choice (design.md section 7). No action starts or ends an
experiment: the local script starts it and the human ends it. Every reply goes through
the redactor, so the Development environment's key never leaves the runtime.

Sources: Gateway tool names are ``${target_name}___${tool_name}``
(docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html); the
SigV4 client and service name from docs.aws.amazon.com/bedrock/latest/userguide/
kb-gateway-target.html. Flagsmith's API spellings below come from the MCP server's
OpenAPI (github.com/Flagsmith/flagsmith, mcp/src/flagsmith_mcp/openapi.json) and its
metric definition validator (api/experimentation/metric_definitions.py); phase 2 records
the real tool schemas and replies.
"""

from __future__ import annotations

import contextlib
import functools
import json
import re
import uuid
from datetime import datetime
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from contracts import names

# --- names this spike adds (tasks.md 1.3); each starts with names.SPIKE_NAME_PREFIX
SPIKE_WORD = "spike"  # scratch_run_id word for the experiment feature
SPIKE_METRIC = "spike_mcp_metric"
SPIKE_EVENT = "spike_event"
SPIKE_KILL_SWITCH = "spike_kill_switch"

GATEWAY_TOOL_PREFIX = names.GATEWAY_TARGET_FLAGSMITH + "___"
SIGV4_SERVICE = "bedrock-agentcore"

# --- Flagsmith API spellings (task 1.3 records them; 1.4 moves them into contracts)
API_FEATURE_MULTIVARIATE = "MULTIVARIATE"
API_FEATURE_STANDARD = "STANDARD"
API_OPTION_TYPE_STRING = "unicode"
API_VALUE_TYPE_STRING = "string"
API_METRIC_DIRECTION = {names.METRIC_DIRECTION_HIGHER: "up"}
API_EXPECTED_DIRECTION = {names.EXPECTED_DIRECTION_NOT_DECREASE: "not_decrease"}
API_STATUS_COMPLETED = "completed"
METRIC_DEFINITION_VERSION = 1

# Ruling 1 at the 1.1 gate: effort low is approved for the agent's own calls.
AGENT_THINKING_EFFORT = names.ANSWER_THINKING_EFFORT

HELLO_SYSTEM_PROMPT = "You are rm_agent, a deployment check. Answer in one short sentence."
HELLO_PROMPT = "Say hello."
HYPOTHESIS = (
    "Task 1.3 spike: cheaper does not lower spike_mcp_metric. No traffic is sent; "
    "this experiment exists to prove the agent's Flagsmith path."
)
METRIC_DESCRIPTION = "Task 1.3 spike: mean of spike_event, higher is better."
PROBE_DESCRIPTION = "Task 1.3 spike: written while the experiment runs, to test the lock."
PROBE_ALLOCATION = names.TREATMENT_PERCENT - 10  # tried while the experiment runs, then put back

READ_ONLY_PREFIXES = ("get_", "list_")
SECRET_FIELDS = frozenset({"api_key", "environment_api_key"})
REDACTED = "<redacted>"

_SPIKE_KEY = re.compile(
    re.escape(names.EXPERIMENT_FEATURE_PREFIX + names.RUN_ID_PREFIX + SPIKE_WORD) + r"[0-9]{4}"
)


class SpikeError(RuntimeError):
    """Something the spike cannot go on from. The reply carries the message."""


class BadRequest(SpikeError):
    """The payload asks for something this runtime refuses to do."""


class ToolError(SpikeError):
    """An MCP tool answered with an error."""

    def __init__(self, record: dict) -> None:
        super().__init__(f"{record['tool']} failed: {json.dumps(record.get('result'))[:500]}")
        self.record = record


# ----------------------------------------------------------------- feature keys


def feature_key_for(now: datetime) -> str:
    """assistant_model_rspike<HHMM> for this minute (design.md section 4)."""
    return names.experiment_feature_key(names.scratch_run_id(SPIKE_WORD, now))


def is_spike_feature_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_SPIKE_KEY.fullmatch(value))


# ----------------------------------------------------------------- model


def effort_fields() -> dict:
    """additionalModelRequestFields asking for effort low, at names.THINKING_EFFORT_FIELD."""
    fields: Any = AGENT_THINKING_EFFORT
    for part in reversed(names.THINKING_EFFORT_FIELD.split(".")):
        fields = {part: fields}
    return fields


def build_model() -> BedrockModel:
    """MODEL_AGENT with an explicit maxTokens and no sampling parameter.

    Strands puts temperature and topP in inferenceConfig only when they are configured,
    and they never are here; the unit tests check the formatted request.
    """
    return BedrockModel(
        model_id=names.MODEL_AGENT,
        region_name=names.REGION,
        max_tokens=names.AGENT_MAX_TOKENS,
        additional_request_fields=effort_fields(),
    )


def hello(agent: Any = None) -> dict:
    """One Converse call through Strands. stopReason max_tokens is an error (design.md 3)."""
    if agent is None:
        agent = Agent(
            model=build_model(),
            tools=[],
            system_prompt=HELLO_SYSTEM_PROMPT,
            callback_handler=None,
        )
    result = agent(HELLO_PROMPT)
    stop_reason = str(result.stop_reason)
    if stop_reason == names.STOP_REASON_MAX_TOKENS:
        raise SpikeError(f"stopReason {stop_reason}: the answer was cut off, which is an error")
    content = (result.message or {}).get("content", [])
    return {
        "model_id": names.MODEL_AGENT,
        "max_tokens": names.AGENT_MAX_TOKENS,
        "additional_request_fields": effort_fields(),
        "stop_reason": stop_reason,
        "text": "".join(block.get("text", "") for block in content if isinstance(block, dict)),
        "usage": dict(getattr(result.metrics, "accumulated_usage", None) or {}),
    }


# ----------------------------------------------------------------- MCP plumbing


class Redactor:
    """Replaces secret fields and every known secret string in anything returned."""

    def __init__(self) -> None:
        self.secrets: set[str] = set()

    def add(self, value: Any) -> None:
        if isinstance(value, str) and len(value) >= 4:
            self.secrets.add(value)

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            # Only a string under a secret field's name is a secret; a schema that
            # describes the field is not.
            return {
                key: REDACTED
                if key in SECRET_FIELDS and isinstance(item, str) and item
                else self(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self(item) for item in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, REDACTED)
        return value


def _types(prop: Any) -> set[str]:
    kind = prop.get("type") if isinstance(prop, dict) else None
    return set(kind) if isinstance(kind, list) else {kind}


def _fit(tool: str, schema: dict, arguments: dict) -> dict:
    """Check arguments against the tool's listed input schema; coerce int and str IDs.

    A name listed in ``required`` is allowed even without a property: the live
    create_metric schema requires ``definition`` but does not describe it.
    """
    props = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    missing = sorted(required - set(arguments))
    if missing:
        raise SpikeError(f"{tool}: missing required arguments {missing}")
    if props:
        unknown = sorted(set(arguments) - set(props) - required)
        if unknown:
            raise SpikeError(f"{tool}: arguments {unknown} are not in its input schema")
    fitted = {}
    for key, value in arguments.items():
        kinds = _types(props.get(key))
        if "string" in kinds and "integer" not in kinds and type(value) is int:
            value = str(value)
        elif (
            "integer" in kinds
            and "string" not in kinds
            and isinstance(value, str)
            and value.isdigit()
        ):
            value = int(value)
        fitted[key] = value
    return fitted


def _parse(reply: dict) -> Any:
    structured = reply.get("structuredContent")
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    text = "".join(
        block.get("text", "") for block in reply.get("content") or [] if isinstance(block, dict)
    )
    try:
        return json.loads(text)
    except ValueError:
        return text


def _describe(tool: Any) -> dict:
    mcp_tool = getattr(tool, "mcp_tool", tool)
    annotations = getattr(mcp_tool, "annotations", None)
    if hasattr(annotations, "model_dump"):
        annotations = annotations.model_dump(exclude_none=True)
    return {
        "name": mcp_tool.name,
        "description": getattr(mcp_tool, "description", None),
        "inputSchema": getattr(mcp_tool, "input_schema", None)
        or getattr(mcp_tool, "inputSchema", None)
        or {},
        # mcp 2.x spells it output_schema and warns on the old name, so read it only on 1.x.
        "outputSchema": mcp_tool.output_schema
        if hasattr(mcp_tool, "output_schema")
        else getattr(mcp_tool, "outputSchema", None),
        "annotations": annotations,
    }


def _results(body: Any) -> list:
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        return body["results"]
    return body if isinstance(body, list) else []


def _id(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


class FlagsmithTools:
    """The Flagsmith MCP tools, called by name. Records every call it makes."""

    def __init__(self, client: Any, prefix: str = GATEWAY_TOOL_PREFIX) -> None:
        self.client = client
        self.prefix = prefix
        self.redact = Redactor()
        self.calls: list[dict] = []
        self.schemas: dict[str, dict] | None = None

    def list_tools(self) -> list[dict]:
        listed: list[dict] = []
        token = None
        while True:
            page = self.client.list_tools_sync(pagination_token=token)
            listed.extend(_describe(tool) for tool in page)
            token = getattr(page, "pagination_token", None)
            if not token:
                break
        self.schemas = {tool["name"]: tool["inputSchema"] for tool in listed}
        return listed

    def schema(self, tool: str) -> dict:
        if self.schemas is None:
            self.list_tools()
        name = self.prefix + tool
        if name not in self.schemas:
            raise SpikeError(f"tool {name} is not on the Gateway's tool list")
        return self.schemas[name] or {}

    def attempt(self, tool: str, arguments: dict) -> dict:
        """Call a tool and return the record, whether it succeeded or not."""
        name = self.prefix + tool
        fitted = _fit(name, self.schema(tool), arguments)
        reply = self.client.call_tool_sync(f"rm-spike-{uuid.uuid4().hex}", name, fitted)
        record = {
            "tool": name,
            "arguments": fitted,
            "status": reply.get("status"),
            "result": _parse(reply),
        }
        self.calls.append(record)
        return record

    def call(self, tool: str, arguments: dict) -> Any:
        record = self.attempt(tool, arguments)
        if record["status"] != "success":
            raise ToolError(record)
        return record["result"]


# ----------------------------------------------------------------- Flagsmith steps


def development_environment(tools: FlagsmithTools) -> dict:
    listed = _results(tools.call("list_environments", {"project": names.FLAGSMITH_PROJECT_ID}))
    for environment in listed:
        tools.redact.add(environment.get("api_key"))
    found = [e for e in listed if e.get("name") == names.FLAGSMITH_ENVIRONMENT_NAME]
    if len(found) != 1:
        raise SpikeError(
            f"expected one environment named {names.FLAGSMITH_ENVIRONMENT_NAME} in project "
            f"{names.FLAGSMITH_PROJECT_ID}, found {len(found)}"
        )
    environment = found[0]
    return {
        "id": environment["id"],
        "key": environment["api_key"],
        "use_v2_feature_versioning": environment.get("use_v2_feature_versioning"),
    }


def find_feature(tools: FlagsmithTools, environment: dict, name: str) -> dict | None:
    listed = tools.call(
        "list_project_features",
        {
            "project_pk": names.FLAGSMITH_PROJECT_ID,
            "search": name,
            "environment": environment["id"],
        },
    )
    found = [feature for feature in _results(listed) if feature.get("name") == name]
    return found[0] if found else None


def _options(tools: FlagsmithTools, feature_id: int) -> list[dict]:
    listed = tools.call(
        "list_feature_multivariate_options",
        {"project_pk": names.FLAGSMITH_PROJECT_ID, "feature_pk": feature_id},
    )
    return _results(listed)


def _treatment_option(tools: FlagsmithTools, feature_id: int) -> dict | None:
    options = _options(tools, feature_id)
    keyed = [o for o in options if o.get("key") == names.TREATMENT_OPTION_KEY]
    if keyed:
        if keyed[0].get("string_value") != names.TREATMENT_OPTION_VALUE:
            raise SpikeError(
                f"option {names.TREATMENT_OPTION_KEY} on feature {feature_id} has value "
                f"{keyed[0].get('string_value')!r}, want {names.TREATMENT_OPTION_VALUE!r}"
            )
        return keyed[0]
    unkeyed = [o for o in options if o.get("string_value") == names.TREATMENT_OPTION_VALUE]
    if unkeyed:
        raise SpikeError(
            f"feature {feature_id} has a {names.TREATMENT_OPTION_VALUE} option with key "
            f"{unkeyed[0].get('key')!r}: an option without its key records no exposures"
        )
    return None


def _check_metric(metric: dict) -> list[str]:
    want = {
        "aggregation": names.METRIC_AGGREGATION,
        "direction": API_METRIC_DIRECTION[names.METRIC_DIRECTION_HIGHER],
    }
    problems = [
        f"{field} {metric.get(field)!r} (want {value!r})"
        for field, value in want.items()
        if metric.get(field) != value
    ]
    definition = metric.get("definition")
    if isinstance(definition, dict) and definition.get("event") != SPIKE_EVENT:
        problems.append(f"event {definition.get('event')!r} (want {SPIKE_EVENT!r})")
    return problems


def _path_id(tools: FlagsmithTools, tool: str, value: Any) -> dict:
    """The path id under the name the tool lists: id__path when its body also has an id.

    The live Gateway lists update_* tools with id__path (FastMCP renames the clashing path
    parameter); the Flagsmith OpenAPI calls it id.
    """
    try:
        props = tools.schema(tool).get("properties") or {}
    except SpikeError:
        props = {}
    return {"id__path": value} if "id__path" in props else {"id": value}


def _value_of(state: dict) -> dict:
    """The state's current value as an argument, or nothing when it has none (a boolean flag)."""
    value = state.get("feature_state_value")
    return {} if value is None else {"feature_state_value": value}


def _rollout(option_id: int) -> dict:
    return {
        "enabled": True,
        "rollout_percentage": names.EXPERIMENT_ROLLOUT_PERCENT,
        "feature_state_value": {"type": API_VALUE_TYPE_STRING, "value": names.CONTROL_VALUE},
        "multivariate_feature_state_values": [
            {
                "multivariate_feature_option": option_id,
                "percentage_allocation": names.TREATMENT_PERCENT,
            }
        ],
    }


def setup(tools: FlagsmithTools, feature_key: str, *, omit_rollout: bool = False) -> dict:
    project = names.FLAGSMITH_PROJECT_ID
    created: list[str] = []
    environment = development_environment(tools)  # the first call lists the tools first

    feature = find_feature(tools, environment, feature_key)
    if feature is None:
        feature = tools.call(
            "create_feature",
            {
                "project_pk": project,
                "name": feature_key,
                "type": API_FEATURE_MULTIVARIATE,
                "initial_value": names.CONTROL_VALUE,
                "default_enabled": True,
            },
        )
        created.append(feature_key)
    elif feature.get("type") != API_FEATURE_MULTIVARIATE:
        raise SpikeError(f"feature {feature_key} exists as {feature.get('type')}, not multivariate")
    feature_id = feature["id"]

    option = _treatment_option(tools, feature_id)
    if option is None:
        option = tools.call(
            "create_feature_multivariate_option",
            {
                "project_pk": project,
                "feature_pk": feature_id,
                "feature": feature_id,
                "type": API_OPTION_TYPE_STRING,
                "string_value": names.TREATMENT_OPTION_VALUE,
                "key": names.TREATMENT_OPTION_KEY,
                "default_percentage_allocation": names.TREATMENT_PERCENT,
            },
        )
        created.append(f"{feature_key} option {names.TREATMENT_OPTION_KEY}")
        # Read it back: design.md section 13 asks whether the MCP server sets the key.
        stored = [o for o in _options(tools, feature_id) if o.get("id") == option.get("id")]
        option = stored[0] if stored else option

    metrics = tools.call(
        "list_metrics", {"environment_api_key": environment["key"], "q": SPIKE_METRIC}
    )
    found_metric = [m for m in _results(metrics) if m.get("name") == SPIKE_METRIC]
    if found_metric:
        metric = found_metric[0]
        problems = _check_metric(metric)
        if problems:
            raise SpikeError(f"metric {SPIKE_METRIC} exists but differs: " + "; ".join(problems))
    else:
        metric = tools.call(
            "create_metric",
            {
                "environment_api_key": environment["key"],
                "name": SPIKE_METRIC,
                "description": METRIC_DESCRIPTION,
                "aggregation": names.METRIC_AGGREGATION,
                "direction": API_METRIC_DIRECTION[names.METRIC_DIRECTION_HIGHER],
                "definition": {"version": METRIC_DEFINITION_VERSION, "event": SPIKE_EVENT},
            },
        )
        created.append(SPIKE_METRIC)

    experiments = tools.call(
        "list_experiments", {"environment_api_key": environment["key"], "q": feature_key}
    )
    active = [
        e
        for e in _results(experiments)
        if _id(e.get("feature")) == feature_id and e.get("status") != API_STATUS_COMPLETED
    ]
    experiment, without_rollout = (active[0] if active else None), None
    if experiment is None:
        body = {
            "environment_api_key": environment["key"],
            "feature": feature_id,
            "name": feature_key,
            "hypothesis": HYPOTHESIS,
            "metrics": [
                {
                    "metric": str(metric["id"]),
                    "expected_direction": API_EXPECTED_DIRECTION[
                        names.EXPECTED_DIRECTION_NOT_DECREASE
                    ],
                }
            ],
        }
        if omit_rollout:
            without_rollout = tools.attempt("create_experiment", body)
            if without_rollout["status"] == "success":
                experiment = without_rollout["result"]
                created.append(f"experiment {_id(experiment)} without rollout")
        else:
            experiment = tools.call(
                "create_experiment", {**body, "experiment_rollout": _rollout(option["id"])}
            )
            created.append(f"experiment {_id(experiment)}")

    switch = find_feature(tools, environment, SPIKE_KILL_SWITCH)
    if switch is None:
        switch = tools.call(
            "create_feature",
            {
                "project_pk": project,
                "name": SPIKE_KILL_SWITCH,
                "type": API_FEATURE_STANDARD,
                "default_enabled": names.KILL_SWITCH_DEFAULT_ENABLED,
            },
        )
        created.append(SPIKE_KILL_SWITCH)
    elif switch.get("type") != API_FEATURE_STANDARD:
        raise SpikeError(f"feature {SPIKE_KILL_SWITCH} exists as {switch.get('type')}, not boolean")

    return {
        "feature_key": feature_key,
        "feature_id": feature_id,
        "option_id": option["id"],
        "option_key_set_by_mcp": option.get("key") == names.TREATMENT_OPTION_KEY,
        "metric_id": metric["id"],
        "experiment_id": _id(experiment) if experiment else None,
        "experiment_status": experiment.get("status") if isinstance(experiment, dict) else None,
        "experiment_without_rollout": without_rollout,
        "kill_switch_id": switch["id"],
        "environment": {
            "id": environment["id"],
            "use_v2_feature_versioning": environment["use_v2_feature_versioning"],
        },
        "created": created,
    }


def probe(tools: FlagsmithTools, feature_key: str) -> dict:
    """Try to change the running experiment's feature; toggle spike_kill_switch."""
    project = names.FLAGSMITH_PROJECT_ID
    environment = development_environment(tools)  # the first call lists the tools first
    feature = find_feature(tools, environment, feature_key)
    switch = find_feature(tools, environment, SPIKE_KILL_SWITCH)
    if feature is None or switch is None:
        raise SpikeError(f"run setup first: {feature_key} or {SPIKE_KILL_SWITCH} is missing")
    option = _treatment_option(tools, feature["id"])
    state = feature.get("environment_feature_state") or {}
    switch_state = switch.get("environment_feature_state") or {}
    if option is None or "id" not in state or "id" not in switch_state:
        raise SpikeError("the feature's option or an environment feature state was not listed")
    attempts: list[dict] = []

    def try_then_restore(tool: str, change: dict, restore: dict) -> None:
        try:
            record = tools.attempt(tool, change)
        except SpikeError as exc:  # the arguments do not fit the listed schema: nothing sent
            attempts.append(
                {
                    "tool": tools.prefix + tool,
                    "arguments": change,
                    "status": "not_sent",
                    "result": str(exc),
                }
            )
            return
        attempts.append(record)
        if record["status"] == "success":
            attempts.append(tools.attempt(tool, restore))

    option_args = {
        "project_pk": project,
        "feature_pk": feature["id"],
        **_path_id(tools, "update_feature_multivariate_option", option["id"]),
        "feature": feature["id"],
        "type": option.get("type", API_OPTION_TYPE_STRING),
        "string_value": option.get("string_value"),
        "key": option.get("key"),
    }
    allocation = option.get("default_percentage_allocation", names.TREATMENT_PERCENT)
    try_then_restore(
        "update_feature_multivariate_option",
        {**option_args, "default_percentage_allocation": PROBE_ALLOCATION},
        {**option_args, "default_percentage_allocation": allocation},
    )
    state_args = {
        "environment_api_key": environment["key"],
        **_path_id(tools, "update_environment_feature_state", state["id"]),
        "feature": feature["id"],
        # Sent unchanged, so a full-object PUT cannot blank the control value.
        **_value_of(state),
    }
    enabled = bool(state.get("enabled", True))
    try_then_restore(
        "update_environment_feature_state",
        {**state_args, "enabled": not enabled},
        {**state_args, "enabled": enabled},
    )
    feature_args = {
        "project_pk": project,
        **_path_id(tools, "update_feature", feature["id"]),
        "name": feature_key,
    }
    try_then_restore(
        "update_feature",
        {**feature_args, "description": PROBE_DESCRIPTION},
        {**feature_args, "description": feature.get("description") or ""},
    )
    switch_args = {
        "environment_api_key": environment["key"],
        **_path_id(tools, "update_environment_feature_state", switch_state["id"]),
        "feature": switch["id"],
        **_value_of(switch_state),
    }
    switch_enabled = bool(switch_state.get("enabled", True))
    try_then_restore(
        "update_environment_feature_state",
        {**switch_args, "enabled": not switch_enabled},
        {**switch_args, "enabled": switch_enabled},
    )
    return {
        "feature_key": feature_key,
        "feature_id": feature["id"],
        "kill_switch_id": switch["id"],
        "environment": {
            "id": environment["id"],
            "use_v2_feature_versioning": environment["use_v2_feature_versioning"],
        },
        "attempts": attempts,
    }


def read(tools: FlagsmithTools, tool: Any, arguments: Any) -> dict:
    if not isinstance(tool, str) or not tool.startswith(READ_ONLY_PREFIXES):
        raise BadRequest(f"read takes a get_ or list_ tool, not {tool!r}")
    args = dict(arguments) if isinstance(arguments, dict) else {}
    schema = tools.schema(tool)
    if "environment_api_key" in (schema.get("properties") or {}):
        args["environment_api_key"] = development_environment(tools)["key"]
    return {"tool": tools.prefix + tool, "result": tools.call(tool, args)}


# ----------------------------------------------------------------- transports


@functools.cache
def gateway_url() -> str:
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=names.REGION)
    token = None
    while True:
        page = control.list_gateways(**({"nextToken": token} if token else {}))
        for item in page.get("items", []):
            if item.get("name") == names.GATEWAY_NAME:
                return control.get_gateway(gatewayIdentifier=item["gatewayId"])["gatewayUrl"]
        token = page.get("nextToken")
        if not token:
            raise SpikeError(f"gateway {names.GATEWAY_NAME} not found in {names.REGION}")


def gateway_tools() -> FlagsmithTools:
    """MCP over rm-gateway, signed with the runtime role's credentials (SigV4)."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client  # in the CodeZip only

    url = gateway_url()
    client = MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=url, aws_service=SIGV4_SERVICE, aws_region=names.REGION
        )
    )
    return FlagsmithTools(client)


def direct_tools() -> FlagsmithTools:
    """Design.md section 7's fallback: straight to the Flagsmith MCP server with the key.

    Used only when the payload asks for transport "direct", and only works once the
    runtime role may read the secret, which is granted only if the fallback is taken.
    """
    import boto3

    secret = boto3.client("secretsmanager", region_name=names.REGION).get_secret_value(
        SecretId=names.SECRET_FLAGSMITH_API_KEY
    )
    key = secret["SecretString"]
    client = MCPClient(
        url=names.FLAGSMITH_MCP_URL,
        headers={names.GATEWAY_API_KEY_HEADER: names.GATEWAY_API_KEY_PREFIX + key},
    )
    tools = FlagsmithTools(client, prefix="")
    tools.redact.add(key)
    return tools


# ----------------------------------------------------------------- entrypoint

ACTIONS = ("hello", "tools", "setup", "probe", "read")


def handle(payload: Any, *, tools: FlagsmithTools | None = None) -> dict:
    """Run one action; always return a JSON-safe reply with secrets redacted."""
    payload = payload if isinstance(payload, dict) else {}
    action = payload.get("action")
    reply: dict = {"action": action, "ok": False, "result": None, "error": None}
    try:
        if action not in ACTIONS:
            raise BadRequest(f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
        if action in ("setup", "probe") and not is_spike_feature_key(payload.get("feature_key")):
            raise BadRequest(
                f"feature_key must look like {names.EXPERIMENT_FEATURE_PREFIX}"
                f"{names.RUN_ID_PREFIX}{SPIKE_WORD}<HHMM>, got {payload.get('feature_key')!r}"
            )
        if action == "read" and not str(payload.get("tool", "")).startswith(READ_ONLY_PREFIXES):
            raise BadRequest(f"read takes a get_ or list_ tool, not {payload.get('tool')!r}")
        if action == "hello":
            reply["result"] = hello()
        else:
            if tools is None:
                tools = direct_tools() if payload.get("transport") == "direct" else gateway_tools()
            session = (
                tools.client if hasattr(tools.client, "__enter__") else contextlib.nullcontext()
            )
            with session:
                if action == "tools":
                    reply["result"] = {"tools": tools.list_tools()}
                elif action == "setup":
                    reply["result"] = setup(
                        tools,
                        payload["feature_key"],
                        omit_rollout=bool(payload.get("omit_rollout")),
                    )
                elif action == "probe":
                    reply["result"] = probe(tools, payload["feature_key"])
                else:
                    reply["result"] = read(tools, payload.get("tool"), payload.get("arguments"))
        reply["ok"] = True
    except BadRequest as exc:
        reply["error"] = {"code": names.ERROR_BAD_REQUEST, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the runtime always answers with JSON
        reply["error"] = {
            "code": names.ERROR_EFFECT_FAILED,
            "message": f"{type(exc).__name__}: {exc}",
        }
    if tools is not None:
        reply["calls"] = tools.calls
        return tools.redact(reply)
    return reply


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: Any) -> dict:
    return handle(payload)


if __name__ == "__main__":
    app.run()
