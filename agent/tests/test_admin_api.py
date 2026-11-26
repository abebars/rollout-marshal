"""Unit tests for agent/spike/admin_api.py (task 1.3): httpx.MockTransport, no network."""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from agent.spike import admin_api
from contracts import names

ORG_KEY = "OrgKeyThatMustNeverPrint0123456789"
ENV_KEY = "EnvKeyThatMustNeverPrint123"
SPIKE_KEY = "assistant_model_rspike2215"
EXPERIMENT_ID = 903
FEATURE_ID = 900
HYPOTHESIS = "cheaper does not lower spike_mcp_metric"


class Recorder:
    def __init__(self, feature_name=SPIKE_KEY, status="created"):
        self.feature_name = feature_name
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/environments/" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "name": "Production", "api_key": "OtherEnv"},
                        {"id": 70001, "name": names.FLAGSMITH_ENVIRONMENT_NAME, "api_key": ENV_KEY},
                    ]
                },
            )
        base = f"/api/v1/environments/{ENV_KEY}/experiments/{EXPERIMENT_ID}/"
        if path == base and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": EXPERIMENT_ID,
                    "feature": FEATURE_ID,
                    "name": SPIKE_KEY,
                    "hypothesis": HYPOTHESIS,
                    "status": self.status,
                    "env": ENV_KEY,
                },
            )
        if path == f"/api/v1/projects/{names.FLAGSMITH_PROJECT_ID}/features/{FEATURE_ID}/":
            return httpx.Response(200, json={"id": FEATURE_ID, "name": self.feature_name})
        if path == base + "start/" and request.method == "POST":
            # The published OpenAPI spec requires name and hypothesis in start's body.
            body = json.loads(request.content or b"{}")
            if body != {"name": SPIKE_KEY, "hypothesis": HYPOTHESIS}:
                return httpx.Response(400, json={"name": ["This field is required."]})
            self.status = "running"
            return httpx.Response(200, json={"id": EXPERIMENT_ID, "status": "running"})
        if path in (base + "results/refresh/", base + "exposures/refresh/"):
            return httpx.Response(202)
        if path in (base + "results/", base + "exposures/"):
            return httpx.Response(200, json={"results": None})
        if path.startswith("/api/v1/organisations/"):
            return httpx.Response(200, json={"total": 1234})
        return httpx.Response(404, json={"detail": "not stubbed", "path": path})


def _api(recorder, lines):
    client = httpx.Client(
        base_url=admin_api.API_BASE_URL,
        transport=httpx.MockTransport(recorder),
        headers={names.GATEWAY_API_KEY_HEADER: names.GATEWAY_API_KEY_PREFIX + ORG_KEY},
    )
    return admin_api.AdminApi(client, out=lines.append, secrets={ORG_KEY})


def test_start_posts_to_the_documented_path_for_a_spike_experiment():
    recorder, lines = Recorder(), []
    result = _api(recorder, lines).start(EXPERIMENT_ID)
    posts = [r for r in recorder.requests if r.method == "POST"]
    assert [r.url.path for r in posts] == [
        f"/api/v1/environments/{ENV_KEY}/experiments/{EXPERIMENT_ID}/start/"
    ]
    assert json.loads(posts[0].content) == {"name": SPIKE_KEY, "hypothesis": HYPOTHESIS}
    assert result["start"]["status"] == 200
    assert result["after"]["status"] == "running"
    header = posts[0].headers[names.GATEWAY_API_KEY_HEADER]
    assert header == names.GATEWAY_API_KEY_PREFIX + ORG_KEY


@pytest.mark.parametrize(
    "feature_name",
    [
        names.KILL_SWITCH_FEATURE,
        "spike_flag",
        "assistant_model_r202611271530",
        "assistant_model_rsmoke2215",
    ],
)
def test_start_refuses_any_experiment_whose_feature_is_not_a_spike_key(feature_name):
    recorder, lines = Recorder(feature_name=feature_name), []
    with pytest.raises(admin_api.Refused):
        _api(recorder, lines).start(EXPERIMENT_ID)
    assert [r for r in recorder.requests if r.method != "GET"] == []


def test_start_refuses_an_experiment_that_is_not_waiting_to_start():
    recorder, lines = Recorder(status="running"), []
    with pytest.raises(admin_api.Refused):
        _api(recorder, lines).start(EXPERIMENT_ID)
    assert [r for r in recorder.requests if r.method != "GET"] == []


def test_reads_and_refreshes_use_the_documented_paths():
    recorder, lines = Recorder(), []
    api = _api(recorder, lines)
    api.refresh_results(EXPERIMENT_ID)
    api.results(EXPERIMENT_ID)
    api.exposures(EXPERIMENT_ID)
    api.status(EXPERIMENT_ID)
    base = f"/api/v1/environments/{ENV_KEY}/experiments/{EXPERIMENT_ID}/"
    seen = [(r.method, r.url.path) for r in recorder.requests if "experiments" in r.url.path]
    assert seen == [
        ("POST", base + "results/refresh/"),
        ("GET", base + "results/"),
        ("GET", base + "exposures/"),
        ("GET", base),
    ]


def test_printed_output_never_holds_either_key():
    recorder, lines = Recorder(), []
    api = _api(recorder, lines)
    api.start(EXPERIMENT_ID)
    api.results(EXPERIMENT_ID)
    api.get(f"/api/v1/organisations/{names.FLAGSMITH_ORGANISATION_ID}/usage-data/")
    text = "\n".join(lines)
    assert ENV_KEY not in text
    assert ORG_KEY not in text
    assert "OtherEnv" not in text
    assert "<environment key>" in text


@pytest.mark.parametrize(
    "path", ["https://example.com/api/v1/x", "/admin/", "/api/v1/../x", "api/v1/organisations/"]
)
def test_get_reads_only_under_api_v1(path):
    recorder, lines = Recorder(), []
    with pytest.raises(admin_api.Refused):
        _api(recorder, lines).get(path)
    assert recorder.requests == []


def test_the_script_has_no_way_to_end_or_pause_an_experiment():
    source = Path(admin_api.__file__).read_text()
    assert not re.search(r"/(complete|pause|end|stop)/", source)
    assert not re.search(r"def (complete|pause|end|stop|finish)", source)


def test_usable_key_rejects_shapes_without_echoing_them():
    for value in ("  ", '{"k": "v"}', "two words", "Api-Key abc"):
        with pytest.raises(admin_api.Refused) as raised:
            admin_api.usable_key(value)
        if value.strip():
            assert value not in str(raised.value)
    assert admin_api.usable_key("abc.def") == "abc.def"


@pytest.mark.parametrize(
    "key",
    [
        SPIKE_KEY,
        "assistant_model_rspike0000",
        "assistant_model_rspike",
        "assistant_model_rspike22150",
        "assistant_model_rsmoke2215",
        names.KILL_SWITCH_FEATURE,
        "spike_flag",
    ],
)
def test_spike_key_rule_matches_the_agents(key):
    from agent.spike import agent as spike

    assert bool(admin_api.SPIKE_FEATURE_KEY.fullmatch(key)) == spike.is_spike_feature_key(key)


def test_json_bodies_are_printed_redacted():
    recorder, lines = Recorder(), []
    _api(recorder, lines).status(EXPERIMENT_ID)
    printed = json.loads(lines[-1])
    assert printed["status"] == 200
    assert printed["body"]["env"] == admin_api.REDACTED_ENV
