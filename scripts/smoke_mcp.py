"""MCP smoke test: start the internal and public servers over stdio, list tools and call the read paths.

Run: uv run python scripts/smoke_mcp.py
"""
from __future__ import annotations

import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _text(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


async def _session(module: str, env: dict, checks):
    params = StdioServerParameters(command=sys.executable, args=["-m", module], env={**os.environ, **env}, cwd=ROOT)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = sorted(t.name for t in (await s.list_tools()).tools)
        print(f"\n== {module} {env} -> {len(names)} tools")
        print("  " + ", ".join(names))
        return await checks(s, names)


async def main() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    async def internal_read(s, names):
        check("kb_add_event" not in names and "action_propose_meeting" not in names, "read scope exposes no write/action tools")
        acc = _text(await s.call_tool("kb_get_account", {"account": "ACC-0001"}))
        check("ACC-0001" in acc and "raw/" in acc, "kb_get_account ACC-0001 returns a cited front page")
        denied = _text(await s.call_tool("kb_get_account", {"account": "ACC-0002"}))
        check("not allowed" in denied.lower() or "denied" in denied.lower() or "error" in denied.lower(), "OWN-002 is denied an account it does not own")
        bridge = _text(await s.call_tool("insight_ops_bridge", {"account": "ACC-0001"}))
        check("p-demo-mss" in bridge and "service_recovery_first" in bridge, "ops bridge maps TKT-9001 to p-demo-mss with recovery gate")
        crm = _text(await s.call_tool("crm_get_opportunities", {"account": "ACC-0001"}))
        check("backend" in crm, "crm_get_opportunities answers")

    async def internal_all(s, names):
        check({"kb_add_event", "crm_update_record", "action_propose_meeting"} <= set(names), "all scope exposes write + action tools")

    async def public(s, names):
        need = {"search", "fetch", "lore_account", "lore_transcripts", "lore_crm", "lore_crm_update", "lore_ask", "lore_ingest",
                "lore_account_map", "lore_ops_bridge", "lore_who_to_call_this_week", "lore_schedule_meeting"}
        check(need <= set(names), "public MCP exposes the ChatGPT tool set")
        prompts = sorted(p.name for p in (await s.list_prompts()).prompts)
        check({"weekly_call_plan", "meeting_prep", "grow_account", "ops_to_sales"} <= set(prompts), f"playbook prompts {prompts}")
        mp = await s.get_prompt("meeting_prep", {"account": "ACC-0001"})
        check("ACC-0001" in mp.messages[0].content.text, "meeting_prep prompt fills the account")
        res = json.loads(_text(await s.call_tool("search", {"query": "monitoring"})))
        check(len(res.get("results", [])) > 0, f"search returns results ({len(res.get('results', []))})")
        if res.get("results"):
            f = json.loads(_text(await s.call_tool("fetch", {"id": res["results"][0]["id"]})))
            check("text" in f, "fetch opens a search hit")
        calls = json.loads(_text(await s.call_tool("lore_who_to_call_this_week", {})))
        check(len(calls.get("calls", [])) > 0, "who-to-call ranks accounts")
        tr = json.loads(_text(await s.call_tool("lore_transcripts", {"account": "ACC-0001"})))
        check(len(tr.get("transcripts", [])) > 0, f"transcripts for ACC-0001 ({len(tr.get('transcripts', []))})")
        crm = json.loads(_text(await s.call_tool("lore_crm", {"account": "ACC-0001"})))
        check(crm.get("account") is not None, f"lore_crm reads the CRM ({crm.get('backend')})")
        m = json.loads(_text(await s.call_tool("lore_account_map", {"account": "ACC-0001"})))
        check("mermaid" in json.dumps(m).lower() or "graph" in json.dumps(m), "account map returns Mermaid")
        d = json.loads(_text(await s.call_tool("lore_account", {"account": "ACC-0002"})))
        check("error" in d, "public MCP enforces account scope")
        bad = json.loads(_text(await s.call_tool("lore_crm_update", {"object": "Opportunity", "record_id": "OPP-0001",
                                                                      "fields": {"OwnerId": "x"}, "reason": "smoke"})))
        check("error" in bad, "CRM update rejects a non-allowlisted field")

    await _session("lore.internal_mcp", {"LORE_AGENT_SCOPE": "read", "LORE_USER": "OWN-002"}, internal_read)
    await _session("lore.internal_mcp", {"LORE_AGENT_SCOPE": "all", "LORE_USER": "OWN-002"}, internal_all)
    await _session("lore.public_mcp", {"LORE_USER": "OWN-002"}, public)
    print(f"\n{'ALL OK' if not failures else 'FAILURES: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
