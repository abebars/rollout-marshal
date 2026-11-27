"""rm_agent's AgentCore Runtime entrypoint (design.md section 7; tasks.md 2.5).

Each invocation is one payload, handed to rm_agent.handlers.handle with the real tools. The
Flagsmith tools reach the Gateway rm-gateway over MCP, signed with the runtime role's
credentials (SigV4, through mcp-proxy-for-aws, as task 1.3's spike did). That session opens
the first time a Flagsmith tool is used, so status, and every tick with nothing to do in
Flagsmith, opens none. It closes when the invocation ends.
"""

from __future__ import annotations

import contextlib
import functools
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from rm_agent import handlers, planner
from rm_agent.tools import flagsmith, notify, probe, report, schedule, signals

from contracts import names

SIGV4_SERVICE = "bedrock-agentcore"  # the Gateway's signing name (task 1.3's spike)


@functools.cache
def gateway_url() -> str:
    """rm-gateway's URL, found by name once per container."""
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=names.REGION)
    token = None
    while True:
        page = control.list_gateways(**({"nextToken": token} if token else {}))
        for item in page.get("items", []):
            if item.get("name") == names.GATEWAY_NAME:
                return control.get_gateway(gatewayIdentifier=item["gatewayId"])["gatewayUrl"]
        token = page.get("nextToken")
        if not token:
            raise LookupError(f"gateway {names.GATEWAY_NAME} not found in {names.REGION}")


def gateway_client():
    """An MCP client for rm-gateway; entering it opens the session."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client  # in the CodeZip only
    from strands.tools.mcp import MCPClient

    url = gateway_url()
    return MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=url, aws_service=SIGV4_SERVICE, aws_region=names.REGION
        )
    )


class LazyFlagsmith:
    """tools.flagsmith.Flagsmith, made with its Gateway session on first use."""

    def __init__(self, stack: contextlib.ExitStack) -> None:
        self._stack = stack
        self._flagsmith: flagsmith.Flagsmith | None = None

    def open(self) -> flagsmith.Flagsmith:
        if self._flagsmith is None:
            client = self._stack.enter_context(gateway_client())
            self._flagsmith = flagsmith.Flagsmith(
                flagsmith.Mcp(client), flagsmith.GatewayAdmin(client)
            )
        return self._flagsmith

    def __getattr__(self, name: str) -> Any:
        return getattr(self.open(), name)


@contextlib.contextmanager
def real_tools():
    """The tools of one invocation; the Gateway session, if one was opened, closes after."""
    with contextlib.ExitStack() as stack:
        yield handlers.Tools(
            flagsmith=LazyFlagsmith(stack),
            alarm_state=signals.alarm_state,
            probe_cheaper=probe.probe_cheaper,
            set_tick=schedule.set_tick,
            notify=notify.notify,
            write_report=report.write_report,
            make_plan=planner.make_plan,
            converse=planner.converse,
        )


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: Any) -> dict:
    with real_tools() as tools:
        return handlers.handle(payload, tools)


if __name__ == "__main__":
    app.run()
