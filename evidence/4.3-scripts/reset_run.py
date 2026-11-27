"""After the lead ended experiment 1140: identity, scripts/reset.py, then the pointer, read-only.

Usage: python3 -I reset_run.py <repository root> <output .jsonl>
"""

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
ENV = {k: v for k, v in os.environ.items()
       if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
ENV["AWS_PROFILE"] = "abisb_IsbAdminsPS-818928662830"
ENV["TZ"] = "UTC"
records = []


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def run(kind, step, argv):
    at = now()
    done = subprocess.run(argv, env=ENV, cwd=repo, capture_output=True, text=True)
    records.append({"kind": kind, "at": at, "step": step, "cmd": shlex.join(argv),
                    "stdout": done.stdout, "stderr": done.stderr, "exit": done.returncode})
    return done


who = run("read", "identity", ["aws", "sts", "get-caller-identity", "--query", "Account",
                               "--output", "text", "--region", "us-east-1"])
if who.stdout.strip() != "818928662830":
    out.write_text("".join(json.dumps(r) + "\n" for r in records))
    sys.exit(f"identity is {who.stdout.strip()!r}; reset.py not run")
run("write", "reset.py", ["uv", "run", "python", "scripts/reset.py"])
run("read", "the ACTIVE pointer in rm-runs, after reset.py", [
    "aws", "dynamodb", "get-item", "--table-name", "rm-runs", "--key", '{"pk": {"S": "ACTIVE"}}',
    "--consistent-read", "--query", "Item.{run_id:run_id.S,feature_key:feature_key.S}",
    "--output", "json", "--region", "us-east-1"])
text = json.dumps(records).replace(str(repo), "<repo>")
out.write_text("".join(json.dumps(r) + "\n" for r in json.loads(text)))
for r in json.loads(text):
    print("=====", r["at"], r["kind"], r["step"], "exit", r["exit"])
    print(r["stdout"].rstrip())
    if r["stderr"].strip():
        print("[stderr]", r["stderr"].rstrip())
