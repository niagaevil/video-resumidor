param(
    [string]$SourceDir,
    [switch]$RemoveFiles,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$ManifestName = 'video-resumidor.manifest.json'

function Info { param([string]$m) Write-Host ">> $m" }
function Ok   { param([string]$m) Write-Host "OK: $m" -ForegroundColor Green }
function Warn { param([string]$m) Write-Host "AVISO: $m" -ForegroundColor Yellow }

function Normalize-PathString {
    param([string]$Path)
    if (-not $Path) { return $null }
    try { return [System.IO.Path]::GetFullPath($Path.TrimEnd('\')) }
    catch { return $Path.TrimEnd('\') }
}

function Remove-DirFromUserPath {
    param([string]$Dir)
    $Dir = Normalize-PathString $Dir
    if (-not $Dir) { return $false }

    $cur = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $cur) { return $false }

    $kept = @()
    $removed = $false
    foreach ($part in $cur -split ';') {
        if (-not $part) { continue }
        $norm = Normalize-PathString $part
        if ($norm -eq $Dir) {
            $removed = $true
            continue
        }
        $kept += $part
    }

    if ($removed) {
        $newPath = ($kept -join ';').Trim(';')
        [System.Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Ok "Removido do PATH do usuario: $Dir"
    }
    return $removed
}

function Find-Manifests {
    param([string[]]$SearchDirs)
    $found = @()
    foreach ($dir in $SearchDirs) {
        if (-not $dir) { continue }
        $path = Join-Path $dir $ManifestName
        if (Test-Path $path) {
            $found += $path
        }
    }
    return $found | Select-Object -Unique
}

function Read-Manifest {
    param([string]$Path)
    try {
        return Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Warn "Manifesto invalido: $Path"
        return $null
    }
}

function Remove-DesktopShortcut {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desktop 'Video Resumidor.lnk'
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Ok "Atalho removido: $lnk"
        return $true
    }
    return $false
}

function Remove-InstallArtifacts {
    param($Manifest)

    $installDir = Normalize-PathString $Manifest.installDir
    if (-not $installDir -or -not (Test-Path $installDir)) {
        Warn "Pasta de instalacao nao encontrada: $installDir"
        return
    }

    if (-not $Manifest.installInPlace) {
        Info "Removendo pasta de instalacao: $installDir"
        Remove-Item $installDir -Recurse -Force
        Ok "Pasta removida"
        return
    }

    Info "Removendo artefatos gerados em: $installDir"
    $toRemove = @(
        'venv',
        'resumir.bat',
        'temp',
        'videos',
        $ManifestName
    )
    if ($Manifest.portableFfmpeg) {
        $toRemove += 'ffmpeg.exe', 'ffprobe.exe'
    }

    foreach ($name in $toRemove) {
        $target = Join-Path $installDir $name
        if (Test-Path $target) {
            Remove-Item $target -Recurse -Force
            Ok "Removido: $name"
        }
    }
}

if (-not $SourceDir) {
    $SourceDir = $PSScriptRoot
}
$SourceDir = Normalize-PathString $SourceDir
$userInstall = Normalize-PathString (Join-Path $env:USERPROFILE 'video-resumidor')

$manifestPaths = Find-Manifests @($SourceDir, $userInstall)
$manifests = @()
foreach ($mp in $manifestPaths) {
    $m = Read-Manifest $mp
    if ($m) { $manifests += $m }
}

if ($manifests.Count -eq 0) {
    Info 'Manifesto nao encontrado; tentando detectar instalacao antiga...'
    $legacyPaths = @()
    foreach ($dir in @($SourceDir, $userInstall)) {
        if (-not $dir) { continue }
        $resumir = Join-Path $dir 'resumir.bat'
        if (Test-Path $resumir) {
            $norm = Normalize-PathString $dir
            $legacyPaths += $norm
            $manifests += [pscustomobject]@{
                version          = 0
                installDir       = $norm
                sourceDir        = $SourceDir
                installInPlace   = ($norm -eq $SourceDir)
                mode             = 'Local'
                pathEntriesAdded = @($norm)
                portableFfmpeg   = (Test-Path (Join-Path $dir 'ffmpeg.exe'))
            }
        }
    }
    $manifests = $manifests | Select-Object -Unique -Property installDir, sourceDir, installInPlace, mode, pathEntriesAdded, portableFfmpeg, version
}

if ($manifests.Count -eq 0) {
    throw @"
Nenhuma instalacao encontrada.
Procurei manifesto em:
  $SourceDir
  $userInstall

Rode o instalador primeiro ou remova manualmente entradas do PATH do usuario.
"@
}

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host "  Video Resumidor - Desinstalacao" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

foreach ($m in $manifests) {
    $place = if ($m.installInPlace) { 'nesta pasta' } else { 'pasta do usuario' }
    Write-Host "  - $($m.installDir)  ($place, modo $($m.mode))" -ForegroundColor Yellow
}
Write-Host ""

if (-not $Quiet -and -not $RemoveFiles) {
    $ans = Read-Host "Remover tambem arquivos/venv da instalacao? [s/N]"
    if ($ans -match '^(s|sim|y|yes)$') {
        $RemoveFiles = $true
    }
}

$pathsRemoved = 0
foreach ($m in $manifests) {
    foreach ($entry in @($m.pathEntriesAdded)) {
        if (Remove-DirFromUserPath $entry) { $pathsRemoved++ }
    }
}

# Limpar entradas antigas no PATH mesmo sem manifesto completo
$knownDirs = @($SourceDir, $userInstall)
foreach ($dir in $manifests | ForEach-Object { $_.installDir }) {
    if ($dir) { $knownDirs += $dir }
}
foreach ($dir in $knownDirs | Select-Object -Unique) {
    if (Remove-DirFromUserPath $dir) { $pathsRemoved++ }
}

if ($pathsRemoved -eq 0) {
    Warn 'Nenhuma entrada do PATH foi removida (talvez ja tenham sido limpas).'
}

Remove-DesktopShortcut | Out-Null

if ($RemoveFiles) {
    foreach ($m in $manifests) {
        Remove-InstallArtifacts $m
    }
} else {
    Info 'Arquivos mantidos. So PATH e atalho foram tratados.'
}

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "  Desinstalacao concluida!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Feche e abra o terminal para o PATH atualizar." -ForegroundColor Yellow
Write-Host ""
