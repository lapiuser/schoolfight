@echo off
setlocal

rem KSL local launcher: use Python 3.14 when available.
set "PY_CMD=python"
where py >nul 2>&1
if %errorlevel%==0 (
    py -3.14 --version >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3.14"
)

rem Recreate a venv created with an incompatible/old Python (e.g. 3.9).
set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    set "VENV_VERSION="
    for /f "tokens=2" %%V in ('"%VENV_PY%" --version 2^>^&1') do set "VENV_VERSION=%%V"
    if not "%VENV_VERSION:~0,4%"=="3.14" (
        echo Existing .venv uses Python %VENV_VERSION%. Recreating it with Python 3.14...
        rmdir /s /q .venv
    )
)

if not exist .venv %PY_CMD% -m venv .venv
call .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
