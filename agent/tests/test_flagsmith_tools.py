"""Tests for task 2.2: agent/runtime/rm_agent/tools/flagsmith.py and rm-flagsmith-admin
(agent/flagsmith_admin/handler.py).

The unit tests use fakes only, no AWS and no Flagsmith: a fake MCP client holding a small
Flagsmith, which checks every call's arguments against the tool's input schema as the
Gateway listed it in task 1.3 (evidence/1.3-mcp-tools.json); a fake admin transport over the
same Flagsmith; and a fake HTTP sender for the Lambda. Recorded calls and results come from
the evidence files, so a run ID or an ID is read from them, never retyped.

The live tests at the end (pytest -m live, after the lead applies infra/modules/agent) call
Flagsmith and rm-flagsmith-admin with the human's AWS credentials.
"""

from __future__ import annotations

import copy
import io
import json
import re
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from rm_agent.tools import flagsmith

import contracts
from agent.flagsmith_admin import handler
from contracts import names

ROOT = Path(__file__).resolve().parents[2]
SPIKE_EVIDENCE = (ROOT / "evidence/1.3-flagsmith-agent-spike.md").read_text()
EXPERIMENT_EVIDENCE = (ROOT / "evidence/1.4-spike-experiment.md").read_text()
GATEWAY_TOOLS = {
    tool["name"]: tool
    for tool in json.loads((ROOT / "evidence/1.3-mcp-tools.json").read_text())["tools"]
}
RESULTS_SAMPLE = json.loads((ROOT / "evidence/1.4-results-sample.json").read_text())["results"]
RESULTS_36 = ROOT / "evidence/3.6-results.json"

# The calls task 1.3 recorded with their full arguments (its step 6 tables).
_ROW = re.compile(r"^\| `(\w+)`(?: \([^)]*\))? \| `(\{.*?\})` \|", re.M)
RECORDED = [(tool, json.loads(arguments)) for tool, arguments in _ROW.findall(SPIKE_EVIDENCE)]


def recorded(tool: str, **match) -> dict:
    for name, arguments in RECORDED:
        if name == tool and all(arguments.get(k) == v for k, v in match.items()):
            return copy.deepcopy(arguments)
    raise AssertionError(f"evidence/1.3 records no {tool} call matching {match}")


ENV_ID = recorded("list_project_features")["environment"]
ENV_KEY = "test-environment-key"  # a stand-in for the redacted key, never a real one
ORG_KEY = "test-organisation-key-0123456789"
START = datetime(2026, 11, 27, 15, 30, 5, tzinfo=UTC)
PLAN = {"hypothesis": "Sonnet 5 answers as helpfully as Fable 5.1, for less."}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# A small Flagsmith, behind a fake MCP client and a fake admin transport
# ---------------------------------------------------------------------------
class World:
    def __init__(self, *, feature_id: int = 306314, option_id: int = 37546) -> None:
        self.environments = [
            {
                "id": ENV_ID,
                "name": names.FLAGSMITH_ENVIRONMENT_NAME,
                "api_key": ENV_KEY,
                "use_v2_feature_versioning": True,
            },
            {
                "id": ENV_ID + 1,
                "name": "Production",
                "api_key": "test-production-key",
                "use_v2_feature_versioning": True,
            },
        ]
        self.features: dict[str, dict] = {}
        self.options: dict[int, list[dict]] = {}
        self.experiments: list[dict] = []
        self.versions: dict[str, dict] = {}
        self.metrics: dict[str, dict] = {}
        self._next = {
            "feature": feature_id,
            "option": option_id,
            "experiment": 900,
            "state": 1883398,
            "metric": 1000,
        }

    def next_id(self, kind: str) -> int:
        self._next[kind] += 1
        return self._next[kind] - 1

    def add_feature(self, name: str, kind: str, value=None, enabled: bool = True) -> dict:
        feature = {
            "id": self.next_id("feature"),
            "name": name,
            "type": kind,
            "initial_value": value,
            "default_enabled": enabled,
            "environment_feature_state": {
                "id": self.next_id("state"),
                "enabled": enabled,
                "feature_state_value": value,
                "feature_segment": None,
                "identity": None,
            },
        }
        self.features[name] = feature
        return feature

    def add_option(self, feature_id: int, key, value, allocation) -> dict:
        option = {
            "id": self.next_id("option"),
            "feature": feature_id,
            "type": flagsmith.API_OPTION_TYPE,
            "key": key,
            "string_value": value,
            "default_percentage_allocation": allocation,
        }
        self.options.setdefault(feature_id, []).append(option)
        return option

    def experiment(self, experiment_id: int) -> dict:
        return next(e for e in self.experiments if e["id"] == experiment_id)


def _typed(value) -> dict:
    return {"type": flagsmith.API_VERSION_VALUE_TYPE, "string_value": value}


class FakeMcp:
    """call_tool_sync as Strands' MCPClient answers it, over World."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, dict]] = []

    def call_tool_sync(self, tool_use_id: str, name: str, arguments: dict) -> dict:
        assert name in GATEWAY_TOOLS, f"{name} is not on the Gateway's tool list"
        Draft202012Validator(GATEWAY_TOOLS[name]["inputSchema"]).validate(arguments)
        tool = name.removeprefix(flagsmith.MCP_TOOL_PREFIX)
        self.calls.append((tool, copy.deepcopy(arguments)))
        result = getattr(self, f"_{tool}")(arguments)
        return {
            "status": "success",
            "toolUseId": tool_use_id,
            "content": [{"text": json.dumps(result)}],
        }

    def tools_called(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def first(self, tool: str) -> dict:
        return next(arguments for name, arguments in self.calls if name == tool)

    def _list_environments(self, a):
        return {"count": len(self.world.environments), "results": self.world.environments}

    def _list_project_features(self, a):
        found = [f for n, f in self.world.features.items() if a.get("search", "") in n]
        return {"count": len(found), "results": copy.deepcopy(found)}

    def _create_feature(self, a):
        return self.world.add_feature(
            a["name"], a["type"], a.get("initial_value"), a.get("default_enabled", False)
        )

    def _list_feature_multivariate_options(self, a):
        return {"results": self.world.options.get(a["feature_pk"], [])}

    def _create_feature_multivariate_option(self, a):
        return self.world.add_option(
            a["feature_pk"], a.get("key"), a.get("string_value"), a["default_percentage_allocation"]
        )

    def _list_experiments(self, a):
        found = [e for e in self.world.experiments if a.get("q", "") in e["name"]]
        return {"count": len(found), "results": copy.deepcopy(found)}

    def _create_experiment(self, a):
        experiment = {
            "id": self.world.next_id("experiment"),
            "feature": a["feature"],
            "name": a["name"],
            "hypothesis": a["hypothesis"],
            "status": flagsmith.STATUS_CREATED,
            "metrics": a.get("metrics", []),
            "experiment_rollout": a["experiment_rollout"],
            "started_at": None,
            "ended_at": None,
        }
        self.world.experiments.append(experiment)
        return experiment

    def _feature_by_id(self, feature_id):
        return next(f for f in self.world.features.values() if f["id"] == feature_id)

    def _create_environment_feature_version(self, a):
        live = self._feature_by_id(a["feature_pk"])["environment_feature_state"]
        version = str(uuid.uuid4())
        self.world.versions[version] = {
            "feature": a["feature_pk"],
            "published": False,
            "states": [
                {
                    "id": self.world.next_id("state"),
                    "feature": a["feature_pk"],
                    "enabled": live["enabled"],
                    "feature_state_value": _typed(live["feature_state_value"]),
                    "feature_segment": None,
                    "identity": None,
                }
            ],
        }
        return {"uuid": version, "is_live": False, "published": False}

    def _get_environment_feature_version_states(self, a):
        return self.world.versions[a["environment_feature_version_pk"]]["states"]

    def _update_environment_feature_version_state(self, a):
        version = self.world.versions[a["environment_feature_version_pk"]]
        state = next(s for s in version["states"] if s["id"] == a["id__path"])
        state.update(enabled=a["enabled"], feature_state_value=a["feature_state_value"])
        return state

    def _publish_environment_feature_version(self, a):
        version = self.world.versions[a["id"]]
        version["published"] = True
        state = version["states"][0]
        live = self._feature_by_id(a["feature_pk"])["environment_feature_state"]
        live.update(
            enabled=state["enabled"],
            feature_state_value=(state["feature_state_value"] or {}).get("string_value"),
        )
        return {"uuid": a["id"], "live_from": "2026-11-27T15:30:06Z"}


class FakeAdmin:
    """rm-flagsmith-admin's tools over World, as GatewayAdmin and LambdaAdmin answer them."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, dict]] = []
        self.refresh_accepted = True
        self.results = copy.deepcopy(RESULTS_SAMPLE)

    def call(self, tool: str, arguments: dict):
        assert tool in flagsmith.ADMIN_TOOLS
        assert arguments["environment_api_key"] == ENV_KEY
        self.calls.append((tool, dict(arguments)))
        if tool == "ensure_metrics":
            created = []
            for name in names.METRICS:
                if name not in self.world.metrics:
                    self.world.metrics[name] = {"id": self.world.next_id("metric"), "name": name}
                    created.append(name)
            return {
                "metric_ids": {n: m["id"] for n, m in self.world.metrics.items()},
                "created": created,
            }
        if tool == "start_experiment":
            experiment = self.world.experiment(arguments["experiment_id"])
            started = experiment["status"] == flagsmith.STATUS_CREATED
            experiment["status"] = flagsmith.STATUS_RUNNING
            return {"started": started, "experiment": experiment}
        if tool == "refresh_results":
            return {"refreshed": self.refresh_accepted}
        if tool == "get_results":
            return self.results
        return self.world.experiment(arguments["experiment_id"])

    def tools_called(self) -> list[str]:
        return [tool for tool, _ in self.calls]


def make(world: World | None = None):
    world = world or World()
    mcp, admin, clock = FakeMcp(world), FakeAdmin(world), Clock(START)
    fs = flagsmith.Flagsmith(flagsmith.Mcp(mcp), admin, clock=clock)
    return SimpleNamespace(fs=fs, mcp=mcp, admin=admin, clock=clock, world=world)


@pytest.fixture
def t():
    return make()


WRITE_TOOLS = {
    "create_feature",
    "create_feature_multivariate_option",
    "create_experiment",
    "create_environment_feature_version",
    "update_environment_feature_version_state",
    "publish_environment_feature_version",
}


def writes(mcp: FakeMcp) -> list[str]:
    return [tool for tool in mcp.tools_called() if tool in WRITE_TOOLS]


# ---------------------------------------------------------------------------
# The calls task 1.3 recorded, replayed with run IDs and IDs taken from them
# ---------------------------------------------------------------------------
def test_ensure_experiment_sends_what_task_1_3_recorded():
    feature_create = recorded("create_feature", type=flagsmith.API_FEATURE_MULTIVARIATE)
    option_create = recorded("create_feature_multivariate_option")
    experiment_create = recorded("create_experiment")
    feature_key = feature_create["name"]
    run_id = feature_key.removeprefix(names.EXPERIMENT_FEATURE_PREFIX)
    rollout = experiment_create["experiment_rollout"]
    world = World(
        feature_id=option_create["feature_pk"],
        option_id=rollout["multivariate_feature_state_values"][0]["multivariate_feature_option"],
    )
    t = make(world)

    out = t.fs.ensure_experiment(run_id, PLAN)

    assert t.mcp.first("list_environments") == recorded("list_environments")
    assert t.mcp.first("list_project_features") == recorded("list_project_features")
    assert t.mcp.first("create_feature") == feature_create
    assert t.mcp.first("list_feature_multivariate_options") == recorded(
        "list_feature_multivariate_options"
    )
    assert t.mcp.first("create_feature_multivariate_option") == option_create
    sent = t.mcp.first("create_experiment")
    assert set(sent) == set(experiment_create)
    assert sent["experiment_rollout"] == rollout  # rollout 100, current, cheaper at 50
    assert (sent["feature"], sent["name"]) == (experiment_create["feature"], feature_key)
    assert sent["environment_api_key"] == ENV_KEY  # recorded redacted
    assert sent["hypothesis"] == PLAN["hypothesis"]
    shape = set(experiment_create["metrics"][0])
    assert [set(m) for m in sent["metrics"]] == [shape] * len(names.METRICS)
    assert out == {
        "feature_key": names.experiment_feature_key(run_id),
        "experiment_id": world.experiments[0]["id"],
        "metric_ids": {name: world.metrics[name]["id"] for name in names.METRICS},
    }


def test_experiment_metrics_go_primary_first_with_their_expected_directions(t):
    out = t.fs.ensure_experiment("rlive1530", PLAN)
    metrics = t.mcp.first("create_experiment")["metrics"]
    assert [m["metric"] for m in metrics] == [str(out["metric_ids"][n]) for n in names.METRICS]
    assert names.METRICS[0] == names.PRIMARY_METRIC  # Flagsmith has no primary flag
    assert [m["expected_direction"] for m in metrics] == [
        names.EXPECTED_DIRECTION_API[names.EXPERIMENT_EXPECTED_DIRECTIONS[n]] for n in names.METRICS
    ]
    assert t.admin.tools_called() == ["ensure_metrics"]  # metrics through the Admin API, not MCP


def test_permanent_features_are_created_as_task_1_3_created_its_switch(t):
    switch = recorded("create_feature", type=flagsmith.API_FEATURE_STANDARD)
    t.fs.ensure_permanent_features()
    created = {a["name"]: a for tool, a in t.mcp.calls if tool == "create_feature"}
    assert created[names.KILL_SWITCH_FEATURE] == {**switch, "name": names.KILL_SWITCH_FEATURE}
    assert created[names.DEFAULT_MODEL_FEATURE] == {
        "project_pk": names.FLAGSMITH_PROJECT_ID,
        "name": names.DEFAULT_MODEL_FEATURE,
        "type": flagsmith.API_FEATURE_STANDARD,
        "initial_value": names.DEFAULT_MODEL_DEFAULT,
        "default_enabled": True,
    }


# ---------------------------------------------------------------------------
# Get-or-create: twice creates once; a mismatch stops; nothing found is reset
# ---------------------------------------------------------------------------
def test_ensure_permanent_features_twice_creates_once(t):
    first = t.fs.ensure_permanent_features()
    second = t.fs.ensure_permanent_features()
    assert first == second == {n: t.world.features[n]["id"] for n in names.PERMANENT_FEATURES}
    assert writes(t.mcp) == ["create_feature", "create_feature"]


def test_ensure_experiment_twice_creates_once(t):
    first = t.fs.ensure_experiment("rlive1530", PLAN)
    second = t.fs.ensure_experiment("rlive1530", PLAN)
    assert first == second
    assert writes(t.mcp) == [
        "create_feature",
        "create_feature_multivariate_option",
        "create_experiment",
    ]
    assert t.admin.tools_called() == ["ensure_metrics", "ensure_metrics"]  # get-or-create there


def test_found_permanent_features_keep_their_state_and_value():
    """Gate 1 rule 5: a found feature is never reset. Resetting assistant_default_model would
    undo a promotion; resetting the kill switch would undo a rollback."""
    world = World()
    world.add_feature(names.KILL_SWITCH_FEATURE, flagsmith.API_FEATURE_STANDARD, enabled=False)
    world.add_feature(
        names.DEFAULT_MODEL_FEATURE, flagsmith.API_FEATURE_STANDARD, names.VARIANT_CHEAPER
    )
    before = copy.deepcopy(world.features)
    t = make(world)
    t.fs.ensure_permanent_features()
    assert writes(t.mcp) == []
    assert world.features == before


@pytest.mark.parametrize(
    "name, kind, value",
    [
        (names.KILL_SWITCH_FEATURE, "MULTIVARIATE", None),
        (names.DEFAULT_MODEL_FEATURE, "MULTIVARIATE", names.VARIANT_CURRENT),
        (names.DEFAULT_MODEL_FEATURE, "STANDARD", "fable"),
        (names.DEFAULT_MODEL_FEATURE, "STANDARD", True),
        (names.DEFAULT_MODEL_FEATURE, "STANDARD", None),
    ],
)
def test_a_found_permanent_feature_that_differs_stops(name, kind, value):
    world = World()
    world.add_feature(name, kind, value)
    t = make(world)
    with pytest.raises(flagsmith.FlagsmithError, match=name):
        t.fs.ensure_permanent_features()
    assert writes(t.mcp) == []


def test_a_found_experiment_feature_that_is_not_multivariate_stops():
    world = World()
    world.add_feature(names.experiment_feature_key("rlive1530"), "STANDARD")
    t = make(world)
    with pytest.raises(flagsmith.FlagsmithError, match="MULTIVARIATE"):
        t.fs.ensure_experiment("rlive1530", PLAN)
    assert writes(t.mcp) == []


@pytest.mark.parametrize("key, value", [("cheaper", "current"), (None, "cheaper")])
def test_a_found_option_that_differs_stops(key, value):
    world = World()
    feature = world.add_feature(
        names.experiment_feature_key("rlive1530"), "MULTIVARIATE", names.CONTROL_VALUE
    )
    world.add_option(feature["id"], key, value, 50)
    t = make(world)
    with pytest.raises(flagsmith.FlagsmithError):
        t.fs.ensure_experiment("rlive1530", PLAN)
    assert writes(t.mcp) == []


def test_the_rollout_carries_the_split_even_when_the_option_defaults_to_0():
    """Gate 1 rule 3: 1.2's experiment 807 had its option at 0 and split 78 to 72; the
    experiment's rollout block decides the split."""
    world = World()
    feature = world.add_feature(
        names.experiment_feature_key("rlive1530"), "MULTIVARIATE", names.CONTROL_VALUE
    )
    option = world.add_option(feature["id"], names.TREATMENT_OPTION_KEY, "cheaper", 0.0)
    t = make(world)
    t.fs.ensure_experiment("rlive1530", PLAN)
    rollout = t.mcp.first("create_experiment")["experiment_rollout"]
    assert rollout["rollout_percentage"] == names.EXPERIMENT_ROLLOUT_PERCENT == 100
    assert rollout["multivariate_feature_state_values"] == [
        {"multivariate_feature_option": option["id"], "percentage_allocation": 50}
    ]
    assert names.TREATMENT_PERCENT == 50


def test_an_ended_experiment_on_the_runs_feature_stops():
    world = World()
    t = make(world)
    t.fs.ensure_experiment("rlive1530", PLAN)
    world.experiments[0]["status"] = flagsmith.EXPERIMENT_STATUS_ENDED
    with pytest.raises(flagsmith.FlagsmithError, match="ended"):
        t.fs.ensure_experiment("rlive1530", PLAN)
    assert len(world.experiments) == 1


# ---------------------------------------------------------------------------
# Gate 1 rule 1: no experiment can end or change status through this code
# ---------------------------------------------------------------------------
def test_the_mcp_allow_list_is_on_the_gateways_list_and_writes_no_experiment_status():
    gateway = {name.removeprefix(flagsmith.MCP_TOOL_PREFIX) for name in GATEWAY_TOOLS}
    assert set(flagsmith.MCP_TOOLS) <= gateway
    assert "update_experiment" not in flagsmith.MCP_TOOLS
    experiment_tools = [tool for tool in flagsmith.MCP_TOOLS if "experiment" in tool]
    assert experiment_tools == ["list_experiments", "create_experiment"]
    assert "create_metric" not in flagsmith.MCP_TOOLS  # it drops the definition (task 1.3)


@pytest.mark.parametrize(
    "tool",
    sorted(
        {n.removeprefix(flagsmith.MCP_TOOL_PREFIX) for n in GATEWAY_TOOLS}
        - set(flagsmith.MCP_TOOLS)
    ),
)
def test_a_tool_off_the_allow_list_is_refused_before_the_call(tool):
    world = World()
    fake = FakeMcp(world)
    with pytest.raises(flagsmith.ToolRefused):
        flagsmith.Mcp(fake).call(tool, {})
    assert fake.calls == []


@pytest.mark.parametrize("tool", ["create_experiment", "update_experiment"])
@pytest.mark.parametrize("field", flagsmith.MCP_EXPERIMENT_FORBIDDEN_FIELDS)
def test_experiment_arguments_that_carry_a_status_field_are_refused(tool, field):
    experiment = recorded("create_experiment")
    for arguments in (
        {**experiment, field: "completed"},
        {**experiment, "experiment_rollout": {**experiment["experiment_rollout"], field: None}},
    ):
        with pytest.raises(flagsmith.ToolRefused, match=field):
            flagsmith.check_experiment_arguments(tool, arguments)
    world = World()
    fake = FakeMcp(world)
    with pytest.raises(flagsmith.ToolRefused):
        flagsmith.Mcp(fake).call(tool, {**experiment, field: "completed"})
    assert fake.calls == []


def test_the_forbidden_fields_are_the_ones_the_gateway_lists_for_experiments():
    schema = GATEWAY_TOOLS[flagsmith.MCP_TOOL_PREFIX + "update_experiment"]["inputSchema"]
    assert set(flagsmith.MCP_EXPERIMENT_FORBIDDEN_FIELDS) <= set(schema["properties"])
    assert flagsmith.MCP_EXPERIMENT_FORBIDDEN_FIELDS == (
        "status",
        "rollout_decision",
        "rollout_variant",
        "started_at",
        "ended_at",
    )


def test_an_admin_tool_off_its_allow_list_is_refused_before_the_call():
    class Client:
        calls = []

        def call_tool_sync(self, *args):
            self.calls.append(args)

        def invoke(self, **kwargs):
            self.calls.append(kwargs)

    for transport in (flagsmith.GatewayAdmin(Client()), flagsmith.LambdaAdmin(Client())):
        for tool in ("complete_experiment", "pause_experiment", "end_experiment", "get"):
            with pytest.raises(flagsmith.ToolRefused):
                transport.call(tool, {"environment_api_key": ENV_KEY, "experiment_id": 1})
    assert Client.calls == []


# ---------------------------------------------------------------------------
# Gate 1 rule 2: ended means completed
# ---------------------------------------------------------------------------
def test_experiment_status_is_the_raw_status_and_only_completed_is_ended(t):
    out = t.fs.ensure_experiment("rlive1530", PLAN)
    experiment = t.world.experiment(out["experiment_id"])
    assert "`status` (`created`, `running`, `paused`, `completed`)" in SPIKE_EVIDENCE
    assert flagsmith.FLAGSMITH_EXPERIMENT_STATUSES == ("created", "running", "paused", "completed")
    for status in flagsmith.FLAGSMITH_EXPERIMENT_STATUSES:
        experiment["status"] = status
        assert t.fs.experiment_status(out["experiment_id"]) == status
        assert flagsmith.is_ended(status) is (status == "completed")
    assert flagsmith.is_ended("paused") is False


def test_start_experiment_goes_through_the_admin_tool(t):
    out = t.fs.ensure_experiment("rlive1530", PLAN)
    started = t.fs.start_experiment(out["experiment_id"])
    assert started["started"] is True
    assert t.fs.experiment_status(out["experiment_id"]) == flagsmith.STATUS_RUNNING
    assert t.admin.calls[-2] == (
        "start_experiment",
        {"environment_api_key": ENV_KEY, "experiment_id": out["experiment_id"]},
    )


# ---------------------------------------------------------------------------
# Kill switch and default model: a v2 version, created, updated, published
# ---------------------------------------------------------------------------
# The four calls and their arguments, as task 1.3 step 9 changed a state with v2 versioning.
VERSION_CALLS = {
    "create_environment_feature_version": {"environment_pk", "feature_pk"},
    "get_environment_feature_version_states": {
        "environment_pk",
        "feature_pk",
        "environment_feature_version_pk",
    },
    "update_environment_feature_version_state": {
        "environment_pk",
        "feature_pk",
        "environment_feature_version_pk",
        flagsmith.API_PATH_ID,
        "feature",
        "enabled",
        "feature_state_value",
    },
    "publish_environment_feature_version": {"environment_pk", "feature_pk", "id"},
}


def test_the_version_calls_are_the_ones_task_1_3_made():
    for tool in VERSION_CALLS:
        assert f"`{tool}" in SPIKE_EVIDENCE
    assert "id__path: <state id>" in SPIKE_EVIDENCE
    assert '{"type":\n   "unicode", "string_value": "current"}' in SPIKE_EVIDENCE


def _permanent(t) -> None:
    t.fs.ensure_permanent_features()
    t.mcp.calls.clear()


def test_set_default_model_publishes_a_version_with_the_new_string(t):
    _permanent(t)
    feature = t.world.features[names.DEFAULT_MODEL_FEATURE]
    t.fs.set_default_model(names.VARIANT_CHEAPER)
    called = [tool for tool in t.mcp.tools_called() if tool in VERSION_CALLS]
    assert called == list(VERSION_CALLS)
    for tool, keys in VERSION_CALLS.items():
        assert set(t.mcp.first(tool)) == keys, tool
    update = t.mcp.first("update_environment_feature_version_state")
    assert update["feature_state_value"] == {
        "type": flagsmith.API_VERSION_VALUE_TYPE,
        "string_value": names.VARIANT_CHEAPER,
    }
    assert update["enabled"] is True and update["feature"] == feature["id"]
    assert update["environment_pk"] == ENV_ID
    live = feature["environment_feature_state"]
    assert (live["feature_state_value"], live["enabled"]) == (names.VARIANT_CHEAPER, True)


def test_set_kill_switch_changes_only_enabled(t):
    _permanent(t)
    switch = t.world.features[names.KILL_SWITCH_FEATURE]
    t.fs.set_kill_switch(False)
    update = t.mcp.first("update_environment_feature_version_state")
    assert update["enabled"] is False
    assert update["feature_state_value"] == _typed(None)  # the state's own value, unchanged
    assert switch["environment_feature_state"]["enabled"] is False
    t.mcp.calls.clear()
    t.fs.set_kill_switch(True)
    assert switch["environment_feature_state"]["enabled"] is True
    assert "publish_environment_feature_version" in t.mcp.tools_called()


def test_setting_a_value_already_live_writes_nothing(t):
    _permanent(t)
    t.fs.set_kill_switch(True)
    t.fs.set_default_model(names.VARIANT_CURRENT)
    assert writes(t.mcp) == []


def test_set_default_model_refuses_a_value_that_is_not_a_variant(t):
    _permanent(t)
    with pytest.raises(ValueError):
        t.fs.set_default_model("fable")
    assert t.mcp.calls == []


def test_setting_a_feature_that_is_missing_stops(t):
    with pytest.raises(flagsmith.FlagsmithError, match=names.KILL_SWITCH_FEATURE):
        t.fs.set_kill_switch(False)
    assert writes(t.mcp) == []


# ---------------------------------------------------------------------------
# Results: refresh window, freshness, and the mapping onto results.json
# ---------------------------------------------------------------------------
def test_refresh_inside_5_minutes_of_the_last_one_does_not_call_the_api(t):
    last = names.utc_iso(START)
    t.clock.advance(names.RESULTS_REFRESH_MIN_SECONDS - 1)
    assert t.fs.refresh_results(819, last) is None
    assert t.admin.calls == []
    t.clock.advance(1)
    assert t.fs.refresh_results(819, last) == names.utc_iso(t.clock.now)
    assert t.admin.tools_called() == ["refresh_results"]


def test_refresh_with_no_earlier_refresh_calls_the_api_and_a_429_refreshes_nothing(t):
    assert t.fs.refresh_results(819, None) == names.utc_iso(START)
    t.admin.refresh_accepted = False  # Flagsmith's own 5-minute limit answered 429
    assert t.fs.refresh_results(819, None) is None


def _envelopes() -> list[dict]:
    """Every results envelope task 1.4 recorded for experiment 807."""
    return [
        json.loads(line)["results"]
        for line in EXPERIMENT_EVIDENCE.splitlines()
        if line.startswith('{"results":')
    ]


def test_freshness_on_the_reads_task_1_4_recorded():
    reads = _envelopes()
    running = next(r for r in reads if not r["is_final"])
    final = next(r for r in reads if r["is_final"])
    # Running, read 2.3 minutes after the refresh (22:26:54Z on the caller's clock).
    assert flagsmith.results_current(running, "2026-11-26T22:26:54.000000Z")
    # Ended, then refreshed (23:40:46Z): as_of is the end, earlier than the request, but final.
    assert final["as_of"] < final["refresh_requested_at"]
    assert flagsmith.results_current(final, "2026-11-26T23:40:46.000000Z")
    # The same running read is not current for a refresh asked 5 minutes later.
    assert not flagsmith.results_current(running, "2026-11-26T22:31:54.000000Z")


def _section_reads(heading: str) -> list[dict]:
    """The results envelopes recorded under one heading of evidence/1.4-spike-experiment.md
    (the same reads as c2's evidence/1.4-logs/)."""
    text = EXPERIMENT_EVIDENCE.split(heading, 1)[1].split("\n## ", 1)[0]
    return [
        json.loads(line)["results"] for line in text.splitlines() if line.startswith('{"results":')
    ]


def test_freshness_after_the_end_takes_the_final_read_and_not_the_stale_one():
    """c2's review: just after the end, with no new refresh, a read is the pre-end snapshot,
    with as_of later than refresh_requested_at and is_final false. After a refresh it is
    final, with as_of (the end) earlier than refresh_requested_at. A rule of as_of at or after
    the request alone would take the first and refuse the second."""
    (stale,) = _section_reads("## Ending both spike experiments")
    finals = _section_reads("## A results refresh after the end")
    assert stale["as_of"] > stale["refresh_requested_at"] and stale["is_final"] is False
    assert finals and all(r["as_of"] < r["refresh_requested_at"] and r["is_final"] for r in finals)
    refreshed_after_the_end = "2026-11-26T23:40:46.000000Z"  # the refresh 1.4 asked for
    assert not flagsmith.results_current(stale, refreshed_after_the_end)
    for final in finals:
        assert flagsmith.results_current(final, refreshed_after_the_end)


def test_the_metric_id_types_are_the_ones_each_api_uses(t):
    """For c2 to record: an int from the Admin API, a string in MCP create's metrics[].metric,
    an int in the results' metric_id. This code sends and matches each as its API does."""
    admin_create = re.search(r'-> 201 \{"id": (\S+?),', SPIKE_EVIDENCE).group(1)
    assert admin_create.isdigit()  # JSON int: no quotes
    assert isinstance(recorded("create_experiment")["metrics"][0]["metric"], str)
    assert type(RESULTS_SAMPLE["payload"]["metrics"][0]["metric_id"]) is int
    out = t.fs.ensure_experiment("rlive1530", PLAN)
    assert all(type(metric_id) is int for metric_id in out["metric_ids"].values())
    sent = t.mcp.first("create_experiment")["metrics"]
    assert [m["metric"] for m in sent] == [str(out["metric_ids"][n]) for n in names.METRICS]


@pytest.mark.parametrize(
    "change, current",
    [
        ({}, True),
        ({"as_of": "2026-11-26T22:26:54.700000Z"}, False),  # still computing that request
        ({"as_of": "2026-11-26T22:26:54.700000Z", "is_final": True}, True),
        ({"last_error_at": "2026-11-26T22:26:55.000000Z"}, False),  # that refresh failed
        ({"last_error_at": "2026-11-26T22:20:00.000000Z"}, True),  # an earlier one did
        ({"refresh_requested_at": "2026-11-26T22:24:54.721003Z"}, False),  # an older request
        ({"refresh_requested_at": "2026-11-26T22:26:30.000000Z"}, True),  # clocks 25 s apart
        ({"refresh_requested_at": None}, False),
    ],
)
def test_freshness_rule(change, current):
    """The sample read, changed one field at a time, against a refresh asked at the moment
    the sample's refresh_requested_at records."""
    envelope = {**RESULTS_SAMPLE, **change}
    assert flagsmith.results_current(envelope, "2026-11-26T22:26:54.721003Z") is current


def test_get_results_refuses_a_read_that_is_not_current_after_the_refresh(t):
    metric_ids = {names.PRIMARY_METRIC: RESULTS_SAMPLE["payload"]["metrics"][0]["metric_id"]}
    t.admin.results = {**RESULTS_SAMPLE, "as_of": "2026-11-26T22:26:54.700000Z"}
    with pytest.raises(flagsmith.ResultsNotCurrent):
        t.fs.get_results(807, metric_ids, refreshed_at="2026-11-26T22:26:54.721003Z")
    assert t.fs.get_results(807, metric_ids)["ready"] is False  # no refresh named: no check


def _metric(metric_id: int, control: tuple, cheaper: tuple, ci_low=None) -> dict:
    stats = {
        label: {"n": n, "sum": total, "sum_squares": total}
        for label, (n, total) in (("control", control), ("cheaper", cheaper))
    }
    inference = {} if ci_low is None else {"cheaper": {"lift": 0.1, "ci_low": ci_low}}
    return {"metric_id": metric_id, "variants": stats, "inference": inference}


def _envelope(metrics: list[dict], points: list[dict]) -> dict:
    return {
        **RESULTS_SAMPLE,
        "payload": {
            "metrics": metrics,
            "srm_p_value": None,
            "exposures_timeseries": {"points": points, "granularity": "hour"},
        },
    }


METRIC_IDS = {name: 858 + n for n, name in enumerate(names.METRICS)}


def test_get_results_maps_task_1_4s_sample(t):
    metric = RESULTS_SAMPLE["payload"]["metrics"][0]
    control, cheaper = metric["variants"]["control"], metric["variants"]["cheaper"]
    exposures = RESULTS_SAMPLE["payload"]["exposures_timeseries"]["points"][0]["new_identities"]
    results = t.fs.get_results(807, {names.PRIMARY_METRIC: metric["metric_id"]})
    contracts.validate("results", results)
    assert results == {
        "ready": False,  # the two guardrail means are null
        "variations": {
            names.VARIANT_CURRENT: {
                "identities": exposures[names.CONTROL_LABEL],
                "helpful_rate": control["sum"] / control["n"],
                "mean_latency_ms": None,
                "mean_cost_micro_usd": None,
            },
            names.VARIANT_CHEAPER: {
                "identities": exposures[names.VARIANT_CHEAPER],
                "helpful_rate": cheaper["sum"] / cheaper["n"],
                "mean_latency_ms": None,
                "mean_cost_micro_usd": None,
            },
        },
        "prob_cheaper_not_worse": None,  # Flagsmith has no such field (task 1.4)
        "helpful_lift_ci_lower": metric["inference"]["cheaper"]["ci_low"],
    }


@pytest.mark.parametrize(
    "control_identities, cheaper_identities, null_metric, ready",
    [
        (50, 50, None, True),
        (49, 50, None, False),
        (50, 49, None, False),
        (50, 50, names.METRIC_MEAN_COST_MICRO_USD, False),
        (50, 50, names.METRIC_HELPFUL_RATE, False),
    ],
)
def test_get_results_computes_ready(t, control_identities, cheaper_identities, null_metric, ready):
    metrics = [
        _metric(metric_id, (control_identities, 40), (cheaper_identities, 41), -0.02)
        for name, metric_id in METRIC_IDS.items()
        if name != null_metric
    ]
    # Two hourly buckets: identities add up across them, and never come from n.
    points = [
        {"bucket": "b1", "new_identities": {"control": control_identities - 10, "cheaper": 20}},
        {"bucket": "b2", "new_identities": {"control": 10, "cheaper": cheaper_identities - 20}},
    ]
    for metric in metrics:
        metric["variants"]["control"]["n"] = 999
    t.admin.results = _envelope(metrics, points)
    results = t.fs.get_results(807, METRIC_IDS)
    contracts.validate("results", results)
    assert results["ready"] is ready
    current = results["variations"][names.VARIANT_CURRENT]
    assert current["identities"] == control_identities
    assert results["variations"][names.VARIANT_CHEAPER]["identities"] == cheaper_identities
    if null_metric != names.METRIC_HELPFUL_RATE:
        assert current["helpful_rate"] == 40 / 999
        assert results["helpful_lift_ci_lower"] == -0.02


@pytest.mark.skipif(not RESULTS_36.exists(), reason="evidence/3.6-results.json arrives with 3.6")
def test_get_results_maps_task_3_6s_real_response(t):
    recorded_36 = json.loads(RESULTS_36.read_text())
    t.admin.results = recorded_36.get("results", recorded_36)
    metric_ids = {m["metric_name"]: m["metric"] for m in recorded_36.get("experiment_metrics", [])}
    results = t.fs.get_results(1, metric_ids or METRIC_IDS)
    contracts.validate("results", results)


# ---------------------------------------------------------------------------
# The API spellings (Gate 1 rule 4), pinned against the evidence
# ---------------------------------------------------------------------------
def test_the_api_spellings_match_the_evidence():
    assert (
        flagsmith.API_FEATURE_MULTIVARIATE
        == recorded("create_feature", name=recorded("create_experiment")["name"])["type"]
        == "MULTIVARIATE"
    )
    assert flagsmith.API_FEATURE_STANDARD == "STANDARD"
    assert flagsmith.API_OPTION_TYPE == recorded("create_feature_multivariate_option")["type"]
    value = recorded("create_experiment")["experiment_rollout"]["feature_state_value"]
    assert value == {"type": flagsmith.API_ROLLOUT_VALUE_TYPE, "value": names.CONTROL_VALUE}
    assert flagsmith.API_VERSION_VALUE_TYPE == "unicode"
    assert flagsmith.API_PATH_ID == "id__path"
    assert f"`{flagsmith.API_PATH_ID}`" in SPIKE_EVIDENCE
    assert flagsmith.MCP_TOOL_PREFIX == "flagsmith___"
    assert flagsmith.ADMIN_TOOL_PREFIX == "flagsmith-admin___"
    assert '"definition": {"version": 1, "event": "spike_event"}' in SPIKE_EVIDENCE
    assert handler.API_METRIC_DEFINITION_VERSION == 1
    assert "/experiment-metrics/" in SPIKE_EVIDENCE and "-> 201" in SPIKE_EVIDENCE
    assert handler.API_CREATED == 201 and handler.API_REFRESH_ACCEPTED == 202
    assert handler.API_REFRESH_TOO_SOON == 429 and "429" in SPIKE_EVIDENCE


# ---------------------------------------------------------------------------
# The admin transports
# ---------------------------------------------------------------------------
def test_gateway_admin_calls_flagsmith_admin_tools_and_unwraps_the_answer():
    class Client:
        def __init__(self, answer):
            self.answer, self.calls = answer, []

        def call_tool_sync(self, tool_use_id, name, arguments):
            self.calls.append((name, arguments))
            return {"status": "success", "content": [{"text": json.dumps(self.answer)}]}

    ok = Client({"ok": True, "result": {"refreshed": True}})
    args = {"environment_api_key": ENV_KEY, "experiment_id": 819}
    assert flagsmith.GatewayAdmin(ok).call("refresh_results", args) == {"refreshed": True}
    assert ok.calls == [(f"{names.GATEWAY_TARGET_FLAGSMITH_ADMIN}___refresh_results", args)]
    failed = Client({"ok": False, "error": {"type": "refused", "message": "no"}})
    with pytest.raises(flagsmith.FlagsmithError, match="no"):
        flagsmith.GatewayAdmin(failed).call("refresh_results", args)


def test_lambda_admin_invokes_the_function_with_the_direct_event():
    class Lambda:
        def __init__(self, answer, error=None):
            self.answer, self.error, self.calls = answer, error, []

        def invoke(self, **kwargs):
            self.calls.append(kwargs)
            reply = {"Payload": io.BytesIO(json.dumps(self.answer).encode())}
            return {**reply, "FunctionError": self.error} if self.error else reply

    args = {"environment_api_key": ENV_KEY, "experiment_id": 819}
    client = Lambda({"ok": True, "result": {"status": "running"}})
    assert flagsmith.LambdaAdmin(client).call("get_experiment", args) == {"status": "running"}
    assert client.calls[0]["FunctionName"] == names.FLAGSMITH_ADMIN_FUNCTION
    assert json.loads(client.calls[0]["Payload"]) == {"tool": "get_experiment", "arguments": args}
    with pytest.raises(flagsmith.FlagsmithError):
        flagsmith.LambdaAdmin(Lambda({"errorMessage": "x"}, "Unhandled")).call(
            "get_experiment", args
        )


# ---------------------------------------------------------------------------
# rm-flagsmith-admin: paths (Gate 1 rule 1(c)), tools, routing and the key
# ---------------------------------------------------------------------------
def test_admin_path_builds_only_the_six_gate_1_paths():
    base = f"/api/v1/environments/{ENV_KEY}/"
    assert handler.admin_path(handler.PATH_METRICS, ENV_KEY) == base + "experiment-metrics/"
    assert (
        handler.admin_path(handler.PATH_METRICS, ENV_KEY, query="helpful_rate")
        == base + "experiment-metrics/?q=helpful_rate"
    )
    built = {
        kind: handler.admin_path(kind, ENV_KEY, 819)
        for kind in handler.PATH_KINDS
        if kind != handler.PATH_METRICS
    }
    assert built == {
        handler.PATH_EXPERIMENT: base + "experiments/819/",
        handler.PATH_START: base + "experiments/819/start/",
        handler.PATH_RESULTS: base + "experiments/819/results/",
        handler.PATH_REFRESH: base + "experiments/819/results/refresh/",
        handler.PATH_EXPOSURES: base + "experiments/819/exposures/",
    }


@pytest.mark.parametrize(
    "kind, key, experiment_id, query",
    [
        ("complete", ENV_KEY, 819, None),
        ("pause", ENV_KEY, 819, None),
        ("rollout", ENV_KEY, 819, None),
        ("complete/", ENV_KEY, 819, None),
        ("experiments", ENV_KEY, 819, None),
        ("exposures-refresh", ENV_KEY, 819, None),
        (handler.PATH_EXPERIMENT, "../projects", 819, None),
        (handler.PATH_EXPERIMENT, "a/b", 819, None),
        (handler.PATH_EXPERIMENT, "key?x=1", 819, None),
        (handler.PATH_EXPERIMENT, "", 819, None),
        (handler.PATH_EXPERIMENT, ENV_KEY, "819/complete", None),
        (handler.PATH_EXPERIMENT, ENV_KEY, True, None),
        (handler.PATH_EXPERIMENT, ENV_KEY, 0, None),
        (handler.PATH_EXPERIMENT, ENV_KEY, None, None),
        (handler.PATH_START, ENV_KEY, 819, "x"),
        (handler.PATH_METRICS, ENV_KEY, 819, None),
        (handler.PATH_METRICS, ENV_KEY, None, "a&status=completed"),
    ],
)
def test_admin_path_refuses_everything_else(kind, key, experiment_id, query):
    with pytest.raises(handler.Refused):
        handler.admin_path(kind, key, experiment_id, query)


def test_the_handler_has_no_way_to_end_or_pause_an_experiment():
    """The forbidden paths appear once, in the constant the final guard checks, and no path
    the builder makes contains one."""
    assert handler.FLAGSMITH_ADMIN_FORBIDDEN_PATHS == ("complete/", "pause/", "rollout/")
    source = (ROOT / "agent/flagsmith_admin/handler.py").read_text()
    for forbidden in handler.FLAGSMITH_ADMIN_FORBIDDEN_PATHS:
        assert source.count(f'"{forbidden}"') == 1
    buildable = [handler.admin_path(handler.PATH_METRICS, ENV_KEY)] + [
        handler.admin_path(kind, ENV_KEY, 819)
        for kind in handler.PATH_KINDS
        if kind != handler.PATH_METRICS
    ]
    assert not [p for p in buildable for f in handler.FLAGSMITH_ADMIN_FORBIDDEN_PATHS if f in p]
    assert set(handler.TOOLS) == set(flagsmith.ADMIN_TOOLS)


def test_the_final_guard_refuses_a_forbidden_path_even_if_a_kind_for_one_were_added(
    monkeypatch,
):
    """Defence in depth: no kind builds one today, so this edits the suffix table the way a
    careless change would, and the guard still refuses."""
    for forbidden in handler.FLAGSMITH_ADMIN_FORBIDDEN_PATHS:
        monkeypatch.setitem(handler._EXPERIMENT_SUFFIXES, "added", forbidden)
        with pytest.raises(handler.Refused, match="ends, pauses or rolls out"):
            handler.admin_path("added", ENV_KEY, 819)


def test_the_only_calls_are_get_reads_and_four_posts():
    """c2's review: Flagsmith's spec lets DELETE, PUT and PATCH on the experiment read's own
    path delete it or rewrite its rollout, metrics and feature."""
    assert handler.FLAGSMITH_ADMIN_METHODS == ("GET", "POST")
    assert handler.ADMIN_ROUTES == {
        ("GET", handler.PATH_METRICS),
        ("POST", handler.PATH_METRICS),
        ("GET", handler.PATH_EXPERIMENT),
        ("POST", handler.PATH_START),
        ("GET", handler.PATH_RESULTS),
        ("POST", handler.PATH_REFRESH),
        ("GET", handler.PATH_EXPOSURES),
    }


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH"])
def test_delete_put_and_patch_are_never_sent(method, monkeypatch):
    http = FakeHttp()
    api = handler.Api(http, ENV_KEY)
    for kind in handler.PATH_KINDS:
        experiment_id = None if kind == handler.PATH_METRICS else 819
        with pytest.raises(handler.Refused):
            api.send(method, kind, experiment_id=experiment_id)
    assert http.calls == []

    def no_request(*args, **kwargs):
        raise AssertionError("no request may leave")

    monkeypatch.setattr(urllib.request, "urlopen", no_request)
    path = handler.admin_path(handler.PATH_EXPERIMENT, ENV_KEY, 819)
    with pytest.raises(handler.Refused):  # the HTTP layer refuses it too
        handler.urllib_http(names.api_key_authorization(ORG_KEY))(method, path, None)


@pytest.mark.parametrize(
    "method, kind",
    [
        ("POST", handler.PATH_EXPERIMENT),
        ("GET", handler.PATH_START),
        ("GET", handler.PATH_REFRESH),
        ("POST", handler.PATH_RESULTS),
        ("POST", handler.PATH_EXPOSURES),
    ],
)
def test_a_method_on_a_path_it_is_not_paired_with_is_refused(method, kind):
    http = FakeHttp()
    with pytest.raises(handler.Refused):
        handler.Api(http, ENV_KEY).send(method, kind, experiment_id=819)
    assert http.calls == []


def test_no_delete_tool_and_no_experiment_update_can_be_called():
    assert not [tool for tool in flagsmith.MCP_TOOLS if tool.startswith("delete_")]
    assert "update_experiment" not in flagsmith.MCP_TOOLS
    deletes = sorted(
        n.removeprefix(flagsmith.MCP_TOOL_PREFIX) for n in GATEWAY_TOOLS if "___delete_" in n
    )
    assert deletes  # the Gateway lists some; each is refused before the call
    fake = FakeMcp(World())
    for tool in ["update_experiment", *deletes]:
        with pytest.raises(flagsmith.ToolRefused):
            flagsmith.Mcp(fake).call(tool, {})
    assert fake.calls == []


class FakeHttp:
    """The Admin API as rm-flagsmith-admin sees it: (method, path) -> (status, body)."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        answer = self.routes.get((method, path))
        if callable(answer):
            return answer(body)
        return answer if answer is not None else (404, {"detail": "Not found."})


def direct(tool: str, **arguments) -> dict:
    return {"tool": tool, "arguments": {"environment_api_key": ENV_KEY, **arguments}}


def metrics_path(query: str | None = None) -> str:
    return handler.admin_path(handler.PATH_METRICS, ENV_KEY, query=query)


def test_ensure_metrics_creates_the_three_then_finds_them():
    stored: dict[str, dict] = {}

    def search(name):
        return lambda body: (
            200,
            {"count": 1, "results": [stored[name]]}
            if name in stored
            else {"count": 0, "results": []},
        )

    def create(body):
        stored[body["name"]] = {"id": 868 + len(stored), **body}
        return 201, stored[body["name"]]

    http = FakeHttp(
        {("GET", metrics_path(name)): search(name) for name in names.METRICS}
        | {("POST", metrics_path()): create}
    )
    first = handler.handle(direct("ensure_metrics"), None, http=http)
    assert first["ok"] is True
    assert first["result"]["created"] == list(names.METRICS)
    posts = [body for method, _, body in http.calls if method == "POST"]
    for body, (name, definition) in zip(posts, names.METRIC_DEFINITIONS.items(), strict=True):
        assert {k: body[k] for k in ("name", "aggregation", "direction", "definition")} == {
            "name": name,
            "aggregation": names.METRIC_AGGREGATION,
            "direction": names.METRIC_DIRECTION_API[definition["direction"]],
            "definition": {"version": 1, "event": definition["event"]},
        }
    http.calls.clear()
    second = handler.handle(direct("ensure_metrics"), None, http=http)
    assert second["result"] == {"metric_ids": first["result"]["metric_ids"], "created": []}
    assert [method for method, _, _ in http.calls] == ["GET"] * len(names.METRICS)


@pytest.mark.parametrize(
    "change",
    [{"aggregation": "sum"}, {"direction": "down"}, {"definition": {"version": 1, "event": "x"}}],
)
def test_a_found_metric_that_differs_stops_and_creates_nothing(change):
    name = names.PRIMARY_METRIC
    definition = names.METRIC_DEFINITIONS[name]
    found = {
        "id": 868,
        "name": name,
        "aggregation": definition["aggregation"],
        "direction": names.METRIC_DIRECTION_API[definition["direction"]],
        "definition": {"version": 1, "event": definition["event"]},
        **change,
    }
    http = FakeHttp({("GET", metrics_path(name)): (200, {"count": 1, "results": [found]})})
    answer = handler.handle(direct("ensure_metrics"), None, http=http)
    assert answer["ok"] is False and name in answer["error"]["message"]
    assert [method for method, _, _ in http.calls] == ["GET"]


def _experiment_routes(status: str, feature: str = "assistant_model_rlive1530") -> FakeHttp:
    experiment = {
        "id": 819,
        "name": feature,
        "hypothesis": "h",
        "status": status,
        "feature": {"id": 306314, "name": feature},
    }
    path = handler.admin_path(handler.PATH_EXPERIMENT, ENV_KEY, 819)
    start = handler.admin_path(handler.PATH_START, ENV_KEY, 819)
    return FakeHttp(
        {
            ("GET", path): (200, experiment),
            ("POST", start): lambda body: (200, {**experiment, "status": "running"}),
        }
    )


def test_start_experiment_starts_only_a_created_experiment_on_a_runs_feature():
    http = _experiment_routes("created")
    answer = handler.handle(direct("start_experiment", experiment_id=819), None, http=http)
    assert answer == {
        "ok": True,
        "result": {
            "started": True,
            "experiment": {**http.routes[("GET", http.calls[0][1])][1], "status": "running"},
        },
    }
    assert http.calls[-1] == (
        "POST",
        handler.admin_path(handler.PATH_START, ENV_KEY, 819),
        {"name": "assistant_model_rlive1530", "hypothesis": "h"},
    )
    running = _experiment_routes("running")
    answer = handler.handle(direct("start_experiment", experiment_id=819), None, http=running)
    assert answer["ok"] and answer["result"]["started"] is False
    assert [method for method, _, _ in running.calls] == ["GET"]


@pytest.mark.parametrize(
    "status, feature",
    [("paused", None), ("completed", None), ("created", "spike_flag"), ("created", "x_rlive1530")],
)
def test_start_experiment_refuses_the_rest(status, feature):
    http = _experiment_routes(status, feature or "assistant_model_rlive1530")
    answer = handler.handle(direct("start_experiment", experiment_id=819), None, http=http)
    assert answer["ok"] is False and answer["error"]["type"] == "refused"
    assert [method for method, _, _ in http.calls] == ["GET"]


def test_refresh_and_results_answers():
    refresh = handler.admin_path(handler.PATH_REFRESH, ENV_KEY, 807)
    results = handler.admin_path(handler.PATH_RESULTS, ENV_KEY, 807)
    http = FakeHttp(
        {("POST", refresh): (202, None), ("GET", results): (200, {"results": RESULTS_SAMPLE})}
    )
    assert handler.handle(direct("refresh_results", experiment_id=807), None, http=http) == {
        "ok": True,
        "result": {"refreshed": True},
    }
    assert handler.handle(direct("get_results", experiment_id=807), None, http=http) == {
        "ok": True,
        "result": RESULTS_SAMPLE,
    }
    too_soon = FakeHttp({("POST", refresh): (429, {"detail": "A refresh was requested recently."})})
    answer = handler.handle(direct("refresh_results", experiment_id=807), None, http=too_soon)
    assert answer["result"] == {"refreshed": False, "detail": "A refresh was requested recently."}


def test_a_gateway_call_names_its_tool_in_the_client_context():
    results = handler.admin_path(handler.PATH_RESULTS, ENV_KEY, 807)
    http = FakeHttp({("GET", results): (200, {"results": RESULTS_SAMPLE})})
    context = SimpleNamespace(
        client_context=SimpleNamespace(
            custom={
                handler.TOOL_NAME_FIELD: f"{names.GATEWAY_TARGET_FLAGSMITH_ADMIN}___get_results"
            }
        )
    )
    event = {"environment_api_key": ENV_KEY, "experiment_id": 807}
    assert handler.handle(event, context, http=http) == {"ok": True, "result": RESULTS_SAMPLE}
    for name in ("flagsmith___get_results", "get_results", "flagsmith-admin___complete"):
        context.client_context.custom[handler.TOOL_NAME_FIELD] = name
        answer = handler.handle(event, context, http=http)
        assert answer["ok"] is False and answer["error"]["type"] == "refused"


@pytest.mark.parametrize(
    "event",
    [
        {"tool": "complete_experiment", "arguments": {"environment_api_key": ENV_KEY}},
        {"tool": "get_results", "arguments": {"environment_api_key": ENV_KEY}},
        {"tool": "get_results", "arguments": {"experiment_id": 1}},
        {
            "tool": "get_results",
            "arguments": {
                "environment_api_key": ENV_KEY,
                "experiment_id": 1,
                "status": "completed",
            },
        },
        {"tool": "ensure_metrics"},
        "get_results",
    ],
)
def test_a_direct_invoke_that_does_not_fit_is_refused_and_sends_nothing(event):
    http = FakeHttp()
    answer = handler.handle(event, None, http=http)
    assert answer["ok"] is False and answer["error"]["type"] == "refused"
    assert http.calls == []


def test_the_key_comes_from_the_secret_and_travels_only_in_the_authorization_header(monkeypatch):
    class Secrets:
        asked = []

        def get_secret_value(self, SecretId):  # noqa: N803 - boto3's spelling
            self.asked.append(SecretId)
            return {"SecretString": ORG_KEY}

    sent = []

    def urlopen(request, timeout):
        sent.append((request, timeout))
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(handler, "_key_cache", {})
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    key = handler._organisation_key(Secrets())
    assert Secrets.asked == [names.SECRET_FLAGSMITH_API_KEY]
    http = handler.urllib_http(names.api_key_authorization(key))
    answer = handler.handle(direct("get_experiment", experiment_id=819), None, http=http)
    request, timeout = sent[0]
    assert request.get_header(names.GATEWAY_API_KEY_HEADER.capitalize()) == (
        names.api_key_authorization(ORG_KEY)
    )
    assert request.full_url == handler.API_BASE_URL + handler.admin_path(
        handler.PATH_EXPERIMENT, ENV_KEY, 819
    )
    assert timeout == handler.REQUEST_TIMEOUT_SECONDS
    assert answer["ok"] is False and ORG_KEY not in json.dumps(answer)


@pytest.mark.parametrize("value", ["", " padded ", None])
def test_a_secret_that_is_not_a_bare_key_is_refused(monkeypatch, value):
    class Secrets:
        def get_secret_value(self, SecretId):  # noqa: N803 - boto3's spelling
            return {"SecretString": value}

    monkeypatch.setattr(handler, "_key_cache", {})
    with pytest.raises(handler.Failed):
        handler._organisation_key(Secrets())


def test_the_environment_key_never_appears_in_an_answer_a_log_or_the_output(caplog, capsys):
    """The lead's condition on passing the key: the Lambda never logs it and never puts it
    in an error, even when an exception's own message names the URL."""

    def unreachable(method, path, body):
        raise OSError(f"connection reset fetching {handler.API_BASE_URL}{path}")

    cases = [
        (direct("get_experiment", experiment_id=819), FakeHttp()),  # Flagsmith answers 404
        (direct("ensure_metrics"), FakeHttp()),
        (direct("get_results", experiment_id=819), unreachable),
        (direct("start_experiment", experiment_id=819), unreachable),
        (
            {
                "tool": "get_results",
                "arguments": {"environment_api_key": "a/b", "experiment_id": 1},
            },
            FakeHttp(),
        ),
    ]
    for event, http in cases:
        key = event["arguments"]["environment_api_key"]
        answer = handler.handle(event, None, http=http)
        assert answer["ok"] is False
        assert key not in json.dumps(answer)
    printed = capsys.readouterr()
    assert ENV_KEY not in printed.out + printed.err + caplog.text


# ---------------------------------------------------------------------------
# Live: pytest -m live, after the lead applies infra/modules/agent (Gate 1 rule 9).
# Scratch spike_ features only, so nothing the real runs use changes; the one experiment
# created and started is rlive<HHMM>, which the human ends. No code here ends one.
# ---------------------------------------------------------------------------
LIVE_STRING_FEATURE = names.SPIKE_NAME_PREFIX + "default_model"  # spike_default_model
LIVE_KILL_SWITCH = names.SPIKE_NAME_PREFIX + "kill_switch"  # made by task 1.3
LIVE_VISIBLE_TIMEOUT_SECONDS = 180
LIVE_POLL_SECONDS = 3


@pytest.fixture(scope="module")
def live():
    """Flagsmith's MCP server directly, with the organisation key (design.md section 8 lists
    live tests among its readers), rm-flagsmith-admin by direct invoke, and the Flagsmith SDK
    with rm/flagsmith/server-key, the key the router reads flags with, so a change is timed
    as the router would see it. All with the human's AWS credentials; no key is printed."""
    import boto3
    from flagsmith import Flagsmith as Sdk
    from strands.tools.mcp import MCPClient

    session = boto3.Session(region_name=names.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    assert account == names.ACCOUNT_ID, f"refusing to run in account {account}"
    key = session.client("secretsmanager").get_secret_value(
        SecretId=names.SECRET_FLAGSMITH_API_KEY
    )["SecretString"]
    client = MCPClient(
        url=names.FLAGSMITH_MCP_URL,
        headers={names.GATEWAY_API_KEY_HEADER: names.api_key_authorization(key)},
    )
    sdk = Sdk(
        environment_key=session.client("secretsmanager").get_secret_value(
            SecretId=names.SECRET_FLAGSMITH_SERVER_KEY
        )["SecretString"]
    )
    del key
    with client:
        fs = flagsmith.Flagsmith(
            flagsmith.Mcp(client, prefix=""), flagsmith.LambdaAdmin(session.client("lambda"))
        )
        run_id = names.scratch_run_id("live", datetime.now(UTC))
        yield SimpleNamespace(fs=fs, sdk=sdk, run_id=run_id, identity=names.run_identity(run_id, 1))


def _seconds_until(check) -> float:
    """Seconds until check() is true, polling; fails after LIVE_VISIBLE_TIMEOUT_SECONDS."""
    started = time.monotonic()
    while not check():
        if time.monotonic() - started > LIVE_VISIBLE_TIMEOUT_SECONDS:
            pytest.fail(f"not visible after {LIVE_VISIBLE_TIMEOUT_SECONDS} s")
        time.sleep(LIVE_POLL_SECONDS)
    return round(time.monotonic() - started, 1)


@pytest.mark.live
def test_live_a_v2_version_changes_a_scratch_string_feature(live):
    """current -> cheaper -> current on spike_default_model, through set_feature_state,
    each read back through the SDK's get_identity_flags."""
    if live.fs._find_feature(LIVE_STRING_FEATURE) is None:
        live.fs.mcp.call(
            "create_feature",
            {
                "project_pk": names.FLAGSMITH_PROJECT_ID,
                "name": LIVE_STRING_FEATURE,
                "type": flagsmith.API_FEATURE_STANDARD,
                "initial_value": names.VARIANT_CURRENT,
                "default_enabled": True,
            },
        )
    seconds = {}
    try:
        for value in (names.VARIANT_CHEAPER, names.VARIANT_CURRENT):
            live.fs.set_feature_state(LIVE_STRING_FEATURE, value=value)
            seconds[value] = _seconds_until(
                lambda value=value: (
                    live.sdk.get_identity_flags(live.identity).get_feature_value(
                        LIVE_STRING_FEATURE
                    )
                    == value
                )
            )
    finally:
        live.fs.set_feature_state(LIVE_STRING_FEATURE, value=names.VARIANT_CURRENT)
    print(json.dumps({"feature": LIVE_STRING_FEATURE, "publish_to_visible_seconds": seconds}))


@pytest.mark.live
def test_live_a_v2_version_toggles_a_scratch_kill_switch(live):
    seconds = {}
    try:
        for enabled in (False, True):
            live.fs.set_feature_state(LIVE_KILL_SWITCH, enabled=enabled)
            seconds[str(enabled).lower()] = _seconds_until(
                lambda enabled=enabled: (
                    live.sdk.get_identity_flags(live.identity).is_feature_enabled(LIVE_KILL_SWITCH)
                    is enabled
                )
            )
    finally:
        live.fs.set_feature_state(LIVE_KILL_SWITCH, enabled=True)
    print(json.dumps({"feature": LIVE_KILL_SWITCH, "publish_to_visible_seconds": seconds}))


@pytest.mark.live
def test_live_rlive_is_created_started_and_read_back(live):
    """tasks.md 2.2: create and start rlive<HHMM>, time the start against rm-flagsmith-admin's
    30-second timeout, and read it back. The human ends it in the dashboard."""
    plan = {"hypothesis": f"Task 2.2 live test on {live.run_id}: no traffic is sent."}
    created = live.fs.ensure_experiment(live.run_id, plan)
    began = time.monotonic()
    started = live.fs.start_experiment(created["experiment_id"])
    start_seconds = round(time.monotonic() - began, 2)
    status = live.fs.experiment_status(created["experiment_id"])
    print(json.dumps({**created, "start_seconds": start_seconds, "status": status}))
    assert start_seconds < names.FLAGSMITH_ADMIN_TIMEOUT_SECONDS
    assert started["experiment"]["status"] == status == flagsmith.STATUS_RUNNING
