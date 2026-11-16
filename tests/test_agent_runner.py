"""Offline tests for the agent runtime (no network, no model calls)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import httpx

from lore.agent import runner


class FakeSession:
    def __init__(self):
        self.called = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name="kb_read", description="read a page",
                                                      inputSchema={"type": "object", "properties": {"path": {"type": "string"}}})])

    async def call_tool(self, name, args):
        self.called.append((name, args))
        return SimpleNamespace(content=[SimpleNamespace(text="Managed Security Services (raw/x.md#L3)")], isError=False)


def test_anthropic_tool_loop_runs_tools_then_answers():
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        requests.append(body)
        assert req.url.path == "/anthropic/v1/messages"
        assert body["tools"][0] == {"name": "kb_read", "description": "read a page",
                                    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}
        if len(requests) == 1:
            return httpx.Response(200, json={"stop_reason": "tool_use", "content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "tu_1", "name": "kb_read", "input": {"path": "accounts/ACC-0001.md"}}]})
        last = body["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["type"] == "tool_result"
        assert last["content"][0]["tool_use_id"] == "tu_1" and "raw/x.md#L3" in last["content"][0]["content"]
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": "Sell MSS (raw/x.md#L3)."}]})

    session = FakeSession()

    async def go():
        async with httpx.AsyncClient(base_url="https://gw.test/anthropic", transport=httpx.MockTransport(handler)) as http:
            return await runner.anthropic_tool_loop(session, http, "what to sell?", "sys", 0.0, model="m")

    res = anyio.run(go)
    assert res.answer == "Sell MSS (raw/x.md#L3)." and res.error is None
    assert res.tool_calls == ["kb_read"]
    assert session.called == [("kb_read", {"path": "accounts/ACC-0001.md"})]
    assert requests[0]["system"] == "sys" and requests[0]["model"] == "m"


def test_anthropic_tool_loop_raises_on_http_error():
    async def go():
        transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "unknown key"}))
        async with httpx.AsyncClient(base_url="https://gw.test", transport=transport) as http:
            return await runner.anthropic_tool_loop(FakeSession(), http, "q", "sys", 0.0, model="m")

    try:
        anyio.run(go)
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_fallback_chain_reports_both_providers(monkeypatch):
    def fake_one(provider, *a, **k):
        if provider == "claude":
            return runner.AgentResult("", "claude", 1.0, error="claude CLI error (api_error)")
        return runner.AgentResult("answer", provider, 1.0, ["kb_read"])

    monkeypatch.setattr(runner, "_run_one", fake_one)
    monkeypatch.setattr(runner.config, "CLAUDE_FALLBACK", ["anthropic", "codex"])
    res = runner.run("q", user="OWN-002", provider="claude")
    assert res.provider == "claude->anthropic" and res.answer == "answer" and res.error is None


def test_no_fallback_after_tool_calls(monkeypatch):
    seen = []

    def fake_one(provider, *a, **k):
        seen.append(provider)
        return runner.AgentResult("", provider, 1.0, ["kb_write_event"], error="boom")

    monkeypatch.setattr(runner, "_run_one", fake_one)
    monkeypatch.setattr(runner.config, "LLM_CHAIN", ["anthropic", "codex"])
    res = runner.run("q", user="OWN-002", provider="auto")
    assert seen == ["anthropic"] and res.error and res.provider == "anthropic"

def test_run_async_works_inside_running_event_loop():
    async def inner(x):
        return x * 2

    async def outer():
        return runner._run_async(inner, 21)

    assert anyio.run(outer) == 42
