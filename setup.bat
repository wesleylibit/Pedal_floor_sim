@echo off
rem Creates .venv next to this file and installs the Python packages.
cd /d "%~dp0"
py -3 --version >nul 2>nul
if %errorlevel%==0 (py -3 -m venv .venv) else (python -m venv .venv)
if not exist .venv\Scripts\python.exe (
    echo Could not create the virtual environment. Install Python 3.11-3.13 from python.org and run this again.
    exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo Done. Open Pedal_floor_FEA_script.ipynb in VS Code and select the .venv kernel.
