@echo off
REM ---------------------------------------------------------------------------
REM  Copy punches from the punching machines into the factory database.
REM
REM  Wrapper for Task Scheduler, which cannot set a working directory reliably
REM  enough to trust: attendance_sync.py reads .env from the directory it runs
REM  in, and a task that starts in C:\Windows\System32 finds no .env, fails on
REM  the first need() call, and looks exactly like a night nobody punched.
REM  So this cd's to the repo itself, relative to where this file lives.
REM
REM  Scheduled twice a day, 12:45 and 23:15. The application server rolls these
REM  up at 13:00 and 23:30 -- fifteen minutes later, and that order is not
REM  optional. Roll up before the punches land and the register marks the whole
REM  workforce absent for the day.
REM
REM  Exits with the script's own exit code so Task Scheduler shows a red "Last
REM  Run Result" rather than a green one over a failed pull.
REM ---------------------------------------------------------------------------

setlocal

REM The repo root is one level up from this file, wherever it was installed.
cd /d "%~dp0.."

REM The venv if there is one, the PATH interpreter if not. pymssql and psycopg2
REM are the reason a venv is worth having here; a bare python without them
REM fails on import, which the log will say plainly.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "logs" mkdir "logs"
set "LOG=logs\attendance_sync.log"

echo.>> "%LOG%"
echo ==== %DATE% %TIME% ====>> "%LOG%"
"%PY%" attendance_sync.py --days 2 >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo FAILED: exit code %RC%. Punches were NOT copied.>> "%LOG%"
    echo The server roll-up will refuse rather than mark people absent,>> "%LOG%"
    echo so the register keeps yesterday until this is fixed.>> "%LOG%"
)

endlocal & exit /b %RC%
