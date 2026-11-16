"""Adapter for the official Salesforce Hosted MCP Server (default server: `sobject-all`).

Salesforce supports only OAuth 2.0 authorization code + PKCE against a pre-created External Client App
(scopes `mcp_api refresh_token`) and offers no dynamic client registration. The token storage below hands
the MCP SDK a pre-seeded public-client registration built from `SF_CLIENT_ID`, so the SDK skips
registration; the authorization server itself is still discovered from the server's protected resource
metadata.

Tool and argument names are discovered from `list_tools()` input schemas at runtime.

CLI:
    uv run python -m lore.connectors.sf_hosted login   # browser sign-in, then prints the user and tool count
    uv run python -m lore.connectors.sf_hosted smoke   # user info + a few Accounts
    uv run python -m lore.connectors.sf_hosted tools   # tool names and argument names
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import html
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

import anyio
import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

try:
    from mcp.client.streamable_http import (
        streamable_http_client as _streamable_http_client,
    )
except ImportError:  # older 1.x releases only ship the (now deprecated) camel-free name
    _streamable_http_client = None
    from mcp.client.streamable_http import (
        streamablehttp_client as _legacy_streamable_http_client,
    )

from lore import config

SCOPES = os.environ.get("SF_SCOPES", "mcp_api refresh_token")
# Salesforce token responses usually omit expires_in; assume a short session unless the JWT says otherwise.
DEFAULT_TOKEN_TTL_S = int(os.environ.get("SF_TOKEN_TTL_S", "3600"))
OAUTH_TIMEOUT_S = 300.0
_SF_ID = re.compile(r"\b([a-zA-Z0-9]{15}|[a-zA-Z0-9]{18})\b")

T = TypeVar("T")


class HostedMCPError(RuntimeError):
    pass


class LoginRequired(HostedMCPError):
    pass


def redirect_uri(port: int | None = None) -> str:
    return f"http://localhost:{port or config.SF_CALLBACK_PORT}/callback"


def _require_client_id(client_id: str | None) -> str:
    client_id = (client_id if client_id is not None else config.SF_CLIENT_ID or "").strip()
    if not client_id:
        raise HostedMCPError(
            "SF_CLIENT_ID is empty. Create an External Client App in Salesforce (callback "
            f"{redirect_uri()}, scopes mcp_api + refresh_token, PKCE, no secret required) and put its "
            "Consumer Key in .env as SF_CLIENT_ID=... (see docs/SALESFORCE_SETUP.md)."
        )
    return client_id


# --------------------------------------------------------------------------------------------------------
# Token storage
# --------------------------------------------------------------------------------------------------------


def _jwt_exp(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return float(payload["exp"]) if "exp" in payload else None
    except (ValueError, KeyError, TypeError):
        return None


class FileTokenStorage(TokenStorage):
    """Tokens in `<SF_TOKEN_DIR>/tokens.json` (mode 600), bound to one client id + server URL.

    `get_client_info` always returns the pre-registered public client, so the SDK never attempts dynamic
    client registration; `set_client_info` is ignored for the same reason.
    """

    def __init__(
        self,
        client_id: str | None = None,
        token_dir: str | Path | None = None,
        server_url: str | None = None,
        callback_port: int | None = None,
    ):
        self.client_id = _require_client_id(client_id)
        self.dir = Path(token_dir or config.SF_TOKEN_DIR).expanduser()
        self.path = self.dir / "tokens.json"
        self.server_url = server_url or config.SF_MCP_URL
        self.redirect_uri = redirect_uri(callback_port)

    # -- file helpers -----------------------------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if data.get("client_id") != self.client_id or data.get("server_url") != self.server_url:
            return {}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        payload = {**data, "client_id": self.client_id, "server_url": self.server_url}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    # -- TokenStorage protocol --------------------------------------------------------------------------
    async def get_tokens(self) -> OAuthToken | None:
        token = self._read().get("token")
        return OAuthToken.model_validate(token) if token else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        previous = data.get("token") or {}
        token = tokens.model_dump(exclude_none=True)
        # A Salesforce refresh response carries no new refresh token; keep the one we have.
        if not token.get("refresh_token") and previous.get("refresh_token"):
            token["refresh_token"] = previous["refresh_token"]
        now = time.time()
        expires_at = _jwt_exp(tokens.access_token) or (now + tokens.expires_in if tokens.expires_in else now + DEFAULT_TOKEN_TTL_S)
        self._write({**data, "token": token, "saved_at": now, "expires_at": expires_at})

    async def get_client_info(self) -> OAuthClientInformationFull:
        return OAuthClientInformationFull(
            client_id=self.client_id,
            redirect_uris=[self.redirect_uri],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPES,
            client_name="Lore",
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        return None

    # -- extras used by the provider --------------------------------------------------------------------
    def expires_at(self) -> float | None:
        value = self._read().get("expires_at")
        return float(value) if value else None

    def save_server_metadata(self, oauth_metadata: OAuthMetadata | None, auth_server_url: str | None) -> None:
        data = self._read()
        if oauth_metadata is not None:
            data["oauth_metadata"] = oauth_metadata.model_dump(mode="json", exclude_none=True)
        if auth_server_url:
            data["auth_server_url"] = auth_server_url
        if data.get("token"):
            self._write(data)

    def server_metadata(self) -> tuple[OAuthMetadata | None, str | None]:
        data = self._read()
        raw = data.get("oauth_metadata")
        try:
            meta = OAuthMetadata.model_validate(raw) if raw else None
        except ValueError:
            meta = None
        return meta, data.get("auth_server_url")


class SalesforceOAuthProvider(OAuthClientProvider):
    """OAuthClientProvider with three Salesforce-specific adjustments (mcp 1.x internals, pinned <2):

    * always requests `SCOPES` (the SDK would otherwise copy the server's advertised scope list);
    * persists the discovered authorization-server metadata, so a new process refreshes against the real
      Salesforce token endpoint instead of `<mcp host>/token`;
    * restores a token expiry on start-up, so an expired access token is refreshed rather than replayed.
    """

    def __init__(self, storage: FileTokenStorage, interactive: bool = True, open_browser: bool = True):
        self.storage = storage
        self._callback = _CallbackServer(storage.redirect_uri)
        self._interactive = interactive
        self._open_browser = open_browser
        super().__init__(
            server_url=storage.server_url,
            client_metadata=OAuthClientMetadata(
                redirect_uris=[storage.redirect_uri],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=SCOPES,
                client_name="Lore",
            ),
            storage=storage,
            redirect_handler=self._redirect,
            callback_handler=self._callback.wait,
            timeout=OAUTH_TIMEOUT_S,
        )

    async def _initialize(self) -> None:
        await super()._initialize()
        meta, auth_server_url = self.storage.server_metadata()
        if meta is not None and self.context.oauth_metadata is None:
            self.context.oauth_metadata = meta
        if auth_server_url and not self.context.auth_server_url:
            self.context.auth_server_url = auth_server_url
        if self.context.current_tokens is not None:
            self.context.token_expiry_time = self.storage.expires_at()

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        self.context.client_metadata.scope = SCOPES
        return await super()._perform_authorization_code_grant()

    async def _handle_token_response(self, response: httpx.Response) -> None:
        await super()._handle_token_response(response)
        self._after_token()

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        previous = self.context.current_tokens
        ok = await super()._handle_refresh_response(response)
        if ok and self.context.current_tokens and not self.context.current_tokens.refresh_token and previous:
            self.context.current_tokens = self.context.current_tokens.model_copy(
                update={"refresh_token": previous.refresh_token}
            )
        if ok:
            self._after_token()
        return ok

    def _after_token(self) -> None:
        if self.context.current_tokens is not None:
            self.context.token_expiry_time = self.storage.expires_at() or self.context.token_expiry_time
        self.storage.save_server_metadata(self.context.oauth_metadata, self.context.auth_server_url)

    async def _redirect(self, url: str) -> None:
        if not self._interactive:
            raise LoginRequired("Salesforce sign-in required. Run: uv run python -m lore.connectors.sf_hosted login")
        self._callback.start()
        print(f"\nOpen this URL to sign in to Salesforce (a browser window should open):\n  {url}\n", flush=True)
        if self._open_browser:
            # Printing the URL above is the fallback when no browser can be opened.
            with contextlib.suppress(Exception):
                webbrowser.open(url)


class _CallbackServer:
    """One-shot HTTP server on 127.0.0.1:<port> that captures `?code=...&state=...` on /callback."""

    def __init__(self, uri: str):
        parsed = urllib.parse.urlparse(uri)
        self.port = parsed.port or 80
        self.path = parsed.path or "/callback"
        self._server: http.server.HTTPServer | None = None
        self._done = threading.Event()
        self._stop = threading.Event()
        self.result: dict[str, str] = {}

    def start(self) -> None:
        if self._server is not None:
            return
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != outer.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                outer.result = params
                ok = "code" in params
                body = (
                    "<h2>Lore: Salesforce sign-in complete.</h2><p>You can close this tab.</p>"
                    if ok
                    else f"<h2>Sign-in failed</h2><p>{html.escape(params.get('error_description') or params.get('error') or 'no code')}</p>"
                )
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())
                outer._done.set()

            def log_message(self, *args: Any) -> None:
                pass

        try:
            self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            raise HostedMCPError(f"Cannot listen on 127.0.0.1:{self.port} for the OAuth callback: {exc}") from exc
        self._server.timeout = 0.5
        self._stop.clear()
        threading.Thread(target=self._serve, args=(self._server,), daemon=True).start()

    def _serve(self, server: http.server.HTTPServer) -> None:
        try:
            while not self._done.is_set() and not self._stop.is_set():
                server.handle_request()
        finally:
            server.server_close()

    def _wait_blocking(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    async def wait(self) -> tuple[str, str | None]:
        if self._server is None:
            self.start()
        try:
            got = await anyio.to_thread.run_sync(self._wait_blocking, OAUTH_TIMEOUT_S, abandon_on_cancel=True)
        finally:
            self._stop.set()
            self._server = None
        if not got:
            raise HostedMCPError(f"Timed out after {OAUTH_TIMEOUT_S:.0f}s waiting for the Salesforce OAuth callback.")
        if "code" not in self.result:
            err = self.result.get("error_description") or self.result.get("error") or "no authorization code"
            raise HostedMCPError(f"Salesforce sign-in failed: {err}")
        self._done.clear()
        return self.result["code"], self.result.get("state")


# --------------------------------------------------------------------------------------------------------
# Session plumbing
# --------------------------------------------------------------------------------------------------------


@asynccontextmanager
async def _session(interactive: bool = True, open_browser: bool = True) -> AsyncIterator[ClientSession]:
    storage = FileTokenStorage()
    if not interactive and await storage.get_tokens() is None:
        raise LoginRequired("No stored Salesforce tokens. Run: uv run python -m lore.connectors.sf_hosted login")
    auth = SalesforceOAuthProvider(storage, interactive=interactive, open_browser=open_browser)
    timeout = httpx.Timeout(30.0, read=300.0)
    # The first request may block on the browser sign-in, so allow the OAuth window plus slack.
    read_timeout = timedelta(seconds=OAUTH_TIMEOUT_S + 60 if interactive else 120)
    if _streamable_http_client is not None:
        async with httpx.AsyncClient(auth=auth, timeout=timeout) as http_client:
            async with _streamable_http_client(storage.server_url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
                    await session.initialize()
                    yield session
    else:  # pragma: no cover - only on old SDKs
        async with _legacy_streamable_http_client(storage.server_url, auth=auth) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=read_timeout) as session:
                await session.initialize()
                yield session


def _run(fn: Callable[[], Awaitable[T]]) -> T:
    """Run an async function from sync code, also when the caller already sits inside an event loop."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(fn)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(anyio.run, fn).result()


def _unwrap_error(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _call(fn: Callable[[ClientSession], Awaitable[T]], interactive: bool = True) -> T:
    async def runner() -> T:
        async with _session(interactive=interactive) as session:
            return await fn(session)

    try:
        return _run(runner)
    except BaseExceptionGroup as group:
        inner = _unwrap_error(group)
        if isinstance(inner, Exception):
            raise inner from group
        raise


# --------------------------------------------------------------------------------------------------------
# Tool + argument discovery (pure functions; unit-tested with fake schemas)
# --------------------------------------------------------------------------------------------------------


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "soql": ("soqlQuery", "soql", "query", "runSoqlQuery", "executeSoql"),
    "user": ("getUserInfo", "userInfo", "whoami", "getCurrentUser"),
    "create": ("createSobjectRecord", "createRecord", "createSobject", "create", "insertRecord"),
    "update": ("updateSobjectRecord", "updateRecord", "updateSobject", "update"),
    "delete": ("deleteSobjectRecord", "deleteRecord", "deleteSobject", "delete"),
    "upsert": ("upsertSobjectRecord", "upsertRecord", "upsertSobject", "upsert"),
}
TOOL_KEYWORDS = {
    "soql": ("soql",),
    "user": ("userinfo",),
    "create": ("create", "insert"),
    "update": ("update",),
    "delete": ("delete",),
    "upsert": ("upsert",),
}

ARG_CANDIDATES: dict[str, tuple[str, ...]] = {
    "query": ("query", "soql", "soqlQuery", "q", "statement"),
    "sobject": ("sobjectName", "sobject", "sObject", "sobjectType", "sObjectType", "objectName", "objectApiName",
                "object", "objectType", "entity", "entityName", "type"),
    "fields": ("fields", "record", "fieldValues", "values", "data", "recordData", "body", "attributes", "payload"),
    "records": ("records", "recordList", "items"),
    "id": ("recordId", "id", "sobjectId", "objectId"),
    "ids": ("recordIds", "ids"),
    "external_id_field": ("externalIdField", "externalIdFieldName", "externalIdFieldApiName", "externalId"),
    "external_id_value": ("externalIdValue", "externalValue"),
}


def find_tool(tools: Sequence[Mapping[str, Any]], role: str) -> Mapping[str, Any] | None:
    """Best tool for a role: exact candidate names first, then a keyword match (shortest name wins)."""
    by_norm = {_norm(t["name"]): t for t in tools}
    for cand in TOOL_CANDIDATES[role]:
        if _norm(cand) in by_norm:
            return by_norm[_norm(cand)]
    hits = [t for t in tools if any(k in _norm(t["name"]) for k in TOOL_KEYWORDS[role])]
    if role in ("create", "update", "delete", "upsert"):
        hits = [t for t in hits if "schema" not in _norm(t["name"]) and "metadata" not in _norm(t["name"])]
        bulk = [t for t in hits if "record" in _norm(t["name"]) or "sobject" in _norm(t["name"])]
        hits = bulk or hits
    return min(hits, key=lambda t: len(t["name"])) if hits else None


def schema_props(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict((schema or {}).get("properties") or {})


def find_arg(schema: Mapping[str, Any] | None, role: str, *, exclude: Sequence[str] = ()) -> str | None:
    """Property name in an inputSchema that plays `role` (query, sobject, fields, id, ...)."""
    props = {k: v for k, v in schema_props(schema).items() if k not in exclude}
    by_norm = {_norm(k): k for k in props}
    for cand in ARG_CANDIDATES[role]:
        if _norm(cand) in by_norm:
            return by_norm[_norm(cand)]
    # Fall back on shape: a lone required property of the expected JSON type.
    want = {"query": "string", "sobject": "string", "id": "string", "fields": "object", "records": "array"}.get(role)
    if want:
        required = [k for k in (schema or {}).get("required") or [] if k in props]
        typed = [k for k in required if props[k].get("type") == want]
        if len(typed) == 1:
            return typed[0]
    return None


def build_args(schema: Mapping[str, Any] | None, values: Mapping[str, Any]) -> dict[str, Any]:
    """Map role -> value onto the tool's real argument names. Raises if a required role has no home."""
    args: dict[str, Any] = {}
    used: list[str] = []
    for role, value in values.items():
        if value is None:
            continue
        name = find_arg(schema, role, exclude=used)
        if name is None:
            props = ", ".join(schema_props(schema)) or "(none)"
            raise HostedMCPError(f"Cannot map {role!r} onto the tool's arguments: {props}")
        prop = schema_props(schema).get(name, {})
        if prop.get("type") == "string" and isinstance(value, (dict, list)):
            value = json.dumps(value)
        args[name] = value
        used.append(name)
    return args


# --------------------------------------------------------------------------------------------------------
# Result parsing
# --------------------------------------------------------------------------------------------------------


def _result_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    texts = [getattr(c, "text", "") for c in getattr(result, "content", None) or [] if getattr(c, "type", "") == "text"]
    joined = "\n".join(t for t in texts if t)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


def _error_text(result: Any) -> str:
    payload = _result_payload(result)
    return payload if isinstance(payload, str) else json.dumps(payload)[:2000]


def strip_attributes(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_attributes(v) for k, v in value.items() if k != "attributes"}
    if isinstance(value, list):
        return [strip_attributes(v) for v in value]
    return value


def extract_records(payload: Any) -> list[dict]:
    """Records from a soqlQuery result in any of the shapes seen in the wild: list, {records}, {result: {records}}."""
    if isinstance(payload, list):
        return [strip_attributes(r) for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            return [strip_attributes(r) for r in payload["records"] if isinstance(r, dict)]
        for key in ("result", "data", "response"):
            if key in payload:
                return extract_records(payload[key])
        if "totalSize" in payload:
            return []
        return [strip_attributes(payload)]
    if isinstance(payload, str) and payload.strip():
        raise HostedMCPError(f"Unexpected soqlQuery result: {payload[:500]}")
    return []


def _dml_outcome(result: Any, fallback_id: str | None = None) -> dict[str, Any]:
    if getattr(result, "isError", False):
        return {"id": fallback_id, "success": False, "errors": [_error_text(result)]}
    payload = _result_payload(result)
    if isinstance(payload, dict):
        body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        success = body.get("success", True)
        errors = body.get("errors") or []
        return {"id": body.get("id") or body.get("Id") or fallback_id, "success": bool(success) and not errors,
                "errors": [e if isinstance(e, str) else e.get("message", json.dumps(e)) for e in errors]}
    text = payload if isinstance(payload, str) else json.dumps(payload)
    if re.search(r"\b(error|failed|invalid)\b", text, re.IGNORECASE):
        return {"id": fallback_id, "success": False, "errors": [text[:2000]]}
    match = _SF_ID.search(text)
    return {"id": match.group(1) if match else fallback_id, "success": True, "errors": []}


# --------------------------------------------------------------------------------------------------------
# Async operations
# --------------------------------------------------------------------------------------------------------


async def _tools(session: ClientSession) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
        out.extend({"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}} for t in page.tools)
        cursor = getattr(page, "nextCursor", None)
        if not cursor:
            return out


def _require_tool(tools: Sequence[Mapping[str, Any]], role: str) -> Mapping[str, Any]:
    tool = find_tool(tools, role)
    if tool is None:
        names = ", ".join(t["name"] for t in tools)
        raise HostedMCPError(f"The Salesforce MCP server exposes no {role!r} tool. Available: {names}")
    return tool


async def _soql(session: ClientSession, query: str, tools: Sequence[Mapping[str, Any]] | None = None) -> list[dict]:
    tools = tools or await _tools(session)
    tool = _require_tool(tools, "soql")
    result = await session.call_tool(tool["name"], build_args(tool["inputSchema"], {"query": query}))
    if result.isError:
        raise HostedMCPError(f"{tool['name']} failed: {_error_text(result)}")
    return extract_records(_result_payload(result))


async def _call_record_tool(session: ClientSession, tool: Mapping[str, Any], values: Mapping[str, Any]) -> Any:
    return await session.call_tool(tool["name"], build_args(tool["inputSchema"], values))


def _record_id(record: Mapping[str, Any]) -> str | None:
    return record.get("Id") or record.get("id")


def _fields_only(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in ("Id", "id", "attributes")}


def _soql_literal(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


async def _dml(
    session: ClientSession,
    operation: str,
    sobject: str,
    records: list[dict],
    external_id_field: str | None,
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict]:
    tools = tools or await _tools(session)
    op = operation.lower()
    if op not in ("insert", "update", "upsert", "delete"):
        raise HostedMCPError(f"Unsupported operation {operation!r}; use insert, update, upsert or delete.")

    if op == "upsert":
        upsert_tool = find_tool(tools, "upsert")
        if upsert_tool is not None and external_id_field:
            out = []
            for rec in records:
                values = {"sobject": sobject, "external_id_field": external_id_field,
                          "external_id_value": rec.get(external_id_field), "fields": _fields_only(rec)}
                if find_arg(upsert_tool["inputSchema"], "external_id_value") is None:
                    values.pop("external_id_value")
                out.append(_dml_outcome(await _call_record_tool(session, upsert_tool, values)))
            return out
        # Emulate: match on the external id (or Id) and split into updates and inserts.
        existing: dict[str, str] = {}
        if external_id_field:
            keys = sorted({str(r[external_id_field]) for r in records if r.get(external_id_field)})
            for start in range(0, len(keys), 100):
                chunk = ", ".join(_soql_literal(k) for k in keys[start : start + 100])
                rows = await _soql(session, f"SELECT Id, {external_id_field} FROM {sobject} WHERE {external_id_field} IN ({chunk})", tools)
                existing.update({str(r[external_id_field]): r["Id"] for r in rows if r.get(external_id_field)})
        out = []
        for rec in records:
            rid = _record_id(rec) or (existing.get(str(rec.get(external_id_field))) if external_id_field else None)
            sub = await _dml(session, "update" if rid else "insert", sobject, [{**rec, "Id": rid} if rid else rec], None, tools)
            out.extend(sub)
        return out

    tool = _require_tool(tools, {"insert": "create"}.get(op, op))
    schema = tool["inputSchema"]
    bulk_arg = find_arg(schema, "records")
    if bulk_arg and op != "delete":
        values = {"sobject": sobject, "records": [_fields_only(r) if op == "insert" else dict(r) for r in records]}
        result = await _call_record_tool(session, tool, values)
        payload = None if result.isError else _result_payload(result)
        if isinstance(payload, list) and len(payload) == len(records):
            return [{"id": p.get("id"), "success": bool(p.get("success", True)), "errors": p.get("errors") or []}
                    for p in payload if isinstance(p, dict)]
        outcome = _dml_outcome(result)
        return [dict(outcome) for _ in records]

    out = []
    for rec in records:
        rid = _record_id(rec)
        if op in ("update", "delete") and not rid:
            out.append({"id": None, "success": False, "errors": [f"{op} needs an Id on every record"]})
            continue
        if op == "insert":
            values = {"sobject": sobject, "fields": _fields_only(rec)}
        elif op == "update":
            values = {"sobject": sobject, "id": rid, "fields": _fields_only(rec)}
        else:
            values = {"sobject": sobject, "id": rid}
        if find_arg(schema, "sobject") is None:
            values.pop("sobject")
        try:
            result = await _call_record_tool(session, tool, values)
        except HostedMCPError as exc:
            out.append({"id": rid, "success": False, "errors": [str(exc)]})
            continue
        out.append(_dml_outcome(result, rid))
    return out


# --------------------------------------------------------------------------------------------------------
# Public sync API
# --------------------------------------------------------------------------------------------------------


def list_tools(*, interactive: bool = True) -> list[dict]:
    """[{name, description, inputSchema}] for every tool on the configured server."""
    return _call(_tools, interactive)


def soql(query: str, *, interactive: bool = True) -> list[dict]:
    """Run SOQL through the server's soqlQuery tool; returns records without `attributes`."""
    return _call(lambda s: _soql(s, query), interactive)


def dml(operation: str, sobject: str, records: list[dict], external_id_field: str | None = None,
        *, interactive: bool = True) -> list[dict]:
    """insert/update/upsert/delete via the server's create/update/delete tools. Returns [{id, success, errors}]."""
    if not records:
        return []
    return _call(lambda s: _dml(s, operation, sobject, list(records), external_id_field), interactive)


async def _user_info(session: ClientSession, tools: Sequence[Mapping[str, Any]]) -> Any:
    tool = find_tool(tools, "user")
    if tool is None:
        return None
    result = await session.call_tool(tool["name"], {})
    return _error_text(result) if result.isError else _result_payload(result)


def login() -> dict[str, Any]:
    """Interactive browser sign-in (skipped when stored tokens still work); prints the user and tool count."""
    async def run(session: ClientSession) -> dict[str, Any]:
        tools = await _tools(session)
        return {"user": await _user_info(session, tools), "tools": [t["name"] for t in tools]}

    info = _call(run, interactive=True)
    print(f"Signed in. Tokens: {FileTokenStorage().path}")
    print(f"User: {json.dumps(info['user'], indent=2, default=str) if not isinstance(info['user'], str) else info['user']}")
    print(f"{len(info['tools'])} tools: {', '.join(info['tools'])}")
    return info


def smoke() -> dict[str, Any]:
    """User info, Lore account count and three accounts."""
    async def run(session: ClientSession) -> dict[str, Any]:
        tools = await _tools(session)
        out: dict[str, Any] = {"server": config.SF_MCP_URL, "tools": len(tools), "user": await _user_info(session, tools)}
        out["accounts"] = await _soql(session, "SELECT Id, Name, Lore_Id__c FROM Account ORDER BY Name LIMIT 3", tools)
        try:
            out["lore_accounts"] = len(await _soql(session, "SELECT Id FROM Account WHERE Lore_Id__c != null LIMIT 2000", tools))
        except HostedMCPError as exc:
            out["lore_accounts"] = f"unavailable ({exc})"
        return out

    info = _call(run, interactive=True)
    print(json.dumps(info, indent=2, default=str))
    return info


def _print_tools(tools: Sequence[Mapping[str, Any]]) -> None:
    for tool in tools:
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required") or [])
        args = ", ".join(f"{k}{'*' if k in required else ''}:{v.get('type', '?')}" for k, v in schema_props(schema).items())
        print(f"- {tool['name']}({args})")
        if tool.get("description"):
            print(f"    {tool['description'].splitlines()[0][:160]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lore.connectors.sf_hosted",
                                     description="Salesforce Hosted MCP Server client for Lore.")
    parser.add_argument("command", choices=("login", "smoke", "tools", "logout"))
    args = parser.parse_args(argv)
    try:
        if args.command == "logout":
            FileTokenStorage().clear()
            print("Stored Salesforce tokens removed.")
        elif args.command == "login":
            login()
        elif args.command == "smoke":
            smoke()
        else:
            _print_tools(list_tools())
        return 0
    except HostedMCPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
