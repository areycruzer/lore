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
