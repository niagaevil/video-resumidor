@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions

title Video Resumidor - Instalador Windows

echo.
echo ==============================================
echo    Video Resumidor - Instalador Windows
echo ==============================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERRO: Execute como Administrador!
    echo Clique direito no .bat e escolha "Executar como administrador"
    echo.
    pause
    exit /b 1
)

set "SOURCE_DIR=%~dp0"
if "%SOURCE_DIR:~-1%"=="\" set "SOURCE_DIR=%SOURCE_DIR:~0,-1%"

echo ==============================================
echo  Onde instalar?
echo ==============================================
echo.
echo  1) Nesta pasta (onde esta o instalador)
echo  2) Copiar para pasta do usuario
echo     %USERPROFILE%\video-resumidor
echo.
set /p LOC_CHOICE="Escolha [1-2]: "

if "%LOC_CHOICE%"=="2" (
    set "INSTALL_DIR=%USERPROFILE%\video-resumidor"
    set "INSTALL_LABEL=Copia na pasta do usuario"
) else (
    set "INSTALL_DIR=%SOURCE_DIR%"
    set "INSTALL_LABEL=Nesta pasta"
)

set "VENV_DIR=%INSTALL_DIR%\venv"

echo.
echo Pasta de instalacao: %INSTALL_DIR%
echo Modo de pasta: %INSTALL_LABEL%
echo.

echo ==============================================
echo  Como deseja rodar o Video Resumidor?
echo ==============================================
echo.
echo  1) Local  - Python no Windows + Ollama do Windows
echo  2) Docker  - Tudo em containers (Whisper + Ollama na GPU)
echo.
set /p MODE_CHOICE="Escolha [1-2]: "

if "%MODE_CHOICE%"=="2" (
    set "INSTALL_MODE=Docker"
) else (
    set "INSTALL_MODE=Local"
)

echo.
echo Modo selecionado: %INSTALL_MODE%
echo.

mkdir "%INSTALL_DIR%" 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -File "%SOURCE_DIR%\install_setup.ps1" -InstallDir "%INSTALL_DIR%" -VenvDir "%VENV_DIR%" -SourceDir "%SOURCE_DIR%" -Mode "%INSTALL_MODE%"
if %errorLevel% neq 0 goto :erro

powershell -NoProfile -ExecutionPolicy Bypass -File "%SOURCE_DIR%\install_choose_model.ps1" -InstallDir "%INSTALL_DIR%" -Mode "%INSTALL_MODE%"
if %errorLevel% neq 0 (
    echo.
    echo AVISO: Falha ao baixar modelo. Tente manualmente: ollama pull qwen2.5:7b-instruct
    echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SOURCE_DIR%\criar_atalho_desktop.ps1" -InstallDir "%INSTALL_DIR%"
if %errorLevel% neq 0 (
    echo.
    echo AVISO: Nao foi possivel criar o atalho na area de trabalho.
    echo Rode: Criar_Atalho_Area_Trabalho.bat
    echo.
)

echo.
echo ==============================================
echo  Instalacao concluida! (modo %INSTALL_MODE%)
echo ==============================================
echo.
echo  Tudo instalado em:
echo  %INSTALL_DIR%
echo.
echo  Abra um NOVO Prompt de Comando e use:
echo.
echo  resumir "C:\caminho\do\video.mp4"
echo.
echo  Ou arraste o video em cima de Arrastar_Video_Aqui.bat
echo  Ou use o atalho "Video Resumidor" na area de trabalho
echo.
echo  Para desinstalar (PATH e atalho):
echo  uninstall_windows.bat
echo.
if /i "%INSTALL_MODE%"=="Docker" (
    echo  Antes do primeiro uso, confirme que o Docker Desktop esta aberto.
    echo  O Ollama sobe automaticamente ao rodar resumir.
    echo.
)
pause
exit /b 0

:erro
echo.
echo ERRO na instalacao. Verifique sua conexao e tente novamente.
pause
exit /b 1
