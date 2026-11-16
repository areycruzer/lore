"""Lore demo: the ACC-0001 story, driven through the public MCP exactly as ChatGPT would call it.

  uv run python scripts/demo.py               fast, deterministic steps (about 5 seconds)
  uv run python scripts/demo.py --agent       also runs the internal agent: Q&A, brief, and ingest of a Vexa transcript

Reseed first for a clean run: uv run python -m lore.kb.seed --reset
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
ACC = "ACC-0001"


def say(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


def show(obj, width: int = 2400) -> None:
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str)
    print(textwrap.shorten(text, width=width, placeholder=" ...") if len(text) > width and "\n" not in text else text[:width])


async def main(with_agent: bool) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "lore.public_mcp"],
                                   env={**os.environ, "LORE_USER": os.environ.get("LORE_USER", "OWN-002")}, cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def call(tool: str, **args):
                res = await s.call_tool(tool, args)
                body = "\n".join(getattr(c, "text", "") for c in res.content)
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    return body

            tools = (await s.list_tools()).tools
            say(f"0. ChatGPT connects to Lore's public MCP: {len(tools)} tools")
            print(", ".join(t.name for t in tools))

            say("1. Monday morning: 'Who should I call this week?'  (lore_who_to_call_this_week)")
            calls = await call("lore_who_to_call_this_week")
            print(f"For {calls['for']}, week of {calls['week_of']}:")
            for c in calls["calls"][:5]:
                show({k: c.get(k) for k in ("account", "name", "score", "why", "call", "gate") if k in c}, 900)

            say("2. Ops -> AM bridge: what did the customer's engineers ask Ops for?  (lore_ops_bridge)")
            bridge = (await call("lore_ops_bridge", account=ACC))["items"]
            for b in bridge:
                print(f"- {b['for_the_am']}\n  product: {b['product']} | gate: {b['gate']} | ticket {b['ticket']} | source {b['source']}")
            if bridge:
                src = await call("lore_source", path=bridge[0]["source"])
                print(f"\n  Citation check (lore_source {bridge[0]['source']}):\n  > {src['quote']}")

            say("3. Lore tells the account owner on Teams (internal, no confirmation needed)  (lore_notify_am)")
            if bridge:
                msg = await call("lore_notify_am", account=ACC, text=f"{bridge[0]['for_the_am']} Fix the open service issue first. Source: {bridge[0]['source']}")
                print(f"Teams message to {msg['to']['name']} -> {msg['outbox_file']}")

            say("4. Sources disagree: which one wins?  (lore_conflicts)")
            for c in (await call("lore_conflicts", account=ACC))["conflicts"]:
                print(f"- {c['id']}: WINNER {c['winner']['summary']}  ({c['winner'].get('source')})")
                for l in c["losers"]:
                    print(f"    loses: {l['summary']}  ({l.get('source')})")
                print(f"    rule: {c.get('rule')}")

            say("5. What changed: the budget fact was superseded, not overwritten  (lore_timeline kind=budget)")
            for e in (await call("lore_timeline", account=ACC, kind="budget"))["events"]:
                print(f"- {e['date']} {'[superseded] ' if e['superseded'] else '[current]    '}{e['summary']}  ({e['source_type']}, {e['source']})")

            say("6. Buying committee  (lore_account_map)")
            m = await call("lore_account_map", account=ACC)
            show({k: v for k, v in m.items() if k != "mermaid"}, 1500)
            print("\nMermaid:\n" + m.get("mermaid", "")[:1200])

            say("7. Whitespace + the internal product chatbot  (lore_whitespace, lore_product_assistant)")
            show(await call("lore_whitespace", account=ACC), 1500)
            pa = await call("lore_product_assistant", question="auditors want 24x7 security monitoring with one alert dashboard", account=ACC)
            for a in pa["answers"]:
                print(f"- {a['name']} ({a['product_id']}): solves {a['solves']}; owns it already: {a['customer_already_owns']}")

            say("8. Book the meeting: do-not-contact people are removed, nothing is sent until confirmed")
            crm = await call("lore_crm", account=ACC)
            contacts = crm["contacts"]
            print(f"CRM backend: {crm['backend']}; contacts:")
            for c in contacts:
                print(f"  {c.get('contact_id')} {c.get('name') or c.get('full_name')} - {c.get('title') or c.get('job_title')} "
                      f"consent={c.get('consent') or c.get('consent_status')} dnc={c.get('do_not_contact')}")
            ids = [c["contact_id"] for c in contacts][:3]
            if "CON-9001" not in ids and any(c["contact_id"] == "CON-9001" for c in contacts):
                ids.append("CON-9001")
            prop = await call("lore_schedule_meeting", account=ACC, contact_ids=ids, title="Asterforge service review + security monitoring",
                              start="2026-11-19T10:00", duration_minutes=45,
                              agenda="1) Close out the open service issue 2) Auditor requirement for 24x7 monitoring")
            show({k: prop.get(k) for k in ("id", "status", "attendees", "blocked", "warnings")} if "id" in prop else prop, 1800)
            if "id" in prop:
                done = await call("lore_confirm_action", action_id=prop["id"], approve=True)
                print(f"\nConfirmed -> {done['status']}; files: {done.get('outbox_files')}; wiki commitment {done.get('kb_event')}")

            say("9. ChatGPT updates the CRM (governed): allowlisted field, own account, reason, mirrored to the wiki")
            bad = await call("lore_crm_update", object="Opportunity", record_id="OPP-0002", fields={"OwnerId": "005000000000001"}, reason="reassign")
            print(f"Off-allowlist field -> {bad.get('error')}")
            upd = await call("lore_crm_update", object="Opportunity", record_id="OPP-0002",
                             fields={"Amount": 120000, "NextStep": "Service review 19 Nov, then MSS proposal"},
                             reason="CFO confirmed the S$120k phase-one budget on the 10 Nov call")
            show(upd, 1500)

            say("10. Search and fetch (ChatGPT deep-research shape)")
            hits = (await call("search", query="24x7 monitoring"))["results"]
            for h in hits[:5]:
                print(f"- {h['id']}: {h['title'][:110]}")

            if with_agent:
                say("11. Agent: lore_ask (internal agent over the internal MCP)")
                a = await call("lore_ask", question="For ACC-0001, what did their engineers ask Ops for, what should I sell, and what blocks outreach?")
                print(a.get("answer") or a.get("error"))
                print(f"[{a.get('provider')}, {a.get('elapsed_s')}s, tools: {a.get('tool_calls')}]")

                say("12. Agent: ingest a new Vexa meeting transcript (stored verbatim, facts extracted with citations)")
                from lore.connectors import vexa

                transcript = json.loads((ROOT / "seed" / "vexa_sample_transcript.json").read_text())
                ing = await call("lore_ingest", account=ACC, source_type="meeting_transcript", text=vexa.transcript_to_text(transcript),
                                 occurred_at=vexa.meeting_date(transcript), origin="vexa:teams/3817264905123")
                print(f"stored {ing.get('stored')}")
                print((ing.get("ingest_agent") or {}).get("answer") or ing)

                say("13. Agent: pre-meeting brief  (lore_account_brief)")
                b = await call("lore_account_brief", account=ACC)
                print(b.get("answer") or b.get("error"))

            say("Done. Outbox: ../outputs/lore-actions/  Account map HTML: ../outputs/lore-maps/ACC-0001.html  Audit: knowledge-base/audit/ledger.jsonl")


if __name__ == "__main__":
    anyio.run(main, "--agent" in sys.argv)
