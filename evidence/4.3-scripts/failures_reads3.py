"""Read-only, third pass on the two HTTP 500s, once the load's last minute has landed:
Lambda Errors, Throttles and ConcurrentExecutions per minute through 08:38Z for rm-router and
rm-answer-cheaper; rm-router's WARN and ERROR lines over the load; and rm-router's platform
reports whose status is not success.

Usage: python3 -I failures_reads3.py <output .jsonl, appended>
"""

import json
import os
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
START = int(datetime(2026, 11, 27, 8, 31, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 11, 27, 8, 37, tzinfo=timezone.utc).timestamp() * 1000)
records = []


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def aws(step, args):
    argv = ["aws", *args, "--region", "us-east-1"]
    at = now()
    done = subprocess.run(argv, env=ENV, capture_output=True, text=True)
    records.append({"kind": "read", "at": at, "step": step, "cmd": shlex.join(argv),
                    "stdout": done.stdout, "stderr": done.stderr, "exit": done.returncode})


for fn in ("rm-router", "rm-answer-cheaper"):
    for metric, stat in (("Errors", "Sum"), ("Throttles", "Sum"), ("ConcurrentExecutions", "Maximum")):
        aws(f"{fn}: {metric} ({stat}) per minute, 08:30:00Z to 08:38:00Z", [
            "cloudwatch", "get-metric-statistics", "--namespace", "AWS/Lambda",
            "--metric-name", metric, "--dimensions", f"Name=FunctionName,Value={fn}",
            "--statistics", stat, "--period", "60", "--start-time", "2026-11-27T08:30:00Z",
            "--end-time", "2026-11-27T08:38:00Z",
            "--query", f"sort_by(Datapoints,&Timestamp)[].{{T:Timestamp,{stat}:{stat}}}",
            "--output", "json"])
for label, pattern in (("WARN-level lines", '{ $.level = "WARN" }'),
                       ("ERROR-level lines", '{ $.level = "ERROR" }'),
                       ("platform reports not ending in success",
                        '{ $.type = "platform.report" && $.record.status != "success" }')):
    aws(f"/aws/lambda/rm-router: {label}, 08:31:00Z to 08:37:00Z", [
        "logs", "filter-log-events", "--log-group-name", "/aws/lambda/rm-router",
        "--start-time", str(START), "--end-time", str(END), "--filter-pattern", pattern,
        "--query", "events[].message", "--output", "json"])
with out.open("a") as f:
    for r in records:
        f.write(json.dumps(r) + "\n")
for r in records:
    print("=====", r["at"], r["step"], "exit", r["exit"])
    print(r["stdout"].rstrip()[:3000])
    if r["stderr"].strip():
        print("[stderr]", r["stderr"].strip()[:400])
