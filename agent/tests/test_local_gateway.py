"""Unit tests for agent/spike/local_gateway.py (task 1.3): a fake MCP client, no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.spike import agent as spike
from agent.spike import local_gateway
from contracts import names


class _Page(list):
    def __init__(self, items, token=None):
        super().__init__(items)
        self.pagination_token = token


class _FakeClient:
    """Two Gateway tools; context manager like strands' MCPClient."""

    def __init__(self):
        self.entered = 0
        self.calls = []

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False

    def list_tools_sync(self, pagination_token=None):
        return _Page(
            [
                SimpleNamespace(
                    mcp_tool=SimpleNamespace(
                        name=spike.GATEWAY_TOOL_PREFIX + name,
                        description=name,
                        input_schema={"type": "object", "properties": {}, "required": []},
                        annotations=None,
                    )
                )
                for name in ("list_organizations", "get_project")
            ]
        )

    def call_tool_sync(self, tool_use_id, name, arguments=None):
        self.calls.append((name, arguments))
        return {
            "status": "success",
            "toolUseId": tool_use_id,
            "content": [{"text": '{"count": 1}'}],
        }


def test_tools_writes_every_listed_tool_sorted(tmp_path, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(local_gateway, "gateway_client", lambda url: fake)
    out = tmp_path / "tools.json"
    assert local_gateway.main(["--url", "https://gateway.example/mcp", "tools", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["count"] == 2
    assert [tool["name"] for tool in written["tools"]] == [
        spike.GATEWAY_TOOL_PREFIX + "get_project",
        spike.GATEWAY_TOOL_PREFIX + "list_organizations",
    ]
    assert names.GATEWAY_NAME in written["source"]
    assert fake.entered == 1
    assert fake.calls == []  # listing tools calls none


def test_handle_writes_the_agents_own_reply(tmp_path, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(local_gateway, "gateway_client", lambda url: fake)
    out = tmp_path / "reply.json"
    payload = '{"action": "read", "tool": "list_organizations"}'
    assert (
        local_gateway.main(["--url", "https://gateway.example/mcp", "handle", payload, str(out)])
        == 0
    )
    reply = json.loads(out.read_text())
    assert reply["ok"] is True
    assert reply["result"] == {
        "tool": spike.GATEWAY_TOOL_PREFIX + "list_organizations",
        "result": {"count": 1},
    }
    assert fake.calls == [(spike.GATEWAY_TOOL_PREFIX + "list_organizations", {})]


def test_handle_refuses_what_the_agent_refuses(tmp_path, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(local_gateway, "gateway_client", lambda url: fake)
    out = tmp_path / "reply.json"
    payload = '{"action": "read", "tool": "create_feature"}'
    assert (
        local_gateway.main(["--url", "https://gateway.example/mcp", "handle", payload, str(out)])
        == 1
    )
    assert json.loads(out.read_text())["error"]["code"] == names.ERROR_BAD_REQUEST
    assert fake.calls == []
