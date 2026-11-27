"""report: the decision report, its guardrails and its recommendation (tasks.md 2.4;
design.md sections 6 and 10).

The report is written to s3://rm-reports-818928662830/runs/<run_id>/report.md with a plain
put, so S3 keeps the report of the latest entry into RESULTS_READY. Its nine sections come in
section 10's order and are built from the run item and the results by code. The model's
prose, written by the caller before the claim (design.md section 6), may be added under any
section. Imports only contracts, the standard library and boto3.
"""

from __future__ import annotations

import functools
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3

from contracts import names

# Flagsmith's credible interval for the lift is 95 percent (design.md section 6). Its lower
# bound at or above the floor leaves at most one tail, 2.5 percent, below the floor: at least
# a 97.5 percent probability that the lift is at least the floor.
CREDIBLE_INTERVAL_PERCENT = 95  # names.<this> after c3
PROMOTE_PROBABILITY_PERCENT = 100 - (100 - CREDIBLE_INTERVAL_PERCENT) / 2  # 97.5
_CONTENT_TYPE = "text/markdown; charset=utf-8"
# A Markdown heading: up to three spaces, then #. The model's text is escaped there, so
# a heading in it can never add a section.
_HEADING = re.compile(r"^( {0,3})#", re.M)


@functools.cache
def _client():
    """The S3 client, made on first use, in us-east-1 (design.md section 2)."""
    return boto3.client("s3", region_name=names.REGION)


def _now() -> datetime:
    return datetime.now(UTC)


def _item(run: Any) -> dict:
    """The run item, from 2.1's Run (its .item) or the item itself."""
    return getattr(run, "item", run)


def _plain(text: str) -> str:
    """The model's text with every line that would be a heading escaped."""
    return _HEADING.sub(r"\1\\#", text)


def _number(value: Any) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def guardrails_pass(results: dict) -> bool:
    """The cheaper arm's mean_latency_ms is at most 110 percent of the current arm's, and its
    mean_cost_micro_usd not higher. Compared as decimals, so exactly 110 percent passes. A
    missing mean does not pass."""
    try:
        current = results["variations"][names.VARIANT_CURRENT]
        cheaper = results["variations"][names.VARIANT_CHEAPER]
    except (KeyError, TypeError):
        return False
    for metric, ratio in (
        (names.METRIC_MEAN_LATENCY_MS, names.GUARDRAIL_MAX_LATENCY_RATIO),
        (names.METRIC_MEAN_COST_MICRO_USD, names.GUARDRAIL_MAX_COST_RATIO),
    ):
        mine, theirs = cheaper.get(metric), current.get(metric)
        if mine is None or theirs is None:
            return False
        if Decimal(str(mine)) > Decimal(str(ratio)) * Decimal(str(theirs)):
            return False
    return True


def recommend(run: Any, results: dict) -> str:
    """The recommendation rule, in code (design.md section 6): any incident is hold, re-run;
    else promote only if the lower bound of the interval for the lift on helpful_rate is at or
    above the floor and both guardrails pass; else hold. It runs on entry to RESULTS_READY,
    whose guard has already found the results ready, so it adds no check of its own."""
    if _item(run).get("incidents"):
        return names.RECOMMEND_HOLD_RERUN
    lower = results.get("helpful_lift_ci_lower")
    if (
        lower is not None
        and lower >= names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER
        and guardrails_pass(results)
    ):
        return names.RECOMMEND_PROMOTE
    return names.RECOMMEND_HOLD


def _hypothesis(item: dict, results: dict, now: datetime) -> list[str]:
    return [_plain(item["plan"]["hypothesis"])]


def _setup(item: dict, results: dict, now: datetime) -> list[str]:
    plan = item["plan"]
    split = plan["split"]
    lines = [
        f"- Current model: `{plan['current_model']}`; cheaper model: `{plan['cheaper_model']}`.",
        f"- Split: {split[names.VARIANT_CURRENT]} percent {names.VARIANT_CURRENT}, "
        f"{split[names.VARIANT_CHEAPER]} percent {names.VARIANT_CHEAPER}.",
        f"- Primary metric `{plan['primary_metric']}`; guardrails "
        + ", ".join(f"`{m}`" for m in plan["guardrail_metrics"])
        + ".",
        f"- At least {plan['min_identities_per_variation']} identities per variation; rollback "
        f"alarm `{plan['stop_rules']['rollback_alarm']}`.",
    ]
    if item.get("feature_key"):
        experiment = item.get("experiment_id")
        lines.append(
            f"- Flagsmith feature `{item['feature_key']}`"
            + (f", experiment {experiment}." if experiment is not None else ".")
        )
    return lines


def _timeline(item: dict, results: dict, now: datetime) -> list[str]:
    rows = ["| At | From | To | Actor | Reason |", "| --- | --- | --- | --- | --- |"]
    for entry in item["history"]:
        rows.append(
            f"| {entry['at']} | {entry['from'] or '(none)'} | {entry['to']} | {entry['actor']} | "
            f"{entry['reason']} |"
        )
    return rows


def _results(item: dict, results: dict, now: datetime) -> list[str]:
    rows = [
        "| Variation | " + " | ".join(names.VARIATION_FIELDS) + " |",
        "| --- |" + " --- |" * len(names.VARIATION_FIELDS),
    ]
    for variant in names.VARIANTS:
        values = results["variations"][variant]
        rows.append(
            f"| {variant} | "
            + " | ".join(_number(values.get(field)) for field in names.VARIATION_FIELDS)
            + " |"
        )
    rows.append("")
    rows.append(
        f"Lower bound of the {CREDIBLE_INTERVAL_PERCENT} percent credible interval for the "
        f"relative lift on `{names.PRIMARY_METRIC}`: "
        f"{_number(results.get('helpful_lift_ci_lower'))}."
    )
    return rows


def _guardrails(item: dict, results: dict, now: datetime) -> list[str]:
    current = results["variations"][names.VARIANT_CURRENT]
    cheaper = results["variations"][names.VARIANT_CHEAPER]
    latency, cost = names.METRIC_MEAN_LATENCY_MS, names.METRIC_MEAN_COST_MICRO_USD
    latency_limit = round(names.GUARDRAIL_MAX_LATENCY_RATIO * 100)
    verdict = "pass" if guardrails_pass(results) else "fail"
    return [
        f"- `{latency}`: cheaper {_number(cheaper.get(latency))} against current "
        f"{_number(current.get(latency))}; the limit is {latency_limit} percent of current.",
        f"- `{cost}`: cheaper {_number(cheaper.get(cost))} against current "
        f"{_number(current.get(cost))}; it may not be higher.",
        f"- Both guardrails together: {verdict}.",
    ]


def _incident(item: dict, results: dict, now: datetime) -> list[str]:
    incidents = item.get("incidents") or []
    if not incidents:
        return ["No incident: the run was never rolled back."]
    return [
        f"- Rolled back at {incident['started_at']}; "
        + (
            f"resumed at {incident['resumed_at']}."
            if incident.get("resumed_at")
            else "not resumed."
        )
        for incident in incidents
    ]


def _known_bias(item: dict, results: dict, now: datetime) -> list[str]:
    switch = names.KILL_SWITCH_FEATURE
    return [
        f"- Rolled-back users still count in the cheaper arm: while `{switch}` is off they are "
        "served the current model, but their outcomes count as cheaper's.",
        f"- An identity first seen while `{switch}` is off is enrolled in "
        f"`{names.VARIANT_CHEAPER}` without ever being served it.",
    ]


def _recommendation(item: dict, results: dict, now: datetime) -> list[str]:
    recommendation = recommend(item, results)
    floor = round(names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER * 100)
    lines = [
        f"**{recommendation}**",
        "",
        f"The rule, applied in code: any incident means {names.RECOMMEND_HOLD_RERUN}. Otherwise "
        f"{names.RECOMMEND_PROMOTE} only if there is at least a {PROMOTE_PROBABILITY_PERCENT:g} "
        f"percent probability that the lift on `{names.PRIMARY_METRIC}` is at least {floor} "
        f"percent (the lower bound of the {CREDIBLE_INTERVAL_PERCENT} percent credible interval "
        f"at or above {floor} percent) and both guardrails pass; otherwise "
        f"{names.RECOMMEND_HOLD}. Promotion still waits for the engineer to end the experiment in "
        "Flagsmith and approve.",
    ]
    if recommendation == names.RECOMMEND_HOLD_RERUN:
        lines += [
            "",
            "Rolled-back users still count in the cheaper arm, so this comparison is biased: "
            "re-run it without an incident.",
        ]
    return lines


def _time_taken(item: dict, results: dict, now: datetime) -> list[str]:
    started = datetime.fromisoformat(item["created_at"])
    minutes = (now - started).total_seconds() / 60
    return [f"{minutes:.0f} minutes from the start, {item['created_at']}, to this report."]


_SECTIONS = dict(
    zip(
        names.REPORT_SECTIONS,
        (
            _hypothesis,
            _setup,
            _timeline,
            _results,
            _guardrails,
            _incident,
            _known_bias,
            _recommendation,
            _time_taken,
        ),
        strict=True,
    )
)


def render_report(
    run: Any, results: dict, prose: dict[str, str] | None = None, now: datetime | None = None
) -> str:
    """The report as Markdown: a title, then the nine sections in order. prose maps a section
    name to the model's words, added under that section after its facts."""
    item = _item(run)
    prose = dict(prose or {})
    unknown = sorted(set(prose) - set(names.REPORT_SECTIONS))
    if unknown:
        raise ValueError(f"no report section {unknown}; the sections are {names.REPORT_SECTIONS}")
    moment = now or _now()
    lines = [f"# Rollout Marshal report: {item['run_id']}"]
    for name, build in _SECTIONS.items():
        lines += ["", f"## {name}", "", *build(item, results, moment)]
        if prose.get(name):
            lines += ["", _plain(prose[name])]
    return "\n".join(lines) + "\n"


def write_report(run: Any, results: dict, prose: dict[str, str] | None = None) -> str:
    """Put the report at runs/<run_id>/report.md in REPORTS_BUCKET, a plain put, and return
    the key."""
    item = _item(run)
    key = names.report_key(item["run_id"])
    _client().put_object(
        Bucket=names.REPORTS_BUCKET,
        Key=key,
        Body=render_report(item, results, prose, _now()).encode(),
        ContentType=_CONTENT_TYPE,
    )
    return key
