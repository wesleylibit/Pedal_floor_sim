@echo off
setlocal
rem Creates .venv next to this file and installs the Python packages.
rem Needs Python 3.11-3.14; if none is found, offers to install Python 3.13 with winget.
cd /d "%~dp0"

call :find_python
if not defined PYTHON (
    echo No Python 3.11-3.14 found on this PC.
    where winget >nul 2>nul || goto :no_python
    choice /c YN /m "Install Python 3.13 for this Windows user now (winget, no admin rights needed)"
    if errorlevel 2 goto :no_python
    winget install --exact --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    call :find_python
)
if not defined PYTHON goto :no_python
echo Using Python: %PYTHON%

rem A .venv copied from another PC points to that PC's Python and does not run here.
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -c "import sys" >nul 2>nul || (
        echo The existing .venv does not work on this PC; recreating it.
        rmdir /s /q .venv
    )
)
if not exist .venv\Scripts\python.exe %PYTHON% -m venv .venv
if not exist .venv\Scripts\python.exe (
    echo Could not create the virtual environment .venv in this folder.
    goto :end_failed
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || (
    echo.
    echo Installing the packages failed; see the messages above. Check the internet connection and run setup.bat again.
    goto :end_failed
)
echo.
echo Done. Open this folder in VS Code, open Pedal_floor_FEA_script.ipynb and select the .venv kernel.
pause
exit /b 0

:no_python
echo.
echo Install Python 3.13 from https://www.python.org/downloads/ and run setup.bat again.
:end_failed
pause
exit /b 1

rem Sets PYTHON to a command that runs Python 3.11-3.14, or leaves it undefined.
rem "python" alone is not enough: on Windows it is often the Microsoft Store placeholder.
:find_python
set "PYTHON="
for %%v in (3.13 3.14 3.12 3.11) do (
    if not defined PYTHON py -%%v -c "import sys" >nul 2>nul && set "PYTHON=py -%%v"
)
if not defined PYTHON (
    python -c "import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 14))" >nul 2>nul && set "PYTHON=python"
)
rem Freshly installed: not on this window's PATH yet, so look in the default per-user folders.
for %%v in (313 314 312 311) do (
    if not defined PYTHON if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" set "PYTHON="%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe""
)
exit /b 0
