param(
    [string]$InstallDir
)

function Get-UserDesktopPath {
    $candidates = @()

    if ($env:OneDrive) {
        $candidates += Join-Path $env:OneDrive 'Desktop'
    }
    if ($env:USERPROFILE) {
        $candidates += Join-Path $env:USERPROFILE 'Desktop'
    }

    try {
        $candidates += [Environment]::GetFolderPath('Desktop')
    } catch {}

    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) {
            return $path
        }
    }

    throw 'Nao foi possivel localizar a pasta Desktop do usuario.'
}

if (-not $InstallDir) {
    $InstallDir = $PSScriptRoot
    $installed = Join-Path $env:USERPROFILE 'video-resumidor'
    $installedBat = Join-Path $installed 'abrir_interface.bat'
    $installedUi = Join-Path $installed 'interface_web.py'
    $localUi = Join-Path $PSScriptRoot 'interface_web.py'
    if ((Test-Path $installedBat) -and (Test-Path $installedUi) -and (Test-Path (Join-Path $installed 'venv')) -and -not (Test-Path $localUi)) {
        $InstallDir = $installed
    }
}

$bat = Join-Path $InstallDir 'abrir_interface.bat'
if (-not (Test-Path $bat)) {
    throw "Nao encontrado: $bat"
}

$desktop = Get-UserDesktopPath
$lnk = Join-Path $desktop 'Video Resumidor.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $bat
$shortcut.WorkingDirectory = $InstallDir
$shortcut.Description = 'Transcrever e resumir reunioes em video'
$shortcut.IconLocation = 'imageres.dll,184'
$shortcut.Save()

Write-Host ''
Write-Host 'Atalho criado na area de trabalho:' -ForegroundColor Green
Write-Host "  $lnk"
Write-Host ''
