"""Agent actions. Internal notifications go straight to the outbox; anything that touches a customer waits for a human.

The outbox stands in for Microsoft Graph (Outlook calendar + Teams). Files land in outputs/lore-actions/.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path

from lore import config
from lore.bridge import open_churn_tickets
from lore.connectors.mock_sources import owners
from lore.kb.store import KBError, KnowledgeBase, now_iso

PENDING = config.VAR_DIR / "actions"


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def notify_am_teams(account: str, text: str, to_owner: str | None = None, requested_by: str = "system", source: str | None = None) -> dict:
    kb = KnowledgeBase()
    meta = kb.account_meta(account)
    to = to_owner or meta.get("owner")
    o = owners().get(to, {})
    msg = {"id": f"TEAMS-{secrets.token_hex(4)}", "channel": "Teams chat (mock Microsoft Graph)", "to": {"owner_id": to, "name": o.get("full_name"),
           "email": o.get("email")}, "account": account, "account_name": meta["name"], "text": text, "source": source,
           "sent_by": f"Lore on behalf of {requested_by}", "sent_at": now_iso()}
    path = config.OUTBOX_DIR / "teams" / f"{msg['sent_at'][:10]}-{msg['id']}.json"
    _save(path, msg)
    return {**msg, "outbox_file": str(path)}


def propose_meeting(account: str, contact_ids: list[str], title: str, start: str, duration_minutes: int = 30, agenda: str = "",
                    requested_by: str = "system") -> dict:
    kb = KnowledgeBase()
    meta = kb.account_meta(account)
    contacts = {c["contact_id"]: c for c in kb.contacts(account)}
    warnings, blocked = [], []
    attendees = []
    for cid in contact_ids:
        c = contacts.get(cid)
        if not c:
            raise KBError(f"unknown contact {cid} for {account}")
        if c.get("do_not_contact") or c.get("consent") == "revoked":
            blocked.append(f"{c['name']} is marked do-not-contact; removed from the invite")
            continue
        if c.get("consent") != "granted":
            warnings.append(f"{c['name']} consent is {c.get('consent')}; confirm before sending")
        attendees.append({"contact_id": cid, "name": c["name"], "role": c.get("role")})
    if not attendees:
        raise KBError("no contact can be invited: " + "; ".join(blocked))
    churn = open_churn_tickets(account)
    if churn:
        warnings.append(f"Open churn risk ({', '.join(t['ticket_id'] for t in churn)}): keep this meeting about service recovery, no upsell pitch")
    try:
        start_dt = dt.datetime.fromisoformat(start)
    except ValueError as exc:
        raise KBError("start must be ISO datetime, e.g. 2026-11-18T10:00") from exc
    o = owners().get(meta.get("owner"), {})
    action = {"id": f"ACT-{secrets.token_hex(4)}", "type": "schedule_meeting", "status": "pending_confirmation", "account": account,
              "account_name": meta["name"], "title": title, "start": start_dt.isoformat(), "duration_minutes": duration_minutes,
              "agenda": agenda, "organiser": {"owner_id": meta.get("owner"), "name": o.get("full_name"), "email": o.get("email")},
              "attendees": attendees, "warnings": warnings, "blocked": blocked, "requested_by": requested_by, "created_at": now_iso()}
    _save(PENDING / f"{action['id']}.json", action)
    return action


def list_pending() -> list[dict]:
    if not PENDING.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(PENDING.glob("*.json")) if json.loads(p.read_text())["status"] == "pending_confirmation"]


def _ics(a: dict) -> str:
    start = dt.datetime.fromisoformat(a["start"])
    end = start + dt.timedelta(minutes=a["duration_minutes"])
    fmt = "%Y%m%dT%H%M%S"
    att = "\n".join(f"ATTENDEE;CN={x['name']}:mailto:{x['contact_id'].lower()}@customer.example" for x in a["attendees"])
    return (f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Lore//Mock Graph//EN\nBEGIN:VEVENT\nUID:{a['id']}@lore\nDTSTAMP:{dt.datetime.now(dt.UTC).strftime(fmt)}Z\n"
            f"DTSTART;TZID=Asia/Singapore:{start.strftime(fmt)}\nDTEND;TZID=Asia/Singapore:{end.strftime(fmt)}\nSUMMARY:{a['title']}\n"
            f"DESCRIPTION:{a['agenda'].replace(chr(10), ' ')}\nORGANIZER;CN={a['organiser']['name']}:mailto:{a['organiser']['email']}\n{att}\n"
            "LOCATION:Microsoft Teams meeting\nEND:VEVENT\nEND:VCALENDAR\n")


def decide(action_id: str, approve: bool, decided_by: str) -> dict:
    path = PENDING / f"{action_id}.json"
    if not path.exists():
        raise KBError(f"no pending action {action_id}")
    a = json.loads(path.read_text())
    if a["status"] != "pending_confirmation":
        raise KBError(f"{action_id} already {a['status']}")
    a["decided_by"], a["decided_at"] = decided_by, now_iso()
    if not approve:
        a["status"] = "rejected"
        _save(path, a)
        return a
    a["status"] = "sent"
    out = config.OUTBOX_DIR / "meetings"
    ics = out / f"{a['id']}.ics"
    out.mkdir(parents=True, exist_ok=True)
    ics.write_text(_ics(a))
    _save(out / f"{a['id']}.json", a)
    a["outbox_files"] = [str(ics), str(out / f"{a['id']}.json")]
    _save(path, a)
    # Close the loop: the scheduled meeting becomes a calendar source and a commitment in the wiki.
    kb = KnowledgeBase()
    lines = [f"Calendar invite: {a['title']}", f"When: {a['start']} ({a['duration_minutes']} min, Teams)",
             f"Attendees: {', '.join(x['name'] for x in a['attendees'])}; organiser {a['organiser']['name']}", f"Agenda: {a['agenda']}"]
    raw = kb.add_raw("calendar", a["account"], a["start"][:10], "\n".join(lines), f"outlook-calendar:{a['id']}", actor=decided_by, title=a["title"])
    a["kb_event"] = kb.add_event({"account": a["account"], "kind": "commitment", "summary": f"Meeting booked: {a['title']} on {a['start'][:16]}",
                                  "source": f"{raw}#L1-L2", "epistemic_status": "confirmed"}, actor=decided_by)
    _save(path, a)
    return a
