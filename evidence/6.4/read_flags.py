"""Read-only, for evidence/6.4: the permanent flags' live state, the recorded run's feature,
and any running experiment. It reads the organisation key from Secrets Manager, as
scripts/reset.py does, and prints no key. Run from the repository root:

    uv run python evidence/6.4/read_flags.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "agent" / "runtime"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import boto3  # noqa: E402

from contracts import names  # noqa: E402
from scripts.bootstrap_flagsmith import (  # noqa: E402
    Admin,
    check_account,
    live_state,
    organisation_key,
)
from scripts.reset import running_experiments  # noqa: E402

RUN_FEATURE = "assistant_model_r202611280335"


def main() -> int:
    check_account(boto3.client("sts"))
    admin = Admin(organisation_key())
    features = (
        names.KILL_SWITCH_FEATURE,
        names.DEFAULT_MODEL_FEATURE,
        names.CHAOS_FEATURE,
        RUN_FEATURE,
    )
    for name in features:
        feature = admin.find_feature(name)
        if feature is None:
            print(f"{name}: does not exist")
        else:
            enabled, value = live_state(feature)
            print(f"{name}: enabled={enabled} value={value!r}")
    print("running experiments:", [e.get("id") for e in running_experiments(admin)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
