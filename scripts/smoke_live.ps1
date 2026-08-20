param(
    [switch]$DeepSeekOnly,
    [switch]$SendFeishuTest
)

$ErrorActionPreference = 'Stop'
if ($DeepSeekOnly -eq $SendFeishuTest) {
    throw 'Choose exactly one of -DeepSeekOnly or -SendFeishuTest.'
}

$WorkspaceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$ProjectPython = Join-Path $WorkspaceRoot '.venv\Scripts\python.exe'
$SmokeScript = Join-Path $WorkspaceRoot 'scripts\live_smoke.py'
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { 'C:\Users\Joe\AppData\Local\hermes' }

foreach ($required in @($ProjectPython, $SmokeScript, $HermesHome)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path is missing: $required"
    }
}

$Mode = if ($DeepSeekOnly) { '--deepseek-only' } else { '--send-feishu-test' }
& $ProjectPython $SmokeScript $Mode --hermes-home $HermesHome
exit $LASTEXITCODE
