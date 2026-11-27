"""Scratch (task 3.6): what evidence/3.6-results.json holds, and the interval level its
confidence intervals imply, worked out from the file's own n, sum and sum_squares.

    python3 -I inspect_results.py <results json>
"""

import json
import math
import sys
from statistics import NormalDist

record = json.load(open(sys.argv[1]))
print("top-level keys:", sorted(record))
print("experiment_metrics:", [(m["metric_name"], m["metric"]) for m in record["experiment_metrics"]])
envelope = record["results"]
print({k: envelope[k] for k in ("as_of", "refresh_requested_at", "is_final", "last_error_at")})
payload = envelope["payload"]
print("payload keys:", sorted(payload), "srm_p_value:", payload.get("srm_p_value"))
print("exposures:", payload["exposures_timeseries"])
names = {m["metric"]: m["metric_name"] for m in record["experiment_metrics"]}
for metric in payload["metrics"]:
    name = names[metric["metric_id"]]
    v = metric["variants"]
    print(f"\n{name}: variants {json.dumps(v)}")
    inference = metric["inference"]["cheaper"]
    print(f"  inference {json.dumps(inference)}")
    stats = {}
    for arm in ("control", "cheaper"):
        n, s, ss = v[arm]["n"], v[arm]["sum"], v[arm]["sum_squares"]
        mean = s / n
        var = (ss - n * mean * mean) / (n - 1) if n > 1 else 0.0
        stats[arm] = (n, mean, var)
    (n0, m0, v0), (n1, m1, v1) = stats["control"], stats["cheaper"]
    lift = m1 / m0 - 1
    # The delta method's standard error of the relative lift m1/m0 - 1.
    se = math.sqrt(v1 / n1 / m0**2 + (m1**2) * v0 / n0 / m0**4)
    print(f"  means: control {m0:.6g}, cheaper {m1:.6g}; lift {lift:.6g} (reported {inference['lift']:.6g})")
    if se > 0:
        half = (inference["ci_high"] - inference["ci_low"]) / 2
        z = half / se
        print(f"  half-width {half:.6g}, delta-method se {se:.6g}: z = {z:.4f}, two-sided level {2 * NormalDist().cdf(z) - 1:.4f}")
