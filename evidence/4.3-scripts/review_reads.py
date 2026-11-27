"""PR #34's review reads, all read-only, through the SSO profile with TZ=UTC.

Usage: python3 -I review_reads.py <repository root> <reads .jsonl> <failures .jsonl, appended>

1. identity; 2. Flagsmith experiment 1140 by GET (evidence/4.3-scripts/read_experiment.py);
3. the FIS experiment in full (get-experiment, no --query); 4. for the two HTTP 500s: the
"timeout" search in rm-answer-cheaper's log, rm-answer-current's timeout searches and its
Errors and Throttles, and Invocations per minute for rm-router and both answer functions.
"""

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

repo, reads_out, failures_out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3])
ENV = {k: v for k, v in os.environ.items()
       if k not in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")}
ENV["AWS_PROFILE"] = "abisb_IsbAdminsPS-818928662830"
ENV["TZ"] = "UTC"
START = int(datetime(2026, 11, 27, 8, 31, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 11, 27, 8, 36, tzinfo=timezone.utc).timestamp() * 1000)


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def run(records, kind, step, argv):
    at = now()
    done = subprocess.run(argv, env=ENV, cwd=repo, capture_output=True, text=True)
    records.append({"kind": kind, "at": at, "step": step, "cmd": shlex.join(argv),
                    "stdout": done.stdout, "stderr": done.stderr, "exit": done.returncode})
    return done


reads, failures = [], []
who = run(reads, "read", "identity", ["aws", "sts", "get-caller-identity", "--query", "Account",
                                       "--output", "text", "--region", "us-east-1"])
assert who.stdout.strip() == "818928662830", who.stdout
run(reads, "read", "Flagsmith experiment 1140, by GET",
    ["uv", "run", "python", "evidence/4.3-scripts/read_experiment.py", "1140"])
run(reads, "read", "the FIS experiment, in full",
    ["aws", "fis", "get-experiment", "--id", "EXPe6W82BJGXLorQnn", "--output", "json",
     "--region", "us-east-1"])

for group, pattern in (("/aws/lambda/rm-answer-cheaper", '"timeout"'),
                       ("/aws/lambda/rm-answer-current", '"Task timed out"'),
                       ("/aws/lambda/rm-answer-current", '"timeout"')):
    run(failures, "read", f"{group}: {pattern} from 08:31:00Z to 08:36:00Z", [
        "aws", "logs", "filter-log-events", "--log-group-name", group,
        "--start-time", str(START), "--end-time", str(END), "--filter-pattern", pattern,
        "--query", "events[].message", "--output", "json", "--region", "us-east-1"])
for fn, metrics in (("rm-answer-current", ("Errors", "Throttles", "Invocations")),
                    ("rm-router", ("Invocations",)),
                    ("rm-answer-cheaper", ("Invocations",))):
    for metric in metrics:
        run(failures, "read", f"{fn}: {metric} (Sum) per minute, 08:30:00Z to 08:38:00Z", [
            "aws", "cloudwatch", "get-metric-statistics", "--namespace", "AWS/Lambda",
            "--metric-name", metric, "--dimensions", f"Name=FunctionName,Value={fn}",
            "--statistics", "Sum", "--period", "60", "--start-time", "2026-11-27T08:30:00Z",
            "--end-time", "2026-11-27T08:38:00Z",
            "--query", "sort_by(Datapoints,&Timestamp)[].{T:Timestamp,Sum:Sum}",
            "--output", "json", "--region", "us-east-1"])

for path, records in ((reads_out, reads), (failures_out, failures)):
    text = json.dumps(records).replace(str(repo), "<repo>")
    assert "Api-Key" not in text and "ser." not in text, "a Flagsmith key leaked"
    with path.open("a") as f:
        for r in json.loads(text):
            f.write(json.dumps(r) + "\n")
for r in reads + failures:
    print("=====", r["at"], r["step"], "exit", r["exit"])
    out = r["stdout"].rstrip()
    print(out if len(out) < 1500 else out[:1500] + " ...")
    if r["stderr"].strip():
        print("[stderr]", r["stderr"].strip()[:400])
