@echo off
REM ============================================================
REM  Coin Monitor - Windows .exe build script
REM  Run this ONCE on your Windows PC (double-click it, or run
REM  from a Command Prompt in this folder). It will:
REM    1) find a working Python
REM    2) create a local virtual environment (.venv)
REM    3) install the required packages
REM    4) install PyInstaller
REM    5) build dist\CoinMonitor.exe
REM
REM  Requirements: Python 3.10+ installed
REM  (https://www.python.org/downloads/ - check "Add python.exe
REM  to PATH" during install).
REM ============================================================

setlocal enabledelayedexpansion

echo [0/5] Looking for a working Python...

REM Try the "py" launcher first (installed by python.org's installer,
REM more reliable than "python" on PATH - Windows ships a fake python.exe
REM stub of its own that "where python" finds but that doesn't actually
REM run Python, which is the single most common reason this script fails).
set PYCMD=
py -3 --version >nul 2>nul
if not errorlevel 1 (
    set PYCMD=py -3
    goto :found_python
)
python --version >nul 2>nul
if not errorlevel 1 (
    set PYCMD=python
    goto :found_python
)

echo.
echo ============================================================
echo  [ERROR] No working Python found.
echo.
echo  If you already installed Python and are seeing this, the
echo  most common cause is Windows' built-in fake "python.exe"
echo  shortcut that opens the Microsoft Store instead of running
echo  Python. To fix it:
echo    1. Press the Windows key, type "Manage app execution aliases"
echo       and open it.
echo    2. Turn OFF the switches for "python.exe" and "python3.exe".
echo    3. Re-run this script.
echo.
echo  If you haven't installed Python yet: get it from
echo    https://www.python.org/downloads/
echo  and check "Add python.exe to PATH" during setup.
echo ============================================================
pause
exit /b 1

:found_python
for /f "delims=" %%v in ('%PYCMD% --version 2^>^&1') do echo   Found: %%v (using "%PYCMD%")

echo [1/5] Creating virtual environment (.venv)...
%PYCMD% -m venv .venv
if not exist .venv\Scripts\activate.bat (
    echo [ERROR] Failed to create the virtual environment - scroll up for the real error.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

echo [2/5] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] pip upgrade failed - scroll up for the real error ^(often a network/proxy issue^).
    pause
    exit /b 1
)

echo [3/5] Installing app requirements...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Installing requirements.txt failed - scroll up for the real error.
    pause
    exit /b 1
)

echo [4/5] Installing PyInstaller...
pip install pyinstaller
if errorlevel 1 (
    echo [ERROR] Installing PyInstaller failed - scroll up for the real error.
    pause
    exit /b 1
)

echo [5/5] Building CoinMonitor.exe (this can take a minute or two)...
pyinstaller --noconfirm --onefile --name CoinMonitor ^
    --hidden-import=tzdata ^
    --collect-data tzdata ^
    --collect-all pywebview ^
    main.py

echo.
if exist dist\CoinMonitor.exe (
    echo ============================================================
    echo  Done! Your app is at:  dist\CoinMonitor.exe
    echo  Double-click it to run - it opens your browser automatically.
    echo  You can copy that one file anywhere (USB stick, another PC, etc.)
    echo  and run it there without installing Python.
    echo ============================================================
) else (
    echo [ERROR] Build did not produce dist\CoinMonitor.exe - scroll up for the
    echo         PyInstaller error above (search for the first line starting
    echo         with "ERROR:" further up this window).
)

pause
