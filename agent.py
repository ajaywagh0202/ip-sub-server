#!/usr/bin/env python3
"""
agent.py  (GUI-compatible edition)
====================================
Lightweight HTTP agent — runs on EVERY server in the LAN.
Listens on port 7878 and exposes four endpoints:

  PUT  /upload
      Upload the ITC_System_Scanner binary to this server.
      Returns JSON: {"status": "ok", "bytes": N}

  POST /run
      Run ITC_System_Scanner in the background (headless / no GUI shown).
      Returns JSON: {"status": "started"} immediately.

  GET  /status
      Returns JSON: {"status": "idle"|"running"|"done"|"error",
                     "files": [...], "error": "..."}

  GET  /download/<filename>
      Streams the requested PDF or JSON file back to the caller.
      Only files inside OUTPUT_DIR are accessible (path-traversal safe).

  GET  /ping
      Health-check: returns {"alive": true, "host": "<hostname>"}

COMPATIBILITY NOTE
------------------
The binary produced by build_executable.py is a windowed GUI application.
When the agent launches it via subprocess.run(), the OS starts the process
without showing any window (because the agent itself has no desktop session).
The scanner still writes its PDF and JSON outputs to OUTPUT_DIR as normal —
the GUI simply has nothing to display on a headless server, which is fine.

HOW TO START (on each CentOS server):
    sudo python3 agent.py &

Or as a systemd service (see the unit snippet at the bottom of this file).

The agent auto-stops itself IDLE_SHUTDOWN seconds after the scan finishes
and all files have been downloaded.
"""

import os
import sys
import re
import json
import time
import socket
import logging
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

# ── Configuration ──────────────────────────────────────────────────────────────
AGENT_PORT    = 7878
BINARY_PATH   = "/tmp/ITC_System_Scanner"
OUTPUT_DIR    = "/tmp"
SECRET_TOKEN  = "ITC2024LAN"
IDLE_SHUTDOWN = 120          # seconds of idle after scan before auto-shutdown
# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/agent.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("agent")

# ── Global scan state ─────────────────────────────────────────────────────────
_lock           = threading.Lock()
_scan_status    = "idle"      # idle | running | done | error
_scan_files     = []
_scan_error     = ""
_scan_finished  = None
_download_count = 0


def _get_local_ip() -> str:
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


def _find_output_files() -> list:
    """Return PDF and JSON files in OUTPUT_DIR created in the last 10 minutes."""
    cutoff = time.time() - 600
    found  = []
    try:
        for entry in os.scandir(OUTPUT_DIR):
            if entry.is_file() and entry.stat().st_mtime >= cutoff:
                if entry.name.endswith(".pdf") or entry.name.endswith(".json"):
                    found.append(entry.path)
    except Exception as exc:
        log.warning(f"Error scanning output dir: {exc}")
    return sorted(found)


def _run_scanner():
    """
    Execute ITC_System_Scanner in a background thread and update global state.

    The binary is a windowed GUI application, but since the agent has no
    display session, the GUI simply never appears.  The scanner still
    collects data and writes PDF/JSON files to OUTPUT_DIR as usual.
    On Linux we set DISPLAY="" to suppress any Tk display errors cleanly.
    """
    global _scan_status, _scan_files, _scan_error, _scan_finished

    with _lock:
        _scan_status = "running"
        _scan_files  = []
        _scan_error  = ""

    log.info("Starting ITC_System_Scanner ...")

    # Prepare environment: suppress display on headless Linux servers
    env = os.environ.copy()
    if sys.platform.startswith("linux"):
        env.pop("DISPLAY", None)          # prevent Tk from trying to connect X11
        env["DISPLAY"] = ""

    try:
        result = subprocess.run(
            [BINARY_PATH],
            cwd=OUTPUT_DIR,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stdout:
            for line in stdout.splitlines()[-20:]:
                log.info(f"  scanner: {line}")

        # Filter out harmless Tk "no display" messages from stderr
        real_errors = [
            l for l in stderr.splitlines()
            if l.strip() and "no display" not in l.lower()
               and "_tkinter" not in l.lower()
               and "cannot connect" not in l.lower()
        ]
        if real_errors:
            log.warning("  scanner stderr: " + "\n".join(real_errors[:10]))

        files = _find_output_files()

        with _lock:
            if result.returncode == 0 and files:
                _scan_status   = "done"
                _scan_files    = files
                _scan_finished = datetime.now()
                log.info(f"Scan complete.  Files: {[os.path.basename(f) for f in files]}")
            elif files:
                # Some PDF scanners exit non-zero but still produce output —
                # accept the files if they exist.
                _scan_status   = "done"
                _scan_files    = files
                _scan_finished = datetime.now()
                log.info(
                    f"Scan finished (exit={result.returncode}) with files: "
                    f"{[os.path.basename(f) for f in files]}"
                )
            else:
                _scan_status = "error"
                _scan_error  = (
                    "\n".join(real_errors[:5]) if real_errors
                    else f"Exit code {result.returncode}, no output files found."
                )
                log.error(f"Scan failed: {_scan_error}")

    except subprocess.TimeoutExpired:
        with _lock:
            _scan_status = "error"
            _scan_error  = "Scanner timed out after 600 seconds."
        log.error(_scan_error)

    except Exception as exc:
        with _lock:
            _scan_status = "error"
            _scan_error  = str(exc)
        log.error(f"Scanner exception: {exc}")


def _auto_shutdown_thread(server: HTTPServer):
    """Shut the agent down IDLE_SHUTDOWN seconds after the scan finishes."""
    while True:
        time.sleep(10)
        with _lock:
            status   = _scan_status
            finished = _scan_finished
        if status in ("done", "error") and finished is not None:
            elapsed = (datetime.now() - finished).total_seconds()
            if elapsed >= IDLE_SHUTDOWN:
                log.info(f"Auto-shutdown after {IDLE_SHUTDOWN}s idle.")
                server.shutdown()
                return


def _safe_filename(name: str) -> bool:
    """Return True if the filename is safe (no path traversal)."""
    name = os.path.basename(unquote(name))
    return bool(re.match(r'^[\w\-. ]+$', name))


# =============================================================================
# HTTP request handler
# =============================================================================

class AgentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(f"HTTP  {self.address_string()}  {fmt % args}")

    def _auth(self) -> bool:
        token = self.headers.get("X-Token", "")
        if token != SECRET_TOKEN:
            self._send_json({"error": "Unauthorized"}, code=401)
            return False
        return True

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, msg: str, code: int = 400):
        self._send_json({"error": msg}, code=code)

    # ── PUT /upload ───────────────────────────────────────────────────────────
    def _handle_upload(self):
        if not self._auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_error_json("Empty upload.", 400)
            return
        try:
            data = self.rfile.read(length)
            with open(BINARY_PATH, "wb") as f:
                f.write(data)
            os.chmod(BINARY_PATH, 0o755)
            log.info(f"Binary uploaded -> {BINARY_PATH}  ({len(data)} bytes)")
            self._send_json({"status": "ok", "bytes": len(data)})
        except Exception as exc:
            log.error(f"Upload error: {exc}")
            self._send_error_json(str(exc), 500)

    # ── POST /run ─────────────────────────────────────────────────────────────
    def _handle_run(self):
        global _scan_status

        if not self._auth():
            return

        with _lock:
            status = _scan_status

        if status == "running":
            self._send_json({"status": "already_running"})
            return

        if not os.path.isfile(BINARY_PATH):
            self._send_error_json(
                f"Binary not found: {BINARY_PATH}.  "
                f"Upload ITC_System_Scanner via PUT /upload first.", 404
            )
            return

        if not os.access(BINARY_PATH, os.X_OK):
            os.chmod(BINARY_PATH, 0o755)

        t = threading.Thread(target=_run_scanner, daemon=True)
        t.start()
        self._send_json({"status": "started"})

    # ── GET /status ───────────────────────────────────────────────────────────
    def _handle_status(self):
        if not self._auth():
            return
        with _lock:
            self._send_json({
                "status": _scan_status,
                "files":  [os.path.basename(f) for f in _scan_files],
                "error":  _scan_error,
            })

    # ── GET /download/<filename> ──────────────────────────────────────────────
    def _handle_download(self, filename: str):
        global _download_count

        if not self._auth():
            return

        if not _safe_filename(filename):
            self._send_error_json("Invalid filename.", 400)
            return

        filepath    = os.path.realpath(os.path.join(OUTPUT_DIR, filename))
        output_real = os.path.realpath(OUTPUT_DIR)

        if not filepath.startswith(output_real + os.sep) and filepath != output_real:
            self._send_error_json("Access denied.", 403)
            return

        if not os.path.isfile(filepath):
            self._send_error_json(f"File not found: {filename}", 404)
            return

        size  = os.path.getsize(filepath)
        ctype = "application/pdf" if filename.endswith(".pdf") else "application/json"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

        with _lock:
            _download_count += 1
        log.info(f"Downloaded: {filename}  ({size} bytes)")

    # ── Router ────────────────────────────────────────────────────────────────
    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self._handle_upload()
        else:
            self._send_error_json("Not found.", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/run":
            self._handle_run()
        else:
            self._send_error_json("Not found.", 404)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/status":
            self._handle_status()
        elif path.startswith("/download/"):
            filename = path[len("/download/"):]
            self._handle_download(filename)
        elif path == "/ping":
            self._send_json({"alive": True, "host": socket.gethostname()})
        else:
            self._send_error_json("Not found.", 404)


# =============================================================================
# Main
# =============================================================================

def main():
    local_ip = _get_local_ip()
    log.info("=" * 60)
    log.info("  ITC Agent starting  (GUI-compatible edition)")
    log.info(f"  Host   : {socket.gethostname()}  ({local_ip})")
    log.info(f"  Port   : {AGENT_PORT}")
    log.info(f"  Binary : {BINARY_PATH}")
    log.info(f"  Output : {OUTPUT_DIR}")
    log.info("=" * 60)

    if not os.path.isfile(BINARY_PATH):
        log.warning(
            f"Binary not found at {BINARY_PATH}.  "
            f"Upload ITC_System_Scanner via PUT /upload before triggering /run."
        )

    server = HTTPServer(("0.0.0.0", AGENT_PORT), AgentHandler)

    shutdown_thread = threading.Thread(
        target=_auto_shutdown_thread, args=(server,), daemon=True
    )
    shutdown_thread.start()

    log.info(f"Agent listening on 0.0.0.0:{AGENT_PORT} ...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")
    finally:
        server.server_close()
        log.info("Agent stopped.")


# =============================================================================
# systemd unit snippet
# Paste into /etc/systemd/system/itc-agent.service
# then run: systemctl daemon-reload && systemctl enable --now itc-agent
# =============================================================================
SYSTEMD_UNIT = """
[Unit]
Description=ITC Inventory Agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/itc/agent.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

if __name__ == "__main__":
    main()