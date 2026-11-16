import base64
import json
from urllib.parse import parse_qs, unquote, urlparse

from lore import connect_page

URL = "https://abc.app.lhr.life/mcp?user=OWN-002"


def test_install_links_encode_the_mcp_url():
    ln = connect_page.links(URL)
    q = parse_qs(urlparse(ln["claude"]).query)
    assert q["modal"] == ["add-custom-connector"] and q["connectorName"] == ["Lore"] and q["connectorUrl"] == [URL]
    cfg = parse_qs(urlparse(ln["cursor"]).query)["config"][0]
    assert json.loads(base64.b64decode(cfg)) == {"url": URL}
    assert json.loads(unquote(ln["vscode"].split("?", 1)[1])) == {"name": "lore", "type": "http", "url": URL}


def test_base_url_prefers_forwarded_headers():
    assert connect_page.base_url({"host": "abc.app.lhr.life"}, "x") == "https://abc.app.lhr.life"
    assert connect_page.base_url({"host": "127.0.0.1:8848"}, "x") == "http://127.0.0.1:8848"
    assert connect_page.base_url({"host": "h", "x-forwarded-proto": "http", "x-forwarded-host": "f"}, "x") == "http://f"


def test_render_escapes_and_embeds_url():
    page = connect_page.render(URL, "https://abc.app.lhr.life/?user=OWN-002", "A <b> (am, 3 accounts)", 25, "salesforce", "glm")
    assert "Connect to ChatGPT" in page and "Add to Claude" in page
    assert "A &lt;b&gt;" in page and "<b> (am" not in page
    assert json.dumps(URL) in page


def test_render_walks_through_chatgpt_form_and_picker():
    users = [("OWN-002", "Demo AM (account manager, 3 accounts)"), ("OWN-009", "Other <AM>")]
    page = connect_page.render(URL, "https://abc.app.lhr.life/?user=OWN-002", "Demo AM", 25, "salesforce", "glm",
                               users=users, user_id="OWN-002", expires_at=1_800_000_000)
    assert f'data-copy="{connect_page.PLUGIN_NAME}"' in page and "No authentication" in page
    assert '<option value="OWN-002" selected>' in page and "Other &lt;AM&gt;" in page
    assert "const EXPIRES_AT = 1800000000" in page and "can't reach this link" not in page
    assert all(connect_page.html.escape(x) in page for x in connect_page.EXAMPLES)


def test_render_warns_when_only_a_local_link_exists():
    page = connect_page.render("http://127.0.0.1:8848/mcp?user=OWN-002", "http://127.0.0.1:8848/?user=OWN-002",
                               "Demo AM", 25, "csv", "glm", reachable=False)
    assert "can't reach this link" in page and "const REACHABLE = false" in page


def _client(tmp_path, monkeypatch, public=None):
    from starlette.testclient import TestClient

    from lore import public_mcp

    state = tmp_path / "public_url.json"
    if public is not None:
        state.write_text(json.dumps(public))
    monkeypatch.setattr(public_mcp, "PUBLIC_URL_FILE", state)
    return TestClient(public_mcp.http_app(), base_url="http://127.0.0.1:8848")


def test_status_routes_without_public_link(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/healthz").json() == {"ok": True}
    s = c.get("/api/status").json()
    assert s["ok"] and s["tools"] > 0 and s["public_url"] is None and s["public_ok"] is None
    page = c.get("/?user=OWN-002", headers={"accept": "text/html"}).text
    assert "can't reach this link" in page and "http://127.0.0.1:8848/mcp?user=OWN-002" in page


def test_local_page_shows_public_link(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch, {"url": "https://abc.free.pinggy.net", "expires_at": 1_800_000_000})
    page = c.get("/?user=OWN-002", headers={"accept": "text/html"}).text
    assert json.dumps("https://abc.free.pinggy.net/mcp?user=OWN-002") in page
    assert "can't reach this link" not in page and "const EXPIRES_AT = 1800000000" in page
