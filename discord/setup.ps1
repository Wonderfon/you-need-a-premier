param(
    [switch]$SetToken,
    [switch]$InstallDependencies,
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) { throw "Python not found: $Python" }
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
}
if ($InstallDependencies) {
    Write-Host 'Installing pinned Discord SDK dependencies. This downloads packages from PyPI.'
    & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
if ($SetToken) {
    $Secrets = Join-Path $Root '.secrets'
    New-Item -ItemType Directory -Path $Secrets -Force | Out-Null
    $Token = Read-Host 'Discord bot token (input hidden)' -AsSecureString
    if ($Token.Length -eq 0) { throw 'Token was empty; no secret file written.' }
    $Token | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $Secrets 'bot-token.dpapi') -NoNewline
    Write-Host 'Token encrypted for this Windows user.'
}
Write-Host "Setup files are in $Root"
if (-not $InstallDependencies) { Write-Host 'SDK not installed. Coordinate with the workspace coordinator before using -InstallDependencies.' }
