# Open-source projects Lore reuses or sits next to

Star counts and licences were checked in November 2026 and are approximate.

## Vexa (meeting capture)

- **What it is:** a self-hosted meeting bot and real-time transcription service for Microsoft Teams, Zoom and Google Meet, with a REST API. https://github.com/Vexa-ai/vexa
- **Licence / stars:** Apache-2.0, about 3k stars.
- **How Lore uses it:** `lore/connectors/vexa.py` sends a "Lore Notetaker" bot into a Teams call, fetches the speaker-attributed transcript, and files it as a `meeting_transcript` raw file (source rank 1, the most trusted). Lore's ingest agent then extracts cited events from it. Vexa runs on Singtel infrastructure, so audio and transcripts stay on-prem.
- **Endpoints used** (Vexa 0.12 Meetings API, https://docs.vexa.ai/api/meetings, all with header `X-API-Key`): `POST /bots`, `GET /bots/status`, `DELETE /bots/{platform}/{native_meeting_id}`, `GET /meetings`, `GET /transcripts/{platform}/{native_meeting_id}`. The self-hosted gateway defaults to `http://localhost:18056` in 0.12 (older releases used 8056).

```bash
export VEXA_API_URL=http://localhost:18056   # your Vexa gateway
export VEXA_API_KEY=...                      # key issued by your Vexa deployment

# Teams: numeric meeting ID plus passcode, or the thread id from a meetup-join link
uv run python -m lore.connectors.vexa bot teams 3817264905123 --passcode ABC123
uv run python -m lore.connectors.vexa status
uv run python -m lore.connectors.vexa meetings
uv run python -m lore.connectors.vexa transcript teams 3817264905123
uv run python -m lore.connectors.vexa ingest ACC-0001 teams 3817264905123
uv run python -m lore.connectors.vexa bot teams 3817264905123 --stop

# Offline demo: file the bundled sample transcript (adds a raw file to the knowledge base)
uv run python -m lore.connectors.vexa ingest-file ACC-0001 seed/vexa_sample_transcript.json
```

## Graphiti (temporal knowledge graph export)

- **What it is:** a library from Zep that builds temporal knowledge graphs from "episodes" on Neo4j, FalkorDB or Amazon Neptune. https://github.com/getzep/graphiti
- **Licence / stars:** Apache-2.0, about 32.8k stars.
- **How Lore uses it:** `lore/export/graphiti_export.py` turns every wiki event (current and superseded) into one JSON episode. The episode keeps the citation, source rank, epistemic status, speaker and superseded flag; `group_id` is the account id and `reference_time` is the date the event happened. Lore stays the system of record and the graph is a derived view that can be rebuilt. The JSONL export works offline; the push needs `graphiti-core`, a graph database, and an LLM key (OpenAI by default, or an OpenAI-compatible on-prem endpoint) because Graphiti extracts entities with an LLM.

```bash
uv run python -m lore.export.graphiti_export jsonl --account ACC-0001 --out /tmp/lore_graphiti_acc0001.jsonl
uv run python -m lore.export.graphiti_export jsonl --out /tmp/lore_graphiti_all.jsonl   # all accounts

uv add graphiti-core            # or 'graphiti-core[falkordb]'
export GRAPHITI_URI=bolt://localhost:7687 GRAPHITI_USER=neo4j GRAPHITI_PASSWORD=...   # or falkor://localhost:6379
export OPENAI_API_KEY=...       # needed by Graphiti for extraction and embeddings
uv run python -m lore.export.graphiti_export push --account ACC-0001
```

## tsmztech/mcp-server-salesforce

- **What it is:** an MCP server that exposes Salesforce query, search and DML tools. https://github.com/tsmztech/mcp-server-salesforce
- **Licence / stars:** MIT, about 165 stars.
- **How Lore uses it:** the read/write path to Salesforce CRM records (accounts, contacts, opportunities, tasks) when a self-managed MCP server is preferred.

## Salesforce Hosted MCP Server (official)

- **What it is:** Salesforce's own hosted MCP endpoint for sObject access, authenticated with OAuth.
- **Licence:** proprietary Salesforce service (not open source), listed here because Lore treats it as the default supported CRM connector.
- **How Lore uses it:** `LORE_CRM=salesforce` points Lore at `SF_MCP_URL` (see `lore/config.py`) so CRM reads and writes go through the official endpoint.

## Karpathy "LLM Wiki" pattern

- **What it is:** Andrej Karpathy's pattern of having an LLM maintain a plain-markdown wiki that it reads and updates, instead of retrieving raw chunks at query time.
- **Licence:** a published idea, not a software dependency.
- **How Lore uses it:** the knowledge base layout (`raw/` sources, `wiki/` pages, an index to walk links) follows this pattern, with Lore adding append-only files, line citations, source ranks and an audit ledger.

## IBM mcp-context-forge

- **What it is:** an MCP gateway and registry that federates MCP servers and adds auth, rate limiting and observability. https://github.com/IBM/mcp-context-forge
- **Licence / stars:** Apache-2.0, about 4.8k stars.
- **How Lore uses it:** the production option for fronting Lore's MCP servers (and the Salesforce MCP) behind one governed gateway for ChatGPT Enterprise. Not required for the demo.

## Onyx and Airweave (adjacent platforms)

- **Onyx** (https://github.com/onyx-dot-app/onyx, about 33k stars) and **Airweave** (https://github.com/airweave-ai/airweave, about 6.5k stars) are open-source retrieval platforms that index many SaaS sources for search and agents.
- **How Lore relates:** they are adjacent or competing approaches. Lore does not use them; it differs by keeping a curated, cited, append-only account wiki with conflict resolution by source rank rather than a search index over raw documents.
