"""Tests for evidence/3.6-scripts/smoke_reads.py, task 3.6's reads, prepared before the apply.

CloudWatch is botocore's Stubber on a real client, which checks each request against the
service model and against the exact parameters expected. Flagsmith is
app/tests/flagsmith_fake.py. Nothing leaves the machine.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
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


def _expect_cost(cloudwatch, variant: str, datapoints: list[dict]) -> None:
    cloudwatch.stubber.add_response(
        "get_metric_statistics",
        {"Label": names.CW_METRIC_ANSWER_COST_MICRO_USD, "Datapoints": datapoints},
        {
            "Namespace": names.CW_NAMESPACE,
            "MetricName": names.CW_METRIC_ANSWER_COST_MICRO_USD,
            "Dimensions": [{"Name": names.CW_DIMENSION_VARIANT, "Value": variant}],
            "StartTime": SINCE,
            "EndTime": ANY,
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


def test_the_projection_is_spent_plus_the_remaining_answers_at_the_higher_mean():
    costs = {
        CURRENT: {"sum": 3_300_000.0, "count": 150.0, "mean": 22_000.0},
        CHEAPER: {"sum": 660_000.0, "count": 150.0, "mean": 4_400.0},
    }

    projected = reads.projection(costs, answers_sent=300, expected_failures=1)

    rehearsal = names.LOAD_SMOKE.users * names.LOAD_SMOKE.questions
    remaining = names.ANSWER_BUDGET - 300 + rehearsal  # 4,800 planned + one 150 x 2
    assert projected["spent_micro_usd"] == 3_960_000
    assert projected["higher_mean_micro_usd"] == 22_000.0
    assert projected["remaining_answers"] == remaining == 5_100
    assert projected["projected_micro_usd"] == 3_960_000 + remaining * 22_000
    assert projected["answers_in_all"] == 300 + remaining
    assert projected["ask_the_human"] is True  # 116,160,000 passes 60,000,000
    assert projected["reasons"] == [f"the projection passes {names.ANSWER_COST_STOP_MICRO_USD:,}"]


def test_the_projection_asks_when_the_answers_would_pass_7500():
    costs = {
        CURRENT: {"sum": 0.0, "count": 1.0, "mean": 1.0},
        CHEAPER: {"sum": 0.0, "count": 1.0, "mean": 1.0},
    }
    projected = reads.projection(costs, answers_sent=5_000, expected_failures=8)
    assert projected["answers_in_all"] == 5_000 + 100 + 8 * 300 == 7_500
    assert projected["ask_the_human"] is False  # 7,500 does not pass 7,500
    projected = reads.projection(costs, answers_sent=5_000, expected_failures=9)
    assert projected["ask_the_human"] is True
    assert projected["reasons"] == [f"the answers pass {names.ANSWER_HARD_STOP:,}"]


def test_a_smoke_well_under_the_limits_needs_no_question():
    costs = {
        CURRENT: {"sum": 3_400_000.0, "count": 300.0, "mean": 11_333.3},
        CHEAPER: {"sum": 1.0, "count": 1.0, "mean": 1.0},
    }
    projected = reads.projection(costs, answers_sent=300, expected_failures=0)
    assert projected["projected_micro_usd"] == round(3_400_001 + 4_800 * 11_333.3)
    assert projected["ask_the_human"] is False and projected["reasons"] == []


# ---------------------------------------------------------------------------
# The baseline latency
# ---------------------------------------------------------------------------
def test_the_baseline_is_p95_of_exactly_variant_cheaper_over_the_window(cloudwatch):
    start = datetime(2026, 11, 28, 5, 0, 30, tzinfo=UTC)
    end = datetime(2026, 11, 28, 5, 11, 0, tzinfo=UTC)
    window_start = datetime(2026, 11, 28, 5, 0, tzinfo=UTC)  # CloudWatch's minute
    common = {
        "Namespace": names.CW_NAMESPACE,
        "MetricName": names.CW_METRIC_ANSWER_LATENCY_MS,
        "StartTime": window_start,
        "EndTime": end,
        "Period": 660,  # one bucket for the whole window
    }
    cloudwatch.stubber.add_response(
        "get_metric_statistics",
        {"Datapoints": [{"Timestamp": window_start, "ExtendedStatistics": {"p95": 3_450.0}}]},
        {
            **common,
            "Dimensions": [{"Name": "Variant", "Value": "cheaper"}],
            "ExtendedStatistics": ["p95"],
        },
    )
    for variant in names.VARIANTS:
        cloudwatch.stubber.add_response(
            "get_metric_statistics",
            {"Datapoints": [{"Timestamp": window_start, "SampleCount": 150.0}]},
            {
                **common,
                "Dimensions": [{"Name": "Variant", "Value": variant}],
                "Statistics": ["SampleCount"],
            },
        )

    baseline = reads.latency_baseline(cloudwatch, start, end)

    assert baseline["p95_ms"] == 3_450.0
    assert baseline["samples"] == {CURRENT: 150.0, CHEAPER: 150.0}
    # 4.1's recalibration: the p95, plus half the fault, rounded up to the next 1000.
    assert baseline["threshold_ms"] == 9_000
    assert baseline["ask_the_human"] is False


def test_a_baseline_over_8000_ms_is_flagged_for_the_human():
    assert reads.recalibrated_threshold(8_001.0) == (14_000, True)
    assert reads.recalibrated_threshold(8_000.0) == (13_000, False)
    assert reads.recalibrated_threshold(4_000.0) == (9_000, False)  # exactly 9,000: no rounding up


def test_no_latency_datapoint_is_unknown(cloudwatch):
    start, end = datetime(2026, 11, 28, 5, tzinfo=UTC), datetime(2026, 11, 28, 5, 10, tzinfo=UTC)
    cloudwatch.stubber.add_response("get_metric_statistics", {"Datapoints": []})
    with pytest.raises(reads.Unknown, match="p95"):
        reads.latency_baseline(cloudwatch, start, end)


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
