param(
    [Parameter(Mandatory)][string]$ModelTag,
    [Parameter(Mandatory)][string]$InstallDir,
    [ValidateSet('Local', 'Docker')]
    [string]$Mode = 'Local'
)

$ErrorActionPreference = 'Stop'

$modelMap = @{
    'qwen2.5-7b-instruct'       = 'qwen2.5:7b-instruct'
    'qwen2.5-coder-7b-instruct' = 'qwen2.5-coder:7b-instruct'
    'qwen3-8b'                  = 'qwen3:8b'
    'deepseek-r1-8b'            = 'deepseek-r1:8b'
    'llama3.2-3b'               = 'llama3.2:3b'
}

if (-not $modelMap.ContainsKey($ModelTag)) {
    throw "Modelo desconhecido: $ModelTag"
}
$Model = $modelMap[$ModelTag]

function Info { param([string]$m) Write-Host ">> $m" }
function Ok   { param([string]$m) Write-Host "OK: $m" -ForegroundColor Green }

if ($Mode -eq 'Docker') {
    Info 'Baixando modelo no container Ollama...'
    Push-Location $InstallDir
    try {
        $env:OLLAMA_MODEL = $Model
        docker compose up -d ollama | Out-Host
        Start-Sleep -Seconds 5
        docker exec ollama ollama pull $Model
        if ($LASTEXITCODE -ne 0) { throw "docker pull falhou (codigo $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }
} else {
    Info 'Iniciando Ollama para baixar modelo...'
    $ollamaCandidates = @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe"
    )
    $ollamaExe = $null
    foreach ($p in $ollamaCandidates) {
        if (Test-Path $p) { $ollamaExe = $p; break }
    }
    if (-not $ollamaExe) {
        $cmd = Get-Command ollama -ErrorAction SilentlyContinue
        if ($cmd) { $ollamaExe = $cmd.Source }
    }
    if (-not $ollamaExe) {
        throw 'Ollama nao encontrado. Instale em https://ollama.com'
    }

    $running = $false
    try {
        $r = Invoke-WebRequest 'http://localhost:11434/api/tags' -TimeoutSec 3 -UseBasicParsing
        $running = ($r.StatusCode -eq 200)
    } catch {}

    if (-not $running) {
        Info 'Subindo servico Ollama...'
        Start-Process $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
        for ($i = 1; $i -le 10; $i++) {
            Start-Sleep -Seconds 2
            try {
                $r = Invoke-WebRequest 'http://localhost:11434/api/tags' -TimeoutSec 3 -UseBasicParsing
                if ($r.StatusCode -eq 200) { $running = $true; break }
            } catch {}
            Info "Aguardando Ollama... ($i/10)"
        }
        if (-not $running) {
            throw 'Ollama nao respondeu na porta 11434.'
        }
    }

    Info "Baixando modelo $Model (pode demorar varios minutos)..."
    & $ollamaExe pull $Model
    if ($LASTEXITCODE -ne 0) { throw "ollama pull falhou (codigo $LASTEXITCODE)" }
}

Ok "Modelo $Model pronto"
