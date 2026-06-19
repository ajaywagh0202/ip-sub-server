#!/usr/bin/env python3
"""
deploy_client.py  —  ITC System Scanner  (Zero-Touch Client Auto-Deployer)
===========================================================================
Run this ONE script on any client PC.  It does EVERYTHING automatically:

  1. Downloads client_agent.py + app_inventory.py from the server
  2. Installs reportlab (pip) if missing
  3. Patches SERVER_URL in client_agent.py to point at the correct server
  4. Starts client_agent.py in the background
  5. Registers itself with the web server
  6. Installs itself as a persistent startup service (survives reboots)

USAGE — paste and run ONE command on the client PC:
----------------------------------------------------
  Windows (PowerShell as Administrator):
      python deploy_client.py --server http://192.168.1.10:5051

  Linux / macOS:
      python3 deploy_client.py --server http://192.168.1.10:5051

Or double-click after editing SERVER_URL below.

NO other steps needed.  The page at http://<server>:5051 will show this
PC within ~10 seconds.
"""

import os
import sys
import json
import time
import shutil
import socket
import argparse
import platform
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ── Default config (edit here if not using --server flag) ─────────────────────
DEFAULT_SERVER_URL = "http://192.168.1.10:5051"   # ← change to your server IP
AGENT_PORT         = 7979
SECRET_TOKEN       = "ITC2024CLIENT"
INSTALL_DIR        = Path.home() / "itc_agent"
SERVICE_NAME       = "itc-client-agent"
LOG_FILE           = INSTALL_DIR / "deploy.log"
# ──────────────────────────────────────────────────────────────────────────────

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX   = platform.system() == "Linux"
IS_MAC     = platform.system() == "Darwin"


# =============================================================================
# Logging
# =============================================================================

class Logger:
    def __init__(self):
        self._lines = []

    def log(self, msg: str, level: str = "INFO"):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line)
        self._lines.append(line)

    def ok(self,   msg): self.log(f"  ✓  {msg}", "OK")
    def warn(self, msg): self.log(f"  ⚠  {msg}", "WARN")
    def err(self,  msg): self.log(f"  ✗  {msg}", "ERROR")
    def step(self, msg): self.log(f"\n{'─'*55}\n  {msg}\n{'─'*55}")

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self._lines), encoding="utf-8")


L = Logger()


# =============================================================================
# Helpers
# =============================================================================

def run(cmd, check=True, capture=False, shell=False):
    kwargs = dict(shell=shell, text=True)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        kwargs.update(stdout=None, stderr=None)
    return subprocess.run(cmd, check=check, **kwargs)


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post_json(url: str, payload: dict, token: str, timeout: int = 15) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Token": token},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def python_exe() -> str:
    """Return the current Python executable path."""
    return sys.executable


# =============================================================================
# Step 1 — Verify server is reachable
# =============================================================================

def verify_server(server_url: str):
    L.step("Step 1/6 — Verifying server is reachable")
    try:
        data = json.loads(http_get(f"{server_url}/api/info", timeout=10))
        L.ok(f"Server reachable: {data.get('hostname')} ({data.get('ip')})  OS: {data.get('os')}")
    except Exception as exc:
        L.err(f"Cannot reach server at {server_url}: {exc}")
        L.err("Make sure web_app.py is running on the server and port 5051 is open.")
        sys.exit(1)


# =============================================================================
# Step 2 — Download agent files from server
# =============================================================================

def download_files(server_url: str):
    L.step("Step 2/6 — Downloading agent files from server")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    files = {
        "client_agent.py":  f"{server_url}/static/client_agent.py",
        "app_inventory.py": f"{server_url}/static/app_inventory.py",
    }

    for fname, url in files.items():
        dest = INSTALL_DIR / fname
        try:
            content = http_get(url, timeout=30)
            dest.write_bytes(content)
            L.ok(f"Downloaded {fname}  ({len(content):,} bytes)  ->  {dest}")
        except Exception as exc:
            # Fallback: copy from same directory as this script
            local = Path(__file__).parent / fname
            if local.exists():
                shutil.copy2(local, dest)
                L.warn(f"Server download failed ({exc}). Copied from local: {local}")
            else:
                L.err(f"Could not obtain {fname}: {exc}")
                L.err(f"Place {fname} next to deploy_client.py and retry.")
                sys.exit(1)


# =============================================================================
# Step 3 — Patch SERVER_URL inside client_agent.py
# =============================================================================

def patch_server_url(server_url: str):
    L.step("Step 3/6 — Configuring agent with server address")
    agent_path = INSTALL_DIR / "client_agent.py"
    text       = agent_path.read_text(encoding="utf-8")

    import re
    # Replace any SERVER_URL = "..." line
    patched = re.sub(
        r'^(SERVER_URL\s*=\s*)["\'].*?["\']',
        rf'\g<1>"{server_url}"',
        text,
        flags=re.MULTILINE,
    )

    if patched == text:
        L.warn("SERVER_URL line not found — appending it.")
        patched = f'SERVER_URL = "{server_url}"\n' + text

    agent_path.write_text(patched, encoding="utf-8")
    L.ok(f"SERVER_URL set to: {server_url}")


# =============================================================================
# Step 4 — Install Python dependencies
# =============================================================================

def install_deps():
    L.step("Step 4/6 — Installing Python dependencies")

    packages = ["reportlab"]
    for pkg in packages:
        try:
            __import__(pkg.split("[")[0].replace("-", "_"))
            L.ok(f"{pkg} already installed")
        except ImportError:
            L.log(f"Installing {pkg} ...")
            try:
                run([python_exe(), "-m", "pip", "install", pkg, "--quiet"])
                L.ok(f"{pkg} installed successfully")
            except subprocess.CalledProcessError as exc:
                L.err(f"pip install {pkg} failed: {exc}")
                L.err("Try manually: pip install reportlab")
                sys.exit(1)


# =============================================================================
# Step 5 — Start client_agent.py in background
# =============================================================================

def start_agent() -> bool:
    L.step("Step 5/6 — Starting client agent")

    agent_script = INSTALL_DIR / "client_agent.py"
    log_file     = INSTALL_DIR / "agent.log"

    # Kill any existing instance
    _kill_existing_agent()

    if IS_WINDOWS:
        # Use DETACHED_PROCESS so the window doesn't pop up
        import ctypes
        DETACHED_PROCESS        = 0x00000008
        CREATE_NO_WINDOW        = 0x08000000
        creationflags           = DETACHED_PROCESS | CREATE_NO_WINDOW
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [python_exe(), str(agent_script)],
                cwd=str(INSTALL_DIR),
                stdout=lf, stderr=lf,
                creationflags=creationflags,
                close_fds=True,
            )
    else:
        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [python_exe(), str(agent_script)],
                cwd=str(INSTALL_DIR),
                stdout=lf, stderr=lf,
                start_new_session=True,
            )

    # Save PID for future management
    pid_file = INSTALL_DIR / "agent.pid"
    pid_file.write_text(str(proc.pid))

    # Give it 4 seconds to start up
    time.sleep(4)
    if proc.poll() is not None:
        L.err(f"Agent process exited immediately (code {proc.returncode}). Check {log_file}")
        return False

    L.ok(f"Agent started (PID {proc.pid})  Log: {log_file}")
    return True


def _kill_existing_agent():
    """Kill any previously running agent instance."""
    pid_file = INSTALL_DIR / "agent.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(old_pid), "/F"],
                               capture_output=True, check=False)
            else:
                os.kill(old_pid, 9)
            L.log(f"Stopped previous agent (PID {old_pid})")
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)


# =============================================================================
# Step 6 — Install as startup service (survives reboots)
# =============================================================================

def install_startup_service(server_url: str):
    L.step("Step 6/6 — Installing startup service (auto-start on reboot)")

    if IS_WINDOWS:
        _install_windows_startup(server_url)
    elif IS_LINUX:
        _install_linux_systemd(server_url)
    elif IS_MAC:
        _install_mac_launchd(server_url)
    else:
        L.warn(f"Unsupported OS for auto-start: {platform.system()}")


# ── Windows: Task Scheduler ────────────────────────────────────────────────────

def _install_windows_startup(server_url: str):
    agent_script = INSTALL_DIR / "client_agent.py"
    task_xml     = INSTALL_DIR / f"{SERVICE_NAME}.xml"

    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT10S</Delay></BootTrigger>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT5S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval><Count>10</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python_exe()}</Command>
      <Arguments>"{agent_script}"</Arguments>
      <WorkingDirectory>{INSTALL_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    task_xml.write_text(xml, encoding="utf-16")

    try:
        run(["schtasks", "/Create", "/TN", SERVICE_NAME,
             "/XML", str(task_xml), "/F"], capture=True)
        L.ok(f"Windows Task Scheduler task '{SERVICE_NAME}' created — runs at login/boot")
    except subprocess.CalledProcessError as exc:
        L.warn(f"Task Scheduler registration failed (need Admin?): {exc}")
        L.warn("Agent is running now but won't auto-start after reboot.")
        _install_windows_registry_fallback()


def _install_windows_registry_fallback():
    """Fallback: HKCU Run key (no admin needed)."""
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        agent    = INSTALL_DIR / "client_agent.py"
        value    = f'"{python_exe()}" "{agent}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, SERVICE_NAME, 0, winreg.REG_SZ, value)
        L.ok("Fallback: added to Windows Registry Run key (current user auto-start)")
    except Exception as exc:
        L.warn(f"Registry fallback also failed: {exc}")


# ── Linux: systemd (with fallback to crontab) ──────────────────────────────────

def _install_linux_systemd(server_url: str):
    agent_script = INSTALL_DIR / "client_agent.py"
    unit_content = f"""[Unit]
Description=ITC Client Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python_exe()} {agent_script}
WorkingDirectory={INSTALL_DIR}
Restart=on-failure
RestartSec=10
StandardOutput=append:{INSTALL_DIR}/agent.log
StandardError=append:{INSTALL_DIR}/agent.log

[Install]
WantedBy=multi-user.target
"""
    # Try system-level systemd (requires root)
    system_unit = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
    user_unit   = Path.home() / f".config/systemd/user/{SERVICE_NAME}.service"

    installed = False

    if os.geteuid() == 0:
        # Running as root — install system-wide service
        try:
            system_unit.write_text(unit_content)
            run(["systemctl", "daemon-reload"],          capture=True, check=False)
            run(["systemctl", "enable", SERVICE_NAME],   capture=True, check=False)
            run(["systemctl", "restart", SERVICE_NAME],  capture=True, check=False)
            L.ok(f"systemd system service '{SERVICE_NAME}' installed and started")
            installed = True
        except Exception as exc:
            L.warn(f"System-level systemd install failed: {exc}")
    else:
        # No root — install user-level systemd service
        try:
            user_unit.parent.mkdir(parents=True, exist_ok=True)
            user_unit.write_text(unit_content)
            run(["systemctl", "--user", "daemon-reload"],         capture=True, check=False)
            run(["systemctl", "--user", "enable", SERVICE_NAME],  capture=True, check=False)
            run(["systemctl", "--user", "restart", SERVICE_NAME], capture=True, check=False)
            # Enable lingering so service starts even before login
            run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                capture=True, check=False)
            L.ok(f"systemd user service '{SERVICE_NAME}' installed (runs at login)")
            installed = True
        except Exception as exc:
            L.warn(f"User-level systemd install failed: {exc}")

    if not installed:
        _install_crontab_fallback(agent_script)


def _install_crontab_fallback(agent_script: Path):
    """Add a @reboot crontab entry."""
    try:
        result  = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        current = result.stdout if result.returncode == 0 else ""
        entry   = f"@reboot {python_exe()} {agent_script} >> {INSTALL_DIR}/agent.log 2>&1"
        if entry not in current:
            new_cron = current.rstrip("\n") + f"\n{entry}\n"
            proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
            proc.communicate(input=new_cron)
            L.ok("Fallback: @reboot crontab entry added")
        else:
            L.ok("@reboot crontab entry already exists")
    except Exception as exc:
        L.warn(f"Crontab fallback failed: {exc}")


# ── macOS: LaunchAgent ─────────────────────────────────────────────────────────

def _install_mac_launchd(server_url: str):
    agent_script = INSTALL_DIR / "client_agent.py"
    plist_dir    = Path.home() / "Library" / "LaunchAgents"
    plist_path   = plist_dir / f"com.itc.{SERVICE_NAME}.plist"

    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.itc.{SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe()}</string>
        <string>{agent_script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{INSTALL_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{INSTALL_DIR}/agent.log</string>
    <key>StandardErrorPath</key>
    <string>{INSTALL_DIR}/agent.log</string>
</dict>
</plist>"""

    plist_path.write_text(plist_content, encoding="utf-8")

    try:
        run(["launchctl", "unload", str(plist_path)], capture=True, check=False)
        run(["launchctl", "load",   str(plist_path)], capture=True)
        L.ok(f"macOS LaunchAgent installed — auto-starts at login")
    except Exception as exc:
        L.warn(f"LaunchAgent install failed: {exc}")


# =============================================================================
# Verify registration
# =============================================================================

def verify_registration(server_url: str):
    L.step("Verifying registration with server")
    local_ip   = get_local_ip()
    hostname   = socket.gethostname()
    deadline   = time.time() + 20

    while time.time() < deadline:
        try:
            data    = json.loads(http_get(f"{server_url}/api/clients", timeout=5))
            clients = data.get("clients", [])
            for c in clients:
                if c.get("ip") == local_ip or c.get("hostname") == hostname:
                    L.ok(f"This PC is now registered with the server!")
                    L.ok(f"  Hostname : {c['hostname']}")
                    L.ok(f"  IP       : {c['ip']}")
                    L.ok(f"  Status   : {c['status']}")
                    return True
        except Exception:
            pass
        time.sleep(2)

    L.warn("PC not yet visible in server client list — agent may still be starting.")
    L.warn(f"Check {INSTALL_DIR / 'agent.log'} for errors.")
    return False


# =============================================================================
# Print final summary
# =============================================================================

def print_summary(server_url: str, success: bool):
    local_ip = get_local_ip()
    hostname = socket.gethostname()
    print("\n" + "=" * 60)
    print("  ITC Client Auto-Deploy — COMPLETE")
    print("=" * 60)
    print(f"  This PC    : {hostname}  ({local_ip})")
    print(f"  Server     : {server_url}")
    print(f"  Install    : {INSTALL_DIR}")
    print(f"  Agent log  : {INSTALL_DIR / 'agent.log'}")
    print(f"  Deploy log : {LOG_FILE}")
    print()
    if success:
        print(f"  ✓  Open {server_url} in a browser.")
        print(f"  ✓  This PC will appear in the list — click 'Scan This PC'.")
    else:
        print(f"  ⚠  Agent may not have registered yet.")
        print(f"  ⚠  Wait 30 seconds then refresh {server_url}")
    print("=" * 60 + "\n")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ITC Client Auto-Deployer — zero-touch setup"
    )
    parser.add_argument(
        "--server", default=DEFAULT_SERVER_URL,
        help=f"Server URL (default: {DEFAULT_SERVER_URL})"
    )
    parser.add_argument(
        "--no-service", action="store_true",
        help="Skip startup service installation"
    )
    args = parser.parse_args()
    server_url = args.server.rstrip("/")

    print("=" * 60)
    print("  ITC System Scanner  —  Zero-Touch Client Deployer")
    print(f"  Server  : {server_url}")
    print(f"  This PC : {socket.gethostname()}  ({get_local_ip()})")
    print(f"  Python  : {python_exe()}")
    print(f"  Install : {INSTALL_DIR}")
    print("=" * 60 + "\n")

    verify_server(server_url)
    download_files(server_url)
    patch_server_url(server_url)
    install_deps()
    started = start_agent()

    if not args.no_service:
        install_startup_service(server_url)

    if started:
        registered = verify_registration(server_url)
    else:
        registered = False

    L.save(LOG_FILE)
    print_summary(server_url, registered)


if __name__ == "__main__":
    main()
