"""Offline tests for the Salesforce loader and the Hosted MCP adapter (no network, no sf CLI)."""
from __future__ import annotations

import csv
import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from lore import config, sf_load
from lore.connectors import sf_hosted

DATA_DIR = Path(config.DATA_DIR)
pytestmark = pytest.mark.skipif(not (DATA_DIR / "accounts.csv").exists(), reason="synthetic CRM CSVs not present")


def csv_rows(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / f"{name}.csv").open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def plan() -> dict[str, sf_load.Step]:
    return {step.label: step for step in sf_load.build_plan(sf_load.load_tables())}


# ------------------------------------------------------------------------------------------------ loader


def test_picklist_values_are_valid(plan):
    checked = 0
    for step in plan.values():
        for record in step.records:
            for (sobject, field), allowed in sf_load.VALID_PICKLISTS.items():
                if record["attributes"]["type"] == sobject and field in record:
                    assert record[field] in allowed, (sobject, field, record[field])
                    checked += 1
    assert checked > 1000


def test_every_synthetic_enum_has_an_explicit_mapping():
    assert {r["stage"] for r in csv_rows("opportunities")} <= set(sf_load.STAGE)
    tickets = csv_rows("support_tickets")
    assert {r["ticket_status"] for r in tickets} <= set(sf_load.CASE_STATUS)
    assert {r["priority"] for r in tickets} <= set(sf_load.CASE_PRIORITY)
    assert {r["channel"] for r in tickets} <= set(sf_load.CASE_ORIGIN)
    assert {r["account_status"] for r in csv_rows("accounts")} <= set(sf_load.ACCOUNT_TYPE)
    assert {r["activity_status"] for r in csv_rows("activities")} <= set(sf_load.TASK_STATUS)


def test_specific_mappings():
    assert sf_load.STAGE["Solution Design"] == "Value Proposition"
    assert sf_load.CASE_STATUS["pending_customer"] == "Working"
    assert sf_load.CASE_PRIORITY["critical"] == "High"
    assert sf_load.CASE_ORIGIN["portal"] == "Web"
    acc = sf_load.account_record({"account_id": "ACC-1", "account_name": "X", "account_status": "disqualified",
                                  "industry": "Financial Services"})
    assert acc["Type"] == "Other" and acc["Industry"] == "Finance"


def test_external_id_references(plan):
    contact = plan["Contact"].records[0]
    assert contact["Account"] == {"Lore_Id__c": "ACC-0001"}
    assert contact["attributes"] == {"type": "Contact"}
    assert all(set(r) == {"attributes", "Lore_Id__c", "Parent"} for r in plan["Account (parent)"].records)
    assert all(set(r["Parent"]) == {"Lore_Id__c"} for r in plan["Account (parent)"].records)
    assert all(set(r) == {"attributes", "Lore_Id__c", "ReportsTo"} for r in plan["Contact (reports-to)"].records)
    opp = plan["Opportunity"].records[0]
    assert opp["Account"] == {"Lore_Id__c": "ACC-0001"}
    case = plan["Case"].records[0]
    assert case["Account"] == {"Lore_Id__c": "ACC-0001"} and case["Contact"] == {"Lore_Id__c": "CON-0001"}
    task = plan["Task"].records[0]
    assert task["What"] == {"attributes": {"type": "Opportunity"}, "Lore_Id__c": "OPP-0001"}
    assert task["Who"] == {"attributes": {"type": "Contact"}, "Lore_Id__c": "CON-0001"}
    for step in plan.values():
        for record in step.records:
            assert record["Lore_Id__c"] and len(record["Lore_Id__c"]) <= 40


def test_task_without_opportunity_relates_to_account():
    row = {"activity_id": "ACT-1", "account_id": "ACC-0002", "contact_id": "CON-0004", "opportunity_id": "",
           "activity_type": "meeting", "activity_status": "scheduled", "subject": "Demo meeting"}
    rec = sf_load.task_record(row)
    assert rec["What"] == {"attributes": {"type": "Account"}, "Lore_Id__c": "ACC-0002"}
    assert rec["Status"] == "Not Started" and rec["Priority"] == "Normal"
    live = sf_load.task_record(row, {"Account": {"ACC-0002": "001A"}, "Contact": {"CON-0004": "003B"}, "Opportunity": {}})
    assert live["WhatId"] == "001A" and live["WhoId"] == "003B"
    assert "What" not in live and "Who" not in live


def test_contact_mapping():
    row = {"contact_id": "CON-9", "account_id": "ACC-1", "full_name": "Demo Avery Tan 001", "do_not_contact": "true",
           "is_champion": "true", "consent_status": "revoked", "authority": "budget", "influence_level": "economic_buyer",
           "email": "a@b.example"}
    rec = sf_load.contact_record(row)
    assert (rec["FirstName"], rec["LastName"]) == ("Demo Avery", "Tan 001")
    assert rec["Lore_Do_Not_Contact__c"] is True and rec["Lore_Champion__c"] is True
    assert rec["Lore_Consent__c"] == "revoked" and rec["Lore_Authority__c"] == "budget"
    assert sf_load.split_name("Cher") == ("", "Cher")


def test_truncation_respects_field_limits():
    long = "x" * 50_000
    acc = sf_load.account_record({"account_id": "ACC-1", "account_name": long, "pain_points": long, "website": long})
    assert len(acc["Name"]) == 255 and acc["Name"].endswith("…")
    assert len(acc["Website"]) <= 255
    assert len(acc["Description"]) <= 32000
    opp = sf_load.opportunity_record({"opportunity_id": "OPP-1", "opportunity_name": long, "stage": "Proposal"})
    assert len(opp["Name"]) == 120
    case = sf_load.case_record({"ticket_id": "TKT-1", "subject": long, "customer_message": long})
    assert len(case["Subject"]) == 255 and len(case["Description"]) <= 32000
    con = sf_load.contact_record({"contact_id": "CON-1", "full_name": "A " + long, "job_title": long})
    assert len(con["Title"]) <= 128 and len(con["LastName"]) <= 80
    task = sf_load.task_record({"activity_id": "ACT-1", "subject": long, "body_summary": long})
    assert len(task["Subject"]) == 255 and len(task["Description"]) <= 32000


def test_description_is_key_value_lines(plan):
    line_re = re.compile(r"^[a-z_]+: \S.*$")
    for step in plan.values():
        for record in step.records:
            if "Description" in record:
                lines = record["Description"].splitlines()
                assert lines and all(line_re.match(line) for line in lines), step.label
    acc = plan["Account"].records[0]["Description"].splitlines()
    assert acc[0].startswith("pain_points: ") and acc[1].startswith("business_objective: ")
    assert any(line.startswith("strategic_notes: ") for line in acc)
    assert any(line.startswith("next_best_action: ") for line in acc)
    case = plan["Case"].records[1]["Description"]
    for key in ("customer_message", "technical_summary", "root_cause", "signal_reason", "evidence_excerpt"):
        assert f"\n{key}: " in f"\n{case}"
    task = plan["Task"].records[0]["Description"]
    for key in ("body_summary", "outcome", "next_action"):
        assert f"\n{key}: " in f"\n{task}"
    opp = plan["Opportunity"].records[0]["Description"]
    assert "\nstage: Closed Won" in f"\n{opp}"  # original value of a picklist-mapped column survives


def test_description_collapses_newlines_and_skips_empty():
    text = sf_load.format_description([("a_key", "line1\nline2"), ("empty", ""), ("Bad-Key", "x"), ("b", None)])
    assert text == "a_key: line1 line2"


def test_batches_at_most_200(plan):
    assert [len(b) for b in sf_load.batches([{}] * 450)] == [200, 200, 50]
    for step in plan.values():
        assert all(0 < len(b) <= 200 for b in sf_load.batches(step.records))


def test_dry_run_counts_match_csv_and_stay_offline(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network or subprocess used during dry run")

    monkeypatch.setattr(sf_load.httpx, "Client", boom)
    monkeypatch.setattr(sf_load.subprocess, "run", boom)
    lines: list[str] = []
    steps = {s.label: len(s.records) for s in sf_load.dry_run(out=lines.append)}
    activities = csv_rows("activities")
    assert steps["Product2"] == len(csv_rows("products"))
    assert steps["Account"] == len(csv_rows("accounts")) == 100
    assert steps["Account (parent)"] == sum(1 for r in csv_rows("accounts") if r["parent_account_id"])
    assert steps["Contact"] == len(csv_rows("contacts"))
    assert steps["Contact (reports-to)"] == sum(1 for r in csv_rows("contacts") if r["reports_to_contact_id"])
    assert steps["Opportunity"] == len(csv_rows("opportunities"))
    assert steps["Case"] == len(csv_rows("support_tickets"))
    assert steps["Task"] == sum(1 for r in activities if r["activity_type"] in {"meeting", "call", "email"})
    assert steps["Task"] < len(activities)
    assert any("sample Task" in line for line in lines)


def test_main_dry_run_exit_code(capsys):
    assert sf_load.main(["dry-run"]) == 0
    assert "Dry run" in capsys.readouterr().out


def test_not_authorized_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(sf_load, "ensure_sf", lambda: "sf")
    monkeypatch.setattr(sf_load.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout='{"status":1,"message":"No authorization"}', stderr=""))
    assert sf_load.main(["status"]) == 2
    assert "sf org login web --alias lore --instance-url https://login.salesforce.com" in capsys.readouterr().err


def test_rest_session_shape(monkeypatch):
    out = json.dumps({"status": 0, "result": {"accessToken": "TOKEN", "instanceUrl": "https://x.my.salesforce.com/",
                                               "connectedStatus": "Connected", "username": "u@x"}})
    monkeypatch.setattr(sf_load, "ensure_sf", lambda: "sf")
    monkeypatch.setattr(sf_load.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    url, headers = sf_load.rest_session("lore")
    assert url == "https://x.my.salesforce.com"
    assert headers["Authorization"] == "Bearer TOKEN"


def test_upsert_request_shape():
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json=[{"id": "001", "success": True, "created": True, "errors": []}] * len(body["records"]))

    rest = sf_load.SalesforceREST("https://x.my.salesforce.com", {"Authorization": "Bearer t"},
                                  client=httpx.Client(transport=httpx.MockTransport(handler)))
    step = sf_load.Step("Account", "Account", [sf_load.account_record({"account_id": f"ACC-{i}", "account_name": "n"})
                                               for i in range(250)])
    res = rest.upsert(step)
    assert res.succeeded == 250 and res.failed == 0
    assert [len(b["records"]) for _, _, b in seen] == [200, 50]
    method, path, body = seen[0]
    assert method == "PATCH" and path == "/services/data/v62.0/composite/sobjects/Account/Lore_Id__c"
    assert body["allOrNone"] is False and body["records"][0]["attributes"] == {"type": "Account"}


# ------------------------------------------------------------------------------------------ hosted MCP


def test_token_storage_requires_client_id(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SF_CLIENT_ID", "")
    with pytest.raises(sf_hosted.HostedMCPError, match="SF_CLIENT_ID"):
        sf_hosted.FileTokenStorage(token_dir=tmp_path)
    with pytest.raises(sf_hosted.HostedMCPError):
        sf_hosted.FileTokenStorage(client_id="  ", token_dir=tmp_path)


async def test_token_storage_returns_preseeded_client(tmp_path):
    from mcp.client.auth.utils import credentials_match_issuer
    from mcp.shared.auth import OAuthToken

    storage = sf_hosted.FileTokenStorage(client_id="3MVG9_consumer_key", token_dir=tmp_path / "sf", callback_port=8787)
    info = await storage.get_client_info()
    assert info.client_id == "3MVG9_consumer_key"
    assert info.token_endpoint_auth_method == "none" and info.client_secret is None
    assert [str(u) for u in info.redirect_uris] == ["http://localhost:8787/callback"]
    assert set(info.scope.split()) == {"mcp_api", "refresh_token"}
    assert credentials_match_issuer(info, "https://login.salesforce.com", None)  # no issuer binding -> never dropped
    await storage.set_client_info(info.model_copy(update={"client_id": "other"}))
    assert (await storage.get_client_info()).client_id == "3MVG9_consumer_key"

    assert await storage.get_tokens() is None
    await storage.set_tokens(OAuthToken(access_token="a1", refresh_token="r1"))
    assert stat.S_IMODE(os.stat(storage.path).st_mode) == 0o600
    assert (await storage.get_tokens()).refresh_token == "r1"
    await storage.set_tokens(OAuthToken(access_token="a2"))  # refresh response without refresh_token
    tok = await storage.get_tokens()
    assert tok.access_token == "a2" and tok.refresh_token == "r1"
    assert storage.expires_at() is not None

    other = sf_hosted.FileTokenStorage(client_id="different", token_dir=tmp_path / "sf")
    assert await other.get_tokens() is None


async def test_sdk_skips_registration_with_preseeded_client(tmp_path):
    storage = sf_hosted.FileTokenStorage(client_id="ck", token_dir=tmp_path)
    provider = sf_hosted.SalesforceOAuthProvider(storage, interactive=False)
    await provider._initialize()
    assert provider.context.client_info is not None and provider.context.client_info.client_id == "ck"


async def test_provider_restores_metadata_and_expiry(tmp_path):
    from mcp.shared.auth import OAuthMetadata, OAuthToken

    storage = sf_hosted.FileTokenStorage(client_id="ck", token_dir=tmp_path)
    await storage.set_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=10))
    meta = OAuthMetadata(issuer="https://login.salesforce.com",
                         authorization_endpoint="https://login.salesforce.com/services/oauth2/authorize",
                         token_endpoint="https://login.salesforce.com/services/oauth2/token",
                         response_types_supported=["code"])
    storage.save_server_metadata(meta, "https://login.salesforce.com")
    provider = sf_hosted.SalesforceOAuthProvider(storage, interactive=False)
    await provider._initialize()
    assert str(provider.context.oauth_metadata.token_endpoint).endswith("/services/oauth2/token")
    assert provider.context.token_expiry_time is not None
    assert provider.context.can_refresh_token()


def test_jwt_expiry_is_read():
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1900000000}).encode()).decode().rstrip("=")
    assert sf_hosted._jwt_exp(f"h.{payload}.s") == 1900000000
    assert sf_hosted._jwt_exp("00Dxx!opaque") is None


# Shapes copied from the published sobject-all reference.
HOSTED_TOOLS = [
    {"name": "getObjectSchema", "inputSchema": {"type": "object", "properties": {"object-name": {"type": "string"}}}},
    {"name": "soqlQuery", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find", "inputSchema": {"type": "object", "properties": {"search": {"type": "string"}}, "required": ["search"]}},
    {"name": "getUserInfo", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "createSobjectRecord", "inputSchema": {"type": "object", "required": ["sobject-name", "body"], "properties": {
        "sobject-name": {"type": "string"}, "body": {"type": "object"}}}},
    {"name": "updateRelatedRecord", "inputSchema": {"type": "object", "properties": {
        "sobject-name": {"type": "string"}, "id": {"type": "string"}, "relationship-path": {"type": "string"},
        "body": {"type": "object"}}}},
    {"name": "updateSobjectRecord", "inputSchema": {"type": "object", "required": ["sobject-name", "id", "body"], "properties": {
        "sobject-name": {"type": "string"}, "id": {"type": "string"}, "body": {"type": "object"}}}},
    {"name": "deleteSobjectRecord", "inputSchema": {"type": "object", "properties": {
        "sobject-name": {"type": "string"}, "id": {"type": "string"}}}},
]


def test_tool_and_arg_discovery():
    assert sf_hosted.find_tool(HOSTED_TOOLS, "soql")["name"] == "soqlQuery"
    assert sf_hosted.find_tool(HOSTED_TOOLS, "create")["name"] == "createSobjectRecord"
    assert sf_hosted.find_tool(HOSTED_TOOLS, "update")["name"] == "updateSobjectRecord"
    assert sf_hosted.find_tool(HOSTED_TOOLS, "delete")["name"] == "deleteSobjectRecord"
    assert sf_hosted.find_tool(HOSTED_TOOLS, "upsert") is None
    create = sf_hosted.find_tool(HOSTED_TOOLS, "create")["inputSchema"]
    assert sf_hosted.build_args(create, {"sobject": "Account", "fields": {"Name": "A"}}) == {
        "sobject-name": "Account", "body": {"Name": "A"}}
    update = sf_hosted.find_tool(HOSTED_TOOLS, "update")["inputSchema"]
    assert sf_hosted.build_args(update, {"sobject": "Case", "id": "500x", "fields": {"Status": "Closed"}}) == {
        "sobject-name": "Case", "id": "500x", "body": {"Status": "Closed"}}
    assert sf_hosted.build_args(HOSTED_TOOLS[1]["inputSchema"], {"query": "SELECT Id FROM Account"}) == {
        "query": "SELECT Id FROM Account"}


def test_arg_discovery_other_naming_styles():
    schema = {"type": "object", "required": ["objectApiName", "recordFields"], "properties": {
        "objectApiName": {"type": "string"}, "recordFields": {"type": "string"}}}
    args = sf_hosted.build_args({**schema, "properties": {**schema["properties"], "fields": {"type": "string"}}},
                                {"sobject": "Lead", "fields": {"LastName": "X"}})
    assert args == {"objectApiName": "Lead", "fields": json.dumps({"LastName": "X"})}  # string-typed -> JSON text
    lone = {"type": "object", "required": ["stmt"], "properties": {"stmt": {"type": "string"}}}
    assert sf_hosted.find_arg(lone, "query") == "stmt"
    with pytest.raises(sf_hosted.HostedMCPError, match="Cannot map"):
        sf_hosted.build_args({"properties": {"foo": {"type": "number"}}}, {"query": "x"})


def test_extract_records_strips_attributes():
    payload = {"totalSize": 1, "done": True, "records": [
        {"attributes": {"type": "Account"}, "Id": "001", "Owner": {"attributes": {"type": "User"}, "Name": "U"}}]}
    assert sf_hosted.extract_records(payload) == [{"Id": "001", "Owner": {"Name": "U"}}]
    assert sf_hosted.extract_records([{"attributes": {}, "Id": "1"}]) == [{"Id": "1"}]
    assert sf_hosted.extract_records({"result": {"records": []}}) == []


class FakeSession:
    def __init__(self, existing: dict[str, str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.existing = existing or {}

    async def list_tools(self, cursor=None):
        tools = [SimpleNamespace(name=t["name"], description="", inputSchema=t["inputSchema"]) for t in HOSTED_TOOLS]
        return SimpleNamespace(tools=tools, nextCursor=None)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "soqlQuery":
            rows = [{"attributes": {"type": "Account"}, "Id": sid, "Lore_Id__c": lid} for lid, sid in self.existing.items()]
            return SimpleNamespace(isError=False, structuredContent=None,
                                   content=[SimpleNamespace(type="text", text=json.dumps(rows))])
        if name == "createSobjectRecord":
            return SimpleNamespace(isError=False, structuredContent={"id": "001NEW000000001AAA", "success": True}, content=[])
        if name == "updateSobjectRecord" and args["id"] == "bad":
            return SimpleNamespace(isError=True, structuredContent=None,
                                   content=[SimpleNamespace(type="text", text="INVALID_ID: bad id")])
        return SimpleNamespace(isError=False, structuredContent=None,
                               content=[SimpleNamespace(type="text", text=f"Record {args.get('id')} updated")])


async def test_dml_insert_update_delete_via_hosted_tools():
    session = FakeSession()
    out = await sf_hosted._dml(session, "insert", "Account", [{"Name": "A", "attributes": {"type": "Account"}}], None)
    assert out == [{"id": "001NEW000000001AAA", "success": True, "errors": []}]
    assert session.calls[-1] == ("createSobjectRecord", {"sobject-name": "Account", "body": {"Name": "A"}})
    out = await sf_hosted._dml(session, "update", "Account", [{"Id": "001A", "Name": "B"}, {"Id": "bad", "Name": "C"}, {"Name": "no id"}], None)
    assert [o["success"] for o in out] == [True, False, False]
    assert session.calls[-1] == ("updateSobjectRecord", {"sobject-name": "Account", "id": "bad", "body": {"Name": "C"}})
    assert out[2]["errors"] == ["update needs an Id on every record"]
    out = await sf_hosted._dml(session, "delete", "Account", [{"Id": "001A"}], None)
    assert session.calls[-1] == ("deleteSobjectRecord", {"sobject-name": "Account", "id": "001A"}) and out[0]["success"]
    with pytest.raises(sf_hosted.HostedMCPError):
        await sf_hosted._dml(session, "merge", "Account", [{}], None)


async def test_dml_upsert_emulated_with_external_id():
    session = FakeSession(existing={"ACC-0001": "001EXIST"})
    records = [{"Lore_Id__c": "ACC-0001", "Name": "Old"}, {"Lore_Id__c": "ACC-0999", "Name": "New"}]
    out = await sf_hosted._dml(session, "upsert", "Account", records, "Lore_Id__c")
    names = [c[0] for c in session.calls]
    assert names == ["soqlQuery", "updateSobjectRecord", "createSobjectRecord"]
    assert "WHERE Lore_Id__c IN ('ACC-0001', 'ACC-0999')" in session.calls[0][1]["query"]
    assert session.calls[1][1]["id"] == "001EXIST"
    assert [o["success"] for o in out] == [True, True]
