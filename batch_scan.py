#!/usr/bin/env python3
"""
batch_scan.py  (GUI-binary compatible)
========================================
Runs on YOUR PC.  No SSH required — communicates with agent.py over HTTP.

The workflow is identical whether the deployed binary is the old CLI version
or the new GUI version.  When agent.py launches the binary on a headless
server, the GUI simply has nothing to display — the scanner still runs and
produces the PDF/JSON outputs that this script collects.

Flow:
  1. Ping-sweep 10.31.0.0/16 to find live hosts
  2. For each live host:
       a. HTTP GET  /ping          — confirm agent is running
       b. HTTP PUT  /upload        — upload ITC_System_Scanner binary
       c. HTTP POST /run           — trigger the scanner
       d. Poll     /status         — wait until done
       e. HTTP GET  /download/xxx  — pull PDF + JSON
       f. Save to  scan_results/<ip>/
       g. Wait INTER_HOST_DELAY seconds before next host
  3. Print summary

REQUIREMENTS (on this PC):
    Python 3.7+  —  no extra packages needed (uses stdlib only)

CONFIGURATION — edit the constants below.
"""

import os
import sys
import time
import json
import socket
import logging
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ── Configuration ──────────────────────────────────────────────────────────────
SUBNET                = "10.31"          # First two octets: scans 10.31.0.1–10.31.255.254
AGENT_PORT            = 7878             # Must match agent.py AGENT_PORT
SECRET_TOKEN          = "ITC2024LAN"    # Must match agent.py SECRET_TOKEN

LOCAL_OUTPUT_DIR      = "./scan_results"
BINARY_NAME           = "ITC_System_Scanner"   # binary next to this script

PING_TIMEOUT          = 1               # seconds per ping
INTER_HOST_DELAY      = 5               # seconds between hosts (firewall-friendly)
AGENT_CONNECT_TIMEOUT = 5               # seconds to wait for HTTP connect
POLL_INTERVAL         = 10              # seconds between /status polls
SCAN_WAIT_TIMEOUT     = 600             # max seconds to wait for scan to finish
SWEEP_THREADS         = 256             # parallel threads for ping sweep
# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("batch_scan.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("batch_scan")


# =============================================================================
# HTTP helpers  (stdlib only — no requests/paramiko)
# =============================================================================

def _http_request(method: str, url: str, token: str,
                  data: bytes = None, timeout: int = 10):
    """Minimal HTTP request using stdlib urllib.
    Returns (status_code, response_bytes)."""
    req = Request(url, data=data, method=method)
    req.add_header("X-Token", token)
    if data and method in ("POST", "PUT"):
        req.add_header("Content-Length", str(len(data)))
    with urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def http_get_json(url: str, token: str, timeout: int = 10) -> dict:
    _, body = _http_request("GET", url, token, timeout=timeout)
    return json.loads(body.decode("utf-8"))


def http_post_json(url: str, token: str,
                   payload: dict = None, timeout: int = 10) -> dict:
    body = json.dumps(payload or {}).encode("utf-8")
    _, resp = _http_request("POST", url, token, data=body, timeout=timeout)
    return json.loads(resp.decode("utf-8"))


def http_put_binary(url: str, token: str,
                    filepath: str, timeout: int = 180) -> dict:
    """Upload a binary file via HTTP PUT.  Returns response dict."""
    with open(filepath, "rb") as f:
        data = f.read()
    req = Request(url, data=data, method="PUT")
    req.add_header("X-Token", token)
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("Content-Length", str(len(data)))
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_download_file(url: str, token: str,
                       dest_path: str, timeout: int = 120) -> int:
    """Download a file via HTTP GET, save to dest_path.  Returns file size."""
    req = Request(url, method="GET")
    req.add_header("X-Token", token)
    with urlopen(req, timeout=timeout) as resp:
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    return os.path.getsize(dest_path)


def agent_url(ip: str, path: str) -> str:
    return f"http://{ip}:{AGENT_PORT}{path}"


# =============================================================================
# Ping sweep
# =============================================================================

def ping_host(ip: str) -> bool:
    """Return True if the host responds to ICMP ping."""
    flag = "-n" if sys.platform.startswith("win") else "-c"
    wait = "-w" if sys.platform.startswith("win") else "-W"
    cmd  = ["ping", flag, "1", wait, str(PING_TIMEOUT), ip]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PING_TIMEOUT + 2,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def tcp_check(ip: str, port: int, timeout: int = 2) -> bool:
    """TCP connect check — catches hosts that block ICMP."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def sweep_subnet(subnet_prefix: str) -> list:
    """
    Sweep all IPs in <subnet_prefix>.0.0/16.
    Uses SWEEP_THREADS parallel threads for speed.
    Returns sorted list of live IP strings.
    """
    ips = [
        f"{subnet_prefix}.{third}.{fourth}"
        for third  in range(0, 256)
        for fourth in range(1, 255)
    ]
    total = len(ips)
    live  = []
    lock  = threading.Lock()

    log.info(f"Sweeping {subnet_prefix}.0.0/16  ({total} addresses) ...")

    def worker(ip: str):
        if ping_host(ip) or tcp_check(ip, AGENT_PORT):
            with lock:
                live.append(ip)
            log.info(f"  [LIVE] {ip}")

    for start in range(0, total, SWEEP_THREADS):
        batch   = ips[start: start + SWEEP_THREADS]
        threads = [threading.Thread(target=worker, args=(ip,), daemon=True)
                   for ip in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        done = min(start + SWEEP_THREADS, total)
        if done % 2048 == 0 or done == total:
            log.info(f"  Sweep progress: {done}/{total}  live so far: {len(live)}")

    live.sort(key=lambda x: tuple(int(o) for o in x.split(".")))
    log.info(f"Sweep complete.  Live hosts: {len(live)}")
    return live


# =============================================================================
# Per-host scan workflow
# =============================================================================

def ping_agent(ip: str) -> bool:
    """Return True if the ITC agent is alive on this host."""
    try:
        data = http_get_json(agent_url(ip, "/ping"), SECRET_TOKEN,
                             timeout=AGENT_CONNECT_TIMEOUT)
        return data.get("alive") is True
    except Exception:
        return False


def upload_binary(ip: str, binary_path: str) -> bool:
    """Upload ITC_System_Scanner binary via HTTP PUT /upload."""
    size_mb = os.path.getsize(binary_path) / 1_048_576
    log.info(f"[{ip}] Uploading binary ({size_mb:.1f} MB) ...")
    try:
        resp = http_put_binary(
            agent_url(ip, "/upload"), SECRET_TOKEN, binary_path, timeout=180
        )
        if resp.get("status") == "ok":
            log.info(f"[{ip}] Binary uploaded OK.")
            return True
        log.error(f"[{ip}] Upload failed: {resp}")
        return False
    except Exception as exc:
        log.error(f"[{ip}] Upload error: {exc}")
        return False


def trigger_scan(ip: str) -> bool:
    """POST /run to start the scanner.  Returns True if accepted."""
    try:
        resp   = http_post_json(agent_url(ip, "/run"), SECRET_TOKEN,
                                timeout=AGENT_CONNECT_TIMEOUT)
        status = resp.get("status", "")
        if status in ("started", "already_running"):
            log.info(f"[{ip}] Scan triggered (status={status})")
            return True
        log.error(f"[{ip}] Unexpected /run response: {resp}")
        return False
    except Exception as exc:
        log.error(f"[{ip}] Failed to trigger scan: {exc}")
        return False


def wait_for_scan(ip: str) -> tuple:
    """
    Poll /status every POLL_INTERVAL seconds until done or timeout.
    Returns (status_str, filenames_list, error_str).
    """
    deadline = time.time() + SCAN_WAIT_TIMEOUT
    while time.time() < deadline:
        try:
            resp   = http_get_json(agent_url(ip, "/status"), SECRET_TOKEN,
                                   timeout=10)
            status = resp.get("status", "unknown")
            log.info(f"[{ip}] Scan status: {status}")
            if status == "done":
                return "done", resp.get("files", []), ""
            if status == "error":
                return "error", [], resp.get("error", "Unknown error")
        except Exception as exc:
            log.warning(f"[{ip}] Status poll error: {exc}")
        time.sleep(POLL_INTERVAL)

    return "timeout", [], f"Scan did not finish within {SCAN_WAIT_TIMEOUT}s"


def collect_files(ip: str, filenames: list, host_dir: Path) -> list:
    """Download each filename from /download/<name> into host_dir."""
    downloaded = []
    for fname in filenames:
        url  = agent_url(ip, f"/download/{fname}")
        dest = str(host_dir / fname)
        log.info(f"[{ip}] Downloading {fname} ...")
        try:
            size = http_download_file(url, SECRET_TOKEN, dest, timeout=120)
            log.info(f"[{ip}] Saved {fname} ({size:,} bytes) -> {dest}")
            downloaded.append(dest)
        except Exception as exc:
            log.error(f"[{ip}] Failed to download {fname}: {exc}")
    return downloaded


def scan_host(ip: str, local_output_dir: Path, binary_path: str) -> dict:
    """Full scan workflow for one host.  Returns result dict."""
    result = {"ip": ip, "status": "failed", "files": [], "error": ""}

    log.info(f"[{ip}] Checking agent on port {AGENT_PORT} ...")
    if not ping_agent(ip):
        result["error"] = f"Agent not responding on port {AGENT_PORT}"
        log.error(f"[{ip}] {result['error']}")
        return result

    if not upload_binary(ip, binary_path):
        result["error"] = "Binary upload failed"
        return result

    if not trigger_scan(ip):
        result["error"] = "Failed to trigger scan"
        return result

    status, filenames, error = wait_for_scan(ip)
    if status != "done":
        result["error"] = error or f"Scan ended with status: {status}"
        log.error(f"[{ip}] {result['error']}")
        return result

    host_dir   = local_output_dir / ip.replace(".", "_")
    host_dir.mkdir(parents=True, exist_ok=True)
    downloaded = collect_files(ip, filenames, host_dir)

    result["status"] = "success" if downloaded else "no_files"
    result["files"]  = downloaded
    return result


# =============================================================================
# Summary report
# =============================================================================

def print_summary(results: list, elapsed: float):
    success = [r for r in results if r["status"] == "success"]
    failed  = [r for r in results if r["status"] != "success"]

    print("\n" + "=" * 68)
    print("  BATCH SCAN SUMMARY")
    print("=" * 68)
    print(f"  Total hosts scanned : {len(results)}")
    print(f"  Successful          : {len(success)}")
    print(f"  Failed / skipped    : {len(failed)}")
    print(f"  Time elapsed        : {elapsed:.0f} seconds")
    print(f"  Reports saved in    : {LOCAL_OUTPUT_DIR}")

    if success:
        print("\n  SUCCESSFUL HOSTS:")
        for r in success:
            fnames = ", ".join(os.path.basename(f) for f in r["files"])
            print(f"    {r['ip']:<18}  {fnames}")

    if failed:
        print("\n  FAILED / SKIPPED HOSTS:")
        for r in failed:
            print(f"    {r['ip']:<18}  [{r['status']}]  {r['error'][:70]}")

    print("=" * 68 + "\n")


# =============================================================================
# Main
# =============================================================================

def main():
    start_time = time.time()

    print("=" * 68)
    print("  ITC Batch Scanner  (HTTP / No-SSH mode)")
    print(f"  Subnet  : {SUBNET}.0.0/16")
    print(f"  Agent   : port {AGENT_PORT}")
    print(f"  Delay   : {INTER_HOST_DELAY}s between hosts")
    print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 68 + "\n")

    # Validate binary exists
    script_dir  = Path(os.path.dirname(os.path.abspath(__file__)))
    binary_path = script_dir / BINARY_NAME

    if sys.platform.startswith("win"):
        # Accept either the bare name or the .exe variant on Windows
        if not binary_path.is_file():
            binary_path = binary_path.with_suffix(".exe")

    if not binary_path.is_file():
        log.error(
            f"Binary not found: {binary_path}\n"
            f"Build it first:  python build_executable.py\n"
            f"Then place {BINARY_NAME} in the same folder as batch_scan.py"
        )
        sys.exit(1)

    log.info(f"Binary: {binary_path}  ({binary_path.stat().st_size / 1_048_576:.1f} MB)")

    local_output_dir = Path(LOCAL_OUTPUT_DIR)
    local_output_dir.mkdir(parents=True, exist_ok=True)

    # Ping sweep
    live_hosts = sweep_subnet(SUBNET)

    if not live_hosts:
        log.warning("No live hosts found in subnet.  Exiting.")
        sys.exit(0)

    # Filter to hosts with the agent running
    log.info(
        f"\nChecking which live hosts have the agent running "
        f"on port {AGENT_PORT} ..."
    )
    agent_hosts = [ip for ip in live_hosts if ping_agent(ip)]
    no_agent    = [ip for ip in live_hosts if ip not in agent_hosts]

    if no_agent:
        log.warning(
            f"{len(no_agent)} live host(s) have no agent — skipping: "
            f"{', '.join(no_agent[:10])}"
            f"{'...' if len(no_agent) > 10 else ''}"
        )

    if not agent_hosts:
        log.error(
            "No hosts have the agent running.\n"
            "Start agent.py on each server first:  sudo python3 agent.py &"
        )
        sys.exit(1)

    log.info(f"Scanning {len(agent_hosts)} host(s) with agent running ...\n")

    results = []
    for idx, ip in enumerate(agent_hosts, 1):
        log.info(f"── Host {idx}/{len(agent_hosts)}: {ip}  {'─'*28}")
        result = scan_host(ip, local_output_dir, str(binary_path))
        results.append(result)

        if idx < len(agent_hosts):
            log.info(f"  Waiting {INTER_HOST_DELAY}s before next host ...")
            time.sleep(INTER_HOST_DELAY)

    elapsed = time.time() - start_time
    print_summary(results, elapsed)


if __name__ == "__main__":
    main()