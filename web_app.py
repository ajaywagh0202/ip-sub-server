"""
web_app.py  —  ITC System Scanner  (Client-Scan Edition)
=========================================================
Central web server.  Runs ONCE on a server/admin machine.
Client PCs run  client_agent.py  to register themselves.

HOW IT WORKS
------------
  Server machine:   python web_app.py            (runs always-on)
  Client PC:        python client_agent.py        (run on each PC to scan)
  Browser:          http://<server-ip>:5001       (opened on any device)

  1. client_agent.py runs on the client PC, listens on port 7979, and
     POSTs its IP/hostname to /api/register_client here.
  2. The browser opens the web UI — the info bar shows the BROWSER's client IP.
  3. The UI finds the matching registered agent and shows "Scan This PC".
  4. Clicking Scan -> this server calls POST http://<client-ip>:7979/run.
  5. The agent scans the client PC, saves PDF+JSON locally, then PUTs them
     to /api/client_upload/<agent_id>/<type>/<filename> on this server.
  6. The browser polls /api/client_status/<agent_id> and shows the log.
  7. When done, Download PDF / Download JSON links appear.

INSTALL (server machine, once)
-------------------------------
    pip install flask reportlab

RUN
---
    python web_app.py

FILES NEEDED
------------
    web_app.py          <- this file (server)
    client_agent.py     <- copy to each client PC along with app_inventory.py
"""

import os
import sys
import re
import json
import time
import socket
import types
import threading
import importlib.util
import uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, request, redirect, jsonify

# ── PIL stub ──────────────────────────────────────────────────────────────────
_pil_stub = types.ModuleType("PIL")
_pil_stub.Image = None
sys.modules.setdefault("PIL", _pil_stub)
sys.modules.setdefault("PIL.Image", types.ModuleType("PIL.Image"))

from flask import Flask, render_template_string, jsonify, send_file, request, abort

# ── Config ────────────────────────────────────────────────────────────────────
PORT          = 5001
AGENT_PORT    = 7979           # port client_agent.py listens on
OUTPUT_DIR    = Path.home() / "itc_scanner_reports"
SECRET_TOKEN  = "ITC2024CLIENT"   # must match client_agent.py
AGENT_TIMEOUT = 600            # seconds — long scans can take several minutes
DEVICE_SCAN_API_URL = os.environ.get(
    "DEVICE_SCAN_API_URL",
    os.environ.get("REGISTER_API_URL", "http://10.31.3.114:5000/api/device-scan"),
)
# ─────────────────────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, static_folder=None)


def _clear_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    root = OUTPUT_DIR.resolve()
    for item in root.iterdir():
        try:
            resolved = item.resolve()
            if root != resolved and root not in resolved.parents:
                continue
            if item.is_file():
                item.unlink()
        except Exception as exc:
            print(f"Could not delete old report file {item}: {exc}")


# =============================================================================
# Registered client agents
# =============================================================================

_agents_lock = threading.Lock()
_agents: dict = {}
# key = agent_id,  value = {ip, hostname, port, last_seen, status}


def _agent_id(ip: str, hostname: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_\-]', '_', hostname)
    return f"{ip.replace('.','_')}_{safe}"


def _prune_agents():
    """Remove agents that haven't pinged in AGENT_TIMEOUT seconds."""
    cutoff = time.time() - AGENT_TIMEOUT
    with _agents_lock:
        stale = [k for k, v in _agents.items() if v["last_seen"] < cutoff]
        for k in stale:
            del _agents[k]


# =============================================================================
# Per-client scan state (PDF + JSON stored here after upload)
# =============================================================================

_client_files_lock = threading.Lock()
_client_files: dict = {}
# key = agent_id, value = {"pdf": path, "json": path}

_client_form_data_lock = threading.Lock()
_client_form_data: dict = {}
_client_register_sent = set()
# key = agent_id, value = form data captured from /api/scan_client/<agent_id>?data=...


# =============================================================================
# Helper: call the client agent's HTTP API
# =============================================================================

def _call_agent(agent_id: str, method: str, path: str, body=None) -> dict:
    import urllib.request
    import urllib.error

    with _agents_lock:
        agent = _agents.get(agent_id)
    if not agent:
        return {"error": f"Agent '{agent_id}' not registered or timed out."}

    url  = f"http://{agent['ip']}:{agent['port']}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={"X-Token": SECRET_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"error": str(e), "_transient": True}
    except Exception as e:
        return {"error": str(e), "_transient": True}


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# =============================================================================
# HTML Template
# =============================================================================

HTML = r"""<!DOCTYPE html>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ITC System Scanner</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0F1221; color: #E2E8F0;
    font-family: 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh; display: flex; flex-direction: column;
  }
  header {
    background: #1A1F36; border-bottom: 2px solid #4F6EF7;
    padding: 18px 32px; display: flex; align-items: center;
    justify-content: space-between; gap: 16px;
  }
  .hdr-left p     { color:#7B93FF; font-size:11px; font-weight:600; letter-spacing:.06em; text-transform:uppercase; }
  .hdr-left h1    { font-size:22px; font-weight:700; margin:2px 0; }
  .hdr-left small { color:#94A3B8; font-size:12px; }
  .badge { background:#232946; color:#4F6EF7; font-size:11px; font-weight:700; padding:4px 12px; border-radius:20px; }

  .info-bar {
    background:#232946; border-bottom:1px solid #2D3561;
    padding:10px 32px; display:flex; flex-wrap:wrap; gap:32px; align-items:center;
  }
  .info-item label { display:block; font-size:10px; color:#94A3B8; text-transform:uppercase; letter-spacing:.05em; }
  .info-item span  { font-size:13px; font-weight:600; }

  .client-banner {
    background: #1A2E1A; border: 1px solid #22C55E; border-radius:8px;
    padding: 10px 18px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;
    font-size:13px;
  }
  .client-banner.not-registered {
    background: #2E1A1A; border-color: #EF4444;
  }
  .client-banner strong { font-weight:700; }

  main { flex:1; max-width:1100px; width:100%; margin:28px auto; padding:0 24px; display:flex; flex-direction:column; gap:20px; }

  .card { background:#1A1F36; border:1px solid #2D3561; border-radius:10px; overflow:hidden; }
  .card-header { padding:14px 20px; background:#232946; border-bottom:1px solid #2D3561; font-weight:700; font-size:14px; display:flex; align-items:center; gap:8px; }

  .btn { display:inline-flex; align-items:center; gap:8px; padding:11px 24px; border-radius:8px; border:none; font-size:14px; font-weight:600; cursor:pointer; transition:filter .15s,opacity .15s; text-decoration:none; }
  .btn:hover:not(:disabled) { filter:brightness(1.15); }
  .btn:disabled { opacity:.45; cursor:not-allowed; }
  .btn-primary   { background:#4F6EF7; color:#fff; }
  .btn-success   { background:#22C55E; color:#fff; }
  .btn-secondary { background:#2D3561; color:#E2E8F0; }
  .btn-small     { padding:6px 14px; font-size:12px; }

  .action-row { display:flex; align-items:center; gap:12px; padding:18px 20px; flex-wrap:wrap; }

  #log {
    background:#0B0F1A; font-family:'Consolas','Courier New',monospace;
    font-size:12px; line-height:1.65; padding:16px 20px;
    height:380px; overflow-y:auto; white-space:pre-wrap; color:#94A3B8;
  }
  .log-ok   { color:#22C55E; }
  .log-warn { color:#F59E0B; }
  .log-err  { color:#EF4444; }
  .log-head { color:#7B93FF; font-weight:bold; }

  #result-banner {
    display: none; padding:16px 20px; border-top:1px solid #2D3561;
    align-items:center; flex-wrap:wrap; gap:12px;
  }
  #result-banner.visible { display: flex; }
  .result-text { font-size:13px; color:#22C55E; font-weight:600; flex:1; }

  /* Registered clients list */
  .client-list { padding:16px 20px; display:flex; flex-direction:column; gap:8px; }
  .client-row  {
    background:#232946; border:1px solid #2D3561; border-radius:8px;
    padding:10px 16px; display:flex; align-items:center; gap:16px; flex-wrap:wrap;
  }
  .client-row .dot { width:9px; height:9px; border-radius:50%; background:#22C55E; flex-shrink:0; }
  .client-row .info { flex:1; }
  .client-row .info strong { font-size:13px; }
  .client-row .info small  { color:#94A3B8; font-size:11px; display:block; }
  .client-row .actions     { display:flex; gap:8px; }

  .btn-danger    { background:#EF4444; color:#fff; }
  .btn-warning   { background:#F59E0B; color:#fff; }

  /* agent status pill inside client rows */
  .agent-pill {
    display:inline-flex; align-items:center; gap:5px;
    font-size:10px; font-weight:700; padding:3px 9px; border-radius:20px;
    letter-spacing:.04em; text-transform:uppercase;
  }
  .agent-pill.running { background:#1A2E1A; color:#22C55E; border:1px solid #22C55E; }
  .agent-pill.stopped { background:#2E1A1A; color:#EF4444; border:1px solid #EF4444; }

  /* client-only view — single machine card */
  .my-machine-card {
    background:#1A1F36; border:2px solid #4F6EF7; border-radius:10px; overflow:hidden;
  }
  .my-machine-card .card-header { background:#232946; border-bottom:1px solid #2D3561; }

  footer { text-align:center; padding:14px; color:#2D3561; font-size:11px; border-top:1px solid #1A1F36; }

  #status-dot { width:10px; height:10px; border-radius:50%; background:#2D3561; display:inline-block; transition:background .3s; }
  #status-dot.scanning { background:#4F6EF7; animation:pulse 1s infinite; }
  #status-dot.done     { background:#22C55E; animation:none; }
  #status-dot.error    { background:#EF4444; animation:none; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }

  /* ── Scan Complete Banner ── */
  #scan-complete-banner {
    display:none; background:linear-gradient(135deg,#0D2B1A 0%,#1A2E1A 100%);
    border:2px solid #22C55E; border-radius:12px; padding:20px 24px;
    align-items:center; flex-wrap:wrap; gap:16px; margin-top:4px;
  }
  #scan-complete-banner.visible { display:flex; }
  .complete-icon { font-size:36px; flex-shrink:0; }
  .complete-info { flex:1; min-width:180px; }
  .complete-info h3 { color:#22C55E; font-size:16px; font-weight:700; margin-bottom:4px; }
  .complete-info p  { color:#94A3B8; font-size:12px; }
  .complete-actions { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }

  /* ── PDF Viewer Modal ── */
  #pdf-modal {
    display:none; position:fixed; inset:0; background:rgba(0,0,0,.82);
    z-index:9999; align-items:center; justify-content:center;
  }
  #pdf-modal.open { display:flex; }
  .pdf-modal-box {
    background:#1A1F36; border:1px solid #4F6EF7; border-radius:12px;
    width:90vw; max-width:1050px; height:90vh;
    display:flex; flex-direction:column; overflow:hidden; box-shadow:0 24px 80px #000a;
  }
  .pdf-modal-header {
    background:#232946; border-bottom:1px solid #2D3561;
    padding:12px 20px; display:flex; align-items:center; gap:12px;
  }
  .pdf-modal-header span { flex:1; font-weight:700; font-size:14px; color:#7B93FF; }
  .pdf-modal-header button { background:#2D3561; border:none; color:#E2E8F0; border-radius:6px; padding:6px 14px; cursor:pointer; font-size:12px; font-weight:600; }
  .pdf-modal-header button:hover { background:#4F6EF7; }
  #pdf-iframe { flex:1; border:none; background:#fff; }

  .setup-hint {
    background:#1A1F36; border:1px solid #2D3561; border-radius:10px;
    padding:20px 24px; font-size:13px; color:#94A3B8; line-height:1.8;
  }
  .setup-hint code { background:#0B0F1A; padding:2px 7px; border-radius:4px; font-family:monospace; color:#7B93FF; }
  .setup-hint h3   { color:#E2E8F0; margin-bottom:10px; font-size:15px; }

  /* ── Auto-deploy styles ── */
  /* Bug 3 fix: indeterminate progress bar animation */
  @keyframes progress-indeterminate {
    0%   { margin-left:-30%; width:30%; }
    100% { margin-left:100%; width:30%; }
  }
  #scan-progress-wrap { overflow:hidden; border-radius:4px; background:#1A1F36; height:6px; margin-top:10px; }
  #scan-progress-bar  { height:6px; background:#4F6EF7; border-radius:4px;
                         animation: progress-indeterminate 1.4s linear infinite; width:30%; }
  .deploy-tabs { display:flex; gap:0; border-bottom:1px solid #2D3561; margin:14px 0 0; }
  .deploy-tab  { padding:7px 18px; font-size:12px; font-weight:600; cursor:pointer;
                 border:1px solid transparent; border-bottom:none; border-radius:6px 6px 0 0;
                 color:#94A3B8; background:transparent; transition:all .15s; }
  .deploy-tab.active  { background:#0B0F1A; color:#7B93FF; border-color:#2D3561; }
  .deploy-tab:hover:not(.active) { color:#E2E8F0; }
  .deploy-pane { display:none; background:#0B0F1A; border:1px solid #2D3561;
                 border-top:none; border-radius:0 0 6px 6px; padding:14px 16px; }
  .deploy-pane.active { display:block; }
  .cmd-box { display:flex; align-items:center; gap:8px; margin:6px 0; }
  .cmd-box code {
    flex:1; background:#0B0F1A; border:1px solid #2D3561; border-radius:5px;
    padding:8px 12px; font-family:monospace; font-size:11.5px; color:#7B93FF;
    white-space:nowrap; overflow-x:auto; display:block;
  }
  .copy-btn {
    flex-shrink:0; background:#2D3561; border:none; color:#E2E8F0; border-radius:5px;
    padding:6px 10px; font-size:11px; cursor:pointer; transition:background .15s;
  }
  .copy-btn:hover { background:#4F6EF7; }
  .copy-btn.copied { background:#22C55E; color:#fff; }
  .deploy-label { font-size:11px; color:#94A3B8; margin-bottom:4px; }
  .auto-poll-badge {
    display:inline-flex; align-items:center; gap:5px;
    background:#1A2E1A; border:1px solid #22C55E; border-radius:20px;
    padding:3px 10px; font-size:11px; color:#22C55E; font-weight:600; margin-top:8px;
  }
  .auto-poll-dot { width:7px; height:7px; border-radius:50%; background:#22C55E;
                   animation:pulse 1.2s infinite; }
</style>
</head>
<body>

<header>
  <div class="hdr-left">
  <h1>Central Railway - Information Technology Centre - System Scanning &amp; Inventory Tool</h1>
   <small>&nbsp;&bull;&nbsp; Client-Scan Edition v3.0</small>
  </div>
  <span class="badge">v3.0</span>
</header>

<div class="info-bar">
  <div class="info-item"><label>Your Browser IP</label><span id="ib-client-ip">detecting…</span></div>
  <div class="info-item"><label>Server Host</label><span id="ib-host">…</span></div>
  <div class="info-item"><label>Server IP</label><span id="ib-ip">…</span></div>
  <div class="info-item"><label>Server OS</label><span id="ib-os">…</span></div>
  <div class="info-item"><label>Server Time</label><span id="ib-time">…</span></div>
</div>

<main>

  <!-- Client detection banner (admin sees this too, just informational) -->
  <div id="client-banner-wrap"></div>

  <!-- ══ CLIENT-ONLY VIEW — only shown when the browser IS a registered client ══ -->
  <div id="my-machine-section" style="display:none">
    <div class="my-machine-card">
      <div class="card-header">🖥️ Your Machine — <span id="my-machine-label" style="color:#7B93FF">Detecting…</span>
        <span id="my-agent-pill" class="agent-pill running" style="margin-left:10px">● Running</span>
      </div>
      <div class="action-row" style="flex-wrap:wrap;gap:10px;">
        <button class="btn btn-success" id="my-start-btn" onclick="agentAction('start')" title="Start the agent on this machine">▶&nbsp; Start Agent</button>
        <button class="btn btn-danger"  id="my-stop-btn"  onclick="agentAction('stop')"  title="Stop the agent on this machine">■&nbsp; Stop Agent</button>
        <button class="btn btn-primary" id="my-scan-btn"  onclick="selectAndScanMine()" title="Run a full system scan on this machine">🔍&nbsp; Scan This PC</button>
        <span id="my-agent-msg" style="color:#94A3B8;font-size:13px;margin-left:6px;"></span>
      </div>
    </div>
  </div>

  <!-- Registered clients list (admin always sees; client sees only in admin-mode) -->
  <div class="card" id="admin-clients-card">
    <div class="card-header">🖥️ Registered Client PCs
      <button class="btn btn-secondary btn-small" style="margin-left:auto" onclick="loadClients()">↻ Refresh</button>
    </div>
    <div class="client-list" id="client-list">
      <span style="color:#94A3B8;font-size:13px;padding:8px 0">Loading client list…</span>
    </div>
  </div>

  <!-- Scan controls (Always fully visible on page load) -->
  <div class="card" id="scan-card">
    <div class="card-header"><span id="status-dot"></span> Scan Controls — <span id="scan-target-label" style="color:#7B93FF">Please select a client PC</span></div>
    <div class="action-row">
      <button class="btn btn-primary" id="scan-btn" onclick="startScan()" disabled>▶&nbsp; Scan This PC</button>
      
      <!-- Persistent View PDF Button - beautifully rendered and visible on page load -->
      <button class="btn btn-success" id="view-pdf-btn" onclick="openPdfViewer()" disabled style="opacity: 0.5; cursor: not-allowed;">👁&nbsp; View PDF</button>
      
      <span id="status-msg" style="color:#94A3B8;font-size:13px;margin-left:10px;">Select a client above to begin.</span>
    </div>
    <!-- Bug 3 fix: progress bar shown during scan -->
    <div id="scan-progress-wrap" style="display:none;">
      <div id="scan-progress-bar"></div>
    </div>
  </div>

  <!-- Log panel (Always fully visible on page load) -->
  <div class="card" id="log-card">
    <div class="card-header">📋 Live Scan Log</div>
    <div id="log">Select a client PC above and click Scan to begin…</div>
    <div id="result-banner">
      <span class="result-text" id="result-text"></span>
      <button class="btn btn-success"   id="dl-pdf"  disabled onclick="window.location=this.dataset.href" data-href="#">⬇&nbsp; Download PDF</button>
      <button class="btn btn-secondary" id="dl-json" disabled onclick="window.location=this.dataset.href" data-href="#">⬇&nbsp; Download JSON</button>
    </div>
  </div>

  <!-- Scan Complete Banner -->
  <div id="scan-complete-banner">
    <div class="complete-icon">✅</div>
    <div class="complete-info">
      <h3>Scan Complete — Report Ready</h3>
      <p id="complete-filename">PDF report generated successfully.</p>
    </div>
    <div class="complete-actions">
      <button class="btn btn-primary" id="dl-pdf2" onclick="window.location=this.dataset.href" data-href="#">⬇&nbsp; Download PDF</button>
      <button class="btn btn-secondary" id="dl-json2" onclick="window.location=this.dataset.href" data-href="#" style="display:none">⬇&nbsp; Download JSON</button>
      </div>
    </div>
  </div>

  <!-- Bug 5 fix: minimal no-clients notice (without OS tabs) -->
  <div id="setup-hint" style="display:none" class="setup-hint">
    <h3>No clients registered yet</h3>
    <p>Use the Deploy Client Agent panel above to add your first PC.</p>
  </div>

  <!-- PDF Viewer Modal -->
  <div id="pdf-modal">
    <div class="pdf-modal-box">
      <div class="pdf-modal-header">
        <span id="pdf-modal-title">📄 Scan Report</span>
        <button onclick="window.location=document.getElementById('dl-pdf2').dataset.href">⬇ Download</button>
        <button onclick="closePdfViewer()" style="background:#3B1A1A;color:#EF4444;">✕ Close</button>
      </div>
      <iframe id="pdf-iframe" src="about:blank"></iframe>
    </div>
  </div>

  <!-- Bug 5 fix: OS deploy tabs moved here — always visible regardless of client count -->
  <div class="card" id="deploy-card" style="margin-top:12px;">
    <div class="card-header" onclick="toggleDeployCard()" style="cursor:pointer;user-select:none;">
      ⚡ Deploy Client Agent
      <span id="deploy-toggle-label" style="font-weight:400;font-size:12px;margin-left:8px;color:#94A3B8;">▼ click to expand</span>
    </div>
    <div id="deploy-content" style="display:none;padding:14px;">
      <p style="margin-bottom:4px">
        Run <strong>one command</strong> on the target PC. It automatically downloads and installs
        the agent, configures the server address, and registers this PC — no file copying, no editing, no manual steps.
      </p>
      <span class="auto-poll-badge">
        <span class="auto-poll-dot"></span>
        This page auto-refreshes every 15 s — the PC will appear above automatically
      </span>

      <div class="deploy-tabs" style="margin-top:16px">
        <button class="deploy-tab active" onclick="switchTab('win',this)">🪟 Windows</button>
        <button class="deploy-tab"        onclick="switchTab('lin',this)">🐧 Linux</button>
        <button class="deploy-tab"        onclick="switchTab('mac',this)">🍎 macOS</button>
      </div>

    <!-- Windows -->
    <div class="deploy-pane active" id="pane-win">
      <p class="deploy-label" style="margin-top:10px">
        Open <strong>PowerShell as Administrator</strong> on the target PC and paste:
      </p>
      <div class="cmd-box">
        <code id="cmd-win" class="deploy-cmd" data-tpl="powershell -ExecutionPolicy Bypass -Command &quot;(New-Object System.Net.WebClient).DownloadFile('{S}/static/bootstrap_client.ps1','$env:TEMP\itc_boot.ps1'); &amp; '$env:TEMP\itc_boot.ps1'&quot;"></code>
        <button class="copy-btn" onclick="copyCmd('cmd-win',this)">Copy</button>
      </div>
      <p class="deploy-label" style="margin-top:10px">
        Or use the Python one-liner (no PowerShell execution policy needed):
      </p>
      <div class="cmd-box">
        <code id="cmd-win2" class="deploy-cmd" data-tpl="python -c &quot;import urllib.request,os,sys,subprocess; f=open(os.path.join(os.environ['TEMP'],'itc_d.py'),'wb'); f.write(urllib.request.urlopen('{S}/static/deploy_client.py').read()); f.close(); subprocess.call([sys.executable,os.path.join(os.environ['TEMP'],'itc_d.py'),'--server','{S}'])&quot;"></code>
        <button class="copy-btn" onclick="copyCmd('cmd-win2',this)">Copy</button>
      </div>
    </div>

    <!-- Linux -->
    <div class="deploy-pane" id="pane-lin">
      <p class="deploy-label" style="margin-top:10px">
        Open a <strong>terminal</strong> on the target PC and paste:
      </p>
      <div class="cmd-box">
        <code id="cmd-lin" class="deploy-cmd" data-tpl="curl -fsSL {S}/static/bootstrap_client.sh | bash"></code>
        <button class="copy-btn" onclick="copyCmd('cmd-lin',this)">Copy</button>
      </div>
      <p class="deploy-label" style="margin-top:10px">If <code>curl</code> is not available, use <code>wget</code>:</p>
      <div class="cmd-box">
        <code id="cmd-lin2" class="deploy-cmd" data-tpl="wget -qO- {S}/static/bootstrap_client.sh | bash"></code>
        <button class="copy-btn" onclick="copyCmd('cmd-lin2',this)">Copy</button>
      </div>
    </div>

    <!-- macOS -->
    <div class="deploy-pane" id="pane-mac">
      <p class="deploy-label" style="margin-top:10px">
        Open <strong>Terminal</strong> on the target Mac and paste:
      </p>
      <div class="cmd-box">
        <code id="cmd-mac" class="deploy-cmd" data-tpl="curl -fsSL {S}/static/bootstrap_client.sh | bash"></code>
        <button class="copy-btn" onclick="copyCmd('cmd-mac',this)">Copy</button>
      </div>
    </div>

    <p style="margin-top:14px;font-size:11.5px;color:#64748B">
      The command downloads and runs <code>deploy_client.py</code> which installs dependencies,
      patches the server URL, starts the agent silently in the background, and installs a
      startup service so the agent survives reboots — all without any manual steps.
    </p>
  </div>

</main>

<footer><h3 style="color:#ffffff;">ITC System Scanner &nbsp;&bull;&nbsp; Client-Scan Edition v3.0 Build:- 20260618</br> 
					Designed & Developed by :- Central Railway Information Technology Centre, Mumbai CSMT HQ - [psn]</h3></footer>

<script>
// ── Server info bar ───────────────────────────────────────────────────────────
async function loadServerInfo() {
  try {
    const d = await fetch('/api/info').then(r => r.json());
    document.getElementById('ib-host').textContent = d.hostname;
    document.getElementById('ib-ip').textContent   = d.ip;
    document.getElementById('ib-os').textContent   = d.os;
  } catch(e) {}
}
loadServerInfo();
setInterval(() => {
  document.getElementById('ib-time').textContent = new Date().toLocaleTimeString();
}, 1000);
document.getElementById('ib-time').textContent = new Date().toLocaleTimeString();

// ── Detect browser's IP via the server ───────────────────────────────────────
let myClientIp   = null;
let myAgentId    = null;   // set when this browser's IP matches a registered client
let isClientMode = false;  // true = this browser belongs to a registered client PC

async function detectClientIp() {
  try {
    const d = await fetch('/api/my_ip').then(r => r.json());
    myClientIp = d.ip;
    document.getElementById('ib-client-ip').textContent = d.ip;
    updateClientBanner();
  } catch(e) {
    document.getElementById('ib-client-ip').textContent = 'unknown';
  }
}
detectClientIp();

// ── Client banner ─────────────────────────────────────────────────────────────
let registeredClients = [];
let selectedAgentId   = null;

function updateClientBanner() {
  const wrap = document.getElementById('client-banner-wrap');
  if (!myClientIp) { wrap.innerHTML = ''; return; }

  const match = registeredClients.find(c => c.ip === myClientIp);
  if (match) {
    // This browser IS a registered client — switch to client mode
    isClientMode = true;
    myAgentId    = match.agent_id;

    // Show the my-machine section, hide admin list
    document.getElementById('my-machine-section').style.display = '';
    document.getElementById('my-machine-label').textContent =
      `${match.hostname}  (${match.ip})`;
    document.getElementById('admin-clients-card').style.display = 'none';

    // Auto-select this client for scan controls
    selectClient(match.agent_id, match.ip, match.hostname);
    wrap.innerHTML = '';
  } else {
    // Admin / unregistered view
    isClientMode = false;
    myAgentId    = null;
    document.getElementById('my-machine-section').style.display = 'none';
    document.getElementById('admin-clients-card').style.display = '';

    const serverUrl = window.location.origin;
    const isWin = navigator.userAgentData
      ? navigator.userAgentData.platform.toLowerCase().includes('win')
      : navigator.userAgent.toLowerCase().includes('win');

    if (isWin) {
      wrap.innerHTML = `
        <div class="client-banner not-registered" style="flex-direction:column;align-items:flex-start;gap:12px">
          <div style="display:flex;align-items:center;gap:10px">
            <span style="color:#EF4444;font-size:20px">✗</span>
            <span>Your PC (<strong>${myClientIp}</strong>) does not have the agent installed yet.</span>
          </div>
          <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
            <a href="${serverUrl}/install/windows" id="install-btn"
               style="display:inline-flex;align-items:center;gap:8px;background:#4F6EF7;color:#fff;
                      font-weight:700;font-size:14px;padding:11px 24px;border-radius:8px;
                      text-decoration:none;border:none;cursor:pointer">
              ⬇&nbsp; Download &amp; Install Agent
            </a>
            <div style="font-size:12px;color:#94A3B8;line-height:1.6">
              <strong style="color:#E2E8F0">How to use:</strong><br>
              1. Click the button — a <code style="background:#0B0F1A;padding:1px 5px;border-radius:3px;color:#7B93FF">.bat</code> file downloads<br>
              2. Double-click it &amp; click <em>Run</em> when prompted<br>
              3. This page registers your PC automatically ✓
            </div>
          </div>
          <div style="font-size:10.5px;color:#64748B">
            ✓ Installs silently &nbsp;·&nbsp; ✓ No terminal needed &nbsp;·&nbsp;
            ✓ Survives reboots &nbsp;·&nbsp; ✓ This page refreshes automatically
          </div>
        </div>`;
    } else {
      wrap.innerHTML = `
        <div class="client-banner not-registered" style="flex-direction:column;align-items:flex-start;gap:12px">
          <div style="display:flex;align-items:center;gap:10px">
            <span style="color:#EF4444;font-size:20px">✗</span>
            <span>Your PC (<strong>${myClientIp}</strong>) does not have the agent installed yet.</span>
          </div>
          <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
            <a href="${serverUrl}/install/linux" id="install-btn"
               style="display:inline-flex;align-items:center;gap:8px;background:#4F6EF7;color:#fff;
                      font-weight:700;font-size:14px;padding:11px 24px;border-radius:8px;
                      text-decoration:none">
              ⬇&nbsp; Download &amp; Install Agent
            </a>
            <div style="font-size:12px;color:#94A3B8;line-height:1.6">
              <strong style="color:#E2E8F0">How to use:</strong><br>
              1. Click the button — a <code style="background:#0B0F1A;padding:1px 5px;border-radius:3px;color:#7B93FF">.sh</code> file downloads<br>
              2. Open a terminal and run: <code style="background:#0B0F1A;padding:1px 5px;border-radius:3px;color:#7B93FF">bash ~/Downloads/itc_install.sh</code><br>
              3. This page registers your PC automatically ✓
            </div>
          </div>
          <div style="font-size:10.5px;color:#64748B">
            ✓ Installs silently &nbsp;·&nbsp; ✓ Survives reboots &nbsp;·&nbsp; ✓ This page refreshes automatically
          </div>
        </div>`;
    }
  }
}

// ── Agent Start / Stop (client self-control + admin remote control) ────────────
async function agentAction(action, agentId, _unused, pillEl, msgEl) {
  const aid  = agentId || myAgentId;
  const lbl  = msgEl  || document.getElementById('my-agent-msg');
  const pill = pillEl || document.getElementById('my-agent-pill');

  if (!aid) { if (lbl) lbl.textContent = 'No agent selected.'; return; }

  const startBtn = agentId
    ? document.getElementById(`start-btn-${agentId}`)
    : document.getElementById('my-start-btn');
  const stopBtn  = agentId
    ? document.getElementById(`stop-btn-${agentId}`)
    : document.getElementById('my-stop-btn');

  if (startBtn) startBtn.disabled = true;
  if (stopBtn)  stopBtn.disabled  = true;
  if (lbl)      lbl.textContent   = action === 'start' ? 'Starting agent…' : 'Stopping agent…';

  try {
    const res = await fetch(`/api/${action}_agent/${aid}`, { method: 'POST' });
    const d   = await res.json();
    if (d.error) {
      if (lbl) lbl.textContent = '⚠ ' + d.error;
    } else {
      if (lbl) lbl.textContent = action === 'start' ? '✓ Agent started.' : '✓ Agent stopped.';
      if (pill) {
        pill.className   = action === 'start' ? 'agent-pill running' : 'agent-pill stopped';
        pill.textContent = action === 'start' ? '● Running' : '● Stopped';
      }
      setTimeout(loadClients, 1500);
    }
  } catch(e) {
    if (lbl) lbl.textContent = '⚠ ' + String(e);
  } finally {
    if (startBtn) startBtn.disabled = false;
    if (stopBtn)  stopBtn.disabled  = false;
  }
}

async function loadClients() {
  try {
    const d = await fetch('/api/clients').then(r => r.json());
    registeredClients = d.clients || [];
    if (registeredClients.length > 0) {
      appendLog([`[${new Date().toLocaleTimeString()}] Server connected — ${registeredClients.length} client PC(s) registered.`]);
    } else {
      appendLog([`[${new Date().toLocaleTimeString()}] Server connected — no client PCs registered yet. Deploy the agent to begin.`]);
    }
    renderClientList(registeredClients);
    updateClientBanner();
  } catch(e) {
    appendLog([`[ERROR] Could not reach server: ${e}`]);
  }
}
loadClients();
setInterval(loadClients, 15051);

function renderClientList(clients) {
  const el   = document.getElementById('client-list');
  const hint = document.getElementById('setup-hint');
  if (!clients.length) {
    el.innerHTML = '<span style="color:#94A3B8;font-size:13px;padding:8px 0">No client PCs registered yet. Use the Deploy panel above.</span>';
    hint.style.display = '';
    return;
  }
  hint.style.display = 'none';
  el.innerHTML = clients.map(c => `
    <div class="client-row" id="row-${c.agent_id}">
      <span class="dot"></span>
      <div class="info">
        <strong>${c.hostname}</strong>
        <small>${c.ip}  &bull;  port ${c.port}  &bull;  last seen ${c.last_seen_ago}s ago</small>
      </div>
      <div class="actions">
        <button class="btn btn-success btn-small" id="start-btn-${c.agent_id}"
          onclick="agentAction('start','${c.agent_id}',null,
            document.querySelector('#row-${c.agent_id} .agent-pill'),
            document.getElementById('msg-${c.agent_id}'))"
          title="Start agent on ${c.hostname}">
          ▶ Start Agent
        </button>
        <button class="btn btn-danger btn-small" id="stop-btn-${c.agent_id}"
          onclick="agentAction('stop','${c.agent_id}',null,
            document.querySelector('#row-${c.agent_id} .agent-pill'),
            document.getElementById('msg-${c.agent_id}'))"
          title="Stop agent on ${c.hostname}">
          ■ Stop Agent
        </button>
        <button class="btn btn-primary btn-small"
          onclick="selectClient('${c.agent_id}','${c.ip}','${c.hostname}')">
          🔍 Select &amp; Scan
        </button>
        <span class="agent-pill running">● Running</span>
        <span id="msg-${c.agent_id}" style="color:#94A3B8;font-size:11px;"></span>
      </div>
    </div>`).join('');
}

// Client-mode: click "Scan This PC" on own machine card
function selectAndScanMine() {
  if (!myAgentId) return;
  const match = registeredClients.find(c => c.agent_id === myAgentId);
  if (match) selectClient(match.agent_id, match.ip, match.hostname);
  document.getElementById('scan-card').scrollIntoView({behavior:'smooth'});
}

async function selectClient(agentId, ip, hostname) {
  selectedAgentId = agentId;
  document.getElementById('scan-target-label').textContent = `${hostname}  (${ip})`;
  document.getElementById('scan-btn').disabled = false;

  const viewBtn = document.getElementById('view-pdf-btn');
  viewBtn.disabled = true;
  viewBtn.style.opacity = '0.5';
  viewBtn.style.cursor = 'not-allowed';

  document.getElementById('status-msg').textContent = 'Checking report history...';
  document.getElementById('log').textContent = '';
  appendLog([`[${new Date().toLocaleTimeString()}] Target selected: ${hostname} (${ip})`, 'Checking scan history...']);

  document.getElementById('result-banner').classList.remove('visible');
  document.getElementById('scan-complete-banner').classList.remove('visible');
  _currentPdfUrl = '';

  const dlPdf  = document.getElementById('dl-pdf');
  const dlJson = document.getElementById('dl-json');
  dlPdf.disabled  = true;  dlPdf.dataset.href  = '#';
  dlJson.disabled = true;  dlJson.dataset.href = '#';
  document.getElementById('dl-pdf2').dataset.href  = '#';
  document.getElementById('dl-json2').style.display = 'none';
  setDot('');

  try {
    const d = await fetch(`/api/client_result/${agentId}`).then(r => r.json());
    if (d.ok && d.pdf_name) {
      viewBtn.disabled = false;
      viewBtn.style.opacity = '1';
      viewBtn.style.cursor = 'pointer';
      _currentPdfUrl = `/api/client_download/${agentId}/pdf?inline=1`;
      document.getElementById('status-msg').textContent = '✓ Previous scan report loaded and ready to view.';
      document.getElementById('complete-filename').textContent = `📄 ${d.pdf_name}  —  Previous report loaded.`;
      document.getElementById('dl-pdf2').dataset.href = `/api/client_download/${agentId}/pdf`;
      document.getElementById('scan-complete-banner').classList.add('visible');
    } else {
      document.getElementById('status-msg').textContent = `Ready to scan ${hostname} (${ip}). Click Scan.`;
    }
  } catch(e) {
    document.getElementById('status-msg').textContent = `Ready to scan ${hostname} (${ip}). Click Scan.`;
  }
}

// ── Scan ──────────────────────────────────────────────────────────────────────
let pollTimer         = null;
let refreshTimer      = null;
let logOffset         = 0;
let resultCheckCount  = 0;
let consecutiveErrors = 0;

function setDot(state) { document.getElementById('status-dot').className = state; }

function openRegistration() {
  window.location.href = 'http://10.31.3.114:5173/';
}

async function startScan() {
  if (!selectedAgentId) { alert('Please select a client PC first.'); return; }

  const btn = document.getElementById('scan-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Scanning…';

  const viewBtn = document.getElementById('view-pdf-btn');
  viewBtn.disabled = true;
  viewBtn.style.opacity = '0.5';
  viewBtn.style.cursor = 'not-allowed';

  document.getElementById('log').textContent = '';
  document.getElementById('scan-progress-wrap').style.display = 'block';
  document.getElementById('result-banner').classList.remove('visible');
  document.getElementById('scan-complete-banner').classList.remove('visible');

  const dlPdf  = document.getElementById('dl-pdf');
  const dlJson = document.getElementById('dl-json');
  dlPdf.disabled  = true;  dlPdf.dataset.href  = '#';
  dlJson.disabled = true;  dlJson.dataset.href = '#';
  document.getElementById('status-msg').textContent = 'Scan running on client PC…';
  logOffset = 0; resultCheckCount = 0; consecutiveErrors = 0;
  setDot('scanning');

  try {
    const queryData = new URLSearchParams(window.location.search).get('data');
    const scanUrl = queryData
      ? `/api/scan_client/${selectedAgentId}?data=${encodeURIComponent(queryData)}`
      : `/api/scan_client/${selectedAgentId}`;
    const d = await fetch(scanUrl, { method: 'POST' }).then(r => r.json());
    if (d.error) { onScanError(d.error); return; }
    clearInterval(pollTimer);
    pollTimer = setInterval(pollClientLog, 1500);
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      appendLog(["[INFO] Interval limit reached. Re-synchronizing page..."]);
      setTimeout(() => { window.location.reload(); }, 1200);
    }, 120000);
    appendLog(["[INFO] Diagnostic pipeline triggered. Safety auto-refresh established for T+2m."]);
  } catch(e) { onScanError(String(e)); }
}

function appendLog(lines) {
  const el = document.getElementById('log');
  lines.forEach(line => {
    const span = document.createElement('span');
    if      (/\[ERROR\]|Scan failed/i.test(line)) span.className = 'log-err';
    else if (/Done\.|complete|PDF saved|JSON saved|Uploaded/i.test(line)) span.className = 'log-ok';
    else if (/={5,}/.test(line)) span.className = 'log-head';
    else if (/WARNING|warn/i.test(line)) span.className = 'log-warn';
    span.textContent = line + '\n';
    el.appendChild(span);
  });
  el.scrollTop = el.scrollHeight;
}

function pollClientLog() {
  fetch(`/api/client_status/${selectedAgentId}?offset=${logOffset}`)
    .then(r => r.json())
    .then(d => {
      if (d.log && d.log.length) { appendLog(d.log); logOffset = d.total_lines; }
      if (d.status === 'done') {
        clearInterval(pollTimer); pollTimer = null; setDot('done');
        _currentPdfUrl = `/api/client_download/${selectedAgentId}/pdf?inline=1`;
        document.getElementById('scan-progress-wrap').style.display = 'none';
        document.getElementById('scan-btn').disabled = false;
        document.getElementById('scan-btn').textContent = '▶  Scan This PC';
        document.getElementById('view-pdf-btn').disabled = false;
        document.getElementById('view-pdf-btn').style.opacity = '1';
        document.getElementById('view-pdf-btn').style.cursor = 'pointer';
        document.getElementById('scan-complete-banner').classList.add('visible');
        checkForResultSilent();
      } else if (d.status === 'error') {
        clearInterval(pollTimer);
        onScanError(d.error || 'Scan reported an error on the client agent.');
      }
      consecutiveErrors = 0;
    })
    .catch(err => {
      consecutiveErrors++;
      appendLog([`[WARN] Log poll failed (${consecutiveErrors}): ${err}`]);
      if (consecutiveErrors >= 5) { clearInterval(pollTimer); onScanError('Lost contact with agent after 5 consecutive failures.'); }
    });
}

function checkForResultSilent() {
  resultCheckCount++;
  fetch(`/api/client_result/${selectedAgentId}`)
    .then(r => r.json())
    .then(d => {
      if (d.ok && d.pdf_name) {
        _currentPdfUrl = `/api/client_download/${selectedAgentId}/pdf?inline=1`;
        document.getElementById('view-pdf-btn').disabled = false;
        document.getElementById('view-pdf-btn').style.opacity = '1';
        document.getElementById('view-pdf-btn').style.cursor = 'pointer';
        document.getElementById('complete-filename').textContent =
          `📄 ${d.pdf_name}  —  Click "View PDF" to open or "Download PDF" to save.`;
        document.getElementById('pdf-modal-title').textContent = `📄 ${d.pdf_name}`;
        document.getElementById('dl-pdf2').dataset.href = `/api/client_download/${selectedAgentId}/pdf`;
        const dlJson2 = document.getElementById('dl-json2');
        if (d.json_name) { dlJson2.dataset.href = `/api/client_download/${selectedAgentId}/json`; dlJson2.style.display = ''; }
      } else if (resultCheckCount < 30) { setTimeout(checkForResultSilent, 2000); }
    })
    .catch(() => {});
}

let _currentPdfUrl = '';

function openPdfViewer() {
  if (!_currentPdfUrl) return;
  document.getElementById('pdf-iframe').src = _currentPdfUrl;
  document.getElementById('pdf-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closePdfViewer() {
  document.getElementById('pdf-modal').classList.remove('open');
  document.getElementById('pdf-iframe').src = 'about:blank';
  document.body.style.overflow = '';
}

document.getElementById('pdf-modal').addEventListener('click', function(e) {
  if (e.target === this) closePdfViewer();
});

function onScanError(msg) {
  clearInterval(pollTimer);
  document.getElementById('scan-progress-wrap').style.display = 'none';
  document.getElementById('status-msg').textContent = 'Error: ' + msg;
  const btn = document.getElementById('scan-btn');
  btn.disabled = false;
  btn.textContent = '▶ Scan This PC';
  setDot('error');
}

// ── Auto-deploy panel helpers ─────────────────────────────────────────────────
function injectServerUrl() {
  const url = window.location.origin;
  document.querySelectorAll('code.deploy-cmd[data-tpl]').forEach(el => {
    el.textContent = el.dataset.tpl.replaceAll('{S}', url);
  });
}
injectServerUrl();
document.addEventListener('DOMContentLoaded', injectServerUrl);

function switchTab(os, btn) {
  document.querySelectorAll('.deploy-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.deploy-pane').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pane-' + os).classList.add('active');
}

function toggleDeployCard() {
  const c = document.getElementById('deploy-content');
  const lbl = document.getElementById('deploy-toggle-label');
  if (c.style.display === 'none') {
    c.style.display = ''; lbl.textContent = '▲ click to collapse';
  } else {
    c.style.display = 'none'; lbl.textContent = '▼ click to expand';
  }
}

function copyCmd(codeId, btn) {
  const el   = document.getElementById(codeId);
  const text = el ? el.textContent : '';
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = '✓ Copied'; btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    btn.textContent = '✓ Copied';
    setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
  });
}

function copyBannerCmd() {
  const el  = document.getElementById('banner-cmd');
  const btn = document.getElementById('banner-copy-btn');
  const text = el ? el.textContent : '';
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = '✓ Copied!'; btn.style.background = '#22C55E';
    setTimeout(() => { btn.textContent = '📋 Copy'; btn.style.background = '#2D3561'; }, 2000);
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    btn.textContent = '✓ Copied!';
    setTimeout(() => { btn.textContent = '📋 Copy'; }, 2000);
  });
}
</script>
</body>
</html>
"""


# =============================================================================
# Routes
# =============================================================================

@app.route("/")
def index():
    return render_template_string(HTML)


# =============================================================================
# Static auto-deploy file serving
# =============================================================================

import re as _re

_DEPLOY_CLIENT_PY = r'''#!/usr/bin/env python3
"""
deploy_client.py  -  ITC System Scanner  (Zero-Touch Client Auto-Deployer)
Run with:  python deploy_client.py --server http://<server-ip>:5001
"""
import os, sys, json, time, shutil, socket, argparse, platform
import subprocess, threading, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime

DEFAULT_SERVER_URL = "http://10.31.3.114:5001"
AGENT_PORT   = 7979
SECRET_TOKEN = "ITC2024CLIENT"
INSTALL_DIR  = Path.home() / "itc_agent"
SERVICE_NAME = "itc-client-agent"
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"
IS_MAC     = platform.system() == "Darwin"

def log(msg): ts=datetime.now().strftime("%H:%M:%S"); print(f"[{ts}] {msg}")
def run(cmd, **kw): return subprocess.run(cmd, check=True, **kw)

def get_local_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80))
        ip=s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

def http_get(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return r.read()

def http_post_json(url, payload, token, timeout=15):
    data=json.dumps(payload).encode()
    req=urllib.request.Request(url,data=data,method="POST",
        headers={"Content-Type":"application/json","X-Token":token})
    with urllib.request.urlopen(req,timeout=timeout) as r: return json.loads(r.read())

def python_exe(): return sys.executable

def step(n, total, msg): log(f"\n[{n}/{total}] {msg}")

def verify_server(url):
    step(1,6,"Verifying server is reachable")
    try:
        d=json.loads(http_get(f"{url}/api/info",timeout=10))
        log(f"  OK  Server: {d.get('hostname')} ({d.get('ip')}) {d.get('os')}")
    except Exception as e:
        log(f"  ERROR  Cannot reach {url}: {e}"); sys.exit(1)

def download_files(url):
    step(2,6,"Downloading agent files from server")
    INSTALL_DIR.mkdir(parents=True,exist_ok=True)
    for fname in ("client_agent.py","app_inventory.py"):
        dest=INSTALL_DIR/fname
        try:
            content=http_get(f"{url}/static/{fname}",timeout=30)
            dest.write_bytes(content)
            log(f"  OK  {fname} ({len(content):,} bytes)")
        except Exception as e:
            local=Path(__file__).parent/fname
            if local.exists(): shutil.copy2(local,dest); log(f"  WARN  Used local copy of {fname}")
            else: log(f"  ERROR  Cannot get {fname}: {e}"); sys.exit(1)

def patch_server_url(url):
    step(3,6,"Patching SERVER_URL in client_agent.py")
    import re
    p=INSTALL_DIR/"client_agent.py"; t=p.read_text(encoding="utf-8")
    patched=re.sub(r'^(SERVER_URL\s*=\s*)["\'].*?["\']',rf'\g<1>"{url}"',t,flags=re.MULTILINE)
    if patched==t: patched=f'SERVER_URL="{url}"\n'+t
    p.write_text(patched,encoding="utf-8")
    log(f"  OK  SERVER_URL -> {url}")

def install_deps():
    step(4,6,"Installing Python dependencies (requests, reportlab)")
    # ── Ensure pip is available ───────────────────────────────────────────────
    try:
        import ensurepip
        ensurepip.bootstrap(upgrade=True)
    except Exception:
        pass
    for pkg in ["requests", "reportlab"]:
        try:
            __import__(pkg.replace("-","_"))
            log(f"  OK  {pkg} already installed")
        except ImportError:
            log(f"  ... Installing {pkg}")
            try:
                run([python_exe(),"-m","pip","install",pkg,"--quiet"])
                log(f"  OK  {pkg} installed")
            except Exception as e:
                log(f"  WARN  pip install {pkg} failed: {e}")

def kill_existing():
    pid_file=INSTALL_DIR/"agent.pid"
    if pid_file.exists():
        try:
            pid=int(pid_file.read_text().strip())
            if IS_WINDOWS: subprocess.run(["taskkill","/PID",str(pid),"/F"],capture_output=True,check=False)
            else: os.kill(pid,9)
        except: pass
        pid_file.unlink(missing_ok=True)

def start_agent():
    step(5,6,"Starting client agent in background")
    kill_existing()
    agent=INSTALL_DIR/"client_agent.py"
    lf=open(INSTALL_DIR/"agent.log","a")
    if IS_WINDOWS:
        p=subprocess.Popen([python_exe(),str(agent)],cwd=str(INSTALL_DIR),stdout=lf,stderr=lf,
            creationflags=0x00000008|0x08000000,close_fds=True)
    else:
        p=subprocess.Popen([python_exe(),str(agent)],cwd=str(INSTALL_DIR),stdout=lf,stderr=lf,
            start_new_session=True)
    (INSTALL_DIR/"agent.pid").write_text(str(p.pid))
    time.sleep(4)
    if p.poll() is not None: log(f"  ERROR  Agent exited (code {p.returncode})"); return False
    log(f"  OK  Agent running (PID {p.pid})"); return True

def install_service(url):
    step(6,6,"Installing startup service (auto-start on reboot)")
    agent=INSTALL_DIR/"client_agent.py"
    if IS_WINDOWS:
        xml=(INSTALL_DIR/f"{SERVICE_NAME}.xml")
        xml.write_text(f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><BootTrigger><Enabled>true</Enabled><Delay>PT10S</Delay></BootTrigger>
  <LogonTrigger><Enabled>true</Enabled><Delay>PT5S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>true</Enabled>
  <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure></Settings>
  <Actions><Exec><Command>{python_exe()}</Command>
  <Arguments>"{agent}"</Arguments><WorkingDirectory>{INSTALL_DIR}</WorkingDirectory>
  </Exec></Actions></Task>""", encoding="utf-16")
        try:
            run(["schtasks","/Create","/TN",SERVICE_NAME,"/XML",str(xml),"/F"],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            log(f"  OK  Task Scheduler task '{SERVICE_NAME}' created")
        except:
            try:
                import winreg
                k=winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",0,winreg.KEY_SET_VALUE)
                winreg.SetValueEx(k,SERVICE_NAME,0,winreg.REG_SZ,f'"{python_exe()}" "{agent}"')
                log("  OK  Fallback: Registry Run key added")
            except Exception as e2: log(f"  WARN  Service install failed: {e2}")
    elif IS_LINUX:
        unit=f"""[Unit]\nDescription=ITC Client Agent\nAfter=network-online.target\n\n[Service]\nType=simple\nExecStart={python_exe()} {agent}\nWorkingDirectory={INSTALL_DIR}\nRestart=on-failure\nRestartSec=10\nStandardOutput=append:{INSTALL_DIR}/agent.log\nStandardError=append:{INSTALL_DIR}/agent.log\n\n[Install]\nWantedBy=multi-user.target\n"""
        installed=False
        if os.geteuid()==0:
            try:
                Path(f"/etc/systemd/system/{SERVICE_NAME}.service").write_text(unit)
                run(["systemctl","daemon-reload"],capture_output=True,check=False)
                run(["systemctl","enable","--now",SERVICE_NAME],capture_output=True,check=False)
                log("  OK  systemd system service installed"); installed=True
            except: pass
        if not installed:
            try:
                ud=Path.home()/".config/systemd/user"; ud.mkdir(parents=True,exist_ok=True)
                (ud/f"{SERVICE_NAME}.service").write_text(unit)
                run(["systemctl","--user","daemon-reload"],capture_output=True,check=False)
                run(["systemctl","--user","enable","--now",SERVICE_NAME],capture_output=True,check=False)
                run(["loginctl","enable-linger",os.environ.get("USER","")],capture_output=True,check=False)
                log("  OK  systemd user service installed"); installed=True
            except: pass
        if not installed:
            try:
                res=subprocess.run(["crontab","-l"],capture_output=True,text=True)
                cur=res.stdout if res.returncode==0 else ""
                entry=f"@reboot {python_exe()} {agent} >> {INSTALL_DIR}/agent.log 2>&1"
                if entry not in cur:
                    p2=subprocess.Popen(["crontab","-"],stdin=subprocess.PIPE,text=True)
                    p2.communicate(cur.rstrip("\n")+f"\n{entry}\n")
                log("  OK  @reboot crontab entry added")
            except Exception as e: log(f"  WARN  Crontab fallback failed: {e}")
    elif IS_MAC:
        ld=Path.home()/"Library/LaunchAgents"; ld.mkdir(parents=True,exist_ok=True)
        pl=ld/f"com.itc.{SERVICE_NAME}.plist"
        pl.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.itc.{SERVICE_NAME}</string>
  <key>ProgramArguments</key><array><string>{python_exe()}</string><string>{agent}</string></array>
  <key>WorkingDirectory</key><string>{INSTALL_DIR}</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{INSTALL_DIR}/agent.log</string>
  <key>StandardErrorPath</key><string>{INSTALL_DIR}/agent.log</string>
</dict></plist>""")
        try:
            subprocess.run(["launchctl","unload",str(pl)],capture_output=True,check=False)
            run(["launchctl","load",str(pl)],capture_output=True)
            log("  OK  macOS LaunchAgent installed")
        except Exception as e: log(f"  WARN  LaunchAgent failed: {e}")

def verify_registration(url):
    log("\n  Waiting for registration with server...")
    ip=get_local_ip(); hn=socket.gethostname(); deadline=time.time()+20
    while time.time()<deadline:
        try:
            d=json.loads(http_get(f"{url}/api/clients",timeout=5))
            for c in d.get("clients",[]):
                if c.get("ip")==ip or c.get("hostname")==hn:
                    log(f"  OK  Registered: {c['hostname']} ({c['ip']})"); return True
        except: pass
        time.sleep(2)
    log("  WARN  Not yet visible — agent may still be starting up")
    return False

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--server",default=DEFAULT_SERVER_URL)
    ap.add_argument("--no-service",action="store_true"); args=ap.parse_args()
    url=args.server.rstrip("/")
    print("="*60)
    print(f"  ITC Zero-Touch Client Deployer"); print(f"  Server: {url}")
    print(f"  This PC: {socket.gethostname()} ({get_local_ip()})")
    print("="*60)
    verify_server(url); download_files(url); patch_server_url(url)
    install_deps(); ok=start_agent()
    if not args.no_service: install_service(url)
    if ok: verify_registration(url)
    log(f"\n  Done. Open {url} — this PC should appear in the list.")

if __name__=="__main__": main()
'''

_BOOTSTRAP_PS1 = r'''# bootstrap_client.ps1 - ITC Zero-Touch Windows Deployer
param([string]$Server = "http://10.31.3.114:5001")
$ErrorActionPreference = "Continue"
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  ITC System Scanner - Windows Auto-Deploy  " -ForegroundColor Cyan
Write-Host "  Server: $Server" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Detect Python ────────────────────────────────────────────────────
$python = $null
foreach ($c in @("python","python3","py")) {
  try {
    $v = & $c --version 2>&1
    if ($v -match "Python 3") { $python=$c; Write-Host "[OK] Found: $v" -ForegroundColor Green; break }
  } catch {}
}

# ── Step 2: Auto-install Python if missing ───────────────────────────────────
if (-not $python) {
  Write-Host "[INFO] Python 3 not found. Downloading and installing automatically ..." -ForegroundColor Yellow
  Write-Host "       This may take 2-3 minutes. Please wait." -ForegroundColor Yellow
  Write-Host ""
  $pyVer     = "3.11.9"
  $pyUrl     = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-amd64.exe"
  $pyInst    = "$env:TEMP\python-$pyVer-amd64.exe"
  try {
    Write-Host "[...] Downloading Python $pyVer ..." -ForegroundColor Yellow
    (New-Object System.Net.WebClient).DownloadFile($pyUrl, $pyInst)
    Write-Host "[OK] Downloaded Python installer." -ForegroundColor Green
  } catch {
    Write-Host "[ERROR] Could not download Python: $($_)" -ForegroundColor Red
    Write-Host "        Install manually: https://www.python.org/downloads/" -ForegroundColor Yellow
    exit 1
  }
  Write-Host "[...] Installing Python silently ..." -ForegroundColor Yellow
  $proc = Start-Process -FilePath $pyInst `
    -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_launcher=1" `
    -Wait -PassThru
  if ($proc.ExitCode -notin @(0,3010)) {
    Write-Host "[ERROR] Python installer failed (exit $($proc.ExitCode))." -ForegroundColor Red
    Write-Host "        Try running PowerShell as Administrator." -ForegroundColor Yellow
    exit 1
  }
  Write-Host "[OK] Python $pyVer installed." -ForegroundColor Green
  # Refresh PATH for this session
  $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
              [System.Environment]::GetEnvironmentVariable("Path","User")
  # Re-detect
  foreach ($c in @("python","python3","py")) {
    try {
      $v = & $c --version 2>&1
      if ($v -match "Python 3") { $python=$c; Write-Host "[OK] Confirmed: $v" -ForegroundColor Green; break }
    } catch {}
  }
  if (-not $python) {
    Write-Host "[WARN] Python installed but not on PATH yet." -ForegroundColor Yellow
    Write-Host "       Please close this window and re-run the command in a new PowerShell." -ForegroundColor Yellow
    exit 0
  }
}

# ── Step 3: Download and run deployer ────────────────────────────────────────
Write-Host "[...] Downloading deployer from $Server ..." -ForegroundColor Yellow
$f = "$env:TEMP\itc_deploy_client.py"
try {
  (New-Object System.Net.WebClient).DownloadFile("$Server/static/deploy_client.py", $f)
  Write-Host "[OK] Downloaded deploy_client.py" -ForegroundColor Green
} catch {
  Write-Host "[ERROR] Download failed: $($_)" -ForegroundColor Red
  Write-Host "        Is web_app.py running on $Server ?" -ForegroundColor Yellow
  exit 1
}
Write-Host "[...] Running deployer ..." -ForegroundColor Yellow
Write-Host ""
& $python $f --server $Server
if ($LASTEXITCODE -eq 0) {
  Write-Host ""
  Write-Host "[OK] Done! Open $Server in a browser to scan this PC." -ForegroundColor Green
} else {
  Write-Host "[ERROR] Deployer exited with code $LASTEXITCODE" -ForegroundColor Red
}
'''

_BOOTSTRAP_SH = r'''#!/bin/bash
# bootstrap_client.sh - ITC Zero-Touch Linux/macOS Deployer
SERVER="http://10.31.3.114:5001"
while [[ $# -gt 0 ]]; do case $1 in --server) SERVER="$2"; shift 2;; *) shift;; esac; done
echo ""
echo "============================================"
echo "  ITC System Scanner - Linux/macOS Auto-Deploy"
echo "  Server: $SERVER"
echo "============================================"
echo ""

# ── Step 1: Detect Python ────────────────────────────────────────────────────
PYTHON=""
for c in python3 python python3.12 python3.11 python3.10; do
  if command -v "$c" &>/dev/null; then
    VER=$("$c" --version 2>&1)
    if echo "$VER" | grep -q "Python 3"; then
      PYTHON="$c"
      echo "[OK] Found: $VER"
      break
    fi
  fi
done

# ── Step 2: Auto-install Python if missing ───────────────────────────────────
if [ -z "$PYTHON" ]; then
  echo "[INFO] Python 3 not found. Installing automatically ..."
  echo ""
  OS_TYPE="$(uname -s)"

  if [ "$OS_TYPE" = "Linux" ]; then
    if command -v apt-get &>/dev/null; then
      echo "[...] Detected Debian/Ubuntu — using apt-get ..."
      sudo apt-get update -y && sudo apt-get install -y python3 python3-pip python3-venv
    elif command -v dnf &>/dev/null; then
      echo "[...] Detected Fedora/RHEL — using dnf ..."
      sudo dnf install -y python3 python3-pip
    elif command -v yum &>/dev/null; then
      echo "[...] Detected CentOS/RHEL 7 — using yum ..."
      sudo yum install -y python3 python3-pip
    elif command -v zypper &>/dev/null; then
      echo "[...] Detected openSUSE — using zypper ..."
      sudo zypper install -y python3 python3-pip
    elif command -v pacman &>/dev/null; then
      echo "[...] Detected Arch Linux — using pacman ..."
      sudo pacman -Sy --noconfirm python python-pip
    else
      echo "[ERROR] No supported package manager found."
      echo "        Please install Python 3 manually: https://www.python.org/downloads/"
      exit 1
    fi

  elif [ "$OS_TYPE" = "Darwin" ]; then
    if command -v brew &>/dev/null; then
      echo "[...] Detected macOS with Homebrew — installing python3 ..."
      brew install python3
    else
      echo "[...] Homebrew not found. Downloading official Python PKG ..."
      PYVER="3.11.9"
      PYPKG="/tmp/python-${PYVER}-macos11.pkg"
      PYURL="https://www.python.org/ftp/python/${PYVER}/python-${PYVER}-macos11.pkg"
      if command -v curl &>/dev/null; then
        curl -fsSL "$PYURL" -o "$PYPKG"
      elif command -v wget &>/dev/null; then
        wget -qO "$PYPKG" "$PYURL"
      else
        echo "[ERROR] Neither curl nor wget found. Cannot download Python."
        echo "        Install Python manually: https://www.python.org/downloads/"
        exit 1
      fi
      echo "[...] Installing Python PKG (you may be prompted for password) ..."
      sudo installer -pkg "$PYPKG" -target /
    fi
  else
    echo "[ERROR] Unsupported OS: $OS_TYPE"
    exit 1
  fi

  # Re-detect after install
  for c in python3 python python3.12 python3.11 python3.10; do
    if command -v "$c" &>/dev/null; then
      VER=$("$c" --version 2>&1)
      if echo "$VER" | grep -q "Python 3"; then
        PYTHON="$c"
        echo "[OK] Python installed and confirmed: $VER"
        break
      fi
    fi
  done

  if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python install appeared to succeed but Python 3 is still not on PATH."
    echo "        Please open a new terminal and re-run this script."
    exit 1
  fi
fi

# ── Step 3: Download and run the deployer ────────────────────────────────────
echo "[...] Downloading deployer from $SERVER ..."
F="/tmp/itc_deploy_client_$$.py"
if command -v curl &>/dev/null; then
  curl -fsSL "$SERVER/static/deploy_client.py" -o "$F" || { echo "[ERROR] curl download failed"; exit 1; }
elif command -v wget &>/dev/null; then
  wget -qO "$F" "$SERVER/static/deploy_client.py" || { echo "[ERROR] wget download failed"; exit 1; }
else
  echo "[ERROR] Neither curl nor wget found. Install one and retry."
  exit 1
fi
echo "[OK] Downloaded deploy_client.py"
echo "[...] Running deployer ..."
echo ""
$PYTHON "$F" --server "$SERVER"
echo ""
echo "[OK] Done! Open $SERVER in a browser to scan this PC."
'''

_INSTALL_BAT = r'''@echo off
setlocal enabledelayedexpansion
set SERVER=__SERVER_URL__
echo.
echo ============================================
echo   ITC System Scanner - Windows Auto-Install
echo   Server: %SERVER%
echo ============================================
echo.

:: ── Step 1: Check if pre-built EXE is available from server ──────────────────
echo [...] Checking for pre-built EXE from server ...
set EXEFILE=%TEMP%\itc_client_agent.exe
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%SERVER%/static/client_agent.exe' -OutFile '%EXEFILE%' -UseBasicParsing -ErrorAction Stop; Write-Host '[OK] EXE downloaded.' } catch { Write-Host '[INFO] No pre-built EXE found, will use Python installer.' }"

if exist "%EXEFILE%" (
    echo [OK] Found pre-built EXE. Running agent directly ^(no Python needed^) ...
    echo.
    start "" "%EXEFILE%"
    echo [OK] Agent started. Open %SERVER% in a browser to scan this PC.
    timeout /t 5
    goto :end
)

:: ── Step 2: Look for Python 3 ─────────────────────────────────────────────────
echo [...] Looking for Python 3 on this PC ...
set PYTHON=
where python >nul 2>nul && (python --version 2>&1 | findstr /i "Python 3" >nul && set PYTHON=python)
if not defined PYTHON (where py >nul 2>nul && (py --version 2>&1 | findstr /i "Python 3" >nul && set PYTHON=py))
if not defined PYTHON (where python3 >nul 2>nul && set PYTHON=python3)

:: ── Step 3: Auto-install Python if not found ─────────────────────────────────
if not defined PYTHON (
    echo.
    echo [INFO] Python 3 not found. Downloading and installing automatically ...
    echo        This may take 2-3 minutes. Please wait.
    echo.

    set PYVER=3.11.9
    set PYINSTALLER=%TEMP%\python-3.11.9-amd64.exe
    set PYURL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

    echo [...] Downloading Python 3.11.9 from python.org ...
    powershell -NoProfile -Command ^
        "try { (New-Object System.Net.WebClient).DownloadFile('%PYURL%', '%PYINSTALLER%'); Write-Host '[OK] Python installer downloaded.' } catch { Write-Host '[ERROR] Download failed: ' + $_.Exception.Message; exit 1 }"
    if errorlevel 1 (
        echo [ERROR] Could not download Python installer.
        echo         Please install Python manually: https://www.python.org/downloads/
        echo         Tick "Add python.exe to PATH", then re-run this script.
        pause
        exit /b 1
    )

    echo [...] Installing Python silently ^(InstallAllUsers, PrependPath^) ...
    "%PYINSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_launcher=1
    if errorlevel 1 (
        echo [WARN] Silent install returned a non-zero code.
        echo        If Python was installed, please re-run this script.
        pause
        exit /b 1
    )
    echo [OK] Python installed successfully.

    :: Refresh PATH in this session
    for /f "tokens=*" %%i in ('powershell -NoProfile -Command "[System.Environment]::GetEnvironmentVariable(\"Path\",\"Machine\")"') do set SYSPATH=%%i
    for /f "tokens=*" %%i in ('powershell -NoProfile -Command "[System.Environment]::GetEnvironmentVariable(\"Path\",\"User\")"') do set USRPATH=%%i
    set PATH=%SYSPATH%;%USRPATH%

    :: Re-detect Python after install
    where python >nul 2>nul && (python --version 2>&1 | findstr /i "Python 3" >nul && set PYTHON=python)
    if not defined PYTHON (where py >nul 2>nul && (py --version 2>&1 | findstr /i "Python 3" >nul && set PYTHON=py))

    if not defined PYTHON (
        echo.
        echo [WARN] Python was installed but is not yet on PATH in this session.
        echo        Please close this window, open a NEW Command Prompt and re-run:
        echo          %~f0
        pause
        exit /b 0
    )
)

echo [OK] Using Python: %PYTHON%

:: ── Step 4: Download and run the deployer ────────────────────────────────────
set TMPFILE=%TEMP%\itc_deploy_client.py
echo [...] Downloading deployer from %SERVER% ...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%SERVER%/static/deploy_client.py' -OutFile '%TMPFILE%' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
    echo [ERROR] Download failed. Is web_app.py running on %SERVER% ?
    pause
    exit /b 1
)
echo [OK] Downloaded deploy_client.py

echo [...] Running deployer ...
echo.
%PYTHON% "%TMPFILE%" --server %SERVER%

:end
echo.
echo [OK] Done. Open %SERVER% in a browser to see this PC.
pause
'''


@app.route("/static/<path:filename>")
def serve_static_deploy_file(filename: str):
    """
    Serve auto-deploy scripts and agent source files.
    Also serves client_agent.exe if placed next to web_app.py.
    """
    if filename.endswith(".exe"):
        here      = Path(__file__).parent
        file_path = (here / filename).resolve()
        if not str(file_path).startswith(str(here.resolve())):
            abort(403)
        if not file_path.exists():
            abort(404)
        return send_file(
            file_path,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=filename,
        )

    if not _re.match(r'^[\w\-]+\.(py|ps1|sh)$', filename):
        abort(403)

    _static = {
        "deploy_client.py":      (_DEPLOY_CLIENT_PY,   "text/x-python"),
        "bootstrap_client.ps1":  (_BOOTSTRAP_PS1,      "text/plain"),
        "bootstrap_client.sh":   (_BOOTSTRAP_SH,       "text/x-sh"),
        "client_agent.py":       (None, "text/x-python"),
        "app_inventory.py":      (None, "text/x-python"),
    }

    if filename not in _static:
        abort(404)

    content_str, mimetype = _static[filename]

    if content_str is not None:
        local_ip   = _get_local_ip()
        server_url = f"http://10.31.3.114:{PORT}"
        content_str = content_str.replace("http://10.31.3.114:5001", server_url)
        return app.response_class(
            response=content_str.encode("utf-8"),
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    here      = Path(__file__).parent
    file_path = (here / filename).resolve()
    if not str(file_path).startswith(str(here.resolve())):
        abort(403)
    if not file_path.exists():
        abort(404)
    return send_file(str(file_path), mimetype=mimetype,
                     as_attachment=True, download_name=filename)


@app.route("/install/<os_type>")
def install_os(os_type: str):
    """
    One-click installer — returns a .bat (Windows) or .sh (Linux/macOS) script.
    """
    local_ip   = _get_local_ip()
    server_url = f"http://10.31.3.114:{PORT}"

    if os_type == "windows":
        content = _INSTALL_BAT.replace("__SERVER_URL__", server_url)
        return app.response_class(
            response=content.encode("utf-8"),
            mimetype="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="itc_install.bat"'},
        )
    elif os_type in ("linux", "mac", "macos"):
        content = _BOOTSTRAP_SH.replace("http://10.31.3.114:5001", server_url)
        return app.response_class(
            response=content.encode("utf-8"),
            mimetype="text/x-sh",
            headers={"Content-Disposition": 'attachment; filename="itc_install.sh"'},
        )
    else:
        abort(404)


@app.route("/api/info")
def api_info():
    import platform
    return jsonify({
        "hostname": socket.gethostname(),
        "ip":       _get_local_ip(),
        "os":       f"{platform.system()} {platform.release()}",
        "arch":     platform.machine(),
    })


@app.route("/api/my_ip")
def api_my_ip():
    """Return the IP address of whoever is calling this endpoint."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    return jsonify({"ip": ip})


# ── Client registration ───────────────────────────────────────────────────────

@app.route("/api/register_client", methods=["POST"])
def api_register_client():
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        abort(401)
    data     = request.get_json(force=True, silent=True) or {}
    ip       = data.get("ip", request.remote_addr)
    hostname = data.get("hostname", "unknown")
    port     = int(data.get("port", AGENT_PORT))
    aid      = _agent_id(ip, hostname)
    with _agents_lock:
        _agents[aid] = {
            "agent_id":  aid,
            "ip":        ip,
            "hostname":  hostname,
            "port":      port,
            "last_seen": time.time(),
            "status":    "idle",
        }
    return jsonify({"registered": True, "agent_id": aid})


@app.route("/api/clients")
def api_clients():
    _prune_agents()
    now = time.time()
    with _agents_lock:
        clients = [
            {**v, "last_seen_ago": int(now - v["last_seen"])}
            for v in _agents.values()
        ]
    return jsonify({"clients": clients})


# ── Trigger scan on a client ──────────────────────────────────────────────────

@app.route("/api/scan_client/<agent_id>", methods=["POST"])
def api_scan_client(agent_id: str):
    raw = request.args.get("data")
    try:
        form_data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Invalid data query JSON: {exc}"}), 400

    if not isinstance(form_data, dict):
        return jsonify({"error": "data query must be a JSON object"}), 400

    with _client_form_data_lock:
        _client_form_data[agent_id] = dict(form_data)
        _client_register_sent.discard(agent_id)

    with _client_files_lock:
        _client_files.clear()
    _clear_output_dir()

    result = _call_agent(agent_id, "POST", "/run", {"form_data": form_data})
    if "error" in result:
        return jsonify(result), 500
    return jsonify(result)


# ── Start / Stop agent on client ─────────────────────────────────────────────

@app.route("/api/start_agent/<agent_id>", methods=["POST"])
def api_start_agent(agent_id: str):
    """
    Ask the client agent to restart itself (wake it up if sleeping).
    The agent must expose POST /agent/start for this to work.
    If it is already running and registered, we just return success.
    """
    with _agents_lock:
        agent = _agents.get(agent_id)
    if not agent:
        return jsonify({"error": f"Agent '{agent_id}' not registered or timed out."}), 404
    result = _call_agent(agent_id, "POST", "/agent/start")
    if "error" in result:
        # Agent is already running (we can reach it), treat as success
        return jsonify({"status": "running", "note": "Agent is reachable — already running."})
    return jsonify({"status": "started", **result})


@app.route("/api/stop_agent/<agent_id>", methods=["POST"])
def api_stop_agent(agent_id: str):
    """
    Ask the client agent to shut itself down gracefully.
    The agent must expose POST /agent/stop for this to work.
    After stopping, remove it from the registered list.
    """
    with _agents_lock:
        agent = _agents.get(agent_id)
    if not agent:
        return jsonify({"error": f"Agent '{agent_id}' not registered or timed out."}), 404
    result = _call_agent(agent_id, "POST", "/agent/stop")
    # Remove from registry regardless of whether the call succeeded
    with _agents_lock:
        _agents.pop(agent_id, None)
    if "error" in result and not result.get("_transient"):
        return jsonify({"status": "error", "detail": result["error"]}), 500
    return jsonify({"status": "stopped"})


# ── Poll the client's scan log ────────────────────────────────────────────────

@app.route("/api/client_status/<agent_id>")
def api_client_status(agent_id: str):
    offset = int(request.args.get("offset", 0))
    result = _call_agent(agent_id, "GET", f"/status?offset={offset}")
    if "error" in result:
        with _agents_lock:
            agent_known = agent_id in _agents
        if agent_known:
            return jsonify({
                "status":      "running",
                "lines":       [],
                "total_lines": offset,
                "error":       "",
            })
        return jsonify({
            "status":      "error",
            "lines":       [result["error"]],
            "total_lines": offset,
            "error":       result["error"],
        })

    lines       = result.get("log", [])
    total_lines = result.get("total_lines", offset + len(lines))

    pdf_path  = result.get("pdf_path")
    json_path = result.get("json_path")
    if pdf_path or json_path:
        with _client_files_lock:
            entry = _client_files.setdefault(agent_id, {})
            if pdf_path:
                entry.setdefault("pdf_remote",  pdf_path)
            if json_path:
                entry.setdefault("json_remote", json_path)
        if result.get("status") == "done":
            def _forward_from_status():
                forward_result = _client_upload_forward_saved_files(agent_id)
                if forward_result:
                    print(f"Register forward from status for {agent_id}: {forward_result}")

            threading.Thread(target=_forward_from_status, daemon=True).start()

    return jsonify({
        "status":      result.get("status", "running"),
        "lines":       lines,
        "total_lines": total_lines,
        "error":       result.get("error", ""),
    })


# ── Receive uploaded PDF / JSON from the client agent ────────────────────────

def _client_upload_value(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value).strip()


def _client_upload_header_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _client_upload_file(field_names):
    for field_name in field_names:
        upload = request.files.get(field_name)
        if upload is not None and upload.filename:
            return upload
    return None


def _client_upload_safe_filename(filename: str, fallback: str) -> str:
    name = os.path.basename(filename or fallback)
    name = re.sub(r'[^A-Za-z0-9_\-.]', '_', name)
    return name or fallback


def _client_upload_device_scan_fields(values: dict):
    """Return only the multipart fields accepted by POST /api/device-scan."""
    source = {str(key): _client_upload_value(value) for key, value in values.items()}
    aliases = {
        "dsr_no": ("dsr_no", "DSR_no"),
        "serial_no": ("serial_no", "Serial_no", "serial_number"),
        "pf_no": ("pf_no", "PF_no"),
        "target_ip": ("target_ip", "ip"),
        "name": ("name", "employee_name", "client_name"),
        "phone": ("phone", "phone_no", "mobile"),
    }

    fields = {}
    for destination, candidates in aliases.items():
        value = next((source.get(key, "") for key in candidates if source.get(key, "")), "")
        if value:
            fields[destination] = value

    missing = [name for name in ("dsr_no", "serial_no", "pf_no", "target_ip") if not fields.get(name)]
    return fields, missing


def _client_upload_fields_from_data(form_data: dict):
    return _client_upload_device_scan_fields(form_data)


def _client_upload_request_fields(form_data: dict):
    fields = {
        str(key): _client_upload_value(request.form.get(key))
        for key in request.form
        if key != "data"
    }
    fields.update({str(key): _client_upload_value(value) for key, value in form_data.items()})
    return _client_upload_device_scan_fields(fields)


def _client_upload_validate_json(content: bytes):
    try:
        json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"json_file must contain valid JSON: {exc}"
    return None


def _client_upload_build_multipart(fields: dict, files: list):
    boundary = f"----ITCScanner{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{_client_upload_header_value(name)}"\r\n\r\n'
            .encode("utf-8")
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for field_name, filename, content_type, content in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{_client_upload_header_value(field_name)}"; '
                f'filename="{_client_upload_header_value(filename)}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), boundary


def _client_upload_decode_response(content_type: str, data: bytes):
    text = data.decode("utf-8", errors="replace")
    if not text:
        return {}
    if "application/json" in (content_type or "").lower():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _client_upload_forward_to_device_scan(fields: dict, files: list):
    import http.client
    import urllib.error
    import urllib.request

    body, boundary = _client_upload_build_multipart(fields, files)
    file_names = [filename for _, filename, _, _ in files]
    print(f"Forwarding to DEVICE_SCAN_API_URL: fields={sorted(fields.keys())}, files={file_names}")
    req = urllib.request.Request(
        DEVICE_SCAN_API_URL,
        # urllib must receive the encoded multipart bytes.  Supplying a dict
        # makes it start a POST but fail while streaming the body, which
        # aborts the peer's Multer request.
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = _client_upload_decode_response(
                resp.headers.get("Content-Type", ""),
                resp.read(),
            )
            print(f"DEVICE_SCAN_API_URL response {resp.getcode()}: {payload}")
            return resp.getcode(), payload, None
    except urllib.error.HTTPError as exc:
        payload = _client_upload_decode_response(
            exc.headers.get("Content-Type", ""),
            exc.read(),
        )
        print(f"DEVICE_SCAN_API_URL rejected upload {exc.code}: {payload}")
        return exc.code, payload, "Device-scan service rejected upload"
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # A Node/Multer server can reset the socket while parsing a multipart
        # body.  It is an upstream failure, not an unhandled Flask error.
        print(f"DEVICE_SCAN_API_URL connection failed: {exc}")
        return 502, {"detail": str(exc)}, "Device-scan service connection failed"


def _client_upload_fetch_agent_file(agent_id: str, ftype: str, target_name: str = ""):
    import urllib.error
    import urllib.request

    with _agents_lock:
        agent = _agents.get(agent_id)
    if not agent:
        return None, f"Agent '{agent_id}' not registered; cannot fetch {ftype}"

    url = f"http://{agent['ip']}:{agent['port']}/download/{ftype}"
    req = urllib.request.Request(url, headers={"X-Token": SECRET_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            if not data:
                return None, f"Downloaded {ftype} from agent was empty"
            disposition = resp.headers.get("Content-Disposition", "")
            match = re.search(r'filename="?([^";]+)"?', disposition)
            filename = target_name or (match.group(1) if match else f"latest.{ftype}")
            filename = _client_upload_safe_filename(filename, f"latest.{ftype}")
            save_path = OUTPUT_DIR / filename
            with open(save_path, "wb") as fh:
                fh.write(data)
            return str(save_path), None
    except urllib.error.URLError as exc:
        return None, f"Could not fetch {ftype} from agent: {exc}"
    except OSError as exc:
        return None, f"Could not save fetched {ftype}: {exc}"


def _client_upload_forward_saved_files(agent_id: str):
    with _client_form_data_lock:
        if agent_id in _client_register_sent:
            return {"status": "already_sent"}
        form_data = dict(_client_form_data.get(agent_id, {}))

    with _client_files_lock:
        files_entry = dict(_client_files.get(agent_id, {}))

    pdf_path = files_entry.get("pdf")
    json_path = files_entry.get("json")
    fetched_updates = {}
    if (not pdf_path or not os.path.isfile(pdf_path)) and files_entry.get("pdf_remote"):
        pdf_path, fetch_error = _client_upload_fetch_agent_file(
            agent_id,
            "pdf",
            os.path.basename(files_entry.get("pdf_remote", "")),
        )
        if fetch_error:
            print(fetch_error)
        elif pdf_path:
            fetched_updates["pdf"] = pdf_path
    if (not json_path or not os.path.isfile(json_path)) and files_entry.get("json_remote"):
        json_path, fetch_error = _client_upload_fetch_agent_file(
            agent_id,
            "json",
            os.path.basename(files_entry.get("json_remote", "")),
        )
        if fetch_error:
            print(fetch_error)
        elif json_path:
            fetched_updates["json"] = json_path
    if fetched_updates:
        with _client_files_lock:
            entry = _client_files.setdefault(agent_id, {})
            entry.update(fetched_updates)

    if not pdf_path or not json_path:
        return None
    if not (os.path.isfile(pdf_path) and os.path.isfile(json_path)):
        return None

    try:
        with open(pdf_path, "rb") as pdf_fh:
            pdf_bytes = pdf_fh.read()
        with open(json_path, "rb") as json_fh:
            json_bytes = json_fh.read()
    except OSError as exc:
        return {"error": f"Could not read latest report files: {exc}"}
    if not pdf_bytes or not json_bytes:
        return None

    json_error = _client_upload_validate_json(json_bytes)
    if json_error:
        return {"error": json_error}

    form_data.setdefault("target_ip", "")
    if not form_data["target_ip"]:
        with _agents_lock:
            form_data["target_ip"] = _agents.get(agent_id, {}).get("ip", "")
    fields, missing = _client_upload_fields_from_data(form_data)
    if missing:
        return {"error": f"Device-scan requires: {', '.join(missing)}"}

    with _client_form_data_lock:
        if agent_id in _client_register_sent:
            return {"status": "already_sent"}
        _client_register_sent.add(agent_id)

    files = [
        ("pdf_file", os.path.basename(pdf_path), "application/pdf", pdf_bytes),
        ("json_file", os.path.basename(json_path), "application/json", json_bytes),
    ]
    status_code, payload, error = _client_upload_forward_to_device_scan(fields, files)
    result = {"target_status": status_code, "data": payload}
    if error:
        with _client_form_data_lock:
            _client_register_sent.discard(agent_id)
        result["error"] = error
    else:
        result["status"] = "ok"
    return result


@app.route("/api/client_upload", methods=["POST"])
def api_client_upload_device_scan():
    raw = request.args.get("data")
    try:
        form_data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        return jsonify({"error": f"Invalid data query JSON: {exc}"}), 400

    if not isinstance(form_data, dict):
        return jsonify({"error": "data query must be a JSON object"}), 400

    pdf_upload = _client_upload_file(("pdf_file", "pdf"))
    json_upload = _client_upload_file(("json_file", "json"))
    if pdf_upload is None or json_upload is None:
        return jsonify({"error": "Both pdf_file and json_file are required"}), 400

    pdf_name = _client_upload_safe_filename(pdf_upload.filename, "upload.pdf")
    json_name = _client_upload_safe_filename(json_upload.filename, "upload.json")
    if not pdf_name.lower().endswith(".pdf"):
        return jsonify({"error": "pdf_file must be a .pdf file"}), 400
    if not json_name.lower().endswith(".json"):
        return jsonify({"error": "json_file must be a .json file"}), 400

    pdf_bytes = pdf_upload.read()
    json_bytes = json_upload.read()
    if not pdf_bytes or not json_bytes:
        return jsonify({"error": "Uploaded PDF and JSON files must not be empty"}), 400
    json_error = _client_upload_validate_json(json_bytes)
    if json_error:
        return jsonify({"error": json_error}), 400

    request_data = dict(form_data)
    if not request_data.get("target_ip"):
        request_data["target_ip"] = request.headers.get("X-Client-IP", "").strip()
    fields, missing = _client_upload_request_fields(request_data)
    if missing:
        return jsonify({"error": f"Device-scan requires: {', '.join(missing)}"}), 400

    _clear_output_dir()
    pdf_path = OUTPUT_DIR / pdf_name
    json_path = OUTPUT_DIR / json_name
    with open(pdf_path, "wb") as pdf_fh:
        pdf_fh.write(pdf_bytes)
    with open(json_path, "wb") as json_fh:
        json_fh.write(json_bytes)

    with _client_files_lock:
        _client_files.clear()

    agent_id = request.headers.get("X-Agent-ID", "").strip()
    if agent_id:
        with _client_files_lock:
            _client_files[agent_id] = {
                "pdf": str(pdf_path),
                "json": str(json_path),
            }
        with _client_form_data_lock:
            _client_form_data[agent_id] = dict(form_data)

    files = [
        ("pdf_file", pdf_path.name, "application/pdf", pdf_bytes),
        ("json_file", json_path.name, "application/json", json_bytes),
    ]

    status_code, device_scan_payload, error = _client_upload_forward_to_device_scan(fields, files)
    response = {
        "target_status": status_code,
        "data": device_scan_payload,
    }
    if error:
        response["error"] = error
        return jsonify(response), status_code
    if agent_id:
        with _client_form_data_lock:
            _client_register_sent.add(agent_id)
    response["status"] = "ok"
    return jsonify(response), status_code


@app.route("/api/client_upload/<agent_id>/<ftype>/<filename>", methods=["PUT"])
def api_client_upload(agent_id: str, ftype: str, filename: str):
    token = request.headers.get("X-Token", "")
    if token != SECRET_TOKEN:
        abort(401)

    if ftype not in ("pdf", "json"):
        abort(400)

    filename  = re.sub(r'[^A-Za-z0-9_\-.]', '_', os.path.basename(filename))
    save_path = OUTPUT_DIR / filename

    data = request.get_data(cache=False)
    if len(data) == 0:
        return jsonify({"error": "Empty file received"}), 400

    if ftype == "pdf":
        _clear_output_dir()
        with _client_files_lock:
            _client_files.clear()

    with open(save_path, "wb") as fh:
        fh.write(data)

    with _client_files_lock:
        if agent_id not in _client_files:
            _client_files[agent_id] = {}
        _client_files[agent_id][ftype] = str(save_path)

    def _forward_saved_files():
        result = _client_upload_forward_saved_files(agent_id)
        if result:
            print(f"Register forward for {agent_id}: {result}")

    threading.Thread(target=_forward_saved_files, daemon=True).start()

    return jsonify({"status": "ok", "bytes": len(data), "path": str(save_path)})


# ── Get scan result metadata ──────────────────────────────────────────────────

@app.route("/api/client_result/<agent_id>")
def api_client_result(agent_id: str):
    with _client_files_lock:
        files = dict(_client_files.get(agent_id, {}))

    # ── Case 1: server received the uploaded files directly ───────────────────
    pdf_path  = files.get("pdf",  "")
    json_path = files.get("json", "")
    pdf_ok  = bool(pdf_path  and os.path.isfile(pdf_path)  and os.path.getsize(pdf_path)  > 0)
    json_ok = bool(json_path and os.path.isfile(json_path) and os.path.getsize(json_path) > 0)

    if pdf_ok:
        return jsonify({
            "pdf_name":  os.path.basename(pdf_path),
            "json_name": os.path.basename(json_path) if json_ok else "",
            "ok":        True,
            "via":       "upload",
        })

    # ── Case 2: files still on client — proxy will serve them ────────────────
    pdf_remote  = files.get("pdf_remote",  "")
    json_remote = files.get("json_remote", "")

    if pdf_remote:
        pdf_name  = os.path.basename(pdf_remote)
        json_name = os.path.basename(json_remote) if json_remote else ""
        return jsonify({
            "pdf_name":  pdf_name,
            "json_name": json_name,
            "ok":        True,
            "via":       "proxy",
        })

    # ── Case 3: files not ready yet ───────────────────────────────────────────
    status_resp = _call_agent(agent_id, "GET", "/status")
    if isinstance(status_resp, dict) and status_resp.get("status") == "error":
        return jsonify({"error": status_resp.get("error", "Scan failed.")})
    return jsonify({"pending": True})


# ── Download the result files ─────────────────────────────────────────────────

@app.route("/api/client_download/<agent_id>/pdf")
def download_client_pdf(agent_id: str):
    with _client_files_lock:
        files = _client_files.get(agent_id, {})
        path  = files.get("pdf")

    inline = request.args.get("inline", "0") == "1"

    # ── Served from server (upload succeeded) ────────────────────────────────
    if path and os.path.isfile(path):
        abs_path = Path(path).resolve()
        return send_file(abs_path, as_attachment=not inline,
                         download_name=abs_path.name, mimetype="application/pdf")

    # ── Proxy from client agent ───────────────────────────────────────────────
    return _proxy_from_agent(agent_id, "pdf")


@app.route("/api/client_download/<agent_id>/json")
def download_client_json(agent_id: str):
    with _client_files_lock:
        files = _client_files.get(agent_id, {})
        path  = files.get("json")

    inline = request.args.get("inline", "0") == "1"

    if path and os.path.isfile(path):
        abs_path = Path(path).resolve()
        return send_file(abs_path, as_attachment=not inline,
                         download_name=abs_path.name, mimetype="application/json")

    return _proxy_from_agent(agent_id, "json")


def _proxy_from_agent(agent_id: str, ftype: str):
    """Stream a file directly from the client agent to the browser."""
    import urllib.request, urllib.error
    with _agents_lock:
        agent = _agents.get(agent_id)
    if not agent:
        return f"Agent not registered — cannot proxy {ftype} file.", 404

    url = f"http://{agent['ip']}:{agent['port']}/download/{ftype}"
    req = urllib.request.Request(url, headers={"X-Token": SECRET_TOKEN})
    try:
        resp     = urllib.request.urlopen(req, timeout=60)
        data     = resp.read()
        fname    = resp.headers.get("Content-Disposition", "")
        import re as _re2
        m = _re2.search(r'filename="?([^";\r\n]+)"?', fname)
        download_name = m.group(1) if m else f"report.{ftype}"
        
        mime = "application/pdf" if ftype == "pdf" else "application/json"
        inline = request.args.get("inline", "0") == "1"
        disposition = "inline" if inline else "attachment"
        
        from flask import Response
        return Response(
            data,
            mimetype=mime,
            headers={"Content-Disposition": f'{disposition}; filename="{download_name}"'},
        )
    except urllib.error.URLError as exc:
        return f"Could not fetch {ftype} from client agent: {exc}", 502
    except Exception as exc:
        return f"Proxy error: {exc}", 500


# =============================================================================
# Entry point
# =============================================================================

def main():
    local_ip = _get_local_ip()
    print("=" * 60)
    print("  ITC System Scanner  —  Client-Scan Web Server  v3.0")
    print("=" * 60)
    print(f"  Local URL  :  http://127.0.0.1:{PORT}")
    print(f"  LAN URL    :  http://{local_ip}:{PORT}")
    print(f"  Reports    :  {OUTPUT_DIR}")
    print()
    print("  On each PC you want to scan, run:")
    print("      python client_agent.py")
    print()
    print("  Then open the LAN URL in a browser on any device.")
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
