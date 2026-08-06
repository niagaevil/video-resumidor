@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions

title Video Resumidor - Desinstalador

echo.
echo ==============================================
echo    Video Resumidor - Desinstalador
echo ==============================================
echo.
echo  Remove entradas do PATH do usuario e o atalho
echo  da area de trabalho criados pelo instalador.
echo.

set "SOURCE_DIR=%~dp0"
if "%SOURCE_DIR:~-1%"=="\" set "SOURCE_DIR=%SOURCE_DIR:~0,-1%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SOURCE_DIR%\uninstall_setup.ps1" -SourceDir "%SOURCE_DIR%"

if %errorLevel% neq 0 (
    echo.
    echo ERRO na desinstalacao.
    pause
    exit /b 1
)

pause
