"""Load the synthetic CRM export into a Salesforce org (Developer Edition friendly).

The Salesforce CLI (`sf`) deploys the metadata in `salesforce/` and supplies the access token; data goes in
through the REST API (sObject Collections upsert by the `Lore_Id__c` external id). Log in once with:

    sf org login web --alias lore --instance-url https://login.salesforce.com

Usage:
    uv run python -m lore.sf_load deploy  [--alias lore]
    uv run python -m lore.sf_load load    [--alias lore] [--data-dir DIR]
    uv run python -m lore.sf_load status  [--alias lore]
    uv run python -m lore.sf_load all     [--alias lore]      # deploy + load + status
    uv run python -m lore.sf_load dry-run [--data-dir DIR]    # build every payload offline, no network

Fields without a dedicated Salesforce home are written to Description as `key: value` lines (snake_case keys
from the export), including the original value of every column squeezed into a standard picklist, so a
reader can recover the export row.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from lore import config

EXTERNAL_ID = "Lore_Id__c"
API_VERSION = "62.0"
BATCH_SIZE = 200
DEFAULT_ALIAS = "lore"
PERMISSION_SET = "Lore_Access"
SF_PROJECT_DIR = config.ROOT / "salesforce"
TASK_ACTIVITY_TYPES = frozenset({"meeting", "call", "email"})
LOAD_ORDER = ("Product2", "Account", "Contact", "Opportunity", "Case", "Task")
DESCRIPTION_LIMIT = 32000

LOGIN_HINT = (
    "The Salesforce CLI is not authorized for org alias {alias!r}. Log in once with:\n"
    "  sf org login web --alias {alias} --instance-url https://login.salesforce.com"
)

# --------------------------------------------------------------------------------------------------------
# Picklist mappings (synthetic value -> standard picklist value of a fresh Developer Edition org)
# --------------------------------------------------------------------------------------------------------

ACCOUNT_TYPE = {"existing_customer": "Customer", "prospect": "Prospect", "disqualified": "Other"}
INDUSTRY = {
    "Logistics": "Transportation",
    "Financial Services": "Finance",
    "Professional Services": "Consulting",
}
STAGE = {
    "Discovery": "Prospecting",
    "Qualification": "Qualification",
    "Solution Design": "Value Proposition",
    "Proposal": "Proposal/Price Quote",
    "Negotiation": "Negotiation/Review",
    "Closed Won": "Closed Won",
    "Closed Lost": "Closed Lost",
}
CASE_STATUS = {
    "open": "New",
    "in_progress": "Working",
    "pending_customer": "Working",
    "escalated": "Escalated",
    "resolved": "Closed",
    "closed": "Closed",
}
CASE_PRIORITY = {"critical": "High", "high": "High", "medium": "Medium", "low": "Low"}
CASE_ORIGIN = {"email": "Email", "call": "Phone", "phone": "Phone", "portal": "Web", "web": "Web", "chat": "Web"}
TASK_STATUS = {"completed": "Completed", "scheduled": "Not Started"}

VALID_PICKLISTS: dict[tuple[str, str], frozenset[str]] = {
    ("Account", "Type"): frozenset({"Customer", "Prospect", "Other"}),
    ("Account", "Industry"): frozenset({
        "Agriculture", "Apparel", "Banking", "Biotechnology", "Chemicals", "Communications", "Construction",
        "Consulting", "Education", "Electronics", "Energy", "Engineering", "Entertainment", "Environmental",
        "Finance", "Food & Beverage", "Government", "Healthcare", "Hospitality", "Insurance", "Machinery",
        "Manufacturing", "Media", "Not For Profit", "Other", "Recreation", "Retail", "Shipping", "Technology",
        "Telecommunications", "Transportation", "Utilities",
    }),
    ("Opportunity", "StageName"): frozenset({
        "Prospecting", "Qualification", "Needs Analysis", "Value Proposition", "Id. Decision Makers",
        "Perception Analysis", "Proposal/Price Quote", "Negotiation/Review", "Closed Won", "Closed Lost",
    }),
    ("Case", "Status"): frozenset({"New", "Working", "Escalated", "Closed"}),
    ("Case", "Priority"): frozenset({"High", "Medium", "Low"}),
    ("Case", "Origin"): frozenset({"Phone", "Email", "Web"}),
    ("Task", "Status"): frozenset({"Completed", "Not Started"}),
    ("Task", "Priority"): frozenset({"Normal"}),
}

# Text field length limits; "Object.Field" entries override the bare field name.
LIMITS = {
    "Name": 255,
    "Opportunity.Name": 120,
    "Subject": 255,
    "Title": 128,
    "FirstName": 40,
    "LastName": 80,
    "Department": 80,
    "Email": 80,
    "Website": 255,
    "BillingCity": 40,
    "BillingCountry": 80,
    "MailingCountry": 80,
    "NextStep": 255,
    "ProductCode": 255,
    "Description": DESCRIPTION_LIMIT,
    EXTERNAL_ID: 40,
    "Lore_Owner__c": 20,
    "Lore_Owned_Products__c": 255,
    "Lore_Status__c": 40,
    "Lore_Relationship_Health__c": 40,
    "Lore_Influence__c": 40,
    "Lore_Authority__c": 20,
    "Lore_Consent__c": 20,
    "Lore_Signal_Type__c": 40,
    "Lore_Signal_Product__c": 40,
    "Lore_Outreach_Gate__c": 40,
    "Lore_Tier__c": 5,
}

# Export columns stored losslessly in a dedicated field; they are left out of Description. Columns mapped into a
# standard picklist are deliberately absent so their original value survives in Description.
DIRECT_COLUMNS = {
    "Product2": {"product_id", "product_name", "sku", "is_active"},
    "Account": {
        "account_id", "account_name", "website", "employee_count", "annual_revenue_sgd", "country", "city",
        "owner_id", "owned_product_ids", "parent_account_id", "account_status", "contract_renewal_date",
        "budget_estimate_sgd", "relationship_health",
    },
    "Contact": {
        "contact_id", "account_id", "full_name", "job_title", "department", "email", "country", "do_not_contact",
        "reports_to_contact_id", "influence_level", "authority", "is_champion", "consent_status",
    },
    "Opportunity": {"opportunity_id", "account_id", "opportunity_name", "amount_sgd", "expected_close_date", "next_step"},
    "Case": {
        "ticket_id", "account_id", "contact_id", "subject", "sales_signal", "signal_type", "signal_product_id",
        "outreach_gate", "support_tier",
    },
    "Task": {"activity_id", "subject", "activity_date"},
}

# Narrative columns written first in Description so they survive truncation.
RICH_COLUMNS = {
    "Product2": ("description", "price_disclaimer"),
    "Account": ("pain_points", "business_objective", "strategic_notes", "next_best_action"),
    "Contact": ("relationship_notes", "interests"),
    "Opportunity": ("pain_summary", "business_value_hypothesis", "success_metric", "next_step", "win_loss_reason"),
    "Case": ("customer_message", "technical_summary", "root_cause", "signal_reason", "evidence_excerpt",
             "resolution_summary", "next_action"),
    "Task": ("body_summary", "outcome", "next_action"),
}

_KEY_RE = re.compile(r"^[a-z_]+$")


class LoadError(RuntimeError):
    pass


class NotAuthorized(LoadError):
    pass


# --------------------------------------------------------------------------------------------------------
# CSV -> Salesforce record mapping (pure functions, no network)
# --------------------------------------------------------------------------------------------------------


def read_table(name: str, data_dir: str | Path | None = None) -> list[dict[str, str]]:
    path = Path(data_dir or config.DATA_DIR) / f"{name}.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def load_tables(data_dir: str | Path | None = None) -> dict[str, list[dict[str, str]]]:
    names = ("products", "accounts", "contacts", "opportunities", "support_tickets", "activities")
    return {name: read_table(name, data_dir) for name in names}


def truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…" if limit > 1 else value[:limit]
    return value


def _limit_for(sobject: str, field_name: str) -> int | None:
    return LIMITS.get(f"{sobject}.{field_name}", LIMITS.get(field_name))


def _bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y"}


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def ext_ref(value: str | None) -> dict[str, str] | None:
    """Relationship reference by external id, e.g. `"Account": {"Lore_Id__c": "ACC-0001"}`."""
    return {EXTERNAL_ID: value} if value else None


def poly_ref(sobject: str, value: str | None) -> dict[str, Any] | None:
    """Polymorphic reference (Task.What / Task.Who) by external id; the target type must be named."""
    return {"attributes": {"type": sobject}, EXTERNAL_ID: value} if value else None


def split_name(full_name: str) -> tuple[str, str]:
    """FirstName/LastName from a full name; a trailing number stays with the surname ("Demo Avery", "Tan 001")."""
    parts = (full_name or "").split()
    if not parts:
        return "", "Unknown"
    if len(parts) == 1:
        return "", parts[0]
    if len(parts) >= 3 and parts[-1].isdigit():
        return " ".join(parts[:-2]), " ".join(parts[-2:])
    return " ".join(parts[:-1]), parts[-1]


def format_description(fields: Iterable[tuple[str, Any]], limit: int = DESCRIPTION_LIMIT) -> str:
    """One `key: value` line per non-empty field; newlines inside a value collapse to spaces."""
    lines = []
    for key, value in fields:
        if value is None or not _KEY_RE.match(key):
            continue
        text = re.sub(r"\s+", " ", str(value)).strip()
        if text:
            lines.append(f"{key}: {text}")
    return truncate("\n".join(lines), limit)


def describe(sobject: str, row: Mapping[str, str]) -> str:
    direct = DIRECT_COLUMNS[sobject]
    rich = [k for k in RICH_COLUMNS[sobject] if k in row]
    rest = [k for k in row if k not in direct and k not in rich]
    return format_description((k, row[k]) for k in (*rich, *rest))


def finalize(sobject: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Add `attributes`, drop empty values and cut text to the Salesforce field length."""
    record: dict[str, Any] = {"attributes": {"type": sobject}}
    for name, value in fields.items():
        if value is None or value == "":
            continue
        limit = _limit_for(sobject, name)
        record[name] = truncate(value, limit) if limit else value
    return record


def product_record(row: Mapping[str, str]) -> dict[str, Any]:
    return finalize("Product2", {
        EXTERNAL_ID: row["product_id"],
        "Name": row.get("product_name") or row["product_id"],
        "ProductCode": row.get("sku"),
        "IsActive": _bool(row.get("is_active")),
        "Description": describe("Product2", row),
    })


def account_record(row: Mapping[str, str]) -> dict[str, Any]:
    industry = row.get("industry") or ""
    return finalize("Account", {
        EXTERNAL_ID: row["account_id"],
        "Name": row.get("account_name") or row["account_id"],
        "Type": ACCOUNT_TYPE.get(row.get("account_status") or "", "Other"),
        "Industry": INDUSTRY.get(industry, industry) if industry else None,
        "Website": row.get("website"),
        "NumberOfEmployees": _int(row.get("employee_count")),
        "AnnualRevenue": _float(row.get("annual_revenue_sgd")),
        "BillingCity": row.get("city"),
        "BillingCountry": row.get("country"),
        "Lore_Owner__c": row.get("owner_id"),
        "Lore_Owned_Products__c": row.get("owned_product_ids"),
        "Lore_Status__c": row.get("account_status"),
        "Lore_Renewal_Date__c": row.get("contract_renewal_date"),
        "Lore_Budget_SGD__c": _float(row.get("budget_estimate_sgd")),
        "Lore_Relationship_Health__c": row.get("relationship_health"),
        "Description": describe("Account", row),
    })


def account_parent_record(row: Mapping[str, str]) -> dict[str, Any] | None:
    if not row.get("parent_account_id"):
        return None
    return finalize("Account", {EXTERNAL_ID: row["account_id"], "Parent": ext_ref(row["parent_account_id"])})


def contact_record(row: Mapping[str, str]) -> dict[str, Any]:
    first, last = split_name(row.get("full_name", ""))
    return finalize("Contact", {
        EXTERNAL_ID: row["contact_id"],
        "Account": ext_ref(row.get("account_id")),
        "FirstName": first,
        "LastName": last,
        "Title": row.get("job_title"),
        "Department": row.get("department"),
        "Email": row.get("email"),
        "MailingCountry": row.get("country"),
        "Lore_Do_Not_Contact__c": _bool(row.get("do_not_contact")),
        "Lore_Influence__c": row.get("influence_level"),
        "Lore_Authority__c": row.get("authority"),
        "Lore_Champion__c": _bool(row.get("is_champion")),
        "Lore_Consent__c": row.get("consent_status"),
        "Description": describe("Contact", row),
    })


def contact_reports_to_record(row: Mapping[str, str]) -> dict[str, Any] | None:
    if not row.get("reports_to_contact_id"):
        return None
    return finalize("Contact", {EXTERNAL_ID: row["contact_id"], "ReportsTo": ext_ref(row["reports_to_contact_id"])})


def opportunity_record(row: Mapping[str, str]) -> dict[str, Any]:
    probability = _float(row.get("probability"))
    return finalize("Opportunity", {
        EXTERNAL_ID: row["opportunity_id"],
        "Account": ext_ref(row.get("account_id")),
        "Name": row.get("opportunity_name") or row["opportunity_id"],
        "StageName": STAGE.get(row.get("stage") or "", "Prospecting"),
        "CloseDate": row.get("expected_close_date") or row.get("actual_close_date") or config.TODAY,
        "Amount": _float(row.get("amount_sgd")),
        "Probability": round(probability * 100, 2) if probability is not None else None,
        "NextStep": row.get("next_step"),
        "Description": describe("Opportunity", row),
    })


def case_record(row: Mapping[str, str]) -> dict[str, Any]:
    return finalize("Case", {
        EXTERNAL_ID: row["ticket_id"],
        "Account": ext_ref(row.get("account_id")),
        "Contact": ext_ref(row.get("contact_id")),
        "Subject": row.get("subject"),
        "Status": CASE_STATUS.get(row.get("ticket_status") or "", "New"),
        "Priority": CASE_PRIORITY.get(row.get("priority") or "", "Medium"),
        "Origin": CASE_ORIGIN.get(row.get("channel") or "", "Web"),
        "Lore_Sales_Signal__c": _bool(row.get("sales_signal")),
        "Lore_Signal_Type__c": row.get("signal_type"),
        "Lore_Signal_Product__c": row.get("signal_product_id"),
        "Lore_Outreach_Gate__c": row.get("outreach_gate"),
        "Lore_Tier__c": row.get("support_tier"),
        "Description": describe("Case", row),
    })


def task_record(row: Mapping[str, str], id_maps: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Task for one meeting/call/email activity. What = the opportunity if any, else the account; Who = contact.

    Without `id_maps` the payload uses polymorphic external-id references (dry run). With `id_maps`
    (Lore id -> Salesforce Id per object, from a query) WhatId/WhoId are set directly, which works on
    every API version and avoids depending on polymorphic external-id support.
    """
    opp, acc, con = row.get("opportunity_id"), row.get("account_id"), row.get("contact_id")
    fields: dict[str, Any] = {
        EXTERNAL_ID: row["activity_id"],
        "Subject": row.get("subject") or (row.get("activity_type") or "Activity").title(),
        "ActivityDate": row.get("activity_date"),
        "Status": TASK_STATUS.get(row.get("activity_status") or "", "Completed"),
        "Priority": "Normal",
    }
    if id_maps is None:
        fields["What"] = poly_ref("Opportunity", opp) if opp else poly_ref("Account", acc)
        fields["Who"] = poly_ref("Contact", con)
    else:
        what = id_maps.get("Opportunity", {}).get(opp or "") if opp else None
        fields["WhatId"] = what or id_maps.get("Account", {}).get(acc or "")
        fields["WhoId"] = id_maps.get("Contact", {}).get(con or "")
    fields["Description"] = describe("Task", row)
    return finalize("Task", fields)


@dataclass
class Step:
    label: str
    sobject: str
    records: list[dict[str, Any]]
    mode: str = "upsert"  # "upsert" by Lore_Id__c, or "insert" (Tasks: insert new, update ones matched on Lore_Id__c)


def task_rows(tables: Mapping[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    return [r for r in tables["activities"] if (r.get("activity_type") or "").lower() in TASK_ACTIVITY_TYPES]


def build_plan(
    tables: Mapping[str, list[dict[str, str]]], id_maps: Mapping[str, Mapping[str, str]] | None = None
) -> list[Step]:
    accounts, contacts = tables["accounts"], tables["contacts"]
    return [
        Step("Product2", "Product2", [product_record(r) for r in tables["products"]]),
        Step("Account", "Account", [account_record(r) for r in accounts]),
        Step("Account (parent)", "Account", [p for r in accounts if (p := account_parent_record(r))]),
        Step("Contact", "Contact", [contact_record(r) for r in contacts]),
        Step("Contact (reports-to)", "Contact", [p for r in contacts if (p := contact_reports_to_record(r))]),
        Step("Opportunity", "Opportunity", [opportunity_record(r) for r in tables["opportunities"]]),
        Step("Case", "Case", [case_record(r) for r in tables["support_tickets"]]),
        Step("Task", "Task", [task_record(r, id_maps) for r in task_rows(tables)], mode="insert"),
    ]


def batches(records: list[dict[str, Any]], size: int = BATCH_SIZE) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(records), size):
        yield records[start : start + size]


# --------------------------------------------------------------------------------------------------------
# Salesforce CLI
# --------------------------------------------------------------------------------------------------------


def ensure_sf() -> str:
    path = shutil.which("sf")
    if path:
        return path
    npm = shutil.which("npm")
    if not npm:
        raise LoadError("Salesforce CLI `sf` not found and npm is unavailable. Install Node.js, then: npm install -g @salesforce/cli")
    print("Installing the Salesforce CLI: npm install -g @salesforce/cli", flush=True)
    subprocess.run([npm, "install", "-g", "@salesforce/cli"], check=True)
    path = shutil.which("sf")
    if not path:
        raise LoadError("Installed @salesforce/cli but `sf` is still not on PATH.")
    return path


def _cli_json(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        return {}
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return {}


@dataclass
class OrgAuth:
    instance_url: str
    access_token: str
    username: str = ""


def org_auth(alias: str = DEFAULT_ALIAS) -> OrgAuth:
    sf = ensure_sf()
    proc = subprocess.run([sf, "org", "display", "--target-org", alias, "--json"], capture_output=True, text=True, check=False)
    data = _cli_json(proc.stdout)
    result = data.get("result") or {}
    status = result.get("connectedStatus") or ""
    if proc.returncode != 0 or not result.get("accessToken") or not result.get("instanceUrl") or (
        status and status not in ("Connected", "Unknown")
    ):
        detail = data.get("message") or status or proc.stderr.strip()
        raise NotAuthorized(LOGIN_HINT.format(alias=alias) + (f"\n(sf: {detail})" if detail else ""))
    token = result["accessToken"]
    if token.startswith("[REDACTED"):
        # sf CLI >= 2.1xx redacts tokens in `org display`; the dedicated command still reveals it.
        shown = subprocess.run([sf, "org", "auth", "show-access-token", "--target-org", alias, "--no-prompt", "--json"],
                               capture_output=True, text=True, check=False)
        token = (_cli_json(shown.stdout).get("result") or {}).get("accessToken") or ""
        if shown.returncode != 0 or not token:
            raise NotAuthorized(LOGIN_HINT.format(alias=alias) + "\n(sf: could not read the access token)")
    return OrgAuth(result["instanceUrl"].rstrip("/"), token, result.get("username", ""))


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Demo accounts have similar names and can trip standard duplicate rules; save anyway.
        "Sforce-Duplicate-Rule-Header": "allowSave=true",
    }


def rest_session(alias: str = DEFAULT_ALIAS) -> tuple[str, dict[str, str]]:
    """(instance_url, headers) for REST calls as the CLI-authorized user of `alias`."""
    auth = org_auth(alias)
    return auth.instance_url, _headers(auth.access_token)


# --------------------------------------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------------------------------------


@dataclass
class StepResult:
    label: str
    attempted: int = 0
    succeeded: int = 0
    created: int = 0
    failed: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # distinct error text -> first record id

    def add(self, record: Mapping[str, Any], outcome: Mapping[str, Any]) -> None:
        self.attempted += 1
        if outcome.get("success"):
            self.succeeded += 1
            self.created += 1 if outcome.get("created") else 0
            return
        self.failed += 1
        text = "; ".join(
            f"{e.get('statusCode')}: {e.get('message')}" + (f" {e['fields']}" if e.get("fields") else "")
            for e in outcome.get("errors") or []
        ) or json.dumps(outcome)[:300]
        if text not in self.errors and len(self.errors) < 5:
            self.errors[text] = str(record.get(EXTERNAL_ID) or record.get("id") or "?")

    def fail_all(self, records: list[dict[str, Any]], message: str) -> None:
        for record in records:
            self.add(record, {"success": False, "errors": [{"statusCode": "BATCH_FAILED", "message": message}]})


class SalesforceREST:
    def __init__(self, instance_url: str, headers: Mapping[str, str], client: httpx.Client | None = None):
        self.instance_url = instance_url.rstrip("/")
        self.base = f"{self.instance_url}/services/data/v{API_VERSION}"
        self.headers = dict(headers)
        self.http = client or httpx.Client(timeout=120.0)

    @classmethod
    def for_alias(cls, alias: str = DEFAULT_ALIAS) -> SalesforceREST:
        return cls(*rest_session(alias))

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self.http.request(method, url, headers=self.headers, **kwargs)
        if response.status_code == 401:
            raise NotAuthorized("Salesforce rejected the access token (HTTP 401).\n" + LOGIN_HINT.format(alias=DEFAULT_ALIAS))
        if response.status_code >= 400:
            raise LoadError(f"{method} {url} -> HTTP {response.status_code}: {response.text[:800]}")
        return response.json() if response.content else None

    def query(self, soql: str) -> dict[str, Any]:
        data = self.request("GET", f"{self.base}/query", params={"q": soql})
        records = list(data.get("records", []))
        total = data.get("totalSize", len(records))
        while not data.get("done", True) and data.get("nextRecordsUrl"):
            data = self.request("GET", f"{self.instance_url}{data['nextRecordsUrl']}")
            records.extend(data.get("records", []))
        return {"totalSize": total, "records": records}

    def count(self, sobject: str, where: str | None = None) -> int:
        return int(self.query(f"SELECT COUNT() FROM {sobject}" + (f" WHERE {where}" if where else ""))["totalSize"])

    def id_map(self, sobject: str) -> dict[str, str]:
        rows = self.query(f"SELECT Id, {EXTERNAL_ID} FROM {sobject} WHERE {EXTERNAL_ID} != null")["records"]
        return {r[EXTERNAL_ID]: r["Id"] for r in rows}

    def _collection(self, result: StepResult, method: str, url: str, records: list[dict[str, Any]], created: bool) -> None:
        for batch in batches(records):
            try:
                outcomes = self.request(method, url, json={"allOrNone": False, "records": batch})
            except NotAuthorized:
                raise
            except (LoadError, httpx.HTTPError) as exc:
                result.fail_all(batch, str(exc)[:400])
                continue
            for record, outcome in zip(batch, outcomes or []):
                if created and outcome.get("success"):
                    outcome = {**outcome, "created": True}
                result.add(record, outcome)

    def upsert(self, step: Step) -> StepResult:
        result = StepResult(step.label)
        url = f"{self.base}/composite/sobjects/{step.sobject}/{EXTERNAL_ID}"
        self._collection(result, "PATCH", url, step.records, created=False)
        return result

    def insert_or_update(self, step: Step) -> StepResult:
        """Insert records whose Lore_Id__c is new and update the rest by Id. Used for Task: an Activity external
        id cannot be Unique and upserting on it needs the View All Data permission."""
        result = StepResult(step.label)
        existing = self.id_map(step.sobject)
        inserts = [r for r in step.records if r[EXTERNAL_ID] not in existing]
        updates = [{**r, "id": existing[r[EXTERNAL_ID]]} for r in step.records if r[EXTERNAL_ID] in existing]
        url = f"{self.base}/composite/sobjects"
        self._collection(result, "POST", url, inserts, created=True)
        self._collection(result, "PATCH", url, updates, created=False)
        return result


# --------------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------------


def _print_result(res: StepResult) -> None:
    print(f"  {res.label:<22} attempted={res.attempted:<4} ok={res.succeeded:<4} created={res.created:<4} failed={res.failed}")
    for text, example in res.errors.items():
        print(f"      ! {text}  (e.g. {example})")


def _preview(record: Mapping[str, Any], desc_chars: int = 300) -> str:
    shown = dict(record)
    desc = shown.get("Description")
    if isinstance(desc, str) and len(desc) > desc_chars:
        shown["Description"] = desc[:desc_chars] + f" ... (+{len(desc) - desc_chars} chars)"
    return json.dumps(shown, ensure_ascii=False, indent=2)


def dry_run(data_dir: str | Path | None = None, out: Callable[[str], None] = print) -> list[Step]:
    plan = build_plan(load_tables(data_dir))
    out(f"Dry run (no network). Source: {Path(data_dir or config.DATA_DIR)}")
    for step in plan:
        n = len(list(batches(step.records)))
        out(f"  {step.label:<22} {len(step.records):>4} records  {n} batch(es)  {step.sobject} {step.mode}")
    seen: set[str] = set()
    for step in plan:
        if step.records and step.label not in seen:
            seen.add(step.label)
            out(f"\n--- sample {step.label} ---\n{_preview(step.records[0])}")
    out("\nNote: the live Task load resolves What/Who to Salesforce Ids (WhatId/WhoId) by querying Lore_Id__c first.")
    return plan


def cmd_deploy(alias: str) -> int:
    sf = ensure_sf()
    print(f"Deploying {SF_PROJECT_DIR / 'force-app'} to {alias} ...", flush=True)
    # sf requires the working directory to be inside the SFDX project (sfdx-project.json lives in salesforce/).
    deploy = subprocess.run(
        [sf, "project", "deploy", "start", "--source-dir", "force-app", "--target-org", alias, "--wait", "10"],
        cwd=SF_PROJECT_DIR,
        check=False,
    )
    if deploy.returncode != 0:
        print("Deploy failed. If the org is not authorized:\n" + LOGIN_HINT.format(alias=alias), file=sys.stderr)
        return 1
    assign = subprocess.run(
        [sf, "org", "assign", "permset", "--name", PERMISSION_SET, "--target-org", alias, "--json"],
        capture_output=True, text=True, cwd=SF_PROJECT_DIR, check=False,
    )
    output = (assign.stdout + assign.stderr).lower()
    if assign.returncode != 0 and "duplicate" not in output and "already assigned" not in output:
        print(f"Permission set assignment failed:\n{assign.stdout}{assign.stderr}", file=sys.stderr)
        return 1
    print(f"Permission set {PERMISSION_SET} assigned (or already assigned).")
    return 0


def cmd_load(alias: str, data_dir: str | Path | None = None) -> int:
    tables = load_tables(data_dir)
    auth = org_auth(alias)
    rest = SalesforceREST(auth.instance_url, _headers(auth.access_token))
    print(f"Loading into {auth.instance_url} as {auth.username or alias} (REST v{API_VERSION})")
    failed = 0
    for step in build_plan(tables):
        if step.mode == "insert":
            id_maps = {name: rest.id_map(name) for name in ("Account", "Contact", "Opportunity")}
            step = Step(step.label, step.sobject, [task_record(r, id_maps) for r in task_rows(tables)], step.mode)
            res = rest.insert_or_update(step)
        else:
            res = rest.upsert(step)
        _print_result(res)
        failed += res.failed
    print("Done." if not failed else f"Done with {failed} failed record(s).")
    return 0 if not failed else 1


def cmd_status(alias: str) -> int:
    auth = org_auth(alias)
    rest = SalesforceREST(auth.instance_url, _headers(auth.access_token))
    print(f"Org {auth.instance_url} ({auth.username or alias})")
    print(f"  {'Object':<12} {'Lore':>6} {'Total':>7}")
    for sobject in LOAD_ORDER:
        try:
            lore: Any = rest.count(sobject, f"{EXTERNAL_ID} != null")
        except LoadError as exc:
            lore = "no field" if "INVALID_FIELD" in str(exc) else "error"
        try:
            total: Any = rest.count(sobject)
        except LoadError:
            total = "error"
        print(f"  {sobject:<12} {lore!s:>6} {total!s:>7}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lore.sf_load", description="Load the Lore synthetic CRM export into Salesforce.")
    parser.add_argument("command", choices=("deploy", "load", "status", "all", "dry-run"))
    parser.add_argument("--alias", default=DEFAULT_ALIAS, help="sf CLI org alias (default: lore)")
    parser.add_argument("--data-dir", default=None, help=f"CSV directory (default: {config.DATA_DIR})")
    args = parser.parse_args(argv)
    try:
        if args.command == "dry-run":
            dry_run(args.data_dir)
            return 0
        if args.command == "deploy":
            return cmd_deploy(args.alias)
        if args.command == "load":
            return cmd_load(args.alias, args.data_dir)
        if args.command == "status":
            return cmd_status(args.alias)
        org_auth(args.alias)  # fail fast with the login hint before deploying
        code = cmd_deploy(args.alias) or cmd_load(args.alias, args.data_dir)
        cmd_status(args.alias)
        return code
    except NotAuthorized as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LoadError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
