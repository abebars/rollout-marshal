"""Get or create the two permanent Flagsmith features (task 3.5; design.md section 4).

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=<the human's profile in 818928662830>
    uv run python scripts/bootstrap_flagsmith.py
    uv run python scripts/bootstrap_flagsmith.py --v2-probe spike_<name>   # task 3.5's run

The features are assistant_cheaper_enabled (boolean kill switch, default enabled) and
assistant_default_model (string, current or cheaper, default current). Get-or-create checks
what it finds, as design.md section 4 says. Both must be standard features, not
multivariate, and the default model must hold current or cheaper in Development. Their
state is never compared with the defaults, because rollback and promote change it; the
defaults apply only on create. A mismatch stops with an error for the human to fix in the
dashboard. Nothing updates itself.

It is also the Admin API client that seed_experiment.py and reset.py share:
- Every call uses Flagsmith's REST Admin API, with the paths and bodies of its published
  OpenAPI spec (https://api.flagsmith.com/api/v1/swagger.json, linked from
  docs.flagsmith.com).
- The organisation key is read from Secrets Manager rm/flagsmith/api-key in-process, with
  the shell's credentials. It travels only in the Authorization header, built by
  names.api_key_authorization(). Nothing prints it, and every printed path has the
  environment key replaced.
- Development uses v2 feature versioning, so a state changes through a new version, never
  in place (task 1.3, through MCP). set_state() takes the same four steps over REST:
  1. create a version;
  2. read its states;
  3. update the environment default's state;
  4. publish the version.
  --v2-probe runs them once against a scratch spike_ feature (tasks.md 3.5).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # contracts and scripts, when run as a script

import boto3  # noqa: E402

from contracts import names  # noqa: E402

API_BASE_URL = "https://api.flagsmith.com"  # the Admin API (docs.flagsmith.com; task 1.3)
REQUEST_TIMEOUT_SECONDS = 30
SPIKE_PREFIX = "spike_"  # every scratch name in Flagsmith (design.md section 4)
REDACTED_ENVIRONMENT = "<environment key>"
STRING_VALUE_TYPE = names.API_VERSION_VALUE_TYPE  # a v2 state's string value: {"type": "unicode"}
EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 3

# A transport sends one request: (method, url, headers, body or None) -> (status, parsed body).
Transport = Callable[[str, str, dict, object], tuple[int, object]]


class FlagsmithError(RuntimeError):
    """The Admin API answered with an error, or what it holds is not what the contract says.
    The message never holds the organisation key or an environment key."""


class Refused(RuntimeError):
    """The script will not do this, and has written nothing for it."""


def urllib_transport(method: str, url: str, headers: dict, body: object) -> tuple[int, object]:
    """One HTTPS request with the standard library; the reply's JSON, or None."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            status, raw = reply.status, reply.read()
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read()
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def path_of(url: object) -> str | None:
    """A list reply's `next` link as a path under the Admin API, or None when there is no
    next page. A link to any other host is a FlagsmithError, not Refused: it comes after
    a script's writes (reset.py reads the list last), and Refused means nothing was
    written."""
    if not url:
        return None
    parts, base = urllib.parse.urlsplit(str(url)), urllib.parse.urlsplit(API_BASE_URL)
    if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
        raise FlagsmithError(f"a next page outside {API_BASE_URL}")
    return parts.path + (f"?{parts.query}" if parts.query else "")


def redact_path(path: str) -> str:
    """The path with any environment key replaced: /environments/<key>/ but not an ID."""
    return re.sub(r"/environments/(?!\d+/)[^/?]+", f"/environments/{REDACTED_ENVIRONMENT}", path)


class Admin:
    """Flagsmith's Admin API for one project and its Development environment."""

    def __init__(self, key: str, *, transport: Transport = urllib_transport, log=None) -> None:
        self._headers = {
            "Authorization": names.api_key_authorization(key),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._transport = transport
        self._log = log  # called with each request's method, redacted path and status
        self._development: dict | None = None

    def request(self, method: str, path: str, body: object = None) -> object:
        if method not in ("GET", "POST", "PUT"):
            raise Refused(f"the scripts never send {method}")
        if any(part in path for part in names.FLAGSMITH_ADMIN_FORBIDDEN_PATHS):
            raise Refused(f"the scripts never call {redact_path(path)}")
        status, reply = self._transport(method, API_BASE_URL + path, self._headers, body)
        if self._log is not None:
            self._log(method, redact_path(path), status)
        if status >= 400:
            raise FlagsmithError(f"{method} {redact_path(path)} answered {status}")
        return reply

    # -- the project and its environment ---------------------------------------------
    def development(self) -> dict:
        """Development's ID and environment key, read once."""
        if self._development is None:
            reply = self.request(
                "GET", f"/api/v1/environments/?project={names.FLAGSMITH_PROJECT_ID}"
            )
            listed = reply.get("results", []) if isinstance(reply, dict) else (reply or [])
            found = [e for e in listed if e.get("name") == names.FLAGSMITH_ENVIRONMENT_NAME]
            if len(found) != 1:
                raise FlagsmithError(
                    f"found {len(found)} environments named {names.FLAGSMITH_ENVIRONMENT_NAME}"
                )
            self._development = {"id": found[0]["id"], "key": found[0]["api_key"]}
        return self._development

    def features_path(self, suffix: str = "") -> str:
        return f"/api/v1/projects/{names.FLAGSMITH_PROJECT_ID}/features/{suffix}"

    def environment_path(self, suffix: str) -> str:
        return f"/api/v1/environments/{self.development()['key']}/{suffix}"

    # -- features ---------------------------------------------------------------------
    def find_feature(self, name: str) -> dict | None:
        """The feature named exactly name, with its live state in Development, or None."""
        environment = self.development()["id"]
        reply = self.request("GET", self.features_path(f"?search={name}&environment={environment}"))
        found = [f for f in (reply or {}).get("results", []) if f.get("name") == name]
        if len(found) > 1:
            raise FlagsmithError(f"found {len(found)} features named {name}")
        return found[0] if found else None

    def create_feature(self, name: str, feature_type: str, *, enabled: bool, value=None) -> dict:
        body = {"name": name, "type": feature_type, "default_enabled": enabled}
        if value is not None:
            body["initial_value"] = value
        return self.request("POST", self.features_path(), body)

    # -- v2 feature versioning ------------------------------------------------------------
    def set_state(
        self, feature: dict, *, enabled: bool | None = None, value: str | None = None
    ) -> dict:
        """Change the Development default state of feature through a new published version:
        enabled, and for a string feature its value. Returns the published version."""
        environment = self.development()["id"]
        versions = f"/api/v1/environments/{environment}/features/{feature['id']}/versions/"
        version = self.request("POST", versions, {})
        uuid = version["uuid"]
        states = self.request("GET", f"{versions}{uuid}/featurestates/")
        default = [s for s in states if s.get("feature_segment") is None and not s.get("identity")]
        if len(default) != 1:
            raise FlagsmithError(f"version of {feature['name']} has {len(default)} default states")
        state = default[0]
        state_value = state.get("feature_state_value")
        if value is not None:
            state_value = {"type": STRING_VALUE_TYPE, "string_value": value}
        body = {
            "feature": feature["id"],
            "enabled": state["enabled"] if enabled is None else enabled,
            "feature_state_value": state_value,
        }
        self.request("PUT", f"{versions}{uuid}/featurestates/{state['id']}/", body)
        published = self.request("POST", f"{versions}{uuid}/publish/", {})
        return {"uuid": uuid, **(published or {})}  # publish's reply has live_from, no uuid


def live_state(feature: dict) -> tuple[bool | None, object]:
    """A found feature's live Development state: (enabled, value)."""
    state = feature.get("environment_feature_state") or {}
    value = state.get("feature_state_value")
    if isinstance(value, dict):
        value = value.get("string_value", value.get("value"))
    return state.get("enabled"), value


def organisation_key(secrets=None) -> str:
    """The organisation API key, read in-process from Secrets Manager. Never printed."""
    if secrets is None:
        secrets = boto3.client("secretsmanager", region_name=names.REGION)
    key = secrets.get_secret_value(SecretId=names.SECRET_FLAGSMITH_API_KEY)["SecretString"]
    if not isinstance(key, str) or not key or any(c.isspace() for c in key) or key[0] in "{[":
        raise Refused(f"secret {names.SECRET_FLAGSMITH_API_KEY} does not hold a bare key")
    return key


def check_account(sts=None) -> None:
    """Refuse to run in any account but the project's (CLAUDE.md)."""
    if sts is None:
        sts = boto3.client("sts", region_name=names.REGION)
    account = sts.get_caller_identity()["Account"]
    if account != names.ACCOUNT_ID:
        raise Refused(f"these credentials are for account {account}, not {names.ACCOUNT_ID}")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def ensure_permanent_features(admin: Admin, out=print) -> dict[str, dict]:
    """Get or create both permanent features, checking what it finds."""
    features = {}
    for name in names.PERMANENT_FEATURES:
        definition = names.PERMANENT_FEATURE_DEFINITIONS[name]
        found = admin.find_feature(name)
        if found is None:
            value = definition["default"] if definition["type"] == "string" else None
            enabled = definition["default"] if definition["type"] == "boolean" else True
            created = admin.create_feature(
                name, names.API_FEATURE_STANDARD, enabled=enabled, value=value
            )
            out(f"created {name} (feature {created['id']})")
            features[name] = created
            continue
        problem = feature_problem(found, definition)
        if problem:
            raise FlagsmithError(f"{name} exists but {problem}; fix it in the dashboard")
        out(f"found {name} (feature {found['id']}), as the contract says")
        features[name] = found
    return features


def feature_problem(feature: dict, definition: dict) -> str | None:
    """Why a found permanent feature does not match its definition, or None. Its type is
    checked, and a string feature's value; its state never (design.md section 4)."""
    if feature.get("type") != names.API_FEATURE_STANDARD:
        return f"its type is {feature.get('type')!r}, not {names.API_FEATURE_STANDARD}"
    if definition["type"] == "string":
        _, value = live_state(feature)
        if value not in names.DEFAULT_MODEL_VALUES:
            return f"its value in Development is {value!r}, not one of {names.DEFAULT_MODEL_VALUES}"
    return None


def v2_probe(admin: Admin, name: str, out=print) -> list[tuple]:
    """Task 3.5's one run of v2 versioning over REST: turn a scratch spike_ feature off,
    then on again, each through a published version, reading the live state back after
    each. Returns what was read: (step, enabled)."""
    if not name.startswith(SPIKE_PREFIX):
        raise Refused(f"--v2-probe runs only on a scratch {SPIKE_PREFIX} feature, not {name!r}")
    feature = admin.find_feature(name)
    if feature is None:
        raise Refused(f"no feature named {name}")
    if feature.get("type") != names.API_FEATURE_STANDARD:
        raise Refused(f"{name} is not a standard feature")
    readings = [("before", live_state(feature)[0])]
    for enabled in (False, True):
        version = admin.set_state(feature, enabled=enabled)
        read = live_state(admin.find_feature(name))[0]
        uuid, live_from = version.get("uuid"), version.get("live_from")
        out(f"published version {uuid} live_from {live_from}: enabled {read}")
        readings.append((f"set {enabled}", read))
    return readings


def main(argv: list[str] | None = None, *, admin: Admin | None = None, out=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--v2-probe", metavar="SPIKE_FEATURE", help="task 3.5's v2 run, once")
    args = parser.parse_args(argv)
    try:
        if admin is None:
            check_account()
            admin = Admin(organisation_key(), log=lambda m, p, s: out(f"  {m} {p} -> {s}"))
        if args.v2_probe:
            v2_probe(admin, args.v2_probe, out)
        else:
            ensure_permanent_features(admin, out)
    except Refused as err:
        out(f"refused: {err}")
        return EXIT_REFUSED
    except FlagsmithError as err:
        out(f"failed: {err}")
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
