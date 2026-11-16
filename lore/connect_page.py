"""Browser landing page for Lore's MCP server: connect ChatGPT, Claude, Cursor or VS Code without technical steps.

ChatGPT has no link that pre-fills its "Add custom MCP server" form, so the page walks through that form field by
field with a Copy button for each value. Claude, Cursor and VS Code have documented install links.
The page polls `/api/status` so a non-technical user sees whether Lore and its public link are working.
"""
from __future__ import annotations

import base64
import html
import json
from urllib.parse import quote

CHATGPT_PLUGINS = "https://chatgpt.com/plugins"
CHATGPT_DEV_MODE = "https://chatgpt.com/settings/security"
PLUGIN_NAME = "Lore"
PLUGIN_DESCRIPTION = "Singtel customer knowledge: CRM, meetings, email and Ops reports, with sources"

EXAMPLES = [
    "Give me a CRM snapshot of ACC-0001: owner, contacts, opportunities and support cases.",
    "What are the stage, amount, probability, close date and next step on OPP-0002?",
    "Who are the contacts at ACC-0001, who do they report to, and can I contact them?",
    "Is anything open at ACC-0001 that should stop me pitching an upsell right now?",
    "ACC-0001 asked Ops about 24x7 security monitoring. Is that a sales opportunity?",
]


def links(mcp_url: str, name: str = PLUGIN_NAME) -> dict[str, str]:
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


def is_local(host: str) -> bool:
    return host.split(":")[0] in ("127.0.0.1", "localhost", "[::1]")


def base_url(headers, fallback_host: str) -> str:
    host = headers.get("x-forwarded-host") or headers.get("host") or fallback_host
    proto = headers.get("x-forwarded-proto") or ("http" if is_local(host) else "https")
    return f"{proto}://{host}"


def _field(label: str, value: str, hint: str = "") -> str:
    e = html.escape
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    return (f'<li><div class="lbl">{label}</div><div class="val"><code>{e(value)}</code>'
            f'<button class="mini" data-copy="{e(value)}">Copy</button></div>{hint_html}</li>')


def render(mcp_url: str, page_url: str, user_label: str, tools: int, crm: str, agent: str, *,
           users: list[tuple[str, str]] | None = None, user_id: str = "", reachable: bool = True,
           expires_at: float | None = None) -> str:
    """`reachable` is False when the only known address is this computer's, which ChatGPT cannot reach."""
    ln = links(mcp_url)
    e = html.escape
    ex_html = "".join(f'<li><span>{e(x)}</span><button class="mini" data-copy="{e(x)}">Copy</button></li>' for x in EXAMPLES)
    opts = "".join(f'<option value="{e(uid)}"{" selected" if uid == user_id else ""}>{e(label)}</option>'
                   for uid, label in (users or []))
    picker = (f'<label class="who">Who are you? <select id="who">{opts}</select></label>'
              '<div class="mut small">Lore only shows the accounts this person is allowed to see.</div>') if opts else ""
    local_warn = "" if reachable else (
        '<div class="warn"><b>ChatGPT can\'t reach this link yet.</b> It only works on this computer. Close this page and '
        'double-click <b>Start Lore</b> in the Lore folder; it opens this page again with a public link.</div>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Lore</title>
<style>
:root{{--red:#d0021b;--ink:#111;--mut:#5b6170;--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--ok:#0a7d32;--bad:#b42318;--amber:#9a6700}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg)}}
main{{max-width:780px;margin:0 auto;padding:36px 20px 64px}}
h1{{font-size:30px;margin:0 0 4px}}h2{{font-size:19px;margin:0 0 6px;display:flex;align-items:center;gap:10px}}.mut{{color:var(--mut)}}.small{{font-size:14px}}
.num{{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:var(--red);color:#fff;font-size:15px;flex:none}}
.status{{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 20px}}.pill{{background:#fff;border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:14px}}
.pill i{{font-style:normal;margin-right:6px}}.pill.ok i{{color:var(--ok)}}.pill.bad{{border-color:#f1b5b0;background:#fff4f3}}.pill.bad i{{color:var(--bad)}}
.pill.warn{{border-color:#f0d58c;background:#fffaeb}}.pill.warn i{{color:var(--amber)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin:0 0 16px}}
.big{{display:inline-flex;align-items:center;gap:10px;background:var(--ink);color:#fff;border:0;border-radius:10px;padding:14px 22px;font-size:17px;font-weight:600;cursor:pointer;text-decoration:none}}
.big:hover{{background:#333}}.big.red{{background:var(--red)}}.big.red:hover{{background:#a80016}}
.row{{display:flex;flex-wrap:wrap;gap:10px}}.alt{{background:#fff;color:var(--ink);border:1px solid var(--line);font-size:15px;padding:11px 16px}}.alt:hover{{background:#f0f1f3}}
.mini{{border:1px solid var(--line);background:#fff;border-radius:8px;padding:5px 12px;cursor:pointer;font-size:13px;flex:none}}.mini:hover{{background:#f0f1f3}}
ol.form{{list-style:none;counter-reset:f;margin:16px 0 0;padding:0}}ol.form>li{{counter-increment:f;padding:10px 0 10px 34px;border-top:1px solid var(--line);position:relative}}
ol.form>li:before{{content:counter(f);position:absolute;left:0;top:11px;width:22px;height:22px;border-radius:50%;background:#eef0f3;display:grid;place-items:center;font-size:13px;color:var(--mut)}}
.lbl{{font-weight:600}}.val{{display:flex;gap:8px;align-items:center;margin-top:4px}}.val code{{flex:1;min-width:0;overflow-wrap:anywhere;font:13px ui-monospace,Menlo,monospace;background:#f3f4f6;border-radius:6px;padding:6px 8px}}
.hint{{color:var(--mut);font-size:14px;margin-top:2px}}
.warn,.banner{{margin:0 0 16px;padding:14px 16px;border-radius:10px;background:#fffaeb;border:1px solid #f0d58c}}.banner{{display:none}}
.who{{display:flex;gap:10px;align-items:center;font-weight:600;margin-bottom:2px}}.who select{{font:inherit;font-weight:400;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:#fff;max-width:100%}}
ul.ex{{list-style:none;padding:0;margin:12px 0 0}}ul.ex li{{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}}
details{{border-top:1px solid var(--line);padding:10px 0}}details:first-of-type{{border-top:0}}summary{{cursor:pointer;font-weight:600}}details p{{margin:6px 0 0;color:var(--mut)}}
.qr{{display:flex;gap:18px;align-items:center}}.qr svg{{width:120px;height:120px;flex:none}}
.toast{{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#111;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:.2s;pointer-events:none}}.toast.on{{opacity:1}}
</style></head><body><main>
<h1>Connect Lore</h1>
<div class="mut">Ask your AI assistant about any customer and get answers from CRM, meetings, email and Ops reports, with sources.</div>
<div class="status">
  <span class="pill ok" id="s-srv"><i>&#9679;</i>Lore is running</span>
  <span class="pill" id="s-pub"><i>&#9679;</i>Checking public link&hellip;</span>
  <span class="pill" id="s-exp" hidden><i>&#9679;</i></span>
  <span class="pill">Signed in as {e(user_label)}</span>
  <span class="pill">CRM: {e(crm)} &middot; {tools} tools</span>
</div>
<div class="banner" id="changed"><b>Lore's link has changed</b> since you last set up ChatGPT, so the old plugin will stop
working. In ChatGPT open <b>Plugins &rarr; Personal</b>, delete the old Lore plugin, then do step 1 again.</div>
{local_warn}
<section class="card">{picker}</section>

<section class="card">
  <h2><span class="num">1</span>Add Lore to ChatGPT <span class="mut small">(once, about 1 minute)</span></h2>
  <div class="mut small">Click the button. It copies Lore's link and opens ChatGPT. Then fill in ChatGPT's form like this:</div>
  <div style="margin-top:14px"><button class="big" id="gpt">Connect to ChatGPT</button></div>
  <ol class="form">
    <li><div class="lbl">Click <b>Add</b> (top right), then <b>Add custom MCP server</b></div>
      <div class="hint">Don't see it? Turn on <b>Developer mode</b> in <a href="{CHATGPT_DEV_MODE}" target="_blank" rel="noopener">ChatGPT settings &rarr; Security</a>, then reload.</div></li>
    {_field("Name", PLUGIN_NAME)}
    {_field("Description (optional)", PLUGIN_DESCRIPTION)}
    {_field("Server URL", mcp_url, "Already copied by the button. Click in the box and paste (&#8984;V on Mac, Ctrl+V on Windows).")}
    <li><div class="lbl">Authentication: choose <b>No authentication</b></div></li>
    <li><div class="lbl">Tick <b>I understand and want to continue</b>, click <b>Create as a plugin</b>, then <b>Connect</b></div>
      <div class="hint">Creating can take up to 30 seconds.</div></li>
  </ol>
</section>

<section class="card">
  <h2><span class="num">2</span>Ask a question</h2>
  <div class="mut small">Start a new chat in <b>Chat</b> mode. Type <b>@Lore</b>, pick <b>Lore</b> from the list, then paste a question.
  Answers take about 15 seconds.</div>
  <ul class="ex">{ex_html}</ul>
</section>

<section class="card">
  <h2>Using Claude, Cursor or VS Code? <span class="mut small">(one click)</span></h2>
  <div class="row" style="margin-top:10px">
    <a class="big red" href="{e(ln['claude'])}" target="_blank" rel="noopener">Add to Claude</a>
    <a class="big alt" href="{e(ln['cursor'])}">Add to Cursor</a>
    <a class="big alt" href="{e(ln['vscode'])}">Add to VS Code</a>
  </div>
</section>

<section class="card">
  <h2>Something not working?</h2>
  <details><summary>ChatGPT says the connector failed or returned an error</summary>
    <p>Lore's link probably changed (it changes every time Lore restarts). Delete the old Lore plugin in ChatGPT
    (Plugins &rarr; Personal) and do step 1 again with the link on this page.</p></details>
  <details><summary>ChatGPT answers without using Lore</summary>
    <p>Type <b>@Lore</b> and click <b>Lore</b> in the list before your question, and make sure you are in <b>Chat</b> mode, not Work mode.</p></details>
  <details><summary>"Lore is not responding" at the top of this page</summary>
    <p>Lore has stopped, or the computer running it went to sleep. Double-click <b>Start Lore</b> in the Lore folder again.</p></details>
  <details><summary>The public link has expired</summary>
    <p>Free public links last 60 minutes. Close the Lore window, double-click <b>Start Lore</b> again, and update ChatGPT (step 1).</p></details>
  <details><summary>"Access denied" for an account</summary>
    <p>Working as intended: you can only see accounts you are on the team for. Pick a different person under "Who are you?" to demo another view.</p></details>
</section>

<section class="card qr">
  {qr_svg(page_url)}
  <div><h2>Open on another device</h2><div class="mut small">Scan to open this page on a phone or another laptop.</div></div>
</section>
</main><div class="toast" id="t">Copied</div>
<script>
const MCP_URL = {json.dumps(mcp_url)};
const REACHABLE = {json.dumps(reachable)};
const EXPIRES_AT = {json.dumps(expires_at)};
const KEY = 'lore.chatgptUrl';
function toast(m){{const t=document.getElementById('t');t.textContent=m;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),1600)}}
async function copy(s){{try{{await navigator.clipboard.writeText(s);return true}}catch(e){{const i=document.createElement('textarea');i.value=s;document.body.appendChild(i);i.select();const ok=document.execCommand('copy');i.remove();return ok}}}}
function remember(){{if(REACHABLE){{localStorage.setItem(KEY,MCP_URL);document.getElementById('changed').style.display='none'}}}}
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{{if(await copy(b.dataset.copy)){{toast('Copied');if(b.dataset.copy===MCP_URL)remember()}}}}));
document.getElementById('gpt').addEventListener('click',async()=>{{await copy(MCP_URL);remember();toast('Link copied. Paste it in ChatGPT.');window.open({json.dumps(CHATGPT_PLUGINS)},'_blank','noopener')}});
const who=document.getElementById('who');if(who)who.addEventListener('change',()=>{{const u=new URL(location.href);u.searchParams.set('user',who.value);location.href=u}});
const prev=localStorage.getItem(KEY);if(REACHABLE&&prev&&prev!==MCP_URL)document.getElementById('changed').style.display='block';
function pill(id,cls,text){{const p=document.getElementById(id);p.hidden=false;p.className='pill '+cls;p.innerHTML='<i>&#9679;</i>'+text}}
function expiry(){{if(!EXPIRES_AT)return;const m=Math.round((EXPIRES_AT*1000-Date.now())/60000);
  if(m<=0)pill('s-exp','bad','Public link expired. Start Lore again');else pill('s-exp',m<=10?'warn':'ok','Public link lasts '+m+' more min')}}
async function poll(){{try{{const r=await fetch('/api/status',{{cache:'no-store'}});const s=await r.json();pill('s-srv','ok','Lore is running');
  if(!s.public_url)pill('s-pub','warn','No public link: ChatGPT can\\'t reach Lore');
  else if(s.public_ok===false)pill('s-pub','bad','Public link not reachable. Start Lore again');
  else pill('s-pub','ok','Public link working');}}
  catch(e){{pill('s-srv','bad','Lore is not responding. Double-click Start Lore');pill('s-pub','bad','Public link down')}}expiry()}}
poll();setInterval(poll,20000);
</script></body></html>"""
