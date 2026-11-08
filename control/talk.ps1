<#
Talk to a deputy, or to one project's resident agent, inside its session from a terminal without colliding with the scheduler.
While the interactive session is open this window holds deputy-<role>.lock (deputy) or lane-<role>-<project>.lock
(agent), so the scheduler skips that unit (a deputy's executors keep working; an agent's deputy keeps reviewing) and
Discord commands to a deputy wait.
The conversation is recorded like any other run: /compact first if the session is already large, dashboard
status while open; on exit a runs.jsonl row (kind 'terminal', tokens, cost), last/runs, and /compact
if the session grew past the limit. Closing the window instead of /exit is fine: the scheduler records it.
An agent runs in its project's folder with the scheduler's --add-dir folders and the executor model (from `tick.py launch`).
Usage:  pwsh -File <incore>\control\talk.ps1 work
        pwsh -File <incore>\control\talk.ps1 work fos
        pwsh -File <incore>\control\talk.ps1 uvox uvox
        (work = Arbitros 线 | uvox = Beacon Listener 线 | quant | intelligence | life | coordinator; project: a key of tick.py PROJECTS, or general)
        -PrintOnly prints the claude command line and exits without taking the lock.
#>
param([Parameter(Mandatory)][ValidateSet('work', 'uvox', 'quant', 'intelligence', 'life', 'coordinator')][string]$Role,
      [string]$Project, [switch]$PrintOnly)

# This machine's paths: instance\instance.json (or the file INCORE_INSTANCE names); a key it lacks keeps today's value (control/instance_config.py)
$InstFile = if ($env:INCORE_INSTANCE) { $env:INCORE_INSTANCE } else { Join-Path $PSScriptRoot '..\instance\instance.json' }
$Inst = if (Test-Path -LiteralPath $InstFile) { Get-Content -Raw -LiteralPath $InstFile -Encoding UTF8 | ConvertFrom-Json } else { [pscustomobject]@{} }
$UserHome = if ($Inst.user_home) { $Inst.user_home } else { 'C:\orghome' }
$Workspace = if ($Inst.workspace) { $Inst.workspace } else { 'C:\orghome\orgcore_workspace' }  # sessions are filed under the directory claude runs in
$Claude = if ($Inst.claude_exe) { $Inst.claude_exe } else { "$UserHome\.local\bin\claude.exe" }
$Python = if ($Inst.python_exe) { $Inst.python_exe } else { "$UserHome\AppData\Local\Programs\Python\Python312\python.exe" }
$Tick = Join-Path $PSScriptRoot 'tick.py'
$Data = Join-Path $Workspace 'incore\control\data'
$Lock = Join-Path $Data $(if ($Project) { "lane-$Role-$Project.lock" } else { "deputy-$Role.lock" })
$Who = if ($Project) { "$Role 线的 $Project agent" } else { $Role }
$Tail = if ($Project) { @($Project) } else { @() }  # trailing tick.py argument

$deputy = (Get-Content -Raw -LiteralPath (Join-Path $Data 'deputies.json') | ConvertFrom-Json).$Role
if ($Project) {
    $info = & $Python $Tick launch $Role $Project | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { Write-Host "没有这个项目：$Project。"; exit 1 }
    $entry = $deputy.executors.$Project
    $cwd = $info.cwd
} else {
    $entry = $deputy
    $cwd = $Workspace
}
if (-not $entry -or -not $entry.session_id) {
    Write-Host $(if ($Project) { "$Who 还没有会话，第一次派活时才建立。" } else { "$Role 还没有常驻会话（第一次规划、审查或写日报时才会建立）。" })
    exit 1
}
$note = if ($Project) { "`n现在是 owner 在终端里直接跟你这个项目（$Project）的常驻 agent 对话（control/talk.ps1），不是无人值守运行；他在场，可以当场问他。" }
        else { "`n现在是 owner 在终端里直接跟你对话（control/talk.ps1），不是无人值守运行；他在场，可以当场问他。" }
$rulesFile = Join-Path $PSScriptRoot 'roles\tick-rules.md'
$privateRules = Join-Path $PSScriptRoot '..\data\config-sync\private-layer\roles\tick-rules.md'  # private layer first (control/layered.py); a custom private_layer_dir in instance.json is not read here
if (Test-Path -LiteralPath $privateRules) { $rulesFile = $privateRules }
$rules = (Get-Content -Raw -Encoding UTF8 -LiteralPath $rulesFile) + $note
$claudeArgs = @('--resume', $entry.session_id)
if ($Project) { $claudeArgs += @('--model', $info.model) }
$claudeArgs += @('--append-system-prompt', $rules)
if ($Project -and $info.add_dirs) { $claudeArgs += @('--add-dir') + @($info.add_dirs) }

if ($PrintOnly) {
    Write-Host "cwd: $cwd"
    $shown = $claudeArgs | ForEach-Object { if ($_ -eq $rules) { "`"<$rulesFile 全文 + 备注>`"" } elseif ($_ -match '\s') { "`"$_`"" } else { $_ } }
    Write-Host "$Claude $($shown -join ' ')"
    Write-Host "备注：$($note.Trim())"
    exit 0
}

$holder = (Get-Content -LiteralPath $Lock -ErrorAction SilentlyContinue) -as [int]
if ($holder -and (Get-Process -Id $holder -ErrorAction SilentlyContinue)) {
    $what = if ($entry.current.text_preview) { "：$($entry.current.text_preview)" } elseif ($entry.item_preview) { "：$($entry.item_preview)" } else { '' }
    Write-Host "$Who 正在被占用（PID $holder）$what。等它跑完再进。"
    exit 1
}
Set-Content -LiteralPath $Lock -Value $PID -NoNewline
$started = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
try {
    Write-Host '检查会话大小（上下文过长时先压缩，可能要一两分钟）…'
    & $Python $Tick terminal $Role begin $started @Tail
    Push-Location $cwd
    & $Claude @claudeArgs
} finally {
    Pop-Location
    Write-Host '正在记录这次对话（上下文过长时会顺便压缩，可能要一两分钟）…'
    & $Python $Tick terminal $Role end $started @Tail  # still holding the lock, so compaction cannot collide
    if ((Get-Content -LiteralPath $Lock -ErrorAction SilentlyContinue) -eq "$PID") { Remove-Item -LiteralPath $Lock }
}
