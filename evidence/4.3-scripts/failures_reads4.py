"""Read-only, fourth pass on the two HTTP 500s: rm-api's own 5xx count and its latency and
integration latency maxima per minute, 08:30Z to 08:38Z, and the Lambda minute 08:35 again.

The API ID is part of the ask URL, so it is captured from get-apis and never printed: the
records show <api-id> in its place.

Usage: python3 -I failures_reads4.py <output .jsonl, appended>
"""

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

out = Path(sys.argv[1])
ENV = {k: v for k, v in os.environ.items()
       if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
ENV["AWS_PROFILE"] = "abisb_IsbAdminsPS-818928662830"
ENV["TZ"] = "UTC"
records = []


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def run(argv):
    return subprocess.run(argv, env=ENV, capture_output=True, text=True)


ids = run(["aws", "apigatewayv2", "get-apis", "--query", "Items[?Name=='rm-api'].ApiId",
           "--output", "text", "--region", "us-east-1"]).stdout.split()
assert len(ids) == 1 and re.fullmatch(r"[a-z0-9]{10}", ids[0]), "expected one rm-api"
api_id = ids[0]
records.append({"kind": "read", "at": now(), "step": "rm-api's ID, into a variable (not printed)",
                "cmd": "API_ID=\"$(aws apigatewayv2 get-apis --query \"Items[?Name=='rm-api'].ApiId\" "
                       "--output text --region us-east-1)\"", "stdout": "", "stderr": "", "exit": 0})


def metric(step, namespace, name, dims, stat, display_dims):
    argv = ["aws", "cloudwatch", "get-metric-statistics", "--namespace", namespace,
            "--metric-name", name, "--dimensions", *dims, "--statistics", stat,
            "--period", "60", "--start-time", "2026-11-27T08:30:00Z",
            "--end-time", "2026-11-27T08:38:00Z",
            "--query", f"sort_by(Datapoints,&Timestamp)[].{{T:Timestamp,{stat}:{stat}}}",
            "--output", "json", "--region", "us-east-1"]
    shown = [display_dims if a in dims else a for a in argv]
    shown = [a for i, a in enumerate(shown) if not (a == display_dims and i > 0 and shown[i - 1] == display_dims)]
    at = now()
    done = run(argv)
    records.append({"kind": "read", "at": at, "step": step, "cmd": shlex.join(shown),
                    "stdout": done.stdout, "stderr": done.stderr, "exit": done.returncode})


api_dims = ["Name=ApiId,Value=" + api_id, "Name=Stage,Value=$default"]
for name, stat in (("5xx", "Sum"), ("4xx", "Sum"), ("Count", "Sum"),
                   ("IntegrationLatency", "Maximum"), ("Latency", "Maximum")):
    metric(f"rm-api: {name} ({stat}) per minute", "AWS/ApiGateway", name, api_dims, stat,
           "Name=ApiId,Value=$API_ID Name=Stage,Value=$default")
for fn in ("rm-router", "rm-answer-cheaper"):
    for name in ("Errors", "Throttles"):
        metric(f"{fn}: {name} (Sum) per minute", "AWS/Lambda", name, [f"Name=FunctionName,Value={fn}"],
               "Sum", f"Name=FunctionName,Value={fn}")
text = json.dumps(records)
assert api_id not in text, "the API ID leaked"
with out.open("a") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")
for r in records:
    print("=====", r["at"], r["step"], "exit", r["exit"])
    print(re.sub(r"\n\s+", " ", r["stdout"].rstrip())[:1500])
    if r["stderr"].strip():
        print("[stderr]", r["stderr"].strip()[:400])
