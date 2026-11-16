"""Lore command line: serve the MCP servers, ask the agent, and run scheduled jobs.

  lore serve [--http] [--internal]          public MCP (stdio or streamable HTTP) or the internal MCP
  lore ask "question" [--user OWN-002]      ask the internal agent
  lore brief ACC-0001                       pre-meeting brief
  lore cron list | install                  show jobs / print crontab lines
  lore cron run <job> [--no-agent]          ingest-inbox | ops-bridge-scan | company-watch | weekly-rollup | stale-check | lint
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from lore import config

INBOX_DIR = config._path("LORE_INBOX", config.ROOT.parent / "outputs" / "lore-inbox")
STATE_DIR = config.VAR_DIR / "cron"

JOBS = {
    "ingest-inbox": ("*/15 * * * *", "File new transcripts, emails and ops reports dropped in the inbox, then extract cited facts"),
    "ops-bridge-scan": ("0 * * * *", "Send the account owner a Teams note for each new ops request that maps to a product they lack"),
    "company-watch": ("0 7 * * *", "Pull new public company signals (press, tenders, job posts) into the wiki"),
    "weekly-rollup": ("0 6 * * 1", "Rebuild the weekly signals page across all accounts"),
    "stale-check": ("30 6 * * 1", "Flag facts with no support newer than 26 weeks"),
    "lint": ("0 5 * * *", "Check every fact has a source and status, and the audit ledger hash chain is intact"),
}


def _state(name: str) -> set[str]:
    p = STATE_DIR / f"{name}.json"
    return set(json.loads(p.read_text())) if p.exists() else set()


def _save_state(name: str, values: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(json.dumps(sorted(values), indent=1))


def _known_origins(kb) -> set[str]:
    from lore.kb.store import _split_frontmatter

    return {str(_split_frontmatter(p.read_text())[0].get("origin")) for p in kb.raw_dir.rglob("*.md")}


# ---------------------------------------------------------------------------------------------------- jobs
def job_ingest_inbox(kb, use_agent: bool = True, inbox: Path | None = None) -> dict:
    """Inbox files: .md/.txt with YAML front matter (account, type, occurred_at, optional origin/title),
    or a Vexa transcript .json named ACC-0001_anything.json."""
    from lore.agent import run as run_agent
    from lore.connectors import vexa
    from lore.kb.store import KBError, _split_frontmatter

    inbox = inbox or INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    done_dir = inbox / "processed"
    results = []
    for f in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix in (".md", ".txt", ".json")):
        try:
            if f.suffix == ".json":
                account = f.name.split("_")[0]
                raw = vexa.ingest_file(account, f, kb=kb, actor="cron:ingest-inbox")
            else:
                meta, body = _split_frontmatter(f.read_text())
                missing = [k for k in ("account", "type", "occurred_at") if not meta.get(k)]
                if missing:
                    raise KBError(f"front matter missing {missing}")
                account = meta["account"]
                raw = kb.add_raw(meta["type"], account, str(meta["occurred_at"]), body.strip(), meta.get("origin") or f"inbox:{f.name}",
                                 actor="cron:ingest-inbox", title=meta.get("title") or f.stem)
            item = {"file": f.name, "account": account, "stored": raw}
            if use_agent:
                prompt = (config.PLAYBOOK_DIR / "ingest.md").read_text().replace("{raw}", raw).replace("{account}", account)
                res = run_agent(prompt, user="job:ingest-inbox", scope="write", actor="cron:ingest-inbox").to_dict()
                item["agent"] = {"answer": res["answer"], "error": res["error"], "tool_calls": res["tool_calls"], "elapsed_s": res["elapsed_s"]}
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(f), done_dir / f.name)
        except (KBError, ValueError, KeyError) as exc:
            item = {"file": f.name, "error": str(exc)}
        results.append(item)
    return {"inbox": str(inbox), "processed": results}


def job_ops_bridge_scan(kb, dry_run: bool = False) -> dict:
    from lore import actions, insights
    from lore.governance import principal

    sent = _state("ops-bridge-notified")
    out = []
    for item in insights.ops_bridge(kb, principal("job:ops-bridge-scan")):
        if item["event"] in sent:
            continue
        first = ("Fix the open service issue first, then raise this. " if item.get("gate") == "service_recovery_first" else "")
        text = (f"Ops bridge for {item['name']} ({item['account']}): {item['for_the_am']} {first}"
                f"Product: {item['product']}. Source: {item['source']}")
        if not dry_run:
            msg = actions.notify_am_teams(item["account"], text, requested_by="cron:ops-bridge-scan", source=item["source"])
            sent.add(item["event"])
            out.append({"account": item["account"], "to": msg["to"]["name"], "outbox_file": msg["outbox_file"]})
        else:
            out.append({"account": item["account"], "would_send": text})
    if not dry_run:
        _save_state("ops-bridge-notified", sent)
    return {"notified": out}


def job_company_watch(kb, accounts: list[str] | None = None) -> dict:
    from lore.connectors import mock_sources as src

    known = _known_origins(kb)
    added = []
    for a in kb.list_accounts():
        acc = a["account_id"]
        if accounts and acc not in accounts:
            continue
        name = a["name"].removeprefix("Demo ").strip()
        for r in src.web_search(name, limit=20):
            if r.get("account") != acc:
                continue
            sig_id = r["url"].rstrip("/").split("/")[-1].upper()
            origin = f"web:{r['url']}"
            if origin in known or any(o.endswith("/" + sig_id) for o in known if o.startswith("web:")):
                continue
            lines = [f"Headline: {r['title']}", f"Excerpt: {r['snippet']}", f"Published: {r['date']}", f"URL: {r['url']}"]
            raw = kb.add_raw("public_signal", acc, r["date"], "\n".join(lines), origin, actor="cron:company-watch", title=r["title"])
            ev = kb.add_event({"account": acc, "kind": "signal", "summary": f"Public signal: {r['title']}", "source": f"{raw}#L1-L2",
                               "epistemic_status": "reported", **({"topic": r["topic"]} if r.get("topic") else {})},
                              actor="cron:company-watch")
            known.add(origin)
            added.append({"account": acc, "title": r["title"], "raw": raw, "event": ev})
    return {"new_signals": added}


def run_job(name: str, use_agent: bool = True, dry_run: bool = False, accounts: list[str] | None = None) -> dict:
    from lore.kb import rollup
    from lore.kb.store import KnowledgeBase

    kb = KnowledgeBase()
    if name == "ingest-inbox":
        return job_ingest_inbox(kb, use_agent)
    if name == "ops-bridge-scan":
        return job_ops_bridge_scan(kb, dry_run)
    if name == "company-watch":
        return job_company_watch(kb, accounts)
    if name == "weekly-rollup":
        return {"page": rollup.weekly_rollup(kb, config.TODAY)}
    if name == "stale-check":
        return {"stale_events": rollup.stale_check(kb, config.TODAY)}
    if name == "lint":
        problems = rollup.lint(kb)
        return {"ok": not problems, "problems": problems[:50], "count": len(problems)}
    raise SystemExit(f"unknown job {name}; choose from {', '.join(JOBS)}")


# ---------------------------------------------------------------------------------------------------- entry point
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lore", description="Lore: Singtel customer knowledge layer")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run an MCP server")
    s.add_argument("--http", action="store_true", help=f"streamable HTTP on {config.PUBLIC_HOST}:{config.PUBLIC_PORT}/mcp")
    s.add_argument("--internal", action="store_true", help="run the internal MCP (stdio) instead of the public one")
    a = sub.add_parser("ask", help="ask the internal agent")
    a.add_argument("question")
    a.add_argument("--user", default=config.DEFAULT_USER)
    a.add_argument("--provider", choices=["auto", "claude", "anthropic", "codex", "openai", "onprem"])
    b = sub.add_parser("brief", help="pre-meeting brief for an account")
    b.add_argument("account")
    b.add_argument("--user", default=config.DEFAULT_USER)
    b.add_argument("--provider", choices=["auto", "claude", "anthropic", "codex", "openai", "onprem"])
    c = sub.add_parser("cron", help="scheduled jobs")
    c.add_argument("action", choices=["list", "install", "run"])
    c.add_argument("job", nargs="?")
    c.add_argument("--no-agent", action="store_true", help="ingest-inbox: store files without running the ingest agent")
    c.add_argument("--dry-run", action="store_true", help="ops-bridge-scan: show messages without sending")
    c.add_argument("--account", action="append", help="company-watch: limit to these accounts")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        if args.internal:
            from lore import internal_mcp

            internal_mcp.main()
        else:
            from lore import public_mcp

            public_mcp.main("streamable-http" if args.http else "stdio")
        return 0
    if args.cmd in ("ask", "brief"):
        from lore.agent import run as run_agent

        task = args.question if args.cmd == "ask" else (config.PLAYBOOK_DIR / "meeting_prep.md").read_text().replace("{account}", args.account)
        res = run_agent(task, user=args.user, scope="read", provider=args.provider).to_dict()
        print(res["answer"] or res["error"])
        print(f"\n[{res['provider']}, {res['elapsed_s']}s, tools: {', '.join(res['tool_calls']) or 'n/a'}]", file=sys.stderr)
        return 0 if not res["error"] else 1
    if args.action == "list":
        for name, (cron, desc) in JOBS.items():
            print(f"{name:16} {cron:14} {desc}")
        return 0
    if args.action == "install":
        uv = shutil.which("uv") or "uv"
        print("# Add these lines with `crontab -e` (times are local):")
        for name, (cron, _) in JOBS.items():
            print(f"{cron} cd {config.ROOT} && {uv} run lore cron run {name} >> {config.VAR_DIR}/cron.log 2>&1")
        return 0
    if not args.job:
        p.error("cron run needs a job name")
    out = run_job(args.job, use_agent=not args.no_agent, dry_run=args.dry_run, accounts=args.account)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
