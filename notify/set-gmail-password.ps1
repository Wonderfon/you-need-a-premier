$ErrorActionPreference = 'Stop'
# Owner runs this once. The App Password is typed into a hidden prompt and stored DPAPI-encrypted
# for the current Windows user only; it never appears on screen, in logs or in Git.
$Dir = Join-Path $PSScriptRoot '.secrets'
New-Item -ItemType Directory -Path $Dir -Force | Out-Null
$Secure = Read-Host -AsSecureString 'Gmail App Password for the sender address (sender_email in instance/instance.json) (16 characters, spaces are ignored)'
$Secure | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $Dir 'gmail-app-password.dpapi') -Encoding ascii
Write-Output 'Saved (DPAPI, current Windows user only).'
