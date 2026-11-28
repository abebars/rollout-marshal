"""Lead: create and start task 1.2's spike experiment through the Flagsmith Admin API.

Ahmed said yes to this one launch on 2026-11-26. Get-or-create at every step, so a rerun
never makes a second copy. The organisation key is read from Secrets Manager at run time
and never printed; the environment key is held in memory and redacted from the log.
Every request and response is appended, redacted, to the JSON log given as argv[1].
"""

import json
import sys
import urllib.error
import urllib.request

import boto3

sys.path.insert(0, "/Users/ahmedbebars/Development/Projects/rollout-marshal")
from contracts import names  # noqa: E402

API = "https://api.flagsmith.com/api/v1"
LOG_PATH = sys.argv[1]
log = []
secrets = []


def redact(text: str) -> str:
    for s in secrets:
        text = text.replace(s, "<redacted>")
    return text


def call(method, path, body=None, ok=(200, 201)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"{names.GATEWAY_API_KEY_PREFIX}{KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "rollout-marshal-lead")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    entry = {"request": f"{method} {redact(path)}", "body": json.loads(redact(json.dumps(body))) if body else None,
             "status": status, "response": redact(raw)[:4000]}
    log.append(entry)
    print(f"{status} {method} {redact(path)}")
    if status not in ok:
        raise SystemExit(f"stop: {status} {redact(raw)[:800]}")
    return json.loads(raw) if raw else {}


def results(page):
    return page["results"] if isinstance(page, dict) and "results" in page else page


sm = boto3.client("secretsmanager", region_name=names.REGION)
KEY = sm.get_secret_value(SecretId=names.SECRET_FLAGSMITH_API_KEY)["SecretString"].strip()
secrets.append(KEY)

envs = results(call("GET", f"/environments/?project={names.FLAGSMITH_PROJECT_ID}"))
env = next(e for e in envs if e["name"] == names.FLAGSMITH_ENVIRONMENT_NAME)
ENV = env["api_key"]
secrets.append(ENV)  # the client-side key: not a secret, but kept out of the repo
print("environment:", env["name"], "id", env["id"])

wh = results(call("GET", f"/environments/{ENV}/warehouse-connections/"))
print("warehouse connections:", len(wh))

P = names.FLAGSMITH_PROJECT_ID
found = [f for f in results(call("GET", f"/projects/{P}/features/?search=spike_flag")) if f["name"] == "spike_flag"]
feature = found[0] if found else call("POST", f"/projects/{P}/features/", {
    "name": "spike_flag", "type": "MULTIVARIATE", "initial_value": names.CONTROL_VALUE,
    "default_enabled": True, "description": "Task 1.2 spike: SDK exposure and event join"})
fid = feature["id"]
options = results(call("GET", f"/projects/{P}/features/{fid}/mv-options/"))
option = next((o for o in options if o.get("key") == names.TREATMENT_OPTION_KEY), None) or call(
    "POST", f"/projects/{P}/features/{fid}/mv-options/", {
        "feature": fid, "type": "unicode", "string_value": names.TREATMENT_OPTION_VALUE,
        "default_percentage_allocation": 0, "key": names.TREATMENT_OPTION_KEY})
print("feature", fid, "option", option["id"], "key", option.get("key"), "value", option.get("string_value"))

metrics = results(call("GET", f"/environments/{ENV}/experiment-metrics/"))
metric = next((m for m in metrics if m["name"] == "spike_metric"), None) or call(
    "POST", f"/environments/{ENV}/experiment-metrics/", {
        "name": "spike_metric", "aggregation": names.METRIC_AGGREGATION, "direction": "up",
        "definition": {"version": 1, "event": "spike_event"}})
print("metric", metric["id"], metric.get("aggregation"), metric.get("direction"), metric.get("definition"))

exps = results(call("GET", f"/environments/{ENV}/experiments/"))
exp = next((x for x in exps if x.get("feature") == fid and x.get("status") != "completed"), None)
HYP = ("Task 1.2 spike: an outcome event sent by a second SDK client joins the exposure sent by "
       "the first, per identity; spike_metric differs by arm.")
if exp is None:
    exp = call("POST", f"/environments/{ENV}/experiments/", {
        "feature": fid, "name": "spike_sdk_join", "hypothesis": HYP,
        "metrics": [{"metric": str(metric["id"]), "expected_direction": "not_decrease"}],
        "experiment_rollout": {
            "enabled": True, "rollout_percentage": names.EXPERIMENT_ROLLOUT_PERCENT,
            "feature_state_value": {"type": "string", "value": names.CONTROL_VALUE},
            "multivariate_feature_state_values": [
                {"multivariate_feature_option": option["id"],
                 "percentage_allocation": names.TREATMENT_PERCENT}]}})
print("experiment", exp["id"], exp.get("name"), exp.get("status"))
if exp.get("status") == "created":
    call("POST", f"/environments/{ENV}/experiments/{exp['id']}/start/",
         {"name": exp["name"], "hypothesis": exp.get("hypothesis", HYP)})
final = call("GET", f"/environments/{ENV}/experiments/{exp['id']}/")
print("final:", final.get("id"), final.get("name"), final.get("status"), "started_at", final.get("started_at"))
json.dump(log, open(LOG_PATH, "w"), indent=1)
