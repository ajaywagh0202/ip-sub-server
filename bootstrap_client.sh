#!/bin/bash
# bootstrap_client.sh
# =====================================================================
# ITC System Scanner — Zero-Touch Linux/macOS Client Bootstrap
# =====================================================================
# Run this ONE command on any Linux/macOS client:
#
#   curl -fsSL http://<SERVER_IP>:5001/static/bootstrap_client.sh | bash -s -- --server http://<SERVER_IP>:5001
#
# Or with wget:
#   wget -qO- http://<SERVER_IP>:5001/static/bootstrap_client.sh | bash -s -- --server http://<SERVER_IP>:5001
#
# Or if you have the file locally:
#   bash bootstrap_client.sh --server http://10.31.3.114:5001
# =====================================================================

set -e

SERVER="http://10.31.3.114:5001"   # default, overridden by --server flag

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --server) SERVER="$2"; shift 2 ;;
        *) shift ;;
    esac
done

echo ""
echo "============================================================"
echo "  ITC System Scanner - Zero-Touch Linux/macOS Bootstrap"
echo "  Server: $SERVER"
echo "============================================================"
echo ""

# ── Find Python 3 ────────────────────────────────────────────────────
PYTHON=""
for candidate in python3 python python3.12 python3.11 python3.10; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" --version 2>&1)
        if echo "$version" | grep -q "Python 3"; then
            PYTHON="$candidate"
            echo "[OK] Found Python: $version"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3 not found."
    echo "        Install it with:"
    echo "          Ubuntu/Debian : sudo apt install python3 python3-pip"
    echo "          CentOS/RHEL   : sudo yum install python3"
    echo "          macOS         : brew install python3"
    exit 1
fi

# ── Download deploy_client.py ─────────────────────────────────────────
DEPLOY_SCRIPT="/tmp/deploy_client.py"
DOWNLOAD_URL="$SERVER/static/deploy_client.py"

echo "[...] Downloading deploy_client.py from $DOWNLOAD_URL ..."
if command -v curl &>/dev/null; then
    curl -fsSL "$DOWNLOAD_URL" -o "$DEPLOY_SCRIPT"
elif command -v wget &>/dev/null; then
    wget -qO "$DEPLOY_SCRIPT" "$DOWNLOAD_URL"
else
    echo "[ERROR] Neither curl nor wget found. Install one and retry."
    exit 1
fi
echo "[OK] Downloaded deploy_client.py"

# ── Run deploy_client.py ─────────────────────────────────────────────
echo "[...] Running deployer..."
echo ""
$PYTHON "$DEPLOY_SCRIPT" --server "$SERVER"

echo ""
echo "[OK] Bootstrap complete."
echo "     Open $SERVER in a browser to see this PC."
