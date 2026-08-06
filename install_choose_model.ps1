param(
    [Parameter(Mandatory)][string]$InstallDir,
    [ValidateSet('Local', 'Docker')]
    [string]$Mode = 'Local'
)

$ErrorActionPreference = 'Stop'

# -- Deteccao de hardware (usa hardware_detect.py do projeto) ----------------
# Nota: usar apenas aspas simples no codigo Python. Aspas duplas sao
# destruidas pelo PowerShell ao passar argumentos para programas nativos.
$pyProbe = @'
import sys, json
sys.path.insert(0, sys.argv[1])
import hardware_detect as h
try:
    info = h.detect_tier()
    gpus = info.get('gpus') or []
    print(json.dumps({
        'gpu_name': gpus[0].get('name') if gpus else '',
        'vram_gb': info.get('total_vram_gb') or 0,
        'ram_gb': info.get('ram_gb') or 0,
    }, ensure_ascii=False))
except Exception as e:
    print(json.dumps({'error': str(e)}))
'@

function Get-HardwareInfo {
    param([string]$PyDir)
    foreach ($py in @('python', 'py')) {
        $cmd = Get-Command $py -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $out = (& $py -c $pyProbe $PyDir 2>$null) | Out-String
            $obj = $out | ConvertFrom-Json
            if ($obj -and -not $obj.error) { return $obj }
        } catch {}
    }
    return $null
}

# hardware_detect.py e copiado para a pasta de instalacao pelo instalador.
$hwDir = $InstallDir
if (-not (Test-Path (Join-Path $hwDir 'hardware_detect.py'))) {
    $hwDir = $PSScriptRoot
}
$hw = $null
if (Test-Path (Join-Path $hwDir 'hardware_detect.py')) {
    $hw = Get-HardwareInfo -PyDir $hwDir
}

# -- Recomendacao por VRAM/RAM -----------------------------------------------
$vram = 0.0
$ram  = 0
if ($hw) {
    $vram = [double]($hw.vram_gb)
    $ram  = [int]($hw.ram_gb)
}
# 8B cabe inteiro na GPU com 8GB+ VRAM; 7B com 4GB+ (ou 16GB+ RAM); senao 3B.
if ($vram -ge 8)     { $recommended = '3'; $why = '8B cabe inteiro na sua VRAM' }
elseif ($vram -ge 4) { $recommended = '1'; $why = 'melhor equilibrio para sua VRAM' }
elseif ($ram -ge 16) { $recommended = '1'; $why = 'sem VRAM suficiente, 7B roda via CPU/RAM' }
else                 { $recommended = '5'; $why = 'modelo leve, roda via CPU' }

$modelMap = [ordered]@{
    '1' = @{ Tag = 'qwen2.5-7b-instruct';       Label = 'qwen2.5:7b-instruct       (~4.7 GB, geral)' }
    '2' = @{ Tag = 'qwen2.5-coder-7b-instruct'; Label = 'qwen2.5-coder:7b-instruct (~4.7 GB, codigo e tecnico)' }
    '3' = @{ Tag = 'qwen3-8b';                  Label = 'qwen3:8b                  (~5.5 GB, 8B, melhor qualidade)' }
    '4' = @{ Tag = 'deepseek-r1-8b';            Label = 'deepseek-r1:8b            (~5.0 GB, raciocinio, mais lento)' }
    '5' = @{ Tag = 'llama3.2-3b';               Label = 'llama3.2:3b               (~2.0 GB, leve e rapido)' }
}

Write-Host ''
Write-Host '=============================================='
Write-Host ' Qual modelo LLM deseja baixar?'
Write-Host '=============================================='
Write-Host ''
if ($hw) {
    if ($hw.gpu_name) {
        Write-Host (" GPU detectada: {0} ({1} GB VRAM, {2} GB RAM)" -f $hw.gpu_name, $vram, $ram) -ForegroundColor Cyan
    } else {
        Write-Host (" GPU nao detectada (RAM: {0} GB) - modelo recomendado roda via CPU" -f $ram) -ForegroundColor Cyan
    }
} else {
    Write-Host ' Hardware nao detectado - usando recomendacao generica.' -ForegroundColor DarkGray
}
Write-Host ''
$recName = ($modelMap[$recommended].Label -split '\(')[0].Trim()
Write-Host (" Recomendado para o seu PC: opcao {0} ({1}) - {2}" -f $recommended, $recName, $why) -ForegroundColor Green
Write-Host ''
foreach ($key in $modelMap.Keys) {
    $marker = if ($key -eq $recommended) { '   <== recomendado' } else { '' }
    Write-Host " $key) $($modelMap[$key].Label)$marker"
}
Write-Host ' 6) Pular (ja tenho modelo)'
Write-Host ''
$choice = Read-Host 'Escolha [1-6]'

if ($choice -eq '6' -or -not $choice) {
    Write-Host 'Pulando download do modelo.'
    exit 0
}

if (-not $modelMap.Contains($choice)) {
    Write-Warning "Opcao invalida ($choice). Usando modelo recomendado."
    $choice = $recommended
}

$tag = $modelMap[$choice].Tag
$pullScript = Join-Path $PSScriptRoot 'install_pull_model.ps1'
if (-not (Test-Path $pullScript)) {
    throw "Script nao encontrado: $pullScript"
}

& $pullScript -ModelTag $tag -InstallDir $InstallDir -Mode $Mode
