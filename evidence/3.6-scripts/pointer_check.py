"""Scratch, read-only (task 3.6): is the active-run pointer gone? A consistent read of the
pointer item in rm-runs, through reset.py's own live_run.

    uv run python pointer_check.py <worktree root>
"""

import sys

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, f"{sys.argv[1]}/agent/runtime")

import boto3  # noqa: E402

from contracts import names  # noqa: E402
from scripts.bootstrap_flagsmith import check_account  # noqa: E402
from scripts.reset import POINTER_KEY, live_run  # noqa: E402

check_account()
dynamodb = boto3.client("dynamodb", region_name=names.REGION)
item = dynamodb.get_item(TableName=names.RUNS_TABLE, Key=POINTER_KEY, ConsistentRead=True)
print(f"{names.RUNS_TABLE} pointer item present: {'Item' in item}")
print(f"live_run: {live_run(dynamodb)}")
