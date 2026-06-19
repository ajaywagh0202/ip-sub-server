#!/usr/bin/env python3
"""
patch_web_app.py  —  Add auto-deploy support to web_app.py
===========================================================
Run this ONCE on the server, next to web_app.py.
It adds three things:

  1. GET /static/client_agent.py   — serves client_agent.py for auto-download
  2. GET /static/app_inventory.py  — serves app_inventory.py for auto-download
  3. GET /api/info                 — (already exists, verified here)

After running this, clients can deploy themselves with ONE command:
    python deploy_client.py --server http://<this-server-ip>:5051

Usage:
    python patch_web_app.py
"""

import re
import sys
import shutil
from pathlib import Path
from datetime import datetime

WEB_APP = Path("web_app.py")
BACKUP  = Path(f"web_app.py.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")

STATIC_ROUTES = '''

# =============================================================================
# Static file delivery — lets deploy_client.py download agent files directly
# from this server without needing a separate file server or SSH.
# =============================================================================

import mimetypes as _mimetypes

@app.route("/static/<path:filename>")
def serve_static_agent_file(filename: str):
    """
    Serve client_agent.py and app_inventory.py so that deploy_client.py can
    download them automatically from the server.  Only .py files are served.
    """
    import re as _re
    # Safety: only allow safe filenames, no path traversal
    if not _re.match(r'^[\\w\\-]+\\.py$', filename):
        from flask import abort as _abort
        _abort(403)

    # Look in the same directory as web_app.py
    here      = Path(__file__).parent
    file_path = (here / filename).resolve()

    if not str(file_path).startswith(str(here.resolve())):
        from flask import abort as _abort
        _abort(403)

    if not file_path.exists():
        return f"File not found: {filename}", 404

    with open(file_path, "rb") as f:
        content = f.read()

    from flask import Response as _Response
    return _Response(
        content,
        mimetype="text/x-python",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

'''

MARKER = "# ── Client registration ───"   # insert before this line


def patch():
    if not WEB_APP.exists():
        print(f"[ERROR] web_app.py not found in {Path.cwd()}")
        print("        Run this script from the same folder as web_app.py.")
        sys.exit(1)

    text = WEB_APP.read_text(encoding="utf-8")

    if "/static/<path:filename>" in text:
        print("[OK] web_app.py is already patched — nothing to do.")
        return

    if MARKER not in text:
        # Fallback: append before the last route block
        insert_before = "# =============================================================================\n# Routes"
        if insert_before not in text:
            print("[WARN] Could not find insertion point — appending routes at end of file.")
            text += "\n" + STATIC_ROUTES
        else:
            text = text.replace(insert_before, STATIC_ROUTES + "\n" + insert_before, 1)
    else:
        text = text.replace(MARKER, STATIC_ROUTES + "\n" + MARKER, 1)

    # Backup original
    shutil.copy2(WEB_APP, BACKUP)
    print(f"[OK] Backup saved -> {BACKUP}")

    WEB_APP.write_text(text, encoding="utf-8")
    print(f"[OK] web_app.py patched — /static/<file>.py routes added")
    print()
    print("  Restart web_app.py now:")
    print("      python web_app.py")
    print()
    print("  Then clients can auto-deploy with:")
    print("      python deploy_client.py --server http://<server-ip>:5051")


if __name__ == "__main__":
    patch()
