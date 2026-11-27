"""Scratch, read-only (task 3.6): did rm-answer-cheaper carry the FIS extension through the
baseline, unchanged since the apply? Prints LastModified, the layers' names and versions,
the environment's variable names, and the exec wrapper's value only.

    uv run python cheaper_config.py <worktree root>
"""

import sys

sys.path.insert(0, sys.argv[1])

import boto3  # noqa: E402

from scripts.bootstrap_flagsmith import check_account  # noqa: E402

check_account()
config = boto3.client("lambda", region_name="us-east-1").get_function_configuration(
    FunctionName="rm-answer-cheaper"
)
env = config.get("Environment", {}).get("Variables", {})
print("LastModified:", config["LastModified"])
print("Layers:", [layer["Arn"].split(":")[-2:] for layer in config.get("Layers", [])])
print("Environment names:", sorted(env))
print("AWS_LAMBDA_EXEC_WRAPPER:", env.get("AWS_LAMBDA_EXEC_WRAPPER"))
print("State:", config.get("State"), "LastUpdateStatus:", config.get("LastUpdateStatus"))
