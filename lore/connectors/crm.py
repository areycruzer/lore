"""CRM facade. `get_crm()` returns the offline CSV backend or the live Salesforce Hosted MCP backend.

Both backends return plain dicts keyed with the synthetic export's snake_case column names
(`account_id`, `account_name`, `contact_id`, ...), so callers never care which one is live.
"""
from __future__ import annotations

import csv
from functools import cache, lru_cache
from pathlib import Path
from typing import Protocol

from lore import config


class CRM(Protocol):
    backend: str

    def list_accounts(self) -> list[dict]: ...
    def get_account(self, account_id: str) -> dict | None: ...
    def get_contacts(self, account_id: str) -> list[dict]: ...
    def get_opportunities(self, account_id: str) -> list[dict]: ...
    def get_cases(self, account_id: str) -> list[dict]: ...
    def soql(self, query: str) -> list[dict]: ...


@cache
def load_table(name: str, data_dir: str | None = None) -> tuple[dict, ...]:
    path = Path(data_dir or config.DATA_DIR) / f"{name}.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return tuple(dict(row) for row in csv.DictReader(fh))


def rows_for(table: str, account_id: str) -> list[dict]:
    return [r for r in load_table(table) if r.get("account_id") == account_id]


class CsvCRM:
    """Reads the synthetic Salesforce-shaped export in outputs/synthetic_crm_csv."""

    backend = "csv"

    def list_accounts(self) -> list[dict]:
        return list(load_table("accounts"))

    def get_account(self, account_id: str) -> dict | None:
        return next((r for r in load_table("accounts") if r["account_id"] == account_id), None)

    def get_contacts(self, account_id: str) -> list[dict]:
        return rows_for("contacts", account_id)

    def get_opportunities(self, account_id: str) -> list[dict]:
        return rows_for("opportunities", account_id)

    def get_cases(self, account_id: str) -> list[dict]:
        return rows_for("support_tickets", account_id)

    def soql(self, query: str) -> list[dict]:
        raise RuntimeError("SOQL needs the live Salesforce backend (set LORE_CRM=salesforce).")


@lru_cache(maxsize=1)
def get_crm() -> CRM:
    if config.CRM_BACKEND == "salesforce":
        from lore.connectors.salesforce_crm import SalesforceCRM

        return SalesforceCRM()
    return CsvCRM()
