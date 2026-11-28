"""Scratch, read-only (task 3.6): the organisation's usage, day by day, from the API task 1.3
found (GET usage-data/, a Management API read, which Flagsmith's billing docs list as not
billable). Which days does it hold yet?

    uv run python usage_days.py <worktree root>
"""

import json
import sys

sys.path.insert(0, sys.argv[1])

from contracts import names  # noqa: E402
from scripts.bootstrap_flagsmith import Admin, check_account, organisation_key  # noqa: E402

check_account()
admin = Admin(organisation_key())
base = f"/api/v1/organisations/{names.FLAGSMITH_ORGANISATION_ID}/usage-data/"
for query in ("", "?period=current_billing_period"):
    rows = admin.request("GET", base + query)
    print(f"usage-data/{query}: {json.dumps(rows)}")
