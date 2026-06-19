#!/usr/bin/env python3
"""
app_inventory_gui.py
====================
Tkinter GUI wrapper for ITC System Scanner.

Fixes applied vs original:
  1. Pack order fixed: status_bar and action_bar must be packed with side='bottom'
     BEFORE log_panel (which uses expand=True/fill=both), otherwise they are
     invisible — the log panel grabs all remaining space.
  2. Stray border frame inside _build_action_bar moved to correct position.
  3. Added ttk.Notebook with All / Windows / Linux / macOS tabs for log output.
  4. Progress label turns green on success; buttons enable correctly.
"""

import os
import sys
import types
import queue
import socket
import threading
import subprocess
import platform
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path

# ── PIL stub so reportlab does not crash when Pillow is absent ────────────────
_pil_stub = types.ModuleType("PIL")
_pil_stub.Image = None
sys.modules.setdefault("PIL", _pil_stub)
sys.modules.setdefault("PIL.Image", types.ModuleType("PIL.Image"))

# ── Colour palette ────────────────────────────────────────────────────────────
CLR_BG          = "#0F1221"
CLR_SURFACE     = "#1A1F36"
CLR_SURFACE2    = "#232946"
CLR_ACCENT      = "#4F6EF7"
CLR_ACCENT2     = "#7B93FF"
CLR_SUCCESS     = "#22C55E"
CLR_WARNING     = "#F59E0B"
CLR_DANGER      = "#EF4444"
CLR_TEXT        = "#E2E8F0"
CLR_TEXT_DIM    = "#94A3B8"
CLR_BORDER      = "#2D3561"
CLR_LOG_BG      = "#0B0F1A"

FONT_HEAD   = ("Segoe UI", 14, "bold")
FONT_SUB    = ("Segoe UI", 10, "bold")
FONT_BODY   = ("Segoe UI",  9)
FONT_MONO   = ("Consolas",  9)
FONT_SMALL  = ("Segoe UI",  8)

APP_TITLE   = "ITC System Scanner"
APP_VERSION = "2.0"


# =============================================================================
# Queue-based stdout/stderr redirect
# =============================================================================

class _QueueWriter:
    def __init__(self, q: queue.Queue, tag: str = "normal", mirror=None):
        self._q      = q
        self._tag    = tag
        self._mirror = mirror

    def write(self, text: str):
        if text:
            self._q.put((self._tag, text))
            if self._mirror:
                try:
                    self._mirror.write(text)
                except Exception:
                    pass

    def flush(self):
        if self._mirror:
            try:
                self._mirror.flush()
            except Exception:
                pass


# =============================================================================
# Main Application
# =============================================================================

class ITCScannerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.configure(bg=CLR_BG)
        self.resizable(True, True)
        self.minsize(860, 640)

        w, h = 1000, 760
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._log_queue     = queue.Queue()
        self._scan_thread   = None
        self._is_scanning   = False
        self._output_pdf    = None
        self._output_json   = None
        self._pending_pdf   = None
        self._pending_json  = None
        self._log_file_path = None

        self._build_ui()
        self._poll_log_queue()

    # ── UI construction ───────────────────────────────────────────────────────

    def _configure_styles(self):
        """
        Configure all ttk styles BEFORE any widget is created.
        This ensures notebook tab styles are applied correctly on all platforms
        (especially Linux/older Tk where late style configuration is ignored).
        """
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "ITC.Horizontal.TProgressbar",
            troughcolor=CLR_SURFACE, background=CLR_ACCENT,
            bordercolor=CLR_BORDER, lightcolor=CLR_ACCENT,
            darkcolor=CLR_ACCENT, thickness=6,
        )
        style.configure("ITC.TNotebook", background=CLR_BG, borderwidth=0)
        style.configure("ITC.TNotebook.Tab", background=CLR_SURFACE,
                        foreground=CLR_TEXT_DIM, padding=[12, 4],
                        font=FONT_SMALL, borderwidth=0)
        style.map("ITC.TNotebook.Tab",
                  background=[("selected", CLR_SURFACE2)],
                  foreground=[("selected", CLR_TEXT)])

    def _build_ui(self):
        """
        CORRECT pack order for tkinter geometry manager:
          - Widgets with side='bottom' must be packed BEFORE fill=both+expand=True
            widgets, otherwise they are hidden behind the expanding widget.
        Order: header (top) → info_bar (top) → status_bar (bottom) →
               action_bar (bottom) → progress_row (bottom) → log_panel (fills rest)
        """
        self._configure_styles()    # Bug 5 fix: configure all ttk styles first
        self._build_header()        # packs to top
        self._build_info_bar()      # packs to top
        # ── Pack bottom-anchored widgets BEFORE the expanding log panel ──
        self._build_status_bar()    # side=bottom
        self._build_action_bar()    # side=bottom
        self._build_progress_row()  # side=bottom
        # ── Log panel last — it expands to fill all remaining space ──────
        self._build_log_panel()     # fill=both, expand=True

    def _build_header(self):
        hdr = tk.Frame(self, bg=CLR_SURFACE)
        hdr.pack(fill="x")

        inner = tk.Frame(hdr, bg=CLR_SURFACE)
        inner.pack(fill="x", padx=18, pady=12)

        left = tk.Frame(inner, bg=CLR_SURFACE)
        left.pack(side="left")

        tk.Label(left, text="Information Technology Centre",
                 font=("Segoe UI", 11, "bold"), fg=CLR_ACCENT2, bg=CLR_SURFACE,
                 anchor="w").pack(anchor="w")
        tk.Label(left, text="System Scanning & Inventory Tool",
                 font=("Segoe UI", 18, "bold"), fg=CLR_TEXT, bg=CLR_SURFACE,
                 anchor="w").pack(anchor="w")
        tk.Label(left, text="Central Railway  •  Powered by Artificial Intelligence",
                 font=FONT_SMALL, fg=CLR_TEXT_DIM, bg=CLR_SURFACE,
                 anchor="w").pack(anchor="w")

        tk.Label(inner, text=f"v{APP_VERSION}", font=("Segoe UI", 9, "bold"),
                 fg=CLR_ACCENT, bg=CLR_SURFACE2, padx=10, pady=4
                 ).pack(side="right", anchor="n")

        tk.Frame(self, bg=CLR_ACCENT, height=2).pack(fill="x")

    def _build_info_bar(self):
        bar = tk.Frame(self, bg=CLR_SURFACE2)
        bar.pack(fill="x")

        inner = tk.Frame(bar, bg=CLR_SURFACE2)
        inner.pack(padx=18, pady=6)

        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "127.0.0.1"

        for label, value in [
            ("Hostname",     hostname),
            ("IP Address",   ip),
            ("OS",           f"{platform.system()} {platform.release()}"),
            ("Architecture", platform.machine()),
            ("Python",       platform.python_version()),
        ]:
            cell = tk.Frame(inner, bg=CLR_SURFACE2)
            cell.pack(side="left", padx=16)
            tk.Label(cell, text=label, font=FONT_SMALL, fg=CLR_TEXT_DIM,
                     bg=CLR_SURFACE2).pack(anchor="w")
            tk.Label(cell, text=value, font=("Segoe UI", 9, "bold"),
                     fg=CLR_TEXT, bg=CLR_SURFACE2).pack(anchor="w")

        self._time_var = tk.StringVar()
        tk.Label(inner, textvariable=self._time_var, font=FONT_SMALL,
                 fg=CLR_TEXT_DIM, bg=CLR_SURFACE2).pack(side="right", padx=6)
        self._tick_clock()

        tk.Frame(self, bg=CLR_BORDER, height=1).pack(fill="x")

    def _tick_clock(self):
        self._time_var.set(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick_clock)

    def _build_status_bar(self):
        """Packed with side='bottom' BEFORE the log panel."""
        tk.Frame(self, bg=CLR_BORDER, height=1).pack(side="bottom", fill="x")
        bar = tk.Frame(self, bg=CLR_SURFACE)
        bar.pack(side="bottom", fill="x")

        self._status_var = tk.StringVar(value="Idle  •  Click 'Start Scan' to begin")
        tk.Label(bar, textvariable=self._status_var, font=FONT_SMALL,
                 fg=CLR_TEXT_DIM, bg=CLR_SURFACE, anchor="w"
                 ).pack(side="left", padx=14, pady=4)
        tk.Label(bar, text="ITC System Scanner  •  Central Railway",
                 font=FONT_SMALL, fg=CLR_TEXT_DIM, bg=CLR_SURFACE, anchor="e"
                 ).pack(side="right", padx=14, pady=4)

    def _build_action_bar(self):
        """Packed with side='bottom' BEFORE the log panel."""
        tk.Frame(self, bg=CLR_BORDER, height=1).pack(side="bottom", fill="x")
        bar = tk.Frame(self, bg=CLR_SURFACE)
        bar.pack(side="bottom", fill="x")

        inner = tk.Frame(bar, bg=CLR_SURFACE)
        inner.pack(padx=14, pady=10)

        self._scan_btn = tk.Button(
            inner, text="▶  Start Scan",
            font=("Segoe UI", 10, "bold"), fg="#FFFFFF",
            bg=CLR_ACCENT, activeforeground="#FFFFFF", activebackground=CLR_ACCENT2,
            relief="flat", padx=22, pady=7, cursor="hand2",
            command=self._start_scan,
        )
        self._scan_btn.pack(side="left", padx=(0, 10))

        self._open_pdf_btn = tk.Button(
            inner, text="📄  Open Report PDF",
            font=FONT_BODY, fg=CLR_TEXT,
            bg=CLR_SURFACE2, activeforeground=CLR_TEXT, activebackground=CLR_SURFACE,
            relief="flat", padx=16, pady=7, cursor="hand2", state="disabled",
            command=self._open_pdf,
        )
        self._open_pdf_btn.pack(side="left", padx=(0, 8))

        self._open_dir_btn = tk.Button(
            inner, text="📁  Open Output Folder",
            font=FONT_BODY, fg=CLR_TEXT,
            bg=CLR_SURFACE2, activeforeground=CLR_TEXT, activebackground=CLR_SURFACE,
            relief="flat", padx=16, pady=7, cursor="hand2", state="disabled",
            command=self._open_output_dir,
        )
        self._open_dir_btn.pack(side="left", padx=(0, 8))

        self._output_label = tk.Label(
            inner, text="No report generated yet",
            font=FONT_SMALL, fg=CLR_TEXT_DIM, bg=CLR_SURFACE, anchor="e",
        )
        self._output_label.pack(side="right")

    def _build_progress_row(self):
        """Packed with side='bottom' BEFORE the log panel."""
        row = tk.Frame(self, bg=CLR_BG)
        # Bug 3 fix: increased pady so row is never collapsed to zero height
        row.pack(side="bottom", fill="x", padx=14, pady=(4, 8))

        self._progress_label = tk.Label(
            row, text="Ready to scan",
            font=FONT_BODY, fg=CLR_TEXT_DIM, bg=CLR_BG, anchor="w",
        )
        self._progress_label.pack(side="left")

        # Bug 3 fix: removed fixed length=220; bar now fills available width
        # Bug 5 fix: styles already configured in _configure_styles()
        self._progress_bar = ttk.Progressbar(
            row, orient="horizontal", mode="indeterminate",
            style="ITC.Horizontal.TProgressbar",
        )
        self._progress_bar.pack(side="right", fill="x", expand=True, padx=(10, 0))

    def _build_log_panel(self):
        """
        Packed LAST with fill='both' and expand=True so it fills all space
        left over after the top/bottom widgets are placed.
        Contains a ttk.Notebook with four tabs:
          All Logs / Windows / Linux / macOS
        """
        log_outer = tk.Frame(self, bg=CLR_BG)
        log_outer.pack(fill="both", expand=True, padx=14, pady=(10, 4))

        # Header row
        hdr = tk.Frame(log_outer, bg=CLR_BG)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="Scan Log", font=FONT_SUB,
                 fg=CLR_TEXT, bg=CLR_BG).pack(side="left")
        tk.Button(hdr, text="Clear", font=FONT_SMALL,
                  fg=CLR_TEXT_DIM, bg=CLR_SURFACE,
                  activeforeground=CLR_TEXT, activebackground=CLR_SURFACE2,
                  relief="flat", padx=8, pady=2, cursor="hand2",
                  command=self._clear_log).pack(side="right")

        # Notebook
        nb = ttk.Notebook(log_outer, style="ITC.TNotebook")
        nb.pack(fill="both", expand=True)
        nb.update_idletasks()  # Bug 5 fix: flush tab rendering on Linux/older Tk
        self._notebook = nb

        self._log_texts: dict[str, tk.Text] = {}
        for key, label in [
            ("all",     "📋  All Logs"),
            ("windows", "🪟  Windows"),
            ("linux",   "🐧  Linux"),
            ("macos",   "🍎  macOS"),
        ]:
            frame = tk.Frame(nb, bg=CLR_LOG_BG,
                             highlightbackground=CLR_BORDER, highlightthickness=1)
            nb.add(frame, text=label)

            txt = tk.Text(
                frame, font=FONT_MONO, bg=CLR_LOG_BG, fg=CLR_TEXT,
                insertbackground=CLR_ACCENT, relief="flat",
                padx=10, pady=8, state="disabled", wrap="word", cursor="arrow",
            )
            sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            txt.pack(fill="both", expand=True)

            for tag, cfg in [
                ("normal",  {"foreground": CLR_TEXT}),
                ("section", {"foreground": CLR_ACCENT2, "font": ("Consolas", 9, "bold")}),
                ("success", {"foreground": CLR_SUCCESS, "font": ("Consolas", 9, "bold")}),
                ("warning", {"foreground": CLR_WARNING}),
                ("error",   {"foreground": CLR_DANGER,  "font": ("Consolas", 9, "bold")}),
                ("dim",     {"foreground": CLR_TEXT_DIM}),
            ]:
                txt.tag_configure(tag, **cfg)

            self._log_texts[key] = txt

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _append_log(self, text: str, tag: str = "normal", tabs=("all",)):
        """Write text to every specified tab's Text widget."""
        for key in tabs:
            w = self._log_texts.get(key)
            if w:
                w.configure(state="normal")
                w.insert("end", text, tag)
                w.see("end")
                w.configure(state="disabled")

    def _clear_log(self):
        for w in self._log_texts.values():
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.configure(state="disabled")

    def _classify_line(self, line: str) -> str:
        low = line.lower()
        if any(k in low for k in ("error", "failed", "exception", "traceback", "critical")):
            return "error"
        if any(k in low for k in ("warning", "warn")):
            return "warning"
        if any(k in low for k in ("done", "success", "complete", "pdf saved", "json saved", "✓", "[ok]")):
            return "success"
        if line.strip().startswith("[") and "/11]" in line:
            return "section"
        if line.startswith("===") or line.startswith("---"):
            return "dim"
        return "normal"

    def _os_tabs_for_line(self, line: str) -> tuple:
        """Route a log line to one or more OS-specific tabs in addition to 'all'."""
        low = line.lower()
        tabs = ["all"]
        if any(k in low for k in (
            "windows", "powershell", "hyper-v", "get-vm", "get-service",
            "get-package", "win32", ".exe", "wmic", "wsl",
        )):
            tabs.append("windows")
        if any(k in low for k in (
            "linux", "systemctl", "dpkg", "rpm", "apt ", "snap", "flatpak",
            "ufw", "iptables", "selinux", "apparmor", "/etc/", "/proc/",
            "/sys/", "ubuntu", "debian", "centos", "rhel", "fedora",
            "virsh", "kvm", "qemu", "lxc", "lxd",
        )):
            tabs.append("linux")
        if any(k in low for k in (
            "darwin", "macos", "mac os", "osx", "homebrew", "brew",
            "launchctl", "sysctl", "machdep",
        )):
            tabs.append("macos")
        return tuple(tabs)

    def _poll_log_queue(self):
        """Drain the queue into the log widgets every 50 ms."""
        try:
            while True:
                tag, text = self._log_queue.get_nowait()
                for line in text.splitlines(keepends=True):
                    effective_tag = tag if tag != "normal" else self._classify_line(line)
                    self._append_log(line, effective_tag, tabs=self._os_tabs_for_line(line))
        except queue.Empty:
            pass
        self.after(50, self._poll_log_queue)

    # ── Scan control ──────────────────────────────────────────────────────────

    def _start_scan(self):
        if self._is_scanning:
            return

        self._is_scanning  = True
        self._output_pdf   = None
        self._output_json  = None
        self._pending_pdf  = None
        self._pending_json = None

        self._scan_btn.configure(state="disabled", text="⏳  Scanning …")
        self._open_pdf_btn.configure(state="disabled")
        self._open_dir_btn.configure(state="disabled")
        self._output_label.configure(text="Scan in progress …", fg=CLR_TEXT_DIM)
        self._status_var.set("Scanning …  please wait")
        self._progress_bar.start(12)
        self.update_idletasks()  # Bug 3 fix: flush layout so bar renders before thread starts
        self._progress_label.configure(
            text="Scanning — collecting system data …", fg=CLR_ACCENT2)

        self._clear_log()
        self._append_log(
            f"{'='*64}\n"
            f"  ITC System Scanner  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  OS: {platform.system()} {platform.release()}  |  "
            f"Host: {socket.gethostname()}\n"
            f"{'='*64}\n",
            "dim",
        )

        self._scan_thread = threading.Thread(
            target=self._run_scan_worker, daemon=True)
        self._scan_thread.start()

    def _run_scan_worker(self):
        old_stdout = sys.stdout
        old_stderr = sys.stderr

        log_path = os.path.join(
            os.path.expanduser("~"),
            f"itc_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
        )
        try:
            log_fh = open(log_path, "w", encoding="utf-8")
            self._log_file_path = log_path
        except Exception:
            log_fh = None
            self._log_file_path = None

        sys.stdout = _QueueWriter(self._log_queue, "normal",  mirror=log_fh)
        sys.stderr = _QueueWriter(self._log_queue, "warning", mirror=log_fh)

        pdf_path  = None
        json_path = None
        success   = False

        try:
            import importlib
            import importlib.util

            base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
            inv_path = base_dir / "app_inventory.py"

            if inv_path.exists():
                spec   = importlib.util.spec_from_file_location(
                    "app_inventory", str(inv_path))
                module = importlib.util.module_from_spec(spec)
                sys.modules["app_inventory"] = module
                spec.loader.exec_module(module)
            else:
                import app_inventory as module

            report, pdf_path, json_path = _run_inventory(module, self)
            success = True

        except Exception as exc:
            import traceback
            sys.stdout.write(f"\n[ERROR] Scan failed: {exc}\n")
            sys.stdout.write(traceback.format_exc())
            pdf_path  = pdf_path  or self._pending_pdf
            json_path = json_path or self._pending_json
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            if log_fh:
                try:
                    log_fh.flush()
                    log_fh.close()
                except Exception:
                    pass

        # Capture current values for the closure
        _pdf  = pdf_path
        _json = json_path
        _ok   = success
        self.after(0, lambda: self._on_scan_finished(_ok, _pdf, _json))

    def _on_scan_finished(self, success: bool, pdf_path, json_path):
        self._is_scanning = False  # Bug 1 fix: reset flag first before any widget ops
        self._progress_bar.stop()
        self._progress_bar['value'] = 0  # Bug 3 fix: visually reset bar to empty

        pdf_path  = pdf_path  or self._pending_pdf
        json_path = json_path or self._pending_json
        pdf_ok    = bool(pdf_path) and os.path.isfile(pdf_path)

        self._scan_btn.configure(state="normal", text="▶  Start Scan")

        if pdf_ok:
            self._output_pdf  = pdf_path
            self._output_json = (
                json_path if (json_path and os.path.isfile(json_path)) else None
            )
            self._open_pdf_btn.configure(state="normal")
            self._open_dir_btn.configure(state="normal")
            short = os.path.basename(pdf_path)

            if success:
                self._progress_label.configure(
                    text="✅  Scan complete", fg=CLR_SUCCESS)
                self._output_label.configure(
                    text=f"✓  {short}", fg=CLR_SUCCESS)
                self._status_var.set(
                    f"Scan complete  •  {datetime.now().strftime('%H:%M:%S')}")
                self._append_log(
                    f"\n{'='*64}\n"
                    f"  ✓  Scan complete!\n"
                    f"  PDF  →  {pdf_path}\n"
                    f"  JSON →  {self._output_json or '(not written)'}\n"
                    f"{'='*64}\n",
                    "success",
                )
            else:
                self._progress_label.configure(
                    text="⚠  Report saved (errors encountered — see log)",
                    fg=CLR_WARNING)
                self._output_label.configure(text=f"⚠  {short}", fg=CLR_WARNING)
                self._status_var.set("Report saved — some steps reported errors")
                self._append_log(
                    f"\n{'='*64}\n"
                    f"  ⚠  Report saved but the scan hit errors.\n"
                    f"  PDF  →  {pdf_path}\n"
                    f"  JSON →  {self._output_json or '(not written)'}\n"
                    f"  Log  →  {self._log_file_path or '(not written)'}\n"
                    f"{'='*64}\n",
                    "warning",
                )
        else:
            self._progress_label.configure(
                text="❌  Scan failed — see log above", fg=CLR_DANGER)
            self._output_label.configure(text="Scan failed", fg=CLR_DANGER)
            self._status_var.set("Scan failed  •  check log for details")
            if self._log_file_path:
                self._append_log(
                    f"\n  Full log saved to:\n  {self._log_file_path}\n", "dim")

    # ── File actions ──────────────────────────────────────────────────────────

    def _open_pdf(self):
        if not self._output_pdf or not os.path.isfile(self._output_pdf):
            messagebox.showerror("File Not Found",
                                 f"PDF not found:\n{self._output_pdf}")
            return
        _open_file(self._output_pdf)

    def _open_output_dir(self):
        if self._output_pdf:
            _open_file(os.path.dirname(os.path.abspath(self._output_pdf)))


# =============================================================================
# Helpers
# =============================================================================

def _open_file(path: str):
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        messagebox.showerror("Open Failed", f"Could not open:\n{path}\n\n{exc}")


def _run_inventory(module, app=None):
    """
    Drive app_inventory's scan functions and build the PDF.
    Returns (report, pdf_path, json_path).
    """
    import json
    import re
    from datetime import datetime as _dt

    target_ip   = module.get_local_ip()
    system_name = socket.gethostname()
    ts          = _dt.now().strftime("%Y%m%d_%H%M%S")

    safe_ip   = target_ip.replace(".", "_")
    safe_name = re.sub(r"[^A-Za-z0-9_\-]", "_", system_name)
    out_path  = os.path.join(
        os.path.expanduser("~"),
        f"{safe_ip}_{safe_name}_{ts}.pdf",
    )
    json_path = (out_path[:-4] + ".json"
                 if out_path.lower().endswith(".pdf")
                 else out_path + ".json")

    if app is not None:
        app._pending_pdf  = out_path
        app._pending_json = json_path

    print("=" * 64)
    print(f"  ITC System Scanner  ->  {target_ip}  ({system_name})")
    print(f"  Started : {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Output  : {out_path}")
    print("=" * 64)

    report = {}

    print("[1/11] System info ...")
    report["system_info"] = module.get_system_info(target_ip)

    print("[2/11] Disk storage ...")
    report["disk_info"] = module.get_disk_info()

    print("[3/11] Installed packages ...")
    report["installed_packages"] = module.get_installed_packages()
    total = sum(len(v) for v in report["installed_packages"].values())
    print(f"      {total} packages across {len(report['installed_packages'])} managers")

    print("[4/11] Databases ...")
    report["databases"] = module.get_databases()
    print(f"      {len(report['databases'])} found")

    print("[5/11] Web servers / app frameworks ...")
    report["web_applications"] = module.get_web_applications()
    print(f"      {len(report['web_applications'])} found")

    print("[6/11] Running services ...")
    report["running_services"] = module.get_running_services()
    print(f"      {len(report['running_services'])} running")

    print("[7/11] Containers (Docker / Kubernetes) ...")
    report["containers"] = module.get_containers()

    print("[8/11] Virtual machines ...")
    report["virtual_machines"] = module.get_virtual_machines()
    vm_count = report["virtual_machines"]["total"]
    host_vm  = report["virtual_machines"]["host_is_vm"]
    report["system_info"]["host_hypervisor"] = host_vm
    print(f"      {vm_count} VM(s) found")

    print("[9/11] Language runtimes ...")
    report["runtimes"] = module.get_runtimes()
    print(f"       {', '.join(report['runtimes'].keys()) or 'none'}")

    print("[10/11] System health (CPU / RAM / swap / processes) ...")
    report["system_health"] = module.get_system_health()

    print("[11/11] Cyber security posture ...")
    report["cyber_security"] = module.get_cyber_security_posture(
        report["system_health"],
        report["running_services"],
        report["databases"],
        report["web_applications"],
    )
    cs = report["cyber_security"]
    print(
        f"        Score: {cs['score']}/100  Grade: {cs['grade']}  |  "
        f"Critical: {cs['summary']['CRITICAL']}  High: {cs['summary']['HIGH']}  "
        f"Medium: {cs['summary']['MEDIUM']}  Low: {cs['summary']['LOW']}"
    )

    print("\n  Saving JSON data ...")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  JSON saved -> {json_path}")

    print("\n  Building PDF report ...")
    module.build_pdf(report, out_path, system_name)

    print("\n" + "=" * 64)
    print(f"  Done.  Report -> {out_path}")
    print("=" * 64 + "\n")

    return report, out_path, json_path


# =============================================================================
# Entry point
# =============================================================================

def main():
    app = ITCScannerApp()
    app.mainloop()


if __name__ == "__main__":
    main()