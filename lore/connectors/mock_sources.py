"""Mocked Singtel internal systems, rendered from the synthetic CRM export.

Each function returns `Doc` objects shaped like what the real connector would fetch:
  * Confluence  -> L1/L2 ops support reports (from support tickets)
  * Call storage -> meeting / call transcripts (from CRM activities)
  * Outlook     -> customer emails
  * Teams       -> ops support threads (Singtel uses Teams, not Slack)
  * Product chatbot -> Singtel's internal product Q&A bot (from the product catalogue)
  * Web search  -> public company signals (job posts, tenders, press)
Line numbers inside a Doc are what wiki events cite (`raw/...#Lnn`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from lore import config
from lore.connectors.crm import load_table


@dataclass
class Doc:
    type: str
    account: str | None
    occurred_at: str
    origin: str
    title: str
    lines: list[str] = field(default_factory=list)
    anchors: dict[str, int] = field(default_factory=dict)

    def add(self, key: str | None, text: str) -> int:
        self.lines.append(text)
        n = len(self.lines)
        if key:
            self.anchors[key] = n
        return n

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _idx(table: str, key: str) -> dict[str, dict]:
    return {r[key]: r for r in load_table(table)}


def owners() -> dict[str, dict]:
    return _idx("owners", "owner_id")


def contacts() -> dict[str, dict]:
    return _idx("contacts", "contact_id")


def products() -> dict[str, dict]:
    return _idx("products", "product_id")


def _person(cid: str) -> str:
    c = contacts().get(cid)
    return f"{c['full_name']} ({c['job_title']})" if c else (cid or "unknown contact")


def _owner(oid: str) -> str:
    o = owners().get(oid)
    return f"{o['full_name']} ({o['role'].replace('_', ' ')})" if o else (oid or "unassigned")


# ----------------------------------------------------------------------------- CRM (Salesforce-shaped)
def crm_account_doc(a: dict) -> Doc:
    d = Doc("crm_record", a["account_id"], a["modified_at"] or config.TODAY, f"salesforce:Account/{a['account_id']}",
            f"CRM account record: {a['account_name']}")
    for key in ("account_name", "industry", "segment", "employee_count", "annual_revenue_sgd", "account_status", "country",
                "city", "operating_countries", "site_count", "customer_since", "contract_renewal_date",
                "annual_contract_value_sgd", "owned_product_ids", "pain_points", "business_objective", "technology_stack",
                "current_provider", "security_requirements", "data_residency_requirement", "procurement_route",
                "budget_estimate_sgd", "budget_status", "competitor_risk", "relationship_health", "strategic_notes",
                "next_best_action", "next_action_due"):
        if a.get(key):
            d.add(key, f"{key}: {a[key]}")
    return d


def crm_opportunity_doc(o: dict) -> Doc:
    d = Doc("crm_record", o["account_id"], o["modified_at"] or config.TODAY, f"salesforce:Opportunity/{o['opportunity_id']}",
            f"CRM opportunity: {o['opportunity_name']}")
    for key in ("opportunity_name", "opportunity_type", "stage", "amount_sgd", "probability", "expected_close_date",
                "product_ids", "pain_summary", "business_value_hypothesis", "success_metric", "budget_status",
                "decision_process", "next_step", "next_step_due", "competitor", "win_loss_reason", "security_review_status",
                "service_recovery_required"):
        if o.get(key):
            d.add(key, f"{key}: {o[key]}")
    return d


# ----------------------------------------------------------------------------- call storage / Outlook
def activity_doc(act: dict) -> Doc | None:
    t = act["activity_type"]
    if t in ("meeting", "call"):
        dtype, origin = ("meeting_transcript" if t == "meeting" else "call_transcript"), f"call-storage:{act['activity_id']}"
    elif t == "email":
        dtype, origin = "customer_email", f"outlook:{act['activity_id']}"
    elif t == "note":
        dtype, origin = "am_note", f"salesforce:Task/{act['activity_id']}"
    else:
        dtype, origin = "calendar", f"outlook-calendar:{act['activity_id']}"
    d = Doc(dtype, act["account_id"], act["activity_date"], origin, act["subject"])
    d.add("subject", f"Subject: {act['subject']}")
    d.add("when", f"Date: {act['activity_date']} ({act.get('duration_minutes') or '?'} min, {act.get('direction') or 'n/a'}, status {act['activity_status']})")
    d.add("who", f"Attendees: {_owner(act['owner_id'])} for Singtel; {_person(act['contact_id'])} for the customer")
    body = re.sub(r"^Fictional CRM log\.\s*", "", act.get("body_summary") or "")
    speaker = "Customer" if t in ("meeting", "call", "email") else "AM note"
    for i, sentence in enumerate(s for s in re.split(r"(?<=\.)\s+", body) if s):
        d.add("customer_need" if i == 0 else None, f"[{speaker}] {sentence}")
    if act.get("outcome"):
        d.add("outcome", f"Outcome: {act['outcome']}")
    if act.get("next_action"):
        d.add("next_action", f"Next action: {act['next_action']}" + (f" (due {act['next_action_due']})" if act.get("next_action_due") else ""))
    if act.get("outreach_gate"):
        d.add("gate", f"Outreach gate: {act['outreach_gate']}")
    return d


def transcripts(account: str) -> list[Doc]:
    return [d for a in load_table("activities") if a["account_id"] == account and a["activity_type"] in ("meeting", "call")
            for d in [activity_doc(a)] if d]


def emails(account: str) -> list[Doc]:
    return [d for a in load_table("activities") if a["account_id"] == account and a["activity_type"] == "email"
            for d in [activity_doc(a)] if d]


# ----------------------------------------------------------------------------- Confluence ops reports / Teams
def ops_report_doc(t: dict) -> Doc:
    d = Doc("ops_report", t["account_id"], t["opened_date"], f"confluence:OPS/{t['ticket_id']}",
            f"{t['support_tier']} ops report {t['ticket_id']}: {t['subject']}")
    d.add("header", f"Ticket: {t['ticket_id']} | Tier {t['support_tier']} | {t['category']} | priority {t['priority']} | status {t['ticket_status']}")
    d.add("raised_by", f"Raised by: {_person(t['contact_id'])} via {t['channel']}; handled by {_owner(t['owner_id'])}")
    p = products().get(t.get("product_id") or "")
    if p:
        d.add("product", f"Service affected: {p['product_name']} ({p['product_id']})")
    d.add("customer_message", "Customer message: " + re.sub(r"^Synthetic customer message:\s*", "", t["customer_message"]))
    d.add("technical_summary", f"Technical summary: {t['technical_summary']}")
    d.add("root_cause", f"Root cause: {t['root_cause']}")
    d.add("resolution", f"Resolution: {t['resolution_summary']}")
    d.add("impact", f"Business impact: {t['business_impact']} (sites affected: {t['affected_site_count']}, SLA breached: {t['sla_breached']})")
    if t.get("sales_signal") == "true":
        d.add("signal", f"Sales-relevant: yes ({t['signal_type']}, product {t['signal_product_id']}). Reason: {t['signal_reason']}")
        d.add("evidence", f"Evidence: \"{t['evidence_excerpt']}\"")
        d.add("gate", f"Outreach gate: {t['outreach_gate']}; hand-off to {_owner(t['handoff_owner_id'])}")
    else:
        d.add("signal", f"Sales-relevant: no. {t.get('signal_reason') or ''}".strip())
    if t.get("next_action"):
        d.add("next_action", f"Next action: {t['next_action']} (due {t['next_action_due']})")
    return d


def confluence_ops_reports(account: str) -> list[Doc]:
    return [ops_report_doc(t) for t in load_table("support_tickets") if t["account_id"] == account]


def teams_threads(account: str) -> list[dict]:
    """Ops support escalations as they would appear in a Teams channel."""
    out = []
    for t in load_table("support_tickets"):
        if t["account_id"] != account or t["support_tier"] != "L2":
            continue
        out.append({
            "channel": "Ops Support APAC / L2 escalations", "ticket": t["ticket_id"], "date": t["opened_date"],
            "messages": [
                {"from": "L1 desk", "text": f"Escalating {t['ticket_id']} ({t['subject']}) to L2. Customer says: {t['evidence_excerpt'] or t['customer_message'][:160]}"},
                {"from": _owner(t["owner_id"]), "text": f"Picked up. {t['technical_summary']}. Root cause: {t['root_cause']}."},
            ],
        })
    return out


# ----------------------------------------------------------------------------- public signals / web search
PUBLIC_SIGNAL_TYPES = {"new_site": "press", "tender": "tender notice", "it_hiring": "job post", "funding_growth": "press",
                       "leadership_change": "press", "expansion": "press"}


def public_signal_doc(s: dict, account_name: str) -> Doc:
    kind = PUBLIC_SIGNAL_TYPES.get(s["signal_type"], "press")
    d = Doc("public_signal", s["account_id"], s["observed_date"], f"web:{kind}/{s['signal_id']}",
            f"{kind.title()}: {account_name} ({s['signal_type'].replace('_', ' ')})")
    d.add("headline", f"Headline: {account_name}: {s['signal_type'].replace('_', ' ')}")
    d.add("evidence", "Excerpt: " + re.sub(r"^Synthetic [\w-]+ note:\s*", "", s["summary"]))
    if s.get("event_date"):
        d.add("event_date", f"Expected date: {s['event_date']}")
    d.add("confidence", f"Confidence: {s['confidence']} ({s['polarity']})")
    return d


def web_search(query: str, limit: int = 8) -> list[dict]:
    """Offline stand-in for a web search API. Returns public company-level items only, never individuals."""
    accounts = _idx("accounts", "account_id")
    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    results = []
    for s in load_table("buying_signals"):
        if s["source_object"] != "Accounts" or s["signal_type"] == "churn_risk":
            continue
        name = accounts[s["account_id"]]["account_name"]
        doc = public_signal_doc(s, name)
        hay = (doc.title + " " + doc.text + " " + accounts[s["account_id"]]["industry"]).lower()
        score = sum(hay.count(t) for t in terms)
        if score:
            results.append({"title": doc.title, "url": f"https://news.example/{s['signal_id'].lower()}", "date": s["observed_date"],
                            "account": s["account_id"], "snippet": doc.lines[1][9:], "score": score})
    overlay = config.SEED_DIR / "public_signals.json"
    if overlay.exists():
        for item in json.loads(overlay.read_text()):
            hay = (item["title"] + " " + item["snippet"]).lower()
            score = sum(hay.count(t) for t in terms)
            if score:
                results.append({**item, "score": score + 5})
    results.sort(key=lambda r: (-r["score"], r["date"]), reverse=False)
    return results[:limit]


# ----------------------------------------------------------------------------- internal product chatbot
SYNONYMS = {
    "branch": "multi-site connectivity", "branches": "multi-site connectivity", "sd-wan": "multi-site connectivity",
    "wan": "multi-site connectivity", "failover": "internet resilience", "outage": "internet resilience",
    "security": "ot security", "monitoring": "incident response", "soc": "incident response", "siem": "incident response",
    "alerts": "incident response", "threat": "incident response", "audit": "regulatory compliance", "compliance": "regulatory compliance",
    "cloud": "cloud migration", "aws": "cloud migration", "azure": "cloud migration", "backup": "disaster recovery",
    "recovery": "disaster recovery", "remote": "remote access", "vpn": "remote access", "zero": "remote access",
    "5g": "field workforce", "mobile": "field workforce", "fleet": "field workforce", "iot": "machine monitoring",
    "sensor": "machine monitoring", "factory": "machine monitoring", "voice": "collaboration", "teams": "collaboration",
}


def product_bot_ask(question: str, owned: list[str] | None = None) -> dict:
    """Mock of Singtel's internal product chatbot that AMs already use. Same on-prem trust boundary as Lore."""
    q = question.lower()
    tags = {SYNONYMS[w] for w in re.findall(r"[\w-]+", q) if w in SYNONYMS}
    matches = []
    for p in load_table("products"):
        ptags = set(p["pain_tags"].split("|"))
        score = len(ptags & tags) * 3 + sum(1 for w in re.findall(r"\w+", p["product_name"].lower()) if len(w) > 3 and w in q)
        if score:
            matches.append({
                "product_id": p["product_id"], "name": p["product_name"], "category": p["category"],
                "solves": p["pain_tags"].replace("|", ", "), "ideal_for": f"{p['ideal_segments'].replace('|', ', ')}; {p['ideal_size_bands'].replace('|', ', ')} staff",
                "indicative_price": f"S${p['recurring_fee_sgd']}/{p['billing_unit']}/month + S${p['setup_fee_sgd']} setup (synthetic)",
                "customer_already_owns": bool(owned and p["product_id"] in owned), "score": score,
            })
    matches.sort(key=lambda m: -m["score"])
    return {"bot": "singtel-product-assistant (mock, on-prem)", "question": question, "matched_needs": sorted(tags),
            "answers": matches[:3], "disclaimer": "Synthetic catalogue; not Singtel's real products, prices or coverage."}
