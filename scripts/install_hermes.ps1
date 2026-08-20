param(
    [switch]$WhatIf,
    [string]$WorkspaceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$HermesHome = $env:HERMES_HOME
)

$ErrorActionPreference = 'Stop'
$JobName = 'JNBY Daily Intelligence'
$Schedule = '0 8 * * *'
$WrapperName = 'jnby-news-watch.py'
$ManagedMarker = '# managed-by: jnby-news-watch'

if (-not $HermesHome) {
    $HermesHome = 'C:\Users\Joe\AppData\Local\hermes'
}

$WorkspacePath = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
$HermesPath = (Resolve-Path -LiteralPath $HermesHome).Path
if (-not [System.IO.Path]::IsPathRooted($WorkspacePath) -or -not [System.IO.Path]::IsPathRooted($HermesPath)) {
    throw 'WorkspaceRoot and HermesHome must resolve to absolute paths.'
}

$SkillSource = Join-Path $WorkspacePath 'skills\jnby-news-watch'
$ProjectPython = Join-Path $WorkspacePath '.venv\Scripts\python.exe'
$ScheduledScript = Join-Path $SkillSource 'scripts\run_scheduled.py'
$PlanScript = Join-Path $SkillSource 'scripts\jnby_news_watch\install_plan.py'
$JobsPath = Join-Path $HermesPath 'cron\jobs.json'
$SkillsDir = Join-Path $HermesPath 'skills'
$ScriptsDir = Join-Path $HermesPath 'scripts'
$SkillDestination = Join-Path $SkillsDir 'jnby-news-watch'
$WrapperDestination = Join-Path $ScriptsDir $WrapperName
$RuntimeHome = Join-Path $HermesPath 'runtime\jnby-news-watch'

foreach ($required in @($SkillSource, $ProjectPython, $ScheduledScript, $PlanScript, $JobsPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path is missing: $required"
    }
}

$HermesCommand = Get-Command hermes -ErrorAction Stop
$HermesExecutable = $HermesCommand.Source

$WrapperContent = @"
$ManagedMarker
from __future__ import annotations

import subprocess
import sys

# Hermes 0.18's Windows cron parent decodes child pipes with the system GBK code page.
# Keep the project subprocess UTF-8, then transcode only this final boundary.
sys.stdout.reconfigure(encoding="gbk", errors="replace")
sys.stderr.reconfigure(encoding="gbk", errors="replace")

COMMAND = [
    r"$ProjectPython",
    r"$ScheduledScript",
    "--home",
    r"$RuntimeHome",
    "--hermes-home",
    r"$HermesPath",
]

completed = subprocess.run(
    COMMAND,
    cwd=r"$WorkspacePath",
    text=True,
    capture_output=True,
    encoding="utf-8",
    errors="replace",
)
if completed.stderr:
    print(completed.stderr, file=sys.stderr, end="")
if completed.stdout:
    print(completed.stdout, end="")
raise SystemExit(completed.returncode)
"@

$LinkAction = 'create-junction'
if (Test-Path -LiteralPath $SkillDestination) {
    $existing = Get-Item -LiteralPath $SkillDestination -Force
    $target = @($existing.Target) | Select-Object -First 1
    if ($existing.LinkType -ne 'Junction' -or -not $target) {
        throw "Refusing to overwrite unrelated Hermes skill: $SkillDestination"
    }
    $resolvedTarget = (Resolve-Path -LiteralPath $target).Path
    if ($resolvedTarget -ne (Resolve-Path -LiteralPath $SkillSource).Path) {
        throw "Refusing to repoint unrelated Hermes skill junction: $SkillDestination"
    }
    $LinkAction = 'noop'
}

$WrapperAction = 'create'
if (Test-Path -LiteralPath $WrapperDestination) {
    $existingWrapper = Get-Content -Raw -Encoding UTF8 -LiteralPath $WrapperDestination
    if (-not $existingWrapper.StartsWith($ManagedMarker)) {
        throw "Refusing to overwrite unrelated Hermes wrapper: $WrapperDestination"
    }
    $WrapperAction = if ($existingWrapper.TrimEnd() -eq $WrapperContent.TrimEnd()) { 'noop' } else { 'update' }
}

$PlanJson = & $ProjectPython $PlanScript --jobs $JobsPath --workdir $WorkspacePath
if ($LASTEXITCODE -ne 0) {
    throw 'Cron reconciliation plan failed.'
}
$CronPlan = $PlanJson | ConvertFrom-Json

[pscustomobject]@{
    WhatIf = [bool]$WhatIf
    SkillLink = $LinkAction
    Wrapper = $WrapperAction
    Cron = $CronPlan.action
    CronChanges = @($CronPlan.changes)
    JobName = $JobName
    Schedule = $Schedule
    Delivery = 'feishu (configured home channel; identifier redacted)'
} | ConvertTo-Json -Depth 4

if ($WhatIf) {
    exit 0
}

foreach ($directory in @($SkillsDir, $ScriptsDir, (Split-Path -Parent $RuntimeHome))) {
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory | Out-Null
    }
}

if ($LinkAction -eq 'create-junction') {
    New-Item -ItemType Junction -Path $SkillDestination -Target $SkillSource | Out-Null
}

if ($WrapperAction -in @('create', 'update')) {
    Set-Content -LiteralPath $WrapperDestination -Value $WrapperContent -Encoding UTF8
}

if ($CronPlan.action -eq 'create') {
    & $HermesExecutable cron create $Schedule --name $JobName --deliver feishu --script $WrapperName --no-agent --workdir $WorkspacePath
    if ($LASTEXITCODE -ne 0) { throw 'Hermes cron create failed.' }
} elseif ($CronPlan.action -eq 'update') {
    & $HermesExecutable cron edit $CronPlan.job_id --schedule $Schedule --name $JobName --deliver feishu --script $WrapperName --no-agent --workdir $WorkspacePath
    if ($LASTEXITCODE -ne 0) { throw 'Hermes cron edit failed.' }
    if (@($CronPlan.changes) -contains 'enabled') {
        & $HermesExecutable cron resume $CronPlan.job_id
        if ($LASTEXITCODE -ne 0) { throw 'Hermes cron resume failed.' }
    }
}

$VerifyJson = & $ProjectPython $PlanScript --jobs $JobsPath --workdir $WorkspacePath
if ($LASTEXITCODE -ne 0) { throw 'Post-install cron verification failed.' }
$Verify = $VerifyJson | ConvertFrom-Json
if ($Verify.action -ne 'noop') {
    throw "Post-install cron verification is not clean: $($Verify.action)"
}

Write-Output 'Hermes installation verified: exactly one managed JNBY cron is configured.'
