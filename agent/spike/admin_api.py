"""Task 1.3's local script for the Flagsmith Admin API: start the spike experiment, read it.

It starts only an experiment that has not started yet and whose feature is a spike key,
``assistant_model_rspike<HHMM>`` (CLAUDE.md). It has no way to end or pause an
experiment: the human ends every spike experiment in the Flagsmith dashboard.

The organisation key is read from Secrets Manager ``rm/flagsmith/api-key`` with the
human's credentials, kept in memory, and sent only in the Authorization header. Every
printed request and reply has the organisation key and every environment key redacted.

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=abisb_IsbAdminsPS-818928662830
    uv run python agent/spike/admin_api.py start <experiment_id>
    uv run python agent/spike/admin_api.py status|results|exposures <experiment_id>
    uv run python agent/spike/admin_api.py refresh|refresh-exposures <experiment_id>
    uv run python agent/spike/admin_api.py get /api/v1/<path>     # any read, e.g. usage
    uv run python agent/spike/admin_api.py swagger [word]         # public OpenAPI, no key sent

Paths: start, results refresh and results from docs.flagsmith.com/experimentation/
set-up-with-a-coding-agent; exposures from the Flagsmith MCP server's OpenAPI
(get_experiment_exposures); exposures refresh only from the API source on GitHub
(api/experimentation/views.py), so it is unverified until phase 2 runs it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from contracts import names

API_BASE_URL = "https://api.flagsmith.com"
SWAGGER_URL = (
    API_BASE_URL + "/api/v1/swagger.json"
)  # linked from docs.flagsmith.com Management API page
REDACTED = "<redacted>"
REDACTED_ENV = "<environment key>"
NOT_STARTED = frozenset({"created", "draft"})
# Field names whose string values are environment keys. An option's "key" (cheaper) is not
# a secret and stays visible; the organisation key is replaced wherever its text appears.
SECRET_FIELDS = frozenset({"api_key", "environment_api_key"})
TIMEOUT_SECONDS = 30

# The same rule as agent/spike/agent.py's is_spike_feature_key; a unit test checks they agree.
SPIKE_FEATURE_KEY = re.compile(
    re.escape(names.EXPERIMENT_FEATURE_PREFIX + names.RUN_ID_PREFIX + "spike") + r"[0-9]{4}"
)


class Refused(RuntimeError):
    """The script will not do this. Nothing was sent."""


def usable_key(value: Any) -> str:
    """The organisation key as stored, after a shape check that never repeats it."""
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"secret {names.SECRET_FLAGSMITH_API_KEY} is empty")
    if value.lstrip().startswith(("{", "[")):
        raise Refused(f"secret {names.SECRET_FLAGSMITH_API_KEY} looks like JSON, not a bare key")
    if any(ch.isspace() for ch in value):
        raise Refused(
            f"secret {names.SECRET_FLAGSMITH_API_KEY} contains whitespace (an Api-Key prefix?); "
            "it must hold the bare key"
        )
    return value


class AdminApi:
    def __init__(
        self,
        client: httpx.Client,
        *,
        out: Callable[[str], None] = print,
        secrets: Iterable[str] = (),
    ) -> None:
        self.client = client
        self.out = out
        self.secrets = {s for s in secrets if s}
        self.environment_keys: set[str] = set()
        self._development: dict | None = None

    # -- printing ---------------------------------------------------------------

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: REDACTED if k in SECRET_FIELDS and isinstance(v, str) and v else self.redact(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for key in self.environment_keys:
                value = value.replace(key, REDACTED_ENV)
            for secret in self.secrets:
                value = value.replace(secret, REDACTED)
        return value

    def send(self, method: str, path: str, body: Any = None) -> dict:
        response = self.client.request(method, path, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:2000] or None
        self.out(
            json.dumps(
                {
                    "request": self.redact(f"{method} {path}"),
                    "status": response.status_code,
                    "body": self.redact(payload),
                },
                default=str,
            )
        )
        return {"status": response.status_code, "body": payload}

    # -- lookups ----------------------------------------------------------------

    def development(self) -> dict:
        if self._development is None:
            reply = self.send("GET", f"/api/v1/environments/?project={names.FLAGSMITH_PROJECT_ID}")
            listed = reply["body"].get("results", []) if isinstance(reply["body"], dict) else []
            for environment in listed:
                if environment.get("api_key"):
                    self.environment_keys.add(environment["api_key"])
            found = [e for e in listed if e.get("name") == names.FLAGSMITH_ENVIRONMENT_NAME]
            if reply["status"] != 200 or len(found) != 1:
                raise Refused(f"could not find environment {names.FLAGSMITH_ENVIRONMENT_NAME}")
            self._development = {"id": found[0]["id"], "key": found[0]["api_key"]}
        return self._development

    def experiment_path(self, experiment_id: int, suffix: str = "") -> str:
        key = self.development()["key"]
        return f"/api/v1/environments/{key}/experiments/{int(experiment_id)}/{suffix}"

    # -- commands ---------------------------------------------------------------

    def status(self, experiment_id: int) -> dict:
        return self.send("GET", self.experiment_path(experiment_id))

    def start(self, experiment_id: int) -> dict:
        """Start one spike experiment, after checking its feature and that it has not started."""
        before = self.status(experiment_id)
        experiment = before["body"] if isinstance(before["body"], dict) else {}
        if before["status"] != 200:
            raise Refused(f"experiment {experiment_id} could not be read")
        status = str(experiment.get("status", "")).lower()
        if status not in NOT_STARTED:
            raise Refused(f"experiment {experiment_id} is {status!r}; only a new one is started")
        feature_id = experiment.get("feature")
        feature_id = feature_id.get("id") if isinstance(feature_id, dict) else feature_id
        feature = self.send(
            "GET", f"/api/v1/projects/{names.FLAGSMITH_PROJECT_ID}/features/{int(feature_id)}/"
        )
        name = feature["body"].get("name") if isinstance(feature["body"], dict) else None
        if not (isinstance(name, str) and SPIKE_FEATURE_KEY.fullmatch(name)):
            raise Refused(
                f"experiment {experiment_id} is on feature {name!r}; this script starts only "
                f"{names.EXPERIMENT_FEATURE_PREFIX}{names.RUN_ID_PREFIX}spike<HHMM> experiments"
            )
        started = self.send("POST", self.experiment_path(experiment_id, "start/"))
        after = self.status(experiment_id)
        return {"feature": name, "start": started, "after": after["body"]}

    def refresh_results(self, experiment_id: int) -> dict:
        return self.send("POST", self.experiment_path(experiment_id, "results/refresh/"))

    def results(self, experiment_id: int) -> dict:
        return self.send("GET", self.experiment_path(experiment_id, "results/"))

    def refresh_exposures(self, experiment_id: int) -> dict:
        return self.send("POST", self.experiment_path(experiment_id, "exposures/refresh/"))

    def exposures(self, experiment_id: int) -> dict:
        return self.send("GET", self.experiment_path(experiment_id, "exposures/"))

    def get(self, path: str) -> dict:
        """Any read under /api/v1/, for the usage endpoint and other lookups."""
        if not path.startswith("/api/v1/") or ".." in path or "://" in path:
            raise Refused(f"get reads only paths under /api/v1/, not {path!r}")
        return self.send("GET", path)


def swagger(word: str | None, out: Callable[[str], None] = print) -> None:
    """Print the public OpenAPI document's paths, optionally those containing word. No key sent."""
    response = httpx.get(SWAGGER_URL, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    for path, item in sorted(response.json().get("paths", {}).items()):
        if word is None or word in path:
            out(f"{','.join(sorted(m.upper() for m in item if m != 'parameters'))} {path}")


def _client() -> tuple[httpx.Client, str]:
    import boto3

    session = boto3.Session(region_name=names.REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != names.ACCOUNT_ID:
        raise Refused(f"caller account is {account}; this script runs only in {names.ACCOUNT_ID}")
    secret = session.client("secretsmanager").get_secret_value(
        SecretId=names.SECRET_FLAGSMITH_API_KEY
    )
    key = usable_key(secret.get("SecretString"))
    client = httpx.Client(
        base_url=API_BASE_URL,
        headers={names.GATEWAY_API_KEY_HEADER: names.GATEWAY_API_KEY_PREFIX + key},
        timeout=TIMEOUT_SECONDS,
    )
    return client, key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "status", "results", "exposures", "refresh", "refresh-exposures"):
        sub.add_parser(command).add_argument("experiment_id", type=int)
    sub.add_parser("get").add_argument("path")
    sub.add_parser("swagger").add_argument("word", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "swagger":
            swagger(args.word)
            return 0
        client, key = _client()
        with client:
            api = AdminApi(client, secrets={key})
            del key
            action = {
                "start": api.start,
                "status": api.status,
                "results": api.results,
                "exposures": api.exposures,
                "refresh": api.refresh_results,
                "refresh-exposures": api.refresh_exposures,
            }.get(args.command)
            if action is not None:
                action(args.experiment_id)
            else:
                api.get(args.path)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
