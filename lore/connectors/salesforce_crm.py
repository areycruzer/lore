"""Live CRM backend over Salesforce. Returns dicts with the synthetic export's snake_case keys plus `sf_id`.

LORE_SF_BACKEND=tsmztech (default, open-source MCP server with full read/write) or hosted (official Salesforce
Hosted MCP Server, see lore/connectors/sf_hosted.py).
"""
from __future__ import annotations

import os
import re
from typing import Any

BACKEND = os.environ.get("LORE_SF_BACKEND", "tsmztech")

ACCOUNT_FIELDS = ["Id", "Lore_Id__c", "Name", "Industry", "NumberOfEmployees", "AnnualRevenue", "BillingCity", "BillingCountry",
                  "Type", "Website", "Description", "Lore_Owner__c", "Lore_Owned_Products__c", "Lore_Status__c",
                  "Lore_Renewal_Date__c", "Lore_Budget_SGD__c", "Lore_Relationship_Health__c"]
CONTACT_FIELDS = ["Id", "Lore_Id__c", "FirstName", "LastName", "Title", "Department", "Email", "Lore_Do_Not_Contact__c",
                  "ReportsTo.Lore_Id__c", "Lore_Influence__c", "Lore_Authority__c", "Lore_Champion__c", "Lore_Consent__c"]
OPP_FIELDS = ["Id", "Lore_Id__c", "Name", "StageName", "Amount", "Probability", "CloseDate", "NextStep", "Description"]
CASE_FIELDS = ["Id", "Lore_Id__c", "Subject", "Status", "Priority", "Origin", "Description", "Lore_Sales_Signal__c",
               "Lore_Signal_Type__c", "Lore_Signal_Product__c", "Lore_Outreach_Gate__c", "Lore_Tier__c"]


def _backend():
    if BACKEND == "hosted":
        from lore.connectors import sf_hosted

        class _Hosted:
            @staticmethod
            def query(sobject, fields, where=None, order_by=None, limit=None):
                soql = f"SELECT {', '.join(fields)} FROM {sobject}" + (f" WHERE {where}" if where else "") + \
                       (f" ORDER BY {order_by}" if order_by else "") + (f" LIMIT {limit}" if limit else "")
                rows = sf_hosted.soql(soql)
                return [_flatten(r) for r in rows]

            @staticmethod
            def dml(operation, sobject, records, external_id_field=None):
                res = sf_hosted.dml(operation, sobject, records, external_id_field)
                ok = sum(1 for r in res if r.get("success"))
                return {"successful": ok, "failed": len(res) - ok, "detail": res}

        return _Hosted
    from lore.connectors import sf_tsmztech

    return sf_tsmztech


def _flatten(r: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in r.items():
        if k == "attributes":
            continue
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("'", "\\'")


def parse_description(desc: str | None) -> dict:
    out: dict[str, str] = {}
    for line in (desc or "").splitlines():
        m = re.match(r"^([a-z_]+): (.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _truthy(v: Any) -> bool:
    return str(v).lower() == "true"


class SalesforceCRM:
    backend = "salesforce"

    def __init__(self) -> None:
        self.b = _backend()

    def query(self, sobject: str, fields: list[str], where: str | None = None, order_by: str | None = None, limit: int | None = None):
        return self.b.query(sobject, fields, where, order_by, limit)

    def dml(self, operation: str, sobject: str, records: list[dict], external_id_field: str | None = None) -> dict:
        return self.b.dml(operation, sobject, records, external_id_field)

    # ------------------------------------------------------------ reads mapped to export keys
    def _account(self, r: dict) -> dict:
        d = parse_description(r.get("Description"))
        return {"sf_id": r.get("Id"), "account_id": r.get("Lore_Id__c"), "account_name": r.get("Name"), "industry": r.get("Industry"),
                "employee_count": r.get("NumberOfEmployees"), "annual_revenue_sgd": r.get("AnnualRevenue"), "city": r.get("BillingCity"),
                "country": r.get("BillingCountry"), "account_status": r.get("Lore_Status__c") or r.get("Type"), "website": r.get("Website"),
                "owner_id": r.get("Lore_Owner__c"), "owned_product_ids": r.get("Lore_Owned_Products__c"),
                "contract_renewal_date": r.get("Lore_Renewal_Date__c"), "budget_estimate_sgd": r.get("Lore_Budget_SGD__c"),
                "relationship_health": r.get("Lore_Relationship_Health__c"), **d}

    def list_accounts(self) -> list[dict]:
        return [self._account(r) for r in self.query("Account", ACCOUNT_FIELDS, "Lore_Id__c != null", "Lore_Id__c", 200)]

    def get_account(self, account_id: str) -> dict | None:
        rows = self.query("Account", ACCOUNT_FIELDS, f"Lore_Id__c = '{esc(account_id)}'", limit=1)
        return self._account(rows[0]) if rows else None

    def get_contacts(self, account_id: str) -> list[dict]:
        rows = self.query("Contact", CONTACT_FIELDS, f"Account.Lore_Id__c = '{esc(account_id)}'", "LastName", 200)
        return [{"sf_id": r.get("Id"), "contact_id": r.get("Lore_Id__c"), "account_id": account_id,
                 "full_name": f"{r.get('FirstName') or ''} {r.get('LastName') or ''}".strip(), "job_title": r.get("Title"),
                 "department": r.get("Department"), "email": r.get("Email"), "do_not_contact": _truthy(r.get("Lore_Do_Not_Contact__c")),
                 "reports_to_contact_id": r.get("ReportsTo.Lore_Id__c"), "influence_level": r.get("Lore_Influence__c"),
                 "authority": r.get("Lore_Authority__c"), "is_champion": _truthy(r.get("Lore_Champion__c")),
                 "consent_status": r.get("Lore_Consent__c")} for r in rows]

    def get_opportunities(self, account_id: str) -> list[dict]:
        rows = self.query("Opportunity", OPP_FIELDS, f"Account.Lore_Id__c = '{esc(account_id)}'", "CloseDate", 200)
        return [{"sf_id": r.get("Id"), "opportunity_id": r.get("Lore_Id__c"), "account_id": account_id, "opportunity_name": r.get("Name"),
                 "stage": r.get("StageName"), "amount_sgd": r.get("Amount"), "probability": r.get("Probability"),
                 "expected_close_date": r.get("CloseDate"), "next_step": r.get("NextStep"), **parse_description(r.get("Description"))}
                for r in rows]

    def get_cases(self, account_id: str) -> list[dict]:
        rows = self.query("Case", CASE_FIELDS, f"Account.Lore_Id__c = '{esc(account_id)}'", "CreatedDate", 200)
        return [{"sf_id": r.get("Id"), "ticket_id": r.get("Lore_Id__c"), "account_id": account_id, "subject": r.get("Subject"),
                 "ticket_status": r.get("Status"), "priority": r.get("Priority"), "channel": r.get("Origin"),
                 "sales_signal": str(_truthy(r.get("Lore_Sales_Signal__c"))).lower(), "signal_type": r.get("Lore_Signal_Type__c"),
                 "signal_product_id": r.get("Lore_Signal_Product__c"), "outreach_gate": r.get("Lore_Outreach_Gate__c"),
                 "support_tier": r.get("Lore_Tier__c"), **parse_description(r.get("Description"))} for r in rows]

    def soql(self, query: str) -> list[dict]:
        m = re.match(r"\s*select\s+(.+?)\s+from\s+(\w+)(?:\s+where\s+(.+?))?(?:\s+order\s+by\s+(.+?))?(?:\s+limit\s+(\d+))?\s*$",
                     query, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            raise ValueError("Use SELECT <fields> FROM <Object> [WHERE ...] [ORDER BY ...] [LIMIT n]")
        fields = [f.strip() for f in m.group(1).split(",")]
        return self.query(m.group(2), fields, m.group(3), m.group(4), int(m.group(5)) if m.group(5) else 200)
