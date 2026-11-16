"""Ops -> AM bridge: turn technical support reports into plain-language sales context for the account manager."""
from __future__ import annotations

from lore.connectors.crm import load_table
from lore.connectors.mock_sources import contacts, products

CLOSED_STATUSES = {"resolved", "closed"}


def owned_products(account: str) -> list[str]:
    a = next((r for r in load_table("accounts") if r["account_id"] == account), None)
    return [p for p in (a or {}).get("owned_product_ids", "").split("|") if p]


def open_churn_tickets(account: str) -> list[dict]:
    return [t for t in load_table("support_tickets") if t["account_id"] == account and t.get("signal_type") == "churn_risk"
            and t["ticket_status"] not in CLOSED_STATUSES]


def gate_for(account: str, ticket_gate: str | None = None) -> str:
    if open_churn_tickets(account):
        return "service_recovery_first"
    return ticket_gate or "permitted_demo"


def plain_language(ticket: dict, product_id: str, account_name: str, owned: bool) -> str:
    c = contacts().get(ticket.get("contact_id") or "")
    who = f"{c['full_name']} ({c['job_title']})" if c else "Their technical team"
    p = products().get(product_id)
    pname = p["product_name"] if p else product_id
    need = ticket.get("evidence_excerpt") or ticket.get("customer_message", "")
    need = need.replace("Synthetic customer message: ", "").strip().rstrip(".")
    owned_txt = "they already have it, so this is a usage or upgrade conversation" if owned else f"{account_name} does not have it yet"
    return f"{who} told Ops: \"{need}.\" In sales terms this is {pname}; {owned_txt}."
