"""The agent's Flagsmith tools (tasks.md 2.2; design.md sections 4 and 7).

Features, the multivariate option and the experiment are made through the Flagsmith MCP
server, which rm-gateway lists as flagsmith___<tool>. Metrics, the start, results and the
experiment's status go through rm-flagsmith-admin (the Admin API): through the Gateway as
flagsmith-admin___<tool> (GatewayAdmin), or by direct invoke before task 2.7 adds that
target (LambdaAdmin). Every write is get-or-create or set-to-value, so each is safe to
repeat.

No experiment can end or change status through this module (Gate 1, rule 1). Mcp calls
only MCP_TOOLS, and refuses experiment arguments that carry a status field; the admin
transports call only ADMIN_TOOLS, and rm-flagsmith-admin can build no path that ends or
pauses an experiment. The one status change anywhere is start_experiment, created to
running.

A feature's state changes only through a v2 feature version (create it, update its default
state, publish it), which Development requires (task 1.3). A found feature's state and
value are never reset (Gate 1, rule 5): a reset of assistant_default_model would undo a
promotion.

This module imports only contracts and the standard library. The MCP client (Strands'
MCPClient, call_tool_sync) and the Lambda client are passed in.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from contracts import names

MCP_TOOL_PREFIX = names.GATEWAY_TARGET_FLAGSMITH + "___"
ADMIN_TOOL_PREFIX = names.GATEWAY_TARGET_FLAGSMITH_ADMIN + "___"

# The MCP tools this module may call (Gate 1, rule 1(a)); any other name is refused before
# the call. update_experiment is not among them: nothing here changes an experiment after
# creating it.
MCP_TOOLS = (
    "list_environments",
    "list_project_features",
    "create_feature",
    "list_feature_multivariate_options",
    "create_feature_multivariate_option",
    "list_experiments",
    "create_experiment",
    "create_environment_feature_version",
    "get_environment_feature_version_states",
    "update_environment_feature_version_state",
    "publish_environment_feature_version",
)
# rm-flagsmith-admin's tools (agent/flagsmith_admin/handler.py, TOOLS).
ADMIN_TOOLS = (
    "ensure_metrics",
    "start_experiment",
    "refresh_results",
    "get_results",
    "get_experiment",
)
# names.MCP_EXPERIMENT_FORBIDDEN_FIELDS are refused anywhere in these tools' arguments
# (Gate 1, rule 1(b)): task 1.3 found both tools' schemas list them.
_EXPERIMENT_WRITE_TOOLS = ("create_experiment", "update_experiment")

# Flagsmith's API spellings, as the evidence records them (Gate 1, rule 4). c2 left them
# here, as module constants.
API_FEATURE_MULTIVARIATE = "MULTIVARIATE"  # a feature's type
API_FEATURE_STANDARD = "STANDARD"
API_OPTION_TYPE = "unicode"  # a multivariate option's type for a string value
API_ROLLOUT_VALUE_TYPE = "string"  # the experiment rollout's value: {"type": "string", ...}
API_VERSION_VALUE_TYPE = "unicode"  # a v2 version state's value: {"type": "unicode", ...}
API_PATH_ID = "id__path"  # MCP's name for a path ID that clashes with a body field

# Two of names.FLAGSMITH_EXPERIMENT_STATUSES by name, for the tests' fake Flagsmith. This
# module compares only names.EXPERIMENT_STATUS_ENDED, and rm-flagsmith-admin's start keeps
# its own pair.
STATUS_CREATED = "created"
STATUS_RUNNING = "running"

# The results label each arm: the control as "control" (task 1.2), the treatment by its key.
_VARIANT_BY_LABEL = {
    names.CONTROL_LABEL: names.VARIANT_CURRENT,
    names.TREATMENT_OPTION_KEY: names.VARIANT_CHEAPER,
}
# How far apart this module's clock and Flagsmith's may be when a read is matched to a
# refresh. Refreshes are RESULTS_REFRESH_MIN_SECONDS apart, so an older refresh never passes.
REFRESH_CLOCK_ALLOWANCE_SECONDS = 60
_PROJECT = names.FLAGSMITH_PROJECT_ID


class FlagsmithError(RuntimeError):
    """A call failed, or what a get-or-create found differs from its definition. A mismatch
    is for the human to fix in the dashboard; the check itself changed nothing."""


class ToolRefused(RuntimeError):
    """This module will not make the call (Gate 1, rule 1). Nothing was sent."""


class ResultsNotCurrent(RuntimeError):
    """The results read does not yet reflect the refresh asked for; read again later."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _time(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _parse(reply: dict) -> Any:
    """The result of a Strands MCPClient.call_tool_sync reply."""
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


def _results(body: Any) -> list:
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        return body["results"]
    return body if isinstance(body, list) else []


def _id(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


def _keys(value: Any) -> set[str]:
    """Every key at any depth."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def check_experiment_arguments(tool: str, arguments: dict) -> None:
    """Refuse create_experiment or update_experiment arguments that carry a status field."""
    if tool in _EXPERIMENT_WRITE_TOOLS:
        found = sorted(_keys(arguments) & set(names.MCP_EXPERIMENT_FORBIDDEN_FIELDS))
        if found:
            raise ToolRefused(f"{tool} may not send {found}: it could change the status")


REDACTED = "<redacted>"


def _redacted(text: Any, arguments: dict) -> str:
    """Upstream text, with the environment key replaced. Every admin call carries it, and so do
    MCP's list_experiments and create_experiment. Whatever the Gateway, the function or
    Flagsmith echoes back, no error raised here carries the key. It is replaced before any
    cut, so no part of it survives one."""
    text, key = str(text), arguments.get("environment_api_key")
    return text.replace(key, REDACTED) if isinstance(key, str) and key else text


class Mcp:
    """The Flagsmith MCP tools through the Gateway, called by name, never by the model."""

    def __init__(self, client: Any, prefix: str = MCP_TOOL_PREFIX) -> None:
        self.client = client
        self.prefix = prefix

    def call(self, tool: str, arguments: dict) -> Any:
        if tool not in MCP_TOOLS:
            raise ToolRefused(f"{tool} is not one of the MCP tools this module calls")
        check_experiment_arguments(tool, arguments)
        reply = self.client.call_tool_sync(f"rm-{uuid.uuid4().hex}", self.prefix + tool, arguments)
        result = _parse(reply)
        if reply.get("status") != "success":
            raise FlagsmithError(f"{tool} failed: {_redacted(result, arguments)[:300]}")
        return result


def _admin_tool(tool: str) -> None:
    if tool not in ADMIN_TOOLS:
        raise ToolRefused(f"{tool} is not one of rm-flagsmith-admin's tools")


def _admin_result(tool: str, answer: Any, arguments: dict) -> Any:
    if not isinstance(answer, dict) or "ok" not in answer:
        copied = _redacted(answer, arguments)[:300]
        raise FlagsmithError(f"{tool}: rm-flagsmith-admin answered {copied}")
    if not answer["ok"]:
        error = answer.get("error") or {}
        raise FlagsmithError(f"{tool}: {_redacted(error.get('message'), arguments)}")
    return answer.get("result")


class GatewayAdmin:
    """rm-flagsmith-admin through the Gateway: tools flagsmith-admin___<tool>."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def call(self, tool: str, arguments: dict) -> Any:
        _admin_tool(tool)
        reply = self.client.call_tool_sync(
            f"rm-{uuid.uuid4().hex}", ADMIN_TOOL_PREFIX + tool, arguments
        )
        if reply.get("status") != "success":
            raise FlagsmithError(f"{tool} failed: {_redacted(_parse(reply), arguments)[:300]}")
        return _admin_result(tool, _parse(reply), arguments)


class LambdaAdmin:
    """rm-flagsmith-admin by direct invoke: {"tool": <tool>, "arguments": {...}}."""

    def __init__(self, client: Any) -> None:
        self.client = client  # a boto3 Lambda client

    def call(self, tool: str, arguments: dict) -> Any:
        _admin_tool(tool)
        response = self.client.invoke(
            FunctionName=names.FLAGSMITH_ADMIN_FUNCTION,
            Payload=json.dumps({"tool": tool, "arguments": arguments}),
        )
        raw = response["Payload"].read()
        answer = json.loads(raw) if raw else None
        if response.get("FunctionError"):
            copied = _redacted(answer, arguments)[:300]
            raise FlagsmithError(f"{tool}: rm-flagsmith-admin failed: {copied}")
        return _admin_result(tool, answer, arguments)


def is_ended(status: str) -> bool:
    """Ended in Flagsmith means completed (Gate 1, rule 2). paused is not ended."""
    return status == names.EXPERIMENT_STATUS_ENDED


def results_current(envelope: dict, refreshed_at: str) -> bool:
    """Whether a results read reflects the refresh this module asked for at refreshed_at.

    All three must hold (Gate 1, rule 6, from task 1.3's and 1.4's recorded reads):
    1. Flagsmith has that request or a later one: refresh_requested_at is no earlier than
       refreshed_at less REFRESH_CLOCK_ALLOWANCE_SECONDS;
    2. the computation that request started has finished: is_final, or as_of at or after
       refresh_requested_at. An ended experiment's refreshed results are final and as of
       its end, earlier than the request;
    3. that computation did not fail: last_error_at is null or before refresh_requested_at.
    """
    requested = _time(envelope.get("refresh_requested_at"))
    asked = _time(refreshed_at)
    if requested is None or asked is None:
        return False
    if requested < asked - timedelta(seconds=REFRESH_CLOCK_ALLOWANCE_SECONDS):
        return False
    failed = _time(envelope.get("last_error_at"))
    if failed is not None and failed >= requested:
        return False
    as_of = _time(envelope.get("as_of"))
    return bool(envelope.get("is_final")) or (as_of is not None and as_of >= requested)


def map_results(envelope: dict, metric_ids: dict[str, int]) -> dict:
    """Flagsmith's results onto contracts/schemas/results.json (design.md section 7).

    metric_ids is the run item's map, metric name to Flagsmith metric ID. A variation's
    identities is the sum of its new_identities over the exposures timeseries, never n; each
    mean is sum / n; the control arm, labelled control, is current; helpful_lift_ci_lower is
    helpful_rate's ci_low.
    """
    payload = envelope.get("payload") or {}
    identities = dict.fromkeys(names.VARIANTS, 0)
    for point in (payload.get("exposures_timeseries") or {}).get("points") or []:
        for label, count in (point.get("new_identities") or {}).items():
            if label in _VARIANT_BY_LABEL:
                identities[_VARIANT_BY_LABEL[label]] += int(count)
    by_id = {metric.get("metric_id"): metric for metric in payload.get("metrics") or []}
    variations = {
        variant: {"identities": identities[variant], **dict.fromkeys(names.METRICS)}
        for variant in names.VARIANTS
    }
    for name in names.METRICS:
        metric = by_id.get(metric_ids[name]) if name in metric_ids else None
        for label, stats in ((metric or {}).get("variants") or {}).items():
            n, total = stats.get("n"), stats.get("sum")
            if label in _VARIANT_BY_LABEL and n and total is not None:
                variations[_VARIANT_BY_LABEL[label]][name] = total / n
    primary = by_id.get(metric_ids.get(names.PRIMARY_METRIC)) or {}
    inference = (primary.get("inference") or {}).get(names.TREATMENT_OPTION_KEY) or {}
    ready = all(
        variation["identities"] >= names.MIN_IDENTITIES_PER_VARIATION
        and all(variation[name] is not None for name in names.METRICS)
        for variation in variations.values()
    )
    return {
        "ready": ready,
        "variations": variations,
        "helpful_lift_ci_lower": inference.get("ci_low"),
    }


class Flagsmith:
    """The tools of tasks.md 2.2 over one MCP connection and one admin transport."""

    def __init__(self, mcp: Mcp, admin: Any, clock: Callable[[], datetime] = _utc_now) -> None:
        self.mcp = mcp
        self.admin = admin
        self.clock = clock
        self._environment: dict | None = None

    # -- lookups ---------------------------------------------------------------
    def environment(self) -> dict:
        """The Development environment's ID and API key, read once through MCP."""
        if self._environment is None:
            listed = _results(self.mcp.call("list_environments", {"project": _PROJECT}))
            found = [e for e in listed if e.get("name") == names.FLAGSMITH_ENVIRONMENT_NAME]
            if len(found) != 1:
                raise FlagsmithError(
                    f"expected one environment named {names.FLAGSMITH_ENVIRONMENT_NAME} in "
                    f"project {_PROJECT}, found {len(found)}"
                )
            self._environment = {"id": found[0]["id"], "key": found[0]["api_key"]}
        return self._environment

    def _admin(self, tool: str, **arguments: Any) -> Any:
        return self.admin.call(
            tool, {"environment_api_key": self.environment()["key"], **arguments}
        )

    def _find_feature(self, name: str) -> dict | None:
        listed = self.mcp.call(
            "list_project_features",
            {"project_pk": _PROJECT, "search": name, "environment": self.environment()["id"]},
        )
        found = [feature for feature in _results(listed) if feature.get("name") == name]
        return found[0] if found else None

    # -- the permanent features ----------------------------------------------
    def ensure_permanent_features(self) -> dict[str, int]:
        """Get or create assistant_cheaper_enabled and assistant_default_model; their IDs.

        A found one is checked on type only (design.md section 4) and never changed. Every
        found one is checked before anything is created, so a mismatch stops with no write.
        """
        found = {name: self._find_feature(name) for name in names.PERMANENT_FEATURES}
        for name, feature in found.items():
            default = names.PERMANENT_FEATURE_DEFINITIONS[name]["default"]
            if feature is not None:
                self._check_permanent(name, feature, string_valued=not isinstance(default, bool))
        ids = {}
        for name, feature in found.items():
            if feature is None:
                default = names.PERMANENT_FEATURE_DEFINITIONS[name]["default"]
                arguments = {"project_pk": _PROJECT, "name": name, "type": API_FEATURE_STANDARD}
                if isinstance(default, bool):  # the kill switch: on or off, no value
                    arguments["default_enabled"] = default
                else:  # the default model: a string value, served while enabled
                    arguments.update(initial_value=default, default_enabled=True)
                feature = self.mcp.call("create_feature", arguments)
            ids[name] = feature["id"]
        return ids

    @staticmethod
    def _check_permanent(name: str, feature: dict, *, string_valued: bool) -> None:
        problems = []
        if feature.get("type") != API_FEATURE_STANDARD:
            problems.append(f"type {feature.get('type')!r} (want {API_FEATURE_STANDARD!r})")
        if string_valued:
            # The value may be either variant: rollback and promote change it.
            state = feature.get("environment_feature_state") or {}
            values = [feature.get("initial_value")]
            if state:
                values.append(state.get("feature_state_value"))
            for value in values:
                if not isinstance(value, str) or value not in names.DEFAULT_MODEL_VALUES:
                    problems.append(
                        f"value {value!r} (want one of {list(names.DEFAULT_MODEL_VALUES)})"
                    )
        if problems:
            raise FlagsmithError(
                f"feature {name} exists but differs: {'; '.join(problems)}. Fix it in the "
                "Flagsmith dashboard; nothing was changed"
            )

    # -- the run's experiment ----------------------------------------------------
    def ensure_experiment(self, run_id: str, plan: dict) -> dict:
        """Get or create the run's feature, its cheaper option, the metrics and the experiment.

        The experiment's create carries its whole rollout: 100 percent of identities, control
        value current, and the cheaper option at 50 percent. That block decides the split,
        whatever the option's own default allocation (Gate 1, rule 3).
        """
        feature_key = names.experiment_feature_key(run_id)
        environment = self.environment()
        feature = self._find_feature(feature_key)
        if feature is None:
            feature = self.mcp.call(
                "create_feature",
                {
                    "project_pk": _PROJECT,
                    "name": feature_key,
                    "type": API_FEATURE_MULTIVARIATE,
                    "initial_value": names.CONTROL_VALUE,
                    "default_enabled": True,
                },
            )
        elif feature.get("type") != API_FEATURE_MULTIVARIATE:
            raise FlagsmithError(
                f"feature {feature_key} exists as {feature.get('type')!r}, not "
                f"{API_FEATURE_MULTIVARIATE!r}; nothing was changed"
            )
        option = self._treatment_option(feature["id"])
        if option is None:
            option = self.mcp.call(
                "create_feature_multivariate_option",
                {
                    "project_pk": str(_PROJECT),  # the Gateway's schema types it as a string
                    "feature_pk": feature["id"],
                    "feature": feature["id"],
                    "type": API_OPTION_TYPE,
                    "string_value": names.TREATMENT_OPTION_VALUE,
                    "key": names.TREATMENT_OPTION_KEY,
                    "default_percentage_allocation": names.TREATMENT_PERCENT,
                },
            )
        metric_ids = self._admin("ensure_metrics")["metric_ids"]
        experiment = self._experiment_on(feature_key, feature["id"])
        if experiment is None:
            experiment = self.mcp.call(
                "create_experiment",
                {
                    "environment_api_key": environment["key"],
                    "feature": feature["id"],
                    "name": feature_key,
                    "hypothesis": plan["hypothesis"],
                    # Flagsmith has no primary flag, so the primary metric goes first.
                    "metrics": [
                        {
                            "metric": str(metric_ids[name]),
                            "expected_direction": names.EXPECTED_DIRECTION_API[
                                names.EXPERIMENT_EXPECTED_DIRECTIONS[name]
                            ],
                        }
                        for name in names.METRICS
                    ],
                    "experiment_rollout": {
                        "enabled": True,
                        "rollout_percentage": names.EXPERIMENT_ROLLOUT_PERCENT,
                        "feature_state_value": {
                            "type": API_ROLLOUT_VALUE_TYPE,
                            "value": names.CONTROL_VALUE,
                        },
                        "multivariate_feature_state_values": [
                            {
                                "multivariate_feature_option": option["id"],
                                "percentage_allocation": names.TREATMENT_PERCENT,
                            }
                        ],
                    },
                },
            )
        return {
            "feature_key": feature_key,
            "experiment_id": experiment["id"],
            "metric_ids": metric_ids,
        }

    def _treatment_option(self, feature_id: int) -> dict | None:
        listed = self.mcp.call(
            "list_feature_multivariate_options",
            {"project_pk": str(_PROJECT), "feature_pk": feature_id},
        )
        options = _results(listed)
        keyed = [o for o in options if o.get("key") == names.TREATMENT_OPTION_KEY]
        if keyed:
            if keyed[0].get("string_value") != names.TREATMENT_OPTION_VALUE:
                raise FlagsmithError(
                    f"option {names.TREATMENT_OPTION_KEY} on feature {feature_id} has value "
                    f"{keyed[0].get('string_value')!r}, not {names.TREATMENT_OPTION_VALUE!r}"
                )
            return keyed[0]
        if any(o.get("string_value") == names.TREATMENT_OPTION_VALUE for o in options):
            raise FlagsmithError(
                f"feature {feature_id} has a {names.TREATMENT_OPTION_VALUE} option without the "
                f"key {names.TREATMENT_OPTION_KEY}: it would record no exposures"
            )
        return None

    def _experiment_on(self, feature_key: str, feature_id: int) -> dict | None:
        listed = self.mcp.call(
            "list_experiments",
            {"environment_api_key": self.environment()["key"], "q": feature_key},
        )
        on_feature = [e for e in _results(listed) if _id(e.get("feature")) == feature_id]
        live = [e for e in on_feature if not is_ended(e.get("status"))]
        if live:
            return live[0]
        if on_feature:
            raise FlagsmithError(
                f"the experiment on {feature_key} has already ended and cannot restart; "
                "start a new run"
            )
        return None

    def start_experiment(self, experiment_id: int) -> dict:
        """Start it through rm-flagsmith-admin; a running one is left as it is."""
        return self._admin("start_experiment", experiment_id=experiment_id)

    def experiment_status(self, experiment_id: int) -> str:
        """The experiment's raw status: created, running, paused or completed."""
        return self._admin("get_experiment", experiment_id=experiment_id).get("status")

    # -- results -----------------------------------------------------------------
    def refresh_results(self, experiment_id: int, last_refresh_at: str | None) -> str | None:
        """Ask for a refresh, unless the last one was under RESULTS_REFRESH_MIN_SECONDS ago.

        Returns the time to store as the run's last_refresh_at, or None when no refresh was
        asked (inside the window, or Flagsmith's own limit answered 429).
        """
        now = self.clock()
        last = _time(last_refresh_at)
        if last is not None and now - last < timedelta(seconds=names.RESULTS_REFRESH_MIN_SECONDS):
            return None
        answer = self._admin("refresh_results", experiment_id=experiment_id)
        return names.utc_iso(now) if answer.get("refreshed") else None

    def get_results(
        self, experiment_id: int, metric_ids: dict[str, int], refreshed_at: str | None = None
    ) -> dict:
        """The results.json shape. With refreshed_at, ResultsNotCurrent until the read
        reflects that refresh (results_current)."""
        envelope = self._admin("get_results", experiment_id=experiment_id)
        if refreshed_at is not None and not results_current(envelope, refreshed_at):
            raise ResultsNotCurrent(
                f"experiment {experiment_id}'s results are as of {envelope.get('as_of')}, "
                f"refresh requested {envelope.get('refresh_requested_at')}; asked {refreshed_at}"
            )
        return map_results(envelope, metric_ids)

    # -- feature state, through a v2 version ---------------------------------------
    def set_kill_switch(self, enabled: bool) -> dict:
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled must be a bool, not {enabled!r}")
        return self.set_feature_state(names.KILL_SWITCH_FEATURE, enabled=enabled)

    def set_default_model(self, value: str) -> dict:
        if value not in names.DEFAULT_MODEL_VALUES:
            raise ValueError(
                f"the default model is one of {names.DEFAULT_MODEL_VALUES}, not {value!r}"
            )
        return self.set_feature_state(names.DEFAULT_MODEL_FEATURE, value=value)

    def set_feature_state(
        self, name: str, *, enabled: bool | None = None, value: str | None = None
    ) -> dict:
        """Set a standard feature's environment state: create a version, update its default
        state, publish it. Nothing is written when the live state already holds the target.
        """
        environment = self.environment()
        feature = self._find_feature(name)
        if feature is None:
            raise FlagsmithError(
                f"feature {name} does not exist; ensure_permanent_features makes it"
            )
        if feature.get("type") != API_FEATURE_STANDARD:
            raise FlagsmithError(f"feature {name} is {feature.get('type')!r}, not standard")
        live = feature.get("environment_feature_state") or {}
        if (
            live
            and (enabled is None or live.get("enabled") == enabled)
            and (value is None or live.get("feature_state_value") == value)
        ):
            return {"changed": False}
        base = {"environment_pk": environment["id"], "feature_pk": feature["id"]}
        version = self.mcp.call("create_environment_feature_version", base)
        version_id = version.get("uuid") if isinstance(version, dict) else None
        listed = self.mcp.call(
            "get_environment_feature_version_states",
            {**base, "environment_feature_version_pk": version_id},
        )
        defaults = [
            state
            for state in _results(listed)
            if not state.get("feature_segment") and not state.get("identity")
        ]
        if len(defaults) != 1:
            raise FlagsmithError(
                f"version {version_id} of {name} lists {len(defaults)} environment default "
                "states, not one; nothing was published"
            )
        state = defaults[0]
        self.mcp.call(
            "update_environment_feature_version_state",
            {
                **base,
                "environment_feature_version_pk": version_id,
                API_PATH_ID: state["id"],
                "feature": feature["id"],
                "enabled": state.get("enabled") if enabled is None else enabled,
                "feature_state_value": (
                    {"type": API_VERSION_VALUE_TYPE, "string_value": value}
                    if value is not None
                    else state.get("feature_state_value") or {}
                ),
            },
        )
        self.mcp.call("publish_environment_feature_version", {**base, "id": version_id})
        return {"changed": True, "version": version_id}
