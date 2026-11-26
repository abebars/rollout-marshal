"""Unit tests for agent/spike/agent.py (task 1.3): no network, no AWS, no Flagsmith.

A fake Gateway MCP client stands in for rm-gateway. Its tool schemas follow the
Flagsmith MCP server's OpenAPI (github.com/Flagsmith/flagsmith, mcp/src/flagsmith_mcp/
openapi.json); phase 2 records the real ones from tools/list.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from agent.spike import agent as spike
from contracts import names

ENV_KEY = "EnvKeyThatMustNeverLeave123"
ENV_ID = 70001
PROJECT = names.FLAGSMITH_PROJECT_ID
KEY = "assistant_model_rspike2215"
FEATURE_ID = 900
OPTION_ID = 901
METRIC_ID = 902
EXPERIMENT_ID = 903
SWITCH_ID = 904
FEATURE_STATE_ID = 905
SWITCH_STATE_ID = 906

SAMPLING_KEYS = {"temperature", "top_p", "topP", "top_k", "topK"}


def _schema(required, optional=(), types=None):
    types = types or {}
    props = {name: {"type": types.get(name, "string")} for name in (*required, *optional)}
    return {"type": "object", "properties": props, "required": list(required)}


SCHEMAS = {
    "list_environments": _schema([], ["project", "page"], {"project": "integer"}),
    "list_project_features": _schema(
        ["project_pk"],
        ["search", "environment", "page"],
        {"project_pk": "integer", "environment": "integer"},
    ),
    "create_feature": _schema(
        ["project_pk", "name"],
        ["type", "initial_value", "default_enabled", "description"],
        {"project_pk": "integer", "default_enabled": "boolean"},
    ),
    "list_feature_multivariate_options": _schema(
        ["project_pk", "feature_pk"], [], {"feature_pk": "integer"}
    ),
    "create_feature_multivariate_option": _schema(
        ["project_pk", "feature_pk", "feature"],
        ["type", "string_value", "key", "default_percentage_allocation"],
        {"feature_pk": "integer", "feature": "integer", "default_percentage_allocation": "number"},
    ),
    # As the live Gateway lists it: the path id is renamed id__path because the body
    # also has an id.
    "update_feature_multivariate_option": _schema(
        ["project_pk", "feature_pk", "id__path", "feature"],
        ["id", "type", "string_value", "key", "default_percentage_allocation"],
        {
            "feature_pk": "integer",
            "id__path": "integer",
            "id": "integer",
            "feature": "integer",
            "default_percentage_allocation": "number",
        },
    ),
    "list_metrics": _schema(["environment_api_key"], ["q", "page"]),
    # As the live Gateway lists it: definition is required but has no property.
    "create_metric": {
        "type": "object",
        "properties": {
            "environment_api_key": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "aggregation": {"type": "string"},
            "direction": {"type": "string"},
        },
        "required": ["environment_api_key", "definition", "name"],
    },
    "list_experiments": _schema(["environment_api_key"], ["q", "status", "page"]),
    "create_experiment": _schema(
        ["environment_api_key", "feature", "name", "hypothesis"],
        ["metrics", "experiment_rollout"],
        {"feature": "integer", "metrics": "array", "experiment_rollout": "object"},
    ),
    "get_experiment": _schema(
        ["environment_api_key", "experiment_id"], [], {"experiment_id": "integer"}
    ),
    "get_experiment_exposures": _schema(
        ["environment_api_key", "experiment_id"], [], {"experiment_id": "integer"}
    ),
    "get_experiment_results": _schema(
        ["environment_api_key", "experiment_id"], [], {"experiment_id": "integer"}
    ),
    "update_environment_feature_state": _schema(
        ["environment_api_key", "id__path", "feature"],
        ["id", "enabled", "feature_state_value"],
        {"id__path": "integer", "id": "integer", "feature": "integer", "enabled": "boolean"},
    ),
    "update_feature": _schema(
        ["id__path", "project_pk"],
        ["id", "name", "description"],
        {"project_pk": "integer", "id__path": "integer", "id": "integer"},
    ),
}


class _Page(list):
    """Like strands' PaginatedList: a list with a pagination_token."""

    def __init__(self, items, token):
        super().__init__(items)
        self.pagination_token = token


class FakeGateway:
    """Duck-types the parts of strands' MCPClient that the spike uses."""

    def __init__(
        self, existing=(), errors=None, page_size=7, metric_aggregation="mean", drops_key=False
    ):
        self.existing = set(existing)
        self.errors = errors or {}
        self.page_size = page_size
        self.metric_aggregation = metric_aggregation
        self.drops_key = drops_key  # an MCP server that does not store the option's key
        self.option_created = False
        self.calls = []
        self.listed = 0

    def list_tools_sync(self, pagination_token=None):
        self.listed += 1
        everything = sorted(SCHEMAS)
        start = int(pagination_token or 0)
        tools = [
            SimpleNamespace(
                mcp_tool=SimpleNamespace(
                    name=spike.GATEWAY_TOOL_PREFIX + name,
                    description=f"fake {name}",
                    input_schema=SCHEMAS[name],
                    annotations=None,
                )
            )
            for name in everything[start : start + self.page_size]
        ]
        nxt = start + self.page_size
        return _Page(tools, str(nxt) if nxt < len(everything) else None)

    def call_tool_sync(self, tool_use_id, name, arguments=None):
        assert name.startswith(spike.GATEWAY_TOOL_PREFIX), name
        bare = name[len(spike.GATEWAY_TOOL_PREFIX) :]
        self.calls.append((bare, dict(arguments or {})))
        if bare in self.errors:
            return {
                "status": "error",
                "toolUseId": tool_use_id,
                "content": [{"text": self.errors[bare]}],
            }
        body = self._respond(bare, arguments or {})
        return {
            "status": "success",
            "toolUseId": tool_use_id,
            "content": [{"text": json.dumps(body)}],
        }

    def _page(self, results):
        return {"count": len(results), "next": None, "previous": None, "results": results}

    def _respond(self, tool, args):
        if tool == "list_environments":
            return self._page(
                [
                    {"id": 1, "name": "Production", "api_key": "OtherEnvKey"},
                    {
                        "id": ENV_ID,
                        "name": names.FLAGSMITH_ENVIRONMENT_NAME,
                        "api_key": ENV_KEY,
                        "use_v2_feature_versioning": False,
                    },
                ]
            )
        if tool == "list_project_features":
            found = []
            if "feature" in self.existing and args.get("search") == KEY:
                found.append(
                    {
                        "id": FEATURE_ID,
                        "name": KEY,
                        "type": "MULTIVARIATE",
                        "environment_feature_state": {
                            "id": FEATURE_STATE_ID,
                            "enabled": True,
                            "feature_state_value": names.CONTROL_VALUE,
                        },
                    }
                )
            if "switch" in self.existing and args.get("search") == spike.SPIKE_KILL_SWITCH:
                found.append(
                    {
                        "id": SWITCH_ID,
                        "name": spike.SPIKE_KILL_SWITCH,
                        "type": "STANDARD",
                        "environment_feature_state": {"id": SWITCH_STATE_ID, "enabled": True},
                    }
                )
            return self._page(found)
        if tool == "create_feature":
            fid = FEATURE_ID if args["name"] == KEY else SWITCH_ID
            return {"id": fid, "name": args["name"], "type": args.get("type")}
        if tool == "list_feature_multivariate_options":
            if self.option_created:
                return self._page(
                    [
                        {
                            "id": OPTION_ID,
                            "key": None if self.drops_key else names.TREATMENT_OPTION_KEY,
                            "type": "unicode",
                            "string_value": names.TREATMENT_OPTION_VALUE,
                            "default_percentage_allocation": names.TREATMENT_PERCENT,
                        }
                    ]
                )
            if "option" in self.existing:
                return self._page(
                    [
                        {
                            "id": OPTION_ID,
                            "key": names.TREATMENT_OPTION_KEY,
                            "type": "unicode",
                            "string_value": names.TREATMENT_OPTION_VALUE,
                            "default_percentage_allocation": names.TREATMENT_PERCENT,
                        }
                    ]
                )
            return self._page([])
        if tool == "create_feature_multivariate_option":
            self.option_created = True
            stored = {k: v for k, v in args.items() if k != "project_pk"}
            if self.drops_key:
                stored["key"] = None
            return {"id": OPTION_ID, **stored}
        if tool == "update_feature_multivariate_option":
            return {"id": OPTION_ID, **{k: v for k, v in args.items() if k != "project_pk"}}
        if tool == "list_metrics":
            if "metric" in self.existing:
                return self._page(
                    [
                        {
                            "id": METRIC_ID,
                            "name": spike.SPIKE_METRIC,
                            "aggregation": self.metric_aggregation,
                            "direction": "up",
                            "definition": {"version": 1, "event": spike.SPIKE_EVENT},
                        }
                    ]
                )
            return self._page([])
        if tool == "create_metric":
            return {
                "id": METRIC_ID,
                **{k: v for k, v in args.items() if k != "environment_api_key"},
            }
        if tool == "list_experiments":
            if "experiment" in self.existing:
                return self._page(
                    [{"id": EXPERIMENT_ID, "name": KEY, "feature": FEATURE_ID, "status": "created"}]
                )
            return self._page([])
        if tool == "create_experiment":
            return {"id": EXPERIMENT_ID, "name": args["name"], "status": "created"}
        if tool in ("get_experiment", "get_experiment_exposures", "get_experiment_results"):
            return {"id": args["experiment_id"], "status": "running", "environment": ENV_KEY}
        if tool in ("update_environment_feature_state", "update_feature"):
            return {"ok": True}
        raise AssertionError(f"unexpected tool {tool}")


def _names(calls):
    return [tool for tool, _ in calls]


def _writes(calls):
    return [(t, a) for t, a in calls if not t.startswith(("get_", "list_"))]


# ---------------------------------------------------------------- the model


def _formatted_request():
    model = spike.build_model()
    return model.format_request(messages=[{"role": "user", "content": [{"text": "hello"}]}])


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_model_request_sends_no_sampling_parameters():
    request = _formatted_request()
    assert SAMPLING_KEYS.isdisjoint(set(_keys(request)))
    assert set(names.FORBIDDEN_SAMPLING_PARAMETERS).isdisjoint(set(_keys(request)))


def test_model_request_uses_the_agent_model_with_an_explicit_token_cap():
    request = _formatted_request()
    assert request["modelId"] == names.MODEL_AGENT
    assert request["inferenceConfig"] == {"maxTokens": names.AGENT_MAX_TOKENS}


def test_model_request_asks_for_effort_low_at_the_contract_field():
    request = _formatted_request()
    outer, inner = names.THINKING_EFFORT_FIELD.split(".")
    assert request["additionalModelRequestFields"] == {outer: {inner: "low"}}


class _FakeAgent:
    def __init__(self, stop_reason):
        self.stop_reason = stop_reason
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            message={"role": "assistant", "content": [{"text": "Hello from rm_agent."}]},
            metrics=SimpleNamespace(
                accumulated_usage={"inputTokens": 12, "outputTokens": 9, "totalTokens": 21}
            ),
        )


def test_hello_returns_text_stop_reason_and_usage_from_one_call():
    fake = _FakeAgent("end_turn")
    result = spike.hello(agent=fake)
    assert fake.prompts == [spike.HELLO_PROMPT]
    assert result["text"] == "Hello from rm_agent."
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["outputTokens"] == 9
    assert result["model_id"] == names.MODEL_AGENT


def test_hello_treats_max_tokens_as_an_error():
    with pytest.raises(spike.SpikeError, match=names.STOP_REASON_MAX_TOKENS):
        spike.hello(agent=_FakeAgent(names.STOP_REASON_MAX_TOKENS))


# ---------------------------------------------------------------- names and guards


def test_gateway_tool_names_are_target_name_three_underscores_tool():
    assert spike.GATEWAY_TOOL_PREFIX == names.GATEWAY_TARGET_FLAGSMITH + "___"


def test_spike_names_follow_the_spike_prefix_rule():
    for name in (spike.SPIKE_METRIC, spike.SPIKE_EVENT, spike.SPIKE_KILL_SWITCH):
        assert name.startswith(names.SPIKE_NAME_PREFIX)
    assert spike.SPIKE_METRIC == "spike_mcp_metric"
    assert spike.SPIKE_EVENT == "spike_event"
    assert spike.SPIKE_KILL_SWITCH == "spike_kill_switch"


def test_feature_key_for_now_is_built_from_contracts():
    import datetime as dt

    now = dt.datetime(2026, 11, 26, 22, 15, tzinfo=dt.UTC)
    assert spike.feature_key_for(now) == names.experiment_feature_key(
        names.scratch_run_id("spike", now)
    )
    assert spike.feature_key_for(now) == KEY


@pytest.mark.parametrize(
    "key",
    [
        names.KILL_SWITCH_FEATURE,
        names.DEFAULT_MODEL_FEATURE,
        "spike_flag",
        "assistant_model_r202611271530",
        "assistant_model_rspike",
        "assistant_model_rspike22150",
        "assistant_model_rsmoke2215",
        "",
        None,
    ],
)
def test_setup_and_probe_refuse_anything_but_a_spike_experiment_key(key):
    for action in ("setup", "probe"):
        fake = FakeGateway()
        reply = spike.handle(
            {"action": action, "feature_key": key}, tools=spike.FlagsmithTools(fake)
        )
        assert reply["ok"] is False
        assert reply["error"]["code"] == names.ERROR_BAD_REQUEST
        assert fake.calls == []


def test_unknown_action_is_a_bad_request():
    reply = spike.handle({"action": "end_experiment"}, tools=spike.FlagsmithTools(FakeGateway()))
    assert reply["ok"] is False
    assert reply["error"]["code"] == names.ERROR_BAD_REQUEST


# ---------------------------------------------------------------- setup


def test_setup_creates_feature_option_metric_and_experiment_by_tool_name():
    fake = FakeGateway()
    reply = spike.handle({"action": "setup", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is True, reply
    writes = _writes(fake.calls)
    assert _names(writes) == [
        "create_feature",
        "create_feature_multivariate_option",
        "create_metric",
        "create_experiment",
        "create_feature",
    ]
    feature, option, metric, experiment, switch = (args for _, args in writes)
    assert feature == {
        "project_pk": PROJECT,
        "name": KEY,
        "type": "MULTIVARIATE",
        "initial_value": names.CONTROL_VALUE,
        "default_enabled": True,
    }
    assert option == {
        "project_pk": str(PROJECT),
        "feature_pk": FEATURE_ID,
        "feature": FEATURE_ID,
        "type": "unicode",
        "string_value": names.TREATMENT_OPTION_VALUE,
        "key": names.TREATMENT_OPTION_KEY,
        "default_percentage_allocation": names.TREATMENT_PERCENT,
    }
    assert metric == {
        "environment_api_key": ENV_KEY,
        "name": spike.SPIKE_METRIC,
        "description": spike.METRIC_DESCRIPTION,
        "aggregation": names.METRIC_AGGREGATION,
        "direction": "up",
        "definition": {"version": 1, "event": spike.SPIKE_EVENT},
    }
    assert experiment == {
        "environment_api_key": ENV_KEY,
        "feature": FEATURE_ID,
        "name": KEY,
        "hypothesis": spike.HYPOTHESIS,
        "metrics": [{"metric": str(METRIC_ID), "expected_direction": "not_decrease"}],
        "experiment_rollout": {
            "enabled": True,
            "rollout_percentage": names.EXPERIMENT_ROLLOUT_PERCENT,
            "feature_state_value": {"type": "string", "value": names.CONTROL_VALUE},
            "multivariate_feature_state_values": [
                {
                    "multivariate_feature_option": OPTION_ID,
                    "percentage_allocation": names.TREATMENT_PERCENT,
                }
            ],
        },
    }
    assert switch == {
        "project_pk": PROJECT,
        "name": spike.SPIKE_KILL_SWITCH,
        "type": "STANDARD",
        "default_enabled": names.KILL_SWITCH_DEFAULT_ENABLED,
    }
    assert reply["result"]["experiment_id"] == EXPERIMENT_ID
    assert reply["result"]["option_key_set_by_mcp"] is True


def test_setup_reads_the_option_back_to_record_whether_mcp_kept_its_key():
    fake = FakeGateway(drops_key=True)
    reply = spike.handle({"action": "setup", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is True, reply
    assert reply["result"]["option_key_set_by_mcp"] is False
    reads = _names(fake.calls)
    created_at = reads.index("create_feature_multivariate_option")
    assert "list_feature_multivariate_options" in reads[created_at + 1 :]


def test_setup_lists_every_page_of_tools_before_writing():
    fake = FakeGateway(page_size=3)
    reply = spike.handle({"action": "setup", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is True
    assert fake.listed == -(-len(SCHEMAS) // 3)


def test_setup_run_twice_creates_nothing_the_second_time():
    fake = FakeGateway(existing={"feature", "option", "metric", "experiment", "switch"})
    reply = spike.handle({"action": "setup", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is True, reply
    assert _writes(fake.calls) == []
    assert reply["result"]["created"] == []


def test_found_metric_that_differs_stops_setup_before_the_experiment():
    fake = FakeGateway(existing={"metric"}, metric_aggregation="sum")
    reply = spike.handle({"action": "setup", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is False
    assert "aggregation" in reply["error"]["message"]
    assert "create_experiment" not in _names(fake.calls)


def test_setup_with_rollout_omitted_sends_no_rollout_and_records_the_reply():
    fake = FakeGateway(errors={"create_experiment": 'HTTP 400: {"experiment_rollout": "required"}'})
    reply = spike.handle(
        {"action": "setup", "feature_key": KEY, "omit_rollout": True},
        tools=spike.FlagsmithTools(fake),
    )
    created = [args for tool, args in fake.calls if tool == "create_experiment"]
    assert len(created) == 1 and "experiment_rollout" not in created[0]
    assert reply["ok"] is True
    assert reply["result"]["experiment_without_rollout"]["status"] == "error"
    assert "required" in json.dumps(reply["result"]["experiment_without_rollout"])


def test_a_tool_error_stops_setup_and_names_the_tool():
    fake = FakeGateway(errors={"create_metric": "HTTP 400: bad definition"})
    reply = spike.handle({"action": "setup", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is False
    assert reply["error"]["code"] == names.ERROR_EFFECT_FAILED
    assert "create_metric" in reply["error"]["message"]
    assert "create_experiment" not in _names(fake.calls)


def test_arguments_that_do_not_fit_the_listed_schema_are_never_sent():
    tools = spike.FlagsmithTools(FakeGateway())
    tools.list_tools()
    with pytest.raises(spike.SpikeError, match="not in"):
        tools.call("create_feature", {"project_pk": PROJECT, "name": KEY, "colour": "red"})
    with pytest.raises(spike.SpikeError, match="missing"):
        tools.call("create_feature", {"project_pk": PROJECT})
    with pytest.raises(spike.SpikeError, match="not on the Gateway"):
        tools.call("delete_feature_multivariate_option", {"project_pk": PROJECT})
    assert tools.client.calls == []


# ---------------------------------------------------------------- probe and read


def test_probe_touches_only_the_spike_feature_and_spike_kill_switch_and_reverts():
    fake = FakeGateway(existing={"feature", "option", "switch"})
    reply = spike.handle({"action": "probe", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is True, reply
    writes = _writes(fake.calls)
    targets = set()
    for tool, args in writes:
        # The path id goes in id__path, as the live Gateway names it.
        if tool == "update_environment_feature_state":
            assert args["id__path"] in (FEATURE_STATE_ID, SWITCH_STATE_ID)
            targets.add(args["feature"])
        elif tool == "update_feature_multivariate_option":
            assert (args["feature"], args["id__path"]) == (FEATURE_ID, OPTION_ID)
            targets.add(args["feature"])
        elif tool == "update_feature":
            assert (args["id__path"], args["name"]) == (FEATURE_ID, KEY)
            targets.add(args["id__path"])
        else:
            raise AssertionError(f"probe wrote with {tool}")
    assert targets == {FEATURE_ID, SWITCH_ID}
    switch_writes = [
        a
        for t, a in writes
        if t == "update_environment_feature_state" and a["feature"] == SWITCH_ID
    ]
    assert [a["enabled"] for a in switch_writes] == [False, True]
    allocations = [
        a["default_percentage_allocation"]
        for t, a in writes
        if t == "update_feature_multivariate_option"
    ]
    assert allocations[-1] == names.TREATMENT_PERCENT  # put back after the attempt


def test_probe_records_a_refusal_instead_of_failing():
    fake = FakeGateway(
        existing={"feature", "option", "switch"},
        errors={"update_feature_multivariate_option": "HTTP 400: feature is locked"},
    )
    reply = spike.handle({"action": "probe", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    assert reply["ok"] is True
    attempts = reply["result"]["attempts"]
    refused = [a for a in attempts if a["tool"].endswith("update_feature_multivariate_option")]
    assert refused and refused[0]["status"] == "error"
    assert "locked" in json.dumps(refused[0])


def test_probe_sends_the_control_value_unchanged_with_each_state_write():
    fake = FakeGateway(existing={"feature", "option", "switch"})
    spike.handle({"action": "probe", "feature_key": KEY}, tools=spike.FlagsmithTools(fake))
    feature_states = [
        args
        for tool, args in fake.calls
        if tool == "update_environment_feature_state" and args["feature"] == FEATURE_ID
    ]
    assert feature_states
    assert all(a["feature_state_value"] == names.CONTROL_VALUE for a in feature_states)


def test_probe_records_arguments_that_do_not_fit_and_goes_on():
    fake = FakeGateway(existing={"feature", "option", "switch"})
    tools = spike.FlagsmithTools(fake)
    tools.list_tools()
    del tools.schemas[spike.GATEWAY_TOOL_PREFIX + "update_feature"]  # as if the Gateway lacked it
    reply = spike.handle({"action": "probe", "feature_key": KEY}, tools=tools)
    assert reply["ok"] is True
    skipped = [a for a in reply["result"]["attempts"] if a["status"] == "not_sent"]
    assert [a["tool"] for a in skipped] == [spike.GATEWAY_TOOL_PREFIX + "update_feature"]
    switch_writes = [
        a
        for t, a in fake.calls
        if t == "update_environment_feature_state" and a["feature"] == SWITCH_ID
    ]
    assert len(switch_writes) == 2  # the kill switch toggle still ran


@pytest.mark.parametrize(
    "tool", ["create_feature", "update_feature", "delete_feature_segment", "start", "x"]
)
def test_read_refuses_any_tool_that_is_not_get_or_list(tool):
    fake = FakeGateway()
    reply = spike.handle(
        {"action": "read", "tool": tool, "arguments": {}}, tools=spike.FlagsmithTools(fake)
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == names.ERROR_BAD_REQUEST
    assert fake.calls == []


def test_read_fills_in_the_development_key_and_hides_it():
    fake = FakeGateway()
    reply = spike.handle(
        {"action": "read", "tool": "get_experiment", "arguments": {"experiment_id": EXPERIMENT_ID}},
        tools=spike.FlagsmithTools(fake),
    )
    assert reply["ok"] is True, reply
    sent = dict(fake.calls)["get_experiment"]
    assert sent["environment_api_key"] == ENV_KEY
    assert ENV_KEY not in json.dumps(reply)


def test_no_reply_ever_contains_the_environment_key():
    for payload, existing in [
        ({"action": "setup", "feature_key": KEY}, ()),
        ({"action": "probe", "feature_key": KEY}, {"feature", "option", "switch"}),
        ({"action": "tools"}, ()),
    ]:
        fake = FakeGateway(existing=existing)
        reply = spike.handle(payload, tools=spike.FlagsmithTools(fake))
        text = json.dumps(reply)
        assert ENV_KEY not in text
        assert "OtherEnvKey" not in text


def test_tools_lists_names_and_input_schemas():
    reply = spike.handle({"action": "tools"}, tools=spike.FlagsmithTools(FakeGateway()))
    assert reply["ok"] is True
    listed = {tool["name"]: tool for tool in reply["result"]["tools"]}
    assert set(listed) == {spike.GATEWAY_TOOL_PREFIX + name for name in SCHEMAS}
    assert (
        listed[spike.GATEWAY_TOOL_PREFIX + "create_metric"]["inputSchema"]
        == SCHEMAS["create_metric"]
    )


def test_the_module_never_names_an_end_experiment_tool_or_endpoint():
    source = open(spike.__file__).read()
    assert not re.search(r"(complete|end|stop|finish|conclude)_experiment", source)
    assert "/complete/" not in source and "/pause/" not in source
