"""Least-privilege access: who may see which accounts and contacts. Every tool call is checked here."""
from __future__ import annotations

from dataclasses import dataclass

from lore.connectors.crm import load_table


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str
    accounts: frozenset[str]

    def can_see(self, account: str | None) -> bool:
        return account is None or account in self.accounts


def principal(user_id: str) -> Principal:
    owners = {o["owner_id"]: o for o in load_table("owners")}
    accounts = load_table("accounts")
    if user_id.startswith("job:"):
        # Scheduled jobs run with service scope over every account; their writes are attributed to the job.
        return Principal(user_id, user_id, "scheduled_job", frozenset(a["account_id"] for a in accounts))
    o = owners.get(user_id)
    if not o:
        return Principal(user_id, user_id, "unknown", frozenset())
    role = o["role"]
    if role == "sales_manager":
        team = {oid for oid, x in owners.items() if x["manager_owner_id"] == user_id} | {user_id}
        visible = {a["account_id"] for a in accounts if a["owner_id"] in team}
    elif role in ("support_specialist", "sales_engineer"):
        # Ops and SEs see accounts they have worked a ticket or opportunity on.
        visible = {t["account_id"] for t in load_table("support_tickets") if user_id in (t["owner_id"], t.get("handoff_owner_id"))}
        visible |= {a["account_id"] for a in accounts if a["owner_id"] == user_id}
    else:
        visible = {a["account_id"] for a in accounts if a["owner_id"] == user_id}
    return Principal(user_id, o["full_name"], role, frozenset(visible))


def denied(account: str, p: Principal) -> str:
    return (f"Access denied: {p.name} ({p.role}) is not on the account team for {account}. "
            "Lore applies least-privilege access; ask the account owner or your manager.")
