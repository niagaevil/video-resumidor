param(
    [string]$InstallDir,
    [string]$VenvDir,
    [string]$SourceDir,
    [ValidateSet('Local', 'Docker')]
    [string]$Mode = 'Local'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$InstallNorm = [System.IO.Path]::GetFullPath($InstallDir)
$SourceNorm  = [System.IO.Path]::GetFullPath($SourceDir)
$InstallInPlace = ($InstallNorm -eq $SourceNorm)

$script:PathEntriesAdded = @()
$script:PortableFfmpeg = $false
$ManifestName = 'video-resumidor.manifest.json'

$TempDir = Join-Path $InstallDir 'temp'
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

function Step { param([string]$n,[string]$t) Write-Host "`n=== $n - $t ===" -ForegroundColor Cyan }
function Ok   { param([string]$m) Write-Host "OK: $m" -ForegroundColor Green }
function Info { param([string]$m) Write-Host ">> $m" }
function warn { param([string]$m) Write-Host "AVISO: $m" -ForegroundColor Yellow }

function Format-Bytes {
    param([long]$Bytes)
    if ($Bytes -ge 1GB) { return '{0:N1} GB' -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return '{0:N1} MB' -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return '{0:N1} KB' -f ($Bytes / 1KB) }
    return "$Bytes B"
}

function Download-FileWithProgress {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Destination,
        [string]$Label = 'Arquivo'
    )

    $destDir = Split-Path $Destination -Parent
    if ($destDir -and -not (Test-Path $destDir)) {
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }

    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        Info "Baixando $Label..."
        Info "Origem: $Url"
        $curl = Start-Process -FilePath 'curl.exe' -ArgumentList @(
            '-L', '--fail', '--progress-bar', '-o', $Destination, $Url
        ) -NoNewWindow -Wait -PassThru
        if ($curl.ExitCode -ne 0 -or -not (Test-Path $Destination)) {
            throw "Falha ao baixar $Label (curl codigo $($curl.ExitCode))."
        }
        $size = (Get-Item $Destination).Length
        Ok "$Label baixado ($(Format-Bytes $size))"
        return
    }

    Info "Baixando $Label (sem curl.exe, progresso em %%)..."
    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.UserAgent = 'video-resumidor-installer'
    $response = $request.GetResponse()
    $total = [long]$response.ContentLength
    $stream = $null
    $fileStream = $null
    try {
        $stream = $response.GetResponseStream()
        $fileStream = [System.IO.File]::Create($Destination)
        $buffer = New-Object byte[] 65536
        $downloaded = 0L
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $fileStream.Write($buffer, 0, $read)
            $downloaded += $read
            if ($total -gt 0) {
                $pct = [int][math]::Min(100, [math]::Floor(($downloaded * 100) / $total))
                Write-Progress -Activity "Baixando $Label" `
                    -Status "$(Format-Bytes $downloaded) / $(Format-Bytes $total) ($pct%%)" `
                    -PercentComplete $pct
            } else {
                Write-Progress -Activity "Baixando $Label" -Status "$(Format-Bytes $downloaded) baixados"
            }
        }
    } finally {
        Write-Progress -Activity "Baixando $Label" -Completed
        if ($fileStream) { $fileStream.Close() }
        if ($stream) { $stream.Close() }
        $response.Close()
    }

    if (-not (Test-Path $Destination)) {
        throw "Falha ao baixar $Label."
    }
    Ok "$Label baixado ($(Format-Bytes ((Get-Item $Destination).Length)))"
}

function Add-DirToUserPath {
    param([string]$Dir)
    if (-not $Dir) { return }
    $Dir = [System.IO.Path]::GetFullPath($Dir.TrimEnd('\'))
    $cur = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $already = $false
    if ($cur) {
        foreach ($part in $cur -split ';') {
            if (-not $part) { continue }
            $norm = [System.IO.Path]::GetFullPath($part.TrimEnd('\'))
            if ($norm -eq $Dir) { $already = $true; break }
        }
    }
    if (-not $already) {
        $newPath = if ($cur) { "$cur;$Dir" } else { $Dir }
        [System.Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        $script:PathEntriesAdded += $Dir
        Ok "PATH do usuario atualizado: $Dir"
    }
    if ($env:Path -notlike "*$Dir*") {
        $env:Path += ";$Dir"
    }
}

function Write-InstallManifest {
    $manifest = [ordered]@{
        version          = 1
        installDir       = $InstallNorm
        sourceDir        = $SourceNorm
        installInPlace   = $InstallInPlace
        mode             = $Mode
        pathEntriesAdded = @($script:PathEntriesAdded | Select-Object -Unique)
        portableFfmpeg   = $script:PortableFfmpeg
        installedAt      = (Get-Date).ToString('o')
    }
    $json = $manifest | ConvertTo-Json -Depth 4
    $path = Join-Path $InstallDir $ManifestName
    [System.IO.File]::WriteAllText($path, $json, [System.Text.UTF8Encoding]::new($false))
    Ok "Registro de instalacao: $path"
}

function Test-RealPython {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { return $false }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = (python --version 2>&1) | Out-String
    $ErrorActionPreference = $oldEap
    return ($out -match 'Python 3') -and ($out -notmatch 'was not found|App Installer|Store')
}

function Test-Docker {
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = (docker version 2>&1) | Out-String
    $ErrorActionPreference = $oldEap
    return $out -match 'Server:' -and $out -notmatch 'error|cannot connect'
}

function Copy-ProjectFile {
    param([string]$Name)
    $src = Join-Path $SourceDir $Name
    if (-not (Test-Path $src)) {
        throw "Arquivo nao encontrado no projeto: $Name"
    }
    if ($InstallInPlace) { return }
    Copy-Item $src (Join-Path $InstallDir $Name) -Force
}

function New-DesktopShortcut {
    param([string]$TargetDir)
    $script = Join-Path $SourceDir 'criar_atalho_desktop.ps1'
    if (-not (Test-Path $script)) { return }
    & $script -InstallDir $TargetDir
}

function Install-FFmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Ok 'FFmpeg ja instalado no PATH'
        return
    }
    if (Test-Path "$InstallDir\ffmpeg.exe") {
        $script:PortableFfmpeg = $true
        Add-DirToUserPath $InstallDir
        Ok 'FFmpeg ja presente na pasta de instalacao'
        return
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Info 'Tentando instalar FFmpeg via winget (mais rapido)...'
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & winget install --id Gyan.FFmpeg -e `
            --accept-source-agreements --accept-package-agreements --disable-interactivity 2>&1 | ForEach-Object { Info $_ }
        $ErrorActionPreference = $oldEap
        if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
            Ok 'FFmpeg instalado via winget'
            return
        }
        warn 'winget nao deixou o ffmpeg no PATH — baixando versao portable...'
    }

    $zip = "$TempDir\ffmpeg.zip"
    Download-FileWithProgress `
        -Url 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' `
        -Destination $zip `
        -Label 'FFmpeg (zip ~80 MB)'

    Info 'Extraindo FFmpeg (pode levar alguns segundos)...'
    Expand-Archive $zip -DestinationPath "$TempDir\ffmpeg_ext" -Force
    $ffDir = Get-ChildItem "$TempDir\ffmpeg_ext" -Directory | Select-Object -First 1
    if (-not $ffDir) {
        throw 'Pacote FFmpeg invalido apos extracao.'
    }
    Copy-Item "$($ffDir.FullName)\bin\ffmpeg.exe"  "$InstallDir\ffmpeg.exe" -Force
    Copy-Item "$($ffDir.FullName)\bin\ffprobe.exe" "$InstallDir\ffprobe.exe" -Force
    $script:PortableFfmpeg = $true
    Add-DirToUserPath $InstallDir
    Ok 'FFmpeg instalado na pasta do projeto'
}

function Add-InstallDirToPath {
    Add-DirToUserPath $InstallDir
}

function Find-OllamaExe {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe",
        ${env:ProgramFiles(x86)} + '\Ollama\ollama.exe'
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    return $null
}

function Install-Ollama {
    $existing = Find-OllamaExe
    if ($existing) {
        Add-DirToUserPath (Split-Path $existing -Parent)
        Ok "Ollama ja instalado: $existing"
        return $existing
    }

    $setup = "$TempDir\OllamaSetup.exe"
    Download-FileWithProgress `
        -Url 'https://ollama.com/download/OllamaSetup.exe' `
        -Destination $setup `
        -Label 'Ollama (~80 MB)'

    Info 'Instalando Ollama (aguarde 1-2 min)...'
    $proc = Start-Process $setup -ArgumentList '/VERYSILENT', '/NORESTART', '/SP-' -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        warn "Instalador retornou codigo $($proc.ExitCode); tentando /silent..."
        Start-Process $setup -ArgumentList '/silent' -Wait | Out-Null
    }

    for ($i = 1; $i -le 12; $i++) {
        $existing = Find-OllamaExe
        if ($existing) { break }
        Info "Aguardando instalacao do Ollama... ($i/12)"
        Start-Sleep -Seconds 2
    }

    $existing = Find-OllamaExe
    if (-not $existing) {
        throw @"
Ollama nao foi encontrado apos a instalacao.
Baixe manualmente em https://ollama.com/download e instale.
Depois rode este instalador novamente.
"@
    }

    Add-DirToUserPath (Split-Path $existing -Parent)

    Info 'Iniciando servico Ollama...'
    Start-Process $existing -ArgumentList 'serve' -WindowStyle Hidden
    Start-Sleep -Seconds 4

    $ready = $false
    for ($i = 1; $i -le 10; $i++) {
        try {
            $r = Invoke-WebRequest 'http://localhost:11434/api/tags' -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
        Info "Aguardando Ollama responder na porta 11434... ($i/10)"
        Start-Sleep -Seconds 2
    }

    if ($ready) {
        Ok "Ollama instalado e respondendo: $existing"
    } else {
        warn "Ollama instalado em $existing, mas ainda nao respondeu."
        warn 'Abra o Ollama pelo Menu Iniciar ou execute: ollama serve'
    }
    return $existing
}

function Get-NvidiaCudaBinPaths {
    param([string]$SitePackages)
    $paths = @()
    foreach ($sub in @('cublas', 'cudnn', 'cuda_runtime', 'cuda_nvrtc')) {
        $p = Join-Path $SitePackages "nvidia\$sub\bin"
        if (Test-Path $p) { $paths += $p }
    }
    return $paths
}

function Install-CudaLibs {
    param([string]$PipExe, [string]$SitePackages)

    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) {
        warn 'GPU NVIDIA nao detectada — pulando bibliotecas CUDA (usara CPU)'
        return ''
    }

    Info 'Instalando cuBLAS/CUDNN para Whisper na GPU...'
    $pipArgs = @(
        'install', '--progress-bar', 'on',
        'nvidia-cublas-cu12', 'nvidia-cudnn-cu12', 'nvidia-cuda-runtime-cu12'
    )
    $pipProc = Start-Process -FilePath $PipExe -ArgumentList $pipArgs -NoNewWindow -Wait -PassThru
    if ($pipProc.ExitCode -ne 0) {
        throw 'Falha ao instalar bibliotecas CUDA no venv.'
    }
    $paths = Get-NvidiaCudaBinPaths $SitePackages
    if ($paths.Count -eq 0) {
        throw 'Pacotes CUDA instalados mas DLLs nao encontradas no venv.'
    }
    Ok "CUDA libs: $($paths.Count) pastas"
    return ($paths -join ';')
}

function Write-ResumirBat {
    param([string[]]$Lines)
    [System.IO.File]::WriteAllLines(
        "$InstallDir\resumir.bat",
        $Lines,
        [System.Text.UTF8Encoding]::new($false)
    )
}

# ── Arquivos comuns ─────────────────────────────────────────────────────────
if ($InstallInPlace) {
    Step '1/4' 'Verificando arquivos do projeto'
} else {
    Step '1/4' 'Copiando arquivos do projeto'
}
Copy-ProjectFile 'video_resumidor.py'
Copy-ProjectFile 'interface_web.py'
Copy-ProjectFile 'prompts.py'
Copy-ProjectFile 'prompts.json'
Copy-ProjectFile 'model_config.py'
Copy-ProjectFile 'requirements.txt'
Copy-ProjectFile 'VERSION'
Copy-ProjectFile 'abrir_interface.bat'
Copy-ProjectFile 'Arrastar_Video_Aqui.bat'
Copy-ProjectFile 'criar_atalho_desktop.ps1'
Copy-ProjectFile 'Criar_Atalho_Area_Trabalho.bat'
Copy-ProjectFile 'uninstall_windows.bat'
Copy-ProjectFile 'uninstall_setup.ps1'
Copy-ProjectFile 'install_pull_model.ps1'
Copy-ProjectFile 'install_choose_model.ps1'
if ($InstallInPlace) {
    Ok 'Arquivos OK nesta pasta'
} else {
    Ok 'Arquivos copiados'
}

if ($Mode -eq 'Docker') {
    # ── DOCKER ──────────────────────────────────────────────────────────────
    Step '2/4' 'Docker'
    if (-not (Test-Docker)) {
        throw @"
Docker nao encontrado ou nao esta rodando.
Instale o Docker Desktop: https://www.docker.com/products/docker-desktop/
Abra o Docker Desktop e rode o instalador novamente.
"@
    }
    Ok 'Docker disponivel'

    Step '3/4' 'Imagens Docker'
    Copy-ProjectFile 'docker-compose.yml'
    Copy-ProjectFile 'Dockerfile'
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'videos') | Out-Null

    Push-Location $InstallDir
    try {
        Info 'Construindo imagem do app (pode demorar na primeira vez)...'
        docker compose build app
        Info 'Subindo Ollama...'
        docker compose up -d ollama
        Ok 'Containers prontos'
    } finally {
        Pop-Location
    }

    Step '4/4' 'Comando resumir'
    $bat = @(
        '@echo off'
        'setlocal EnableExtensions'
        'if "%~1"=="" ('
        '    echo Uso: resumir "C:\caminho\do\video.mp4"'
        '    exit /b 1'
        ')'
        'if not exist "%~1" ('
        '    echo ERRO: Arquivo nao encontrado: %~1'
        '    exit /b 1'
        ')'
        "cd /d `"$InstallDir`""
        'docker compose up -d ollama >nul 2>&1'
        'set "VIDEO_DIR=%~dp1"'
        'set "VIDEO_DIR=%VIDEO_DIR:~0,-1%"'
        'set "VIDEO_NAME=%~nx1"'
        'docker compose --profile run run --rm -e "VIDEO_DIR=%VIDEO_DIR%" app "/videos/%VIDEO_NAME%"'
    )
    Write-ResumirBat $bat
    Add-InstallDirToPath

} else {
    # ── LOCAL (Python + Ollama Windows) ─────────────────────────────────────
    Step '2/4' 'Python'
    if (Test-RealPython) {
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $v = (python --version 2>&1) | Out-String
        $ErrorActionPreference = $oldEap
        Ok "Python ja instalado: $($v.Trim())"
    } else {
        $pySetup = "$TempDir\python_setup.exe"
        Download-FileWithProgress `
            -Url 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' `
            -Destination $pySetup `
            -Label 'Python 3.11'
        Info 'Instalando Python (aguarde)...'
        Start-Process $pySetup `
            -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1' -Wait
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not (Test-RealPython)) {
            throw 'Python nao foi instalado. Instale em https://www.python.org/downloads/'
        }
        Ok 'Python instalado'
    }

    Step '3/4' 'FFmpeg e pacotes Python'
    Install-FFmpeg

    if (-not (Test-Path $VenvDir)) {
        Info 'Criando venv...'
        python -m venv $VenvDir
    }
    Info 'Instalando dependencias Python (faster-whisper, requests)...'
    & "$VenvDir\Scripts\pip.exe" install --progress-bar on --upgrade pip
    & "$VenvDir\Scripts\pip.exe" install --progress-bar on -r "$InstallDir\requirements.txt"
    $sitePackages = Join-Path $VenvDir 'Lib\site-packages'
    $cudaPaths = Install-CudaLibs -PipExe "$VenvDir\Scripts\pip.exe" -SitePackages $sitePackages
    if ($cudaPaths -is [array]) { $cudaPaths = $cudaPaths[-1] }
    Ok 'Ambiente Python pronto'

    Step '4/4' 'Ollama e comando resumir'
    $ollamaExe = Install-Ollama

    $bat = @(
        '@echo off'
        'setlocal EnableExtensions'
        'if "%~1"=="" ('
        '    echo Uso: resumir "C:\caminho\do\video.mp4"'
        '    exit /b 1'
        ')'
        'if not exist "%~1" ('
        '    echo ERRO: Arquivo nao encontrado: %~1'
        '    exit /b 1'
        ')'
        'curl -s http://localhost:11434/api/tags >nul 2>&1'
        'if errorlevel 1 ('
        "    start `"`" `"$ollamaExe`" serve"
        '    timeout /t 4 /nobreak >nul'
        ')'
    )
    if ($cudaPaths) {
        $bat += "set `"PATH=$cudaPaths;%PATH%`""
    }
    $bat += @(
        "`"$VenvDir\Scripts\python.exe`" `"$InstallDir\video_resumidor.py`" --ollama local `"%~1`""
        'exit /b %errorLevel%'
    )
    Write-ResumirBat $bat
    Add-InstallDirToPath
}

Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue

Info 'Criando atalho na area de trabalho...'
New-DesktopShortcut -TargetDir $InstallDir

Write-InstallManifest

Write-Host "`n===============================================" -ForegroundColor Green
Write-Host "  Instalacao concluida! (modo $Mode)" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor Green
