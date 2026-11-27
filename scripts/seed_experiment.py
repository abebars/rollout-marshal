"""Create and start one run's experiment without the agent, then write the pointer (task 3.5).

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=<the human's profile in 818928662830>
    uv run python scripts/seed_experiment.py <run_id>

For task 3.6's smoke, and any test that needs the router in a run without rm_agent:

1. It refuses while a pointer exists (design.md section 6), before any Flagsmith call.
2. It sets assistant_cheaper_enabled enabled through a v2 version, as launch does, unless
   it already is. scripts/bootstrap_flagsmith.py must have created the permanent features.
3. It creates assistant_model_<run_id>, refusing if it exists (an ended experiment cannot
   restart, so each run needs a new feature). The feature is multivariate with the
   control's value current, plus one option, key and value cheaper, at 50 percent.
4. It gets or creates the three metrics, checking a found one's definition.
5. It creates the experiment with its whole experiment_rollout block (rollout 100, the
   control current, cheaper at 50; Flagsmith refuses a create without it, task 1.3) and the
   metrics, helpful_rate first. Then it starts the experiment.
6. Last, it writes the pointer {pk ACTIVE, run_id, feature_key}, on condition no pointer
   exists. So the router sends traffic to the experiment only once it runs.

Every value comes from contracts/names.py. The experiment is never ended here: the human
ends it in the Flagsmith dashboard.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # contracts and scripts, when run as a script

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from contracts import names  # noqa: E402
from scripts.bootstrap_flagsmith import (  # noqa: E402
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    Admin,
    FlagsmithError,
    Refused,
    check_account,
    live_state,
    organisation_key,
)

HYPOTHESIS = (
    "Claude Sonnet 5 answers support questions as helpfully as Claude Fable 5.1, at no more"
    " latency or cost. Seeded by scripts/seed_experiment.py, without the agent."
)
POINTER_KEY = {names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}}


def active_pointer(dynamodb) -> dict | None:
    item = dynamodb.get_item(TableName=names.RUNS_TABLE, Key=POINTER_KEY, ConsistentRead=True)
    return item.get("Item")


def ensure_metric(admin: Admin, name: str) -> int:
    """A metric's ID: found by its exact name and checked, or created."""
    definition = names.METRIC_DEFINITIONS[name]
    wanted = {
        "aggregation": definition["aggregation"],
        "direction": names.METRIC_DIRECTION_API[definition["direction"]],
        "definition": {
            "version": names.API_METRIC_DEFINITION_VERSION,
            "event": definition["event"],
        },
    }
    path = admin.environment_path("experiment-metrics/")
    reply = admin.request("GET", f"{path}?q={name}")
    found = [m for m in (reply or {}).get("results", []) if m.get("name") == name]
    if len(found) > 1:
        raise FlagsmithError(f"found {len(found)} metrics named {name}")
    if found:
        metric = found[0]
        got = {key: metric.get(key) for key in wanted}
        if got["definition"] is not None:
            got["definition"] = {k: got["definition"].get(k) for k in ("version", "event")}
        if got != wanted:
            raise FlagsmithError(f"metric {name} exists but does not match its definition: {got}")
        return int(metric["id"])
    created = admin.request(
        "POST", path, {"name": name, "description": f"{name}, Rollout Marshal", **wanted}
    )
    return int(created["id"])


def experiment_body(feature_key: str, feature_id: int, option_id: int, metric_ids: dict) -> dict:
    """The experiment's create, with its whole rollout block and the metrics in order."""
    body = {
        "feature": feature_id,
        "name": feature_key,
        "hypothesis": HYPOTHESIS,
        "metrics": [
            {
                "metric": str(metric_ids[metric]),
                "expected_direction": names.EXPECTED_DIRECTION_API[
                    names.EXPERIMENT_EXPECTED_DIRECTIONS[metric]
                ],
            }
            for metric in names.METRICS  # helpful_rate first (design.md section 4)
        ],
        "experiment_rollout": {
            "enabled": True,
            "rollout_percentage": names.EXPERIMENT_ROLLOUT_PERCENT,
            "feature_state_value": {
                "type": names.API_ROLLOUT_VALUE_TYPE,
                "value": names.CONTROL_VALUE,
            },
            "multivariate_feature_state_values": [
                {
                    "multivariate_feature_option": option_id,
                    "percentage_allocation": names.TREATMENT_PERCENT,
                }
            ],
        },
    }
    forbidden = set(body) & set(names.MCP_EXPERIMENT_FORBIDDEN_FIELDS)
    if forbidden:  # never a status or rollout decision (design.md section 4)
        raise Refused(f"an experiment create never carries {sorted(forbidden)}")
    return body


def seed(run_id: str, *, admin: Admin, dynamodb, out=print) -> int:
    """Seed run_id's experiment and pointer. Returns the experiment's ID."""
    if not isinstance(run_id, str) or not names.is_run_id(run_id):
        raise Refused(f"not a run ID (expected {names.RUN_ID_PATTERN})")
    feature_key = names.experiment_feature_key(run_id)
    found = active_pointer(dynamodb)
    if found is not None:
        raise Refused(
            f"a pointer exists, naming run {found.get('run_id', {}).get('S')}; end that run"
            " (or run scripts/reset.py) first"
        )

    kill_switch = admin.find_feature(names.KILL_SWITCH_FEATURE)
    if kill_switch is None or admin.find_feature(names.DEFAULT_MODEL_FEATURE) is None:
        raise Refused(
            f"the permanent features are missing; run {names.BOOTSTRAP_FLAGSMITH_SCRIPT} first"
        )
    if admin.find_feature(feature_key) is not None:
        raise Refused(f"{feature_key} exists; each run needs a new feature, so use a new run ID")
    if live_state(kill_switch)[0] is not True:
        admin.set_state(kill_switch, enabled=True)
        out(f"enabled {names.KILL_SWITCH_FEATURE}")

    feature = admin.create_feature(
        feature_key, names.API_FEATURE_MULTIVARIATE, enabled=True, value=names.CONTROL_VALUE
    )
    option = admin.request(
        "POST",
        admin.features_path(f"{feature['id']}/mv-options/"),
        {
            "feature": feature["id"],
            "type": names.API_OPTION_TYPE,
            "string_value": names.TREATMENT_OPTION_VALUE,
            "key": names.TREATMENT_OPTION_KEY,
            "default_percentage_allocation": names.TREATMENT_PERCENT,
        },
    )
    metric_ids = {metric: ensure_metric(admin, metric) for metric in names.METRICS}
    experiments = admin.environment_path("experiments/")
    created = admin.request(
        "POST", experiments, experiment_body(feature_key, feature["id"], option["id"], metric_ids)
    )
    experiment_id = int(created["id"])
    started = admin.request(
        "POST",
        f"{experiments}{experiment_id}/start/",
        {"name": feature_key, "hypothesis": HYPOTHESIS},
    )
    if (started or {}).get("status") != names.EXPERIMENT_STATUS_RUNNING:
        raise FlagsmithError(
            f"experiment {experiment_id} did not start: {(started or {}).get('status')}"
        )
    out(f"started experiment {experiment_id} on {feature_key}")

    item = {
        **POINTER_KEY,
        "run_id": {"S": run_id},
        "feature_key": {"S": feature_key},
    }
    try:
        dynamodb.put_item(
            TableName=names.RUNS_TABLE,
            Item=item,
            ConditionExpression=names.POINTER_CREATE_CONDITION,
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        raise FlagsmithError(
            f"a pointer appeared meanwhile, so none was written; experiment {experiment_id} is"
            " running with no traffic: end it in the Flagsmith dashboard"
        ) from None
    out(f"wrote the pointer: run {run_id}, feature {feature_key}")
    return experiment_id


def main(
    argv: list[str] | None = None, *, admin: Admin | None = None, dynamodb=None, out=print
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        if not names.is_run_id(args.run_id):
            raise Refused(f"not a run ID (expected {names.RUN_ID_PATTERN})")
        if admin is None or dynamodb is None:
            check_account()
            dynamodb = dynamodb or boto3.client("dynamodb", region_name=names.REGION)
            admin = admin or Admin(organisation_key())
        seed(args.run_id, admin=admin, dynamodb=dynamodb, out=out)
    except Refused as err:
        out(f"refused: {err}")
        return EXIT_REFUSED
    except (FlagsmithError, ClientError) as err:
        out(f"failed: {err}")
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
