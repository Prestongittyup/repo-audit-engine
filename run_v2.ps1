[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$TargetRepoPath = $null,
    [ValidateRange(10, 5000)][int]$BatchSize = 200,
    [switch]$DRY_RUN = $false,
    [string]$RunId = $null,
    [string]$AuditWorkspacePath = $null,
    [switch]$CI_MODE = $false
)

$ErrorActionPreference = "Stop"

$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeEntry = Join-Path $engineRoot 'run_runtime.ps1'
if (-not (Test-Path -LiteralPath $runtimeEntry)) {
    throw "Missing script: $runtimeEntry"
}

$result = & $runtimeEntry -TargetRepoPath $TargetRepoPath -BatchSize $BatchSize -DRY_RUN:$DRY_RUN -RunId $RunId -AuditWorkspacePath $AuditWorkspacePath -CI_MODE:$CI_MODE

if ($CI_MODE) {
    exit ([int]$result.exit_code)
}
