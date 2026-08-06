@echo off
chcp 65001 >nul 2>&1
title Video Resumidor - Criar atalho

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%DIR%\criar_atalho_desktop.ps1" -InstallDir "%DIR%"

echo.
pause
