"""4.1's after-3.6 reads, all read-only, through the SSO profile.

Usage: python3 -I after36_reads.py <output .jsonl>

1. identity; 2. 3.6's baseline: p95 and SampleCount of AnswerLatencyMs, exactly
Variant=cheaper, one 240 s period over [08:11:00, 08:15:00)Z; 3. the alarm's threshold now;
4. SNS: the Confirmed query (no ARN, no Endpoint); then PendingConfirmation and
ConfirmationWasAuthenticated, read with the subscription's ARN, which is captured from a
query and never printed, saved or committed.
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
TOPIC = "arn:aws:sns:us-east-1:818928662830:rm-incidents"
records = []


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def aws(step, args, *, display=None, keep=True):
    argv = ["aws", *args, "--region", "us-east-1"]
    at = now()
    done = subprocess.run(argv, env=ENV, capture_output=True, text=True)
    if keep:
        records.append({"kind": "read", "at": at, "step": step,
                        "cmd": display or shlex.join(argv), "stdout": done.stdout,
                        "stderr": done.stderr, "exit": done.returncode})
    return done


who = aws("identity", ["sts", "get-caller-identity", "--query", "Account", "--output", "text"])
assert who.stdout.strip() == "818928662830", who.stdout
base = ["cloudwatch", "get-metric-statistics", "--namespace", "RolloutMarshal",
        "--metric-name", "AnswerLatencyMs", "--dimensions", "Name=Variant,Value=cheaper",
        "--period", "240", "--start-time", "2026-11-27T08:11:00Z",
        "--end-time", "2026-11-27T08:15:00Z"]
aws("3.6's baseline p95, Variant=cheaper, [08:11:00, 08:15:00)Z",
    [*base, "--extended-statistics", "p95", "--query",
     "Datapoints[].{T:Timestamp,p95:ExtendedStatistics.p95}", "--output", "json"])
aws("3.6's baseline sample count, Variant=cheaper, [08:11:00, 08:15:00)Z",
    [*base, "--statistics", "SampleCount", "--query",
     "Datapoints[].{T:Timestamp,N:SampleCount}", "--output", "json"])
aws("the alarm's threshold and state", [
    "cloudwatch", "describe-alarms", "--alarm-names", "rm-cheaper-latency-p95", "--query",
    "MetricAlarms[0].{Threshold:Threshold,StateValue:StateValue,StateUpdatedTimestamp:StateUpdatedTimestamp}",
    "--output", "json"])
aws("rm-incidents subscriptions, confirmed or not (no ARN, no Endpoint)", [
    "sns", "list-subscriptions-by-topic", "--topic-arn", TOPIC, "--query",
    "Subscriptions[].{Confirmed:SubscriptionArn!='PendingConfirmation',Protocol:Protocol,TopicArn:TopicArn}",
    "--output", "json"])
arn = aws("capture", ["sns", "list-subscriptions-by-topic", "--topic-arn", TOPIC, "--query",
                      "Subscriptions[?Protocol=='email'].SubscriptionArn", "--output", "text"],
          keep=False).stdout.split()
assert len(arn) == 1 and arn[0].startswith(TOPIC + ":"), "expected one confirmed email subscription"
records.append({"kind": "read", "at": now(), "step": "the subscription's ARN, into a shell variable (not printed)",
                "cmd": "SUBSCRIPTION_ARN=\"$(aws sns list-subscriptions-by-topic --topic-arn "
                       f"{TOPIC} --query \"Subscriptions[?Protocol=='email'].SubscriptionArn\" "
                       "--output text --region us-east-1)\"",
                "stdout": "", "stderr": "", "exit": 0})
aws("the subscription's confirmation attributes (no ARN, no Endpoint)", [
    "sns", "get-subscription-attributes", "--subscription-arn", arn[0], "--query",
    "Attributes.{PendingConfirmation:PendingConfirmation,ConfirmationWasAuthenticated:ConfirmationWasAuthenticated,Protocol:Protocol}",
    "--output", "json"],
    display="aws sns get-subscription-attributes --subscription-arn \"$SUBSCRIPTION_ARN\" --query "
            "'Attributes.{PendingConfirmation:PendingConfirmation,ConfirmationWasAuthenticated:"
            "ConfirmationWasAuthenticated,Protocol:Protocol}' --output json --region us-east-1")
text = json.dumps(records)
assert arn[0] not in text and arn[0].rsplit(":", 1)[1] not in text, "the ARN leaked"
assert "@" not in text, "an endpoint leaked"
out.write_text("".join(json.dumps(r) + "\n" for r in records))
for r in records:
    print(r["at"], r["step"], "exit", r["exit"])
