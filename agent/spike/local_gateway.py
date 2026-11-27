"""Run rm_agent's Flagsmith code on the laptop against rm-gateway, signed with SigV4.

Task 1.3 used it twice: to list the Gateway's tools (evidence/1.3-mcp-tools.json) and for
step 5's API-key reads. It runs the same agent/spike/agent.py code as the runtime, but with
the caller's own AWS credentials, so a Gateway problem can be told from a runtime problem.
It needs the spike's own environment, which has mcp-proxy-for-aws:

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    export AWS_PROFILE=abisb_IsbAdminsPS-818928662830
    cd agent/spike
    uv run --frozen python -I local_gateway.py tools ../../evidence/1.3-mcp-tools.json
    uv run --frozen python -I local_gateway.py handle \
      '{"action":"read","tool":"list_organizations"}' out.json

The Gateway URL is found by name (rm-gateway) unless --url is given. ``handle`` writes
agent.handle's own reply, redacted as it always is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

try:  # imported by the unit tests, from the repo root
    from agent.spike import agent as spike
except ImportError:  # run as a script from agent/spike, beside agent.py and contracts/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import agent as spike  # type: ignore[no-redef]


def gateway_client(url: str) -> Any:
    """An MCP client for the Gateway URL, signed with the caller's credentials."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client  # spike environment only

    return spike.MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=url, aws_service=spike.SIGV4_SERVICE, aws_region=spike.names.REGION
        )
    )


def tools_document(tools: spike.FlagsmithTools, url: str) -> dict:
    listed = tools.list_tools()
    return {
        "source": (
            f"tools/list on {spike.names.GATEWAY_NAME} ({url}), signed with SigV4 by "
            f"mcp-proxy-for-aws, {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}; listed through "
            "agent/spike/agent.py FlagsmithTools.list_tools"
        ),
        "count": len(listed),
        "tools": sorted(listed, key=lambda tool: tool["name"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="the Gateway's MCP URL; found by name when omitted")
    sub = parser.add_subparsers(dest="command", required=True)
    tools_cmd = sub.add_parser("tools", help="write the Gateway's tool list")
    tools_cmd.add_argument("out")
    handle_cmd = sub.add_parser("handle", help="run one agent payload; write its reply")
    handle_cmd.add_argument("payload")
    handle_cmd.add_argument("out")
    args = parser.parse_args(argv)

    url = args.url or spike.gateway_url()
    client = gateway_client(url)
    tools = spike.FlagsmithTools(client)
    if args.command == "tools":
        with client:
            document = tools_document(tools, url)
        result, ok = document, True
        print(document["count"], "tools")
    else:
        result = spike.handle(json.loads(args.payload), tools=tools)
        ok = bool(result.get("ok"))
        print(json.dumps(result, default=str)[:3000])
    with open(args.out, "w") as handle:
        json.dump(result, handle, indent=1, sort_keys=True, default=str)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
