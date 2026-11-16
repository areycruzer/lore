"""Core guarantees: additive store, citations, epistemic status, access scope, consent, CRM write governance,
and that routine ops tickets (sales_signal=false) never become sales signals."""
import json

import pytest

from lore import actions, cli, config, crm_writes, insights
from lore.connectors.crm import load_table
from lore.governance import principal
from lore.kb.store import KBError, KnowledgeBase


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KB_DIR", tmp_path / "kb")
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    monkeypatch.setattr(config, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(actions, "PENDING", tmp_path / "var" / "actions")
    kb = KnowledgeBase(root=config.KB_DIR, git=False)
    kb.init()
    kb.add_account({"account_id": "ACC-0001", "name": "Demo Asterforge Manufacturing", "owner": "OWN-002"})
    kb.add_contact("ACC-0001", {"contact_id": "CON-0001", "name": "Demo Avery Tan 001", "role": "IT Director", "consent": "granted"})
    kb.add_contact("ACC-0001", {"contact_id": "CON-0002", "name": "Demo Jordan Tan 001", "role": "CFO", "consent": "revoked",
                                "do_not_contact": True})
    kb.add_contact("ACC-0001", {"contact_id": "CON-0003", "name": "Demo Riley Tan 001", "role": "Ops Manager", "consent": "not_requested"})
    return kb


def _raw(kb, text="Line one\nBudget is S$120k\nGo-live February 2027", type="meeting_transcript", **kw):
    return kb.add_raw(type, "ACC-0001", "2026-11-10", text, "test:meeting", **kw)


# ------------------------------------------------------------------ additive store
def test_raw_files_cannot_be_overwritten(kb):
    _raw(kb, raw_id="RAW-TEST-1")
    with pytest.raises(KBError):
        _raw(kb, raw_id="RAW-TEST-1")


def test_generated_pages_cannot_touch_raw_or_timeline(kb):
    raw = _raw(kb)
    with pytest.raises(KBError):
        kb.write_page(raw, "tampered")
    with pytest.raises(KBError):
        kb.write_page("wiki/accounts/ACC-0001/timeline/2026-W46/x.md", "tampered")


def test_superseding_keeps_history(kb):
    raw = _raw(kb)
    old = kb.add_event({"account": "ACC-0001", "kind": "budget", "topic": "budget", "value": "209000", "summary": "Budget S$209k",
                        "source": f"{raw}#L1", "epistemic_status": "stated"})
    old_id = old.split("/")[-1].removesuffix(".md")
    new = kb.add_event({"account": "ACC-0001", "kind": "budget", "topic": "budget", "value": "120000", "summary": "Budget cut to S$120k",
                        "source": f"{raw}#L2", "epistemic_status": "stated", "supersedes": [old_id]})
    new_id = new.split("/")[-1].removesuffix(".md")
    assert {e.id for e in kb.events("ACC-0001")} >= {old_id, new_id}
    assert old_id not in {e.id for e in kb.current_events("ACC-0001")}
    assert old_id in kb.superseded_ids("ACC-0001")


# ------------------------------------------------------------------ citations and epistemic status
@pytest.mark.parametrize("source, msg", [(None, "source missing"), ("{raw}", "line anchor"), ("{raw}#L99", "out of range"),
                                         ("raw/2026/W46/RAW-nope.md#L1", None)])
def test_events_must_cite_real_lines(kb, source, msg):
    raw = _raw(kb)
    src = source.format(raw=raw) if source else None
    with pytest.raises(KBError, match=msg):
        kb.add_event({"account": "ACC-0001", "kind": "fact", "summary": "x", "source": src, "epistemic_status": "stated"})


@pytest.mark.parametrize("status", [None, "certain"])
def test_epistemic_status_required(kb, status):
    raw = _raw(kb)
    with pytest.raises(KBError, match="epistemic_status"):
        kb.add_event({"account": "ACC-0001", "kind": "fact", "summary": "x", "source": f"{raw}#L2", "epistemic_status": status})


def test_supersedes_must_reference_existing_event(kb):
    raw = _raw(kb)
    with pytest.raises(KBError, match="supersedes unknown"):
        kb.add_event({"account": "ACC-0001", "kind": "fact", "summary": "x", "source": f"{raw}#L2", "epistemic_status": "stated",
                      "supersedes": ["EVT-NOPE"]})


def test_audit_ledger_chain_verifies(kb):
    raw = _raw(kb)
    kb.add_event({"account": "ACC-0001", "kind": "fact", "summary": "x", "source": f"{raw}#L2", "epistemic_status": "stated"})
    ok, n = kb.verify_ledger()
    assert ok and n >= 2


# ------------------------------------------------------------------ access scope
def test_account_manager_sees_only_own_accounts():
    owned = {a["account_id"] for a in load_table("accounts") if a["owner_id"] == "OWN-002"}
    p = principal("OWN-002")
    assert p.accounts == owned and "ACC-0001" in owned
    assert not p.can_see(next(a["account_id"] for a in load_table("accounts") if a["owner_id"] != "OWN-002"))
    assert principal("nobody").accounts == frozenset()
    assert len(principal("job:test").accounts) == len(load_table("accounts"))


# ------------------------------------------------------------------ consent
def test_meeting_proposal_drops_do_not_contact_and_warns(kb):
    a = actions.propose_meeting("ACC-0001", ["CON-0001", "CON-0002", "CON-0003"], "Security review", "2026-11-20T10:00")
    assert [x["contact_id"] for x in a["attendees"]] == ["CON-0001", "CON-0003"]
    assert any("do-not-contact" in b for b in a["blocked"])
    assert any("consent is not_requested" in w for w in a["warnings"])
    assert any("churn" in w.lower() for w in a["warnings"])  # ACC-0001 has an open churn ticket in the CRM export
    assert a["status"] == "pending_confirmation"
    assert not (config.OUTBOX_DIR / "meetings").exists()


def test_meeting_with_only_blocked_contacts_is_refused(kb):
    with pytest.raises(KBError, match="no contact can be invited"):
        actions.propose_meeting("ACC-0001", ["CON-0002"], "x", "2026-11-20T10:00")


def test_confirming_a_meeting_writes_outbox_and_commitment(kb):
    a = actions.propose_meeting("ACC-0001", ["CON-0001"], "Security review", "2026-11-20T10:00")
    done = actions.decide(a["id"], True, "OWN-002")
    assert done["status"] == "sent"
    assert (config.OUTBOX_DIR / "meetings" / f"{a['id']}.ics").exists()
    assert any(e.get("kind") == "commitment" for e in kb.events("ACC-0001"))
    with pytest.raises(KBError, match="already sent"):
        actions.decide(a["id"], True, "OWN-002")


# ------------------------------------------------------------------ CRM write governance
def test_crm_update_rejects_fields_off_the_allowlist(kb):
    with pytest.raises(KBError, match="allowlist"):
        crm_writes.update_record(principal("OWN-002"), "Opportunity", "OPP-0002", {"OwnerId": "005x"}, "reassign owner")


def test_crm_update_rejects_other_owners_records(kb):
    other = next(o for o in load_table("opportunities") if o["account_id"] not in principal("OWN-002").accounts)
    with pytest.raises(KBError, match="Access denied"):
        crm_writes.update_record(principal("OWN-002"), "Opportunity", other["opportunity_id"], {"StageName": "Closed Won"}, "won it")


def test_crm_update_validates_values(kb):
    with pytest.raises(KBError, match="StageName"):
        crm_writes.update_record(principal("OWN-002"), "Opportunity", "OPP-0002", {"StageName": "Basically won"}, "customer agreed")
    with pytest.raises(KBError, match="reason"):
        crm_writes.update_record(principal("OWN-002"), "Opportunity", "OPP-0002", {"Amount": 1}, "")


def test_crm_update_is_mirrored_into_the_wiki_and_audited(kb, monkeypatch):
    monkeypatch.setenv("LORE_CRM", "csv")
    out = crm_writes.update_record(principal("OWN-002"), "Opportunity", "OPP-0002", {"StageName": "Proposal/Price Quote", "Amount": 120000},
                                   "Budget confirmed at S$120k on the 10 Nov call")
    assert out["old"]["StageName"] == "Qualification"
    assert len(out["wiki_events"]) == 2
    evs = [e for e in kb.events("ACC-0001") if e.get("topic") == "opp:OPP-0002"]
    assert evs and evs[0].get("epistemic_status") == "confirmed"
    log = [json.loads(l) for l in (config.VAR_DIR / "crm_writes.jsonl").read_text().splitlines()]
    assert log[-1]["ok"] and log[-1]["record"] == "OPP-0002"


# ------------------------------------------------------------------ ops -> AM bridge
def test_routine_tickets_never_become_sales_signals():
    real = KnowledgeBase()
    if not real.has_account("ACC-0001"):
        pytest.skip("knowledge base not seeded; run `uv run python -m lore.kb.seed --reset`")
    routine = {t["ticket_id"] for t in load_table("support_tickets") if t["sales_signal"] != "true"}
    items = insights.ops_bridge(real, principal("job:test"))
    assert items, "the seeded KB should contain bridge items"
    assert not routine & {i["ticket"] for i in items}
    assert all(i["product"] for i in items)


def test_inbox_ingest_files_raw_without_agent(kb, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.md").write_text("---\naccount: ACC-0001\ntype: am_note\noccurred_at: 2026-11-16\n---\nCFO hinted at a Q1 review.\n")
    (inbox / "bad.md").write_text("no front matter\n")
    out = cli.job_ingest_inbox(kb, use_agent=False, inbox=inbox)
    ok = [r for r in out["processed"] if "stored" in r]
    bad = [r for r in out["processed"] if "error" in r]
    assert len(ok) == 1 and ok[0]["stored"].startswith("raw/")
    assert len(bad) == 1 and (inbox / "bad.md").exists()
    assert (inbox / "processed" / "note.md").exists()
