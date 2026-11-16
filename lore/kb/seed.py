"""Build the knowledge base from the mocked Singtel sources (synthetic CRM export) plus the W46 demo story.

Deterministic and offline: no model calls. New material after seeding is ingested by the ingest agent through the MCP.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from lore import config
from lore.bridge import gate_for, owned_products, plain_language
from lore.connectors.crm import load_table
from lore.connectors.mock_sources import (
    Doc,
    activity_doc,
    crm_account_doc,
    crm_opportunity_doc,
    ops_report_doc,
    products,
    public_signal_doc,
)
from lore.kb.store import KnowledgeBase

ACTOR = "seed-loader"


def _raw(kb: KnowledgeBase, d: Doc) -> str:
    return kb.add_raw(d.type, d.account, d.occurred_at, d.text, d.origin, actor=ACTOR, title=d.title, commit=False)


def _ev(kb: KnowledgeBase, raw: str, d: Doc, anchor: str, actor: str = ACTOR, **event) -> str | None:
    line = d.anchors.get(anchor)
    if not line:
        return None
    return kb.add_event({"account": d.account, "source": f"{raw}#L{line}", **event}, actor=actor, commit=False, rebuild=False)


def _bool(v: str) -> bool:
    return str(v).lower() == "true"


def seed_account(kb: KnowledgeBase, a: dict) -> None:
    acc = a["account_id"]
    kb.add_account({
        "account_id": acc, "name": a["account_name"], "status": a["account_status"], "sector": a["industry"],
        "segment": a["segment"], "hq": f"{a['city']}, {a['country']}", "employees": int(a["employee_count"] or 0),
        "owner": a["owner_id"], "renewal": a.get("contract_renewal_date") or None,
        "owned_products": [p for p in a["owned_product_ids"].split("|") if p], "relationship_health": a.get("relationship_health"),
        "domain": a["domain"],
    }, body=f"# {a['account_name']}\n\n{a.get('business_objective', '')}\n", actor=ACTOR, commit=False)

    for c in (r for r in load_table("contacts") if r["account_id"] == acc):
        kb.add_contact(acc, {
            "contact_id": c["contact_id"], "name": c["full_name"], "role": c["job_title"], "department": c["department"],
            "authority": c["authority"], "influence": c["influence_level"], "is_champion": _bool(c["is_champion"]),
            "reports_to": c["reports_to_contact_id"] or None, "consent": c["consent_status"],
            "do_not_contact": _bool(c["do_not_contact"]), "last_contacted": c["last_contacted_date"] or None,
            "interests": c["interests"],
        }, body=f"# {c['full_name']}\n\n{c['relationship_notes']}\n", actor=ACTOR, commit=False)

    d = crm_account_doc(a)
    raw = _raw(kb, d)
    _ev(kb, raw, d, "account_status", kind="status", topic="status", value=a["account_status"],
        summary=f"CRM status: {a['account_status'].replace('_', ' ')}", epistemic_status="confirmed")
    if a.get("budget_estimate_sgd"):
        _ev(kb, raw, d, "budget_estimate_sgd", kind="budget", topic="budget", value=int(float(a["budget_estimate_sgd"])),
            summary=f"Budget estimate S${int(float(a['budget_estimate_sgd'])):,} ({a.get('budget_status') or 'unconfirmed'})",
            epistemic_status="hedged" if a.get("budget_status") != "approved" else "confirmed")
    if a.get("contract_renewal_date"):
        _ev(kb, raw, d, "contract_renewal_date", kind="timeline", topic="renewal", value=a["contract_renewal_date"],
            summary=f"Contract renews {a['contract_renewal_date']}", epistemic_status="confirmed")
    if a.get("pain_points"):
        _ev(kb, raw, d, "pain_points", kind="need", topic="pain_points", value=a["pain_points"],
            summary=f"Known pains: {a['pain_points'].replace('|', ', ')}", epistemic_status="stated")
    if a.get("owned_product_ids"):
        _ev(kb, raw, d, "owned_product_ids", kind="product_owned", topic="owned_products", value=a["owned_product_ids"],
            summary=f"Owns: {', '.join(products()[p]['product_name'] for p in a['owned_product_ids'].split('|') if p in products())}",
            epistemic_status="confirmed")
    if a.get("relationship_health"):
        _ev(kb, raw, d, "relationship_health", kind="relationship", topic="relationship_health", value=a["relationship_health"],
            summary=f"Relationship health: {a['relationship_health']}; competitor risk {a.get('competitor_risk') or 'unknown'}",
            epistemic_status="stated")

    for o in (r for r in load_table("opportunities") if r["account_id"] == acc):
        d = crm_opportunity_doc(o)
        raw = _raw(kb, d)
        _ev(kb, raw, d, "stage", kind="opportunity", topic=f"opp:{o['opportunity_id']}", value=o["stage"],
            summary=f"{o['opportunity_name']}: {o['stage']}, S${float(o['amount_sgd'] or 0):,.0f}, close {o['expected_close_date']}",
            epistemic_status="confirmed", extra={"opportunity_id": o["opportunity_id"], "products": o["product_ids"]})
        if o.get("next_step") and o["stage"] not in ("Closed Won", "Closed Lost"):
            _ev(kb, raw, d, "next_step", kind="commitment", summary=f"Next step on {o['opportunity_id']}: {o['next_step']}",
                epistemic_status="stated")

    for act in (r for r in load_table("activities") if r["account_id"] == acc and r["activity_type"] != "task"):
        d = activity_doc(act)
        raw = _raw(kb, d)
        _ev(kb, raw, d, "customer_need", kind="meeting_note", summary=f"{act['subject']} ({act['activity_type']})",
            epistemic_status="stated", said_by=act["contact_id"] or None)
        if act.get("next_action") and (act["activity_status"] != "completed" or (act.get("next_action_due") or "") >= config.TODAY):
            _ev(kb, raw, d, "next_action", kind="commitment", summary=f"{act['next_action']} ({act['activity_date']})",
                epistemic_status="stated")

    owned = owned_products(acc)
    for t in (r for r in load_table("support_tickets") if r["account_id"] == acc):
        d = ops_report_doc(t)
        raw = _raw(kb, d)
        if t["sales_signal"] != "true":
            _ev(kb, raw, d, "customer_message", kind="ops_request", summary=f"Routine ops request {t['ticket_id']}: {t['subject']} (not a sales signal)",
                epistemic_status="stated", said_by=t["contact_id"] or None, gate="not_applicable",
                extra={"ticket": t["ticket_id"], "bridge": False})
            continue
        if t["signal_type"] == "churn_risk":
            _ev(kb, raw, d, "evidence", kind="churn_risk", summary=f"Churn risk from ops ticket {t['ticket_id']} ({t['ticket_status']}): {t['evidence_excerpt']}",
                epistemic_status="stated", said_by=t["contact_id"] or None, gate="service_recovery_first", product=t["signal_product_id"] or None)
            continue
        pid = t["signal_product_id"]
        _ev(kb, raw, d, "evidence", kind="ops_request", summary=plain_language(t, pid, a["account_name"], pid in owned),
            epistemic_status="stated", said_by=t["contact_id"] or None, product=pid,
            gate=gate_for(acc, t["outreach_gate"]), extra={"ticket": t["ticket_id"], "bridge": pid not in owned})
        _ev(kb, raw, d, "evidence", kind="signal", summary=f"Expansion signal via Ops ({t['ticket_id']}): {t['evidence_excerpt']}",
            epistemic_status="stated", product=pid, gate=gate_for(acc, t["outreach_gate"]))

    for s in (r for r in load_table("buying_signals") if r["account_id"] == acc and r["source_object"] == "Accounts"):
        d = public_signal_doc(s, a["account_name"])
        raw = _raw(kb, d)
        kind = "churn_risk" if s["signal_type"] == "churn_risk" else "signal"
        _ev(kb, raw, d, "evidence", kind=kind, summary=f"{s['signal_type'].replace('_', ' ').title()}: " + d.lines[1].removeprefix("Excerpt: "),
            epistemic_status="reported", product=s.get("product_id") or None, gate=s["outreach_gate"] if s["outreach_gate"] in
            ("permitted_demo", "service_recovery_first", "consent_review") else None,
            extra={"signal_type": s["signal_type"], "potential_sgd": s.get("estimated_potential_sgd")})


def product_pages(kb: KnowledgeBase) -> None:
    for p in load_table("products"):
        body = (f"---\nid: {p['product_id']}\nname: {p['product_name']}\ncategory: {p['category']}\npain_tags: {p['pain_tags'].replace('|', ', ')}\n---\n"
                f"# {p['product_name']}\n\n{p['description']}\n\n- Solves: {p['pain_tags'].replace('|', ', ')}\n"
                f"- Ideal for: {p['ideal_segments'].replace('|', ', ')}; {p['ideal_size_bands'].replace('|', ', ')} staff; min sites {p['min_sites']}\n"
                f"- Indicative price: S${p['recurring_fee_sgd']}/{p['billing_unit']}/month, S${p['setup_fee_sgd']} setup, min term {p['min_term_months']} months\n"
                f"- {p['price_disclaimer']}\n")
        kb.write_page(f"wiki/products/{p['product_id']}.md", body, actor=ACTOR, commit=False)


def seed_demo_story(kb: KnowledgeBase) -> None:
    """Week 46 for ACC-0001: a budget cut said on a call, a press date that contradicts the meeting,
    and an ops request from the customer's engineer that maps to a product they do not own."""
    acc = "ACC-0001"
    actor = "ingest-agent"
    kb.add_contact(acc, {"contact_id": "CON-9001", "name": "Demo Sam Lee 001", "role": "Senior Network Engineer",
                         "department": "Technology", "authority": "tech", "influence": "influencer", "is_champion": True,
                         "reports_to": "CON-0001", "consent": "granted", "do_not_contact": False, "last_contacted": "2026-11-12",
                         "interests": "security monitoring|branch rollout"},
                   body="# Demo Sam Lee 001\n\nRaised the 24x7 monitoring request with Ops. Strong advocate for Singtel inside IT.\n",
                   actor=actor, commit=False)

    budget_ev = next(e for e in kb.events(acc) if e.get("topic") == "budget")

    ops = Doc("ops_report", acc, "2026-11-12", "confluence:OPS/TKT-9001", "L2 ops report TKT-9001: 24x7 security monitoring request")
    ops.add("header", "Ticket: TKT-9001 | Tier L2 | feature_request | priority medium | status open")
    ops.add("raised_by", "Raised by: Demo Sam Lee 001 (Senior Network Engineer) via Teams call; handled by Demo Team Member 11 (support specialist)")
    ops.add("product", "Service affected: Demo Managed SD-WAN (p-demo-sdwan)")
    ops.add("customer_message", "Customer message: Our auditors now want 24x7 evidence of security monitoring across HQ and the new branches. Can you give us one dashboard of security alerts and someone watching it overnight?")
    ops.add("technical_summary", "Technical summary: Request is for managed SIEM correlation and SOC monitoring with audit reporting. Outside current SD-WAN and Dedicated Internet scope. No fault found.")
    ops.add("root_cause", "Root cause: Not a fault; capability gap versus current contract.")
    ops.add("signal", "Sales-relevant: yes (expansion, product p-demo-mss). Reason: Customer engineer asked for a capability Singtel sells but the customer has not bought.")
    ops.add("next_action", "Next action: Hand to account manager; Ops cannot quote (due 2026-11-19)")
    raw_ops = _raw(kb, ops)

    meet = Doc("meeting_transcript", acc, "2026-11-13", "call-storage:TEAMS-2026-11-13-asterforge-qbr", "Quarterly review: branch rollout and service stability")
    meet.add("subject", "Subject: Quarterly account review, branch rollout and service stability")
    meet.add("when", "Date: 2026-11-13 (45 min, Teams video call)")
    meet.add("who", "Attendees: Demo Team Member 02 (Singtel AM); Demo Avery Tan 001 (IT Director); Demo Jordan Tan 001 (Chief Financial Officer)")
    meet.add("budget", "[Jordan Tan, CFO] The board approved the branch programme but cut phase one. Connectivity budget this financial year is S$120k, not the S$209k we discussed in March.")
    meet.add("go_live", "[Avery Tan, IT Director] We are hoping to have the first two branches live in February 2027, Johor first.")
    meet.add("objection", "[Avery Tan, IT Director] Honestly the failover incidents in October hurt us. Fix that and then we can talk about more.")
    meet.add("procurement", "[Jordan Tan, CFO] Anything above S$100k still goes through a competitive RFP.")
    meet.add("commitment", "[Singtel AM] I will send the root-cause report for the October outages by 20 Nov and a phase-one SD-WAN proposal by 25 Nov.")
    raw_meet = _raw(kb, meet)

    press = Doc("public_signal", acc, "2026-11-14", "web:press/asterforge-branches", "Press: Asterforge Manufacturing plans three regional branches")
    press.add("headline", "Headline: Demo Asterforge Manufacturing to open three regional branches")
    press.add("go_live", "Excerpt: The company expects all three new branches, including Johor, to be operational by Q3 2027.")
    raw_press = _raw(kb, press)

    mail = Doc("customer_email", acc, "2026-11-14", "outlook:AAMkAD-demo-asterforge-1114", "Re: Thanks for the review")
    mail.add("from", "From: Demo Avery Tan 001 (IT Director) to Demo Team Member 02")
    mail.add("rca", "[Customer] Thanks for the call. Please send the RCA for the October outages first.")
    mail.add("interest", "[Customer] Once we are confident in stability, Sam's monitoring request is something we would like to scope for the branches.")
    raw_mail = _raw(kb, mail)

    _ev(kb, raw_ops, ops, "customer_message", actor=actor, kind="ops_request",
        summary="Demo Sam Lee 001 (Senior Network Engineer) told Ops their auditors need round-the-clock security monitoring with one alert dashboard. "
                "In sales terms this is Demo Managed Security Services (plus the Security Operations add-on); Asterforge does not have it yet.",
        epistemic_status="stated", said_by="CON-9001", product="p-demo-mss", gate=gate_for(acc),
        extra={"ticket": "TKT-9001", "bridge": True, "also": "p-demo-soc"})
    _ev(kb, raw_meet, meet, "budget", actor=actor, kind="budget", topic="budget", value=120000,
        summary="Budget cut to S$120k for phase one (was S$209k)", epistemic_status="stated", said_by="CON-0002",
        supersedes=[budget_ev.id])
    _ev(kb, raw_meet, meet, "go_live", actor=actor, kind="timeline", topic="branch_go_live", value="2027-02",
        summary="First two branches hoped live Feb 2027, Johor first", epistemic_status="hedged", said_by="CON-0001")
    _ev(kb, raw_meet, meet, "objection", actor=actor, kind="objection",
        summary="IT Director: October failover incidents hurt trust; fix stability before any new pitch", epistemic_status="stated",
        said_by="CON-0001", gate="service_recovery_first")
    _ev(kb, raw_meet, meet, "procurement", actor=actor, kind="fact", topic="procurement", value="rfp_over_100k",
        summary="Competitive RFP required above S$100k", epistemic_status="stated", said_by="CON-0002")
    _ev(kb, raw_meet, meet, "commitment", actor=actor, kind="commitment",
        summary="AM to send October outage RCA by 20 Nov and phase-one SD-WAN proposal by 25 Nov", epistemic_status="confirmed")
    _ev(kb, raw_press, press, "go_live", actor="cron:company-watch", kind="timeline", topic="branch_go_live", value="2027-Q3",
        summary="Press: all three branches operational by Q3 2027", epistemic_status="reported")
    _ev(kb, raw_mail, mail, "interest", actor=actor, kind="signal", product="p-demo-mss",
        summary="IT Director wants to scope Sam's monitoring request once stability is proven", epistemic_status="stated",
        said_by="CON-0001", gate="service_recovery_first")


def run(reset: bool = False) -> dict:
    kb = KnowledgeBase()
    if reset and kb.root.exists():
        shutil.rmtree(kb.root)
    kb.init()
    if kb.list_accounts():
        raise SystemExit(f"{kb.root} already seeded; pass --reset to rebuild from scratch")
    accounts = load_table("accounts")
    for a in accounts:
        seed_account(kb, a)
    product_pages(kb)
    seed_demo_story(kb)
    from lore.kb.rollup import weekly_rollup

    for a in accounts:
        kb.rebuild_account(a["account_id"], actor=ACTOR, commit=False)
    weekly_rollup(kb, config.TODAY, actor=ACTOR, commit=False)
    kb.rebuild_index(actor=ACTOR, commit=False)
    if kb.git_enabled:
        subprocess.run(["git", "add", "-A"], cwd=kb.root, check=False, capture_output=True)
        r = subprocess.run(["git", "commit", "-q", "-m", "seed: synthetic Singtel CRM, ops, meetings, email, signals\n\nLore-Actor: seed-loader"],
                           cwd=kb.root, capture_output=True, text=True)
        git_status = "committed" if r.returncode == 0 else f"git commit skipped: {(r.stderr or r.stdout).strip()[:200]}"
    else:
        git_status = "git disabled"
    n_raw = sum(1 for _ in kb.raw_dir.rglob("*.md"))
    n_ev = sum(1 for _ in (kb.wiki_dir / "accounts").rglob("timeline/**/*.md"))
    ok, n = kb.verify_ledger()
    return {"accounts": len(accounts), "raw": n_raw, "events": n_ev, "ledger_entries": n, "ledger_ok": ok, "git": git_status,
            "kb": str(kb.root)}


if __name__ == "__main__":
    import sys

    print(json.dumps(run(reset="--reset" in sys.argv), indent=2))
