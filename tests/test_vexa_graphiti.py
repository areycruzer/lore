import json
from pathlib import Path

import httpx
import pytest

from lore.connectors import vexa
from lore.export import graphiti_export
from lore.kb.store import KnowledgeBase

SAMPLE = Path(__file__).resolve().parent.parent / "seed" / "vexa_sample_transcript.json"


@pytest.fixture
def kb(tmp_path):
    kb = KnowledgeBase(root=tmp_path, git=False)
    kb.init()
    kb.add_account({"account_id": "ACC-0001", "name": "Demo Asterforge Manufacturing"})
    kb.add_contact("ACC-0001", {"contact_id": "CON-0001", "name": "Demo Avery Tan 001", "role": "IT Director"})
    return kb


def test_transcript_to_text_formats_sample():
    text = vexa.transcript_to_text(json.loads(SAMPLE.read_text()))
    lines = text.splitlines()
    assert lines[0].startswith("Meeting: Asterforge x Singtel")
    assert lines[1] == "Platform: teams (3817264905123)"
    assert lines[2].startswith("Date: 2026-11-17 10:00")
    assert lines[3] == "Participants: Demo Team Member 02, Demo Avery Tan 001, Demo Sam Lee 001"
    seg_lines = [ln for ln in lines if ln.startswith("[")]
    assert len(seg_lines) == 12
    assert seg_lines[0].startswith("[00:00:03] Demo Team Member 02: Thanks both")
    assert seg_lines[-1].startswith("[00:01:36] Demo Team Member 02:")
    assert any("Q1 2027" in ln and "Demo Avery Tan 001" in ln for ln in seg_lines)


def test_ingest_file_writes_raw(kb):
    rel = vexa.ingest_file("ACC-0001", SAMPLE, kb=kb)
    assert rel.startswith("raw/2026/W47/RAW-")
    meta, lines = kb.read_raw(rel)
    assert meta["type"] == "meeting_transcript"
    assert meta["source_rank"] == 1
    assert meta["occurred_at"] == "2026-11-17"
    assert meta["origin"] == "vexa:teams/3817264905123"
    assert meta["author"] == "vexa-connector"
    assert any("24x7 security monitoring" in ln for ln in lines)


def test_client_urls_and_headers():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith("/transcripts/"):
            return httpx.Response(200, json=json.loads(SAMPLE.read_text()))
        return httpx.Response(200, json={"ok": True})

    c = vexa.VexaClient(base_url="http://vexa.test:18056/", api_key="k-123", transport=httpx.MockTransport(handler))
    c.request_bot("teams", "3817264905123", passcode="abc")
    c.list_meetings()
    tr = c.get_transcript("teams", "3817264905123")
    c.stop_bot("teams", "3817264905123")

    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/bots"), ("GET", "/meetings"),
        ("GET", "/transcripts/teams/3817264905123"), ("DELETE", "/bots/teams/3817264905123"),
    ]
    assert all(r.headers["X-API-Key"] == "k-123" for r in seen)
    assert seen[0].url.host == "vexa.test" and seen[0].url.port == 18056
    assert json.loads(seen[0].content) == {"platform": "teams", "native_meeting_id": "3817264905123",
                                           "bot_name": "Lore Notetaker", "passcode": "abc"}
    assert len(tr["segments"]) == 12


def test_client_rejects_bad_platform_and_missing_key():
    c = vexa.VexaClient(base_url="http://x", api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(vexa.VexaError):
        c.get_transcript("skype", "1")
    c2 = vexa.VexaClient(base_url="http://x", api_key="", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(vexa.VexaError, match="VEXA_API_KEY"):
        c2.list_meetings()


def _two_events(kb):
    raw = kb.add_raw("meeting_transcript", "ACC-0001", "2026-11-17", "line one\nBudget is 50k\nBudget is 80k\n",
                     origin="test", title="t")
    first = kb.add_event({"account": "ACC-0001", "kind": "budget", "topic": "budget", "value": "50k",
                          "summary": "Budget 50k", "source": f"{raw}#L2", "epistemic_status": "stated",
                          "said_by": "CON-0001"})
    first_id = Path(first).stem
    kb.add_event({"account": "ACC-0001", "kind": "budget", "topic": "budget", "value": "80k",
                  "summary": "Budget 80k", "source": f"{raw}#L3", "epistemic_status": "hedged",
                  "supersedes": [first_id]})
    return first_id


def test_build_episodes_marks_superseded(kb):
    first_id = _two_events(kb)
    eps = graphiti_export.build_episodes("ACC-0001", kb=kb)
    assert len(eps) == 2
    bodies = {e["name"]: json.loads(e["episode_body"]) for e in eps}
    assert bodies[first_id]["superseded"] is True
    other = next(b for n, b in bodies.items() if n != first_id)
    assert other["superseded"] is False and other["supersedes"] == [first_id]
    assert bodies[first_id]["said_by"] == {"contact_id": "CON-0001", "name": "Demo Avery Tan 001", "role": "IT Director"}
    assert bodies[first_id]["account_name"] == "Demo Asterforge Manufacturing"
    ep = eps[0]
    assert ep["group_id"] == "ACC-0001"
    assert ep["reference_time"] == "2026-11-17T00:00:00+00:00"
    assert ep["source_description"] == "Lore meeting_transcript (rank 1)"
    assert ep["source"] == "json"


def test_export_jsonl(kb, tmp_path):
    _two_events(kb)
    out = tmp_path / "out" / "eps.jsonl"
    n = graphiti_export.export_jsonl(out, "ACC-0001", kb=kb)
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert n == len(rows) == 2
    assert all({"name", "episode_body", "source_description", "reference_time", "group_id"} <= r.keys() for r in rows)
    assert all(isinstance(json.loads(r["episode_body"]), dict) for r in rows)
