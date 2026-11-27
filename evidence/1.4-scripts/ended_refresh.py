"""Lead (task 1.4): does a results refresh work on an ENDED experiment, and does is_final turn
true? One POST results/refresh/ on experiment 807 (completed), then GET results/ twice.
The organisation key is read at run time and never printed; the environment key is redacted.
argv: <log.json>"""
import json, sys, time, urllib.error, urllib.request
import boto3
sys.path.insert(0, "/Users/ahmedbebars/Development/Projects/rollout-marshal")
from contracts import names  # noqa: E402
API = "https://api.flagsmith.com/api/v1"; log, secrets = [], []
def redact(t):
    for s in secrets: t = t.replace(s, "<redacted>")
    return t
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Api-Key " + KEY)
    req.add_header("Content-Type", "application/json"); req.add_header("User-Agent", "rollout-marshal-lead")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as r: st, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e: st, raw = e.code, e.read().decode()
    ms = round((time.monotonic() - t0) * 1000)
    log.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "request": f"{method} {redact(path)}", "status": st, "ms": ms, "response": redact(raw)})
    print(f"{st} {method} {redact(path)} {ms} ms")
    return st, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
KEY = boto3.client("secretsmanager", region_name=names.REGION).get_secret_value(
    SecretId=names.SECRET_FLAGSMITH_API_KEY)["SecretString"].strip(); secrets.append(KEY)
_, envs = call("GET", f"/environments/?project={names.FLAGSMITH_PROJECT_ID}")
envs = envs["results"] if isinstance(envs, dict) else envs
ENV = next(e for e in envs if e["name"] == names.FLAGSMITH_ENVIRONMENT_NAME)["api_key"]; secrets.append(ENV)
E = f"/environments/{ENV}/experiments/807"
call("GET", f"{E}/")
st, r = call("POST", f"{E}/results/refresh/", {})
for i in range(2):
    time.sleep(20)
    st, res = call("GET", f"{E}/results/")
    rr = res.get("results", {}) if isinstance(res, dict) else {}
    print("  as_of", rr.get("as_of"), "is_final", rr.get("is_final"), "refresh_requested_at", rr.get("refresh_requested_at"), "last_error_at", rr.get("last_error_at"))
json.dump(log, open(sys.argv[1], "w"), indent=1)
