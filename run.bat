@echo off
REM ──────────────────────────────────────────
REM  Project Harvest — One-Click Launcher
REM  WiTree Technology Solutions Pvt Ltd
REM ──────────────────────────────────────────

echo.
echo  ╔══════════════════════════════════════╗
echo  ║      Project Harvest  v1.0           ║
echo  ║      Web-Based Image Grabber         ║
echo  ╚══════════════════════════════════════╝
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Please install Python 3.9+ from https://python.org
    pause
    exit /b 1
)

REM Install Python dependencies
echo [1/3] Installing Python dependencies...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

REM Install Playwright browser (Chromium)
echo [2/3] Setting up browser engine (first run only, may take a minute)...
playwright install chromium --with-deps 2>nul
if errorlevel 1 (
    echo         Trying without --with-deps...
    playwright install chromium
)

REM Start server
echo [3/3] Starting server...
echo.
echo  ══════════════════════════════════════════
echo   Application running at:
echo   http://127.0.0.1:5000
echo.
echo   Open the URL above in your browser.
echo   Press Ctrl+C to stop the server.
echo  ══════════════════════════════════════════
echo.

python app.py
pause
