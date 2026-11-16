"""Internal MCP: granular tools over the wiki, CRM (Salesforce Hosted MCP or CSV), mocked Singtel sources and actions.

Only the internal agent connects here. Tool set depends on LORE_AGENT_SCOPE (read | write | actions | all) so read-only
agents physically cannot write. Every call is checked against the caller's account scope and logged.
"""
from __future__ import annotations

import functools
import json
import os
import time

from mcp.server.fastmcp import FastMCP

from lore import actions as act
from lore import config, insights
from lore.connectors import mock_sources as src
from lore.connectors.crm import get_crm
from lore.governance import denied, principal
from lore.kb.store import KBError, KnowledgeBase

USER = os.environ.get("LORE_USER", config.DEFAULT_USER)
ACTOR = os.environ.get("LORE_ACTOR", f"agent-for:{USER}")
SCOPE = os.environ.get("LORE_AGENT_SCOPE", "read")


def _log(tool: str, args: dict, ok: bool, err: str | None, ms: int) -> None:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    with (config.VAR_DIR / "tool_calls.jsonl").open("a") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "server": "internal", "user": USER, "actor": ACTOR,
                             "tool": tool, "args": {k: str(v)[:200] for k, v in args.items()}, "ok": ok, "error": err, "ms": ms}) + "\n")


def build_server(scope: str = SCOPE) -> FastMCP:
    mcp = FastMCP("lore-internal", log_level="WARNING", instructions="Granular Lore tools. Call kb_index first. Cite raw/...#Lnn for every fact.")
    kb = KnowledgeBase()
    who = principal(USER)

    def guard(account: str | None) -> None:
        if account and not who.can_see(account):
            raise KBError(denied(account, who))

    def tool(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            t0 = time.time()
            try:
                out = fn(*a, **kw)
                _log(fn.__name__, kw, True, None, int((time.time() - t0) * 1000))
                return out
            except KBError as exc:
                _log(fn.__name__, kw, False, str(exc), int((time.time() - t0) * 1000))
                return {"error": str(exc)}
        return mcp.tool()(wrapper)

    # ------------------------------------------------------------------ wiki reads
    @tool
    def kb_index() -> str:
        """The wiki map: accounts you can see, products, weekly signal pages. Call this first."""
        lines = kb.get_page("wiki/index.md").splitlines()
        return "\n".join(l for l in lines if not l.startswith("| [ACC-") or l.split("]")[0][3:] in who.accounts)

    @tool
    def kb_search(query: str, account: str | None = None, kind: str | None = None) -> list[dict]:
        """Full-text search over wiki pages and raw sources. All terms must match. Optional account / event kind filter."""
        guard(account)
        return kb.search(query, account=account, kind=kind, allowed_accounts=set(who.accounts))

    @tool
    def kb_get_account(account: str) -> str:
        """The account front page: current facts by source rank, superseded history, contradictions, signals, contacts."""
        guard(account)
        return kb.get_page(f"wiki/accounts/{account}/index.md")

    @tool
    def kb_get_timeline(account: str, from_week: str | None = None, to_week: str | None = None, kind: str | None = None) -> list[dict]:
        """Events in order including superseded ones. Weeks look like 2026-W46."""
        guard(account)
        sup = kb.superseded_ids(account)
        out = []
        for e in kb.events(account):
            w = e.get("week")
            if (from_week and w < from_week) or (to_week and w > to_week) or (kind and e.get("kind") != kind):
                continue
            out.append({**{k: e.get(k) for k in ("id", "week", "occurred_at", "kind", "topic", "value", "summary", "source",
                                                  "source_rank", "epistemic_status", "said_by", "product", "gate", "author")},
                        "superseded": e.id in sup})
        return out[-80:]

    @tool
    def kb_get_page(path: str) -> str:
        """Any wiki page by path relative to the knowledge base, e.g. wiki/products/p-demo-mss.md."""
        acc = path.split("/")[2] if path.startswith("wiki/accounts/") else None
        guard(acc)
        return kb.get_page(path)

    @tool
    def kb_get_raw(path: str) -> dict:
        """The exact source lines a fact cites. Pass raw/...md or raw/...md#L3-L5."""
        meta, _ = kb.read_raw(path)
        guard(meta.get("account"))
        return {"meta": meta, "quote": kb.quote(path)}

    @tool
    def kb_conflicts(account: str | None = None) -> list[dict]:
        """Open CONTRADICTIONs: the winning (higher source rank) fact and the losers, which are kept, not deleted."""
        guard(account)
        return [c for c in kb.conflicts(account) if who.can_see(c["account"])]

    @tool
    def kb_weekly_signals(week: str | None = None) -> str:
        """Weekly signals page across your accounts. week like 2026-W47; default latest."""
        sdir = kb.wiki_dir / "signals"
        pages = sorted(sdir.rglob("W*.md"))
        if week:
            y, w = week.split("-")
            pages = [p for p in pages if p.parent.name == y and p.stem == w]
        if not pages:
            return "no signals page for that week"
        text = pages[-1].read_text()
        blocks = text.split("\n## ")
        keep = [blocks[0]] + [b for b in blocks[1:] if any(f"({a}," in b for a in who.accounts) or b.startswith("Open contradictions")]
        return "\n## ".join(keep)

    # ------------------------------------------------------------------ CRM (Salesforce)
    @tool
    def crm_get_account(account: str) -> dict:
        """Live CRM account record (Salesforce Hosted MCP when LORE_CRM=salesforce, else the synthetic export)."""
        guard(account)
        crm = get_crm()
        return {"backend": crm.backend, "record": crm.get_account(account)}

    @tool
    def crm_get_contacts(account: str) -> dict:
        """CRM contacts with role, authority, consent and do-not-contact flags."""
        guard(account)
        crm = get_crm()
        return {"backend": crm.backend, "records": crm.get_contacts(account)}

    @tool
    def crm_get_opportunities(account: str) -> dict:
        """CRM opportunities: stage, amount, close date, next step."""
        guard(account)
        crm = get_crm()
        return {"backend": crm.backend, "records": crm.get_opportunities(account)}

    @tool
    def crm_get_cases(account: str) -> dict:
        """CRM support cases for the account."""
        guard(account)
        crm = get_crm()
        return {"backend": crm.backend, "records": crm.get_cases(account)}

    if get_crm().backend == "salesforce":
        @tool
        def crm_soql(query: str) -> dict:
            """Run a read-only SOQL query through the Salesforce Hosted MCP Server as the signed-in Salesforce user."""
            if not query.strip().lower().startswith("select"):
                raise KBError("only SELECT queries are allowed")
            return {"backend": "salesforce", "records": get_crm().soql(query)}

    # ------------------------------------------------------------------ mocked Singtel sources
    @tool
    def confluence_ops_reports(account: str) -> list[dict]:
        """L1/L2 ops support reports (Confluence) for an account: customer message, technical summary, sales relevance."""
        guard(account)
        return [{"title": d.title, "date": d.occurred_at, "origin": d.origin, "text": d.text} for d in src.confluence_ops_reports(account)]

    @tool
    def call_transcripts(account: str) -> list[dict]:
        """Meeting and call transcripts from internal call storage."""
        guard(account)
        return [{"title": d.title, "date": d.occurred_at, "origin": d.origin, "text": d.text} for d in src.transcripts(account)]

    @tool
    def outlook_emails(account: str) -> list[dict]:
        """Customer emails from Outlook."""
        guard(account)
        return [{"title": d.title, "date": d.occurred_at, "origin": d.origin, "text": d.text} for d in src.emails(account)]

    @tool
    def teams_ops_threads(account: str) -> list[dict]:
        """Ops support escalation threads from Microsoft Teams."""
        guard(account)
        return src.teams_threads(account)

    @tool
    def product_assistant_ask(question: str, account: str | None = None) -> dict:
        """Ask Singtel's internal product chatbot (on-prem) which product solves a need. Pass account to flag what they own."""
        guard(account)
        owned = (kb.account_meta(account).get("owned_products") if account else None)
        return src.product_bot_ask(question, owned)

    @tool
    def web_search(query: str) -> list[dict]:
        """Public company-level news, job posts and tenders. Never searches individuals."""
        return src.web_search(query)

    # ------------------------------------------------------------------ insights
    @tool
    def insight_who_to_call() -> list[dict]:
        """Ranked accounts to call this week with reasons, recommended contact (consent checked) and citations."""
        return insights.who_to_call(kb, who)

    @tool
    def insight_whitespace(account: str) -> dict:
        """Singtel products the customer does not own that match their pains or recent requests, with evidence and gate."""
        guard(account)
        return insights.whitespace(kb, account)

    @tool
    def insight_account_map(account: str) -> dict:
        """Buying-committee map: reporting lines, champion, economic buyer, concerns, relationship strength (Mermaid + HTML)."""
        guard(account)
        return insights.account_map(kb, account)

    @tool
    def insight_ops_bridge(account: str | None = None) -> list[dict]:
        """Technical ops requests translated for the AM: what the customer's engineers asked for and which product covers it."""
        guard(account)
        return insights.ops_bridge(kb, who, account)

    @tool
    def insight_brief_pack(account: str) -> dict:
        """All inputs for a pre-meeting brief: facts, conflicts, signals, objections, commitments, whitespace, next meeting."""
        guard(account)
        return insights.brief_pack(kb, account)

    # ------------------------------------------------------------------ writes (additive only)
    if scope in ("write", "all"):
        @tool
        def kb_add_raw(type: str, account: str, occurred_at: str, text: str, origin: str, title: str = "") -> dict:
            """Store a new source document (never overwrites). type: meeting_transcript, call_transcript, customer_email,
            crm_record, am_note, ops_report, calendar, public_signal, research_note. Returns the raw path to cite."""
            guard(account)
            path = kb.add_raw(type, account, occurred_at, text, origin, actor=ACTOR, title=title)
            lines = kb.read_raw(path)[1]
            return {"path": path, "lines": {f"L{i}": l for i, l in enumerate(lines, 1)}}

        @tool
        def kb_add_event(account: str, kind: str, summary: str, source: str, epistemic_status: str, topic: str | None = None,
                         value: str | None = None, said_by: str | None = None, product: str | None = None, gate: str | None = None,
                         supersedes: list[str] | None = None, bridge: bool = False) -> dict:
            """Add one fact to the account timeline. source must be raw/...md#Lnn. epistemic_status: confirmed | stated |
            hedged | reported | inferred. kind: fact, need, budget, timeline, signal, churn_risk, objection, commitment,
            ops_request, relationship, status, opportunity, meeting_note. To change a fact, pass supersedes=[old event id]
            with the same topic. Set bridge=true on an ops_request that maps to a product the customer does not own."""
            guard(account)
            extra = {"bridge": True} if bridge else None
            path = kb.add_event({"account": account, "kind": kind, "summary": summary, "source": source, "epistemic_status": epistemic_status,
                                 "topic": topic, "value": value, "said_by": said_by, "product": product, "gate": gate,
                                 "supersedes": supersedes, "extra": extra}, actor=ACTOR)
            return {"path": path, "front_page": f"wiki/accounts/{account}/index.md"}

        @tool
        def kb_add_contact(account: str, name: str, role: str, authority: str | None = None, influence: str | None = None,
                           reports_to: str | None = None) -> dict:
            """Add a new contact page (company roles only; no personal data beyond name and role)."""
            guard(account)
            return {"path": kb.rel(kb.add_contact(account, {"name": name, "role": role, "authority": authority, "influence": influence,
                                                             "reports_to": reports_to, "consent": "not_requested"}, actor=ACTOR))}

        @tool
        def crm_update_record(object: str, record_id: str, fields: dict, reason: str) -> dict:
            """Governed CRM update (Opportunity / Account / Contact allowlisted fields), mirrored into the wiki."""
            from lore import crm_writes

            return crm_writes.update_record(who, object, record_id, fields, reason)

        @tool
        def kb_resolve_conflict(conflict_id: str, winning_event: str, reason: str) -> dict:
            """Record which fact wins a contradiction. Appends a resolution event; the losing facts stay in history."""
            guard(conflict_id.split("-", 1)[1][:8] if conflict_id.startswith("CONF-") else None)
            return {"path": kb.resolve_conflict(conflict_id, winning_event, reason, actor=ACTOR)}

    # ------------------------------------------------------------------ actions
    if scope in ("actions", "all"):
        @tool
        def action_notify_am_teams(account: str, text: str, source: str | None = None) -> dict:
            """Send the account owner a Teams message (internal; mocked Microsoft Graph outbox). Include the citation."""
            guard(account)
            return act.notify_am_teams(account, text, requested_by=ACTOR, source=source)

        @tool
        def action_propose_meeting(account: str, contact_ids: list[str], title: str, start: str, duration_minutes: int = 30,
                                   agenda: str = "") -> dict:
            """Propose a customer meeting (Outlook/Teams). Not sent until a human confirms it. start: ISO datetime SGT."""
            guard(account)
            return act.propose_meeting(account, contact_ids, title, start, duration_minutes, agenda, requested_by=ACTOR)

    return mcp


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
