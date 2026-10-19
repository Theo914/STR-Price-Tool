"""Localhost-only launcher for the ID-first parity workflow."""
from __future__ import annotations

import html
import json
import secrets
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from parity.listing_input import normalise_listing_inputs
from parity.theme import MIDNIGHT_CSS


MAX_REQUEST_BYTES = 16 * 1024


def parse_run_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    mode = payload.get("mode", "single")
    if mode != "single":
        raise ValueError("Skopia accepts one TravelStaytion listing at a time")
    values = payload.get("listings")
    if not isinstance(values, list):
        raise ValueError("listings must be an array")
    listings = normalise_listing_inputs(values)
    if len(listings) != 1:
        raise ValueError("enter exactly one TravelStaytion listing ID or URL")
    guests = payload.get("guests", 2)
    if isinstance(guests, bool) or not isinstance(guests, int) or guests <= 0 or guests > 30:
        raise ValueError("guests must be a whole number from 1 to 30")
    currency = str(payload.get("currency") or "AUTO").strip().upper()
    if currency != "AUTO" and (len(currency) != 3 or not currency.isalpha()):
        raise ValueError("currency must be AUTO or a three-letter code")
    return {
        "mode": mode,
        "listings": [listing_id for listing_id, _ in listings],
        "guests": guests,
        "currency": currency,
    }


def build_auto_command(request: dict[str, Any], work_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "parity.cli",
        "auto",
        *request["listings"],
        "--guests",
        str(request["guests"]),
        "--currency",
        request["currency"],
        "--work-dir",
        str(work_dir / "run"),
        "--output",
        str(work_dir / "latest-results.json"),
        "--dashboard-output",
        str(work_dir / "latest-dashboard.html"),
        # The app runs a visible browser with an operator present: hand any
        # Booking/search-engine human check to them instead of giving up.
        "--interactive-captcha",
    ]


@dataclass
class AppState:
    work_dir: Path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    status: str = "idle"
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def dashboard_path(self) -> Path:
        return self.work_dir / "latest-dashboard.html"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "logs": self.logs[-80:],
                "error": self.error,
                "dashboard_ready": self.status == "done" and self.dashboard_path.is_file(),
            }


def _legacy_launcher_html(token: str) -> str:
    safe_token = html.escape(token, quote=True)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skopia</title><style>
:root{{--ink:#17213a;--muted:#69728a;--line:#e2e8e4;--paper:#f4f7f5;--ts:#173f35;--ts-soft:#eaf3ef;--airbnb:#ff385c;--booking:#003b95;--good:#087a55;--bad:#bd2c3b}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(150deg,#eaf3ef,#fbfbfc 55%,#edf2f8);color:var(--ink);font:13px/1.4 Inter,Segoe UI,Arial,sans-serif}}button,input,textarea,select{{font:inherit}}.shell{{width:min(100%,540px);margin:0 auto;padding:12px}}.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}}.brand{{font-size:18px;font-weight:850;letter-spacing:-.035em}}.brand span{{color:var(--ts);font-size:11px;font-weight:700;letter-spacing:.02em}}.secure{{font-size:10px;color:var(--good)}}.mode{{display:grid;grid-template-columns:1fr 1fr;background:#e6ece9;border-radius:13px;padding:3px;margin-bottom:9px}}.mode button{{border:0;border-radius:10px;padding:7px;background:transparent;color:var(--muted);font-weight:800;cursor:pointer}}.mode .active{{background:#fff;color:var(--ts);box-shadow:0 3px 10px rgba(23,63,53,.10)}}.panel{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:13px;box-shadow:0 8px 24px rgba(23,63,53,.07)}}label{{display:block;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:850;margin:0 0 5px}}input,textarea,select{{width:100%;border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);padding:9px 10px;outline:none}}input:focus,textarea:focus,select:focus{{border-color:#6e9a8c;box-shadow:0 0 0 3px var(--ts-soft)}}textarea{{min-height:112px;resize:vertical}}.hint{{font-size:10px;color:var(--muted);margin:5px 1px 10px}}.settings{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}.actions{{display:grid;grid-template-columns:1fr auto;gap:7px;margin-top:11px}}.run,.clear{{border-radius:12px;padding:10px;font-weight:850;cursor:pointer}}.run{{border:0;background:var(--ts);color:#fff}}.clear{{border:1px solid var(--line);background:#fff;color:var(--muted);min-width:72px}}.run:disabled,.clear:disabled{{opacity:.55;cursor:wait}}.channels{{display:flex;gap:6px;margin-top:9px}}.channel{{font-size:10px;border-radius:999px;padding:4px 8px;font-weight:800}}.channel.ts{{background:var(--ts-soft);color:var(--ts)}}.channel.airbnb{{background:#fff0f3;color:#b81e3a}}.channel.booking{{background:#eef5ff;color:var(--booking)}}.progress{{display:none;margin-top:9px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:11px}}.progress.show{{display:block}}.progress-head{{display:flex;justify-content:space-between;font-weight:800}}.bar{{height:4px;border-radius:99px;background:#e8ebf1;overflow:hidden;margin:8px 0}}.bar i{{display:block;width:35%;height:100%;background:linear-gradient(90deg,var(--ts),var(--airbnb),var(--booking));animation:move 1.1s ease-in-out infinite alternate}}.current{{font-size:11px;color:var(--muted);min-height:30px}}details{{margin-top:7px;border-top:1px solid var(--line);padding-top:6px}}summary{{font-size:10px;color:var(--muted);cursor:pointer}}pre{{margin:6px 0 0;max-height:150px;overflow:auto;white-space:pre-wrap;font:10px/1.45 Consolas,monospace;color:var(--muted)}}.error{{color:var(--bad);font-size:11px;margin-top:7px}}@keyframes move{{to{{transform:translateX(185%)}}}}</style></head>
<body><main class="shell"><div class="top"><div class="brand">Skopia <span>· rate watch</span></div><div class="secure">● This computer only</div></div>
<div class="mode"><button id="single" class="active" type="button">Single check</button><button id="portfolio" type="button">Up to 10</button></div>
<section class="panel"><label id="inputLabel" for="singleInput">TravelStaytion listing ID</label><input id="singleInput" inputmode="numeric" placeholder="e.g. 1069699" autocomplete="off"><textarea id="portfolioInput" hidden placeholder="One ID per line, up to 10"></textarea><div class="hint" id="hint">Enter only the number; the full TravelStaytion link is built automatically.</div><div class="settings"><div><label for="guests">Guests</label><input id="guests" type="number" min="1" max="30" value="2"></div><div><label for="currency">Currency</label><select id="currency"><option value="AUTO">Auto</option><option>EUR</option><option>GBP</option><option>AUD</option><option>USD</option></select></div></div><div class="channels"><span class="channel ts">TravelStaytion</span><span class="channel airbnb">Airbnb</span><span class="channel booking">Booking.com</span></div><div class="actions"><button class="run" id="run" type="button">Find, verify and compare</button><button class="clear" id="clear" type="button">Clear</button></div><div class="error" id="error"></div></section>
<section class="progress" id="progress"><div class="progress-head"><span>Checking listings</span><span id="state">Starting…</span></div><div class="bar"><i></i></div><div class="current" id="current">Preparing the check…</div><details><summary>Technical log</summary><pre id="logs"></pre></details></section></main>
<script>const TOKEN='{safe_token}',DRAFT='parity-desk-draft-v1';let mode='single';const single=document.getElementById('single'),portfolio=document.getElementById('portfolio'),singleInput=document.getElementById('singleInput'),portfolioInput=document.getElementById('portfolioInput'),run=document.getElementById('run'),clear=document.getElementById('clear'),error=document.getElementById('error'),progress=document.getElementById('progress'),guests=document.getElementById('guests'),currency=document.getElementById('currency');function saveDraft(){{localStorage.setItem(DRAFT,JSON.stringify({{mode,single:singleInput.value,portfolio:portfolioInput.value,guests:guests.value,currency:currency.value}}))}}function setMode(value,save=true){{mode=value;const one=value==='single';single.classList.toggle('active',one);portfolio.classList.toggle('active',!one);singleInput.hidden=!one;portfolioInput.hidden=one;document.getElementById('inputLabel').textContent=one?'TravelStaytion listing ID':'TravelStaytion listing IDs';document.getElementById('inputLabel').htmlFor=one?'singleInput':'portfolioInput';document.getElementById('hint').textContent=one?'Enter only the number; the full TravelStaytion link is built automatically.':'Paste one ID per line. Duplicates are removed; maximum 10.';if(save)saveDraft()}}single.onclick=()=>setMode('single');portfolio.onclick=()=>setMode('portfolio');[singleInput,portfolioInput,guests,currency].forEach(el=>el.addEventListener('input',saveDraft));try{{const draft=JSON.parse(localStorage.getItem(DRAFT)||'null');if(draft){{singleInput.value=draft.single||'';portfolioInput.value=draft.portfolio||'';guests.value=draft.guests||'2';currency.value=draft.currency||'AUTO';setMode(draft.mode==='portfolio'?'portfolio':'single',false)}}}}catch(_e){{}}function values(){{const raw=mode==='single'?singleInput.value:portfolioInput.value;return raw.split(/[\\s,]+/).map(x=>x.trim()).filter(Boolean)}}clear.onclick=()=>{{singleInput.value='';portfolioInput.value='';guests.value='2';currency.value='AUTO';error.textContent='';progress.classList.remove('show');localStorage.removeItem(DRAFT);setMode('single',false);singleInput.focus()}};async function poll(){{const response=await fetch('/api/status');const data=await response.json();document.getElementById('state').textContent=data.status.replaceAll('_',' ');document.getElementById('logs').textContent=data.logs.join('\\n');document.getElementById('current').textContent=data.logs.at(-1)||'Working through discovery, identity and price checks…';if(data.dashboard_ready){{location.href='/dashboard';return}}if(data.status==='failed'){{error.textContent=data.error||'The check stopped. Open the technical log for details.';run.disabled=false;clear.disabled=false;return}}setTimeout(poll,900)}}run.onclick=async()=>{{error.textContent='';run.disabled=true;clear.disabled=true;progress.classList.add('show');saveDraft();try{{const response=await fetch('/api/run',{{method:'POST',headers:{{'Content-Type':'application/json','X-Parity-Token':TOKEN}},body:JSON.stringify({{mode,listings:values(),guests:Number(guests.value),currency:currency.value}})}});const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not start');poll()}}catch(e){{error.textContent=e.message;run.disabled=false;clear.disabled=false}}}};</script></body></html>'''


def _launcher_html(token: str) -> str:
    """Render the focused one-property launcher."""
    safe_token = html.escape(token, quote=True)
    template = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skopia</title><style>
:root{--ink:#17213a;--muted:#69728a;--line:#e2e8e4;--ts:#173f35;--ts-soft:#eaf3ef;--airbnb:#ff385c;--booking:#003b95;--good:#087a55;--bad:#bd2c3b}*{box-sizing:border-box}body{margin:0;background:linear-gradient(150deg,#eaf3ef,#fbfbfc 55%,#edf2f8);color:var(--ink);font:13px/1.4 Inter,Segoe UI,Arial,sans-serif}button,input,select{font:inherit}.shell{width:min(100%,540px);margin:0 auto;padding:12px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:3px}.brand{font-size:18px;font-weight:850;letter-spacing:-.035em}.brand span{color:var(--ts);font-size:11px;font-weight:700;letter-spacing:.02em}.secure{font-size:10px;color:var(--good)}.intro{color:var(--muted);font-size:11px;margin:0 0 10px}.panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:13px;box-shadow:0 8px 24px rgba(23,63,53,.07)}label{display:block;font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:850;margin:0 0 5px}input,select{width:100%;border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);padding:9px 10px;outline:none}input:focus,select:focus{border-color:#6e9a8c;box-shadow:0 0 0 3px var(--ts-soft)}.hint{font-size:10px;color:var(--muted);margin:5px 1px 10px}.canonical{display:none;margin:-3px 1px 10px;color:var(--ts);font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.canonical.show{display:block}.settings{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions{display:grid;grid-template-columns:1fr auto;gap:7px;margin-top:11px}.run,.clear{border-radius:12px;padding:10px;font-weight:850;cursor:pointer}.run{border:0;background:var(--ts);color:#fff}.clear{border:1px solid var(--line);background:#fff;color:var(--muted);min-width:72px}.run:disabled,.clear:disabled{opacity:.55;cursor:wait}.channels{display:flex;gap:6px;margin-top:9px}.channel{font-size:10px;border-radius:999px;padding:4px 8px;font-weight:800}.channel.ts{background:var(--ts-soft);color:var(--ts)}.channel.airbnb{background:#fff0f3;color:#b81e3a}.channel.booking{background:#eef5ff;color:var(--booking)}.promise{font-size:9px;color:var(--muted);margin-top:9px}.progress{display:none;margin-top:9px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:11px}.progress.show{display:block}.progress-head{display:flex;justify-content:space-between;font-weight:800}.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin:9px 0}.step{border-top:3px solid #e5e9e7;padding-top:4px;color:#9399a8;font-size:8px;font-weight:800;text-align:center}.step.active{border-color:var(--ts);color:var(--ts)}.step.done{border-color:var(--good);color:var(--good)}.current{font-size:11px;color:var(--muted);min-height:30px}details{margin-top:7px;border-top:1px solid var(--line);padding-top:6px}summary{font-size:10px;color:var(--muted);cursor:pointer}pre{margin:6px 0 0;max-height:150px;overflow:auto;white-space:pre-wrap;font:10px/1.45 Consolas,monospace;color:var(--muted)}.error{color:var(--bad);font-size:11px;margin-top:7px}footer{font-size:9px;color:#8b92a5;text-align:center;padding:9px 0}</style></head>
<body><main class="shell"><div class="top"><div class="brand">Skopia</div></div>
<section class="panel"><label for="listingInput">TravelStaytion listing ID or URL</label><input id="listingInput" inputmode="url" placeholder="1069699 or travelstaytion.com/stay/1069699" autocomplete="off" autofocus><div class="canonical" id="canonical" aria-live="polite"></div><div class="settings"><div><label for="guests">Guests</label><input id="guests" type="number" min="1" max="30" value="2"></div><div><label for="currency">Currency</label><select id="currency"><option value="AUTO">Auto-detect</option><option>EUR</option><option>GBP</option><option>AUD</option><option>USD</option></select></div></div><div class="actions"><button class="run" id="run" type="button">Find, verify and compare</button><button class="clear" id="clear" type="button">Clear</button></div><div class="error" id="error" role="alert"></div></section>
<section class="progress" id="progress" aria-live="polite"><div class="progress-head"><span>Checking property</span><span id="state">Starting…</span></div><div class="steps"><div class="step active">TravelStaytion</div><div class="step">Discover</div><div class="step">Verify</div><div class="step">Price</div></div><div class="current" id="current">Preparing the check…</div><details><summary>Technical log</summary><pre id="logs"></pre></details></section></main>
<script>try{if(window.outerWidth>700||window.outerHeight>900){window.resizeTo(440,680)}}catch(_e){}</script><script>const TOKEN='__TOKEN__',DRAFT='parity-desk-single-draft-v2';const listingInput=document.getElementById('listingInput'),run=document.getElementById('run'),clear=document.getElementById('clear'),error=document.getElementById('error'),progress=document.getElementById('progress'),guests=document.getElementById('guests'),currency=document.getElementById('currency'),canonical=document.getElementById('canonical'),steps=[...document.querySelectorAll('.step')];function listingId(){const value=listingInput.value.trim();const bare=value.match(/^\d+$/);if(bare)return bare[0];const url=value.match(/^https?:\/\/(?:www\.)?travelstaytion\.com\/stay\/(\d+)(?:[/?#].*)?$/i);return url?url[1]:null}function updateCanonical(){const id=listingId();canonical.textContent=id?`travelstaytion.com/stay/${id}`:'';canonical.classList.toggle('show',Boolean(id))}function saveDraft(){localStorage.setItem(DRAFT,JSON.stringify({listing:listingInput.value,guests:guests.value,currency:currency.value}))}[listingInput,guests,currency].forEach(el=>el.addEventListener('input',()=>{saveDraft();if(el===listingInput)updateCanonical()}));try{const draft=JSON.parse(localStorage.getItem(DRAFT)||'null');if(draft){listingInput.value=draft.listing||'';guests.value=draft.guests||'2';currency.value=draft.currency||'AUTO';updateCanonical()}}catch(_e){}try{const saved=localStorage.getItem('parity-desk-currency-v1');if(saved)currency.value=saved}catch(_e){}currency.addEventListener('change',()=>{try{localStorage.setItem('parity-desk-currency-v1',currency.value)}catch(_e){}});function setStage(index){steps.forEach((step,i)=>{step.classList.toggle('done',i<index);step.classList.toggle('active',i===index)})}function stageFrom(logs){const text=logs.join(' ').toLowerCase();if(/result priced|parity:|price breakdown|portfolio job|minimum-stay|minimum stay|pricing/.test(text))return 3;if(/identity|verified|unverified|registration identifier|photo match/.test(text))return 2;if(/candidate|discover|airbnb|booking public search|bounded identity search/.test(text))return 1;return 0}clear.onclick=()=>{listingInput.value='';guests.value='2';error.textContent='';canonical.textContent='';canonical.classList.remove('show');progress.classList.remove('show');localStorage.removeItem(DRAFT);setStage(0);listingInput.focus()};let pollFailures=0;async function poll(){try{const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw new Error('status '+response.status);const data=await response.json();if(pollFailures){pollFailures=0;error.textContent='';error.style.color=''}document.getElementById('state').textContent=data.status==='running'?'In progress':data.status.replaceAll('_',' ');document.getElementById('logs').textContent=data.logs.join('\n');{const cur=document.getElementById('current'),last=data.logs.at(-1)||'Working through discovery, identity and price checks…',act=last.includes('ACTION NEEDED');cur.textContent=act?'⚠ '+last.replace('[parity] ACTION NEEDED: ',''):last;cur.style.color=act?'var(--bad)':'';cur.style.fontWeight=act?'800':''}setStage(stageFrom(data.logs));if(data.dashboard_ready){location.href='/dashboard';return}if(data.status==='failed'){error.textContent=data.error||'The check stopped. Open the technical log for details.';run.disabled=false;clear.disabled=false;return}setTimeout(poll,900)}catch(_e){pollFailures+=1;if(pollFailures<30){error.style.color='var(--muted)';error.textContent='Reconnecting to the local app… the check keeps running in PowerShell.'}else{error.style.color='';error.textContent='Cannot reach the local app. Make sure the PowerShell window is still open; this page keeps retrying.'}setTimeout(poll,pollFailures<30?2000:5000)}}async function start(){error.textContent='';const id=listingId();if(!id){error.textContent='Enter a numeric TravelStaytion ID or a full travelstaytion.com/stay/… URL.';listingInput.focus();return}const guestCount=Number(guests.value);if(!Number.isInteger(guestCount)||guestCount<1||guestCount>30){error.textContent='Guests must be a whole number from 1 to 30.';guests.focus();return}run.disabled=true;clear.disabled=true;progress.classList.add('show');setStage(0);saveDraft();try{const response=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json','X-Parity-Token':TOKEN},body:JSON.stringify({mode:'single',listings:[id],guests:guestCount,currency:currency.value})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Could not start');poll()}catch(e){error.textContent=e.message;run.disabled=false;clear.disabled=false}}run.onclick=start;fetch('/api/status',{cache:'no-store'}).then(r=>r.json()).then(data=>{if(data.status==='running'){run.disabled=true;clear.disabled=true;progress.classList.add('show');poll()}}).catch(()=>{});listingInput.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();start()}});</script></body></html>'''
    return template.replace("__TOKEN__", safe_token).replace("</style>", MIDNIGHT_CSS + "</style>", 1)


def create_app_server(work_dir: Path, port: int = 0) -> ThreadingHTTPServer:
    state = AppState(work_dir=work_dir.resolve())
    state.work_dir.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ParityDesk/1"

        def log_message(self, *_args) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, _launcher_html(state.token).encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                self._json(200, state.snapshot())
            elif self.path == "/dashboard" and state.dashboard_path.is_file():
                self._send(200, state.dashboard_path.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/downloads/results.json" and (state.work_dir / "latest-results.json").is_file():
                self._send(
                    200,
                    (state.work_dir / "latest-results.json").read_bytes(),
                    "application/json; charset=utf-8",
                )
            elif self.path == "/downloads/results.csv" and (state.work_dir / "latest-results.csv").is_file():
                self._send(
                    200,
                    (state.work_dir / "latest-results.csv").read_bytes(),
                    "text/csv; charset=utf-8",
                )
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/run":
                self._json(404, {"error": "not found"})
                return
            if self.headers.get("X-Parity-Token") != state.token:
                self._json(403, {"error": "invalid local request token"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._json(400, {"error": "invalid request size"})
                return
            try:
                request = parse_run_payload(json.loads(self.rfile.read(length)))
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            with state.lock:
                if state.status == "running":
                    self._json(409, {"error": "a parity check is already running"})
                    return
                state.status = "running"
                state.logs = []
                state.error = None

            def worker() -> None:
                command = build_auto_command(request, state.work_dir)
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                    assert process.stdout is not None
                    for line in process.stdout:
                        with state.lock:
                            state.logs.append(line.rstrip())
                            if len(state.logs) > 400:
                                del state.logs[:-400]
                    code = process.wait()
                    with state.lock:
                        if code == 0 and state.dashboard_path.is_file():
                            state.status = "done"
                        else:
                            state.status = "failed"
                            state.error = f"Parity check exited with code {code}."
                except Exception as exc:
                    with state.lock:
                        state.status = "failed"
                        state.error = str(exc)

            threading.Thread(target=worker, daemon=True).start()
            self._json(202, {"status": "running"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.state = state  # type: ignore[attr-defined]
    return server
