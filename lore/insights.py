"""Deterministic sales insights over the wiki: who to call, whitespace, account map, ops bridge, briefs.

The agent narrates these; they also back the fast public MCP tools so a demo never waits on a model.
"""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from lore import config
from lore.bridge import open_churn_tickets, owned_products
from lore.connectors.crm import load_table
from lore.connectors.mock_sources import products
from lore.governance import Principal
from lore.kb.store import KnowledgeBase

SIGNAL_WEIGHT = {"expansion": 3, "new_site": 3, "tender": 3, "inbound": 3, "it_hiring": 2, "funding_growth": 2,
                 "leadership_change": 1}
MAPS_DIR = config.OUTBOX_DIR.parent / "lore-maps"


def _age_days(d: str) -> int:
    return (dt.date.fromisoformat(config.TODAY) - dt.date.fromisoformat(str(d)[:10])).days


def _cite(e) -> str:
    return e.get("source")


def best_contact(kb: KnowledgeBase, account: str) -> dict | None:
    cs = [c for c in kb.contacts(account) if not c.get("do_not_contact") and c.get("consent") == "granted"]
    order = {"economic_buyer": 0, "technical_decision_maker": 1, "influencer": 2, "end_user": 3}
    cs.sort(key=lambda c: (not c.get("is_champion"), order.get(c.get("influence"), 9)))
    return cs[0] if cs else None


def who_to_call(kb: KnowledgeBase, who: Principal, limit: int = 8) -> list[dict]:
    out = []
    for a in kb.list_accounts():
        acc = a["account_id"]
        if not who.can_see(acc) or a.get("status") == "disqualified":
            continue
        score, reasons = 0.0, []
        for e in kb.current_events(acc):
            age = _age_days(e.get("occurred_at"))
            if age > 120 or age < -60:
                continue
            decay = 1.0 if age <= 30 else 0.5
            kind = e.get("kind")
            if kind == "ops_request" and (e.get("extra") or {}).get("bridge"):
                score += 4 * decay
                reasons.append((4 * decay, f"Ops bridge: {e.get('summary')}", _cite(e)))
            elif kind == "signal":
                w = SIGNAL_WEIGHT.get((e.get("extra") or {}).get("signal_type", "expansion"), 2) * decay
                score += w
                reasons.append((w, e.get("summary"), _cite(e)))
            elif kind in ("budget", "timeline", "objection") and age <= 30:
                score += 1.5
                reasons.append((1.5, e.get("summary"), _cite(e)))
        renewal = a.get("renewal")
        if renewal and 0 <= -_age_days(renewal) <= 90:
            score += 2
            reasons.append((2, f"Renewal on {renewal}", f"wiki/accounts/{acc}/index.md"))
        churn = open_churn_tickets(acc)
        if churn:
            score += 3
            reasons.append((3, f"Open churn risk ({', '.join(t['ticket_id'] for t in churn)}): recover service before any pitch", None))
        if score <= 0:
            continue
        reasons.sort(key=lambda r: -r[0])
        c = best_contact(kb, acc)
        out.append({
            "account": acc, "name": a["name"], "score": round(score, 1),
            "call_type": "service recovery first" if churn else "growth",
            "call": f"{c['name']} ({c['role']})" if c else "no contact with granted consent; check consent first",
            "why": [{"reason": r[1], "source": r[2]} for r in reasons[:4]],
        })
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def whitespace(kb: KnowledgeBase, account: str) -> dict:
    meta = kb.account_meta(account)
    owned = set(meta.get("owned_products") or owned_products(account))
    acc_row = next((r for r in load_table("accounts") if r["account_id"] == account), {})
    pains = set((acc_row.get("pain_points") or "").split("|")) - {""}
    evidence: dict[str, list[dict]] = {}
    for e in kb.current_events(account):
        pid = e.get("product")
        if pid and pid not in owned and e.get("kind") in ("signal", "ops_request"):
            evidence.setdefault(pid, []).append({"summary": e.get("summary"), "source": e.get("source"), "gate": e.get("gate")})
        also = (e.get("extra") or {}).get("also")
        if also and also not in owned:
            evidence.setdefault(also, []).append({"summary": f"Related to: {e.get('summary')}", "source": e.get("source"), "gate": e.get("gate")})
    rows = []
    for pid, p in products().items():
        if pid in owned:
            continue
        fit = pains & set(p["pain_tags"].split("|"))
        ev = evidence.get(pid, [])
        if not fit and not ev:
            continue
        rows.append({"product_id": pid, "product": p["product_name"], "fits_pains": sorted(fit), "evidence": ev[:3],
                     "strength": len(ev) * 2 + len(fit)})
    rows.sort(key=lambda r: -r["strength"])
    churn = open_churn_tickets(account)
    return {"account": account, "name": meta["name"], "owns": sorted(owned), "whitespace": rows,
            "gate": "service_recovery_first" if churn else "permitted_demo",
            "gate_reason": (f"Open churn risk on {', '.join(t['ticket_id'] for t in churn)}: fix service before pitching." if churn else "")}


def ops_bridge(kb: KnowledgeBase, who: Principal, account: str | None = None) -> list[dict]:
    out = []
    for a in kb.list_accounts():
        acc = a["account_id"]
        if (account and acc != account) or not who.can_see(acc):
            continue
        for e in kb.current_events(acc):
            if e.get("kind") == "ops_request" and (e.get("extra") or {}).get("bridge"):
                p = products().get(e.get("product") or "", {})
                out.append({"account": acc, "name": a["name"], "owner": a.get("owner"), "date": e.get("occurred_at"),
                            "for_the_am": e.get("summary"), "product": p.get("product_name"), "product_id": e.get("product"), "ticket": (e.get("extra") or {}).get("ticket"),
                            "gate": e.get("gate"), "source": e.get("source"), "event": e.id})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


ROLE_LABEL = {"economic_buyer": "Economic Buyer", "technical_decision_maker": "Technical Decision Maker", "influencer": "Influencer",
              "end_user": "End User"}


def _strength(c: dict) -> str:
    if c.get("do_not_contact") or c.get("consent") == "revoked":
        return "none"
    if c.get("is_champion"):
        return "strong"
    last = c.get("last_contacted")
    if c.get("consent") == "granted" and last and _age_days(last) <= 90:
        return "strong"
    return "developing"


def account_map(kb: KnowledgeBase, account: str, write_html: bool = True) -> dict:
    meta = kb.account_meta(account)
    contacts = kb.contacts(account)
    by_id = {c["contact_id"]: c for c in contacts}
    concerns = {e.get("said_by"): e for e in kb.current_events(account) if e.get("kind") == "objection" and e.get("said_by")}
    nodes, edges = [], []
    for c in contacts:
        labels = [ROLE_LABEL.get(c.get("influence"), c.get("influence") or "Contact")]
        if c.get("is_champion"):
            labels.insert(0, "Champion")
        if c["contact_id"] in concerns:
            labels.append("Has concerns")
        nodes.append({"id": c["contact_id"], "name": c["name"], "title": c.get("role"), "labels": labels, "strength": _strength(c),
                      "consent": c.get("consent"), "do_not_contact": bool(c.get("do_not_contact"))})
        if c.get("reports_to") in by_id:
            edges.append({"from": c["contact_id"], "to": c["reports_to"], "type": "reports_to"})
    buyer = next((c for c in contacts if c.get("influence") == "economic_buyer"), None)
    for c in contacts:
        if buyer and c.get("is_champion") and c["contact_id"] != buyer["contact_id"]:
            edges.append({"from": c["contact_id"], "to": buyer["contact_id"], "type": "influence", "label": "advocates"})
        if buyer and c["contact_id"] in concerns and c["contact_id"] != buyer["contact_id"]:
            edges.append({"from": c["contact_id"], "to": buyer["contact_id"], "type": "concern",
                          "label": concerns[c["contact_id"]].get("summary")[:60], "source": concerns[c["contact_id"]].get("source")})
    color = {"strong": "#22a559", "developing": "#e0b000", "none": "#d6453d"}
    m = ["flowchart BT"]
    for n in nodes:
        m.append(f'  {n["id"].replace("-", "")}["{n["name"]}<br/>{n["title"]}<br/><i>{", ".join(n["labels"])}</i>"]')
    for e in edges:
        a, b = e["from"].replace("-", ""), e["to"].replace("-", "")
        m.append(f"  {a} --> {b}" if e["type"] == "reports_to" else f'  {a} -.->|{e.get("label", "")}| {b}')
    for n in nodes:
        m.append(f'  style {n["id"].replace("-", "")} stroke:{color[n["strength"]]},stroke-width:3px')
    result = {"account": account, "name": meta["name"], "nodes": nodes, "edges": edges, "mermaid": "\n".join(m),
              "legend": "green = strong relationship, yellow = developing, red = none / do not contact; solid = reporting line, dotted = influence"}
    if write_html:
        result["html_path"] = str(_write_map_html(meta["name"], account, nodes, edges, color))
    return result


def _write_map_html(name: str, account: str, nodes: list[dict], edges: list[dict], color: dict) -> Path:
    by_id = {n["id"]: n for n in nodes}
    parent = {e["from"]: e["to"] for e in edges if e["type"] == "reports_to"}

    def depth(i: str, seen=()) -> int:
        return 0 if i not in parent or i in seen else 1 + depth(parent[i], seen + (i,))

    levels: dict[int, list[dict]] = {}
    for n in nodes:
        levels.setdefault(depth(n["id"]), []).append(n)
    rows = []
    for lvl in sorted(levels):
        cards = "".join(
            f'<div class="card" style="border-left:6px solid {color[n["strength"]]}"><b>{html.escape(n["name"])}</b>'
            f'<span>{html.escape(n["title"] or "")}</span><em>{html.escape(", ".join(n["labels"]))}</em>'
            + (f'<small>reports to {html.escape(by_id[parent[n["id"]]]["name"])}</small>' if n["id"] in parent and parent[n["id"]] in by_id else "")
            + ("<small class='dnc'>DO NOT CONTACT</small>" if n["do_not_contact"] else "") + "</div>" for n in levels[lvl])
        rows.append(f'<div class="row">{cards}</div>')
    links = "".join(f"<li>{html.escape(by_id[e['from']]['name'])} → {html.escape(by_id[e['to']]['name'])}: {html.escape(e.get('label', ''))}</li>"
                    for e in edges if e["type"] != "reports_to")
    doc = f"""<!doctype html><meta charset="utf-8"><title>Account map: {html.escape(name)}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;background:#f6f7f9;margin:32px;color:#111}}h1{{margin:0 0 4px}}
.row{{display:flex;gap:18px;justify-content:center;margin:22px 0}}.card{{background:#fff;border-radius:10px;padding:12px 16px;min-width:200px;
box-shadow:0 1px 4px #0002;display:flex;flex-direction:column;gap:3px}}.card span{{color:#444}}.card em{{font-size:12px;background:#eef;padding:2px 6px;border-radius:6px;align-self:flex-start}}
small{{color:#777}}.dnc{{color:#d6453d;font-weight:bold}}.legend{{color:#555;font-size:13px}}</style>
<h1>Account map: {html.escape(name)} ({account})</h1><p class="legend">Top row = most senior. Green = strong relationship, yellow = developing, red = none or do not contact.</p>
{''.join(reversed(rows))}<h3>Influence and concerns</h3><ul>{links or '<li>none recorded</li>'}</ul>
<p class="legend">Generated by Lore from the account wiki. Synthetic data.</p>"""
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    path = MAPS_DIR / f"{account}.html"
    path.write_text(doc, encoding="utf-8")
    return path


def brief_pack(kb: KnowledgeBase, account: str) -> dict:
    """Everything a pre-meeting brief needs, with citations. The agent turns this into one page."""
    cur = kb.current_events(account)
    recent = sorted(cur, key=lambda e: str(e.get("occurred_at")), reverse=True)

    def pick(kinds, n=5):
        return [{"date": e.get("occurred_at"), "summary": e.get("summary"), "status": e.get("epistemic_status"),
                 "said_by": e.get("said_by"), "source": e.get("source")} for e in recent if e.get("kind") in kinds][:n]

    return {"account": kb.account_meta(account), "conflicts": kb.conflicts(account), "facts": pick({"budget", "timeline", "fact", "status", "need"}, 8),
            "signals": pick({"signal", "churn_risk"}), "objections": pick({"objection"}), "commitments": pick({"commitment"}),
            "ops_bridge": pick({"ops_request"}), "whitespace": whitespace(kb, account), "next_meeting": next_meeting(account)}


def next_meeting(account: str) -> dict | None:
    rows = [a for a in load_table("activities") if a["account_id"] == account and a["activity_type"] == "meeting" and a["activity_date"] >= config.TODAY]
    rows.sort(key=lambda a: a["activity_date"])
    return {"date": rows[0]["activity_date"], "subject": rows[0]["subject"], "contact": rows[0]["contact_id"]} if rows else None
