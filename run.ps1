[CmdletBinding()]
param(
    [ValidateSet('status', 'init', 'run-pipeline')]
    [string]$Command = 'status',
    [string]$WorkspacePath = $PSScriptRoot,
    [string]$RepoPath,
    [string[]]$Entrypoints,
    [int]$HeuristicOnlyThreshold = 0,
    [double]$DriftThreshold = 1.0,
    [string]$OutputPath,
    [switch]$DebugMode
)

$ErrorActionPreference = 'Stop'

$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeCommon = Join-Path $engineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon -PathType Leaf)) {
    throw "Missing utility module: $runtimeCommon"
}
. $runtimeCommon

$workspace = [System.IO.Path]::GetFullPath($WorkspacePath)

switch ($Command) {
    'init' {
        Ensure-Directory -Path (Join-Path $workspace 'src') | Out-Null
        Ensure-Directory -Path (Join-Path $workspace 'config') | Out-Null
        Ensure-Directory -Path (Join-Path $workspace 'output') | Out-Null
        Ensure-Directory -Path (Join-Path $workspace 'state') | Out-Null
        Write-Status "Initialized workspace at: $workspace"
        break
    }
    'status' {
        $summary = [ordered]@{
            phase = 'pipeline_only_control_plane'
            workspace = $workspace
            entrypoint = 'run.ps1'
            control_plane = 'run-pipeline'
            direct_stage_cli = 'DISABLED'
            next_action = '.\\run.ps1 run-pipeline -RepoPath <repoRoot>'
        }
        Write-Host ($summary | ConvertTo-Json -Depth 6)
        break
    }
    'run-pipeline' {
        if ([string]::IsNullOrWhiteSpace($RepoPath)) {
            throw 'RepoPath is required for run-pipeline.'
        }

        $orchestratorScript = Join-Path $engineRoot 'src\pipeline_control_plane.ps1'
        if (-not (Test-Path -LiteralPath $orchestratorScript -PathType Leaf)) {
            throw "Missing orchestrator script: $orchestratorScript"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
            $resolvedOutputPath = Join-Path $workspace ("output\runs\$timestamp")
        }

        $result = & $orchestratorScript `
            -EngineRoot $engineRoot `
            -RepoPath $RepoPath `
            -RunOutputDir $resolvedOutputPath `
            -Entrypoints $Entrypoints `
            -HeuristicOnlyThreshold $HeuristicOnlyThreshold `
            -DriftThreshold $DriftThreshold `
            -DebugMode:([bool]$DebugMode)

        Write-Host ($result | ConvertTo-Json -Depth 12)
        break
    }
}
