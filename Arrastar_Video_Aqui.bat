@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions

if "%~1"=="" (
    echo.
    echo  Arraste um arquivo de video para cima DESTE arquivo.
    echo  Ou use: abrir_interface.bat para a interface no navegador.
    echo.
    pause
    exit /b 1
)

if not exist "%~1" (
    echo ERRO: Arquivo nao encontrado: %~1
    pause
    exit /b 1
)

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"

where resumir >nul 2>&1
if not errorlevel 1 (
    resumir "%~1"
    goto :fim
)

set "INSTALL=%USERPROFILE%\video-resumidor"
set "PY=python"
set "SCRIPT=%DIR%\video_resumidor.py"

if exist "%DIR%\venv\Scripts\python.exe" (
    set "PY=%DIR%\venv\Scripts\python.exe"
) else if exist "%INSTALL%\venv\Scripts\python.exe" (
    set "PY=%INSTALL%\venv\Scripts\python.exe"
)

if not exist "%SCRIPT%" if exist "%INSTALL%\video_resumidor.py" (
    set "SCRIPT=%INSTALL%\video_resumidor.py"
)

curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        start "" "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve
        timeout /t 4 /nobreak >nul
    )
)

"%PY%" "%SCRIPT%" --ollama local "%~1"

:fim
echo.
pause
