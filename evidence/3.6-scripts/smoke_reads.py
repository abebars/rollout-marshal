"""Task 3.6's reads, prepared before the apply (the lead's brief for 3.6).

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=<the human's profile in 818928662830>
    uv run python evidence/3.6-scripts/smoke_reads.py usage
    uv run python evidence/3.6-scripts/smoke_reads.py latency --start <ISO> --end <ISO>
    uv run python evidence/3.6-scripts/smoke_reads.py cost --answers-sent N [--expected-failures F]
    uv run python evidence/3.6-scripts/smoke_reads.py results <experiment_id> [--refresh]

- usage: Flagsmith's request count for the organisation (the API 1.3 found), against
  CLAUDE.md's stop line of 35,000.
- latency: AnswerLatencyMs p95 for exactly Variant=cheaper over the load window, as one
  period, which is the baseline 4.1 recalibrates the alarm from. Also each variant's
  sample count, since the Pass needs datapoints for both, and both variants' p95 and count
  minute by minute. The window runs from the start's minute to the first whole minute at
  or after the end: in the live run an end of 08:14:20 dropped the whole 08:14 minute.
- cost: CLAUDE.md's cost check. For each variant, the Sum and SampleCount of
  AnswerCostMicroUsd since 2026-11-26T02:30:00Z, statistic Sum, period 3600, with every
  datapoint added. An empty result is unknown, not zero. Then tasks.md 3.6's projection:
  spent so far, plus the remaining answers (with one 150 x 2 rehearsal per expected
  failure) times the higher mean cost per answer.
- results: the experiment read and the raw results response, written to
  evidence/3.6-results.json: the raw reply with the experiment read's metrics list beside
  it, under "experiment_metrics", as 2.2's waiting test reads it.
  - With --refresh it first asks Flagsmith to recompute, once (POST results/refresh/; a
    429 means within 5 minutes of the last). It then waits until the read is current by
    design.md section 7's three conditions.
  - It prints each arm's n beside its identities, which settles what n counts.

Everything is a read except --refresh. The organisation key is read in-process and never
printed (scripts/bootstrap_flagsmith.py's client).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # contracts and scripts

import boto3  # noqa: E402

from contracts import names  # noqa: E402
from scripts.bootstrap_flagsmith import (  # noqa: E402
    Admin,
    FlagsmithError,
    Refused,
    check_account,
    organisation_key,
)

COST_SINCE = datetime(2026, 11, 26, 2, 30, tzinfo=UTC)  # CLAUDE.md and tasks.md 3.6
COST_PERIOD_SECONDS = 3600
RESULTS_FILE = ROOT / "evidence" / "3.6-results.json"
POLL_SECONDS = 30
ATTEMPTS = 20  # 10 minutes of polling for a current read
LABELS = {names.CONTROL_LABEL: names.VARIANT_CURRENT, names.TREATMENT_OPTION_KEY: names.VARIANT_CHEAPER}


class Unknown(RuntimeError):
    """A read came back empty or stale: unknown, never zero (CLAUDE.md)."""


# ---------------------------------------------------------------------------
# CloudWatch
# ---------------------------------------------------------------------------
def _dimension(variant: str) -> list[dict]:
    return [{"Name": names.CW_DIMENSION_VARIANT, "Value": variant}]


def answer_costs(cloudwatch, *, since: datetime = COST_SINCE, until: datetime | None = None) -> dict:
    """Each variant's Sum, SampleCount and mean of AnswerCostMicroUsd since `since`."""
    until = until or datetime.now(UTC)
    costs = {}
    for variant in names.VARIANTS:
        reply = cloudwatch.get_metric_statistics(
            Namespace=names.CW_NAMESPACE,
            MetricName=names.CW_METRIC_ANSWER_COST_MICRO_USD,
            Dimensions=_dimension(variant),
            StartTime=since,
            EndTime=until,
            Period=COST_PERIOD_SECONDS,
            Statistics=["Sum", "SampleCount"],
        )
        points = reply.get("Datapoints", [])
        if not points:
            raise Unknown(
                f"no {names.CW_METRIC_ANSWER_COST_MICRO_USD} datapoint for "
                f"{names.CW_DIMENSION_VARIANT}={variant} since {since.isoformat()}: unknown,"
                " not zero; fix the query first"
            )
        total = sum(p["Sum"] for p in points)
        count = sum(p["SampleCount"] for p in points)
        costs[variant] = {"sum": total, "count": count, "mean": total / count}
    return costs


def projection(costs: dict, *, answers_sent: int, expected_failures: int) -> dict:
    """tasks.md 3.6's projection, and whether the lead must ask the human first."""
    spent = sum(c["sum"] for c in costs.values())
    higher_mean = max(c["mean"] for c in costs.values())
    rehearsal = names.LOAD_SMOKE.users * names.LOAD_SMOKE.questions  # one 150 x 2
    remaining = max(names.ANSWER_BUDGET - answers_sent, 0) + expected_failures * rehearsal
    projected = round(spent + remaining * higher_mean)
    reasons = []
    if projected > names.ANSWER_COST_STOP_MICRO_USD:
        reasons.append(f"the projection passes {names.ANSWER_COST_STOP_MICRO_USD:,}")
    if answers_sent + remaining > names.ANSWER_HARD_STOP:
        reasons.append(f"the answers pass {names.ANSWER_HARD_STOP:,}")
    return {
        "spent_micro_usd": round(spent),
        "higher_mean_micro_usd": higher_mean,
        "remaining_answers": remaining,
        "projected_micro_usd": projected,
        "answers_in_all": answers_sent + remaining,
        "ask_the_human": bool(reasons),
        "reasons": reasons,
    }


def recalibrated_threshold(p95_ms: float) -> tuple[int, bool]:
    """4.1's threshold from this baseline (design.md section 5): the p95 plus half the
    injected delay, rounded up to the next 1000; and whether the p95 is past 8000 ms,
    where 4.1 stops and asks the human first."""
    threshold = math.ceil((p95_ms + names.ALARM_THRESHOLD_FAULT_MARGIN_MS) / 1000) * 1000
    return threshold, p95_ms > names.ALARM_BASELINE_STOP_MS


def latency_datapoints(cloudwatch, since: datetime, until: datetime) -> dict:
    """Each variant's AnswerLatencyMs sample count from since to until, per minute, and
    whether any was seen: the probe's check before the 150 x 2 load (PR #27's review)."""
    samples = {}
    for variant in names.VARIANTS:
        reply = cloudwatch.get_metric_statistics(
            Namespace=names.CW_NAMESPACE,
            MetricName=names.CW_METRIC_ANSWER_LATENCY_MS,
            Dimensions=_dimension(variant),
            StartTime=since,
            EndTime=until,
            Period=60,
            Statistics=["SampleCount"],
        )
        samples[variant] = sum(p["SampleCount"] for p in reply.get("Datapoints", []))
    return {"samples": samples, "seen": any(count > 0 for count in samples.values())}


def latency_baseline(cloudwatch, start: datetime, end: datetime) -> dict:
    """AnswerLatencyMs p95 for exactly Variant=cheaper over [start, end] as one period, each
    variant's sample count over the same window, and both variants minute by minute.

    The window runs from the start's minute to the first whole minute at or after the end.
    CloudWatch rounds a StartTime down to the minute itself. In 3.6's live run an EndTime of
    08:14:20 dropped the whole 08:14 minute (25 cheaper and 17 current samples), and an
    EndTime of 08:15:00 kept them, so the end rounds up.
    """
    window_start = start.replace(second=0, microsecond=0)
    window_end = end.replace(second=0, microsecond=0)
    if window_end < end:
        window_end += timedelta(minutes=1)
    period = max(int((window_end - window_start).total_seconds()), 60)
    common = {
        "Namespace": names.CW_NAMESPACE,
        "MetricName": names.CW_METRIC_ANSWER_LATENCY_MS,
        "StartTime": window_start,
        "EndTime": window_end,
    }
    reply = cloudwatch.get_metric_statistics(
        **common,
        Period=period,
        Dimensions=_dimension(names.VARIANT_CHEAPER),
        ExtendedStatistics=["p95"],
    )
    points = reply.get("Datapoints", [])
    if len(points) != 1:
        raise Unknown(f"{len(points)} p95 datapoints for the window, not 1: fix the window first")
    p95 = points[0]["ExtendedStatistics"]["p95"]
    samples = {}
    for variant in names.VARIANTS:
        counted = cloudwatch.get_metric_statistics(
            **common, Period=period, Dimensions=_dimension(variant), Statistics=["SampleCount"]
        )
        samples[variant] = sum(p["SampleCount"] for p in counted.get("Datapoints", []))
    per_minute = {variant: _latency_minutes(cloudwatch, common, variant) for variant in names.VARIANTS}
    threshold, ask = recalibrated_threshold(p95)
    return {
        "window": [window_start.isoformat(), window_end.isoformat()],
        "period_seconds": period,
        "p95_ms": p95,
        "samples": samples,
        "per_minute": per_minute,
        "threshold_ms": threshold,
        "ask_the_human": ask,
    }


def _latency_minutes(cloudwatch, common: dict, variant: str) -> list[dict]:
    """One row per minute of the window, in time order: its sample count and p95. botocore
    hands timestamps back in local time, so each is written in UTC."""
    request = {**common, "Period": 60, "Dimensions": _dimension(variant)}
    rows: dict[datetime, dict] = {}
    p95s = cloudwatch.get_metric_statistics(**request, ExtendedStatistics=["p95"])
    for point in p95s.get("Datapoints", []):
        row = rows.setdefault(point["Timestamp"].astimezone(UTC), {})
        row["p95_ms"] = point["ExtendedStatistics"]["p95"]
    counts = cloudwatch.get_metric_statistics(**request, Statistics=["SampleCount"])
    for point in counts.get("Datapoints", []):
        rows.setdefault(point["Timestamp"].astimezone(UTC), {})["n"] = point["SampleCount"]
    return [
        {"minute": at.isoformat(), "n": row.get("n"), "p95_ms": row.get("p95_ms")}
        for at, row in sorted(rows.items())
    ]


# ---------------------------------------------------------------------------
# Flagsmith
# ---------------------------------------------------------------------------
def usage(admin: Admin) -> dict:
    path = f"/api/v1/organisations/{names.FLAGSMITH_ORGANISATION_ID}/usage-data/total-count/"
    count = (admin.request("GET", path) or {}).get("count")
    return {"count": count, "stop_at": names.FLAGSMITH_USAGE_STOP, "past_stop": (count or 0) > names.FLAGSMITH_USAGE_STOP}


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_current(reply: object, refreshed_at: datetime) -> bool:
    """design.md section 7: a read is current after a refresh asked at refreshed_at when
    refresh_requested_at is at or after refreshed_at less 60 s, the computation has
    finished (is_final, or as_of at or after the request), and it did not fail (no
    last_error_at, or one before the request). A read not computed yet is not current."""
    envelope = reply.get("results") if isinstance(reply, dict) else None
    if not isinstance(envelope, dict):
        return False
    requested = _time(envelope.get("refresh_requested_at"))
    if requested is None:
        return False
    allowance = timedelta(seconds=names.REFRESH_CLOCK_ALLOWANCE_SECONDS)
    if requested < refreshed_at - allowance:
        return False
    as_of = _time(envelope.get("as_of"))
    if not (envelope.get("is_final") is True or (as_of is not None and as_of >= requested)):
        return False
    failed = _time(envelope.get("last_error_at"))
    return failed is None or failed < requested


def summarize(envelope: dict, experiment_metrics: list[dict]) -> dict:
    """Each arm's identities, each metric's n, mean and inference, and what n counts."""
    payload = envelope.get("payload") or {}
    identities = dict.fromkeys(names.VARIANTS, 0)
    for point in (payload.get("exposures_timeseries") or {}).get("points") or []:
        for label, count in (point.get("new_identities") or {}).items():
            if label in LABELS:
                identities[LABELS[label]] += count
    name_of = {m["metric"]: m["metric_name"] for m in experiment_metrics}
    metrics = []
    for metric in payload.get("metrics") or []:
        if metric.get("metric_id") not in name_of:
            continue
        stats = {LABELS[k]: v for k, v in (metric.get("variants") or {}).items() if k in LABELS}
        inference = (metric.get("inference") or {}).get(names.TREATMENT_OPTION_KEY) or {}
        metrics.append(
            {
                "metric_name": name_of[metric["metric_id"]],
                "n": {v: s.get("n") for v, s in stats.items()},
                "mean": {v: s["sum"] / s["n"] for v, s in stats.items() if s.get("n")},
                "inference": {f: inference.get(f) for f in names.FLAGSMITH_RESULT_INFERENCE_FIELDS},
            }
        )
    by_identities = all(m["n"].get(v) == identities[v] for m in metrics for v in names.VARIANTS)
    return {
        "identities": identities,
        "metrics": metrics,
        "n_counts": "identities" if metrics and by_identities else "not the identities",
    }


def read_results(
    admin: Admin,
    experiment_id: int,
    out: Path = RESULTS_FILE,
    *,
    refresh: bool,
    now=lambda: datetime.now(UTC),
    sleep=time.sleep,
    attempts: int = ATTEMPTS,
) -> dict:
    """The experiment read's metrics beside the raw results reply, written to out."""
    base = admin.environment_path(f"experiments/{int(experiment_id)}/")
    experiment = admin.request("GET", base)
    if refresh:
        refreshed_at = now()
        admin.request("POST", f"{base}results/refresh/", {})
        for attempt in range(attempts):
            reply = admin.request("GET", f"{base}results/")
            if is_current(reply, refreshed_at):
                break
            if attempt < attempts - 1:
                sleep(POLL_SECONDS)
        else:
            raise Unknown(f"experiment {experiment_id}'s results were not current after {attempts} reads")
    else:
        reply = admin.request("GET", f"{base}results/")
        if not isinstance((reply or {}).get("results"), dict):
            raise Unknown(f"experiment {experiment_id}'s results are not computed yet; use --refresh")
    record = {
        "experiment_id": int(experiment_id),
        "read_at": now().isoformat(),
        **reply,
        "experiment_metrics": experiment.get("metrics", []),
    }
    out.write_text(json.dumps(record, indent=2) + "\n")
    return record


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------
def _iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None, *, admin: Admin | None = None, cloudwatch=None, out=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("usage")
    seen = commands.add_parser("seen")
    seen.add_argument("--since", required=True, type=_iso)
    latency = commands.add_parser("latency")
    latency.add_argument("--start", required=True, type=_iso)
    latency.add_argument("--end", required=True, type=_iso)
    cost = commands.add_parser("cost")
    cost.add_argument("--answers-sent", required=True, type=int)
    cost.add_argument("--expected-failures", type=int, default=0)
    results = commands.add_parser("results")
    results.add_argument("experiment_id", type=int)
    results.add_argument("--refresh", action="store_true")
    results.add_argument("--out", type=Path, default=RESULTS_FILE)
    args = parser.parse_args(argv)

    try:
        if args.command in ("seen", "latency", "cost"):
            if cloudwatch is None:
                check_account()
                cloudwatch = boto3.client("cloudwatch", region_name=names.REGION)
            if args.command == "seen":
                found = latency_datapoints(cloudwatch, args.since, datetime.now(UTC))
                out(json.dumps(found))
                return 0 if found["seen"] else 1
            if args.command == "latency":
                out(json.dumps(latency_baseline(cloudwatch, args.start, args.end), indent=2))
            else:
                costs = answer_costs(cloudwatch)
                plan = projection(
                    costs, answers_sent=args.answers_sent, expected_failures=args.expected_failures
                )
                out(json.dumps({"costs": costs, "projection": plan}, indent=2))
            return 0
        if admin is None:
            check_account()
            admin = Admin(organisation_key())
        if args.command == "usage":
            out(json.dumps(usage(admin)))
            return 0
        record = read_results(admin, args.experiment_id, args.out, refresh=args.refresh)
        out(f"wrote {args.out}")
        envelope = record.get("results") or {}
        out(json.dumps(summarize(envelope, record["experiment_metrics"]), indent=2))
        out(json.dumps({k: envelope.get(k) for k in ("as_of", "refresh_requested_at", "is_final", "last_error_at")}))
        return 0
    except (Unknown, Refused, FlagsmithError) as err:
        out(f"failed: {err}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
