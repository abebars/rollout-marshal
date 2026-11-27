"""Lead: read-only status of experiments 807 and 819 after they were ended, plus 807's frozen
results. The organisation key is read at run time and never printed; the environment key is
redacted. argv: <log.json>"""
import json, sys, urllib.error, urllib.request
import boto3
sys.path.insert(0, "/Users/ahmedbebars/Development/Projects/rollout-marshal")
from contracts import names  # noqa: E402
API = "https://api.flagsmith.com/api/v1"; log, secrets = [], []
def redact(t):
    for s in secrets: t = t.replace(s, "<redacted>")
    return t
def get(path):
    req = urllib.request.Request(API + path)
    req.add_header("Authorization", "Api-Key " + KEY)  # one space: the header on the wire
    req.add_header("User-Agent", "rollout-marshal-lead")
    try:
        with urllib.request.urlopen(req, timeout=60) as r: st, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e: st, raw = e.code, e.read().decode()
    log.append({"request": f"GET {redact(path)}", "status": st, "response": redact(raw)})
    return st, json.loads(raw)
KEY = boto3.client("secretsmanager", region_name=names.REGION).get_secret_value(
    SecretId=names.SECRET_FLAGSMITH_API_KEY)["SecretString"].strip(); secrets.append(KEY)
st0, envs = get(f"/environments/?project={names.FLAGSMITH_PROJECT_ID}"); print("environments", st0)
envs = envs["results"] if isinstance(envs, dict) else envs
ENV = next(e for e in envs if e["name"] == names.FLAGSMITH_ENVIRONMENT_NAME)["api_key"]; secrets.append(ENV)
for exp in (807, 819):
    st, x = get(f"/environments/{ENV}/experiments/{exp}/")
    print(exp, st, x.get("name"), x.get("status"), "started", x.get("started_at"), "ended", x.get("ended_at"),
          "rollout_decision", x.get("rollout_decision"), "rollout_variant", x.get("rollout_variant"))
st, r = get(f"/environments/{ENV}/experiments/807/results/")
res = r.get("results", {})
m = res.get("payload", {}).get("metrics", [{}])[0]
print("807 results", st, "is_final", res.get("is_final"), "as_of", res.get("as_of"),
      {k: (v.get("n"), v.get("sum")) for k, v in m.get("variants", {}).items()})
_, running = get(f"/environments/{ENV}/experiments/")
running = running["results"] if isinstance(running, dict) else running
print("experiments still running:", [(e["id"], e["name"]) for e in running if e.get("status") == "running"])
json.dump(log, open(sys.argv[1], "w"), indent=1)
