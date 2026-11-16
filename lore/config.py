"""Runtime configuration. Everything is overridable through environment variables or a `.env` file."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


KB_DIR = _path("LORE_KB_DIR", ROOT / "knowledge-base")
DATA_DIR = _path("LORE_DATA_DIR", ROOT.parent / "outputs" / "synthetic_crm_csv")
OUTBOX_DIR = _path("LORE_OUTBOX", ROOT.parent / "outputs" / "lore-actions")
VAR_DIR = _path("LORE_VAR_DIR", ROOT / "var")
SEED_DIR = ROOT / "seed"
SKILL_FILE = ROOT / "skills" / "lore-wiki" / "SKILL.md"
PLAYBOOK_DIR = ROOT / "playbook"

# Identity of the human (or job) the request is made for. Governs which accounts are visible.
DEFAULT_USER = os.environ.get("LORE_USER", "OWN-002")

# CRM backend: "csv" (offline synthetic export) or "salesforce" (Salesforce Hosted MCP Server).
CRM_BACKEND = os.environ.get("LORE_CRM", "csv")
SF_MCP_URL = os.environ.get("SF_MCP_URL", "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all")
SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID", "")
SF_CALLBACK_PORT = int(os.environ.get("SF_CALLBACK_PORT", "8787"))
SF_TOKEN_DIR = _path("SF_TOKEN_DIR", Path.home() / ".lore" / "salesforce")

# Agent model provider: "auto", "claude" (Claude Code headless), "anthropic", "codex", "glm", "openai", or "onprem" (OpenAI-compatible endpoint,
# e.g. a Qwen / DeepSeek model served on Singtel infrastructure through vLLM or Ollama).
# "anthropic" calls the Messages API directly (ANTHROPIC_BASE_URL may point at a gateway); "codex" runs `codex exec`
# with the internal MCP; "auto" tries LORE_LLM_CHAIN in order and keeps the first provider that answers.
LLM_PROVIDER = os.environ.get("LORE_LLM", "auto")
# Zhipu GLM through Z.ai's OpenAI-compatible API. Coding-plan keys only work on the /api/coding/ endpoint.
GLM_API_KEY = os.environ.get("LORE_GLM_API_KEY", "")
GLM_BASE_URL = os.environ.get("LORE_GLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
GLM_MODEL = os.environ.get("LORE_GLM_MODEL", "glm-5.3")
GLM_THINKING = os.environ.get("LORE_GLM_THINKING", "disabled")  # "enabled", "disabled", or empty for the model default
_DEFAULT_CHAIN = "glm,anthropic,codex" if GLM_API_KEY else "anthropic,codex"
LLM_CHAIN = [p.strip() for p in os.environ.get("LORE_LLM_CHAIN", _DEFAULT_CHAIN).split(",") if p.strip()]
CLAUDE_MODEL = os.environ.get("LORE_CLAUDE_MODEL", "")
# Claude Code retries API errors (including 401) about 11 times with backoff, which looks like a hang.
CLAUDE_MAX_RETRIES = os.environ.get("LORE_CLAUDE_MAX_RETRIES", "2")
CLAUDE_FALLBACK = [p.strip() for p in os.environ.get("LORE_CLAUDE_FALLBACK", _DEFAULT_CHAIN).split(",") if p.strip()]
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_MODEL = (os.environ.get("LORE_ANTHROPIC_MODEL") or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                   or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-5")
ANTHROPIC_MAX_TOKENS = int(os.environ.get("LORE_ANTHROPIC_MAX_TOKENS", "4096"))
# Codex with a ChatGPT login (~/.codex/auth.json) only accepts some models; gpt-5.6-terra is verified to work.
CODEX_MODEL = os.environ.get("LORE_CODEX_MODEL", "gpt-5.6-terra")
CODEX_MODEL_PROVIDER = os.environ.get("LORE_CODEX_PROVIDER", "openai")
CODEX_EFFORT = os.environ.get("LORE_CODEX_EFFORT", "low")
OPENAI_MODEL = os.environ.get("LORE_OPENAI_MODEL", "gpt-4.1")
ONPREM_BASE_URL = os.environ.get("LORE_ONPREM_BASE_URL", "http://localhost:11434/v1")
ONPREM_MODEL = os.environ.get("LORE_ONPREM_MODEL", "qwen2.5:14b")
ONPREM_API_KEY = os.environ.get("LORE_ONPREM_API_KEY", "onprem")
AGENT_MAX_STEPS = int(os.environ.get("LORE_AGENT_MAX_STEPS", "16"))
AGENT_TIMEOUT_S = int(os.environ.get("LORE_AGENT_TIMEOUT_S", "240"))

# Public MCP HTTP transport.
PUBLIC_HOST = os.environ.get("LORE_PUBLIC_HOST", "127.0.0.1")
PUBLIC_PORT = int(os.environ.get("LORE_PUBLIC_PORT", "8848"))
# Extra Host headers to accept, e.g. a tunnel hostname (comma separated); "*" accepts any host, for tunnels whose
# hostname rotates. The MCP SDK rejects non-localhost hosts otherwise.
PUBLIC_ALLOWED_HOSTS = [h.strip() for h in os.environ.get("LORE_PUBLIC_ALLOWED_HOSTS", "").split(",") if h.strip()]

# "Today" for the demo dataset (snapshot date of the synthetic CRM export).
TODAY = os.environ.get("LORE_TODAY", "2026-11-16")
