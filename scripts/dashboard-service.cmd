@echo off
rem 蜂巢 Hive 看板 —— Windows 服务控制脚本（start / stop / restart / status）
rem
rem   scripts\dashboard-service.cmd start [port]
rem   scripts\dashboard-service.cmd stop
rem   scripts\dashboard-service.cmd restart
rem   scripts\dashboard-service.cmd status
rem
rem 说明：Windows 没有 POSIX shell 去跑刷新循环，所以这里直接让 serve.py 自带的
rem       --auto-refresh 线程负责定期重生成状态文件（等价于 Linux 下的 INTERVAL 循环）。
rem       停止用 taskkill /T —— 杀整棵进程树，避免子进程变孤儿继续占端口。

setlocal EnableDelayedExpansion

set "DIR=%~dp0.."
for %%I in ("%DIR%") do set "DIR=%%~fI"
set "DASH=%DIR%\dashboard"
set "SERVEPY=%DASH%\serve.py"
set "STATUS=%DASH%\status.json"
set "PORT=%PORT%"
if "%PORT%"=="" set "PORT=8791"
set "INTERVAL=%INTERVAL%"
if "%INTERVAL%"=="" set "INTERVAL=20"
set "STATE=%USERPROFILE%\.hive"
set "RUN=%STATE%\run"
set "PIDFILE=%RUN%\dashboard.pid"

if not exist "%SERVEPY%" (
    echo 找不到 %SERVEPY% 1>&2
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
echo 用法: %~nx0 {start^|stop^|restart^|status} [port] 1>&2
exit /b 2

:do_start
if exist "%PIDFILE%" (
    set /p PID=<"%PIDFILE%"
    tasklist /FI "PID eq !PID!" 2>nul | find "!PID!" >nul
    if not errorlevel 1 (
        echo 看板已在运行（pid=!PID!），端口 %PORT%
        echo 访问：http://127.0.0.1:%PORT%/
        exit /b 0
    )
)
powershell -NoProfile -Command ^
  "$d='%DASH%'; $a=@('%SERVEPY%','--status','%STATUS%','--port','%PORT%','--auto-refresh','--refresh-interval','%INTERVAL%');" ^
  "$p=Start-Process -FilePath 'python' -ArgumentList $a -WorkingDirectory $d -PassThru -WindowStyle Hidden;" ^
  "Set-Content -Path '%PIDFILE%' -Value $p.Id; Write-Host ('看板已启动（pid=' + $p.Id + '），端口 %PORT%')"
if errorlevel 1 (
    echo 启动失败 1>&2
    exit /b 1
)
echo 访问：http://127.0.0.1:%PORT%/
exit /b 0

:do_stop
if not exist "%PIDFILE%" (
    echo 看板未在运行
    exit /b 0
)
set /p PID=<"%PIDFILE%"
if "%PID%"=="" (
    echo 看板未在运行
    del "%PIDFILE%" 2>nul
    exit /b 0
)
taskkill /PID %PID% /T /F >nul 2>&1
del "%PIDFILE%" 2>nul
echo 看板已停止
exit /b 0

:do_restart
call :do_stop
call :do_start
exit /b 0

:do_status
if not exist "%PIDFILE%" (
    echo 未运行
    exit /b 1
)
set /p PID=<"%PIDFILE%"
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if errorlevel 1 (
    echo 未运行（PID 文件残留）
    exit /b 1
)
echo 运行中（pid=%PID%）
echo 端口：%PORT%
echo 地址：http://127.0.0.1:%PORT%/
exit /b 0
