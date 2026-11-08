$ErrorActionPreference = 'Stop'
$TaskName = 'Org Core Input Services'
$ScriptPath = Join-Path $PSScriptRoot 'start-services.ps1'
$Account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    if (-not ($Existing.Actions.Arguments -contains ('-NoProfile -WindowStyle Hidden -File "' + $ScriptPath + '"'))) { throw 'A different startup task already uses this name; it was not changed.' }
    Write-Output 'Matching login startup already installed.'
    exit 0
}
$ShellPath = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$Action = New-ScheduledTaskAction -Execute $ShellPath -Argument ('-NoProfile -WindowStyle Hidden -File "' + $ScriptPath + '"') -WorkingDirectory (Split-Path $PSScriptRoot -Parent)
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Account
$Principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Starts local input, original-input backup, Discord transport and intake capture at user login. Does not launch language-model work.' | Select-Object TaskName,State
