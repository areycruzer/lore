"""Internal agent runtime. One interface, several model providers:

  claude     - Claude Code headless (`claude -p`) with the internal MCP as its only tool server
  anthropic  - direct Anthropic Messages API tool loop over the internal MCP tools (works through ANTHROPIC_BASE_URL gateways)
  codex      - `codex exec` (ChatGPT login in ~/.codex) with the internal MCP as its only tool server
  glm        - Zhipu GLM (Z.ai OpenAI-compatible API, LORE_GLM_API_KEY) running the same tool loop as openai
  openai     - OpenAI chat-completions tool loop over the internal MCP tools
  onprem     - the same loop against any OpenAI-compatible endpoint (vLLM / Ollama serving Qwen or DeepSeek inside
               Singtel), so no customer data leaves the network
  auto       - try LORE_LLM_CHAIN in order (default glm,anthropic,codex when a GLM key is set, else anthropic,codex)

`claude` falls back to LORE_CLAUDE_FALLBACK (same default as the chain) when the CLI fails; the provider actually used is
reported as e.g. "claude->anthropic".
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import anyio

from lore import config

PROVIDERS = ("auto", "claude", "anthropic", "codex", "glm", "openai", "onprem")


@dataclass
class AgentResult:
    answer: str
    provider: str
    elapsed_s: float
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {"answer": self.answer, "provider": self.provider, "elapsed_s": round(self.elapsed_s, 1),
                "tool_calls": self.tool_calls, "error": self.error}


def system_prompt(user: str, scope: str) -> str:
    from lore.governance import principal

    p = principal(user)
    skill = config.SKILL_FILE.read_text() if config.SKILL_FILE.exists() else ""
    return (f"You are Lore's internal agent. Today is {config.TODAY} (Singapore). You act for {p.name} ({p.user_id}, {p.role}), "
            f"who can see {len(p.accounts)} accounts. Tool scope: {scope}. Use only the Lore MCP tools. "
            "Answer in plain English for a non-technical account manager, cite raw/...#Lnn for every fact. "
            "When an outreach gate applies, name it (e.g. `service_recovery_first`) and the ticket or case ID behind it.\n\n" + skill)


def _server_env(user: str, scope: str, actor: str) -> dict:
    env = {k: v for k, v in os.environ.items()}
    env.update({"LORE_USER": user, "LORE_AGENT_SCOPE": scope, "LORE_ACTOR": actor})
    return env


def _mcp_env(user: str, scope: str, actor: str) -> dict:
    """Minimal env for the internal MCP server when a CLI (claude / codex) launches it."""
    return {"LORE_USER": user, "LORE_AGENT_SCOPE": scope, "LORE_ACTOR": actor,
            **{k: v for k, v in os.environ.items() if k.startswith(("LORE_", "SF_", "SALESFORCE_"))
               and k not in ("LORE_USER", "LORE_AGENT_SCOPE", "LORE_ACTOR")}}


def run(task: str, user: str | None = None, scope: str = "read", provider: str | None = None, actor: str | None = None,
        allow_web: bool = False) -> AgentResult:
    user = user or config.DEFAULT_USER
    provider = provider or config.LLM_PROVIDER
    actor = actor or f"agent-for:{user}"
    t0 = time.time()
    if provider == "auto":
        chain = list(config.LLM_CHAIN)
    elif provider == "claude":
        chain = ["claude", *[p for p in config.CLAUDE_FALLBACK if p != "claude"]]
    else:
        chain = [provider]
    tried: list[str] = []
    errors: list[str] = []
    for p in chain:
        res = _run_one(p, task, user, scope, actor, allow_web)
        tried.append(p)
        if res.error is None and res.answer.strip():
            res.provider, res.elapsed_s = "->".join(tried), time.time() - t0
            return res
        errors.append(f"{p}: {res.error or 'empty answer'}")
        # A provider that already called tools may have written to the KB; re-running elsewhere could duplicate writes.
        if res.tool_calls:
            res.provider, res.elapsed_s, res.error = "->".join(tried), time.time() - t0, "; ".join(errors)[-1500:]
            return res
    return AgentResult("", "->".join(tried) or provider, time.time() - t0, error="; ".join(errors)[-1500:])


def _run_one(provider: str, task: str, user: str, scope: str, actor: str, allow_web: bool) -> AgentResult:
    t0 = time.time()
    try:
        if provider == "claude":
            return _run_claude(task, user, scope, actor, allow_web, t0)
        if provider == "anthropic":
            return _run_async(_run_anthropic, task, user, scope, actor, t0)
        if provider == "codex":
            return _run_codex(task, user, scope, actor, t0)
        if provider in ("openai", "onprem", "glm"):
            return _run_async(_run_openai_compatible, task, user, scope, actor, provider, t0)
        raise ValueError(f"unknown provider {provider}; use one of {', '.join(PROVIDERS)}")
    except Exception as exc:  # noqa: BLE001
        return AgentResult("", provider, time.time() - t0, _recent_tool_calls(t0, actor), error=f"{type(exc).__name__}: {exc}"[:800])


def _run_async(fn, *args):
    # The public MCP server calls run() from inside its event loop, where anyio.run() refuses to start.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(fn, *args)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(anyio.run, fn, *args).result()


def _run_claude(task: str, user: str, scope: str, actor: str, allow_web: bool, t0: float) -> AgentResult:
    cfg = {"mcpServers": {"lore": {"command": sys.executable, "args": ["-m", "lore.internal_mcp"],
                                   "env": _mcp_env(user, scope, actor)}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cfg, fh)
        cfg_path = fh.name
    allowed = ["mcp__lore__*"] + (["WebSearch", "WebFetch"] if allow_web else [])
    cmd = ["claude", "-p", task, "--mcp-config", cfg_path, "--strict-mcp-config", "--allowedTools", *allowed,
           "--disallowedTools", "Bash", "Edit", "Write", "Read", "Glob", "Grep", "NotebookEdit", "Task",
           "--append-system-prompt", system_prompt(user, scope), "--output-format", "json", "--max-turns", str(config.AGENT_MAX_STEPS)]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    env = _server_env(user, scope, actor)
    env.setdefault("CLAUDE_CODE_MAX_RETRIES", config.CLAUDE_MAX_RETRIES)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=config.AGENT_TIMEOUT_S, cwd=str(config.ROOT),
                              env=env, stdin=subprocess.DEVNULL)
    finally:
        os.unlink(cfg_path)
    if proc.returncode != 0 and not proc.stdout.strip():
        return AgentResult("", "claude", time.time() - t0, error=(proc.stderr or "claude failed")[-800:])
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return AgentResult(proc.stdout.strip(), "claude", time.time() - t0)
    calls = _recent_tool_calls(t0, actor)
    if out.get("is_error"):
        return AgentResult("", "claude", time.time() - t0, calls,
                           f"claude CLI error ({out.get('terminal_reason') or out.get('subtype')}): {str(out.get('result', ''))[:500]}")
    return AgentResult(out.get("result", ""), "claude", time.time() - t0, calls)


def _recent_tool_calls(since: float, actor: str | None = None) -> list[str]:
    path = config.VAR_DIR / "tool_calls.jsonl"
    if not path.exists():
        return []
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since - 1))
    out = []
    for line in path.read_text().splitlines()[-200:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("at", "") >= cutoff and rec.get("server") == "internal" and (actor is None or rec.get("actor") == actor):
            out.append(rec.get("tool", "?"))
    return out


def _toml_str(value: str) -> str:
    # JSON string escapes are a subset of TOML basic-string escapes.
    return json.dumps(value)


def _run_codex(task: str, user: str, scope: str, actor: str, t0: float) -> AgentResult:
    env_table = "{" + ",".join(f"{_toml_str(k)}={_toml_str(v)}" for k, v in _mcp_env(user, scope, actor).items()) + "}"
    workdir = tempfile.mkdtemp(prefix="lore-codex-")
    last = os.path.join(workdir, "last_message.txt")
    cmd = ["codex", "exec", "--ignore-user-config", "--skip-git-repo-check", "--ephemeral", "--json",
           "-s", "read-only", "-C", workdir, "-m", config.CODEX_MODEL,
           "-c", f"model_provider={_toml_str(config.CODEX_MODEL_PROVIDER)}",
           "-c", f"model_reasoning_effort={_toml_str(config.CODEX_EFFORT)}",
           "-c", f"mcp_servers.lore.command={_toml_str(sys.executable)}",
           "-c", 'mcp_servers.lore.args=["-m","lore.internal_mcp"]',
           "-c", f"mcp_servers.lore.cwd={_toml_str(str(config.ROOT))}",
           "-c", f"mcp_servers.lore.env={env_table}",
           "-c", 'mcp_servers.lore.default_tools_approval_mode="approve"',
           "-c", "mcp_servers.lore.startup_timeout_sec=60",
           "-c", "mcp_servers.lore.tool_timeout_sec=180",
           "-o", last, "-"]
    prompt = (system_prompt(user, scope) + "\n\nDo not run shell commands or read local files; use only the `lore` MCP tools."
              "\n\n# Task\n" + task)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=config.AGENT_TIMEOUT_S,
                          env=_server_env(user, scope, actor))
    calls: list[str] = []
    errors: list[str] = []
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            calls.append(item.get("tool", "?"))
        elif ev.get("type") in ("error", "turn.failed"):
            errors.append(str(ev.get("message") or ev.get("error"))[:300])
    answer = open(last).read().strip() if os.path.exists(last) else ""
    if not answer:
        return AgentResult("", "codex", time.time() - t0, calls,
                           error=("; ".join(errors) or proc.stderr[-500:] or f"codex exited {proc.returncode}")[:800])
    return AgentResult(answer, "codex", time.time() - t0, calls)


def _mcp_params(user: str, scope: str, actor: str):
    from mcp import StdioServerParameters

    return StdioServerParameters(command=sys.executable, args=["-m", "lore.internal_mcp"], env=_server_env(user, scope, actor),
                                 cwd=str(config.ROOT))


def _tool_text(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)[:12000]


async def _run_anthropic(task: str, user: str, scope: str, actor: str, t0: float) -> AgentResult:
    import httpx
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if config.ANTHROPIC_API_KEY:
        headers["x-api-key"] = config.ANTHROPIC_API_KEY
    if config.ANTHROPIC_AUTH_TOKEN:
        headers["authorization"] = f"Bearer {config.ANTHROPIC_AUTH_TOKEN}"
    if "x-api-key" not in headers and "authorization" not in headers:
        raise RuntimeError("no ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN set")
    async with httpx.AsyncClient(base_url=config.ANTHROPIC_BASE_URL, headers=headers,
                                 timeout=httpx.Timeout(120.0, connect=15.0)) as http:
        async with stdio_client(_mcp_params(user, scope, actor)) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await anthropic_tool_loop(session, http, task, system_prompt(user, scope), t0)


async def anthropic_tool_loop(session, http, task: str, system: str, t0: float, model: str | None = None) -> AgentResult:
    """Messages API tool loop. `session` needs list_tools()/call_tool(); `http` is an httpx.AsyncClient at the API base."""
    listed = await session.list_tools()
    tools = [{"name": t.name, "description": (t.description or "")[:1000],
              "input_schema": t.inputSchema or {"type": "object", "properties": {}}} for t in listed.tools]
    messages: list[dict] = [{"role": "user", "content": task}]
    calls: list[str] = []
    for _ in range(config.AGENT_MAX_STEPS):
        resp = await http.post("/v1/messages", json={"model": model or config.ANTHROPIC_MODEL, "max_tokens": config.ANTHROPIC_MAX_TOKENS,
                                                     "system": system, "messages": messages, "tools": tools})
        if resp.status_code >= 400:
            raise RuntimeError(f"Anthropic API HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        uses = [b for b in content if b.get("type") == "tool_use"]
        if data.get("stop_reason") != "tool_use" or not uses:
            return AgentResult(text, "anthropic", time.time() - t0, calls)
        messages.append({"role": "assistant", "content": content})
        results = []
        for u in uses:
            calls.append(u["name"])
            is_error = False
            try:
                res = await session.call_tool(u["name"], u.get("input") or {})
                out = _tool_text(res)
                is_error = bool(getattr(res, "isError", False))
            except Exception as exc:  # noqa: BLE001
                out, is_error = f"tool error: {exc}", True
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": out or "(no output)", "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return AgentResult("Stopped: step limit reached before a final answer.", "anthropic", time.time() - t0, calls)


async def _run_openai_compatible(task: str, user: str, scope: str, actor: str, provider: str, t0: float) -> AgentResult:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from openai import AsyncOpenAI

    extra_body = None
    if provider == "openai":
        client, model = AsyncOpenAI(), config.OPENAI_MODEL
    elif provider == "glm":
        if not config.GLM_API_KEY:
            raise RuntimeError("no LORE_GLM_API_KEY set")
        client, model = AsyncOpenAI(base_url=config.GLM_BASE_URL, api_key=config.GLM_API_KEY, timeout=120.0), config.GLM_MODEL
        if config.GLM_THINKING:
            extra_body = {"thinking": {"type": config.GLM_THINKING}}
    else:
        client, model = AsyncOpenAI(base_url=config.ONPREM_BASE_URL, api_key=config.ONPREM_API_KEY), config.ONPREM_MODEL
    calls: list[str] = []
    async with stdio_client(_mcp_params(user, scope, actor)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:1000],
                                                       "parameters": t.inputSchema}} for t in listed.tools]
            messages = [{"role": "system", "content": system_prompt(user, scope)}, {"role": "user", "content": task}]
            for _ in range(config.AGENT_MAX_STEPS):
                resp = await client.chat.completions.create(model=model, messages=messages, tools=tools, extra_body=extra_body)
                msg = resp.choices[0].message
                if not msg.tool_calls:
                    return AgentResult(msg.content or "", provider, time.time() - t0, calls)
                messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
                for tc in msg.tool_calls:
                    calls.append(tc.function.name)
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                        res = await session.call_tool(tc.function.name, args)
                        content = _tool_text(res)
                    except Exception as exc:  # noqa: BLE001
                        content = f"tool error: {exc}"
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
            return AgentResult("Stopped: step limit reached before a final answer.", provider, time.time() - t0, calls)
