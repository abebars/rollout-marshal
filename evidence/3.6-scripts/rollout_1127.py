"""Scratch, read-only (task 3.6): experiment 1127's rollout block and decision fields.

    uv run python rollout_1127.py <worktree root>
"""

import json
import sys

sys.path.insert(0, sys.argv[1])

from scripts.bootstrap_flagsmith import Admin, check_account, organisation_key  # noqa: E402

check_account()
admin = Admin(organisation_key())
experiment = admin.request("GET", admin.environment_path("experiments/1127/")) or {}
keep = ("experiment_rollout", "rollout_decision", "rollout_variant", "hypothesis", "status")
print(json.dumps({k: experiment.get(k) for k in keep}, indent=1))
