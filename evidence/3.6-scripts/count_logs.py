"""Scratch, read-only (task 3.6): count log events by pattern, with every page followed.

    uv run python count_logs.py <log group> <start ISO> <pattern>...
"""

import sys
from datetime import datetime

import boto3

group, start = sys.argv[1], datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
logs = boto3.client("logs", region_name="us-east-1")
paginator = logs.get_paginator("filter_log_events")
for pattern in sys.argv[3:]:
    count = 0
    for page in paginator.paginate(
        logGroupName=group, startTime=int(start.timestamp() * 1000), filterPattern=pattern
    ):
        count += len(page.get("events", []))
    print(f"{group} {pattern}: {count}")
