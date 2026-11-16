"""Lore public MCP: the one server ChatGPT Enterprise (or Claude, Cursor, Codex) connects to.

Direct, permission-checked access to meeting transcripts, emails, ops reports, Teams threads, account descriptions and
the CRM (read and governed update), fast deterministic insights, the internal product assistant, actions, and
agent-backed synthesis (Q&A, briefs, ingestion) that runs behind it on the internal MCP.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from lore import actions as act
from lore import config, crm_writes, insights
from lore.agent import run as run_agent
from lore.connectors import mock_sources as src
from lore.connectors.crm import get_crm
from lore.governance import Principal, denied, principal
from lore.kb.store import KBError, KnowledgeBase, _split_frontmatter

INSTRUCTIONS = """Lore is Singtel's on-prem customer knowledge layer for account managers.
Start with lore_who_to_call_this_week or lore_account. Every fact carries a citation (raw/...#Lnn); keep it in answers.
Meetings outrank email, CRM, ops reports and press. Respect gates: service_recovery_first means fix service before
pitching. CRM updates are governed (allowlisted fields, your accounts only) and mirrored into the wiki history."""

_LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_security = TransportSecuritySettings(
    allowed_hosts=_LOCAL_HOSTS + config.PUBLIC_ALLOWED_HOSTS,
    allowed_origins=[f"http://{h}" for h in _LOCAL_HOSTS] + [f"https://{h}" for h in config.PUBLIC_ALLOWED_HOSTS],
) if config.PUBLIC_ALLOWED_HOSTS else None
mcp = FastMCP("lore", log_level="WARNING", instructions=INSTRUCTIONS, host=config.PUBLIC_HOST, port=config.PUBLIC_PORT,
              transport_security=_security)
kb = KnowledgeBase()


def _user(ctx: Context | None) -> Principal:
    uid = None
    try:
        req = ctx.request_context.request if ctx else None
        if req is not None:
            uid = req.headers.get("x-lore-user") or req.query_params.get("user")
    except Exception:  # noqa: BLE001 - stdio has no HTTP request
        uid = None
    return principal(uid or os.environ.get("LORE_USER", config.DEFAULT_USER))


def _log(tool: str, who: Principal, args: dict, ok: bool, err: str | None = None) -> None:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    with (config.VAR_DIR / "tool_calls.jsonl").open("a") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "server": "public", "user": who.user_id, "tool": tool,
                             "args": {k: str(v)[:200] for k, v in args.items()}, "ok": ok, "error": err}) + "\n")


def _guard(who: Principal, account: str | None) -> None:
    if account and not who.can_see(account):
        raise KBError(denied(account, who))


def _safe(tool: str, who: Principal, args: dict, fn) -> Any:
    try:
        out = fn()
        _log(tool, who, args, True)
        return out
    except KBError as exc:
        _log(tool, who, args, False, str(exc))
        return {"error": str(exc)}


def _raw_docs(account: str, types: set[str], limit: int) -> list[dict]:
    docs = []
    for p in kb.raw_dir.rglob("*.md"):
        meta, body = _split_frontmatter(p.read_text())
        if meta.get("account") == account and meta.get("type") in types:
            docs.append({"path": kb.rel(p), "type": meta["type"], "date": str(meta.get("occurred_at")), "title": meta.get("title"),
                         "origin": meta.get("origin"), "text": "\n".join(f"L{i}: {l}" for i, l in enumerate(body.splitlines(), 1))})
    docs.sort(key=lambda d: d["date"], reverse=True)
    return docs[:limit]


# ============================================================================ discovery (ChatGPT search/fetch shape)
@mcp.tool()
def search(query: str, ctx: Context = None) -> dict:
    """Search everything Lore knows (account pages, timeline facts, transcripts, emails, ops reports, CRM snapshots)."""
    who = _user(ctx)
    hits = kb.search(query, allowed_accounts=set(who.accounts), limit=20)
    _log("search", who, {"query": query}, True)
    return {"results": [{"id": h["path"], "title": f"{h['account'] or ''} {h['kind'] or ''}: {h['summary'] or h['path']}".strip(),
                         "url": f"lore://{h['path']}", "snippet": " | ".join(h["lines"])} for h in hits]}


@mcp.tool()
def fetch(id: str, ctx: Context = None) -> dict:
    """Fetch a page or source returned by search, by its id (path)."""
    who = _user(ctx)

    def go():
        text = kb.get_page(id.split("#")[0])
        meta, _ = _split_frontmatter(text)
        acc = meta.get("account") or (id.split("/")[2] if id.startswith("wiki/accounts/") else None)
        _guard(who, acc)
        return {"id": id, "title": meta.get("title") or meta.get("summary") or id, "text": text, "url": f"lore://{id}", "metadata": meta}

    return _safe("fetch", who, {"id": id}, go)


# ============================================================================ accounts and sources
@mcp.tool()
def lore_my_accounts(ctx: Context = None) -> dict:
    """Accounts you are allowed to see, with status, sector and owner."""
    who = _user(ctx)
    rows = [{"account": a["account_id"], "name": a["name"], "status": a.get("status"), "sector": a.get("sector"), "hq": a.get("hq"),
             "renewal": a.get("renewal")} for a in kb.list_accounts() if who.can_see(a["account_id"])]
    _log("lore_my_accounts", who, {}, True)
    return {"user": f"{who.name} ({who.user_id}, {who.role})", "accounts": rows}


@mcp.tool()
def lore_account(account: str, ctx: Context = None) -> dict:
    """The account description and front page: current facts by source rank, superseded history, contradictions,
    ops-to-AM bridge items, signals, commitments and contacts. Every line cites its source."""
    who = _user(ctx)
    return _safe("lore_account", who, {"account": account}, lambda: (_guard(who, account), {
        "account": account, "page": kb.get_page(f"wiki/accounts/{account}/index.md")})[1])


@mcp.tool()
def lore_transcripts(account: str, limit: int = 5, ctx: Context = None) -> dict:
    """Meeting and call transcripts for an account (call storage, Teams, Vexa), newest first, with line numbers to cite."""
    who = _user(ctx)
    return _safe("lore_transcripts", who, {"account": account}, lambda: (_guard(who, account), {
        "account": account, "transcripts": _raw_docs(account, {"meeting_transcript", "call_transcript"}, limit)})[1])


@mcp.tool()
def lore_emails(account: str, limit: int = 5, ctx: Context = None) -> dict:
    """Customer emails (Outlook) for an account, newest first."""
    who = _user(ctx)
    return _safe("lore_emails", who, {"account": account}, lambda: (_guard(who, account), {
        "account": account, "emails": _raw_docs(account, {"customer_email"}, limit)})[1])


@mcp.tool()
def lore_ops_reports(account: str, limit: int = 5, ctx: Context = None) -> dict:
    """L1/L2 ops support reports (Confluence) and Teams escalation threads for an account."""
    who = _user(ctx)
    return _safe("lore_ops_reports", who, {"account": account}, lambda: (_guard(who, account), {
        "account": account, "reports": _raw_docs(account, {"ops_report"}, limit), "teams_threads": src.teams_threads(account)})[1])


@mcp.tool()
def lore_timeline(account: str, kind: str | None = None, ctx: Context = None) -> dict:
    """Every fact Lore holds on the account in date order, including superseded ones (nothing is ever deleted)."""
    who = _user(ctx)

    def go():
        _guard(who, account)
        sup = kb.superseded_ids(account)
        evs = [{"id": e.id, "date": e.get("occurred_at"), "kind": e.get("kind"), "topic": e.get("topic"), "summary": e.get("summary"),
                "status": e.get("epistemic_status"), "source": e.get("source"), "source_type": e.get("source_type"),
                "author": e.get("author"), "superseded": e.id in sup} for e in kb.events(account) if not kind or e.get("kind") == kind]
        return {"account": account, "events": evs[-60:]}

    return _safe("lore_timeline", who, {"account": account, "kind": kind}, go)


@mcp.tool()
def lore_source(path: str, ctx: Context = None) -> dict:
    """Open the exact source lines behind a citation, e.g. raw/2026/W46/RAW-...md#L4."""
    who = _user(ctx)

    def go():
        meta, _ = kb.read_raw(path)
        _guard(who, meta.get("account"))
        return {"source": path, "type": meta.get("type"), "origin": meta.get("origin"), "date": str(meta.get("occurred_at")),
                "quote": kb.quote(path)}

    return _safe("lore_source", who, {"path": path}, go)


# ============================================================================ CRM (read + governed write)
@mcp.tool()
def lore_crm(account: str, ctx: Context = None) -> dict:
    """Live CRM view of the account: record, contacts (consent / do-not-contact), opportunities and support cases.
    Reads Salesforce when connected, otherwise the synthetic CRM export."""
    who = _user(ctx)

    def go():
        _guard(who, account)
        crm = get_crm()
        return {"backend": crm.backend, "account": crm.get_account(account), "contacts": crm.get_contacts(account),
                "opportunities": crm.get_opportunities(account), "cases": crm.get_cases(account)[:10]}

    return _safe("lore_crm", who, {"account": account}, go)


@mcp.tool()
def lore_crm_update(object: str, record_id: str, fields: dict, reason: str, ctx: Context = None) -> dict:
    """Update a CRM record you own. object: Opportunity | Account | Contact; record_id is the Lore id (OPP-0002,
    ACC-0001, CON-0001). Allowed fields - Opportunity: StageName, Amount, CloseDate, NextStep, Probability;
    Account: Lore_Relationship_Health__c, Lore_Budget_SGD__c, Lore_Renewal_Date__c; Contact: Title, Department,
    Lore_Influence__c, Lore_Champion__c. reason is required and goes into the audit trail and the wiki history."""
    who = _user(ctx)
    return _safe("lore_crm_update", who, {"object": object, "record_id": record_id, "fields": fields},
                 lambda: crm_writes.update_record(who, object, record_id, fields, reason))


@mcp.tool()
def lore_crm_log_activity(account: str, subject: str, notes: str, activity_type: str = "call", contact_id: str | None = None,
                          ctx: Context = None) -> dict:
    """Log a call, email or meeting note against the account in the CRM (Salesforce Task) and the wiki."""
    who = _user(ctx)
    return _safe("lore_crm_log_activity", who, {"account": account, "subject": subject},
                 lambda: crm_writes.log_activity(who, account, subject, notes, activity_type, contact_id))


# ============================================================================ insights (fast, deterministic, cited)
@mcp.tool()
def lore_who_to_call_this_week(ctx: Context = None) -> dict:
    """Ranked accounts to call this week and why: ops requests for products they lack, buying signals, renewals,
    churn risks (service recovery first). Recommends a contact with granted consent."""
    who = _user(ctx)
    _log("lore_who_to_call_this_week", who, {}, True)
    return {"for": f"{who.name} ({who.user_id})", "week_of": config.TODAY, "calls": insights.who_to_call(kb, who)}


@mcp.tool()
def lore_whitespace(account: str, ctx: Context = None) -> dict:
    """Singtel products the customer does not own yet that match their pains or what their engineers asked Ops for."""
    who = _user(ctx)
    return _safe("lore_whitespace", who, {"account": account}, lambda: (_guard(who, account), insights.whitespace(kb, account))[1])


@mcp.tool()
def lore_account_map(account: str, ctx: Context = None) -> dict:
    """Buying-committee map: reporting lines, champion, economic buyer, concerns and relationship strength.
    Returns Mermaid (render it) and an HTML file path."""
    who = _user(ctx)
    return _safe("lore_account_map", who, {"account": account}, lambda: (_guard(who, account), insights.account_map(kb, account))[1])


@mcp.tool()
def lore_ops_bridge(account: str | None = None, ctx: Context = None) -> dict:
    """What customers' engineers asked Ops for, translated into plain sales language, mapped to Singtel products,
    with the outreach gate. The fast lane from L1/L2 ops to the account manager."""
    who = _user(ctx)
    return _safe("lore_ops_bridge", who, {"account": account}, lambda: (_guard(who, account), {"items": insights.ops_bridge(kb, who, account)})[1])


@mcp.tool()
def lore_conflicts(account: str | None = None, ctx: Context = None) -> dict:
    """Contradictions between sources and which one wins (meeting > email > CRM / ops > press)."""
    who = _user(ctx)
    return _safe("lore_conflicts", who, {"account": account}, lambda: (_guard(who, account), {
        "conflicts": [c for c in kb.conflicts(account) if who.can_see(c["account"])]})[1])


@mcp.tool()
def lore_product_assistant(question: str, account: str | None = None, ctx: Context = None) -> dict:
    """Ask Singtel's internal product chatbot (on-prem) which product fits a need; flags what the account owns."""
    who = _user(ctx)
    return _safe("lore_product_assistant", who, {"question": question}, lambda: (_guard(who, account), src.product_bot_ask(
        question, kb.account_meta(account).get("owned_products") if account else None))[1])


# ============================================================================ agent-backed synthesis
@mcp.tool()
def lore_ask(question: str, ctx: Context = None) -> dict:
    """Ask anything about your customers. Lore's internal agent reads the wiki, CRM, transcripts and ops reports and
    answers with citations. Slower (tens of seconds); use the direct tools for simple lookups."""
    who = _user(ctx)
    res = run_agent(question, user=who.user_id, scope="read").to_dict()
    _log("lore_ask", who, {"question": question}, res["error"] is None, res["error"])
    return res


@mcp.tool()
def lore_account_brief(account: str, ctx: Context = None) -> dict:
    """One-page pre-meeting brief written by Lore's internal agent: what changed, open objections, signals,
    commitments, who to talk to, what not to pitch, recommended next step. All cited."""
    who = _user(ctx)
    if not who.can_see(account):
        return {"error": denied(account, who)}
    prompt = (config.PLAYBOOK_DIR / "meeting_prep.md").read_text().replace("{account}", account)
    res = run_agent(prompt, user=who.user_id, scope="read").to_dict()
    _log("lore_account_brief", who, {"account": account}, res["error"] is None, res["error"])
    return res


@mcp.tool()
def lore_ingest(account: str, source_type: str, text: str, occurred_at: str, origin: str = "chatgpt-upload", ctx: Context = None) -> dict:
    """Add new material (meeting transcript, email, ops report, AM note) to Lore. It is stored verbatim, then the
    ingest agent extracts cited facts, supersedes outdated ones and flags contradictions.
    source_type: meeting_transcript | call_transcript | customer_email | am_note | ops_report | public_signal."""
    who = _user(ctx)

    def go():
        _guard(who, account)
        raw = kb.add_raw(source_type, account, occurred_at, text, origin, actor=who.user_id, title=f"{source_type} via Lore")
        prompt = (config.PLAYBOOK_DIR / "ingest.md").read_text().replace("{raw}", raw).replace("{account}", account)
        res = run_agent(prompt, user=who.user_id, scope="write", actor="ingest-agent").to_dict()
        return {"stored": raw, "ingest_agent": res}

    return _safe("lore_ingest", who, {"account": account, "source_type": source_type}, go)


# ============================================================================ actions
@mcp.tool()
def lore_schedule_meeting(account: str, contact_ids: list[str], title: str, start: str, duration_minutes: int = 30, agenda: str = "",
                          ctx: Context = None) -> dict:
    """Propose an Outlook/Teams meeting with customer contacts. Do-not-contact people are removed, consent and
    service-recovery warnings are attached. Nothing is sent until lore_confirm_action approves it."""
    who = _user(ctx)
    return _safe("lore_schedule_meeting", who, {"account": account, "title": title}, lambda: (_guard(who, account), act.propose_meeting(
        account, contact_ids, title, start, duration_minutes, agenda, requested_by=who.user_id))[1])


@mcp.tool()
def lore_pending_actions(ctx: Context = None) -> dict:
    """Actions waiting for your confirmation."""
    who = _user(ctx)
    return {"pending": [a for a in act.list_pending() if who.can_see(a["account"])]}


@mcp.tool()
def lore_confirm_action(action_id: str, approve: bool = True, ctx: Context = None) -> dict:
    """Approve (send) or reject a proposed action. Approved meetings are written to the Outlook/Teams outbox and
    recorded in the wiki as a commitment."""
    who = _user(ctx)
    return _safe("lore_confirm_action", who, {"action_id": action_id, "approve": approve}, lambda: act.decide(action_id, approve, who.user_id))


@mcp.tool()
def lore_notify_am(account: str, text: str, ctx: Context = None) -> dict:
    """Send the account owner an internal Teams message (e.g. an ops request worth acting on)."""
    who = _user(ctx)
    return _safe("lore_notify_am", who, {"account": account}, lambda: (_guard(who, account), act.notify_am_teams(account, text, requested_by=who.user_id))[1])


# ============================================================================ playbook prompts
def _prompt(name: str) -> str:
    return (config.PLAYBOOK_DIR / f"{name}.md").read_text()


@mcp.prompt()
def weekly_call_plan() -> str:
    """Who should I call this week, and why?"""
    return _prompt("weekly_call_plan")


@mcp.prompt()
def meeting_prep(account: str) -> str:
    """Prepare me for my next meeting with an account."""
    return _prompt("meeting_prep").replace("{account}", account)


@mcp.prompt()
def grow_account(account: str) -> str:
    """Find expansion opportunities in an existing account."""
    return _prompt("grow_account").replace("{account}", account)


@mcp.prompt()
def ops_to_sales() -> str:
    """What have customers' engineers asked Ops for that we could sell?"""
    return _prompt("ops_to_sales")


_INFO_PAGE = """<!doctype html><meta charset="utf-8"><title>Lore MCP</title>
<body style="font-family:system-ui;max-width:640px;margin:48px auto;line-height:1.5">
<h1>Lore MCP server is running</h1>
<p>This address is for MCP clients, not web browsers. In ChatGPT, add this page's URL as a custom connector
(no authentication). Keep the <code>/mcp?user=...</code> part; <code>user</code> is the Lore user whose access scope applies.</p>
<p>{tools} tools available.</p></body>"""


def http_app():
    """Streamable HTTP app that shows a short info page to browsers instead of the SDK's JSON-RPC error."""
    from starlette.responses import HTMLResponse

    app = mcp.streamable_http_app()
    mcp_path = mcp.settings.streamable_http_path.rstrip("/")

    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"].rstrip("/") in ("", mcp_path):
            accept = dict(scope.get("headers") or []).get(b"accept", b"").decode()
            if "text/event-stream" not in accept:
                n = len(await mcp.list_tools())
                return await HTMLResponse(_INFO_PAGE.replace("{tools}", str(n)))(scope, receive, send)
        await app(scope, receive, send)

    return wrapped


def main(transport: str = "stdio") -> None:
    if transport == "streamable-http":
        import uvicorn

        uvicorn.run(http_app(), host=mcp.settings.host, port=mcp.settings.port, log_level="warning")
        return
    mcp.run(transport)


if __name__ == "__main__":
    import sys

    main("streamable-http" if "--http" in sys.argv else "stdio")
