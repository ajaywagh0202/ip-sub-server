# bootstrap_client.ps1
# =====================================================================
# ITC System Scanner — Zero-Touch Windows Client Bootstrap
# =====================================================================
# Run this ONE command on any Windows client PC (PowerShell as Admin):
#
#   Set-ExecutionPolicy Bypass -Scope Process -Force; iex ((New-Object System.Net.WebClient).DownloadString('http://<SERVER_IP>:5001/static/bootstrap_client.ps1'))
#
# Or if you have the file locally:
#   powershell -ExecutionPolicy Bypass -File bootstrap_client.ps1
# =====================================================================

param(
    [string]$Server = "http://10.31.3.114:5001"   # <-- SET YOUR SERVER IP HERE
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  ITC System Scanner - Zero-Touch Windows Client Bootstrap" -ForegroundColor Cyan
Write-Host "  Server: $Server" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ── Find Python ──────────────────────────────────────────────────────
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3") {
            $python = $candidate
            Write-Host "[OK] Found Python: $ver" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $python) {
    Write-Host "[ERROR] Python 3 not found." -ForegroundColor Red
    Write-Host "        Download from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "        Tick 'Add Python to PATH' during install, then re-run this script." -ForegroundColor Yellow
    exit 1
}

# ── Download deploy_client.py from server ────────────────────────────
$deployScript = "$env:TEMP\deploy_client.py"
$downloadUrl  = "$Server/static/deploy_client.py"

Write-Host "[...] Downloading deploy_client.py from $downloadUrl ..." -ForegroundColor Yellow
try {
    (New-Object System.Net.WebClient).DownloadFile($downloadUrl, $deployScript)
    Write-Host "[OK] Downloaded deploy_client.py" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Could not download deploy_client.py: $_" -ForegroundColor Red
    Write-Host "        Make sure the server is running and reachable." -ForegroundColor Yellow
    exit 1
}

# ── Run deploy_client.py ─────────────────────────────────────────────
Write-Host "[...] Running deployer..." -ForegroundColor Yellow
Write-Host ""
& $python $deployScript --server $Server

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Deployment complete!" -ForegroundColor Green
    Write-Host "     Open $Server in a browser to see this PC." -ForegroundColor Green
} else {
    Write-Host "[ERROR] Deployment failed. Check output above." -ForegroundColor Red
}
