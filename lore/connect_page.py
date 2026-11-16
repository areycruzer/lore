"""Browser landing page for the public MCP URL: one place to connect ChatGPT, Claude, Cursor or VS Code to Lore.

ChatGPT has no link that pre-fills its "Add custom MCP server" form, so its button copies the URL and opens the
Plugins page. Claude, Cursor and VS Code have documented install links.
"""
from __future__ import annotations

import base64
import html
import json
from urllib.parse import quote

CHATGPT_PLUGINS = "https://chatgpt.com/plugins"
CHATGPT_DEV_MODE = "https://chatgpt.com/settings/security"


def links(mcp_url: str, name: str = "Lore") -> dict[str, str]:
    cursor_cfg = base64.b64encode(json.dumps({"url": mcp_url}).encode()).decode()
    return {
        "claude": "https://claude.ai/customize/connectors?modal=add-custom-connector"
                  f"&connectorName={quote(name)}&connectorUrl={quote(mcp_url, safe='')}",
        "cursor": f"cursor://anysphere.cursor-deeplink/mcp/install?name={quote(name)}&config={quote(cursor_cfg, safe='')}",
        "vscode": "vscode:mcp/install?" + quote(json.dumps({"name": name.lower(), "type": "http", "url": mcp_url}), safe=""),
    }


def qr_svg(data: str) -> str:
    try:
        import segno
    except ImportError:
        return ""
    return segno.make(data, error="m").svg_inline(scale=4, dark="#111", light="#fff")


def base_url(headers, fallback_host: str) -> str:
    host = headers.get("x-forwarded-host") or headers.get("host") or fallback_host
    local = host.split(":")[0] in ("127.0.0.1", "localhost", "[::1]")
    proto = headers.get("x-forwarded-proto") or ("http" if local else "https")
    return f"{proto}://{host}"


def render(mcp_url: str, page_url: str, user_label: str, tools: int, crm: str, agent: str) -> str:
    ln = links(mcp_url)
    e = html.escape
    examples = [
        "What should I know before my next call with ACC-0001?",
        "ACC-0001 asked Ops about 24x7 security monitoring. Is that a sales opportunity, and can I pitch it now?",
        "Who is the champion at ACC-0001 and what did they last commit to?",
    ]
    ex_html = "".join(f'<li><span>{e(x)}</span><button class="mini" data-copy="{e(x)}">Copy</button></li>' for x in examples)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Lore</title>
<style>
:root{{--red:#d0021b;--ink:#111;--mut:#5b6170;--bg:#f6f7f9;--card:#fff;--line:#e3e6ea}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}}
main{{max-width:760px;margin:0 auto;padding:40px 20px 64px}}
h1{{font-size:30px;margin:0 0 4px}}h2{{font-size:18px;margin:0 0 12px}}.mut{{color:var(--mut)}}
.status{{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 26px}}.pill{{background:#fff;border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:13px}}
.pill b{{color:#0a7d32}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin:0 0 16px}}
.big{{display:inline-flex;align-items:center;gap:10px;background:var(--ink);color:#fff;border:0;border-radius:10px;padding:14px 22px;font-size:17px;font-weight:600;cursor:pointer;text-decoration:none}}
.big:hover{{background:#333}}.big.red{{background:var(--red)}}.big.red:hover{{background:#a80016}}
.row{{display:flex;flex-wrap:wrap;gap:10px}}.alt{{background:#fff;color:var(--ink);border:1px solid var(--line);font-size:15px;padding:11px 16px}}.alt:hover{{background:#f0f1f3}}
.url{{display:flex;gap:8px;margin:12px 0 0}}.url input{{flex:1;font:13px ui-monospace,Menlo,monospace;padding:10px;border:1px solid var(--line);border-radius:8px;background:#fafbfc}}
.mini{{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}}
ol.steps{{margin:16px 0 0;padding-left:22px}}ol.steps li{{margin:6px 0}}.steps code{{background:#f0f1f3;padding:1px 6px;border-radius:5px}}
.done{{display:none;margin-top:14px;padding:14px 16px;border-radius:10px;background:#eef8f1;border:1px solid #bfe3cb}}
ul.ex{{list-style:none;padding:0;margin:0}}ul.ex li{{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}}
ul.ex li:first-child{{border-top:0}}.qr{{display:flex;gap:18px;align-items:center}}.qr svg{{width:132px;height:132px;flex:none}}
.toast{{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#111;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:.2s}}
.toast.on{{opacity:1}}
</style></head><body><main>
<h1>Connect Lore</h1>
<div class="mut">Cited customer knowledge from CRM, meetings, email and Ops reports, inside your AI assistant.</div>
<div class="status">
  <span class="pill"><b>&#9679;</b> Server online</span>
  <span class="pill">Signed in as {e(user_label)}</span>
  <span class="pill">{tools} tools</span>
  <span class="pill">CRM: {e(crm)}</span>
  <span class="pill">Agent: {e(agent)}</span>
</div>

<section class="card">
  <h2>ChatGPT</h2>
  <button class="big" id="gpt">Connect to ChatGPT</button>
  <div class="url"><input id="u" readonly value="{e(mcp_url)}"><button class="mini" data-copy="{e(mcp_url)}">Copy URL</button></div>
  <div class="done" id="gptsteps"><b>URL copied. In the ChatGPT tab that just opened:</b>
  <ol class="steps">
    <li>Click <b>+</b> &rarr; <b>Add custom MCP server</b>. Name it <code>Lore</code>.</li>
    <li>Paste the URL (&#8984;V / Ctrl+V). Authentication: <b>No authentication</b>.</li>
    <li>Tick the risk notice, click <b>Create as a plugin</b>, then <b>Install</b>.</li>
    <li>In a new chat, type <code>@Lore</code> and ask a question below.</li>
  </ol>
  <div class="mut" style="margin-top:8px;font-size:14px">No "Add custom MCP server" option? On Plus or Pro, turn on
  <b>Developer mode</b> at <a href="{CHATGPT_DEV_MODE}" target="_blank" rel="noopener">Settings &rarr; Security</a>, then reload Plugins.</div></div>
</section>

<section class="card">
  <h2>Other assistants (one click)</h2>
  <div class="row">
    <a class="big red" href="{e(ln['claude'])}" target="_blank" rel="noopener">Add to Claude</a>
    <a class="big alt" href="{e(ln['cursor'])}">Add to Cursor</a>
    <a class="big alt" href="{e(ln['vscode'])}">Add to VS Code</a>
  </div>
</section>

<section class="card">
  <h2>Try asking</h2>
  <ul class="ex">{ex_html}</ul>
</section>

<section class="card qr">
  {qr_svg(page_url)}
  <div><h2>Open on another device</h2><div class="mut">Scan to open this page. Change <code>?user=</code> in the address to connect as a
  different Lore user; access is limited to that user's accounts.</div></div>
</section>
</main><div class="toast" id="t">Copied</div>
<script>
const MCP_URL = {json.dumps(mcp_url)};
function toast(m){{const t=document.getElementById('t');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1600)}}
async function copy(s){{try{{await navigator.clipboard.writeText(s);return true}}catch(e){{const i=document.createElement('textarea');i.value=s;document.body.appendChild(i);i.select();const ok=document.execCommand('copy');i.remove();return ok}}}}
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{{if(await copy(b.dataset.copy))toast('Copied')}}));
document.getElementById('gpt').addEventListener('click',async()=>{{await copy(MCP_URL);document.getElementById('gptsteps').style.display='block';window.open({json.dumps(CHATGPT_PLUGINS)},'_blank','noopener')}});
</script></body></html>"""
