"""Tests for evidence/3.6-scripts/smoke_reads.py, task 3.6's reads, prepared before the apply.

CloudWatch is botocore's Stubber on a real client, which checks each request against the
service model and against the exact parameters expected. Flagsmith is
app/tests/flagsmith_fake.py. Nothing leaves the machine.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

from app.tests.flagsmith_fake import DEV_KEY, FakeFlagsmith
from contracts import names
from scripts import bootstrap_flagsmith as bootstrap

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evidence" / "3.6-scripts" / "smoke_reads.py"
ORG_KEY = "dummyOrganisationKeyForTests"
SINCE = datetime(2026, 11, 26, 2, 30, tzinfo=UTC)  # tasks.md 3.6 and CLAUDE.md
CURRENT, CHEAPER = names.VARIANT_CURRENT, names.VARIANT_CHEAPER


def _load():
    spec = importlib.util.spec_from_file_location("rm_smoke_reads", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reads = _load()


@pytest.fixture(autouse=True)
def _dummy_aws(monkeypatch):
    for name in [name for name in os.environ if name.startswith("AWS_") or name == "BOTO_CONFIG"]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing-secret")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("BOTO_CONFIG", os.devnull)


@pytest.fixture
def cloudwatch():
    client = boto3.client("cloudwatch", region_name=names.REGION)
    with Stubber(client) as stubber:
        client.stubber = stubber
        yield client
        stubber.assert_no_pending_responses()


def _point(at: datetime, **stats) -> dict:
    return {"Timestamp": at, "Unit": "Count", **stats}


def _expect_cost(cloudwatch, variant: str, datapoints: list[dict], until=ANY) -> None:
    cloudwatch.stubber.add_response(
        "get_metric_statistics",
        {"Label": names.CW_METRIC_ANSWER_COST_MICRO_USD, "Datapoints": datapoints},
        {
            "Namespace": names.CW_NAMESPACE,
            "MetricName": names.CW_METRIC_ANSWER_COST_MICRO_USD,
            "Dimensions": [{"Name": names.CW_DIMENSION_VARIANT, "Value": variant}],
            "StartTime": SINCE,
            "EndTime": until,
            "Period": 3600,
            "Statistics": ["Sum", "SampleCount"],
        },
    )


# ---------------------------------------------------------------------------
# The cost check
# ---------------------------------------------------------------------------
def test_the_cost_check_adds_every_hourly_datapoint_of_each_variant(cloudwatch):
    hour = SINCE + timedelta(days=1)
    _expect_cost(
        cloudwatch,
        CURRENT,
        [
            _point(hour, Sum=2_200_000.0, SampleCount=100.0),
            _point(hour + timedelta(hours=1), Sum=1_100_000.0, SampleCount=50.0),
        ],
    )
    _expect_cost(cloudwatch, CHEAPER, [_point(hour, Sum=660_000.0, SampleCount=150.0)])

    costs = reads.answer_costs(cloudwatch)

    assert costs == {
        CURRENT: {"sum": 3_300_000.0, "count": 150.0, "mean": 22_000.0},
        CHEAPER: {"sum": 660_000.0, "count": 150.0, "mean": 4_400.0},
    }


def test_an_empty_cost_read_is_unknown_never_zero(cloudwatch):
    """CLAUDE.md: an empty result means unknown, not zero; fix the query first."""
    _expect_cost(cloudwatch, CURRENT, [_point(SINCE, Sum=1.0, SampleCount=1.0)])
    _expect_cost(cloudwatch, CHEAPER, [])
    with pytest.raises(reads.Unknown, match=CHEAPER):
        reads.answer_costs(cloudwatch)


def test_an_empty_current_cost_read_is_unknown_too(cloudwatch):
    _expect_cost(cloudwatch, CURRENT, [])
    with pytest.raises(reads.Unknown, match=f"{names.CW_DIMENSION_VARIANT}={CURRENT} "):
        reads.answer_costs(cloudwatch)


@pytest.mark.parametrize(
    ("now", "end"),
    [
        (datetime(2026, 11, 27, 8, 16, 48, tzinfo=UTC), datetime(2026, 11, 27, 8, 17, tzinfo=UTC)),
        (datetime(2026, 11, 27, 8, 17, 0, tzinfo=UTC), datetime(2026, 11, 27, 8, 17, tzinfo=UTC)),
    ],
)
def test_the_cost_window_ends_on_the_first_whole_minute_at_or_after_now(cloudwatch, now, end):
    """An EndTime inside a minute drops that whole minute (3.6's latency read), so a
    stop-line check ending at now would undercount (PR #33's review, P33-6)."""
    for variant in names.VARIANTS:
        _expect_cost(cloudwatch, variant, [_point(SINCE, Sum=1.0, SampleCount=1.0)], until=end)
    reads.answer_costs(cloudwatch, until=now)


COSTS = {
    CURRENT: {"sum": 3_300_000.0, "count": 150.0, "mean": 22_000.0},
    CHEAPER: {"sum": 660_000.0, "count": 150.0, "mean": 4_400.0},
}


def test_the_projection_is_spent_plus_the_planned_answers_at_the_higher_mean():
    projected = reads.projection(
        COSTS, answers_sent=300, remaining_planned=3_000, expected_failures=1
    )

    rehearsal = names.LOAD_SMOKE.users * names.LOAD_SMOKE.questions
    remaining = 3_000 + rehearsal  # the planned answers, plus one 150 x 2 per failure
    assert projected["expected_failures"] == 1
    assert projected["spent_micro_usd"] == 3_960_000
    assert projected["higher_mean_micro_usd"] == 22_000.0
    assert projected["remaining_answers"] == remaining == 3_300
    assert projected["projected_micro_usd"] == 3_960_000 + remaining * 22_000
    assert projected["answers_in_all"] == 300 + remaining
    assert projected["ask_the_human"] is True  # 76,560,000 passes 60,000,000
    assert projected["reasons"] == [f"the projection passes {names.ANSWER_COST_STOP_MICRO_USD:,}"]


def test_a_projection_of_exactly_60000000_needs_no_question():
    """The stop is passing 60,000,000: exactly 60,000,000 does not pass it."""
    costs = {
        CURRENT: {"sum": 2_000_000.0, "count": 100.0, "mean": 20_000.0},
        CHEAPER: {"sum": 0.0, "count": 1.0, "mean": 0.0},
    }
    exact = reads.projection(costs, answers_sent=101, remaining_planned=2_900, expected_failures=0)
    assert exact["projected_micro_usd"] == names.ANSWER_COST_STOP_MICRO_USD == 60_000_000
    assert exact["ask_the_human"] is False and exact["reasons"] == []
    over = reads.projection(costs, answers_sent=101, remaining_planned=2_901, expected_failures=0)
    assert over["projected_micro_usd"] == 60_020_000 and over["ask_the_human"] is True


def test_the_projection_asks_when_the_answers_would_pass_7500():
    costs = {
        CURRENT: {"sum": 0.0, "count": 1.0, "mean": 1.0},
        CHEAPER: {"sum": 0.0, "count": 1.0, "mean": 1.0},
    }
    projected = reads.projection(
        costs, answers_sent=5_000, remaining_planned=100, expected_failures=8
    )
    assert projected["answers_in_all"] == 5_000 + 100 + 8 * 300 == 7_500
    assert projected["ask_the_human"] is False  # 7,500 does not pass 7,500
    projected = reads.projection(
        costs, answers_sent=5_000, remaining_planned=100, expected_failures=9
    )
    assert projected["ask_the_human"] is True
    assert projected["reasons"] == [f"the answers pass {names.ANSWER_HARD_STOP:,}"]


def test_the_cost_command_takes_the_planned_answers_and_prints_0_to_3_failures(cloudwatch):
    """PR #33's review, P33-9: the remaining planned answers are an argument (5.1's 1,500
    and one 5.2 run's 1,500 now), not the budget less the answers sent."""
    _expect_cost(cloudwatch, CURRENT, [_point(SINCE, Sum=1_748_021.0, SampleCount=130.0)])
    _expect_cost(cloudwatch, CHEAPER, [_point(SINCE, Sum=459_816.0, SampleCount=170.0)])
    lines = []

    code = reads.main(
        ["cost", "--answers-sent", "301", "--remaining-planned", "3000"],
        cloudwatch=cloudwatch,
        out=lines.append,
    )

    assert code == 0
    printed = json.loads(lines[0])
    assert [p["expected_failures"] for p in printed["projections"]] == [0, 1, 2, 3]
    mean = 1_748_021 / 130
    for plan in printed["projections"]:
        remaining = 3_000 + plan["expected_failures"] * 300
        assert plan["remaining_answers"] == remaining
        assert plan["projected_micro_usd"] == round(2_207_837 + remaining * mean)
    assert printed["projections"][0]["projected_micro_usd"] == 42_546_783


def test_the_cost_command_no_longer_takes_a_single_failure_count(cloudwatch):
    with pytest.raises(SystemExit):
        reads.main(
            ["cost", "--answers-sent", "301", "--expected-failures", "1"], cloudwatch=cloudwatch
        )


# ---------------------------------------------------------------------------
# The baseline latency
# ---------------------------------------------------------------------------
def _latency_request(variant: str, start: datetime, end: datetime, period: int) -> dict:
    return {
        "Namespace": names.CW_NAMESPACE,
        "MetricName": names.CW_METRIC_ANSWER_LATENCY_MS,
        "Dimensions": [{"Name": "Variant", "Value": variant}],
        "StartTime": start,
        "EndTime": end,
        "Period": period,
    }


def _expect_window(cloudwatch, start, end, period, p95, samples) -> None:
    """The window's three reads: p95 of exactly Variant=cheaper, then each variant's count."""
    cloudwatch.stubber.add_response(
        "get_metric_statistics",
        {"Datapoints": [{"Timestamp": start, "ExtendedStatistics": {"p95": p95}}]},
        {**_latency_request(CHEAPER, start, end, period), "ExtendedStatistics": ["p95"]},
    )
    for variant in names.VARIANTS:
        cloudwatch.stubber.add_response(
            "get_metric_statistics",
            {"Datapoints": [{"Timestamp": start, "SampleCount": samples[variant]}]},
            {**_latency_request(variant, start, end, period), "Statistics": ["SampleCount"]},
        )


def _expect_minutes(cloudwatch, start, end, minutes) -> None:
    """Each variant's per-minute reads, p95 then count. minutes: {variant: [(at, n, p95)]}."""
    for variant in names.VARIANTS:
        rows = minutes.get(variant, [])
        p95s = [{"Timestamp": at, "ExtendedStatistics": {"p95": p95}} for at, _, p95 in rows]
        cloudwatch.stubber.add_response(
            "get_metric_statistics",
            {"Datapoints": p95s},
            {**_latency_request(variant, start, end, 60), "ExtendedStatistics": ["p95"]},
        )
        cloudwatch.stubber.add_response(
            "get_metric_statistics",
            {"Datapoints": [{"Timestamp": at, "SampleCount": n} for at, n, _ in rows]},
            {**_latency_request(variant, start, end, 60), "Statistics": ["SampleCount"]},
        )


def test_the_baseline_is_p95_of_exactly_variant_cheaper_over_the_window(cloudwatch):
    start = datetime(2026, 11, 28, 5, 0, 30, tzinfo=UTC)
    end = datetime(2026, 11, 28, 5, 11, 0, tzinfo=UTC)  # on a whole minute: stays as it is
    window_start = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)  # CloudWatch's minute
    samples = {CURRENT: 150.0, CHEAPER: 150.0}
    _expect_window(cloudwatch, window_start, end, 660, 3_450.0, samples)  # one bucket
    _expect_minutes(cloudwatch, window_start, end, {CHEAPER: [(window_start, 150.0, 3_450.0)]})

    baseline = reads.latency_baseline(cloudwatch, start, end)

    assert baseline["window"] == [window_start.isoformat(), end.isoformat()]
    assert baseline["p95_ms"] == 3_450.0
    assert baseline["samples"] == samples
    # 4.1's recalibration: the p95, plus half the fault, rounded up to the next 1000.
    assert baseline["threshold_ms"] == 9_000
    assert baseline["ask_the_human"] is False


def test_the_window_ends_on_the_minute_after_the_end(cloudwatch):
    """3.6's live run: an EndTime of 08:14:20 dropped the whole 08:14 minute (25 cheaper and
    17 current samples), and an EndTime of 08:15:00 kept them. So the end rounds up."""
    start = datetime(2026, 11, 27, 8, 11, 49, tzinfo=UTC)
    end = datetime(2026, 11, 27, 8, 14, 20, tzinfo=UTC)
    window_start = datetime(2026, 11, 27, 8, 11, tzinfo=UTC)
    window_end = datetime(2026, 11, 27, 8, 15, tzinfo=UTC)
    _expect_window(
        cloudwatch, window_start, window_end, 240, 4_772.8, {CURRENT: 130.0, CHEAPER: 170.0}
    )
    _expect_minutes(cloudwatch, window_start, window_end, {})

    baseline = reads.latency_baseline(cloudwatch, start, end)

    assert baseline["window"] == ["2026-11-27T08:11:00+00:00", "2026-11-27T08:15:00+00:00"]
    assert baseline["period_seconds"] == 240
    assert baseline["samples"] == {CURRENT: 130.0, CHEAPER: 170.0}
    assert baseline["threshold_ms"] == 10_000


def test_the_per_minute_rows_come_back_in_utc_and_in_order(cloudwatch):
    """botocore hands timestamps back in local time; the rows are written in UTC, by minute."""
    start = datetime(2026, 11, 27, 8, 11, 49, tzinfo=UTC)
    end = datetime(2026, 11, 27, 8, 13, 0, tzinfo=UTC)
    window_start = datetime(2026, 11, 27, 8, 11, tzinfo=UTC)
    est = timezone(timedelta(hours=-5))
    first = datetime(2026, 11, 27, 3, 11, tzinfo=est)  # 08:11Z
    second = datetime(2026, 11, 27, 3, 12, tzinfo=est)
    _expect_window(cloudwatch, window_start, end, 120, 4_400.0, {CURRENT: 2.0, CHEAPER: 77.0})
    _expect_minutes(
        cloudwatch,
        window_start,
        end,
        {
            CURRENT: [(first, 1.0, 6_377.5), (second, 1.0, 7_000.0)],
            CHEAPER: [(second, 69.0, 4_289.6), (first, 8.0, 5_954.8)],
        },  # out of order
    )

    baseline = reads.latency_baseline(cloudwatch, start, end)

    assert baseline["per_minute"] == {
        CURRENT: [
            {"minute": "2026-11-27T08:11:00+00:00", "n": 1.0, "p95_ms": 6_377.5},
            {"minute": "2026-11-27T08:12:00+00:00", "n": 1.0, "p95_ms": 7_000.0},
        ],
        CHEAPER: [
            {"minute": "2026-11-27T08:11:00+00:00", "n": 8.0, "p95_ms": 5_954.8},
            {"minute": "2026-11-27T08:12:00+00:00", "n": 69.0, "p95_ms": 4_289.6},
        ],
    }


def test_a_baseline_over_8000_ms_is_flagged_for_the_human():
    assert reads.recalibrated_threshold(8_001.0) == (14_000, True)
    assert reads.recalibrated_threshold(8_000.0) == (13_000, False)
    assert reads.recalibrated_threshold(4_000.0) == (9_000, False)  # exactly 9,000: no rounding up


def test_no_latency_datapoint_is_unknown(cloudwatch):
    start, end = datetime(2026, 11, 28, 5, tzinfo=UTC), datetime(2026, 11, 28, 5, 10, tzinfo=UTC)
    cloudwatch.stubber.add_response("get_metric_statistics", {"Datapoints": []})
    with pytest.raises(reads.Unknown, match="p95"):
        reads.latency_baseline(cloudwatch, start, end)


def _expect_latency_count(cloudwatch, variant, since, until, datapoints):
    cloudwatch.stubber.add_response(
        "get_metric_statistics",
        {"Datapoints": datapoints},
        {
            "Namespace": names.CW_NAMESPACE,
            "MetricName": names.CW_METRIC_ANSWER_LATENCY_MS,
            "Dimensions": [{"Name": "Variant", "Value": variant}],
            "StartTime": since,
            "EndTime": until,
            "Period": 60,
            "Statistics": ["SampleCount"],
        },
    )


def test_the_probe_read_finds_the_one_answers_latency_datapoint(cloudwatch):
    """Before the 150 x 2 load: one signed request, then this read. EMF under JSON at INFO
    is documented but not yet seen on a deployed function (PR #27's review)."""
    since = datetime(2026, 11, 28, 4, 58, tzinfo=UTC)
    until = datetime(2026, 11, 28, 5, 3, tzinfo=UTC)
    _expect_latency_count(
        cloudwatch, CURRENT, since, until, [{"Timestamp": since, "SampleCount": 1.0}]
    )
    _expect_latency_count(cloudwatch, CHEAPER, since, until, [])

    seen = reads.latency_datapoints(cloudwatch, since, until)

    assert seen == {"samples": {CURRENT: 1.0, CHEAPER: 0}, "seen": True}


def test_no_probe_datapoint_is_reported_unseen(cloudwatch):
    since = datetime(2026, 11, 28, 4, 58, tzinfo=UTC)
    until = datetime(2026, 11, 28, 5, 3, tzinfo=UTC)
    for variant in names.VARIANTS:
        _expect_latency_count(cloudwatch, variant, since, until, [])
    assert reads.latency_datapoints(cloudwatch, since, until)["seen"] is False


def test_the_probe_read_ends_on_the_first_whole_minute_at_or_after_now(cloudwatch):
    """3.6's probe read ran at 08:11:15Z; ending there would drop the 08:11 minute."""
    since = datetime(2026, 11, 27, 8, 10, tzinfo=UTC)
    now = datetime(2026, 11, 27, 8, 11, 15, tzinfo=UTC)
    end = datetime(2026, 11, 27, 8, 12, tzinfo=UTC)
    _expect_latency_count(
        cloudwatch, CURRENT, since, end, [{"Timestamp": since, "SampleCount": 1.0}]
    )
    _expect_latency_count(cloudwatch, CHEAPER, since, end, [])
    assert reads.latency_datapoints(cloudwatch, since, now)["seen"] is True


# ---------------------------------------------------------------------------
# Flagsmith
# ---------------------------------------------------------------------------
@pytest.fixture
def flagsmith():
    return FakeFlagsmith()


@pytest.fixture
def admin(flagsmith):
    return bootstrap.Admin(ORG_KEY, transport=flagsmith)


def test_usage_reads_the_total_count_and_its_stop_line(admin, flagsmith):
    assert reads.usage(admin) == {
        "count": 150,
        "stop_at": names.FLAGSMITH_USAGE_STOP,
        "past_stop": False,
    }
    flagsmith.usage_count = names.FLAGSMITH_USAGE_STOP + 1
    assert reads.usage(admin)["past_stop"] is True
    assert [m for m, _, _ in flagsmith.requests] == ["GET", "GET"]


def test_a_usage_read_without_a_count_is_unknown_never_under_the_stop(admin, flagsmith):
    """PR #33's review, P33-7: no count is unknown, not past_stop false."""
    flagsmith.usage_count = None
    with pytest.raises(reads.Unknown, match="no count"):
        reads.usage(admin)
    lines = []
    assert reads.main(["usage"], admin=admin, out=lines.append) == 1
    assert lines[0].startswith("failed: ") and "past_stop" not in lines[0]


REQUESTED = "2026-11-28T05:20:00.000000Z"


def _envelope(*, requested=REQUESTED, as_of="2026-11-28T05:20:30.000000Z", final=False, error=None):
    return {
        "results": {
            "as_of": as_of,
            "is_final": final,
            "last_error_at": error,
            "refresh_requested_at": requested,
            "payload": {
                "srm_p_value": None,
                "exposures_timeseries": {
                    "granularity": "hour",
                    "points": [
                        {
                            "bucket": "2026-11-28T05:00:00+00:00",
                            "new_identities": {"control": 72, "cheaper": 78},
                        }
                    ],
                },
                "metrics": [
                    {
                        "metric_id": 901,
                        "variants": {
                            "control": {"n": 72, "sum": 50.0, "sum_squares": 50.0},
                            "cheaper": {"n": 78, "sum": 60.0, "sum_squares": 60.0},
                        },
                        "inference": {
                            "cheaper": {
                                "lift": 0.03,
                                "ci_low": -0.08,
                                "ci_high": 0.14,
                                "chance_to_win": 0.7,
                            }
                        },
                        "conversions_timeseries": None,
                    }
                ],
            },
        }
    }


def _experiment(flagsmith, experiment_id=840):
    flagsmith.experiments.append(
        {
            "id": experiment_id,
            "name": "assistant_model_rsmoke0500",
            "status": "running",
            "metrics": [
                {
                    "id": 1,
                    "metric": 901,
                    "metric_name": names.METRIC_HELPFUL_RATE,
                    "expected_direction": "not_decrease",
                },
                {
                    "id": 2,
                    "metric": 902,
                    "metric_name": names.METRIC_MEAN_LATENCY_MS,
                    "expected_direction": "not_increase",
                },
                {
                    "id": 3,
                    "metric": 903,
                    "metric_name": names.METRIC_MEAN_COST_MICRO_USD,
                    "expected_direction": "not_increase",
                },
            ],
        }
    )
    return experiment_id


@pytest.mark.parametrize(
    ("envelope", "current"),
    [
        (_envelope(), True),
        (
            _envelope(final=True, as_of="2026-11-28T05:00:00.000000Z"),
            True,
        ),  # final: as_of may be older
        (_envelope(as_of="2026-11-28T05:19:00.000000Z"), False),  # computed before the request
        (_envelope(requested="2026-11-28T05:18:59.000000Z"), False),  # an older request
        (
            _envelope(requested="2026-11-28T05:19:00.000000Z", as_of="2026-11-28T05:19:30.000000Z"),
            True,
        ),  # 60 s allowance
        (_envelope(error="2026-11-28T05:20:10.000000Z"), False),  # failed after the request
        (_envelope(error="2026-11-28T05:10:00.000000Z"), True),  # an older failure
        (_envelope(as_of=REQUESTED), True),  # computed at the request itself
        (_envelope(error=REQUESTED), False),  # failed at the request itself
        ({"results": None}, False),  # not computed yet
    ],
)
def test_a_read_is_current_by_design_md_section_7s_three_conditions(envelope, current):
    refreshed_at = datetime(2026, 11, 28, 5, 20, tzinfo=UTC)
    assert reads.is_current(envelope, refreshed_at) is current


def test_results_refreshes_once_waits_until_current_and_writes_the_file(admin, flagsmith, tmp_path):
    experiment_id = _experiment(flagsmith)
    flagsmith.results[experiment_id] = [
        {"results": None},
        _envelope(requested="2026-11-28T05:20:01.000000Z"),
    ]
    out = tmp_path / "3.6-results.json"
    waits = []

    written = reads.read_results(
        admin,
        experiment_id,
        out,
        refresh=True,
        now=lambda: datetime(2026, 11, 28, 5, 20, tzinfo=UTC),
        sleep=waits.append,
    )

    assert flagsmith.refreshes == [experiment_id]
    assert waits == [reads.POLL_SECONDS]
    recorded = json.loads(out.read_text())
    assert recorded == written
    assert recorded["results"] == _envelope(requested="2026-11-28T05:20:01.000000Z")["results"]
    # As 2.2's waiting test reads it: the experiment read's metrics, by metric_name and metric.
    assert {m["metric_name"]: m["metric"] for m in recorded["experiment_metrics"]} == {
        names.METRIC_HELPFUL_RATE: 901,
        names.METRIC_MEAN_LATENCY_MS: 902,
        names.METRIC_MEAN_COST_MICRO_USD: 903,
    }
    assert [m for m, _, _ in flagsmith.requests].count("POST") == 1  # only the refresh


def test_results_without_refresh_reads_once_and_sends_no_post(admin, flagsmith, tmp_path):
    experiment_id = _experiment(flagsmith)
    flagsmith.results[experiment_id] = [_envelope()]
    reads.read_results(
        admin, experiment_id, tmp_path / "r.json", refresh=False, sleep=lambda s: None
    )
    assert flagsmith.refreshes == []
    assert "POST" not in [m for m, _, _ in flagsmith.requests]


def test_results_without_refresh_against_a_null_reply_are_unknown(admin, flagsmith, tmp_path):
    experiment_id = _experiment(flagsmith)
    flagsmith.results[experiment_id] = [{"results": None}]
    out = tmp_path / "r.json"
    with pytest.raises(reads.Unknown, match="not computed"):
        reads.read_results(admin, experiment_id, out, refresh=False, sleep=lambda s: None)
    assert not out.exists()
    assert flagsmith.refreshes == []


def test_results_that_never_become_current_write_nothing(admin, flagsmith, tmp_path):
    experiment_id = _experiment(flagsmith)
    flagsmith.results[experiment_id] = [{"results": None}]
    out = tmp_path / "r.json"
    with pytest.raises(reads.Unknown, match="not current"):
        reads.read_results(
            admin,
            experiment_id,
            out,
            refresh=True,
            now=lambda: datetime(2026, 11, 28, 5, 20, tzinfo=UTC),
            sleep=lambda s: None,
            attempts=3,
        )
    assert not out.exists()


def test_the_summary_puts_each_arms_n_beside_its_identities():
    experiment_metrics = [{"metric": 901, "metric_name": names.METRIC_HELPFUL_RATE}]
    summary = reads.summarize(_envelope()["results"], experiment_metrics)
    assert summary["identities"] == {CURRENT: 72, CHEAPER: 78}
    [metric] = summary["metrics"]
    assert metric["metric_name"] == names.METRIC_HELPFUL_RATE
    assert metric["n"] == {CURRENT: 72, CHEAPER: 78}
    assert metric["mean"] == {CURRENT: 50.0 / 72, CHEAPER: 60.0 / 78}
    assert metric["inference"] == {
        "lift": 0.03,
        "ci_low": -0.08,
        "ci_high": 0.14,
        "chance_to_win": 0.7,
    }
    assert summary["n_counts"] == "identities"  # every metric's n equals its arm's identities


def test_nothing_printed_holds_a_key(admin, flagsmith, tmp_path, capsys):
    experiment_id = _experiment(flagsmith)
    flagsmith.results[experiment_id] = [_envelope()]
    reads.main(["usage"], admin=admin)
    reads.main(["results", str(experiment_id), "--out", str(tmp_path / "r.json")], admin=admin)
    out = capsys.readouterr().out
    assert ORG_KEY not in out and DEV_KEY not in out
    assert "150" in out and "identities" in out
