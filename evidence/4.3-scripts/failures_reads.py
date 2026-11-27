"""Read-only: why two of the fault run's requests got HTTP 500. Timeouts in rm-router's and
rm-answer-cheaper's logs over the load's window, 08:31:00Z to 08:36:00Z.

Usage: python3 -I failures_reads.py <output .jsonl>
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
END = int(datetime(2026, 11, 27, 8, 36, tzinfo=timezone.utc).timestamp() * 1000)
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


aws("identity", ["sts", "get-caller-identity", "--query", "Account", "--output", "text"])
for group, pattern in (("/aws/lambda/rm-router", '"Task timed out"'),
                       ("/aws/lambda/rm-router", '"timeout"'),
                       ("/aws/lambda/rm-answer-cheaper", '"Task timed out"')):
    aws(f"{group}: {pattern} from 08:31:00Z to 08:36:00Z", [
        "logs", "filter-log-events", "--log-group-name", group,
        "--start-time", str(START), "--end-time", str(END), "--filter-pattern", pattern,
        "--query", "events[].message", "--output", "json"])
out.write_text("".join(json.dumps(r) + "\n" for r in records))
for r in records:
    print("=====", r["at"], r["step"], "exit", r["exit"])
    print(r["stdout"].rstrip()[:3000])
    if r["stderr"].strip():
        print("[stderr]", r["stderr"].strip()[:500])
