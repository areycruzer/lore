"""Lore knowledge base: an additive markdown wiki of every customer, filed by ISO week.

Rules enforced here (and only here, because every agent writes through the MCP that calls this module):
  * raw/ and timeline/ files are created, never modified or deleted;
  * every event cites lines of a raw file that exist;
  * every event carries an epistemic_status;
  * source rank is derived from the raw file type, never trusted from the caller;
  * front pages are rebuilt from events, never edited;
  * every write is appended to a hash-chained audit ledger (and committed to git when available).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lore import config

RAW_TYPES: dict[str, int] = {
    # rank 1 = most trusted. Meetings outrank everything.
    "meeting_transcript": 1,
    "call_transcript": 1,
    "customer_email": 2,
    "crm_record": 3,
    "am_note": 3,
    "ops_report": 3,
    "product_catalogue": 3,
    "calendar": 4,
    "public_signal": 5,
    "research_note": 6,
}
RANK_LABEL = {1: "meeting", 2: "customer email", 3: "CRM / AM note / ops report", 4: "calendar", 5: "public signal", 6: "research agent"}

EVENT_KINDS = {
    "fact", "need", "budget", "timeline", "signal", "churn_risk", "objection", "commitment", "ops_request",
    "relationship", "status", "product_owned", "opportunity", "meeting_note", "resolution", "stale", "contact",
}
EPISTEMIC = {"confirmed", "stated", "hedged", "reported", "inferred"}
GATES = {"permitted_demo", "service_recovery_first", "consent_review", "not_applicable", "blocked_do_not_contact", ""}

ACCOUNT_ID_RE = re.compile(r"^ACC-\d{4}$")


class KBError(ValueError):
    """A rejected write. The message tells the calling agent what to do instead."""


def iso_week(date: str | dt.date) -> tuple[int, int]:
    d = dt.date.fromisoformat(date[:10]) if isinstance(date, str) else date
    y, w, _ = d.isocalendar()
    return y, w


def week_label(date: str | dt.date) -> str:
    y, w = iso_week(date)
    return f"{y}-W{w:02d}"


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = yaml.safe_load(text[4:end]) or {}
    return meta, text[end + 5:]


def _render(meta: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body.rstrip("\n") + "\n"


def _new_id(prefix: str, date: str) -> str:
    y, w = iso_week(date)
    return f"{prefix}-{y}W{w:02d}-{secrets.token_hex(3)}"


@dataclass
class Event:
    meta: dict
    body: str
    path: Path

    @property
    def id(self) -> str:
        return self.meta["id"]

    def get(self, key: str, default: Any = None) -> Any:
        return self.meta.get(key, default)


class KnowledgeBase:
    def __init__(self, root: Path | None = None, git: bool | None = None):
        self.root = Path(root or config.KB_DIR)
        self.raw_dir = self.root / "raw"
        self.wiki_dir = self.root / "wiki"
        self.inbox_dir = self.root / "inbox"
        self.ledger = self.root / "audit" / "ledger.jsonl"
        self.lock_file = self.root / ".lock"
        self.git_enabled = (os.environ.get("LORE_KB_GIT", "1") == "1") if git is None else git
        self._git_ok: bool | None = None
        self._event_cache: dict[str, list[Event]] | None = None

    # ------------------------------------------------------------------ setup
    def init(self) -> None:
        for d in (self.raw_dir, self.wiki_dir / "accounts", self.wiki_dir / "products", self.wiki_dir / "signals",
                  self.inbox_dir, self.ledger.parent):
            d.mkdir(parents=True, exist_ok=True)
        if self.git_enabled and not (self.root / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=self.root, check=False)

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ audit
    def _audit(self, actor: str, action: str, paths: Iterable[Path], detail: dict | None = None) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        # Called under the KB file lock; read only the tail so other processes' entries are chained correctly.
        prev = "0" * 64
        if self.ledger.exists() and self.ledger.stat().st_size:
            with self.ledger.open("rb") as fh:
                fh.seek(max(0, self.ledger.stat().st_size - 8192))
                tail = fh.read().splitlines()
            prev = json.loads(tail[-1])["hash"]
        rel = [str(p.relative_to(self.root)) for p in paths]
        entry = {"at": now_iso(), "actor": actor, "action": action, "paths": rel, "detail": detail or {}, "prev": prev}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        with self.ledger.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def verify_ledger(self) -> tuple[bool, int]:
        prev = "0" * 64
        n = 0
        if not self.ledger.exists():
            return True, 0
        for line in self.ledger.read_text().splitlines():
            entry = json.loads(line)
            h = entry.pop("hash")
            if entry["prev"] != prev or hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest() != h:
                return False, n
            prev = h
            n += 1
        return True, n

    def _commit(self, actor: str, message: str, paths: Iterable[Path]) -> None:
        if not self.git_enabled or self._git_ok is False:
            return
        rel = [str(p.relative_to(self.root)) for p in paths]
        add = subprocess.run(["git", "add", "--", *rel, "audit/ledger.jsonl"], cwd=self.root, capture_output=True, text=True)
        msg = f"{message}\n\nLore-Actor: {actor}"
        res = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=self.root, capture_output=True, text=True)
        if add.returncode != 0 or (res.returncode != 0 and "nothing to commit" not in res.stdout + res.stderr):
            # The hash-chained ledger stays the audit log of record; git is a convenience mirror.
            self._git_ok = False

    def _record(self, actor: str, action: str, paths: list[Path], detail: dict | None = None, commit: bool = True) -> None:
        self._audit(actor, action, paths, detail)
        if commit:
            self._commit(actor, f"{action}: {', '.join(str(p.relative_to(self.root)) for p in paths[:3])}", paths)

    # ------------------------------------------------------------------ paths
    def account_dir(self, account: str) -> Path:
        return self.wiki_dir / "accounts" / account

    def _write_new(self, path: Path, text: str) -> None:
        if path.exists():
            raise KBError(f"file exists: {path.relative_to(self.root)}. Files are never overwritten; add a superseding event instead.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(text)

    def _write_generated(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    # ------------------------------------------------------------------ accounts
    def has_account(self, account: str) -> bool:
        return (self.account_dir(account) / "account.md").exists()

    def account_meta(self, account: str) -> dict:
        p = self.account_dir(account) / "account.md"
        if not p.exists():
            raise KBError(f"unknown account {account}. Call kb_index or kb_search to find the right account id.")
        meta, _ = _split_frontmatter(p.read_text())
        return meta

    def list_accounts(self) -> list[dict]:
        out = []
        base = self.wiki_dir / "accounts"
        if not base.exists():
            return out
        for d in sorted(base.iterdir()):
            p = d / "account.md"
            if p.exists():
                out.append(_split_frontmatter(p.read_text())[0])
        return out

    def add_account(self, meta: dict, body: str = "", actor: str = "system", commit: bool = True) -> Path:
        account = meta.get("account_id", "")
        if not ACCOUNT_ID_RE.match(account):
            raise KBError("account_id must look like ACC-0001")
        if not meta.get("name"):
            raise KBError("name required")
        with self._locked():
            path = self.account_dir(account) / "account.md"
            meta = {**meta, "created_at": meta.get("created_at") or now_iso(), "author": actor}
            self._write_new(path, _render(meta, body or f"# {meta['name']}\n"))
            self._record(actor, "add_account", [path], {"account": account}, commit)
        return path

    def add_contact(self, account: str, meta: dict, body: str = "", actor: str = "system", commit: bool = True) -> Path:
        self.account_meta(account)
        cid = meta.get("contact_id") or f"CON-L{secrets.token_hex(2).upper()}"
        if not meta.get("name") or not meta.get("role"):
            raise KBError("name and role required for a contact")
        with self._locked():
            path = self.account_dir(account) / "contacts" / f"{cid}.md"
            meta = {**meta, "contact_id": cid, "account": account, "author": actor, "created_at": now_iso()}
            self._write_new(path, _render(meta, body or f"# {meta['name']}\n"))
            self._record(actor, "add_contact", [path], {"account": account, "contact": cid}, commit)
        return path

    def contacts(self, account: str) -> list[dict]:
        d = self.account_dir(account) / "contacts"
        if not d.exists():
            return []
        return [_split_frontmatter(p.read_text())[0] for p in sorted(d.glob("*.md"))]

    # ------------------------------------------------------------------ raw
    def add_raw(self, type: str, account: str | None, occurred_at: str, text: str, origin: str,
                actor: str = "system", title: str = "", commit: bool = True, raw_id: str | None = None) -> str:
        if type not in RAW_TYPES:
            raise KBError(f"unknown raw type {type!r}; use one of {sorted(RAW_TYPES)}")
        if account and not self.has_account(account):
            raise KBError(f"unknown account {account}; add it with kb_add_account first")
        try:
            dt.date.fromisoformat(occurred_at[:10])
        except ValueError as exc:
            raise KBError("occurred_at must be an ISO date (YYYY-MM-DD)") from exc
        if not text.strip():
            raise KBError("text is empty")
        y, w = iso_week(occurred_at)
        rid = raw_id or _new_id("RAW", occurred_at)
        path = self.raw_dir / str(y) / f"W{w:02d}" / f"{rid}.md"
        meta = {"id": rid, "type": type, "account": account, "occurred_at": occurred_at[:10], "origin": origin,
                "source_rank": RAW_TYPES[type], "title": title, "author": actor, "ingested_at": now_iso()}
        with self._locked():
            self._write_new(path, _render(meta, text))
            self._record(actor, "add_raw", [path], {"type": type, "account": account}, commit)
        return self.rel(path)

    def read_raw(self, ref: str) -> tuple[dict, list[str]]:
        path_part = ref.split("#", 1)[0]
        path = self.root / path_part
        if not path.exists() or not path_part.startswith("raw/"):
            raise KBError(f"source missing: {path_part} does not exist under raw/")
        meta, body = _split_frontmatter(path.read_text())
        return meta, body.splitlines()

    @staticmethod
    def parse_lines(ref: str) -> tuple[int, int] | None:
        if "#" not in ref:
            return None
        frag = ref.split("#", 1)[1]
        m = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", frag)
        if not m:
            raise KBError("source line anchor must look like #L3 or #L3-L5")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        return a, b

    def quote(self, ref: str) -> str:
        meta, lines = self.read_raw(ref)
        span = self.parse_lines(ref)
        if span is None:
            return "\n".join(lines)
        a, b = span
        return "\n".join(lines[a - 1:b])

    # ------------------------------------------------------------------ events
    def add_event(self, event: dict, body: str = "", actor: str = "system", commit: bool = True, rebuild: bool = True) -> str:
        e = dict(event)
        account = e.get("account")
        self.account_meta(account)
        kind = e.get("kind")
        if kind not in EVENT_KINDS:
            raise KBError(f"kind must be one of {sorted(EVENT_KINDS)}")
        if not e.get("summary"):
            raise KBError("summary required: one plain-language line")
        src = e.get("source")
        if not src:
            raise KBError("source missing: every event must cite raw/...#Lnn")
        raw_meta, lines = self.read_raw(src)
        span = self.parse_lines(src)
        if span is None:
            raise KBError("source must include a line anchor, e.g. raw/2026/W46/RAW-...md#L4")
        if span[0] < 1 or span[1] > len(lines) or span[0] > span[1]:
            raise KBError(f"source lines {span} out of range; raw file has {len(lines)} lines")
        if raw_meta.get("account") not in (None, account):
            raise KBError(f"source belongs to {raw_meta.get('account')}, not {account}")
        status = e.get("epistemic_status")
        if status not in EPISTEMIC:
            raise KBError(f"epistemic_status required: one of {sorted(EPISTEMIC)}")
        gate = e.get("gate", "") or ""
        if gate not in GATES:
            raise KBError(f"gate must be one of {sorted(GATES)}")
        supersedes = e.get("supersedes") or []
        if isinstance(supersedes, str):
            supersedes = [supersedes]
        existing = {ev.id for ev in self.events(account)}
        missing = [s for s in supersedes if s not in existing]
        if missing:
            raise KBError(f"supersedes unknown events {missing}")
        occurred = str(e.get("occurred_at") or raw_meta["occurred_at"])[:10]
        eid = _new_id("EVT", occurred)
        meta = {
            "id": eid, "account": account, "kind": kind, "topic": e.get("topic") or None, "value": e.get("value"),
            "summary": e["summary"].strip(), "week": week_label(occurred), "occurred_at": occurred, "source": src,
            "source_type": raw_meta["type"], "source_rank": raw_meta["source_rank"], "epistemic_status": status,
            "said_by": e.get("said_by"), "product": e.get("product"), "gate": gate or None, "supersedes": supersedes or None,
            "author": actor, "created_at": now_iso(),
        }
        if e.get("extra"):
            meta["extra"] = e["extra"]
        meta = {k: v for k, v in meta.items() if v not in (None, [], "")}
        quote = self.quote(src)
        text = "> " + quote.replace("\n", "\n> ") + "\n" + (("\n" + body.strip() + "\n") if body.strip() else "")
        y, w = iso_week(occurred)
        path = self.account_dir(account) / "timeline" / f"{y}-W{w:02d}" / f"{eid}.md"
        with self._locked():
            self._write_new(path, _render(meta, text))
            self._event_cache = None
            self._record(actor, "add_event", [path], {"account": account, "kind": kind, "topic": meta.get("topic")}, commit)
        if rebuild:
            self.rebuild_account(account, actor=actor, commit=commit)
        return self.rel(path)

    def events(self, account: str) -> list[Event]:
        if self._event_cache is not None and account in self._event_cache:
            return self._event_cache[account]
        out = []
        tdir = self.account_dir(account) / "timeline"
        if tdir.exists():
            for p in tdir.rglob("*.md"):
                meta, body = _split_frontmatter(p.read_text())
                out.append(Event(meta, body, p))
        out.sort(key=lambda e: (str(e.get("occurred_at")), str(e.get("created_at"))))
        if self._event_cache is None:
            self._event_cache = {}
        self._event_cache[account] = out
        return out

    def superseded_ids(self, account: str) -> set[str]:
        s: set[str] = set()
        for e in self.events(account):
            for x in e.get("supersedes") or []:
                s.add(x)
        return s

    def current_events(self, account: str) -> list[Event]:
        sup = self.superseded_ids(account)
        return [e for e in self.events(account) if e.id not in sup and e.get("kind") not in ("resolution",)]

    def conflicts(self, account: str | None = None) -> list[dict]:
        accounts = [account] if account else [a["account_id"] for a in self.list_accounts()]
        out = []
        for acc in accounts:
            resolved = {e.get("topic"): e for e in self.events(acc) if e.get("kind") == "resolution"}
            by_topic: dict[str, list[Event]] = {}
            for e in self.current_events(acc):
                if e.get("topic") and e.get("value") not in (None, ""):
                    by_topic.setdefault(e.get("topic"), []).append(e)
            for topic, evs in by_topic.items():
                values = {str(e.get("value")) for e in evs}
                if len(values) < 2:
                    continue
                if topic in resolved:
                    continue
                ranked = sorted(evs, key=lambda e: (e.get("source_rank", 9), _neg_date(e.get("occurred_at"))))
                winner, losers = ranked[0], [e for e in ranked[1:] if str(e.get("value")) != str(ranked[0].get("value"))]
                out.append({
                    "id": f"CONF-{acc}-{topic}", "account": acc, "topic": topic,
                    "winner": _brief(winner), "losers": [_brief(e) for e in losers],
                    "rule": f"{RANK_LABEL[winner.get('source_rank')]} (rank {winner.get('source_rank')}) outranks lower-rank sources",
                })
        return out

    def resolve_conflict(self, conflict_id: str, winning_event: str, reason: str, actor: str = "system") -> str:
        m = re.fullmatch(r"CONF-(ACC-\d{4})-(.+)", conflict_id)
        if not m:
            raise KBError("conflict id must look like CONF-ACC-0001-topic")
        account, topic = m.group(1), m.group(2)
        win = next((e for e in self.events(account) if e.id == winning_event), None)
        if not win or win.get("topic") != topic:
            raise KBError(f"{winning_event} is not an event on topic {topic} for {account}")
        losers = [e.id for e in self.current_events(account) if e.get("topic") == topic and e.id != win.id]
        return self.add_event({
            "account": account, "kind": "resolution", "topic": topic, "value": win.get("value"),
            "summary": f"Conflict on {topic} resolved in favour of {win.id}: {reason}", "source": win.get("source"),
            "epistemic_status": "confirmed", "supersedes": losers,
        }, body=f"Reason: {reason}\nLosing events stay in the timeline: {', '.join(losers) or 'none'}", actor=actor)

    # ------------------------------------------------------------------ front pages
    def rebuild_account(self, account: str, actor: str = "system", commit: bool = True) -> Path:
        meta = self.account_meta(account)
        events = self.events(account)
        sup = self.superseded_ids(account)
        current = [e for e in events if e.id not in sup and e.get("kind") != "resolution"]
        conflicts = self.conflicts(account)
        conflict_topics = {c["topic"] for c in conflicts}
        L: list[str] = []
        L.append(f"# {meta['name']} ({account})")
        L.append("")
        L.append(f"_Generated front page. Do not edit: it is rebuilt from the timeline. Last rebuild {now_iso()}._")
        L.append("")
        L.append("| Field | Value |\n|---|---|")
        for k in ("status", "sector", "segment", "hq", "employees", "owner", "renewal", "owned_products", "relationship_health"):
            if meta.get(k) not in (None, ""):
                v = ", ".join(meta[k]) if isinstance(meta[k], list) else meta[k]
                L.append(f"| {k.replace('_', ' ')} | {v} |")
        L.append("")
        L.append("## Summary")
        L.append(_summary(meta, current, conflicts))
        L.append("")
        # Current facts by topic, highest-ranked source first, superseded history struck through.
        L.append("## Current facts")
        topics: dict[str, list[Event]] = {}
        for e in events:
            if e.get("topic"):
                topics.setdefault(e.get("topic"), []).append(e)
        for topic in sorted(topics):
            evs = topics[topic]
            live = [e for e in evs if e.id not in sup and e.get("kind") != "resolution"]
            if not live:
                continue
            live.sort(key=lambda e: (e.get("source_rank", 9), _neg_date(e.get("occurred_at"))))
            top = live[0]
            flag = " **CONTRADICTION**" if topic in conflict_topics else ""
            L.append(f"- **{topic}**: {top.get('summary')} [{top.get('epistemic_status')}, {RANK_LABEL.get(top.get('source_rank'))}] ({top.get('source')}){flag}")
            for old in [e for e in evs if e.id in sup]:
                L.append(f"  - ~~{old.get('summary')}~~ ({old.get('occurred_at')}, superseded) ({old.get('source')})")
            for other in live[1:]:
                if str(other.get("value")) != str(top.get("value")):
                    L.append(f"  - disagrees: {other.get('summary')} [{RANK_LABEL.get(other.get('source_rank'))}] ({other.get('source')})")
        L.append("")
        if conflicts:
            L.append("## Open contradictions")
            for c in conflicts:
                L.append(f"- `{c['id']}`: winner {c['winner']['summary']} ({c['winner']['source']}); "
                         + "; ".join(f"loser {x['summary']} ({x['source']})" for x in c["losers"]) + f". Rule: {c['rule']}.")
            L.append("")
        bridges = [e for e in current if e.get("kind") == "ops_request" and (e.get("extra") or {}).get("bridge")]
        if bridges:
            L.append("## Ops → AM bridge (technical requests in plain language)")
            for e in bridges[-8:]:
                L.append(f"- {e.get('summary')} Gate: `{e.get('gate', 'n/a')}` ({e.get('source')})")
            L.append("")
        sig = [e for e in current if e.get("kind") in ("signal", "churn_risk")]
        if sig:
            L.append("## Signals")
            for e in sorted(sig, key=lambda e: str(e.get("occurred_at")), reverse=True)[:10]:
                L.append(f"- {e.get('occurred_at')} [{e.get('kind')}] {e.get('summary')} ({e.get('source')})")
            L.append("")
        com = [e for e in current if e.get("kind") == "commitment"]
        if com:
            L.append("## Open commitments and next steps")
            for e in sorted(com, key=lambda e: str(e.get("occurred_at")), reverse=True)[:8]:
                L.append(f"- {e.get('occurred_at')}: {e.get('summary')} ({e.get('source')})")
            L.append("")
        contacts = self.contacts(account)
        if contacts:
            L.append("## Contacts")
            for c in contacts:
                flags = []
                if c.get("is_champion"):
                    flags.append("champion")
                if c.get("do_not_contact"):
                    flags.append("DO NOT CONTACT")
                L.append(f"- [{c['name']}](contacts/{c['contact_id']}.md), {c.get('role')} ({c.get('authority', '?')} authority, influence {c.get('influence', '?')}){' ' + ', '.join(flags) if flags else ''}")
            L.append("")
        L.append(f"Timeline: `{self.rel(self.account_dir(account) / 'timeline')}/` ({len(events)} events, {len(sup)} superseded)")
        path = self.account_dir(account) / "index.md"
        with self._locked():
            self._write_generated(path, "\n".join(L) + "\n")
            if commit:
                self._commit(actor, f"rebuild front page {account}", [path])
        return path

    def rebuild_index(self, actor: str = "system", commit: bool = True) -> Path:
        accounts = self.list_accounts()
        L = ["# Lore index", "", "Start here. Walk links rather than guessing paths. Every fact cites `raw/...#Lnn`.", "",
             "## Accounts", "", "| Account | Name | Status | Sector | HQ | Owner |", "|---|---|---|---|---|---|"]
        for a in accounts:
            L.append(f"| [{a['account_id']}](accounts/{a['account_id']}/index.md) | {a['name']} | {a.get('status', '')} | {a.get('sector', '')} | {a.get('hq', '')} | {a.get('owner', '')} |")
        L += ["", "## Products", ""]
        pdir = self.wiki_dir / "products"
        for p in sorted(pdir.glob("*.md")) if pdir.exists() else []:
            m, _ = _split_frontmatter(p.read_text())
            L.append(f"- [{m.get('name', p.stem)}](products/{p.name}): {m.get('category', '')}; fits {m.get('pain_tags', '')}")
        L += ["", "## Weekly signals", ""]
        sdir = self.wiki_dir / "signals"
        for p in sorted(sdir.rglob("*.md"), reverse=True)[:8] if sdir.exists() else []:
            L.append(f"- [{p.stem}]({p.relative_to(self.wiki_dir)})")
        path = self.wiki_dir / "index.md"
        with self._locked():
            self._write_generated(path, "\n".join(L) + "\n")
            if commit:
                self._commit(actor, "rebuild index", [path])
        return path

    def write_page(self, rel_path: str, text: str, actor: str = "system", commit: bool = True) -> Path:
        """Generated pages only (rollups, product pages). Never raw/ or timeline/."""
        if rel_path.startswith("raw/") or "/timeline/" in rel_path or rel_path.endswith("account.md"):
            raise KBError("raw, timeline and account files are append-only; use kb_add_raw or kb_add_event")
        path = self.root / rel_path
        with self._locked():
            self._write_generated(path, text)
            self._record(actor, "write_generated_page", [path], {}, commit)
        return path

    # ------------------------------------------------------------------ read helpers
    def get_page(self, rel_path: str) -> str:
        path = (self.root / rel_path).resolve()
        if not str(path).startswith(str(self.root.resolve())) or not path.exists():
            raise KBError(f"no such page {rel_path}")
        return path.read_text()

    def search(self, query: str, account: str | None = None, kind: str | None = None, limit: int = 40,
               allowed_accounts: set[str] | None = None) -> list[dict]:
        terms = [t.lower() for t in re.findall(r"[\w$.-]+", query) if len(t) > 1]
        if not terms:
            return []
        roots = [self.account_dir(account)] if account else [self.wiki_dir]
        if not account:
            roots.append(self.raw_dir)
        hits = []
        for root in roots:
            if not root.exists():
                continue
            for p in root.rglob("*.md"):
                text = p.read_text(errors="ignore")
                low = text.lower()
                if not all(t in low for t in terms):
                    continue
                meta, body = _split_frontmatter(text)
                acc = meta.get("account") or meta.get("account_id") or _account_from_path(p)
                if allowed_accounts is not None and acc and acc not in allowed_accounts:
                    continue
                if kind and meta.get("kind") != kind:
                    continue
                lines = [f"L{i}: {ln.strip()}" for i, ln in enumerate(body.splitlines(), 1) if any(t in ln.lower() for t in terms)][:4]
                score = sum(low.count(t) for t in terms)
                hits.append({"path": self.rel(p), "account": acc, "kind": meta.get("kind") or meta.get("type"),
                             "summary": meta.get("summary") or meta.get("title") or "", "lines": lines, "score": score})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]


def _account_from_path(p: Path) -> str | None:
    parts = p.parts
    if "accounts" in parts:
        i = parts.index("accounts")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _neg_date(d: Any) -> int:
    try:
        return -dt.date.fromisoformat(str(d)[:10]).toordinal()
    except ValueError:
        return 0


def _brief(e: Event) -> dict:
    return {"event": e.id, "summary": e.get("summary"), "value": e.get("value"), "source": e.get("source"),
            "source_rank": e.get("source_rank"), "occurred_at": e.get("occurred_at"), "epistemic_status": e.get("epistemic_status")}


def _summary(meta: dict, current: list[Event], conflicts: list[dict]) -> str:
    sig = [e for e in current if e.get("kind") == "signal"]
    churn = [e for e in current if e.get("kind") == "churn_risk"]
    bridges = [e for e in current if e.get("kind") == "ops_request" and (e.get("extra") or {}).get("bridge")]
    status = meta.get("status", "account").replace("_", " ")
    article = "an" if status[:1] in "aeiou" else "a"
    parts = [f"{meta['name']} is {article} {status} in {meta.get('sector', 'an unknown sector')}"
             f" based in {meta.get('hq', 'an unknown location')}."]
    if sig:
        latest = sorted(sig, key=lambda e: str(e.get('occurred_at')))[-1].get('summary').rstrip('.')
        parts.append(f"{len(sig)} live buying signal(s), latest: {latest}.")
    if churn:
        parts.append(f"{len(churn)} churn-risk item(s) open: fix service before any pitch.")
    if bridges:
        parts.append(f"{len(bridges)} ops request(s) map to Singtel products the customer does not own yet.")
    if conflicts:
        parts.append(f"{len(conflicts)} contradiction(s) need an AM decision.")
    return " ".join(parts)
