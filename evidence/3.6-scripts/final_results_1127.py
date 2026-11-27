"""Scratch, read-only (task 3.6): experiment 1127's results envelope after its end (a GET
only, no refresh), against the read stored in evidence/3.6-results.json.

    uv run python final_results_1127.py <worktree root> <results json>
"""

import json
import sys

sys.path.insert(0, sys.argv[1])

from scripts.bootstrap_flagsmith import Admin, check_account, organisation_key  # noqa: E402

check_account()
admin = Admin(organisation_key())
reply = admin.request("GET", admin.environment_path("experiments/1127/results/")) or {}
envelope = reply.get("results") or {}
keep = ("as_of", "refresh_requested_at", "refreshed_at", "is_final", "last_error_at")
print(json.dumps({k: envelope.get(k) for k in keep}))
stored = json.load(open(sys.argv[2]))["results"]
print("envelope keys:", sorted(envelope))
print("payload same as the stored read:", envelope.get("payload") == stored.get("payload"))
