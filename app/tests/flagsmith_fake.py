"""An in-memory Flagsmith Admin API for the scripts' tests (task 3.5).

It answers the REST paths the scripts use, as Flagsmith's published OpenAPI spec spells
them and as task 1.3 saw them behave: v2 versions copy the live states and change nothing
until published, an experiment needs its rollout block, and a version state's update needs
its value. Every request is recorded as (method, path, body), with the query string kept.
"""

from __future__ import annotations

import copy
import itertools
import re
import uuid as uuidlib
from urllib.parse import parse_qs, urlsplit

from contracts import names

DEV_ID, PROD_ID = 119136, 119137
DEV_KEY, PROD_KEY = "devEnvKeyForTests123", "prodEnvKeyForTests456"
P = f"/api/v1/projects/{names.FLAGSMITH_PROJECT_ID}"


class FakeFlagsmith:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.headers: list[dict] = []
        self.features: dict[int, dict] = {}
        self.options: dict[int, list[dict]] = {}
        self.versions: dict[str, dict] = {}
        self.metrics: list[dict] = []
        self.experiments: list[dict] = []
        self.fail: dict[tuple[str, str], int] = {}  # (method, path regex) -> status
        self._ids = itertools.count(306400)

    # -- setup helpers ----------------------------------------------------------------
    def add_feature(self, name, feature_type="STANDARD", *, enabled=True, value=None) -> dict:
        feature = {
            "id": next(self._ids),
            "name": name,
            "type": feature_type,
            "environment_feature_state": {
                "enabled": enabled,
                "feature_state_value": {"type": "unicode", "string_value": value},
            },
        }
        self.features[feature["id"]] = feature
        self.options[feature["id"]] = []
        return feature

    def feature(self, name) -> dict | None:
        found = [f for f in self.features.values() if f["name"] == name]
        return found[0] if found else None

    def state(self, name) -> tuple:
        state = self.feature(name)["environment_feature_state"]
        return state["enabled"], state["feature_state_value"]["string_value"]

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests if m != "GET"]

    # -- the transport ------------------------------------------------------------------
    def __call__(self, method: str, url: str, headers: dict, body: object):
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "api.flagsmith.com", url
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self.requests.append((method, path, copy.deepcopy(body)))
        self.headers.append(dict(headers))
        for (fail_method, pattern), status in self.fail.items():
            if fail_method == method and re.search(pattern, path):
                return status, {"detail": "failed for the test"}
        return self._route(method, parts.path, parse_qs(parts.query), body)

    def _route(self, method, path, query, body):
        if method == "GET" and path == "/api/v1/environments/":
            assert query == {"project": [str(names.FLAGSMITH_PROJECT_ID)]}
            return 200, {
                "results": [
                    {"id": DEV_ID, "name": "Development", "api_key": DEV_KEY},
                    {"id": PROD_ID, "name": "Production", "api_key": PROD_KEY},
                ]
            }
        if path == f"{P}/features/":
            if method == "GET":
                assert query["environment"] == [str(DEV_ID)]
                search = query["search"][0]
                found = [f for f in self.features.values() if search in f["name"]]
                return 200, {"results": copy.deepcopy(found)}
            if method == "POST":
                feature = self.add_feature(
                    body["name"],
                    body["type"],
                    enabled=body.get("default_enabled", False),
                    value=body.get("initial_value"),
                )
                return 201, copy.deepcopy(feature)
        match = re.fullmatch(rf"{P}/features/(\d+)/mv-options/", path)
        if match and method == "POST":
            option = {"id": next(self._ids), **body}
            self.options[int(match[1])].append(option)
            return 201, option
        match = re.fullmatch(rf"/api/v1/environments/{DEV_ID}/features/(\d+)/versions/(.*)", path)
        if match:
            return self._versions(method, int(match[1]), match[2], body)
        match = re.fullmatch(rf"/api/v1/environments/{DEV_KEY}/(.+)", path)
        if match:
            return self._environment(method, match[1], query, body)
        return 404, {"detail": "not found"}

    def _versions(self, method, feature_id, rest, body):
        feature = self.features[feature_id]
        if rest == "" and method == "POST":
            live = feature["environment_feature_state"]
            version = {
                "uuid": str(uuidlib.uuid4()),
                "feature": feature_id,
                "published": False,
                "states": [
                    {
                        "id": next(self._ids),
                        "feature_segment": None,
                        "identity": None,
                        "enabled": live["enabled"],
                        "feature_state_value": copy.deepcopy(live["feature_state_value"]),
                    }
                ],
            }
            self.versions[version["uuid"]] = version
            return 201, {"uuid": version["uuid"], "published": False, "live_from": None}
        match = re.fullmatch(r"([0-9a-f-]+)/featurestates/", rest)
        if match and method == "GET":
            return 200, copy.deepcopy(self.versions[match[1]]["states"])
        match = re.fullmatch(r"([0-9a-f-]+)/featurestates/(\d+)/", rest)
        if match and method == "PUT":
            if "feature_state_value" not in body:
                return 400, {"feature_state_value": ["This field is required."]}
            [state] = [s for s in self.versions[match[1]]["states"] if s["id"] == int(match[2])]
            state.update(enabled=body["enabled"], feature_state_value=body["feature_state_value"])
            return 200, copy.deepcopy(state)
        match = re.fullmatch(r"([0-9a-f-]+)/publish/", rest)
        if match and method == "POST":
            version = self.versions[match[1]]
            [state] = version["states"]
            feature["environment_feature_state"] = {
                "enabled": state["enabled"],
                "feature_state_value": copy.deepcopy(state["feature_state_value"]),
            }
            version["published"] = True
            # As task 3.5's live run saw it: live_from, and no uuid.
            return 200, {"published": True, "live_from": "2026-11-27T05:00:00Z"}
        return 404, {"detail": "not found"}

    def _environment(self, method, rest, query, body):
        if rest == "experiment-metrics/":
            if method == "GET":
                found = [m for m in self.metrics if query["q"][0] in m["name"]]
                return 200, {"results": copy.deepcopy(found)}
            if method == "POST":
                metric = {"id": next(self._ids), **copy.deepcopy(body)}
                self.metrics.append(metric)
                return 201, copy.deepcopy(metric)
        if rest == "experiments/":
            if method == "GET":
                status = query.get("status", [None])[0]
                found = [e for e in self.experiments if status in (None, e["status"])]
                return 200, {"results": copy.deepcopy(found)}
            if method == "POST":
                if "experiment_rollout" not in body:
                    return 400, {"experiment_rollout": ["This field is required."]}
                experiment = {"id": next(self._ids), "status": "created", **copy.deepcopy(body)}
                self.experiments.append(experiment)
                return 201, copy.deepcopy(experiment)
        match = re.fullmatch(r"experiments/(\d+)/start/", rest)
        if match and method == "POST":
            [experiment] = [e for e in self.experiments if e["id"] == int(match[1])]
            experiment["status"] = "running"
            return 200, copy.deepcopy(experiment)
        return 404, {"detail": "not found"}
