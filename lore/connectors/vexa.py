"""Vexa meeting-bot connector: send a notetaker into a Teams/Zoom/Meet call and file its transcript in Lore.

Vexa (https://github.com/Vexa-ai/vexa, Apache-2.0) is a self-hosted meeting bot with a REST API.
Endpoints follow the Vexa 0.12 Meetings API (https://docs.vexa.ai/api/meetings, checked 2026-11-16):

    POST   /bots                                       send a bot   {platform, native_meeting_id, bot_name, passcode?, language?}
    GET    /bots/status                                running bots
    DELETE /bots/{platform}/{native_meeting_id}        stop the bot
    GET    /meetings                                   list meetings (slim, paged rows)
    GET    /transcripts/{platform}/{native_meeting_id} meeting record + "segments"

All requests carry `X-API-Key`. The self-hosted gateway listens on port 18056 in Vexa 0.12; older
releases (0.10 and earlier) used 8056, so set VEXA_API_URL if your deployment differs.

The connector only files the transcript as a `meeting_transcript` raw file. Events are extracted later by
Lore's ingest agent, which keeps the rule that every event cites lines of a raw file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from lore import config

DEFAULT_BASE_URL = "http://localhost:18056"
PLATFORMS = ("teams", "zoom", "google_meet", "jitsi")
LOCAL_TZ = ZoneInfo(os.environ.get("LORE_TZ", "Asia/Singapore"))


class VexaError(RuntimeError):
    pass


class VexaClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or os.environ.get("VEXA_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("VEXA_API_KEY", "")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                    headers={"X-API-Key": self.api_key, "Accept": "application/json"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VexaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.api_key:
            raise VexaError("VEXA_API_KEY is not set; create a key in your Vexa deployment and export it")
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise VexaError(f"cannot reach Vexa at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise VexaError(f"Vexa {method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}

    @staticmethod
    def _check_platform(platform: str) -> None:
        if platform not in PLATFORMS:
            raise VexaError(f"platform must be one of {PLATFORMS}")

    def request_bot(self, platform: str, native_meeting_id: str, passcode: str | None = None,
                    bot_name: str = "Lore Notetaker", language: str | None = None,
                    meeting_url: str | None = None) -> dict:
        """POST /bots. For Teams, `native_meeting_id` is the 10-15 digit meeting ID (send its `passcode`) or
        the thread id from a meetup-join link. Zoom and Jitsi need the full `meeting_url` per Vexa docs."""
        self._check_platform(platform)
        body: dict[str, Any] = {"platform": platform, "native_meeting_id": native_meeting_id, "bot_name": bot_name}
        if passcode:
            body["passcode"] = passcode
        if language:
            body["language"] = language
        if meeting_url:
            body["meeting_url"] = meeting_url
        return self._request("POST", "/bots", json=body)

    def bot_status(self) -> dict:
        """GET /bots/status. 0.12 returns `running`/`count`; 0.10 returned `running_bots` (both present in 0.12)."""
        return self._request("GET", "/bots/status")

    def list_meetings(self, limit: int | None = None, offset: int | None = None) -> Any:
        """GET /meetings. Documented as paged slim rows (default page size 50). The envelope key is not
        pinned in the docs, so the raw JSON is returned unchanged."""
        params = {k: v for k, v in {"limit": limit, "offset": offset}.items() if v is not None}
        return self._request("GET", "/meetings", params=params or None)

    def get_transcript(self, platform: str, native_meeting_id: str) -> dict:
        """GET /transcripts/{platform}/{native_meeting_id}: meeting record fields (id, platform, status,
        start_time, end_time, data, ...) plus `segments` [{speaker, text, start, end, absolute_start_time, ...}]."""
        self._check_platform(platform)
        return self._request("GET", f"/transcripts/{platform}/{native_meeting_id}")

    def stop_bot(self, platform: str, native_meeting_id: str) -> dict:
        """DELETE /bots/{platform}/{native_meeting_id}; answers {"status": "stopping", "meeting_id": ...}."""
        self._check_platform(platform)
        return self._request("DELETE", f"/bots/{platform}/{native_meeting_id}")


# ---------------------------------------------------------------------------- formatting

def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.UTC)


def _hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _segments(transcript: dict) -> list[dict]:
    segs = [s for s in transcript.get("segments") or [] if (s.get("text") or "").strip()]
    # Live drafts (completed: false) are replaced by confirmed segments; keep drafts only if nothing is confirmed.
    confirmed = [s for s in segs if s.get("completed", True)]
    segs = confirmed or segs
    return sorted(segs, key=lambda s: float(s.get("start") or 0))


def meeting_start(transcript: dict) -> dt.datetime | None:
    t = _parse_ts(transcript.get("start_time"))
    if t:
        return t
    for s in _segments(transcript):
        t = _parse_ts(s.get("absolute_start_time"))
        if t:
            return t - dt.timedelta(seconds=float(s.get("start") or 0))
    return None


def meeting_date(transcript: dict) -> str:
    start = meeting_start(transcript)
    return start.astimezone(LOCAL_TZ).date().isoformat() if start else config.TODAY


def meeting_title(transcript: dict) -> str:
    data = transcript.get("data") or {}
    return data.get("name") or data.get("title") or transcript.get("title") or \
        f"{transcript.get('platform', 'meeting')} {transcript.get('native_meeting_id', '')}".strip()


def participants(transcript: dict) -> list[str]:
    listed = (transcript.get("data") or {}).get("participants") or []
    names = [p if isinstance(p, str) else p.get("name", "") for p in listed]
    for s in _segments(transcript):
        if s.get("speaker") and s["speaker"] not in names:
            names.append(s["speaker"])
    return [n for n in names if n]


def transcript_to_text(transcript: dict) -> str:
    """Header lines then one `[HH:MM:SS] Speaker: text` line per segment (offset from session start)."""
    start = meeting_start(transcript)
    lines = [
        f"Meeting: {meeting_title(transcript)}",
        f"Platform: {transcript.get('platform', 'unknown')} ({transcript.get('native_meeting_id', '')})",
        f"Date: {start.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M %Z') if start else meeting_date(transcript)}",
        f"Participants: {', '.join(participants(transcript)) or 'unknown'}",
        "",
    ]
    for s in _segments(transcript):
        speaker = (s.get("speaker") or "").strip() or "Unknown speaker"
        text = " ".join(s["text"].split())
        lines.append(f"[{_hms(float(s.get('start') or 0))}] {speaker}: {text}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- ingest

def _store(account: str, transcript: dict, origin: str, kb: Any, actor: str) -> str:
    if kb is None:
        from lore.kb.store import KnowledgeBase
        kb = KnowledgeBase()
    if not _segments(transcript):
        raise VexaError("transcript has no segments yet; wait for the meeting to produce speech and retry")
    return kb.add_raw("meeting_transcript", account, meeting_date(transcript), transcript_to_text(transcript),
                      origin=origin, actor=actor, title=meeting_title(transcript))


def ingest_meeting(account: str, platform: str, native_meeting_id: str, kb: Any = None, actor: str = "vexa-connector",
                   client: VexaClient | None = None) -> str:
    """Fetch a transcript from Vexa and file it as a meeting_transcript raw file. Returns the raw path."""
    owns = client is None
    client = client or VexaClient()
    try:
        transcript = client.get_transcript(platform, native_meeting_id)
    finally:
        if owns:
            client.close()
    transcript.setdefault("platform", platform)
    transcript.setdefault("native_meeting_id", native_meeting_id)
    return _store(account, transcript, f"vexa:{platform}/{native_meeting_id}", kb, actor)


def ingest_file(account: str, path: str | Path, kb: Any = None, actor: str = "vexa-connector") -> str:
    """File a saved Vexa transcript JSON (the GET /transcripts response). Returns the raw path."""
    transcript = json.loads(Path(path).read_text())
    platform = transcript.get("platform", "unknown")
    mid = transcript.get("native_meeting_id", Path(path).stem)
    return _store(account, transcript, f"vexa:{platform}/{mid}", kb, actor)


# ---------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m lore.connectors.vexa", description=__doc__.split("\n")[0])
    p.add_argument("--base-url", help="Vexa gateway URL (env VEXA_API_URL, default %s)" % DEFAULT_BASE_URL)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bot", help="send the Lore notetaker into a meeting (POST /bots)")
    b.add_argument("platform", choices=PLATFORMS)
    b.add_argument("native_meeting_id")
    b.add_argument("--passcode")
    b.add_argument("--meeting-url")
    b.add_argument("--bot-name", default="Lore Notetaker")
    b.add_argument("--language")
    b.add_argument("--stop", action="store_true", help="stop the bot instead (DELETE /bots/...)")

    sub.add_parser("meetings", help="list meetings (GET /meetings)")
    sub.add_parser("status", help="list running bots (GET /bots/status)")

    t = sub.add_parser("transcript", help="print a transcript (GET /transcripts/...)")
    t.add_argument("platform", choices=PLATFORMS)
    t.add_argument("native_meeting_id")
    t.add_argument("--json", action="store_true", help="print raw JSON instead of text")

    i = sub.add_parser("ingest", help="fetch a transcript and file it in the knowledge base")
    i.add_argument("account")
    i.add_argument("platform", choices=PLATFORMS)
    i.add_argument("native_meeting_id")

    f = sub.add_parser("ingest-file", help="file a saved Vexa transcript JSON in the knowledge base")
    f.add_argument("account")
    f.add_argument("path")

    a = p.parse_args(argv)
    try:
        if a.cmd == "ingest-file":
            print(ingest_file(a.account, a.path))
            return 0
        with VexaClient(base_url=a.base_url) as c:
            if a.cmd == "bot":
                out = c.stop_bot(a.platform, a.native_meeting_id) if a.stop else c.request_bot(
                    a.platform, a.native_meeting_id, passcode=a.passcode, bot_name=a.bot_name,
                    language=a.language, meeting_url=a.meeting_url)
                print(json.dumps(out, indent=2))
            elif a.cmd == "meetings":
                print(json.dumps(c.list_meetings(), indent=2))
            elif a.cmd == "status":
                print(json.dumps(c.bot_status(), indent=2))
            elif a.cmd == "transcript":
                tr = c.get_transcript(a.platform, a.native_meeting_id)
                print(json.dumps(tr, indent=2) if a.json else transcript_to_text(tr), end="" if not a.json else "\n")
            elif a.cmd == "ingest":
                print(ingest_meeting(a.account, a.platform, a.native_meeting_id, client=c))
    except (VexaError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
