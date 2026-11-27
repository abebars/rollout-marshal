"""Scratch, read-only (task 3.6): where AnswerLatencyMs's samples sit, minute by minute, and
what each window and period choice returns.

    uv run python latency_windows.py
"""

from datetime import UTC, datetime

import boto3

cw = boto3.client("cloudwatch", region_name="us-east-1")


def at(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(2026, 11, 27, h, m, s, tzinfo=UTC)


def points(variant, start, end, period, stats=None, extended=None):
    kwargs = {
        "Namespace": "RolloutMarshal",
        "MetricName": "AnswerLatencyMs",
        "Dimensions": [{"Name": "Variant", "Value": variant}],
        "StartTime": start,
        "EndTime": end,
        "Period": period,
    }
    if stats:
        kwargs["Statistics"] = stats
    if extended:
        kwargs["ExtendedStatistics"] = extended
    return sorted(cw.get_metric_statistics(**kwargs)["Datapoints"], key=lambda p: p["Timestamp"])


print("per minute, 08:10 to 08:17:")
for variant in ("current", "cheaper"):
    row = [(p["Timestamp"].strftime("%H:%M"), int(p["SampleCount"]))
           for p in points(variant, at("08:10:00"), at("08:17:00"), 60, stats=["SampleCount"])]
    print(f"  {variant}: {row} total {sum(c for _, c in row)}")

windows = [
    ("08:11:00", "08:14:20", 240),
    ("08:11:00", "08:15:00", 240),
    ("08:11:00", "08:16:00", 300),
    ("08:10:00", "08:20:00", 600),
    ("08:00:00", "09:00:00", 3600),
]
for start, end, period in windows:
    print(f"\nwindow {start} to {end}, period {period}:")
    for variant in ("current", "cheaper"):
        got = points(variant, at(start), at(end), period, stats=["SampleCount"], extended=["p95"])
        print("  " + variant + ": " + ", ".join(
            f"{p['Timestamp'].strftime('%H:%M:%S')} n={int(p['SampleCount'])} p95={p['ExtendedStatistics']['p95']:.1f}"
            for p in got))
