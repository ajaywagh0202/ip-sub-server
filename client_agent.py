#!/usr/bin/env python3
"""
client_agent.py  —  ITC System Scanner  (Client-Side Agent)
============================================================
Run this script on the CLIENT PC (the machine you want to scan).
It opens a tiny HTTP listener, registers itself with the central web server,
and waits for the server to request a scan.

HOW IT WORKS
------------
  1. You run  python client_agent.py  on the client PC.
  2. The agent auto-detects its own LAN IP and registers with the server
     at SERVER_URL (edit the line below to match your server's address).
  3. You open the web interface (http://<server-ip>:5051) in a browser.
  4. The web page now lists this client PC.  Click "Scan This PC".
  5. The server tells the agent to run app_inventory.py locally.
  6. The agent runs the scan, saves PDF + JSON, then uploads them to the server.
  7. The web UI shows a download link — the PDF describes THIS client PC.

SETUP (run once per client PC)
-------------------------------
    pip install requests reportlab
    python client_agent.py

FILES NEEDED in the same folder as this script
-----------------------------------------------
    client_agent.py     <- this file
    app_inventory.py    <- the scanner logic (unchanged)

CONFIGURATION
-------------
Edit SERVER_URL below to point to the machine running web_app.py.
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
import traceback
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, urlparse

# ── Configuration ──────────────────────────────────────────────────────────────
SERVER_URL    = "http://192.168.1.10:5051"   # <- CHANGE to your web_app.py server
AGENT_PORT    = 7979                          # port this agent listens on
OUTPUT_DIR    = Path.home() / "itc_scanner_reports"
SECRET_TOKEN  = "ITC2024CLIENT"              # must match web_app.py
# ───────────────────────────────────────────────────────────────────────────────

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _clear_output_dir(log_fn=None):
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
            msg = f"  [WARN] Could not delete old report file {item}: {exc}"
            if log_fn:
                log_fn(msg)
            else:
                print(msg)

# ── PIL stub so reportlab works without Pillow ────────────────────────────────
_pil_stub = types.ModuleType("PIL")
_pil_stub.Image = None
sys.modules.setdefault("PIL", _pil_stub)
sys.modules.setdefault("PIL.Image", types.ModuleType("PIL.Image"))


# =============================================================================
# EXE / frozen support helpers
# =============================================================================

def _base_dir() -> Path:
    """
    Return the directory where agent source files live.

    - When run as a PyInstaller EXE:  sys._MEIPASS  (the temp extraction dir)
    - When run as a .py script:       directory containing this file
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path(os.getcwd())


# =============================================================================
# Helpers
# =============================================================================

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def _agent_id(ip: str, hostname: str) -> str:
    """
    Must exactly match web_app.py's _agent_id() — this is the key the server
    uses to track this PC.
    """
    safe = re.sub(r'[^A-Za-z0-9_\-]', '_', hostname)
    return f"{ip.replace('.', '_')}_{safe}"


def _load_inventory():
    """
    Load app_inventory.py.

    Search order:
      1. sys._MEIPASS  (PyInstaller bundle)
      2. Same directory as this script / exe
      3. Current working directory
    """
    search_dirs = []

    # PyInstaller temp dir
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        search_dirs.append(Path(sys._MEIPASS))

    # Script / exe directory
    search_dirs.append(_base_dir())

    # CWD
    search_dirs.append(Path(os.getcwd()))

    for d in search_dirs:
        path = d / "app_inventory.py"
        if path.exists():
            spec   = importlib.util.spec_from_file_location("app_inventory", str(path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

    checked = "\n  ".join(str(d / 'app_inventory.py') for d in search_dirs)
    raise FileNotFoundError(
        f"app_inventory.py not found. Searched:\n  {checked}\n"
        "Place app_inventory.py in the same folder as client_agent.py (or the EXE)."
    )


# =============================================================================
# Scan state
# =============================================================================

_lock   = threading.Lock()
_state  = {
    "status":    "idle",   # idle | running | done | error
    "log":       [],
    "pdf_path":  None,
    "json_path": None,
    "error":     "",
}


def _run_scan_worker(form_data=None):
    """Runs in a background thread.  Executes the full inventory scan."""
    form_data = dict(form_data or {})

    with _lock:
        _state["status"]    = "running"
        _state["log"]       = []
        _state["pdf_path"]  = None
        _state["json_path"] = None
        _state["error"]     = ""

    def log(msg):
        with _lock:
            _state["log"].append(str(msg))
        print(msg)

    try:
        _clear_output_dir(log)
        module      = _load_inventory()
        target_ip   = get_local_ip()
        system_name = socket.gethostname()
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")

        safe_ip   = target_ip.replace(".", "_")
        safe_name = re.sub(r'[^A-Za-z0-9_\-]', '_', system_name)
        pdf_path  = str(OUTPUT_DIR / f"{safe_ip}_{safe_name}_{ts}.pdf")
        json_path = pdf_path.replace(".pdf", ".json")

        log("=" * 64)
        log(f"  ITC Client Scanner  ->  {target_ip}  ({system_name})")
        log(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log(f"  Output  : {pdf_path}")
        log("=" * 64)

        report = {}

        log("[1/11] System info ...")
        report["system_info"] = module.get_system_info(target_ip)

        log("[2/11] Disk storage ...")
        report["disk_info"] = module.get_disk_info()
        disk = report["disk_info"]
        log(f"      {len(disk['partitions'])} partition(s)  |  "
            f"Total: {disk['total_size']}  Used: {disk['total_used']}  Free: {disk['total_free']}")

        log("[3/11] Installed packages ...")
        report["installed_packages"] = module.get_installed_packages()
        total = sum(len(v) for v in report["installed_packages"].values())
        log(f"      {total} packages across {len(report['installed_packages'])} managers")

        log("[4/11] Databases ...")
        report["databases"] = module.get_databases()
        log(f"      {len(report['databases'])} found")

        log("[5/11] Web servers / app frameworks ...")
        report["web_applications"] = module.get_web_applications()
        log(f"      {len(report['web_applications'])} found")

        log("[6/11] Running services ...")
        report["running_services"] = module.get_running_services()
        log(f"      {len(report['running_services'])} running")

        log("[7/11] Containers (Docker / Kubernetes) ...")
        report["containers"] = module.get_containers()
        log(f"      Docker: {len(report['containers'].get('containers', []))} containers  |  "
            f"K8s pods: {len(report['containers'].get('k8s_pods', []))}")

        log("[8/11] Virtual machines ...")
        report["virtual_machines"] = module.get_virtual_machines()
        vm_count = report["virtual_machines"]["total"]
        host_vm  = report["virtual_machines"]["host_is_vm"]
        report["system_info"]["host_hypervisor"] = host_vm
        log(f"      {vm_count} VM(s) found")

        log("[9/11] Language runtimes ...")
        report["runtimes"] = module.get_runtimes()
        log(f"       {', '.join(report['runtimes'].keys()) or 'none'}")

        log("[10/11] System health (CPU / RAM / swap / processes) ...")
        report["system_health"] = module.get_system_health()
        h = report["system_health"]
        log(f"        RAM {h.get('ram_pct','-')} used  |  Load: {h.get('load_avg','-')}  |  "
            f"Uptime: {h.get('uptime','')[:40]}")

        log("[11/11] Cyber security posture ...")
        report["cyber_security"] = module.get_cyber_security_posture(
            report["system_health"],
            report["running_services"],
            report["databases"],
            report["web_applications"],
        )
        cs = report["cyber_security"]
        log(f"        Score: {cs['score']}/100  Grade: {cs['grade']}  |  "
            f"Critical: {cs['summary']['CRITICAL']}  High: {cs['summary']['HIGH']}  "
            f"Medium: {cs['summary']['MEDIUM']}  Low: {cs['summary']['LOW']}")

        log("\n  Building PDF report ...")
        module.build_pdf(report, pdf_path, system_name)
        log(f"  PDF saved -> {pdf_path}")

        if form_data:
            report["registration_form_data"] = form_data

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        log(f"  JSON saved -> {json_path}")

        log("")
        log("=" * 64)
        log("  Scan complete.  Files saved locally.")
        log("=" * 64)

        # ── Mark scan as DONE immediately so the browser sees it ─────────────
        # Upload happens in a background thread — it must NOT block status.
        with _lock:
            _state["pdf_path"]  = pdf_path
            _state["json_path"] = json_path
            _state["status"]    = "done"

        # ── Upload to server in background (non-blocking) ─────────────────────
        def _do_upload():
            log("  Uploading to server (background) ...")
            _upload_results(pdf_path, json_path, target_ip, system_name, form_data, log)
            log("  Upload complete.")

        upload_thread = threading.Thread(target=_do_upload, daemon=True)
        upload_thread.start()

    except Exception as exc:
        msg = f"[ERROR] {exc}\n{traceback.format_exc()}"
        with _lock:
            _state["log"].append(msg)
            _state["status"] = "error"
            _state["error"]  = str(exc)
        print(msg)

def _upload_results_legacy(pdf_path, json_path, client_ip, client_name, log_fn):
    """Push the finished PDF + JSON back to the central web server.
    All exceptions are caught here — upload failure is a WARNING, never a scan error.
    The scan is already marked 'done' before this runs.
    Retries up to 3 times per file with a 5-second back-off.
    """
    import urllib.request
    import urllib.error

    agent_id   = _agent_id(client_ip, socket.gethostname())
    MAX_RETRIES = 3

    for fpath, ftype in [(pdf_path, "pdf"), (json_path, "json")]:
        if not os.path.isfile(fpath):
            log_fn(f"  [WARN] {ftype.upper()} file not found, skipping upload: {fpath}")
            continue

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with open(fpath, "rb") as fh:
                    data = fh.read()

                filename = os.path.basename(fpath)
                url = f"{SERVER_URL}/api/client_upload/{agent_id}/{ftype}/{filename}"

                req = urllib.request.Request(
                    url,
                    data=data,
                    method="PUT",
                    headers={
                        "Content-Type":   "application/octet-stream",
                        "X-Token":        SECRET_TOKEN,
                        "X-Client-IP":    client_ip,
                        "X-Client-Name":  client_name,
                    },
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    result = json.loads(resp.read())
                    log_fn(f"  Uploaded {ftype.upper()} -> server: {result}")
                break  # success — no more retries needed

            except urllib.error.URLError as exc:
                log_fn(f"  [WARN] Upload {ftype.upper()} attempt {attempt}/{MAX_RETRIES} failed (network): {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(5)
                else:
                    log_fn(f"  [WARN] Upload {ftype.upper()} failed after {MAX_RETRIES} attempts.")
                    log_fn(f"  File still available locally: {fpath}")
            except Exception as exc:
                log_fn(f"  [WARN] Upload {ftype.upper()} attempt {attempt}/{MAX_RETRIES} failed: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(5)
                else:
                    log_fn(f"  [WARN] Upload {ftype.upper()} failed after {MAX_RETRIES} attempts.")
                    log_fn(f"  File still available locally: {fpath}")


# =============================================================================
# HTTP handler — the server calls these endpoints to control this agent
# =============================================================================

def _multipart_header_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _build_results_multipart(pdf_path, json_path):
    boundary = f"----ITCScanner{uuid.uuid4().hex}"
    body = bytearray()
    file_parts = [
        ("pdf_file", pdf_path, "application/pdf"),
        ("json_file", json_path, "application/json"),
    ]

    for field_name, fpath, content_type in file_parts:
        if not os.path.isfile(fpath):
            raise FileNotFoundError(f"{field_name} not found: {fpath}")
        with open(fpath, "rb") as fh:
            content = fh.read()
        if not content:
            raise ValueError(f"{field_name} is empty: {fpath}")

        filename = os.path.basename(fpath)
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{_multipart_header_value(field_name)}"; '
                f'filename="{_multipart_header_value(filename)}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), boundary


def _decode_upload_response(data: bytes):
    text = data.decode("utf-8", errors="replace")
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _upload_results(pdf_path, json_path, client_ip, client_name, form_data, log_fn):
    """Push the finished PDF + JSON back to the central web server in one request."""
    import urllib.error
    import urllib.request

    agent_id = _agent_id(client_ip, socket.gethostname())
    raw_data = quote(json.dumps(form_data or {}, default=str), safe="")
    url = f"{SERVER_URL}/api/client_upload?data={raw_data}"
    MAX_RETRIES = 3

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            body, boundary = _build_results_multipart(pdf_path, json_path)
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                    "X-Token": SECRET_TOKEN,
                    "X-Client-IP": client_ip,
                    "X-Client-Name": client_name,
                    "X-Agent-ID": agent_id,
                },
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = _decode_upload_response(resp.read())
                log_fn(f"  Uploaded PDF+JSON -> register API via server: {result}")
            return

        except urllib.error.HTTPError as exc:
            detail = _decode_upload_response(exc.read())
            log_fn(f"  [WARN] Upload PDF+JSON attempt {attempt}/{MAX_RETRIES} failed ({exc.code}): {detail}")
        except urllib.error.URLError as exc:
            log_fn(f"  [WARN] Upload PDF+JSON attempt {attempt}/{MAX_RETRIES} failed (network): {exc}")
        except Exception as exc:
            log_fn(f"  [WARN] Upload PDF+JSON attempt {attempt}/{MAX_RETRIES} failed: {exc}")

        if attempt < MAX_RETRIES:
            time.sleep(5)
        else:
            log_fn("  [WARN] Upload PDF+JSON failed after all retries.")
            log_fn(f"  Files still available locally: {pdf_path} | {json_path}")


class ClientAgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"[agent-http] {self.address_string()}  {fmt % args}")

    def _auth(self) -> bool:
        token = self.headers.get("X-Token", "")
        if token != SECRET_TOKEN:
            self._send_json({"error": "Unauthorized"}, 401)
            return False
        return True

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/ping":
            self._send_json({
                "alive":  True,
                "host":   socket.gethostname(),
                "ip":     get_local_ip(),
                "status": _state["status"],
            })
            return

        if path == "/status":
            if not self._auth():
                return
            # Parse offset before acquiring lock
            from urllib.parse import parse_qs, urlparse as _up
            qs     = parse_qs(_up(self.path).query)
            offset = int(qs.get("offset", ["0"])[0])
            # Snapshot state then release lock before doing any I/O
            with _lock:
                full_log    = list(_state["log"])
                total_lines = len(full_log)
                offset      = max(0, min(offset, total_lines))
                snapshot    = {
                    "status":      _state["status"],
                    "error":       _state["error"],
                    "log":         full_log[offset:],
                    "total_lines": total_lines,
                    "pdf_path":    _state["pdf_path"],
                    "json_path":   _state["json_path"],
                }
            # Lock released — safe to do socket I/O now
            self._send_json(snapshot)
            return

        # /download/pdf  or  /download/json  — server proxies the file to browser
        if path in ("/download/pdf", "/download/json"):
            if not self._auth():
                return
            ftype = path.split("/")[-1]
            key   = "pdf_path" if ftype == "pdf" else "json_path"
            with _lock:
                fpath = _state[key]
            if not fpath or not os.path.isfile(fpath):
                self._send_json({"error": f"{ftype} file not ready"}, 404)
                return
            try:
                with open(fpath, "rb") as fh:
                    data = fh.read()
                fname = os.path.basename(fpath)
                mime  = "application/pdf" if ftype == "pdf" else "application/json"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{fname}"')
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/run":
            if not self._auth():
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = {}
            if length:
                try:
                    raw_body = self.rfile.read(length).decode("utf-8")
                    body = json.loads(raw_body) if raw_body else {}
                except json.JSONDecodeError as exc:
                    self._send_json({"error": f"Invalid JSON body: {exc}"}, 400)
                    return
            form_data = body.get("form_data", {}) if isinstance(body, dict) else {}
            if form_data is None:
                form_data = {}
            if not isinstance(form_data, dict):
                self._send_json({"error": "form_data must be an object"}, 400)
                return
            with _lock:
                status = _state["status"]
            if status == "running":
                self._send_json({"status": "already_running"})
                return
            # Reset state so a fresh scan can start after done/error
            with _lock:
                _state["status"]    = "idle"
                _state["log"]       = []
                _state["pdf_path"]  = None
                _state["json_path"] = None
                _state["error"]     = ""
            t = threading.Thread(target=_run_scan_worker, args=(form_data,), daemon=True)
            t.start()
            self._send_json({"status": "started"})
            return

        self._send_json({"error": "not found"}, 404)


# =============================================================================
# Registration — tell the server we exist
# =============================================================================

def _register_with_server(local_ip: str):
    """POST our address to the web server so it knows we exist."""
    import urllib.request
    import urllib.error

    try:
        payload = json.dumps({
            "ip":       local_ip,
            "hostname": socket.gethostname(),
            "port":     AGENT_PORT,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{SERVER_URL}/api/register_client",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Token":      SECRET_TOKEN,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            print(f"  [OK] Registered with server: {result}")
    except urllib.error.URLError as exc:
        print(f"  [WARN] Could not reach server at {SERVER_URL}: {exc}")
        print(f"         Make sure web_app.py is running on that address.")
    except Exception as exc:
        print(f"  [WARN] Registration error: {exc}")


def _keep_alive_loop(local_ip: str):
    """Re-register every 30 s so the server keeps us in its active list."""
    while True:
        time.sleep(30)
        try:
            _register_with_server(local_ip)
        except Exception as exc:
            print(f"  [WARN] Keep-alive registration error (will retry): {exc}")


# =============================================================================
# Main
# =============================================================================

def main():
    local_ip = get_local_ip()

    print("=" * 60)
    print("  ITC Client Agent")
    print("=" * 60)
    print(f"  This PC  :  {socket.gethostname()}  ({local_ip})")
    print(f"  Listening:  http://{local_ip}:{AGENT_PORT}")
    print(f"  Server   :  {SERVER_URL}")
    print(f"  Reports  :  {OUTPUT_DIR}")
    print(f"  Base dir :  {_base_dir()}")
    print("=" * 60)

    # Verify app_inventory.py is present
    try:
        _load_inventory()
        print("  [OK] app_inventory.py loaded successfully.\n")
    except FileNotFoundError as e:
        print(f"\n  [ERROR] {e}\n")
        sys.exit(1)

    # Register with the server
    print("  Registering with server ...")
    _register_with_server(local_ip)

    # Background keep-alive
    t = threading.Thread(target=_keep_alive_loop, args=(local_ip,), daemon=True)
    t.start()

    # Start the local HTTP listener
    server = HTTPServer(("0.0.0.0", AGENT_PORT), ClientAgentHandler)
    print(f"\n  Agent listening on port {AGENT_PORT}.  Press Ctrl+C to stop.")
    print(f"  Open {SERVER_URL} in any browser to scan this PC.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
