@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 若 python 不在 PATH 中，请将下面一行改为本机 python 的绝对路径
set "PY=python"

netstat -ano 2>nul | findstr ":8766" | findstr "LISTEN" >nul
if %errorlevel%==0 (
    echo 迁移控制台已在运行（端口 8766 被占用）。
    echo 请直接在浏览器打开 http://127.0.0.1:8766
    pause
    exit /b
)

echo 正在启动迁移控制台...
echo 访问地址： http://127.0.0.1:8766
echo （关闭此窗口即可停止服务）
%PY% unified_server.py
