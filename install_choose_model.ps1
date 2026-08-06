param(
    [Parameter(Mandatory)][string]$InstallDir,
    [ValidateSet('Local', 'Docker')]
    [string]$Mode = 'Local'
)

$ErrorActionPreference = 'Stop'

$modelMap = [ordered]@{
    '1' = @{ Tag = 'qwen2.5-7b-instruct';       Label = 'qwen2.5:7b-instruct       (~4.7 GB, recomendado GTX 1650)' }
    '2' = @{ Tag = 'qwen2.5-coder-7b-instruct'; Label = 'qwen2.5-coder:7b-instruct (~4.7 GB, codigo e tecnico)' }
    '3' = @{ Tag = 'qwen3-8b';                  Label = 'qwen3:8b                  (~5.5 GB, 8B, apertado na 1650)' }
    '4' = @{ Tag = 'deepseek-r1-8b';            Label = 'deepseek-r1:8b            (~5.0 GB, raciocinio)' }
    '5' = @{ Tag = 'llama3.2-3b';               Label = 'llama3.2:3b               (~2.0 GB, leve e rapido)' }
}

Write-Host ''
Write-Host '=============================================='
Write-Host ' Qual modelo LLM deseja baixar?'
Write-Host '=============================================='
Write-Host ''
foreach ($key in $modelMap.Keys) {
    Write-Host " $key) $($modelMap[$key].Label)"
}
Write-Host ' 6) Pular (ja tenho modelo)'
Write-Host ''
$choice = Read-Host 'Escolha [1-6]'

if ($choice -eq '6' -or -not $choice) {
    Write-Host 'Pulando download do modelo.'
    exit 0
}

if (-not $modelMap.Contains($choice)) {
    Write-Warning "Opcao invalida ($choice). Usando qwen2.5:7b-instruct."
    $choice = '1'
}

$tag = $modelMap[$choice].Tag
$pullScript = Join-Path $PSScriptRoot 'install_pull_model.ps1'
if (-not (Test-Path $pullScript)) {
    throw "Script nao encontrado: $pullScript"
}

& $pullScript -ModelTag $tag -InstallDir $InstallDir -Mode $Mode
