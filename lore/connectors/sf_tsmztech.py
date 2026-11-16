"""Salesforce read/write through the open-source tsmztech/mcp-server-salesforce (MIT) as a child MCP server.

Lore keeps one long-lived child session per process (a background asyncio loop), so each CRM call costs one MCP
round-trip instead of a Node start-up plus Salesforce login. Auth is whatever the child supports; we default to the
Salesforce CLI session (`sf org login web --alias lore` + `sf config set target-org lore --global`).
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CMD = os.environ.get("LORE_SF_MCP_CMD", "npx -y @tsmztech/mcp-server-salesforce")
SF_ENV_KEYS = ("SALESFORCE_CONNECTION_TYPE", "SALESFORCE_USERNAME", "SALESFORCE_PASSWORD", "SALESFORCE_TOKEN",
               "SALESFORCE_INSTANCE_URL", "SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET", "SALESFORCE_ACCESS_TOKEN",
               "SALESFORCE_API_VERSION")


class SalesforceToolError(RuntimeError):
    pass


class _Child:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session: ClientSession | None = None
        self.ready = threading.Event()
        self.err: BaseException | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="sf-mcp-child", daemon=True).start()
        if not self.ready.wait(90):
            raise SalesforceToolError("Salesforce MCP child did not start within 90s")
        if self.err:
            raise SalesforceToolError(f"Salesforce MCP child failed to start: {self.err}")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        env.update({k: os.environ[k] for k in SF_ENV_KEYS if os.environ.get(k)})
        env.setdefault("SALESFORCE_CONNECTION_TYPE", "Salesforce_CLI")
        env.setdefault("SALESFORCE_API_VERSION", "62.0")
        if env["SALESFORCE_CONNECTION_TYPE"] == "Salesforce_CLI":
            # Newer sf CLIs redact the token the child reads from `sf org display`, so hand it over directly.
            from lore.sf_load import org_auth

            auth = org_auth(os.environ.get("LORE_SF_ALIAS", "lore"))
            env.update({"SALESFORCE_CONNECTION_TYPE": "Access_Token", "SALESFORCE_ACCESS_TOKEN": auth.access_token,
                        "SALESFORCE_INSTANCE_URL": auth.instance_url})
        parts = shlex.split(CMD)
        try:
            async with stdio_client(StdioServerParameters(command=parts[0], args=parts[1:], env=env)) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    self.session = s
                    self._stop = asyncio.Event()
                    self.ready.set()
                    await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001
            self.err = exc
            self.ready.set()

    def call(self, name: str, args: dict, timeout: float = 90) -> str:
        assert self.loop and self.session
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(name, args), self.loop)
        res = fut.result(timeout)
        text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        if res.isError or text.startswith("Error"):
            raise SalesforceToolError(text[:800])
        return text


_child: _Child | None = None
_lock = threading.Lock()


def _get() -> _Child:
    global _child
    with _lock:
        if _child is None or _child.err is not None:
            c = _Child()
            c.start()
            _child = c
        return _child


def call_tool(name: str, args: dict) -> str:
    return _get().call(name, args)


def parse_records(text: str, fields: list[str]) -> list[dict]:
    """Parse the child's 'Record N:\n    Field: value' text back into dicts. Multi-line values are re-joined."""
    out = []
    blocks = re.split(r"^Record \d+:\n", text, flags=re.MULTILINE)[1:]
    for b in blocks:
        rec: dict[str, Any] = {}
        current = None
        for line in b.splitlines():
            m = next((f for f in fields if line.startswith(f"    {f}: ")), None)
            if m:
                current = m
                val = line[len(f"    {m}: "):]
                rec[m] = None if val == "null" else val
            elif current and line.strip():
                rec[current] = (rec[current] or "") + "\n" + line
        out.append(rec)
    return out


def query(sobject: str, fields: list[str], where: str | None = None, order_by: str | None = None, limit: int | None = None) -> list[dict]:
    args: dict[str, Any] = {"objectName": sobject, "fields": fields}
    if where:
        args["whereClause"] = where
    if order_by:
        args["orderBy"] = order_by
    if limit:
        args["limit"] = limit
    return parse_records(call_tool("salesforce_query_records", args), fields)


def dml(operation: str, sobject: str, records: list[dict], external_id_field: str | None = None) -> dict:
    args: dict[str, Any] = {"operation": operation, "objectName": sobject, "records": records}
    if external_id_field:
        args["externalIdField"] = external_id_field
    text = call_tool("salesforce_dml_records", args)
    ok = int(re.search(r"Successful: (\d+)", text).group(1)) if "Successful:" in text else 0
    failed = int(re.search(r"Failed: (\d+)", text).group(1)) if "Failed:" in text else 0
    return {"successful": ok, "failed": failed, "detail": text.strip()}


def smoke() -> None:
    rows = query("Account", ["Id", "Name", "Lore_Id__c"], where="Lore_Id__c != null", order_by="Lore_Id__c", limit=3)
    print(f"tsmztech Salesforce MCP OK; sample accounts: {rows}")


if __name__ == "__main__":
    smoke()
