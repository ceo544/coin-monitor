@echo off
REM Quick way to run the app without building an .exe (useful while testing).

setlocal enabledelayedexpansion

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

echo [ERROR] No working Python found. See build_exe.bat for troubleshooting steps
echo         (this is usually the Windows Store fake python.exe shortcut - search
echo         "Manage app execution aliases" in the Start menu and turn python off).
pause
exit /b 1

:found_python
if not exist .venv (
    echo Creating virtual environment...
    %PYCMD% -m venv .venv
    call .venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

python main.py
pause
