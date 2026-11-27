"""rm-flagsmith-admin: the Flagsmith Admin API calls the MCP server cannot make (tasks.md 2.2).

design.md sections 4 and 7: the MCP server cannot create a metric (its create_metric drops
the definition), start an experiment or refresh results, so this function does those, and
reads results and an experiment's status. Its tools:

- ensure_metrics: get or create the three metrics of names.METRIC_DEFINITIONS; a found
  metric that differs on event, aggregation or direction stops with an error;
- start_experiment: start an experiment that has not started, on a run's feature;
- refresh_results: ask for a results refresh (Flagsmith allows one per 5 minutes);
- get_results: the results envelope (as_of, refresh_requested_at, last_error_at, is_final,
  payload), or {} before Flagsmith first computes it;
- get_experiment: the experiment, for its status.

Every tool takes the Development environment's API key, which the agent reads through
MCP's list_environments, and builds its path with admin_path(), the one function that
makes paths. It builds only the paths its tools use, five of Gate 1's six allows (no tool
reads exposures/), and Api.send sends only the GET and POST pairs in ADMIN_ROUTES: never
DELETE, PUT or PATCH, which Flagsmith's spec allows on the experiment's own path. So
nothing here can end, pause, roll out or rewrite an
experiment (Gate 1, rule 1(c); CLAUDE.md).

Two ways in (both answer {"ok": true, "result": ...} or {"ok": false, "error": ...}):
- through the Gateway target flagsmith-admin: the event is the tool's arguments, and the
  tool's name arrives as context.client_context.custom["bedrockAgentCoreToolName"] in the
  form flagsmith-admin___<tool> (docs.aws.amazon.com/bedrock-agentcore/latest/devguide/
  gateway-add-target-lambda.html);
- by direct invoke, before task 2.7 adds that target: {"tool": <tool>, "arguments": {...}}.

The organisation key is read from Secrets Manager rm/flagsmith/api-key at run time, kept in
memory and sent only as the Authorization value names.api_key_authorization() builds. It is
never logged or returned.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from contracts import names

API_BASE_URL = "https://api.flagsmith.com"
# Each request's timeout. ensure_metrics makes the most requests, a search and a create for
# each of its three metrics: six at 4 s is 24 s, inside the function's
# names.FLAGSMITH_ADMIN_TIMEOUT_SECONDS (30 s) with room for the secret's first read.
REQUEST_TIMEOUT_SECONDS = 4
TOOL_SEPARATOR = "___"  # the Gateway names a target's tools <target>___<tool>
TOOL_NAME_FIELD = "bedrockAgentCoreToolName"

# The Admin API's spellings (task 1.3's evidence). c2 left them here, as module constants.
API_METRIC_DEFINITION_VERSION = 1  # a metric's definition is {"version": 1, "event": ...}
API_CREATED = 201  # experiment-metrics/ POST
API_STARTED = 200  # start/
API_REFRESH_ACCEPTED = 202  # results/refresh/
API_REFRESH_TOO_SOON = 429  # "A refresh was requested recently. Try again later."
STATUS_CREATED = "created"
STATUS_RUNNING = "running"

# The only paths admin_path() can build (Gate 1, rule 1(c)).
PATH_METRICS = "experiment-metrics"  # experiment-metrics/, and its ?q= search
PATH_EXPERIMENT = "experiment"  # the experiment read
PATH_START = "start"
PATH_RESULTS = "results"
PATH_REFRESH = "results-refresh"
_EXPERIMENT_SUFFIXES = {
    PATH_EXPERIMENT: "",
    PATH_START: "start/",
    PATH_RESULTS: "results/",
    PATH_REFRESH: "results/refresh/",
}
PATH_KINDS = (PATH_METRICS, *_EXPERIMENT_SUFFIXES)
# names.FLAGSMITH_ADMIN_FORBIDDEN_PATHS are the end, pause and rollout endpoints of
# Flagsmith's published spec: admin_path() has no kind for any, and guards every path it
# returns against them. The spec's DELETE, PUT and PATCH on the experiment read's own path
# delete it or rewrite its rollout, metrics and feature, so the boundary is methods
# (names.FLAGSMITH_ADMIN_METHODS) and paths together. The only (method, path) pairs this
# function sends: reads are GET; the metric create, the start and the results refresh are
# POST.
ADMIN_ROUTES = frozenset(
    {
        ("GET", PATH_METRICS),
        ("POST", PATH_METRICS),
        ("GET", PATH_EXPERIMENT),
        ("POST", PATH_START),
        ("GET", PATH_RESULTS),
        ("POST", PATH_REFRESH),
    }
)

_ENVIRONMENT_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_METRIC_QUERY = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Refused(Exception):
    """This function will not do it; nothing was sent."""


class Failed(Exception):
    """Flagsmith answered something this function cannot go on from."""


def admin_path(
    kind: str,
    environment_key: str,
    experiment_id: int | None = None,
    query: str | None = None,
) -> str:
    """The Admin API path of one of PATH_KINDS, and nothing else.

    Refuses any other kind, a malformed environment key, an experiment ID that is not a
    positive int, and a query anywhere but the metrics search.
    """
    if not isinstance(environment_key, str) or not _ENVIRONMENT_KEY.fullmatch(environment_key):
        raise Refused("the environment key is missing or malformed")
    base = f"/api/v1/environments/{environment_key}/"
    if kind == PATH_METRICS:
        if experiment_id is not None:
            raise Refused("the metrics path takes no experiment ID")
        path = base + "experiment-metrics/"
        if query is not None:
            if not isinstance(query, str) or not _METRIC_QUERY.fullmatch(query):
                raise Refused(f"a metric search must be a metric name, not {query!r}")
            path = f"{path}?q={urllib.parse.quote(query)}"
    else:
        if kind not in _EXPERIMENT_SUFFIXES:
            raise Refused(f"no path {kind!r}; this function builds only {', '.join(PATH_KINDS)}")
        if query is not None:
            raise Refused(f"the {kind} path takes no query")
        if (
            isinstance(experiment_id, bool)
            or not isinstance(experiment_id, int)
            or experiment_id < 1
        ):
            raise Refused(f"an experiment ID must be a positive integer, not {experiment_id!r}")
        path = f"{base}experiments/{experiment_id}/{_EXPERIMENT_SUFFIXES[kind]}"
    if any(forbidden in path for forbidden in names.FLAGSMITH_ADMIN_FORBIDDEN_PATHS):
        raise Refused("no path that ends, pauses or rolls out an experiment")
    return path


# An HTTP sender: (method, path, body) -> (status, parsed JSON or None).
Http = Callable[[str, str, Any], tuple[int, Any]]


class Api:
    """The Admin API, reachable only through ADMIN_ROUTES and admin_path()."""

    def __init__(self, http: Http, environment_key: Any) -> None:
        self._http = http
        self._environment_key = environment_key

    def send(
        self,
        method: str,
        kind: str,
        *,
        experiment_id: int | None = None,
        query: str | None = None,
        body: Any = None,
    ) -> tuple[int, Any]:
        if (method, kind) not in ADMIN_ROUTES:
            raise Refused(f"{method} on the {kind} path is not a call this function makes")
        path = admin_path(kind, self._environment_key, experiment_id, query)
        return self._http(method, path, body)


def _results(body: Any) -> list:
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        return body["results"]
    return body if isinstance(body, list) else []


def _expected(status: int, want: int, what: str) -> None:
    if status != want:
        raise Failed(f"{what} answered HTTP {status}, not {want}")


def ensure_metrics(api: Api) -> dict:
    """Get or create helpful_rate, mean_latency_ms and mean_cost_micro_usd."""
    metric_ids: dict[str, int] = {}
    created: list[str] = []
    for name, definition in names.METRIC_DEFINITIONS.items():
        want = {
            "aggregation": definition["aggregation"],
            "direction": names.METRIC_DIRECTION_API[definition["direction"]],
            "definition": {"version": API_METRIC_DEFINITION_VERSION, "event": definition["event"]},
        }
        status, body = api.send("GET", PATH_METRICS, query=name)
        _expected(status, 200, f"the search for metric {name}")
        found = [metric for metric in _results(body) if metric.get("name") == name]
        if found:
            metric = found[0]
            problems = [
                f"{field} {metric.get(field)!r} (want {value!r})"
                for field, value in want.items()
                if field != "definition" and metric.get(field) != value
            ]
            event = (metric.get("definition") or {}).get("event")
            if event != definition["event"]:
                problems.append(f"event {event!r} (want {definition['event']!r})")
            if problems:
                raise Failed(
                    f"metric {name} exists but differs: {'; '.join(problems)}. Fix it in the "
                    "Flagsmith dashboard; nothing was changed"
                )
        else:
            status, metric = api.send(
                "POST",
                PATH_METRICS,
                body={
                    "name": name,
                    "description": f"Rollout Marshal: {definition['aggregation']} of "
                    f"{definition['event']}, {definition['direction']}.",
                    **want,
                },
            )
            _expected(status, API_CREATED, f"creating metric {name}")
            created.append(name)
        metric_ids[name] = int(metric["id"])
    return {"metric_ids": metric_ids, "created": created}


def _experiment(api: Api, experiment_id: Any) -> dict:
    status, body = api.send("GET", PATH_EXPERIMENT, experiment_id=experiment_id)
    _expected(status, 200, f"reading experiment {experiment_id}")
    if not isinstance(body, dict):
        raise Failed(f"experiment {experiment_id} read back as {type(body).__name__}")
    return body


def _feature_name(experiment: dict) -> Any:
    feature = experiment.get("feature")
    return feature.get("name") if isinstance(feature, dict) else None


def start_experiment(api: Api, experiment_id: Any) -> dict:
    """Start an experiment that has not started, on a run's feature (assistant_model_<run_id>).

    A running one is left as it is, so a repeat is safe. Any other status is refused: this
    function never pauses, resumes, completes or ends an experiment.
    """
    experiment = _experiment(api, experiment_id)
    feature = _feature_name(experiment)
    prefix = names.EXPERIMENT_FEATURE_PREFIX
    on_a_run = (
        isinstance(feature, str)
        and feature.startswith(prefix)
        and names.is_run_id(feature[len(prefix) :])
    )
    if not on_a_run:
        raise Refused(f"experiment {experiment_id} is on feature {feature!r}, not a run's feature")
    status = experiment.get("status")
    if status == STATUS_RUNNING:
        return {"started": False, "experiment": experiment}
    if status != STATUS_CREATED:
        raise Refused(f"experiment {experiment_id} is {status!r}; only a created one is started")
    # Flagsmith's published spec requires name and hypothesis in start's body (task 1.3).
    status_code, started = api.send(
        "POST",
        PATH_START,
        experiment_id=experiment_id,
        body={"name": experiment.get("name"), "hypothesis": experiment.get("hypothesis")},
    )
    _expected(status_code, API_STARTED, f"starting experiment {experiment_id}")
    return {"started": True, "experiment": started}


def refresh_results(api: Api, experiment_id: Any) -> dict:
    status, body = api.send("POST", PATH_REFRESH, experiment_id=experiment_id)
    if status == API_REFRESH_TOO_SOON:
        detail = body.get("detail") if isinstance(body, dict) else None
        return {"refreshed": False, "detail": detail}
    _expected(status, API_REFRESH_ACCEPTED, f"refreshing results of experiment {experiment_id}")
    return {"refreshed": True}


def get_results(api: Api, experiment_id: Any) -> dict:
    """The results envelope. Before Flagsmith first computes the results it answers
    {"results": null} (task 1.3 read that through MCP on 819, before its first refresh).
    That is {} here: the agent finds it not current, or not ready, never a failure."""
    status, body = api.send("GET", PATH_RESULTS, experiment_id=experiment_id)
    _expected(status, 200, f"reading results of experiment {experiment_id}")
    if isinstance(body, dict) and "results" in body and body["results"] is None:
        return {}
    envelope = body.get("results") if isinstance(body, dict) else None
    if not isinstance(envelope, dict):
        raise Failed(f"experiment {experiment_id}'s results have no results object")
    return envelope


def get_experiment(api: Api, experiment_id: Any) -> dict:
    return _experiment(api, experiment_id)


TOOLS: dict[str, Callable[..., dict]] = {
    "ensure_metrics": lambda api, arguments: ensure_metrics(api),
    "start_experiment": lambda api, arguments: start_experiment(
        api, arguments.get("experiment_id")
    ),
    "refresh_results": lambda api, arguments: refresh_results(api, arguments.get("experiment_id")),
    "get_results": lambda api, arguments: get_results(api, arguments.get("experiment_id")),
    "get_experiment": lambda api, arguments: get_experiment(api, arguments.get("experiment_id")),
}
# One line each, for the Gateway target's tool definitions, where a description is
# required (docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/
# API_ToolDefinition.html).
TOOL_DESCRIPTIONS = {
    "ensure_metrics": (
        "Get or create the three Rollout Marshal metrics; a found one that differs is an error."
    ),
    "start_experiment": (
        "Start a created experiment on a run's feature; a running one is left as it is."
    ),
    "refresh_results": (
        "Ask Flagsmith to recompute an experiment's results, at most once per 5 minutes."
    ),
    "get_results": "Read an experiment's results envelope, or {} before the first one.",
    "get_experiment": "Read an experiment, with its status.",
}
# Every tool takes the environment key; all but ensure_metrics take an experiment ID.
TOOL_ARGUMENTS = {
    tool: ("environment_api_key",)
    if tool == "ensure_metrics"
    else ("environment_api_key", "experiment_id")
    for tool in TOOLS
}


# ---------------------------------------------------------------------------
# The key and the HTTP sender
# ---------------------------------------------------------------------------
_key_cache: dict[str, str] = {}


def _organisation_key(secrets: Any = None) -> str:
    """The organisation key, read once per container and kept in memory."""
    if "key" not in _key_cache:
        if secrets is None:
            import boto3

            secrets = boto3.client("secretsmanager", region_name=names.REGION)
        value = secrets.get_secret_value(SecretId=names.SECRET_FLAGSMITH_API_KEY).get(
            "SecretString"
        )
        if not isinstance(value, str) or not value or value != value.strip():
            raise Failed(f"secret {names.SECRET_FLAGSMITH_API_KEY} is empty or not a bare key")
        _key_cache["key"] = value
    return _key_cache["key"]


def urllib_http(authorization: str) -> Http:
    """An Http that sends JSON to API_BASE_URL with the given Authorization value. It sends
    GET and POST only, whatever asks it."""

    def send(method: str, path: str, body: Any) -> tuple[int, Any]:
        if method not in names.FLAGSMITH_ADMIN_METHODS:
            raise Refused(f"{method} is never sent")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            API_BASE_URL + path,
            data=data,
            method=method,
            headers={
                names.GATEWAY_API_KEY_HEADER: authorization,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
                status, raw = reply.status, reply.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, parsed

    return send


# ---------------------------------------------------------------------------
# The Lambda entry point
# ---------------------------------------------------------------------------
def _call(event: Any, context: Any) -> tuple[str, dict]:
    """(tool, arguments) from a Gateway call or a direct invoke."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    gateway_name = custom.get(TOOL_NAME_FIELD) if isinstance(custom, dict) else None
    if gateway_name is not None:
        target, separator, tool = str(gateway_name).partition(TOOL_SEPARATOR)
        target_name = names.GATEWAY_TARGET_FLAGSMITH_ADMIN
        if not separator or target != target_name:
            raise Refused(f"tool {gateway_name!r} is not one of target {target_name}'s")
        arguments = event
    else:
        if not isinstance(event, dict):
            raise Refused('a direct invoke takes {"tool": ..., "arguments": {...}}')
        tool, arguments = event.get("tool"), event.get("arguments")
    if tool not in TOOLS:
        raise Refused(f"no tool {tool!r}; the tools are {', '.join(TOOLS)}")
    if not isinstance(arguments, dict):
        raise Refused(f"{tool} takes an object of arguments")
    unknown = sorted(set(arguments) - set(TOOL_ARGUMENTS[tool]))
    missing = sorted(set(TOOL_ARGUMENTS[tool]) - set(arguments))
    if unknown or missing:
        raise Refused(
            f"{tool} takes {list(TOOL_ARGUMENTS[tool])}; unknown {unknown}, missing {missing}"
        )
    return tool, arguments


def handle(event: Any, context: Any, *, http: Http | None = None) -> dict:
    """Run one tool. Always answers; never raises, never returns the key."""
    try:
        tool, arguments = _call(event, context)
        if http is None:
            http = urllib_http(names.api_key_authorization(_organisation_key()))
        result = TOOLS[tool](Api(http, arguments["environment_api_key"]), arguments)
        return {"ok": True, "result": result}
    except Refused as error:
        return {"ok": False, "error": {"type": "refused", "message": str(error)}}
    except Failed as error:
        return {"ok": False, "error": {"type": "failed", "message": str(error)}}
    except Exception as error:  # noqa: BLE001 - the caller always gets an answer
        return {"ok": False, "error": {"type": "internal", "message": type(error).__name__}}


def handler(event: Any, context: Any) -> dict:
    return handle(event, context)
