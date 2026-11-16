"""Governed CRM writes from ChatGPT (or any MCP client): scope check, field allowlist, validation, audit, wiki mirror.

Every successful update becomes a new crm_record source plus a superseding wiki event, so Lore's history shows who
changed what, when, and why, even though Salesforce only keeps the latest value.
"""
from __future__ import annotations

import datetime as dt
import json
import time

from lore import config
from lore.connectors.crm import get_crm, load_table
from lore.governance import Principal, denied
from lore.kb.store import KBError, KnowledgeBase

ALLOWED: dict[str, set[str]] = {
    "Opportunity": {"StageName", "Amount", "CloseDate", "NextStep", "Probability"},
    "Account": {"Lore_Relationship_Health__c", "Lore_Budget_SGD__c", "Lore_Renewal_Date__c"},
    "Contact": {"Title", "Department", "Lore_Influence__c", "Lore_Champion__c"},
}
STAGES = ["Prospecting", "Qualification", "Needs Analysis", "Value Proposition", "Id. Decision Makers", "Perception Analysis",
          "Proposal/Price Quote", "Negotiation/Review", "Closed Won", "Closed Lost"]
CSV_KEYS = {"Opportunity": {"StageName": "stage", "Amount": "amount_sgd", "CloseDate": "expected_close_date", "NextStep": "next_step",
                            "Probability": "probability"},
            "Account": {"Lore_Relationship_Health__c": "relationship_health", "Lore_Budget_SGD__c": "budget_estimate_sgd",
                        "Lore_Renewal_Date__c": "contract_renewal_date"},
            "Contact": {"Title": "job_title", "Department": "department", "Lore_Influence__c": "influence_level",
                        "Lore_Champion__c": "is_champion"}}
WRITER_ROLES = {"account_executive", "sales_manager"}
TABLE = {"Opportunity": ("opportunities", "opportunity_id"), "Contact": ("contacts", "contact_id"), "Account": ("accounts", "account_id")}


def _audit(entry: dict) -> None:
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    with (config.VAR_DIR / "crm_writes.jsonl").open("a") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")


def _validate(obj: str, fields: dict) -> dict:
    bad = set(fields) - ALLOWED.get(obj, set())
    if obj not in ALLOWED:
        raise KBError(f"Lore does not allow updates to {obj}. Allowed objects: {sorted(ALLOWED)}")
    if bad:
        raise KBError(f"fields {sorted(bad)} are not on the allowlist for {obj}; allowed: {sorted(ALLOWED[obj])}")
    clean = dict(fields)
    if "StageName" in clean and clean["StageName"] not in STAGES:
        raise KBError(f"StageName must be one of {STAGES}")
    for k in ("Amount", "Probability", "Lore_Budget_SGD__c"):
        if k in clean:
            try:
                clean[k] = float(clean[k])
            except (TypeError, ValueError) as exc:
                raise KBError(f"{k} must be a number") from exc
    if "Probability" in clean and not 0 <= clean["Probability"] <= 100:
        raise KBError("Probability is a percentage 0-100")
    for k in ("CloseDate", "Lore_Renewal_Date__c"):
        if k in clean:
            try:
                dt.date.fromisoformat(str(clean[k]))
            except ValueError as exc:
                raise KBError(f"{k} must be YYYY-MM-DD") from exc
    if "Lore_Champion__c" in clean:
        clean["Lore_Champion__c"] = str(clean["Lore_Champion__c"]).lower() in ("true", "1", "yes")
    return clean


def _account_of(obj: str, record_id: str) -> str | None:
    if obj == "Account":
        return record_id
    table, key = TABLE[obj]
    row = next((r for r in load_table(table) if r[key] == record_id), None)
    if row:
        return row["account_id"]
    crm = get_crm()
    if crm.backend == "salesforce":
        rows = crm.query(obj, ["Account.Lore_Id__c"], f"Lore_Id__c = '{record_id}'", limit=1)
        return rows[0].get("Account.Lore_Id__c") if rows else None
    return None


def _old_values(obj: str, record_id: str, fields: list[str]) -> dict:
    crm = get_crm()
    if crm.backend == "salesforce":
        rows = crm.query(obj, ["Id", *fields], f"Lore_Id__c = '{record_id}'", limit=1)
        if not rows:
            raise KBError(f"{obj} {record_id} not found in Salesforce")
        return rows[0]
    table, key = TABLE[obj]
    row = next((r for r in load_table(table) if r[key] == record_id), None)
    if not row:
        raise KBError(f"{obj} {record_id} not found")
    return {f: row.get(CSV_KEYS[obj][f]) for f in fields}


def update_record(who: Principal, obj: str, record_id: str, fields: dict, reason: str) -> dict:
    if who.role not in WRITER_ROLES:
        raise KBError(f"{who.name} ({who.role}) cannot update CRM records; only account executives and sales managers can.")
    if not reason or len(reason.strip()) < 5:
        raise KBError("reason required: one sentence on why the record changes (it goes into the audit trail)")
    clean = _validate(obj, fields)
    account = _account_of(obj, record_id)
    if not account:
        raise KBError(f"cannot find the account for {obj} {record_id}")
    if not who.can_see(account):
        raise KBError(denied(account, who))
    old = _old_values(obj, record_id, list(clean))
    crm = get_crm()
    if crm.backend == "salesforce":
        res = crm.dml("upsert", obj, [{"Lore_Id__c": record_id, **clean}], "Lore_Id__c")
        if res["failed"]:
            _audit({"user": who.user_id, "object": obj, "record": record_id, "fields": clean, "ok": False, "detail": str(res["detail"])[:500]})
            raise KBError(f"Salesforce rejected the update: {res['detail']}")
        where = "Salesforce"
    else:
        where = "offline CRM (LORE_CRM=csv): recorded in Lore only"
    kb = KnowledgeBase()
    lines = [f"CRM update by {who.name} ({who.user_id}) via Lore", f"Record: {obj} {record_id} (account {account})"]
    lines += [f"{f}: {old.get(f)} -> {v}" for f, v in clean.items()]
    lines.append(f"Reason: {reason.strip()}")
    raw = kb.add_raw("crm_record", account, config.TODAY, "\n".join(lines), f"salesforce:{obj}/{record_id}", actor=who.user_id,
                     title=f"CRM update {obj} {record_id}")
    events = []
    for i, (f, v) in enumerate(clean.items(), start=3):
        topic = f"opp:{record_id}" if (obj == "Opportunity" and f == "StageName") else f"{obj.lower()}:{record_id}:{f}"
        prior = [e.id for e in kb.current_events(account) if e.get("topic") == topic]
        events.append(kb.add_event({"account": account, "kind": "opportunity" if obj == "Opportunity" else "fact", "topic": topic,
                                    "value": str(v), "summary": f"{obj} {record_id} {f} changed from {old.get(f)} to {v} ({reason.strip()})",
                                    "source": f"{raw}#L{i}", "epistemic_status": "confirmed", "supersedes": prior or None},
                                   actor=who.user_id, rebuild=(i == 2 + len(clean))))
    _audit({"user": who.user_id, "object": obj, "record": record_id, "fields": clean, "old": {k: old.get(k) for k in clean}, "ok": True})
    return {"updated": f"{obj} {record_id}", "account": account, "written_to": where, "old": {k: old.get(k) for k in clean},
            "new": clean, "wiki_source": raw, "wiki_events": events}


def log_activity(who: Principal, account: str, subject: str, notes: str, activity_type: str = "call", contact_id: str | None = None,
                 date: str | None = None) -> dict:
    if who.role not in WRITER_ROLES:
        raise KBError(f"{who.name} ({who.role}) cannot log CRM activities")
    if not who.can_see(account):
        raise KBError(denied(account, who))
    date = date or config.TODAY
    crm = get_crm()
    where = "offline CRM: recorded in Lore only"
    if crm.backend == "salesforce":
        acc = crm.query("Account", ["Id"], f"Lore_Id__c = '{account}'", limit=1)
        if not acc:
            raise KBError(f"{account} not found in Salesforce")
        rec = {"Subject": subject[:255], "Description": notes[:32000], "ActivityDate": date, "Status": "Completed",
               "Priority": "Normal", "WhatId": acc[0]["Id"], "Type": {"call": "Call", "email": "Email", "meeting": "Meeting"}.get(activity_type, "Other")}
        if contact_id:
            c = crm.query("Contact", ["Id"], f"Lore_Id__c = '{contact_id}'", limit=1)
            if c:
                rec["WhoId"] = c[0]["Id"]
        res = crm.dml("insert", "Task", [rec])
        if res["failed"]:
            raise KBError(f"Salesforce rejected the activity: {res['detail']}")
        where = "Salesforce Task"
    kb = KnowledgeBase()
    raw = kb.add_raw("am_note", account, date, f"Subject: {subject}\nLogged by: {who.name} ({who.user_id}) via Lore\n[AM note] {notes}",
                     f"salesforce:Task/lore-{int(time.time())}", actor=who.user_id, title=subject)
    ev = kb.add_event({"account": account, "kind": "meeting_note", "summary": f"{subject} ({activity_type}, logged via Lore)",
                       "source": f"{raw}#L3", "epistemic_status": "stated", "said_by": contact_id}, actor=who.user_id)
    _audit({"user": who.user_id, "object": "Task", "account": account, "subject": subject, "ok": True})
    return {"logged": subject, "account": account, "written_to": where, "wiki_source": raw, "wiki_event": ev}
