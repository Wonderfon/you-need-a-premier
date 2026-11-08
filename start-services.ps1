param([switch]$StatusOnly, [ValidateSet('input','backup','discord')][string]$Only)
$ErrorActionPreference = 'Stop'
$IncubatorRoot = $PSScriptRoot
$WorkspaceRoot = Split-Path $IncubatorRoot -Parent
$PythonPath = Join-Path $IncubatorRoot 'discord/.venv/Scripts/python.exe'
$Services = @(
    @{ Name = 'input'; Args = @('-u','-m','incore.input.server'); Match = 'incore.input.server'; State = 'input/data/server-process.json'; Logs = 'input/data' },
    @{ Name = 'backup'; Args = @('-u','incore/backup/backup.py','--watch','--drive','--interval','60'); Match = 'incore/backup/backup.py'; State = 'data/backups/watcher-process.json'; Logs = 'data/backups' },
    @{ Name = 'discord'; Args = @('-u','incore/discord/gateway.py'); Match = 'incore/discord/gateway.py'; State = 'discord/data/gateway-process.json'; Logs = 'discord/data' }
)
# The legacy Codex intake watcher is intentionally not auto-started (2026-09-30 coordinator takeover).
$Mutex = [Threading.Mutex]::new($false, 'Local\Demo01IncubatorServiceStart')
if (-not $Mutex.WaitOne(0)) { exit 0 }
try {
    foreach ($Service in $Services) {
        if ($Only -and $Service.Name -ne $Only) { continue }
        $StatePath = Join-Path $IncubatorRoot $Service.State
        $Running = $null
        if (Test-Path -LiteralPath $StatePath) {
            $Saved = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
            $SavedPid = if ($Saved.ProcessId) { $Saved.ProcessId } else { $Saved.pid }
            if ($SavedPid) {
                $Candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$SavedPid" -ErrorAction Stop
                if (-not $Candidate -and (Get-Process -Id $SavedPid -ErrorAction SilentlyContinue)) { throw "Cannot verify existing $($Service.Name) process; refusing duplicate start." }
                if ($Candidate -and $Candidate.ExecutablePath -eq $PythonPath.Replace('/', '\') -and $Candidate.CommandLine.Contains($Service.Match)) { $Running = $Candidate }
                # input only: the server records its own pid, the child of the venv launcher (its exe is the base python)
                elseif ($Candidate -and $Service.Name -eq 'input' -and $Candidate.CommandLine.Contains($Service.Match) -and (Get-CimInstance Win32_Process -Filter "ProcessId=$($Candidate.ParentProcessId)" -ErrorAction SilentlyContinue).ExecutablePath -eq $PythonPath.Replace('/', '\')) { $Running = $Candidate }
            }
        }
        if ($Running) { Write-Output "$($Service.Name): running pid=$($Running.ProcessId)"; continue }
        if ($StatusOnly) { Write-Output "$($Service.Name): stopped"; continue }
        if ($Service.Name -eq 'backup' -and (Test-Path -LiteralPath (Join-Path $IncubatorRoot 'data/backups/STOP'))) { Write-Output 'backup: STOP file set'; continue }
        $LogRoot = Join-Path $IncubatorRoot $Service.Logs
        New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
        $Child = Start-Process -FilePath $PythonPath -ArgumentList $Service.Args -WorkingDirectory $WorkspaceRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "$($Service.Name).stdout.log") -RedirectStandardError (Join-Path $LogRoot "$($Service.Name).stderr.log") -PassThru
        if ($Service.Name -eq 'backup') { Start-Sleep -Seconds 3; if ($Child.HasExited) { Write-Output 'backup: exited at start (another watcher is running); state file kept'; continue } }
        @{ProcessId=$Child.Id; StartedAtUtc=[DateTime]::UtcNow.ToString('o'); Component=$Service.Name; CallsSubscriptionModels=($Service.Name -eq 'discord'); AutoStartup=$true} | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding utf8
        Write-Output "$($Service.Name): started pid=$($Child.Id)"
    }
} finally {
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
