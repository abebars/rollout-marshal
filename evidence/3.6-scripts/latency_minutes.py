"""Scratch, read-only (task 3.6): AnswerLatencyMs per minute (n, p95) for each variant over
08:10:00 to 08:16:00Z, and the load window [08:11:00, 08:15:00) as one 240 s period.
Timestamps are printed in UTC (botocore hands them back in local time).

    uv run python latency_minutes.py
"""

from datetime import UTC, datetime

import boto3

cw = boto3.client("cloudwatch", region_name="us-east-1")


def read(variant, start, end, period):
    reply = cw.get_metric_statistics(
        Namespace="RolloutMarshal",
        MetricName="AnswerLatencyMs",
        Dimensions=[{"Name": "Variant", "Value": variant}],
        StartTime=start,
        EndTime=end,
        Period=period,
        Statistics=["SampleCount"],
        ExtendedStatistics=["p95"],
    )
    return sorted(reply["Datapoints"], key=lambda p: p["Timestamp"])


t = lambda h, m: datetime(2026, 11, 27, h, m, tzinfo=UTC)  # noqa: E731
for variant in ("cheaper", "current"):
    print(f"{variant}, per minute:")
    for p in read(variant, t(8, 10), t(8, 16), 60):
        stamp = p["Timestamp"].astimezone(UTC).strftime("%H:%M:%SZ")
        print(f"  {stamp} n={int(p['SampleCount'])} p95={p['ExtendedStatistics']['p95']:.1f}")
    for p in read(variant, t(8, 11), t(8, 15), 240):
        stamp = p["Timestamp"].astimezone(UTC).strftime("%H:%M:%SZ")
        print(f"  window {stamp} +240 s: n={int(p['SampleCount'])} p95={p['ExtendedStatistics']['p95']:.1f}")
