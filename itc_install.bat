@echo off
setlocal
set SERVER=http://10.31.3.114/:5051
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

:: ── Step 2: Fallback — look for Python ───────────────────────────────────────
echo [...] Looking for Python 3 on this PC ...
set PYTHON=
where python >nul 2>nul && (python --version 2>&1 | findstr /i "Python 3" >nul && set PYTHON=python)
if not defined PYTHON (where py >nul 2>nul && (py --version 2>&1 | findstr /i "Python 3" >nul && set PYTHON=py))
if not defined PYTHON (where python3 >nul 2>nul && set PYTHON=python3)

if not defined PYTHON (
    echo.
    echo [ERROR] Python 3 was not found on this PC, and no pre-built EXE is
    echo         available from the server either.
    echo.
    echo         To fix this, choose ONE of:
    echo.
    echo         Option A ^(Recommended^) — Ask your IT admin to place
    echo           client_agent.exe in the server's static folder, then
    echo           re-run this installer. No Python needed.
    echo.
    echo         Option B — Install Python 3 manually:
    echo           https://www.python.org/downloads/
    echo           IMPORTANT: tick "Add python.exe to PATH", then re-run.
    echo.
    pause
    exit /b 1
)
echo [OK] Found Python: %PYTHON%

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
