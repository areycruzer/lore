"""Export Lore wiki events as Graphiti episodes.

Graphiti (https://github.com/getzep/graphiti, Apache-2.0) builds a temporal knowledge graph from
"episodes". Each Lore event becomes one JSON episode, so the graph inherits Lore's citations, source
ranks, epistemic status, and supersession history. Lore stays the system of record; the graph is a
derived, rebuildable view.

API used (graphiti-core README and examples/quickstart, checked 2026-11-16):

    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType
    graphiti = Graphiti(uri, user, password)                       # Neo4j
    graphiti = Graphiti(graph_driver=FalkorDriver(host=, port=))   # FalkorDB
    await graphiti.build_indices_and_constraints()
    await graphiti.add_episode(name=, episode_body=, source=EpisodeType.json, source_description=,
                               reference_time=datetime, group_id=)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

INSTALL_HINT = "graphiti-core is not installed. Install it with `uv add graphiti-core` (add the `falkordb` extra for FalkorDB)."


def _kb(kb: Any) -> Any:
    if kb is None:
        from lore.kb.store import KnowledgeBase
        kb = KnowledgeBase()
    return kb


def _said_by(raw: Any, contacts: dict[str, dict]) -> dict | None:
    if not raw:
        return None
    c = contacts.get(str(raw))
    if c:
        return {"contact_id": c.get("contact_id"), "name": c.get("name"), "role": c.get("role")}
    return {"contact_id": None, "name": str(raw), "role": None}


def _reference_time(occurred_at: Any) -> str:
    d = dt.date.fromisoformat(str(occurred_at)[:10])
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).isoformat()


def build_episodes(account: str | None = None, kb: Any = None) -> list[dict]:
    """One episode per wiki event (current and superseded), oldest first per account.

    Each episode is a dict with graphiti add_episode arguments: name, episode_body (JSON string),
    source ("json"), source_description, reference_time (ISO, UTC midnight of occurred_at), group_id.
    """
    kb = _kb(kb)
    accounts = [account] if account else [a["account_id"] for a in kb.list_accounts()]
    episodes: list[dict] = []
    for acc in accounts:
        meta = kb.account_meta(acc)
        contacts = {c.get("contact_id"): c for c in kb.contacts(acc)}
        superseded = kb.superseded_ids(acc)
        for e in kb.events(acc):
            m = e.meta
            body = {
                "event_id": m.get("id"),
                "account_id": acc,
                "account_name": meta.get("name"),
                "kind": m.get("kind"),
                "topic": m.get("topic"),
                "value": m.get("value"),
                "summary": m.get("summary"),
                "said_by": _said_by(m.get("said_by"), contacts),
                "product": m.get("product"),
                "epistemic_status": m.get("epistemic_status"),
                "source": m.get("source"),
                "source_type": m.get("source_type"),
                "source_rank": m.get("source_rank"),
                "occurred_at": str(m.get("occurred_at")),
                "week": m.get("week"),
                "supersedes": m.get("supersedes") or [],
                "superseded": m.get("id") in superseded,
            }
            episodes.append({
                "name": m.get("id"),
                "episode_body": json.dumps(body, ensure_ascii=False, default=str),
                "source": "json",
                "source_description": f"Lore {m.get('source_type')} (rank {m.get('source_rank')})",
                "reference_time": _reference_time(m.get("occurred_at")),
                "group_id": acc,
            })
    return episodes


def export_jsonl(path: str | Path, account: str | None = None, kb: Any = None) -> int:
    """Write episodes to a JSONL file (no graphiti install needed). Returns the episode count."""
    episodes = build_episodes(account, kb)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ep in episodes:
            fh.write(json.dumps(ep, ensure_ascii=False) + "\n")
    return len(episodes)


def _graphiti(uri: str, user: str, password: str) -> Any:
    try:
        from graphiti_core import Graphiti
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    scheme = urlparse(uri).scheme
    if scheme in ("falkor", "falkordb", "redis"):
        try:
            from graphiti_core.driver.falkordb_driver import FalkorDriver
        except ImportError as exc:
            raise RuntimeError("FalkorDB support missing: `uv add 'graphiti-core[falkordb]'`") from exc
        u = urlparse(uri)
        return Graphiti(graph_driver=FalkorDriver(host=u.hostname or "localhost", port=u.port or 6379,
                                                  username=user or None, password=password or None))
    return Graphiti(uri, user, password)


async def push_to_graphiti(account: str | None = None, uri: str | None = None, user: str | None = None,
                           password: str | None = None, kb: Any = None) -> int:
    """Add every episode to a Graphiti graph. Returns the episode count.

    Database: GRAPHITI_URI (default bolt://localhost:7687 for Neo4j; use falkor://host:6379 for FalkorDB),
    GRAPHITI_USER (default neo4j), GRAPHITI_PASSWORD. Graphiti runs an LLM over every episode to extract
    entities and edges, so it also needs an LLM key: OPENAI_API_KEY by default, or a configured Anthropic,
    Gemini, Azure OpenAI, or OpenAI-compatible on-prem endpoint (see the graphiti README). This sends event
    text to that LLM; use an on-prem endpoint for customer data.
    """
    uri = uri or os.environ.get("GRAPHITI_URI", "bolt://localhost:7687")
    user = user if user is not None else os.environ.get("GRAPHITI_USER", "neo4j")
    password = password if password is not None else os.environ.get("GRAPHITI_PASSWORD", "")
    episodes = build_episodes(account, kb)
    graphiti = _graphiti(uri, user, password)
    from graphiti_core.nodes import EpisodeType
    try:
        await graphiti.build_indices_and_constraints()
        for ep in episodes:
            await graphiti.add_episode(
                name=ep["name"],
                episode_body=ep["episode_body"],
                source=EpisodeType.json,
                source_description=ep["source_description"],
                reference_time=dt.datetime.fromisoformat(ep["reference_time"]),
                group_id=ep["group_id"],
            )
    finally:
        await graphiti.close()
    return len(episodes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m lore.export.graphiti_export", description="Export Lore events as Graphiti episodes")
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("jsonl", help="write episodes to a JSONL file (offline)")
    j.add_argument("--account")
    j.add_argument("--out", default="graphiti_episodes.jsonl")
    pu = sub.add_parser("push", help="add episodes to a Graphiti graph (needs graphiti-core, a graph DB and an LLM key)")
    pu.add_argument("--account")
    pu.add_argument("--uri")
    pu.add_argument("--user")
    pu.add_argument("--password")
    a = p.parse_args(argv)
    try:
        if a.cmd == "jsonl":
            n = export_jsonl(a.out, a.account)
            print(f"wrote {n} episodes to {a.out}")
        else:
            n = asyncio.run(push_to_graphiti(a.account, a.uri, a.user, a.password))
            print(f"pushed {n} episodes to Graphiti")
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
