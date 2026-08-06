@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions

title Video Resumidor - Interface

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

set "INSTALL=%USERPROFILE%\video-resumidor"
set "UI=%DIR%\interface_web.py"
set "PY=python"

if exist "%DIR%\venv\Scripts\python.exe" (
    set "PY=%DIR%\venv\Scripts\python.exe"
) else if exist "%INSTALL%\venv\Scripts\python.exe" (
    set "PY=%INSTALL%\venv\Scripts\python.exe"
)

set "PYTHONIOENCODING=utf-8"

if not exist "%UI%" if exist "%INSTALL%\interface_web.py" (
    set "UI=%INSTALL%\interface_web.py"
)

if not exist "%UI%" (
    echo.
    echo ERRO: interface_web.py nao encontrado.
    echo Procurei em:
    echo   %DIR%
    echo   %INSTALL%
    echo.
    pause
    exit /b 1
)

echo Encerrando interface antiga na porta 8765 (se houver)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo Usando: %UI%
echo.

curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        start "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
        timeout /t 4 /nobreak >nul
    )
)

"%PY%" "%UI%"
