@echo off
rem Hive dashboard - Windows service control script (start / stop / restart / status)
rem
rem   scripts\dashboard-service.cmd start [port]
rem   scripts\dashboard-service.cmd stop
rem   scripts\dashboard-service.cmd restart
rem   scripts\dashboard-service.cmd status
rem
rem Note: Windows has no POSIX shell to drive a refresh loop, so serve.py's own
rem       --auto-refresh thread regenerates the status file (same as INTERVAL on Linux).
rem       Stop uses taskkill /T to kill the whole tree, so no orphan keeps the port.

setlocal EnableDelayedExpansion

set "DIR=%~dp0.."
for %%I in ("%DIR%") do set "DIR=%%~fI"
set "DASH=%DIR%\dashboard"
set "SERVEPY=%DASH%\serve.py"
set "STATUS=%DASH%\status.json"
set "PORT=%PORT%"
if "%PORT%"=="" set "PORT=%HIVE_DASHBOARD_PORT%"
if "%PORT%"=="" set "PORT=8787"
rem Bind address: default 0.0.0.0 (LAN reachable). Loopback only: set HIVE_DASHBOARD_HOST=127.0.0.1
set "HOST=%HIVE_DASHBOARD_HOST%"
if "%HOST%"=="" set "HOST=0.0.0.0"
set "INTERVAL=%INTERVAL%"
if "%INTERVAL%"=="" set "INTERVAL=20"
set "STATE=%USERPROFILE%\.hive"
set "RUN=%STATE%\run"
set "PIDFILE=%RUN%\dashboard.pid"
rem Crash logs: serve.py banner -> dashboard.log, tracebacks -> dashboard.err.log
rem (no redirect before, so a dashboard that died on startup left nothing behind).
set "LOGOUT=%STATE%\logs\dashboard.log"
set "LOGERR=%STATE%\logs\dashboard.err.log"

if not exist "%SERVEPY%" (
    echo serve.py not found: %SERVEPY% 1>&2
    exit /b 1
)
if "%~2"=="" goto :noports
set "PORT=%~2"
:noports

if not exist "%RUN%" mkdir "%RUN%" 2>nul
if not exist "%STATE%\logs" mkdir "%STATE%\logs" 2>nul

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=status"

if /i "%ACTION%"=="start" goto :do_start
if /i "%ACTION%"=="stop" goto :do_stop
if /i "%ACTION%"=="restart" goto :do_restart
if /i "%ACTION%"=="status" goto :do_status
echo usage: %~nx0 {start^|stop^|restart^|status} [port] 1>&2
exit /b 2
rem Human-facing URLs: when bound to 0.0.0.0 print both loopback and LAN (hive dash url for the IP)
:show_url
if "%HOST%"=="0.0.0.0" goto :show_url_all
echo     bind  : http://%HOST%:%PORT%/
goto :eof
:show_url_all
echo     local : http://127.0.0.1:%PORT%/
echo     lan   : http://^<this-host^>:%PORT%/  (hive dash url prints the exact IP)
goto :eof


:do_start
rem Liveness check + start in ONE shell call. Do NOT use `tasklist | find`:
rem the right-hand side of a pipe is a helper process, and when the parent cmd
rem has no console that helper gets a *visible* console window of its own and
rem then blocks on stdin forever. 2026-11-01: a stray window titled
rem `find  "11916"` sat on screen since 00:52 while the dashboard was down.
powershell -NoProfile -Command ^
  "$d='%DASH%'; $a=@('-u','%SERVEPY%','--status','%STATUS%','--port','%PORT%','--host','%HOST%','--auto-refresh','--refresh-interval','%INTERVAL%');" ^
  "$pf='%PIDFILE%'; $old=if(Test-Path $pf){Get-Content $pf -TotalCount 1 -ErrorAction SilentlyContinue}else{$null};" ^
  "if($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)){Write-Host ('dashboard already running (pid=' + $old + '), port %PORT%'); exit 0};" ^
  "$p=Start-Process -FilePath 'python' -ArgumentList $a -WorkingDirectory $d -PassThru -WindowStyle Hidden -RedirectStandardOutput '%LOGOUT%' -RedirectStandardError '%LOGERR%';" ^
  "Set-Content -Path $pf -Value $p.Id; Write-Host ('dashboard started (pid=' + $p.Id + '), port %PORT%')"
if errorlevel 1 (
    echo start failed 1>&2
    exit /b 1
)
call :show_url
exit /b 0


:do_stop
if not exist "%PIDFILE%" (
    echo dashboard not running
    exit /b 0
)
set /p PID=<"%PIDFILE%"
if "%PID%"=="" (
    echo dashboard not running
    del "%PIDFILE%" 2>nul
    exit /b 0
)
taskkill /PID %PID% /T /F >nul 2>&1
del "%PIDFILE%" 2>nul
echo dashboard stopped
exit /b 0

:do_restart
call :do_stop
call :do_start
exit /b 0

:do_status
rem Same reason as :do_start - no pipe, no tasklist/find pair.
if not exist "%PIDFILE%" (
    echo not running
    exit /b 1
)
set /p PID=<"%PIDFILE%"
if "%PID%"=="" (
    echo not running ^(stale pid file^)
    exit /b 1
)
powershell -NoProfile -Command "if (Get-Process -Id %PID% -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
    echo not running ^(stale pid file^)
    exit /b 1
)
echo running (pid=%PID%)
echo port: %PORT%
call :show_url
exit /b 0
