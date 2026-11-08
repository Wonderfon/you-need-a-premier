param(
    [switch]$Send,
    [string]$Channel,
    [string]$Message,
    [switch]$Test
)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run setup.ps1 first to create the project virtual environment.' }
if ($Test) {
    & $Python (Join-Path $Root 'test_transport.py')
    exit $LASTEXITCODE
}
if ($Send) {
    if ([string]::IsNullOrWhiteSpace($Channel) -or [string]::IsNullOrEmpty($Message)) { throw 'Send requires -Channel and -Message.' }
    & $Python (Join-Path $Root 'gateway.py') send --channel $Channel --message $Message
    exit $LASTEXITCODE
}
& $Python (Join-Path $Root 'gateway.py')
exit $LASTEXITCODE
