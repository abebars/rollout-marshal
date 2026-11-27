"""Read one Flagsmith experiment back, read-only (task 4.3's fault run).

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=<the SSO profile in 818928662830>
    uv run python evidence/4.3-scripts/read_experiment.py <experiment id>

GET only, through scripts/bootstrap_flagsmith.py's Admin client: Development's environment
(one GET), then the experiment (one GET). It never sends an end, complete, pause or rollout
call. The organisation key is read in-process from Secrets Manager and never printed; the
environment key is in the request path, which is never printed either. It prints only the
experiment's id, status, ended_at and rollout_decision, raw.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bootstrap_flagsmith import Admin, check_account, organisation_key  # noqa: E402

FIELDS = ("id", "status", "ended_at", "rollout_decision")


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print("usage: read_experiment.py <experiment id>", file=sys.stderr)
        return 2
    check_account()
    admin = Admin(organisation_key())
    reply = admin.request("GET", admin.environment_path(f"experiments/{int(argv[0])}/"))
    print(json.dumps({field: (reply or {}).get(field) for field in FIELDS}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
