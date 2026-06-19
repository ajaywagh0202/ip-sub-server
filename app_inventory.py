#!/usr/bin/env python3
"""
Application Inventory Scanner
Runs without any parameters.  Auto-detects local IP and hostname.

Output filename format: <ipaddress>_<computername>_<datetimestamp>.pdf
"""

import os
import sys
import json
import time
import platform
import subprocess
import socket
import shutil
import ipaddress
import re
import types
from datetime import datetime

# Stub out PIL so reportlab does not crash when Pillow is absent
_pil_stub = types.ModuleType("PIL")
_pil_stub.Image = None
sys.modules.setdefault("PIL", _pil_stub)
sys.modules.setdefault("PIL.Image", types.ModuleType("PIL.Image"))

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)

# ── Colour palette ────────────────────────────────────────────────────────────
C_DARK   = colors.HexColor("#1A1F36")
C_MID    = colors.HexColor("#2D3561")
C_ACCENT = colors.HexColor("#4F6EF7")
C_LIGHT  = colors.HexColor("#EEF1FF")
C_GREEN  = colors.HexColor("#22C55E")
C_AMBER  = colors.HexColor("#F59E0B")
C_RED    = colors.HexColor("#EF4444")
C_PURPLE = colors.HexColor("#8B5CF6")
C_WHITE  = colors.white
C_GRID   = colors.HexColor("#CBD5E1")

SYSTEM = platform.system()


# =============================================================================
# Helpers
# =============================================================================

# When packaged as a windowed (--noconsole) PyInstaller .exe on Windows there
# are no valid standard handles to inherit. Passing stdin=DEVNULL and explicit
# stdout/stderr pipes avoids the "[WinError 6] The handle is invalid" crash,
# and CREATE_NO_WINDOW stops a console window from flashing on every command.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if platform.system() == "Windows" else 0


def run(cmd, timeout=20):
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def which(name):
    return shutil.which(name)


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def safe_str(val, maxlen=70):
    """Return a printable ASCII-safe string."""
    if not val:
        return "-"
    val = str(val)
    val = val.encode("ascii", errors="replace").decode("ascii")
    return val[:maxlen] if len(val) > maxlen else val


# =============================================================================
# 1. System Info
# =============================================================================

def get_system_info(target_ip):
    info = {
        "target_ip":     target_ip,
        "hostname":      socket.gethostname(),
        "os":            platform.system(),
        "os_version":    platform.version(),
        "os_release":    platform.release(),
        "architecture":  platform.machine(),
        "python_version": platform.python_version(),
        "scan_time":     datetime.now().isoformat(),
        "distro":        "",
    }
    if SYSTEM == "Linux":
        info["distro"] = run(
            "cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'"
        )
    elif SYSTEM == "Windows":
        info["distro"] = run(
            'powershell -Command "(Get-WmiObject Win32_OperatingSystem).Caption" 2>nul'
        )
    return info


# =============================================================================
# 2. Installed Packages
# =============================================================================

def get_installed_packages():
    packages = {}
    if SYSTEM == "Linux":
        if which("dpkg"):
            out = run("dpkg -l | awk '/^ii/ {print $2, $3}'")
            pkgs = [l.split() for l in out.splitlines() if l]
            packages["dpkg"] = [{"name": p[0], "version": p[1] if len(p) > 1 else ""} for p in pkgs]
        if which("rpm"):
            out = run("rpm -qa --queryformat '%{NAME} %{VERSION}\\n'")
            pkgs = [l.split() for l in out.splitlines() if l]
            packages["rpm"] = [{"name": p[0], "version": p[1] if len(p) > 1 else ""} for p in pkgs]
        if which("snap"):
            out = run("snap list 2>/dev/null | tail -n +2")
            pkgs = [l.split() for l in out.splitlines() if l]
            packages["snap"] = [{"name": p[0], "version": p[1] if len(p) > 1 else ""} for p in pkgs]
        if which("flatpak"):
            out = run("flatpak list --columns=application,version 2>/dev/null")
            pkgs = [l.split() for l in out.splitlines() if l]
            packages["flatpak"] = [{"name": p[0], "version": p[1] if len(p) > 1 else ""} for p in pkgs]
    elif SYSTEM == "Darwin":
        if which("brew"):
            out = run("brew list --versions 2>/dev/null")
            pkgs = [l.split() for l in out.splitlines() if l]
            packages["homebrew"] = [{"name": p[0], "version": p[1] if len(p) > 1 else ""} for p in pkgs]
    elif SYSTEM == "Windows":
        out = run(
            'powershell -Command "Get-Package | Select-Object Name,Version | ConvertTo-Csv -NoTypeInformation"'
        )
        lines = out.splitlines()[1:]
        packages["Windows Installed Programs"] = []
        for line in lines:
            parts = line.strip('"').split('","')
            packages["Windows Installed Programs"].append({"name": parts[0], "version": parts[1] if len(parts) > 1 else ""})
    # pip
    pip = which("pip3") or which("pip")
    if pip:
        out = run(f'"{pip}" list --format=columns 2>/dev/null | tail -n +3')
        pkgs = [l.split() for l in out.splitlines() if l]
        packages["python_pip"] = [{"name": p[0], "version": p[1] if len(p) > 1 else ""} for p in pkgs]
    # npm
    if which("npm"):
        out = run("npm list -g --depth=0 --parseable 2>/dev/null")
        packages["npm_global"] = [{"name": os.path.basename(p)} for p in out.splitlines() if p]
    # Sort every manager's list alphabetically by package name
    for mgr in packages:
        packages[mgr].sort(key=lambda p: p.get("name", "").lower())
    return packages


# =============================================================================
# 3. Databases
# =============================================================================

DB_CHECKS = [
    {"name": "MySQL / MariaDB", "binaries": ["mysql","mysqld"],           "port": 3306,  "vcmd": "mysql --version 2>/dev/null"},
    {"name": "PostgreSQL",      "binaries": ["psql","postgres"],          "port": 5432,  "vcmd": "psql --version 2>/dev/null"},
    {"name": "MongoDB",         "binaries": ["mongo","mongod"],           "port": 27017, "vcmd": "mongod --version 2>/dev/null"},
    {"name": "Redis",           "binaries": ["redis-cli","redis-server"], "port": 6379,  "vcmd": "redis-server --version 2>/dev/null"},
    {"name": "SQLite",          "binaries": ["sqlite3"],                  "port": None,  "vcmd": "sqlite3 --version 2>/dev/null"},
    {"name": "Elasticsearch",   "binaries": ["elasticsearch"],            "port": 9200,  "vcmd": ""},
    {"name": "Cassandra",       "binaries": ["cqlsh","cassandra"],        "port": 9042,  "vcmd": ""},
    {"name": "CouchDB",         "binaries": ["couchdb"],                  "port": 5984,  "vcmd": ""},
    {"name": "Neo4j",           "binaries": ["neo4j"],                    "port": 7474,  "vcmd": "neo4j --version 2>/dev/null"},
    {"name": "InfluxDB",        "binaries": ["influx","influxd"],         "port": 8086,  "vcmd": "influx version 2>/dev/null"},
    {"name": "MSSQL",           "binaries": ["sqlcmd"],                   "port": 1433,  "vcmd": "sqlcmd -? 2>/dev/null | head -1"},
    {"name": "Oracle",          "binaries": ["sqlplus"],                  "port": 1521,  "vcmd": ""},
    {"name": "RabbitMQ",        "binaries": ["rabbitmqctl"],              "port": 5672,  "vcmd": "rabbitmqctl version 2>/dev/null"},
    {"name": "Memcached",       "binaries": ["memcached"],                "port": 11211, "vcmd": "memcached --version 2>/dev/null"},
]

def get_databases():
    found = []
    for db in DB_CHECKS:
        binary = next((b for b in db["binaries"] if which(b)), None)
        listening = port_open(db["port"]) if db["port"] else False
        if binary or listening:
            ver = run(db["vcmd"]).split("\n")[0] if db["vcmd"] else "-"
            found.append({
                "name": db["name"], "binary": binary or "-",
                "port": str(db["port"]) if db["port"] else "-",
                "port_listening": listening, "version": ver or "-"
            })
    return found


# =============================================================================
# 4. Web / App Servers
# =============================================================================

WEB_CHECKS = [
    {"name": "Nginx",         "binaries": ["nginx"],                 "port": 80,   "vcmd": "nginx -v 2>&1"},
    {"name": "Apache HTTP",   "binaries": ["apache2","httpd"],       "port": 80,   "vcmd": "apache2 -v 2>/dev/null || httpd -v 2>/dev/null"},
    {"name": "Caddy",         "binaries": ["caddy"],                 "port": 80,   "vcmd": "caddy version 2>/dev/null"},
    {"name": "Lighttpd",      "binaries": ["lighttpd"],              "port": 80,   "vcmd": "lighttpd -v 2>/dev/null"},
    {"name": "Traefik",       "binaries": ["traefik"],               "port": 8080, "vcmd": "traefik version 2>/dev/null"},
    {"name": "HAProxy",       "binaries": ["haproxy"],               "port": None, "vcmd": "haproxy -v 2>/dev/null"},
    {"name": "Tomcat",        "binaries": [],                        "port": 8080, "vcmd": ""},
    {"name": "Gunicorn",      "binaries": ["gunicorn"],              "port": 8000, "vcmd": "gunicorn --version 2>/dev/null"},
    {"name": "uWSGI",         "binaries": ["uwsgi"],                 "port": None, "vcmd": "uwsgi --version 2>/dev/null"},
    {"name": "Uvicorn",       "binaries": ["uvicorn"],               "port": 8000, "vcmd": "uvicorn --version 2>/dev/null"},
    {"name": "Node.js",       "binaries": ["node"],                  "port": 3000, "vcmd": "node --version 2>/dev/null"},
    {"name": "PHP-FPM",       "binaries": ["php-fpm","php-fpm8.2"],  "port": 9000, "vcmd": "php --version 2>/dev/null | head -1"},
    {"name": "Ruby / Puma",   "binaries": ["puma","rails"],          "port": 3000, "vcmd": "ruby --version 2>/dev/null"},
    {"name": "Django",        "binaries": ["django-admin"],          "port": 8000, "vcmd": "django-admin --version 2>/dev/null"},
    {"name": "Flask",         "binaries": ["flask"],                 "port": 5051, "vcmd": "flask --version 2>/dev/null"},
]

def get_web_applications():
    found = []
    for svc in WEB_CHECKS:
        binary = next((b for b in svc["binaries"] if which(b)), None)
        listening = port_open(svc["port"]) if svc["port"] else False
        if binary or listening:
            ver = run(svc["vcmd"]).split("\n")[0] if svc["vcmd"] else "-"
            found.append({
                "name": svc["name"], "binary": binary or "-",
                "port": str(svc["port"]) if svc["port"] else "-",
                "port_listening": listening, "version": ver or "-"
            })
    return found


# =============================================================================
# 5. Running Services
# =============================================================================

def get_running_services():
    services = []
    if SYSTEM == "Linux":
        if which("systemctl"):
            out = run("systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null")
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    svc_name    = parts[0]
                    description = " ".join(parts[4:]) if len(parts) > 4 else ""
                    services.append({
                        "service":     svc_name,
                        "description": description,
                        "state":       "running",
                    })
    elif SYSTEM == "Darwin":
        out = run("launchctl list 2>/dev/null | grep -v '^-'")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                services.append({
                    "service":     parts[2],
                    "description": "",
                    "state":       "running",
                })
    elif SYSTEM == "Windows":
        out = run(
            'powershell -Command "Get-Service | Where-Object {$_.Status -eq \'Running\'} '
            '| Select-Object Name,DisplayName | ConvertTo-Csv -NoTypeInformation"'
        )
        for line in out.splitlines()[1:]:
            parts = line.strip('"').split('","')
            if parts:
                services.append({
                    "service":     parts[0],
                    "description": parts[1] if len(parts) > 1 else "",
                    "state":       "running",
                })
    services.sort(key=lambda x: x["service"].lower())
    return services


# =============================================================================
# 6. Docker & Kubernetes Containers
# =============================================================================

def get_containers():
    result = {"docker_version": "", "containers": [], "kubernetes": "", "k8s_pods": []}

    if which("docker"):
        result["docker_version"] = run("docker --version 2>/dev/null")
        out = run("docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null")
        for line in out.splitlines():
            parts = line.split("\t")
            result["containers"].append({
                "name":   parts[0] if len(parts) > 0 else "-",
                "image":  parts[1] if len(parts) > 1 else "-",
                "status": parts[2] if len(parts) > 2 else "-",
                "ports":  parts[3] if len(parts) > 3 else "-",
            })

    if which("kubectl"):
        result["kubernetes"] = run("kubectl version --client 2>/dev/null | head -1")
        out = run("kubectl get pods --all-namespaces --no-headers 2>/dev/null")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                result["k8s_pods"].append({
                    "namespace": parts[0], "name": parts[1],
                    "ready": parts[2], "status": parts[3],
                })
    return result


# =============================================================================
# 7. Language Runtimes
# =============================================================================

RUNTIMES = [
    ("Python",  "python3 --version 2>/dev/null || python --version 2>/dev/null"),
    ("Node.js", "node --version 2>/dev/null"),
    ("Java",    "java -version 2>&1 | head -1"),
    ("Ruby",    "ruby --version 2>/dev/null"),
    ("PHP",     "php --version 2>/dev/null | head -1"),
    ("Go",      "go version 2>/dev/null"),
    ("Rust",    "rustc --version 2>/dev/null"),
    ("Perl",    "perl --version 2>/dev/null | head -2 | tail -1"),
    (".NET",    "dotnet --version 2>/dev/null"),
    ("Swift",   "swift --version 2>/dev/null | head -1"),
    ("R",       "R --version 2>/dev/null | head -1"),
    ("Elixir",  "elixir --version 2>/dev/null | head -1"),
]

def get_runtimes():
    result = {}
    for name, cmd in RUNTIMES:
        ver = run(cmd)
        if ver:
            result[name] = ver
    return result


# =============================================================================
# 8. Virtual Machines  (NEW)
#    Detects: VirtualBox, VMware, Hyper-V, KVM/QEMU, Proxmox, Xen,
#             libvirt, LXC/LXD, Vagrant, Multipass
# =============================================================================

def _parse_vbox_vms(raw):
    """Parse 'VBoxManage list vms' output into list of dicts."""
    vms = []
    # Each running VM line: "MyVM" {uuid}
    for line in raw.splitlines():
        m = re.match(r'"(.+?)"\s+\{(.+?)\}', line)
        if m:
            vms.append({"name": m.group(1), "uuid": m.group(2)})
    return vms


def _vbox_vm_detail(uuid):
    """Get state + OS for a single VirtualBox VM."""
    out = run(f'VBoxManage showvminfo "{uuid}" --machinereadable 2>/dev/null')
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip().strip('"')
    return {
        "state":  info.get("VMState", "-"),
        "os":     info.get("ostype", "-"),
        "memory": info.get("memory", "-") + " MB" if info.get("memory") else "-",
        "cpus":   info.get("cpus", "-"),
    }


def _get_virtualbox():
    vbox_cmd = which("VBoxManage") or which("vboxmanage")
    if not vbox_cmd:
        return []
    ver = run(f'"{vbox_cmd}" --version 2>/dev/null')
    raw_all     = run(f'"{vbox_cmd}" list vms 2>/dev/null')
    raw_running = run(f'"{vbox_cmd}" list runningvms 2>/dev/null')
    all_vms     = _parse_vbox_vms(raw_all)
    running_ids = {m.group(2) for l in raw_running.splitlines()
                   if (m := re.search(r'\{(.+?)\}', l))}
    vms = []
    for vm in all_vms:
        detail = _vbox_vm_detail(vm["uuid"])
        vms.append({
            "platform":  "VirtualBox",
            "host_ver":  ver,
            "vm_name":   vm["name"],
            "uuid":      vm["uuid"],
            "state":     "running" if vm["uuid"] in running_ids else detail["state"],
            "guest_os":  detail["os"],
            "memory":    detail["memory"],
            "cpus":      detail["cpus"],
        })
    return vms


def _get_vmware():
    vms = []
    # VMware Workstation / Fusion (vmrun)
    vmrun = which("vmrun")
    if vmrun:
        ver_raw = run(f'"{vmrun}" 2>&1 | head -3')
        ver = next((l for l in ver_raw.splitlines() if "vmrun" in l.lower()), "VMware")
        running_raw = run(f'"{vmrun}" list 2>/dev/null')
        lines = running_raw.splitlines()
        for line in lines[1:]:   # skip "Total running VMs: N"
            line = line.strip()
            if line.endswith(".vmx"):
                vms.append({
                    "platform": "VMware Workstation/Fusion",
                    "host_ver": ver,
                    "vm_name":  os.path.splitext(os.path.basename(line))[0],
                    "uuid":     "-",
                    "state":    "running",
                    "guest_os": "-",
                    "memory":   "-",
                    "cpus":     "-",
                })
    # VMware ESXi (vim-cmd)
    if which("vim-cmd"):
        ver = run("vmware -v 2>/dev/null")
        out = run("vim-cmd vmsvc/getallvms 2>/dev/null")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if parts:
                vmid = parts[0]
                name = parts[1] if len(parts) > 1 else "-"
                state_raw = run(f"vim-cmd vmsvc/power.getstate {vmid} 2>/dev/null")
                state = "running" if "on" in state_raw.lower() else "off"
                vms.append({
                    "platform": "VMware ESXi",
                    "host_ver": ver,
                    "vm_name":  name,
                    "uuid":     vmid,
                    "state":    state,
                    "guest_os": "-",
                    "memory":   "-",
                    "cpus":     "-",
                })
    return vms


def _get_hyperv():
    vms = []
    if SYSTEM != "Windows":
        return vms
    out = run(
        'powershell -Command "Get-VM | Select-Object Name,State,Generation,'
        'MemoryAssigned,ProcessorCount,Version | ConvertTo-Csv -NoTypeInformation" 2>nul'
    )
    lines = out.splitlines()
    if len(lines) < 2:
        return vms
    headers = [h.strip('"') for h in lines[0].split(",")]
    for row in lines[1:]:
        cols = [c.strip('"') for c in row.split(",")]
        if len(cols) < len(headers):
            continue
        d = dict(zip(headers, cols))
        mem_mb = "-"
        try:
            mem_mb = str(int(d.get("MemoryAssigned", "0")) // 1_048_576) + " MB"
        except Exception:
            pass
        vms.append({
            "platform": "Hyper-V",
            "host_ver": d.get("Version", "-"),
            "vm_name":  d.get("Name", "-"),
            "uuid":     "-",
            "state":    d.get("State", "-"),
            "guest_os": f"Gen {d.get('Generation','-')}",
            "memory":   mem_mb,
            "cpus":     d.get("ProcessorCount", "-"),
        })
    return vms


def _get_kvm_qemu():
    vms = []
    virsh = which("virsh")
    if not virsh:
        return vms
    ver = run(f'"{virsh}" version 2>/dev/null | head -2')
    out = run(f'"{virsh}" list --all 2>/dev/null')
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 3:
            dom_id   = parts[0]
            dom_name = parts[1]
            state    = " ".join(parts[2:])
            # try to get OS/memory info
            info_raw = run(f'"{virsh}" dominfo "{dom_name}" 2>/dev/null')
            mem = "-"
            cpus = "-"
            for l in info_raw.splitlines():
                if l.startswith("Max memory"):
                    try:
                        mem = str(int(l.split(":")[1].strip().split()[0]) // 1024) + " MB"
                    except Exception:
                        pass
                if l.startswith("CPU(s)"):
                    cpus = l.split(":")[1].strip()
            vms.append({
                "platform": "KVM / QEMU (libvirt)",
                "host_ver": ver.splitlines()[0] if ver else "-",
                "vm_name":  dom_name,
                "uuid":     dom_id,
                "state":    state,
                "guest_os": "-",
                "memory":   mem,
                "cpus":     cpus,
            })
    return vms


def _get_proxmox():
    vms = []
    if not which("qm") and not which("pct"):
        return vms
    ver = run("pveversion 2>/dev/null | head -1")
    # QEMU VMs
    if which("qm"):
        out = run("qm list 2>/dev/null")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                vms.append({
                    "platform": "Proxmox VE (QEMU)",
                    "host_ver": ver,
                    "vm_name":  parts[1] if len(parts) > 1 else "-",
                    "uuid":     parts[0],
                    "state":    parts[2] if len(parts) > 2 else "-",
                    "guest_os": "-",
                    "memory":   parts[4] + " MB" if len(parts) > 4 else "-",
                    "cpus":     "-",
                })
    # LXC Containers
    if which("pct"):
        out = run("pct list 2>/dev/null")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                vms.append({
                    "platform": "Proxmox VE (LXC)",
                    "host_ver": ver,
                    "vm_name":  " ".join(parts[2:]) if len(parts) > 2 else parts[0],
                    "uuid":     parts[0],
                    "state":    parts[1] if len(parts) > 1 else "-",
                    "guest_os": "Container",
                    "memory":   "-",
                    "cpus":     "-",
                })
    return vms


def _get_xen():
    vms = []
    xl = which("xl") or which("xm")
    if not xl:
        return vms
    ver = run(f'"{xl}" info 2>/dev/null | grep "xen_version"')
    out = run(f'"{xl}" list 2>/dev/null')
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            vms.append({
                "platform": "Xen",
                "host_ver": ver.split(":")[-1].strip() if ":" in ver else "-",
                "vm_name":  parts[0],
                "uuid":     parts[1],
                "state":    parts[4],
                "guest_os": "-",
                "memory":   parts[2] + " MB" if parts[2].isdigit() else "-",
                "cpus":     parts[3],
            })
    return vms


def _get_lxd():
    vms = []
    lxc = which("lxc")
    if not lxc:
        return vms
    ver = run(f'"{lxc}" version 2>/dev/null')
    out = run(f'"{lxc}" list --format csv 2>/dev/null')
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2:
            vms.append({
                "platform": "LXD / Incus",
                "host_ver": ver,
                "vm_name":  parts[0].strip(),
                "uuid":     "-",
                "state":    parts[1].strip(),
                "guest_os": parts[4].strip() if len(parts) > 4 else "-",
                "memory":   "-",
                "cpus":     "-",
            })
    return vms


def _get_vagrant():
    vms = []
    if not which("vagrant"):
        return vms
    ver = run("vagrant --version 2>/dev/null")
    out = run("vagrant global-status 2>/dev/null")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].isalnum() and len(parts[0]) == 7:
            vms.append({
                "platform": "Vagrant",
                "host_ver": ver,
                "vm_name":  parts[1],
                "uuid":     parts[0],
                "state":    parts[2],
                "guest_os": parts[3],
                "memory":   "-",
                "cpus":     "-",
            })
    return vms


def _get_multipass():
    vms = []
    if not which("multipass"):
        return vms
    ver = run("multipass version 2>/dev/null | head -1")
    out = run("multipass list --format csv 2>/dev/null")
    lines = out.splitlines()
    if len(lines) < 2:
        return vms
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) >= 3:
            vms.append({
                "platform": "Multipass",
                "host_ver": ver,
                "vm_name":  parts[0].strip(),
                "uuid":     "-",
                "state":    parts[1].strip(),
                "guest_os": parts[2].strip(),
                "memory":   "-",
                "cpus":     "-",
            })
    return vms


def _detect_host_hypervisor():
    """
    Detect if THIS machine is itself a VM guest (not a host).
    Returns a string describing the hypervisor or empty string.
    """
    # Linux: read DMI/CPUID info
    if SYSTEM == "Linux":
        dmi = run("cat /sys/class/dmi/id/product_name 2>/dev/null")
        vendor = run("cat /sys/class/dmi/id/sys_vendor 2>/dev/null")
        combined = (dmi + " " + vendor).lower()
        if "vmware"    in combined: return "VMware Guest"
        if "virtualbox" in combined or "innotek" in combined: return "VirtualBox Guest"
        if "kvm"       in combined: return "KVM Guest"
        if "hyper-v"   in combined or "microsoft" in combined: return "Hyper-V Guest"
        if "xen"       in combined: return "Xen Guest"
        if "qemu"      in combined: return "QEMU Guest"
        # systemd-detect-virt
        virt = run("systemd-detect-virt 2>/dev/null")
        if virt and virt != "none":
            return f"Virtual Guest ({virt})"
    elif SYSTEM == "Windows":
        model = run('powershell -Command "(Get-WmiObject Win32_ComputerSystem).Model" 2>nul').lower()
        if "vmware"     in model: return "VMware Guest"
        if "virtualbox" in model: return "VirtualBox Guest"
        if "virtual"    in model: return "Virtual Machine Guest"
    elif SYSTEM == "Darwin":
        hw = run("sysctl -n hw.model 2>/dev/null").lower()
        if "vmware" in hw or "virtual" in hw:
            return "Virtual Machine Guest"
    return ""


def get_virtual_machines():
    """Aggregate all VM platforms."""
    print("      Scanning VirtualBox ...")
    vbox = _get_virtualbox()
    print("      Scanning VMware ...")
    vmw  = _get_vmware()
    print("      Scanning Hyper-V ...")
    hv   = _get_hyperv()
    print("      Scanning KVM/QEMU ...")
    kvm  = _get_kvm_qemu()
    print("      Scanning Proxmox ...")
    prox = _get_proxmox()
    print("      Scanning Xen ...")
    xen  = _get_xen()
    print("      Scanning LXD ...")
    lxd  = _get_lxd()
    print("      Scanning Vagrant ...")
    vag  = _get_vagrant()
    print("      Scanning Multipass ...")
    mp   = _get_multipass()

    all_vms = vbox + vmw + hv + kvm + prox + xen + lxd + vag + mp
    host_virt = _detect_host_hypervisor()

    return {
        "host_is_vm":    host_virt,
        "total":         len(all_vms),
        "vms":           all_vms,
    }


# =============================================================================
# 9. Disk Information
# =============================================================================

def _fmt_bytes(n):
    """Return a human-readable size string (GiB)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n < 0:
        return "-"
    gib = n / (1024 ** 3)
    if gib >= 1:
        return f"{gib:.1f} GiB"
    mib = n / (1024 ** 2)
    if mib >= 1:
        return f"{mib:.1f} MiB"
    return f"{n} B"


def get_disk_info():
    """
    Return a dict with:
      - partitions: list of dicts per mount point
      - total_size, total_used, total_free  (aggregate across all local disks)
    """
    partitions = []
    total_size = total_used = total_free = 0

    if SYSTEM in ("Linux", "Darwin"):
        # Use 'df -Pk' (POSIX, 1-KiB blocks) for reliable cross-platform output
        out = run("df -Pk 2>/dev/null")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            fs, size_kb, used_kb, avail_kb, use_pct, mount = (
                parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            )
            # Skip pseudo/virtual filesystems
            if any(mount.startswith(p) for p in (
                "/proc", "/sys", "/dev", "/run", "/snap",
                "tmpfs", "devtmpfs", "udev"
            )):
                continue
            if fs in ("tmpfs", "devtmpfs", "udev", "none", "overlay", "shm"):
                continue
            try:
                size_b  = int(size_kb)  * 1024
                used_b  = int(used_kb)  * 1024
                avail_b = int(avail_kb) * 1024
            except ValueError:
                continue
            partitions.append({
                "filesystem": fs,
                "mount":      mount,
                "size":       _fmt_bytes(size_b),
                "used":       _fmt_bytes(used_b),
                "free":       _fmt_bytes(avail_b),
                "use_pct":    use_pct,
                "size_b":     size_b,
            })
            total_size += size_b
            total_used += used_b
            total_free += avail_b

    elif SYSTEM == "Windows":
        out = run(
            'powershell -Command "'
            'Get-PSDrive -PSProvider FileSystem | '
            'Select-Object Name,Root,Used,Free | '
            'ConvertTo-Csv -NoTypeInformation" 2>nul'
        )
        for line in out.splitlines()[1:]:
            cols = [c.strip('"') for c in line.split(",")]
            if len(cols) < 4:
                continue
            drv, root, used_s, free_s = cols[0], cols[1], cols[2], cols[3]
            try:
                used_b = int(used_s) if used_s else 0
                free_b = int(free_s) if free_s else 0
                size_b = used_b + free_b
            except ValueError:
                continue
            use_pct = f"{round(used_b/size_b*100)}%" if size_b else "-"
            partitions.append({
                "filesystem": drv + ":",
                "mount":      root or drv + ":\\",
                "size":       _fmt_bytes(size_b),
                "used":       _fmt_bytes(used_b),
                "free":       _fmt_bytes(free_b),
                "use_pct":    use_pct,
                "size_b":     size_b,
            })
            total_size += size_b
            total_used += used_b
            total_free += free_b

    return {
        "partitions":  partitions,
        "total_size":  _fmt_bytes(total_size),
        "total_used":  _fmt_bytes(total_used),
        "total_free":  _fmt_bytes(total_free),
        "total_use_pct": f"{round(total_used/total_size*100)}%" if total_size else "-",
    }


# =============================================================================
# 10. System Health
# =============================================================================

def get_system_health():
    """
    Collect CPU, RAM, swap, load-average, uptime, and top-process info.
    Returns a dict ready for the PDF health section.
    """
    health = {
        "cpu_count":      "",
        "cpu_model":      "",
        "cpu_usage_pct":  "",
        "ram_total":      "",
        "ram_used":       "",
        "ram_free":       "",
        "ram_pct":        "",
        "swap_total":     "",
        "swap_used":      "",
        "swap_pct":       "",
        "load_avg":       "",
        "uptime":         "",
        "top_processes":  [],   # list of {pid, name, cpu, mem}
    }

    if SYSTEM == "Linux":
        # CPU model
        health["cpu_model"] = run(
            "grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2"
        ).strip(" \t")
        # CPU count (logical)
        health["cpu_count"] = run("nproc 2>/dev/null")

        # RAM from /proc/meminfo (kB)
        meminfo = {}
        raw = run("cat /proc/meminfo 2>/dev/null")
        for line in raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                try:
                    meminfo[k.strip()] = int(v.strip().split()[0]) * 1024
                except (ValueError, IndexError):
                    pass
        ram_total = meminfo.get("MemTotal", 0)
        ram_avail = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        ram_used  = ram_total - ram_avail
        swap_total = meminfo.get("SwapTotal", 0)
        swap_free  = meminfo.get("SwapFree", 0)
        swap_used  = swap_total - swap_free

        health["ram_total"] = _fmt_bytes(ram_total)
        health["ram_used"]  = _fmt_bytes(ram_used)
        health["ram_free"]  = _fmt_bytes(ram_avail)
        health["ram_pct"]   = f"{round(ram_used / ram_total * 100)}%" if ram_total else "-"
        health["swap_total"] = _fmt_bytes(swap_total)
        health["swap_used"]  = _fmt_bytes(swap_used)
        health["swap_pct"]   = f"{round(swap_used / swap_total * 100)}%" if swap_total else "0%"

        # Load average
        health["load_avg"] = run("cat /proc/loadavg 2>/dev/null").split()[:3]
        health["load_avg"] = "  ".join(health["load_avg"]) if health["load_avg"] else "-"

        # Uptime
        health["uptime"] = run("uptime -p 2>/dev/null") or run("uptime 2>/dev/null")

        # Top 10 processes by CPU
        ps_out = run(
            "ps aux --sort=-%cpu 2>/dev/null | awk 'NR>1 {print $2,$11,$3,$4}' | head -10"
        )
        for line in ps_out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                health["top_processes"].append({
                    "pid":  parts[0],
                    "name": os.path.basename(parts[1])[:28],
                    "cpu":  parts[2] + "%",
                    "mem":  parts[3] + "%",
                })

        # CPU usage (1-second snapshot via /proc/stat)
        def _cpu_pct():
            try:
                def _read():
                    line = open("/proc/stat").readline().split()
                    return [int(x) for x in line[1:]]
                s1 = _read(); time.sleep(0.5); s2 = _read()
                idle1, idle2 = s1[3], s2[3]
                tot1, tot2 = sum(s1), sum(s2)
                diff_idle = idle2 - idle1
                diff_tot  = tot2  - tot1
                return f"{round((1 - diff_idle / diff_tot) * 100)}%" if diff_tot else "-"
            except Exception:
                return "-"
        import time as _time_mod
        health["cpu_usage_pct"] = _cpu_pct()

    elif SYSTEM == "Darwin":
        health["cpu_model"] = run("sysctl -n machdep.cpu.brand_string 2>/dev/null")
        health["cpu_count"] = run("sysctl -n hw.logicalcpu 2>/dev/null")
        ram_total = int(run("sysctl -n hw.memsize 2>/dev/null") or 0)
        health["ram_total"] = _fmt_bytes(ram_total)
        health["load_avg"]  = run("sysctl -n vm.loadavg 2>/dev/null")
        health["uptime"]    = run("uptime 2>/dev/null")
        ps_out = run("ps aux -r | awk 'NR>1 {print $2,$11,$3,$4}' | head -10 2>/dev/null")
        for line in ps_out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                health["top_processes"].append({
                    "pid": parts[0], "name": os.path.basename(parts[1])[:28],
                    "cpu": parts[2] + "%", "mem": parts[3] + "%",
                })

    elif SYSTEM == "Windows":
        health["cpu_model"] = run(
            'powershell -Command "(Get-WmiObject Win32_Processor).Name" 2>nul'
        )
        health["cpu_count"] = run(
            'powershell -Command "(Get-WmiObject Win32_ComputerSystem).NumberOfLogicalProcessors" 2>nul'
        )
        health["cpu_usage_pct"] = run(
            'powershell -Command "(Get-WmiObject Win32_Processor).LoadPercentage" 2>nul'
        ) + "%"
        # RAM
        os_info = run(
            'powershell -Command "Get-WmiObject Win32_OperatingSystem | '
            'Select TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Csv -NoTypeInformation" 2>nul'
        )
        lines = os_info.splitlines()
        if len(lines) > 1:
            cols = [c.strip('"') for c in lines[1].split(",")]
            try:
                ram_total = int(cols[0]) * 1024
                ram_free  = int(cols[1]) * 1024
                ram_used  = ram_total - ram_free
                health["ram_total"] = _fmt_bytes(ram_total)
                health["ram_used"]  = _fmt_bytes(ram_used)
                health["ram_free"]  = _fmt_bytes(ram_free)
                health["ram_pct"]   = f"{round(ram_used / ram_total * 100)}%" if ram_total else "-"
            except (ValueError, IndexError):
                pass
        health["uptime"] = run(
            'powershell -Command "(Get-Date) - (gcim Win32_OperatingSystem).LastBootUpTime | '
            'Select-Object -ExpandProperty TotalHours" 2>nul'
        )

    return health


# =============================================================================
# 11. Cyber Security Posture
# =============================================================================

# Known risky / legacy services and their severity
_RISKY_SERVICES = {
    # (substring-match on service name)  ->  (severity, reason)
    "telnet":      ("CRITICAL", "Cleartext remote login — replace with SSH immediately"),
    "rsh":         ("CRITICAL", "r-commands transmit credentials in cleartext"),
    "rlogin":      ("CRITICAL", "r-commands transmit credentials in cleartext"),
    "rexec":       ("CRITICAL", "r-commands transmit credentials in cleartext"),
    "ftp":         ("HIGH",     "Cleartext file transfer — use SFTP/SCP instead"),
    "tftp":        ("HIGH",     "Unauthenticated trivial FTP — disable if unused"),
    "snmp":        ("HIGH",     "SNMP v1/v2 community strings are cleartext"),
    "finger":      ("HIGH",     "Information disclosure — almost never needed"),
    "rpc":         ("MEDIUM",   "RPC portmapper exposes attack surface"),
    "nfs":         ("MEDIUM",   "NFS mounts can allow unauthorised data access"),
    "smb":         ("MEDIUM",   "SMB lateral-movement risk — verify necessity"),
    "samba":       ("MEDIUM",   "SMB lateral-movement risk — verify necessity"),
    "vnc":         ("HIGH",     "VNC often runs without encryption — audit access"),
    "x11":         ("MEDIUM",   "X11 forwarding can be exploited if unrestricted"),
    "ircd":        ("MEDIUM",   "IRC daemon rarely needed; common C2 pivot point"),
    "redis":       ("HIGH",     "Redis has no auth by default — bind to localhost"),
    "memcached":   ("HIGH",     "Memcached has no auth — never expose to internet"),
    "mongodb":     ("HIGH",     "MongoDB default install has no auth — verify"),
    "elasticsearch":("HIGH",   "Elasticsearch exposes data via unauthenticated HTTP"),
    "couchdb":     ("HIGH",     "CouchDB admin party mode exposes all data"),
    "rabbitmq":    ("MEDIUM",   "RabbitMQ default guest/guest credentials"),
    "zookeeper":   ("MEDIUM",   "ZooKeeper no auth by default"),
    "jupyter":     ("HIGH",     "Jupyter may allow remote code execution"),
    "jenkins":     ("HIGH",     "Jenkins admin console should never be public"),
}

_RISKY_PORTS = {
    23:    ("CRITICAL", "Telnet — cleartext remote access"),
    21:    ("HIGH",     "FTP — cleartext credentials"),
    69:    ("HIGH",     "TFTP — unauthenticated file transfer"),
    111:   ("MEDIUM",   "RPC portmapper"),
    512:   ("CRITICAL", "rexec — cleartext remote execution"),
    513:   ("CRITICAL", "rlogin — cleartext remote login"),
    514:   ("CRITICAL", "rsh — cleartext remote shell"),
    515:   ("LOW",      "LPD printer daemon — often unnecessary"),
    2049:  ("MEDIUM",   "NFS — verify access controls"),
    5900:  ("HIGH",     "VNC — verify encryption and access"),
    6379:  ("HIGH",     "Redis — verify authentication enabled"),
    11211: ("HIGH",     "Memcached — no auth by default"),
    27017: ("HIGH",     "MongoDB — verify authentication enabled"),
    9200:  ("HIGH",     "Elasticsearch HTTP — verify authentication"),
    5984:  ("HIGH",     "CouchDB — verify authentication"),
    5672:  ("MEDIUM",   "RabbitMQ AMQP — verify credentials"),
    8161:  ("MEDIUM",   "ActiveMQ admin console"),
    2181:  ("MEDIUM",   "ZooKeeper — verify access controls"),
    8888:  ("HIGH",     "Jupyter Notebook — remote code execution risk"),
    8080:  ("LOW",      "Alternate HTTP — confirm service identity"),
    8443:  ("LOW",      "Alternate HTTPS — confirm service identity"),
}

_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
_SEV_COLOR = {
    "CRITICAL": "#EF4444",
    "HIGH":     "#F97316",
    "MEDIUM":   "#F59E0B",
    "LOW":      "#22C55E",
    "INFO":     "#94A3B8",
}


def get_cyber_security_posture(health, services, databases, web_apps):
    """
    Analyse already-collected data and produce a list of findings with severity.
    Returns dict:
      {
        "score":    int  0-100  (higher = better),
        "grade":    str  A/B/C/D/F,
        "findings": [ {severity, category, title, detail}, ... ],
        "summary":  { "CRITICAL": n, "HIGH": n, "MEDIUM": n, "LOW": n, "INFO": n }
      }
    """
    findings = []

    def add(severity, category, title, detail):
        findings.append({
            "severity": severity,
            "category": category,
            "title":    title,
            "detail":   detail,
        })

    # ── 1. Risky running services ─────────────────────────────────────────────
    svc_names = [s["service"].lower() for s in services]
    for svc_key, (sev, reason) in _RISKY_SERVICES.items():
        matched = [s["service"] for s in services if svc_key in s["service"].lower()]
        if matched:
            add(sev, "Running Services",
                f"Risky service detected: {matched[0]}",
                reason)

    # ── 2. Risky listening ports (from web-apps and databases) ────────────────
    all_detected = databases + web_apps
    open_ports = set()
    for item in all_detected:
        if item.get("port_listening"):
            try:
                open_ports.add(int(item["port"]))
            except (ValueError, TypeError):
                pass
    # Also probe known risky ports directly
    for port, (sev, reason) in _RISKY_PORTS.items():
        if port_open(port) or port in open_ports:
            add(sev, "Open Ports",
                f"Risky port open: {port}",
                reason)

    # ── 3. Unauthenticated databases ─────────────────────────────────────────
    no_auth_dbs = {"Redis", "MongoDB", "Memcached", "Elasticsearch", "CouchDB"}
    for db in databases:
        if db["name"] in no_auth_dbs and db.get("port_listening"):
            add("HIGH", "Database Security",
                f"{db['name']} listening — verify authentication",
                "This database engine ships with no authentication by default. "
                "Confirm auth is enabled and port is not publicly reachable.")

    # ── 4. Memory pressure ───────────────────────────────────────────────────
    if health.get("ram_pct"):
        pct_str = health["ram_pct"].rstrip("%")
        try:
            ram_pct = int(pct_str)
            if ram_pct >= 95:
                add("HIGH",   "System Health", "RAM critically high",
                    f"RAM usage is {health['ram_pct']} — system may be under memory pressure, "
                    "increasing risk of OOM kills and service crashes.")
            elif ram_pct >= 85:
                add("MEDIUM", "System Health", "RAM usage elevated",
                    f"RAM usage is {health['ram_pct']} — monitor for memory leaks.")
        except ValueError:
            pass

    # ── 5. Swap in use ────────────────────────────────────────────────────────
    if health.get("swap_pct"):
        pct_str = health["swap_pct"].rstrip("%")
        try:
            if int(pct_str) > 20:
                add("MEDIUM", "System Health", "Swap usage elevated",
                    f"Swap usage is {health['swap_pct']} — indicates RAM pressure; "
                    "performance and stability may be affected.")
        except ValueError:
            pass

    # ── 6. Disk pressure ──────────────────────────────────────────────────────
    # (disk data is not passed in; we re-probe here for completeness)
    if SYSTEM == "Linux":
        df_out = run("df -Pk 2>/dev/null")
        for line in df_out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                pct = int(parts[4].rstrip("%"))
                mount = parts[5]
                if pct >= 95:
                    add("HIGH", "Disk Health",
                        f"Disk critically full: {mount} at {pct}%",
                        "Filesystem near capacity — write failures and service crashes imminent.")
                elif pct >= 85:
                    add("MEDIUM", "Disk Health",
                        f"Disk usage high: {mount} at {pct}%",
                        "Free up space or expand the volume before it impacts services.")
            except ValueError:
                pass

    # ── 7. Root SSH login check ───────────────────────────────────────────────
    if SYSTEM == "Linux":
        sshd_cfg = run("grep -i 'PermitRootLogin' /etc/ssh/sshd_config 2>/dev/null")
        if "yes" in sshd_cfg.lower() and "#" not in sshd_cfg.split("PermitRootLogin")[0]:
            add("HIGH", "SSH Hardening",
                "SSH PermitRootLogin is enabled",
                "Direct root SSH login should be disabled. Use a non-root user with sudo.")
        elif not sshd_cfg or "prohibit-password" in sshd_cfg.lower():
            add("INFO", "SSH Hardening",
                "SSH root login: prohibit-password (key only)",
                "Root can log in via SSH with a key. Consider disabling root login entirely.")

        # Password auth
        pw_auth = run("grep -i 'PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null")
        if "yes" in pw_auth.lower() and not pw_auth.strip().startswith("#"):
            add("HIGH", "SSH Hardening",
                "SSH PasswordAuthentication is enabled",
                "Password-based SSH login is susceptible to brute force. "
                "Disable it and enforce key-based authentication.")

    # ── 8. Firewall status ────────────────────────────────────────────────────
    if SYSTEM == "Linux":
        ufw_status = run("ufw status 2>/dev/null | head -1")
        firewalld  = run("systemctl is-active firewalld 2>/dev/null")
        iptables   = run("iptables -L INPUT -n 2>/dev/null | wc -l")
        if "active" in ufw_status.lower():
            add("INFO", "Firewall", "UFW firewall is active", "Host-based firewall (UFW) is running.")
        elif "active" in firewalld.lower():
            add("INFO", "Firewall", "firewalld is active", "Host-based firewall (firewalld) is running.")
        elif iptables and int(iptables.strip() or 0) <= 3:
            add("HIGH", "Firewall",
                "No active host-based firewall detected",
                "Neither UFW, firewalld, nor iptables rules were found. "
                "All ports may be reachable from the network.")
        else:
            add("LOW", "Firewall", "iptables rules present",
                "Custom iptables rules found — verify they match the intended security policy.")

    # ── 9. SELinux / AppArmor ─────────────────────────────────────────────────
    if SYSTEM == "Linux":
        selinux = run("getenforce 2>/dev/null")
        apparmor = run("aa-status 2>/dev/null | head -1")
        if selinux.lower() == "enforcing":
            add("INFO", "MAC / LSM", "SELinux is Enforcing", "Mandatory access control is active.")
        elif selinux.lower() == "permissive":
            add("MEDIUM", "MAC / LSM", "SELinux is Permissive",
                "SELinux is loaded but not enforcing — switch to Enforcing mode.")
        elif "profiles" in apparmor.lower():
            add("INFO", "MAC / LSM", "AppArmor is active",
                "AppArmor profiles loaded — verify critical services are confined.")
        else:
            add("MEDIUM", "MAC / LSM", "No MAC (SELinux/AppArmor) detected",
                "Mandatory access control reduces blast radius of compromises. "
                "Consider enabling SELinux or AppArmor.")

    # ── 10. Unattended upgrades / auto-patch ─────────────────────────────────
    if SYSTEM == "Linux":
        unattended = run("dpkg -l unattended-upgrades 2>/dev/null | grep '^ii'")
        dnf_auto   = run("rpm -q dnf-automatic 2>/dev/null")
        if unattended:
            add("INFO", "Patch Management",
                "unattended-upgrades installed",
                "Automatic security patches are available — verify the service is enabled.")
        elif "not installed" not in dnf_auto and dnf_auto:
            add("INFO", "Patch Management",
                "dnf-automatic installed",
                "Automatic security patches are available — verify the timer is active.")
        else:
            add("MEDIUM", "Patch Management",
                "No automatic patching tool detected",
                "Consider installing unattended-upgrades (Debian/Ubuntu) or dnf-automatic (RHEL/CentOS) "
                "to keep the system patched automatically.")

    # ── 11. World-writable files quick-check ─────────────────────────────────
    if SYSTEM == "Linux":
        ww = run("find /etc /usr/local/bin /usr/bin -maxdepth 2 -perm -o+w -type f 2>/dev/null | head -5")
        if ww.strip():
            add("HIGH", "File Permissions",
                "World-writable files found in system paths",
                f"Files: {ww.strip()[:200]} — these can be modified by any local user.")

    # ── 12. Sudo nopasswd ─────────────────────────────────────────────────────
    if SYSTEM == "Linux":
        nopasswd = run("grep -r 'NOPASSWD' /etc/sudoers /etc/sudoers.d/ 2>/dev/null | grep -v '^#'")
        if nopasswd.strip():
            add("HIGH", "Privilege Escalation",
                "NOPASSWD sudo entries detected",
                f"Sudo without password: {nopasswd.strip()[:200]} — "
                "an attacker gaining that user's session gets instant root.")

    # ── 13. Core dump check ────────────────────────────────────────────────────
    if SYSTEM == "Linux":
        core_pattern = run("cat /proc/sys/kernel/core_pattern 2>/dev/null")
        if core_pattern and not core_pattern.startswith("|/usr/share/apport") and core_pattern.strip() != "":
            add("LOW", "Core Dumps",
                "Core dumps may be enabled",
                f"core_pattern = '{core_pattern.strip()}' — core files can expose sensitive memory. "
                "Confirm they are limited or redirected securely.")

    # ─── Score calculation ────────────────────────────────────────────────────
    deductions = {"CRITICAL": 20, "HIGH": 10, "MEDIUM": 5, "LOW": 2, "INFO": 0}
    score = 100
    summary = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    for f in findings:
        sev = f["severity"]
        summary[sev] = summary.get(sev, 0) + 1
        score -= deductions.get(sev, 0)
    score = max(0, min(100, score))

    if score >= 85:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 55:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    # Sort findings by severity
    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f["severity"], 99))

    return {
        "score":    score,
        "grade":    grade,
        "findings": findings,
        "summary":  summary,
    }


# =============================================================================
# 12. Package helpers
# =============================================================================

def get_packages_summary(packages):
    """Return per-manager summary (count only)."""
    rows = []
    for mgr, pkgs in packages.items():
        rows.append({
            "manager": mgr,
            "count":   str(len(pkgs)),
        })
    return rows


def get_all_packages_flat(packages):
    """Return every installed package as a flat list of (manager, name, version) tuples."""
    flat = []
    for mgr, pkgs in packages.items():
        for p in pkgs:
            flat.append((mgr, p.get("name", "-"), p.get("version", "-")))
    return flat


# =============================================================================
# PDF Generation
# =============================================================================

def build_pdf(report: dict, output_path: str, system_name: str):
    si      = report["system_info"]
    scan_dt = datetime.fromisoformat(si["scan_time"])
    ip      = si["target_ip"]
    hostname = si["hostname"]
    label   = system_name or hostname

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm,  bottomMargin=15*mm,
        title=f"App Inventory - {label}",
        author="ITC_System_Scanner",
    )

    styles = getSampleStyleSheet()
    W = A4[0] - 30*mm

    def S(name, **kw):
        s = styles[name].clone(name + "_c_" + str(id(kw)))
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    S_TITLE   = S("Title",   fontSize=20, textColor=C_WHITE,  alignment=TA_CENTER, spaceAfter=2)
    S_META    = S("Normal",  fontSize=9,  textColor=C_LIGHT,  alignment=TA_CENTER)
    S_SECTION = S("Heading2",fontSize=11, textColor=C_WHITE,  alignment=TA_LEFT,   spaceBefore=0, spaceAfter=0)
    S_CELL    = S("Normal",  fontSize=8,  textColor=C_DARK,   leading=11)
    S_BADGE_G = S("Normal",  fontSize=7,  textColor=C_GREEN,  alignment=TA_CENTER)
    S_BADGE_A = S("Normal",  fontSize=7,  textColor=C_AMBER,  alignment=TA_CENTER)
    S_BADGE_R = S("Normal",  fontSize=7,  textColor=C_RED,    alignment=TA_CENTER)
    S_BADGE_P = S("Normal",  fontSize=7,  textColor=C_PURPLE, alignment=TA_CENTER)
    S_SMALL   = S("Normal",  fontSize=7,  textColor=colors.HexColor("#64748B"))

    story = []

    # ── Banner ────────────────────────────────────────────────────────────────
    banner = Table([[Paragraph("Infomation Technology Centre, Central Railway <br/> System Scanning Analysis <br/> powered by Artificial Intelligence", S_TITLE)]], colWidths=[W])
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), C_DARK),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
    ]))
    story.append(banner)
    story.append(Spacer(1, 2*mm))

    # ── Meta strip ────────────────────────────────────────────────────────────
    os_label = si.get("distro") or f"{si['os']} {si['os_release']}"
    vm_badge = si.get("host_hypervisor", "")
    meta = Table([[
        Paragraph(f"<b>IP Address</b><br/>{ip}", S_META),
        Paragraph(f"<b>System / Server Name</b><br/>{label}", S_META),
        Paragraph(f"<b>Hostname</b><br/>{hostname}", S_META),
        Paragraph(f"<b>OS</b><br/>{safe_str(os_label, 40)}", S_META),
        Paragraph(f"<b>Architecture</b><br/>{si['architecture']}", S_META),
        Paragraph(f"<b>Scan Time</b><br/>{scan_dt.strftime('%Y-%m-%d  %H:%M:%S')}", S_META),
    ]], colWidths=[W/6]*6)
    meta.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), C_MID),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LINEBEFORE",    (1,0), (-1,-1), 0.5, C_ACCENT),
    ]))
    story.append(meta)

    if vm_badge:
        note = Table([[Paragraph(
            f"NOTE: This machine is running inside a hypervisor -- {vm_badge}",
            S("Normal", fontSize=8, textColor=C_AMBER, alignment=TA_CENTER)
        )]], colWidths=[W])
        note.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#1C1A10")),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(note)

    story.append(Spacer(1, 5*mm))

    # ── Helpers ───────────────────────────────────────────────────────────────
    def sec_hdr(title, sub=""):
        hdr = Table([[Paragraph(
            f"{title}  <font size='8' color='#94A3B8'>{sub}</font>", S_SECTION
        )]], colWidths=[W])
        hdr.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), C_MID),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ]))
        return hdr

    def matrix(headers, rows, cw):
        hrow = [Paragraph(f"<b>{h}</b>", S("Normal", fontSize=8, textColor=C_WHITE)) for h in headers]
        data = [hrow]
        for row in rows:
            data.append([
                cell if hasattr(cell, "text") else Paragraph(safe_str(cell), S_CELL)
                for cell in row
            ])
        t = Table(data, colWidths=cw, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0),  C_ACCENT),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING",   (0,0), (-1,-1), 5),
            ("RIGHTPADDING",  (0,0), (-1,-1), 5),
            ("GRID",          (0,0), (-1,-1), 0.4, C_GRID),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [C_WHITE, C_LIGHT]),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ]))
        return t

    # =========================================================================
    # Section 1 — System Health  (moved before Disk Storage)
    # =========================================================================
    sh = report.get("system_health", {})

    story.append(KeepTogether([
        sec_hdr("System Health",
                f"CPU: {safe_str(sh.get('cpu_usage_pct', '-'), 10)}  |  "
                f"RAM: {sh.get('ram_pct', '-')} used  |  "
                f"Load: {safe_str(sh.get('load_avg', '-'), 30)}"),
        Spacer(1, 1*mm)
    ]))

    # ── Overview row ──────────────────────────────────────────────────────────
    def _health_badge(pct_str, warn=75, crit=90):
        try:
            p = int(pct_str.rstrip("%"))
            if p >= crit:
                return Paragraph(pct_str, S_BADGE_R)
            elif p >= warn:
                return Paragraph(pct_str, S_BADGE_A)
            else:
                return Paragraph(pct_str, S_BADGE_G)
        except (ValueError, AttributeError):
            return Paragraph(str(pct_str) if pct_str else "-", S_CELL)

    health_summary_rows = [[
        safe_str(sh.get("cpu_model", "-"), 40),
        safe_str(sh.get("cpu_count", "-"), 8),
        _health_badge(sh.get("cpu_usage_pct", "-")),
        sh.get("ram_total", "-"),
        sh.get("ram_used",  "-"),
        _health_badge(sh.get("ram_pct", "-"), warn=75, crit=90),
        sh.get("swap_total", "-"),
        _health_badge(sh.get("swap_pct", "0%"), warn=20, crit=50),
    ]]
    story.append(matrix(
        ["CPU Model", "Cores", "CPU %", "RAM Total", "RAM Used", "RAM %",
         "Swap Total", "Swap %"],
        health_summary_rows,
        [W*0.25, W*0.05, W*0.07, W*0.10, W*0.10, W*0.07, W*0.10, W*0.07]
    ))

    # Uptime + Load
    story.append(Spacer(1, 2*mm))
    uptime_rows = [[
        safe_str(sh.get("uptime", "-"), 60),
        safe_str(sh.get("load_avg", "-"), 30),
    ]]
    story.append(matrix(["System Uptime", "Load Average (1m / 5m / 15m)"],
                         uptime_rows, [W*0.55, W*0.45]))

    # ── Top processes ─────────────────────────────────────────────────────────
    procs = sh.get("top_processes", [])
    if procs:
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(
            "<b>Top Processes by CPU</b>",
            S("Normal", fontSize=9, textColor=C_MID, spaceBefore=2, spaceAfter=2)
        ))
        proc_rows = []
        for p in procs:
            try:
                cpu_f = float(p["cpu"].rstrip("%"))
                sc_cpu = S_BADGE_R if cpu_f > 80 else (S_BADGE_A if cpu_f > 40 else S_BADGE_G)
            except (ValueError, AttributeError):
                sc_cpu = S_CELL
            proc_rows.append([
                p.get("pid", "-"),
                p.get("name", "-"),
                Paragraph(p.get("cpu", "-"), sc_cpu),
                p.get("mem", "-"),
            ])
        story.append(matrix(
            ["PID", "Process Name", "CPU %", "MEM %"],
            proc_rows,
            [W*0.10, W*0.55, W*0.18, W*0.17]
        ))

    # ── Health Alerts: RCA + Immediate Remedy ─────────────────────────────────
    _health_alerts = []

    # CPU
    try:
        cpu_pct_val = int(sh.get("cpu_usage_pct", "0").rstrip("%"))
        if cpu_pct_val >= 90:
            _health_alerts.append((
                "CRITICAL", "CPU Usage Critical",
                f"CPU is at {sh.get('cpu_usage_pct','?')}.",
                "Root Cause: One or more processes are consuming almost all available CPU. "
                "This is often caused by a runaway application, an infinite loop in code, "
                "heavy database queries, or a sudden traffic spike.",
                "Immediate Remedy: Check the 'Top Processes' table above to identify the "
                "offending process. If it is not essential, restart or stop it. "
                "Consider scaling up CPU resources or load-balancing the workload."
            ))
        elif cpu_pct_val >= 75:
            _health_alerts.append((
                "HIGH", "CPU Usage High",
                f"CPU is at {sh.get('cpu_usage_pct','?')}.",
                "Root Cause: System workload is elevated. Common causes include scheduled "
                "batch jobs, log processing, backup tasks, or increased user activity.",
                "Immediate Remedy: Review the top CPU-consuming processes. Reschedule "
                "non-urgent tasks to off-peak hours. Monitor over the next 15–30 minutes "
                "to see if usage normalises."
            ))
    except (ValueError, AttributeError):
        pass

    # RAM
    try:
        ram_pct_val = int(sh.get("ram_pct", "0").rstrip("%"))
        if ram_pct_val >= 95:
            _health_alerts.append((
                "CRITICAL", "RAM Critically High",
                f"RAM usage is at {sh.get('ram_pct','?')}.",
                "Root Cause: Available memory is almost exhausted. This is typically caused "
                "by a memory leak in a long-running application, insufficient RAM for the "
                "current workload, or multiple large applications running simultaneously.",
                "Immediate Remedy: Identify the process with the highest MEM % in the table "
                "above and restart it if safe to do so. Free up memory by stopping unused "
                "services. If this recurs, increase the server RAM or add swap space."
            ))
        elif ram_pct_val >= 85:
            _health_alerts.append((
                "HIGH", "RAM Usage Elevated",
                f"RAM usage is at {sh.get('ram_pct','?')}.",
                "Root Cause: High memory consumption, often from caching, large in-memory "
                "datasets, or multiple concurrent services.",
                "Immediate Remedy: Monitor for further increase. Consider restarting services "
                "that are known to have memory leaks. Plan for a RAM upgrade if this is "
                "consistently above 85%."
            ))
    except (ValueError, AttributeError):
        pass

    # Swap
    try:
        swap_pct_val = int(sh.get("swap_pct", "0").rstrip("%"))
        if swap_pct_val >= 50:
            _health_alerts.append((
                "HIGH", "Swap Usage Critical",
                f"Swap is {sh.get('swap_pct','?')} used.",
                "Root Cause: The system has run out of physical RAM and is heavily using "
                "the much slower disk-based swap space. This significantly slows down "
                "the entire system.",
                "Immediate Remedy: Urgently free up RAM by stopping non-critical processes. "
                "A system restart may be needed. Long-term: add more RAM or reduce the "
                "number of running services."
            ))
        elif swap_pct_val >= 20:
            _health_alerts.append((
                "MEDIUM", "Swap In Use",
                f"Swap is {sh.get('swap_pct','?')} used.",
                "Root Cause: Physical RAM is under pressure and the OS has started using "
                "swap space on disk as overflow memory, causing slowdowns.",
                "Immediate Remedy: Monitor RAM usage trends. Identify and restart memory-"
                "hungry processes. Consider reducing services running in parallel."
            ))
    except (ValueError, AttributeError):
        pass

    if _health_alerts:
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(
            "<b>Health Alerts — Root Cause Analysis &amp; Immediate Actions</b>",
            S("Normal", fontSize=9, textColor=C_MID, spaceBefore=2, spaceAfter=2)
        ))
        alert_hrow = [
            Paragraph("<b>Severity</b>",         S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Issue</b>",             S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Observation</b>",       S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Root Cause (RCA)</b>",  S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Immediate Remedy</b>",  S("Normal", fontSize=8, textColor=C_WHITE)),
        ]
        alert_rows = [alert_hrow]
        for sev, title, obs, rca, remedy in _health_alerts:
            hex_c = _SEV_COLOR.get(sev, "#94A3B8")
            s_sev = S("Normal", fontSize=8, textColor=colors.HexColor(hex_c),
                      alignment=TA_CENTER, leading=11)
            alert_rows.append([
                Paragraph(f"<b>{sev}</b>", s_sev),
                Paragraph(safe_str(title,  40), S_CELL),
                Paragraph(safe_str(obs,    60), S_SMALL),
                Paragraph(safe_str(rca,   200), S_SMALL),
                Paragraph(safe_str(remedy,200), S_SMALL),
            ])
        alert_tbl = Table(
            alert_rows,
            colWidths=[W*0.10, W*0.15, W*0.13, W*0.31, W*0.31],
            repeatRows=1
        )
        alert_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1,  0), C_ACCENT),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_GRID),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(alert_tbl)

    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 2 — Cyber Security Posture  (moved before Disk Storage)
    # =========================================================================
    cs = report.get("cyber_security", {})
    cs_score    = cs.get("score", 0)
    cs_grade    = cs.get("grade", "?")
    cs_summary  = cs.get("summary", {})
    cs_findings = cs.get("findings", [])

    grade_color = {
        "A": C_GREEN, "B": colors.HexColor("#22D3EE"),
        "C": C_AMBER, "D": colors.HexColor("#F97316"),
        "F": C_RED,
    }.get(cs_grade, C_AMBER)

    story.append(KeepTogether([
        sec_hdr("Cyber Security Posture",
                f"Score: {cs_score}/100   Grade: {cs_grade}   |   "
                f"Critical: {cs_summary.get('CRITICAL',0)}   "
                f"High: {cs_summary.get('HIGH',0)}   "
                f"Medium: {cs_summary.get('MEDIUM',0)}   "
                f"Low: {cs_summary.get('LOW',0)}"),
        Spacer(1, 1*mm)
    ]))

    # ── Score card ────────────────────────────────────────────────────────────
    def _sev_para(sev):
        color = _SEV_COLOR.get(sev, "#94A3B8")
        return Paragraph(
            f"<b>{sev}</b>",
            S("Normal", fontSize=8, textColor=colors.HexColor(color), alignment=TA_CENTER)
        )

    # Summary scorecard row
    score_rows = [[
        Paragraph(f"<b>{cs_score}</b>",
                  S("Normal", fontSize=18, textColor=grade_color, alignment=TA_CENTER)),
        Paragraph(f"<b>{cs_grade}</b>",
                  S("Normal", fontSize=18, textColor=grade_color, alignment=TA_CENTER)),
        Paragraph(f"<b>{cs_summary.get('CRITICAL',0)}</b>",
                  S("Normal", fontSize=14, textColor=colors.HexColor(_SEV_COLOR['CRITICAL']),
                    alignment=TA_CENTER)),
        Paragraph(f"<b>{cs_summary.get('HIGH',0)}</b>",
                  S("Normal", fontSize=14, textColor=colors.HexColor(_SEV_COLOR['HIGH']),
                    alignment=TA_CENTER)),
        Paragraph(f"<b>{cs_summary.get('MEDIUM',0)}</b>",
                  S("Normal", fontSize=14, textColor=colors.HexColor(_SEV_COLOR['MEDIUM']),
                    alignment=TA_CENTER)),
        Paragraph(f"<b>{cs_summary.get('LOW',0)}</b>",
                  S("Normal", fontSize=14, textColor=colors.HexColor(_SEV_COLOR['LOW']),
                    alignment=TA_CENTER)),
        Paragraph(f"<b>{cs_summary.get('INFO',0)}</b>",
                  S("Normal", fontSize=14, textColor=colors.HexColor(_SEV_COLOR['INFO']),
                    alignment=TA_CENTER)),
    ]]
    hdr_labels = ["Score (/100)", "Grade", "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    score_hrow = [
        Paragraph(f"<b>{h}</b>",
                  S("Normal", fontSize=8, textColor=C_WHITE, alignment=TA_CENTER))
        for h in hdr_labels
    ]
    score_tbl = Table(
        [score_hrow] + score_rows,
        colWidths=[W*0.14, W*0.10, W*0.14, W*0.12, W*0.14, W*0.12, W*0.12],
        rowHeights=[None, 24]
    )
    score_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1,  0), C_MID),
        ("BACKGROUND",    (0, 1), (-1, -1), C_DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("GRID",          (0, 0), (-1, -1), 0.4, C_GRID),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(score_tbl)
    story.append(Spacer(1, 3*mm))

    # ── Severity legend (plain English for non-technical readers) ────────────
    _sev_legend = [
        ("CRITICAL", "#EF4444",
         "Immediate danger — like leaving the front door wide open. "
         "Stop everything and fix this today."),
        ("HIGH",     "#F97316",
         "Serious risk — like a broken lock that anyone could exploit. "
         "Fix within 24–48 hours."),
        ("MEDIUM",   "#F59E0B",
         "Moderate risk — a weak point that makes attacks easier. "
         "Plan to fix within the week."),
        ("LOW",      "#22C55E",
         "Minor issue — a small gap worth tidying up. "
         "Fix during the next maintenance window."),
        ("INFO",     "#94A3B8",
         "Informational — no immediate risk, just something to be aware of."),
    ]
    leg_hrow = [
        Paragraph("<b>Level</b>",           S("Normal", fontSize=8, textColor=C_WHITE)),
        Paragraph("<b>What it means for you (plain English)</b>",
                  S("Normal", fontSize=8, textColor=C_WHITE)),
    ]
    leg_rows = [leg_hrow]
    for lsev, lcolor, ldesc in _sev_legend:
        leg_rows.append([
            Paragraph(f"<b>{lsev}</b>",
                      S("Normal", fontSize=8, textColor=colors.HexColor(lcolor),
                        alignment=TA_CENTER)),
            Paragraph(ldesc, S_SMALL),
        ])
    leg_tbl = Table(leg_rows, colWidths=[W*0.15, W*0.85], repeatRows=1)
    leg_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1,  0), C_MID),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_GRID),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(leg_tbl)
    story.append(Spacer(1, 3*mm))

    # ── Findings table with plain-English explanation + immediate remedy ──────
    # Plain-English descriptions keyed by finding title keywords
    _PLAIN_ENGLISH = {
        "telnet":          ("Telnet is an old remote-access tool that sends your username and "
                            "password as readable text over the network — like sending a postcard "
                            "instead of a sealed letter.",
                            "Disable the Telnet service immediately and switch to SSH, which "
                            "encrypts all communication."),
        "ftp":             ("FTP transfers files without any encryption, meaning anyone on the "
                            "same network can see the files and login credentials.",
                            "Stop the FTP service. Use SFTP or SCP instead, which provide "
                            "encrypted file transfers."),
        "redis":           ("Redis is a database that by default has no password. Anyone who "
                            "can reach it over the network can read or delete all its data.",
                            "Set a strong password in redis.conf (requirepass directive) and "
                            "make sure the port (6379) is blocked from the internet by a firewall."),
        "mongodb":         ("MongoDB in its default setup has no login protection, meaning "
                            "anyone who can connect can see all stored data.",
                            "Enable MongoDB authentication immediately and bind it to listen "
                            "only on internal IP addresses, not 0.0.0.0."),
        "elasticsearch":   ("Elasticsearch stores and searches data but ships without a password "
                            "by default. All data may be publicly readable.",
                            "Enable Elasticsearch security features (X-Pack). Block port 9200 "
                            "from the internet using a firewall."),
        "memcached":       ("Memcached is a caching service with no authentication. If reachable "
                            "from outside, attackers can read cached data or even use it to "
                            "amplify DDoS attacks.",
                            "Block Memcached port (11211) at the firewall. Bind it to 127.0.0.1 "
                            "so only the local machine can connect."),
        "vnc":             ("VNC lets you see and control a computer's screen remotely. Many "
                            "VNC setups run without encryption, making it easy to spy on "
                            "or hijack sessions.",
                            "Enable VNC encryption or tunnel it through SSH. Use a strong "
                            "password and restrict access to trusted IP addresses only."),
        "jupyter":         ("Jupyter Notebook is a coding tool that, if left open, lets anyone "
                            "run commands on your server — like handing a stranger the keys "
                            "to your computer.",
                            "Set a strong Jupyter password or token. Never expose port 8888 to "
                            "the internet. Use a reverse proxy or VPN for remote access."),
        "jenkins":         ("Jenkins is a build automation tool with an admin console. If "
                            "accessible from the internet, attackers can run any code on your "
                            "server.",
                            "Restrict Jenkins to internal network access only. Enable "
                            "authentication and keep Jenkins updated."),
        "ssh permitrootlogin": ("Allowing direct root login over SSH is like giving the master "
                                "key to your entire system to anyone who guesses the password.",
                                "In /etc/ssh/sshd_config, set 'PermitRootLogin no'. Log in as a "
                                "normal user and use 'sudo' when admin access is needed."),
        "ssh passwordauthentication": ("Password-based SSH is vulnerable to brute-force attacks "
                                       "where attackers repeatedly try different passwords until "
                                       "one works.",
                                       "Disable password login: set 'PasswordAuthentication no' "
                                       "in /etc/ssh/sshd_config. Use SSH key pairs instead."),
        "firewall":        ("No firewall means all your server's ports are exposed to the "
                            "internet — any service running is potentially reachable by anyone.",
                            "Enable UFW immediately: run 'sudo ufw enable' and then only open "
                            "the ports you actually need (e.g. 'sudo ufw allow 22/tcp' for SSH)."),
        "nopasswd sudo":   ("Some users can run admin commands without entering a password. "
                            "If their account is compromised, the attacker immediately has "
                            "full control of the server.",
                            "Edit /etc/sudoers (use 'sudo visudo') and remove the NOPASSWD "
                            "entries. Require passwords for all sudo actions."),
        "world-writable":  ("Certain system files can be modified by any user on the machine, "
                            "including potentially malicious ones. Attackers can plant harmful "
                            "code in these files.",
                            "Run 'chmod o-w <filename>' on each listed file to remove the "
                            "world-write permission."),
        "ram":             ("The server is running low on memory. This can cause services to "
                            "crash unexpectedly, slow everything down, or corrupt data.",
                            "Identify the process using the most memory in the Top Processes "
                            "table and restart it if safe. Consider adding more RAM."),
        "swap":            ("The server has used up its real memory and is now using the hard "
                            "drive as emergency memory. This makes everything much slower.",
                            "Free up memory by restarting high-memory processes. Reboot if "
                            "necessary. Long-term: add more RAM or reduce the workload."),
        "disk":            ("A storage drive is nearly full. When a drive fills up completely, "
                            "the system cannot write logs, save files, or run properly.",
                            "Delete old log files, backups, or temporary files. Run "
                            "'du -sh /*' to find what is using the most space."),
        "snmp":            ("SNMP (network monitoring protocol) in older versions sends its "
                            "access credentials as plain readable text.",
                            "Upgrade to SNMPv3 which supports encryption, or disable SNMP "
                            "entirely if not needed. Restrict SNMP access by IP address."),
        "couchdb":         ("CouchDB may be running in 'admin party' mode where there is no "
                            "admin password — anyone can access and modify all data.",
                            "Set an admin password immediately via the CouchDB admin panel or "
                            "configuration file. Block port 5984 from the internet."),
        "rabbitmq":        ("RabbitMQ message broker comes with a default 'guest' account with "
                            "a well-known password that attackers commonly try.",
                            "Delete or change the default 'guest' account password. Create "
                            "specific user accounts with only the permissions they need."),
        "selinux":         ("SELinux is a security layer that limits what each program can do, "
                            "but it is currently in warning-only mode and not actually blocking "
                            "anything.",
                            "Switch SELinux to Enforcing mode: run 'sudo setenforce 1' and set "
                            "SELINUX=enforcing in /etc/selinux/config."),
        "patch":           ("The system has no automatic security updates configured. Known "
                            "security vulnerabilities in installed software will not be patched "
                            "automatically.",
                            "Install unattended-upgrades: 'sudo apt install unattended-upgrades' "
                            "and enable it with 'sudo dpkg-reconfigure unattended-upgrades'."),
    }

    def _get_plain_explanation(title_str, detail_str):
        """Find the best matching plain-English explanation for a finding."""
        combined = (title_str + " " + detail_str).lower()
        for keyword, (plain, remedy) in _PLAIN_ENGLISH.items():
            if keyword in combined:
                return plain, remedy
        # Fallback: use the technical detail as-is
        return "", ""

    if cs_findings:
        find_hrow = [
            Paragraph("<b>Severity</b>",                  S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Category</b>",                  S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Finding</b>",                   S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>What This Means (Plain English)</b>",
                      S("Normal", fontSize=8, textColor=C_WHITE)),
            Paragraph("<b>Immediate Remedy</b>",          S("Normal", fontSize=8, textColor=C_WHITE)),
        ]
        find_rows = [find_hrow]
        for f in cs_findings:
            sev    = f.get("severity", "INFO")
            title  = f.get("title",    "-")
            detail = f.get("detail",   "-")
            hex_c  = _SEV_COLOR.get(sev, "#94A3B8")
            s_sev  = S("Normal", fontSize=8, textColor=colors.HexColor(hex_c),
                       alignment=TA_CENTER, leading=11)
            plain_exp, plain_remedy = _get_plain_explanation(title, detail)
            # Show plain English if found, otherwise show technical detail
            display_explanation = plain_exp if plain_exp else detail
            display_remedy      = plain_remedy if plain_remedy else "Refer to the technical detail above."
            find_rows.append([
                Paragraph(f"<b>{sev}</b>", s_sev),
                Paragraph(safe_str(f.get("category", "-"), 25), S_SMALL),
                Paragraph(safe_str(title,  50), S_CELL),
                Paragraph(safe_str(display_explanation, 220), S_SMALL),
                Paragraph(safe_str(display_remedy,       220), S_SMALL),
            ])

        find_tbl = Table(
            find_rows,
            colWidths=[W*0.09, W*0.13, W*0.20, W*0.29, W*0.29],
            repeatRows=1
        )
        find_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1,  0), C_ACCENT),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_GRID),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(find_tbl)
    else:
        story.append(Paragraph(
            "No security findings detected. Excellent posture!", S_SMALL
        ))

    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 3 — Disk Storage
    # =========================================================================
    disk = report.get("disk_info", {})
    partitions = disk.get("partitions", [])

    story.append(KeepTogether([
        sec_hdr("Disk Storage",
                f"Total: {disk.get('total_size','-')}  |  "
                f"Used: {disk.get('total_used','-')} ({disk.get('total_use_pct','-')})  |  "
                f"Free: {disk.get('total_free','-')}"),
        Spacer(1, 1*mm)
    ]))

    # ── Aggregate summary row ────────────────────────────────────────────
    sum_cw = [W*0.25, W*0.25, W*0.25, W*0.25]
    sum_rows = [[
        disk.get("total_size",  "-"),
        disk.get("total_used",  "-"),
        disk.get("total_free",  "-"),
        disk.get("total_use_pct", "-"),
    ]]
    story.append(matrix(["Total Disk Size", "Total Used", "Total Free", "Usage %"], sum_rows, sum_cw))
    story.append(Spacer(1, 2*mm))

    # ── Per-partition table ───────────────────────────────────────────────
    if partitions:
        cw_p = [W*0.30, W*0.22, W*0.16, W*0.16, W*0.16]
        rows = []
        for p in partitions:
            use_pct_val = p.get("use_pct", "-").rstrip("%")
            try:
                pct_int = int(use_pct_val)
            except ValueError:
                pct_int = 0
            if pct_int >= 90:
                sc = S_BADGE_R
            elif pct_int >= 75:
                sc = S_BADGE_A
            else:
                sc = S_BADGE_G
            rows.append([
                p.get("filesystem", "-"),
                p.get("mount",      "-"),
                p.get("size",       "-"),
                p.get("used",       "-"),
                p.get("free",       "-"),
            ])
        story.append(matrix(
            ["Filesystem / Drive", "Mount Point", "Total Size", "Used", "Free"],
            rows, cw_p
        ))
    else:
        story.append(Paragraph("No disk partition information available.", S_SMALL))

    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 4 — Databases (was Section 2)
    # =========================================================================
    dbs = report["databases"]
    story.append(KeepTogether([sec_hdr("Databases", f"{len(dbs)} detected"), Spacer(1,1*mm)]))
    if dbs:
        cw = [W*0.22, W*0.14, W*0.08, W*0.14, W*0.42]
        rows = []
        for db in dbs:
            sc = S_BADGE_G if db["port_listening"] else S_BADGE_A
            st = "ACTIVE" if db["port_listening"] else "BINARY ONLY"
            rows.append([db["name"], db["binary"], db["port"], Paragraph(st, sc), db["version"]])
        story.append(matrix(["Database", "Binary", "Port", "Status", "Version"], rows, cw))
    else:
        story.append(Paragraph("No databases detected.", S_SMALL))
    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 5 — Web / App Servers (was Section 3)
    # =========================================================================
    webs = report["web_applications"]
    story.append(KeepTogether([sec_hdr("Web Servers & App Frameworks", f"{len(webs)} detected"), Spacer(1,1*mm)]))
    if webs:
        cw = [W*0.22, W*0.14, W*0.08, W*0.14, W*0.42]
        rows = []
        for w in webs:
            sc = S_BADGE_G if w["port_listening"] else S_BADGE_A
            st = "ACTIVE" if w["port_listening"] else "BINARY ONLY"
            rows.append([w["name"], w["binary"], w["port"], Paragraph(st, sc), w["version"]])
        story.append(matrix(["Server / Framework", "Binary", "Port", "Status", "Version"], rows, cw))
    else:
        story.append(Paragraph("No web servers detected.", S_SMALL))
    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 6 — Virtual Machines (was Section 4)
    # =========================================================================
    vm_data = report.get("virtual_machines", {})
    all_vms = vm_data.get("vms", [])

    story.append(KeepTogether([
        sec_hdr("Virtual Machines", f"{len(all_vms)} VM(s) detected across all hypervisors"),
        Spacer(1, 1*mm)
    ]))

    if all_vms:
        cw = [W*0.18, W*0.18, W*0.10, W*0.12, W*0.18, W*0.12, W*0.07, W*0.05]
        rows = []
        for vm in all_vms:
            state = safe_str(vm.get("state", "-"))
            if "run" in state.lower():
                sc = S_BADGE_G
            elif "off" in state.lower() or "stop" in state.lower() or "shut" in state.lower():
                sc = S_BADGE_R
            elif "suspend" in state.lower() or "pause" in state.lower():
                sc = S_BADGE_A
            else:
                sc = S_BADGE_P
            rows.append([
                vm.get("platform", "-"),
                vm.get("vm_name",  "-"),
                Paragraph(state, sc),
                vm.get("guest_os", "-"),
                vm.get("host_ver", "-"),
                vm.get("uuid",     "-"),
                vm.get("memory",   "-"),
                vm.get("cpus",     "-"),
            ])
        story.append(matrix(
            ["Hypervisor", "VM Name", "State", "Guest OS", "Host Version", "ID / UUID", "RAM", "CPUs"],
            rows, cw
        ))
    else:
        story.append(Paragraph(
            "No virtual machines detected. No hypervisor tools (VBoxManage, virsh, qm, xl, "
            "vagrant, multipass, Get-VM) were found or returned results.",
            S_SMALL
        ))

    # Platform summary sub-table
    if all_vms:
        platforms = {}
        for vm in all_vms:
            p = vm.get("platform", "Unknown")
            platforms[p] = platforms.get(p, 0) + 1
        story.append(Spacer(1, 2*mm))
        sum_rows = [[p, str(c)] for p, c in platforms.items()]
        story.append(matrix(["Hypervisor Platform", "VM Count"], sum_rows, [W*0.6, W*0.4]))

    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 7 — Docker / Kubernetes Containers (was Section 5)
    # =========================================================================
    ctrs = report["containers"]
    containers = ctrs.get("containers", [])
    k8s_pods   = ctrs.get("k8s_pods", [])
    story.append(KeepTogether([
        sec_hdr("Containers", f"Docker: {len(containers)} running   |   K8s pods: {len(k8s_pods)}"),
        Spacer(1,1*mm)
    ]))
    if containers:
        cw = [W*0.20, W*0.30, W*0.25, W*0.25]
        rows = [[c["name"], c["image"], c["status"], c["ports"]] for c in containers]
        story.append(matrix(["Container Name", "Image", "Status", "Ports"], rows, cw))
    else:
        story.append(Paragraph("No running Docker containers.", S_SMALL))

    if k8s_pods:
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>Kubernetes Pods</b>", S_CELL))
        story.append(Spacer(1, 1*mm))
        cw = [W*0.25, W*0.40, W*0.15, W*0.20]
        rows = [[p["namespace"], p["name"], p["ready"], p["status"]] for p in k8s_pods]
        story.append(matrix(["Namespace", "Pod Name", "Ready", "Status"], rows, cw))
    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 8 — Language Runtimes (was Section 6)
    # =========================================================================
    runtimes = report["runtimes"]
    story.append(KeepTogether([sec_hdr("Language Runtimes", f"{len(runtimes)} detected"), Spacer(1,1*mm)]))
    if runtimes:
        per_row = 3
        rt_items = list(runtimes.items())
        cw = [W/per_row] * per_row
        rows = []
        for i in range(0, len(rt_items), per_row):
            chunk = rt_items[i:i+per_row]
            row = [Paragraph(f"<b>{n}</b><br/>{safe_str(v, 60)}", S_CELL) for n, v in chunk]
            while len(row) < per_row:
                row.append(Paragraph("", S_CELL))
            rows.append(row)
        story.append(matrix(["Runtime", "Runtime", "Runtime"], rows, cw))
    else:
        story.append(Paragraph("No language runtimes detected.", S_SMALL))
    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 9 — Installed Packages (was Section 7)
    # =========================================================================
    pkg_summary = get_packages_summary(report["installed_packages"])
    all_pkgs    = get_all_packages_flat(report["installed_packages"])
    total_pkgs  = len(all_pkgs)

    story.append(sec_hdr(
        "Installed Packages",
        f"{total_pkgs} total packages across {len(pkg_summary)} package manager(s)"
    ))
    story.append(Spacer(1, 1*mm))

    if not all_pkgs:
        story.append(Paragraph("No installed packages detected.", S_SMALL))
    else:
        # ── Summary bar (one row per manager) ────────────────────────────────
        sum_rows = [[p["manager"], p["count"]] for p in pkg_summary]
        story.append(matrix(["Package Manager", "Total Packages"], sum_rows, [W*0.5, W*0.5]))
        story.append(Spacer(1, 3*mm))

        # ── Full package listing — one table per manager ──────────────────────
        for mgr, pkgs in report["installed_packages"].items():
            if not pkgs:
                continue
            story.append(Paragraph(
                f"<b>{mgr}</b>  ({len(pkgs)} packages)",
                S("Normal", fontSize=9, textColor=C_MID, spaceBefore=4, spaceAfter=2)
            ))

            if mgr == "Windows Installed Programs":
                # Windows: header row + 2 columns (Name | Version), no serial number
                S_HDR  = S("Normal", fontSize=8, textColor=C_WHITE, leading=11)
                cw_win = [W * 0.72, W * 0.28]
                hdr_row = [
                    Paragraph("<b>Program Name</b>", S_HDR),
                    Paragraph("<b>Version</b>",      S_HDR),
                ]
                data_rows = [hdr_row]
                for p in pkgs:
                    data_rows.append([
                        Paragraph(safe_str(p.get("name",    "-"), 80), S_CELL),
                        Paragraph(safe_str(p.get("version", "-"), 25), S_SMALL),
                    ])
                t = Table(data_rows, colWidths=cw_win, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1,  0), C_ACCENT),
                    ("TOPPADDING",    (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
                    ("GRID",          (0, 0), (-1, -1), 0.3, C_GRID),
                    ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ]))
            else:
                # All other managers: 3-column layout  #  |  Name  |  Version
                per_row    = 3
                cw_pkg     = [W * 0.03, W * 0.22, W * 0.08,
                              W * 0.03, W * 0.22, W * 0.08,
                              W * 0.03, W * 0.22, W * 0.09]
                rows = []
                chunk_size = per_row
                for i in range(0, len(pkgs), chunk_size):
                    chunk = pkgs[i:i+chunk_size]
                    row = []
                    for idx, p in enumerate(chunk, start=i+1):
                        row += [
                            Paragraph(str(idx), S("Normal", fontSize=7, textColor=colors.HexColor("#94A3B8"), alignment=TA_CENTER)),
                            Paragraph(safe_str(p.get("name",    "-"), 35), S_CELL),
                            Paragraph(safe_str(p.get("version", "-"), 18), S_SMALL),
                        ]
                    while len(row) < chunk_size * 3:
                        row += [Paragraph("", S_CELL), Paragraph("", S_CELL), Paragraph("", S_CELL)]
                    rows.append(row)
                t = Table(rows, colWidths=cw_pkg, repeatRows=0)
                t.setStyle(TableStyle([
                    ("TOPPADDING",    (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                    ("GRID",          (0, 0), (-1, -1), 0.3, C_GRID),
                    ("ROWBACKGROUNDS",(0, 0), (-1, -1), [C_WHITE, C_LIGHT]),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBEFORE",    (3, 0), (3, -1), 0.8, C_ACCENT),
                    ("LINEBEFORE",    (6, 0), (6, -1), 0.8, C_ACCENT),
                ]))

            story.append(t)
            story.append(Spacer(1, 3*mm))

    story.append(Spacer(1, 5*mm))

    # =========================================================================
    # Section 10 — Running Services (was Section 8)
    # =========================================================================
    all_services = sorted(report["running_services"], key=lambda x: x["service"].lower())
    svc_count    = len(all_services)

    story.append(KeepTogether([
        sec_hdr("Running Services", f"{svc_count} total  |  sorted alphabetically"),
        Spacer(1, 1*mm)
    ]))

    if all_services:
        cw_svc = [W * 0.35, W * 0.65]
        hdr_svc = [
            Paragraph("<b>Service Name</b>",   S("Normal", fontSize=8, textColor=C_WHITE, leading=11)),
            Paragraph("<b>Program / Description</b>", S("Normal", fontSize=8, textColor=C_WHITE, leading=11)),
        ]
        svc_rows = [hdr_svc]
        for s in all_services:
            svc_rows.append([
                Paragraph(safe_str(s.get("service",     "-"), 55), S_CELL),
                Paragraph(safe_str(s.get("description", "-"), 90), S_SMALL),
            ])
        t = Table(svc_rows, colWidths=cw_svc, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1,  0), C_ACCENT),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("GRID",          (0, 0), (-1, -1), 0.3, C_GRID),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("No running services detected.", S_SMALL))

    story.append(Spacer(1, 5*mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width=W, thickness=0.5, color=C_GRID))
    story.append(Paragraph(
        f"Generated by Information Technology System Scanner [PSN] <br/> "
        f"&bull;  System Name :-  {label}  "
        f"&bull;  IP Address {ip} <br/> "
        f"&bull;  Scan Date &amp; Time {scan_dt.strftime('%Y-%m-%d %H:%M:%S')}",
        S("Normal", fontSize=8, textColor=colors.HexColor("#94A3B8"), alignment=TA_CENTER)
    ))

    doc.build(story)
    print(f"\n  PDF saved -> {output_path}")


# =============================================================================
# CLI & Main
# =============================================================================

def sanitize(s):
    """Make a string safe for use in a filename."""
    return re.sub(r'[^A-Za-z0-9_\-]', '_', s)


def get_local_ip():
    """
    Return the primary non-loopback IPv4 address of this machine.
    Uses a UDP connect trick — no packet is actually sent.
    Falls back to hostname resolution, then '127.0.0.1'.
    """
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


def main():
    target_ip   = get_local_ip()
    system_name = socket.gethostname()
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")

    safe_ip   = target_ip.replace(".", "_")
    safe_name = sanitize(system_name)
    out_path  = f"{safe_ip}_{safe_name}_{ts}.pdf"

    print("=" * 64)
    print(f"  ITC System Scanner  ->  {target_ip}  ({system_name})")
    print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Output  : {out_path}")
    print("=" * 64)

    report = {}

    print("[1/11] System info ...")
    report["system_info"] = get_system_info(target_ip)

    print("[2/11] Disk storage ...")
    report["disk_info"] = get_disk_info()
    disk = report["disk_info"]
    print(f"      {len(disk['partitions'])} partition(s)  |  "
          f"Total: {disk['total_size']}  Used: {disk['total_used']}  Free: {disk['total_free']}")

    print("[3/11] Installed packages ...")
    report["installed_packages"] = get_installed_packages()
    total = sum(len(v) for v in report["installed_packages"].values())
    print(f"      {total} packages across {len(report['installed_packages'])} managers")

    print("[4/11] Databases ...")
    report["databases"] = get_databases()
    print(f"      {len(report['databases'])} found")

    print("[5/11] Web servers / app frameworks ...")
    report["web_applications"] = get_web_applications()
    print(f"      {len(report['web_applications'])} found")

    print("[6/11] Running services ...")
    report["running_services"] = get_running_services()
    print(f"      {len(report['running_services'])} running")

    print("[7/11] Containers (Docker / Kubernetes) ...")
    report["containers"] = get_containers()
    print(f"      Docker: {len(report['containers'].get('containers', []))} containers  |  "
          f"K8s pods: {len(report['containers'].get('k8s_pods', []))}")

    print("[8/11] Virtual machines ...")
    report["virtual_machines"] = get_virtual_machines()
    vm_count = report["virtual_machines"]["total"]
    host_vm  = report["virtual_machines"]["host_is_vm"]
    report["system_info"]["host_hypervisor"] = host_vm
    print(f"      {vm_count} VM(s) found"
          + (f"  |  This machine is a VM guest: {host_vm}" if host_vm else ""))

    print("[9/11] Language runtimes ...")
    report["runtimes"] = get_runtimes()
    print(f"       {', '.join(report['runtimes'].keys()) or 'none'}")

    print("[10/11] System health (CPU / RAM / swap / processes) ...")
    report["system_health"] = get_system_health()
    h = report["system_health"]
    print(f"        RAM {h.get('ram_pct','-')} used  |  Load: {h.get('load_avg','-')}  |  "
          f"Uptime: {h.get('uptime','')[:40]}")

    print("[11/11] Cyber security posture ...")
    report["cyber_security"] = get_cyber_security_posture(
        report["system_health"],
        report["running_services"],
        report["databases"],
        report["web_applications"],
    )
    cs = report["cyber_security"]
    print(f"        Score: {cs['score']}/100  Grade: {cs['grade']}  |  "
          f"Critical: {cs['summary']['CRITICAL']}  High: {cs['summary']['HIGH']}  "
          f"Medium: {cs['summary']['MEDIUM']}  Low: {cs['summary']['LOW']}")

    print("\n  Building PDF report ...")
    build_pdf(report, out_path, system_name)

    json_path = out_path.replace(".pdf", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  JSON saved -> {json_path}")

    print("\n" + "=" * 64)
    print(f"  Done.  Report -> {out_path}")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    main()