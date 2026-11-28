"""rm-invoker's InvokeAgentRuntime client, with design.md section 7's one client setting.

design.md section 7: read timeout 290 seconds and one attempt. That sits just under
rm-invoker's own 300 second timeout, so the client normally gives up before Lambda stops the
function, and Lambda's retry of the asynchronous event is the only retry. In botocore these
are two Config fields (docs.aws.amazon.com/botocore/latest/reference/config.html):

- read_timeout: "The time in seconds till a timeout exception is thrown when attempting to
  read from a connection."
- retries["total_max_attempts"]: "the maximum number of total attempts that will be made on
  a single request. This includes the initial request, so a value of 1 indicates that no
  requests will be retried."

An explicit total_max_attempts wins over AWS_MAX_ATTEMPTS and the config file (botocore
args.py, _compute_retry_max_attempts), so the environment cannot add retries back. Every
other setting, connect_timeout included, stays at botocore's default.

The marshal CLI (task 2.6) builds its own Config from the same contracts.names constants;
nothing outside signals/ imports this module.
"""

from __future__ import annotations

import boto3
from botocore.config import Config

from contracts.names import AGENT_CLIENT_MAX_ATTEMPTS, AGENT_CLIENT_READ_TIMEOUT_SECONDS, REGION

AGENT_CLIENT_SERVICE = "bedrock-agentcore"  # the boto3 client that has invoke_agent_runtime


def agent_client_config() -> Config:
    """A new Config with the shared setting.

    A new object each call: botocore normalises the retries dict in place when it builds a
    client, so one shared instance would change under its other users.
    """
    return Config(
        read_timeout=AGENT_CLIENT_READ_TIMEOUT_SECONDS,
        retries={"total_max_attempts": AGENT_CLIENT_MAX_ATTEMPTS},
    )


def new_agent_client(session: boto3.session.Session | None = None):
    """A bedrock-agentcore client in us-east-1 with the shared setting.

    session supplies the credentials; without one, the default session's (rm-invoker's role).
    The region is always REGION, whatever the session or the environment says.
    """
    session = session if session is not None else boto3.session.Session()
    return session.client(AGENT_CLIENT_SERVICE, region_name=REGION, config=agent_client_config())
