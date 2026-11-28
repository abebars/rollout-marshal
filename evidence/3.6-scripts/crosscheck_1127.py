"""Scratch, read-only (task 3.6): experiment 1127's own state, and its results against
CloudWatch. Run from the worktree root:

    uv run python <this file> <worktree root> <results json>

Each identity answered 2 questions, and Flagsmith keeps one value per identity (the mean of
its events). So each arm's sum times 2 should equal the CloudWatch Sum over the load window.
"""

import json
import sys
from datetime import UTC, datetime

sys.path.insert(0, sys.argv[1])

import boto3  # noqa: E402

from scripts.bootstrap_flagsmith import Admin, check_account, organisation_key  # noqa: E402

check_account()
admin = Admin(organisation_key())
experiment = admin.request("GET", admin.environment_path("experiments/1127/")) or {}
keep = ("id", "name", "status", "feature", "feature_name", "created_at", "started_at",
        "ended_at", "updated_at")
print("experiment:", json.dumps({k: experiment.get(k) for k in keep if k in experiment}))
print("experiment keys:", sorted(experiment))

record = json.load(open(sys.argv[2]))
names = {m["metric"]: m["metric_name"] for m in record["experiment_metrics"]}
flagsmith = {names[m["metric_id"]]: m["variants"] for m in record["results"]["payload"]["metrics"]}

cw = boto3.client("cloudwatch", region_name="us-east-1")
start, end = datetime(2026, 11, 27, 8, 11, tzinfo=UTC), datetime(2026, 11, 27, 8, 15, tzinfo=UTC)
pairs = {"mean_latency_ms": "AnswerLatencyMs", "mean_cost_micro_usd": "AnswerCostMicroUsd",
         "helpful_rate": "AnswerHelpful"}
arms = {"current": "control", "cheaper": "cheaper"}
for metric_name, cw_metric in pairs.items():
    for variant, arm in arms.items():
        # The judge's metrics land when it runs, after the load: widen the window's end.
        wide_end = end if cw_metric == "AnswerLatencyMs" else datetime(2026, 11, 27, 8, 20, tzinfo=UTC)
        points = cw.get_metric_statistics(
            Namespace="RolloutMarshal", MetricName=cw_metric,
            Dimensions=[{"Name": "Variant", "Value": variant}],
            StartTime=start, EndTime=wide_end, Period=int((wide_end - start).total_seconds()),
            Statistics=["Sum", "SampleCount"],
        )["Datapoints"]
        cw_sum = sum(p["Sum"] for p in points)
        cw_n = sum(p["SampleCount"] for p in points)
        fs = flagsmith[metric_name][arm]
        print(f"{metric_name} {variant}: flagsmith n={fs['n']} sum*2={fs['sum'] * 2:.4f}; "
              f"cloudwatch {cw_metric} n={cw_n:.0f} sum={cw_sum:.4f}; "
              f"diff={fs['sum'] * 2 - cw_sum:.6f}")
