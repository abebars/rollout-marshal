"""Lead: task 1.2's reads for experiment 807: warehouse events, exposures, results.

Read-only except the two refresh POSTs, which Flagsmith rate-limits (results: once every
5 minutes). The organisation key is read at run time and never printed; the environment key
is redacted from everything logged or saved. argv: <log.json> <results-out.json> [--no-refresh]
"""

import json
import sys
import urllib.error
import urllib.request

import boto3

sys.path.insert(0, "/Users/ahmedbebars/Development/Projects/rollout-marshal")
from contracts import names  # noqa: E402

API = "https://api.flagsmith.com/api/v1"
LOG_PATH, OUT_PATH = sys.argv[1], sys.argv[2]
REFRESH = "--no-refresh" not in sys.argv
EXP = 807
log, secrets = [], []


def redact(text):
    for s in secrets:
        text = text.replace(s, "<redacted>")
    return text


def call(method, path, body=None):
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
    log.append({"request": f"{method} {redact(path)}", "status": status, "response": redact(raw)})
    print(f"{status} {method} {redact(path)}  ({len(raw)} bytes)")
    return status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)


def results(page):
    return page["results"] if isinstance(page, dict) and "results" in page else page


sm = boto3.client("secretsmanager", region_name=names.REGION)
KEY = sm.get_secret_value(SecretId=names.SECRET_FLAGSMITH_API_KEY)["SecretString"].strip()
secrets.append(KEY)
_, envs = call("GET", f"/environments/?project={names.FLAGSMITH_PROJECT_ID}")
env = next(e for e in results(envs) if e["name"] == names.FLAGSMITH_ENVIRONMENT_NAME)
ENV = env["api_key"]
secrets.append(ENV)
E = f"/environments/{ENV}"

_, conns = call("GET", f"{E}/warehouse-connections/")
for c in results(conns):
    cid = c.get("id")
    print("warehouse connection", cid, {k: c.get(k) for k in ("type", "status", "name", "created_at") if k in c})
    call("GET", f"{E}/warehouse-connections/{cid}/events/")
if REFRESH:
    call("POST", f"{E}/experiments/{EXP}/exposures/refresh/", {})
    call("POST", f"{E}/experiments/{EXP}/results/refresh/", {})
call("GET", f"{E}/experiments/{EXP}/exposures/")
st, res = call("GET", f"{E}/experiments/{EXP}/results/")
call("GET", f"{E}/experiments/{EXP}/")
json.dump(log, open(LOG_PATH, "w"), indent=1)
json.dump(json.loads(redact(json.dumps(res))) if not isinstance(res, str) else {"raw": redact(res)},
          open(OUT_PATH, "w"), indent=2)
print("saved")
