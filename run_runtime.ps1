[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$TargetRepoPath = $null,
    [ValidateRange(10, 5000)][int]$BatchSize = 200,
    [switch]$DRY_RUN = $false,
    [string]$RunId = $null,
    [string]$AuditWorkspacePath = $null,
    [switch]$CI_MODE = $false,
    [ValidateSet('static', 'semantic', 'full')][string]$Mode = 'static'
)

$ErrorActionPreference = 'Stop'

$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeCommon = Join-Path $engineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon)) {
    throw "Missing script: $runtimeCommon"
}
. $runtimeCommon
Set-AuditCiMode -Enabled ([bool]$CI_MODE)
Set-LLMMode -Mode $Mode

if ([string]::IsNullOrWhiteSpace($TargetRepoPath)) {
    throw "ERROR: Target repo path required. Usage: .\run.ps1 'C:\path\to\repo' [-BatchSize 10] [-DRY_RUN] [-RunId my_run] [-AuditWorkspacePath C:\audit_workspace] [-CI_MODE]"
}

$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path
if (-not (Test-Path -LiteralPath $TargetRepoPath -PathType Container)) {
    throw "Target repo not found: $TargetRepoPath"
}

$engineVersion = Get-AuditEngineVersion
$schemaVersion = Get-AuditSchemaVersion
$gitCommit = Get-GitCommitHash -RepoPath $TargetRepoPath
$configHash = Get-ConfigHash -ConfigRoot (Join-Path $engineRoot 'config')
$repoId = Get-RepoId -RepoPath $TargetRepoPath

if ([string]::IsNullOrWhiteSpace($AuditWorkspacePath)) {
    if (-not [string]::IsNullOrWhiteSpace($env:REPO_AUDIT_WORKSPACE)) {
        $AuditWorkspacePath = $env:REPO_AUDIT_WORKSPACE
    }
    else {
        $AuditWorkspacePath = Join-Path ([System.IO.Path]::GetTempPath()) 'repo_audit_runtime\audit_workspace'
    }
}

$AUDIT_WORKSPACE = [System.IO.Path]::GetFullPath($AuditWorkspacePath)
$runtimeRoot = Split-Path -Parent $AUDIT_WORKSPACE
if ([string]::IsNullOrWhiteSpace($runtimeRoot)) {
    throw "Could not determine runtime root from audit workspace path: $AUDIT_WORKSPACE"
}

$ENGINE_STATE_ROOT = Join-Path $runtimeRoot 'engine_state'
$ENGINE_STATE_DIR = Join-Path $ENGINE_STATE_ROOT $repoId
$repoRunRoot = Join-Path $AUDIT_WORKSPACE $repoId

$runIdSource = 'provided'
if ([string]::IsNullOrWhiteSpace($RunId)) {
    $RunId = Get-DeterministicRunId -RepoPath $TargetRepoPath -GitCommit $gitCommit -EngineVersion $engineVersion -ConfigHash $configHash
    $runIdSource = 'deterministic'
}
elseif (-not $RunId.StartsWith('run_')) {
    $RunId = 'run_{0}' -f $RunId
}

$RUN_OUTPUT_DIR = Join-Path $repoRunRoot $RunId
New-Item -ItemType Directory -Path $repoRunRoot -Force | Out-Null
New-Item -ItemType Directory -Path $RUN_OUTPUT_DIR -Force | Out-Null
New-Item -ItemType Directory -Path $ENGINE_STATE_DIR -Force | Out-Null

$startTime = (Get-Date).ToUniversalTime().ToString('o')
$runMetadataPath = Join-Path $RUN_OUTPUT_DIR 'run_metadata.json'
$runMetadata = [ordered]@{
    run_id = $RunId
    repo_id = $repoId
    run_id_source = $runIdSource
    start_time = $startTime
    end_time = $null
    engine_version = $engineVersion
    schema_version = $schemaVersion
    repo_path = $TargetRepoPath
    git_commit = $gitCommit
    config_hash = $configHash
    file_count = $null
    scan_mode = $null
    ci_mode = [bool]$CI_MODE
}
Write-JsonFile -Path $runMetadataPath -Data $runMetadata -Depth 8

$latestRunPath = Join-Path $ENGINE_STATE_DIR 'latest_run.json'
Write-JsonFile -Path $latestRunPath -Data ([ordered]@{
    repo_id = $repoId
    run_id = $RunId
    run_dir = $RUN_OUTPUT_DIR
    updated_at = $startTime
    engine_version = $engineVersion
    schema_version = $schemaVersion
}) -Depth 6

$indexPath = Join-Path $RUN_OUTPUT_DIR 'index.json'
$manifestPath = Join-Path $RUN_OUTPUT_DIR 'manifest.json'
$auditLogPath = Join-Path $RUN_OUTPUT_DIR 'audit_log.jsonl'
$configDepsPath = Join-Path $RUN_OUTPUT_DIR 'config_dependencies.json'
$closurePath = Join-Path $RUN_OUTPUT_DIR 'dependency_closure.json'
$truthGraphPath = Join-Path $RUN_OUTPUT_DIR 'dependency_truth_graph.json'
$architecturePath = Join-Path $RUN_OUTPUT_DIR 'architecture_analysis.json'
$deadCodePath = Join-Path $RUN_OUTPUT_DIR 'dead_code_report.json'
$healthPath = Join-Path $RUN_OUTPUT_DIR 'system_health_score.json'
$contradictionsPath = Join-Path $RUN_OUTPUT_DIR 'contradictions.json'
$auditExplanationsPath = Join-Path $RUN_OUTPUT_DIR 'audit_explanations.json'
$reportPath = Join-Path $RUN_OUTPUT_DIR 'final_report.md'
$dryRunReportPath = Join-Path $RUN_OUTPUT_DIR 'dry_run_report.md'
$analysisCacheDir = Join-Path $ENGINE_STATE_DIR 'file_analysis'

Write-Status 'Repository Audit Engine - Production Runtime'
Write-Status "Engine version: $engineVersion"
Write-Status "Schema version: $schemaVersion"
Write-Status "Target repo: $TargetRepoPath"
Write-Status "Audit workspace: $AUDIT_WORKSPACE"
Write-Status "Repo id: $repoId"
Write-Status "Run id: $RunId"
Write-Status "Run directory: $RUN_OUTPUT_DIR"
Write-Status "Engine state: $ENGINE_STATE_DIR"
Write-Status ''

$indexScript = Join-Path $engineRoot 'src\00_codebase_indexer.ps1'
$batchScript = Join-Path $engineRoot 'src\02_batch_runner_v3.ps1'
$configScript = Join-Path $engineRoot 'src\02_5_config_truth_builder.ps1'
$analyzerScript = Join-Path $engineRoot 'src\03_truth_engine.ps1'

foreach ($scriptPath in @($indexScript, $batchScript, $configScript, $analyzerScript)) {
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        throw "Missing script: $scriptPath"
    }
}

Write-Status '[1/4] Incremental Codebase Indexing'
& $indexScript -EngineRoot $engineRoot -TargetRepoPath $TargetRepoPath -RunOutputDir $RUN_OUTPUT_DIR -EngineStateDir $ENGINE_STATE_DIR -RunMetadataPath $runMetadataPath -IndexPath $indexPath -ManifestPath $manifestPath -CI_MODE:$CI_MODE

Write-Status '[2/4] Incremental File Analysis'
& $batchScript -EngineRoot $engineRoot -TargetRepoPath $TargetRepoPath -RunOutputDir $RUN_OUTPUT_DIR -EngineStateDir $ENGINE_STATE_DIR -RunMetadataPath $runMetadataPath -IndexPath $indexPath -AuditLogPath $auditLogPath -AnalysisCacheDir $analysisCacheDir -BatchSize $BatchSize -CI_MODE:$CI_MODE

Write-Status '[3/4] Config And Registry Truth Extraction'
& $configScript -EngineRoot $engineRoot -TargetRepoPath $TargetRepoPath -RunOutputDir $RUN_OUTPUT_DIR -EngineStateDir $ENGINE_STATE_DIR -RunMetadataPath $runMetadataPath -IndexPath $indexPath -ConfigDepsPath $configDepsPath -CI_MODE:$CI_MODE

Write-Status '[4/4] Truth Graph, Architecture, Health, And Audit Outputs'
& $analyzerScript -EngineRoot $engineRoot -TargetRepoPath $TargetRepoPath -RunOutputDir $RUN_OUTPUT_DIR -EngineStateDir $ENGINE_STATE_DIR -RunMetadataPath $runMetadataPath -IndexPath $indexPath -AuditLogPath $auditLogPath -ConfigDepsPath $configDepsPath -CI_MODE:$CI_MODE
Write-Status ''

$deleteList = @()
if ($DRY_RUN -and -not $CI_MODE) {
    Write-Status '[DRY-RUN] Generating impact simulation...'
    $decisions = @(Read-JsonArtifact -Path $auditExplanationsPath -Depth 12)
    foreach ($dec in $decisions) {
        if ($dec.decision -eq 'DELETE_CANDIDATE') {
            $deleteList += [pscustomobject]@{
                file = $dec.file
                confidence = $dec.confidence
                reasons = @($dec.why_not_classified_differently)
            }
        }
    }

    $closure = Read-JsonArtifact -Path $closurePath -Depth 10
    $report = @()
    $report += '# Dry-Run Impact Simulation Report'
    $report += ''
    $report += "**Target Repository:** $TargetRepoPath"
    $report += "**Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $report += '**Mode:** DRY_RUN (No files actually deleted)'
    $report += ''
    $report += '## Deletion Plan Summary'
    $report += ''
    $report += "**Total files to delete:** $($deleteList.Count)"
    $report += ''
    if ($deleteList.Count -gt 0) {
        $report += '### Candidates for deletion:'
        $report += ''
        foreach ($i in 0..($deleteList.Count - 1)) {
            $item = $deleteList[$i]
            $report += "$($i + 1). **$($item.file)** (confidence: $($item.confidence))"
            foreach ($reason in $item.reasons) {
                $report += "   - $reason"
            }
            $report += ''
        }
    }
    else {
        $report += 'No files marked for deletion.'
        $report += ''
    }

    $unsafeCount = 0
    if ($closure.PSObject.Properties) {
        foreach ($file in $closure.PSObject.Properties.Name) {
            if ($closure.$file.blast_radius -gt 0) {
                $unsafeCount++
            }
        }
    }

    $report += '## Safety Validation'
    $report += ''
    if ($unsafeCount -eq 0) {
        $report += 'All delete candidates are isolated in the strong dependency closure model.'
    }
    else {
        $report += 'One or more candidates still have transitive strong dependents.'
    }
    $report += ''
    $report -join "`n" | Out-File -LiteralPath $dryRunReportPath -Encoding UTF8
    Write-Status "Dry-run report: $dryRunReportPath"
}

$indexData = Read-JsonArtifact -Path $indexPath -Depth 12
$healthData = Read-JsonArtifact -Path $healthPath -Depth 12

$runMetadata.end_time = (Get-Date).ToUniversalTime().ToString('o')
$runMetadata.file_count = $indexData.total_files
$runMetadata.scan_mode = $indexData.scan_mode
$runMetadata.issue_summary = $healthData.issue_summary
Write-JsonFile -Path $runMetadataPath -Data $runMetadata -Depth 10

$issuesDetected = ($healthData.issue_summary.dead_code_count -gt 0) -or ($healthData.issue_summary.contradictions_count -gt 0) -or ($healthData.issue_summary.high_risk_count -gt 0)
$exitCode = if ($issuesDetected) { 1 } else { 0 }

# ========================================================
# OPTIONAL LLM LAYERS (POST-PROCESSING)
# ========================================================
$llmMode = Get-LLMMode
if ($llmMode -ne 'static') {
    Write-Status "[LLM] Semantic/Decision layers enabled: $llmMode"

    # Semantic layer: interpret static outputs
    if ($llmMode -in @('semantic', 'full')) {
        $semanticLayerScript = Join-Path $engineRoot 'src\llm\semantic_summarizer.ps1'
        if (Test-Path -LiteralPath $semanticLayerScript) {
            try {
                Write-Status "[LLM] Running semantic summarizer..."
                . $semanticLayerScript
                $semanticOutputDir = Join-Path $RUN_OUTPUT_DIR 'semantic'
                New-Item -ItemType Directory -Path $semanticOutputDir -Force | Out-Null
                Invoke-SemanticSummarizer -RunDir $RUN_OUTPUT_DIR -OutputDir $semanticOutputDir
                Write-Status "[LLM] Semantic layer complete"
            }
            catch {
                Write-AuditWarning "[LLM] Semantic layer failed (non-fatal): $_"
            }
        }
    }

    # Decision layer: prioritize refactoring
    if ($llmMode -eq 'full') {
        $decisionLayerScript = Join-Path $engineRoot 'src\llm\decision_layer.ps1'
        if (Test-Path -LiteralPath $decisionLayerScript) {
            try {
                Write-Status "[LLM] Running decision layer..."
                . $decisionLayerScript
                $decisionOutputDir = Join-Path $RUN_OUTPUT_DIR 'decisions'
                New-Item -ItemType Directory -Path $decisionOutputDir -Force | Out-Null
                Invoke-DecisionLayer -RunDir $RUN_OUTPUT_DIR -OutputDir $decisionOutputDir
                Write-Status "[LLM] Decision layer complete"
            }
            catch {
                Write-AuditWarning "[LLM] Decision layer failed (non-fatal): $_"
            }
        }
    }
}

if (-not $CI_MODE) {
    Write-Status 'Pipeline complete.'
    Write-Status ''
    Write-Status 'Output files:'
    foreach ($path in @($runMetadataPath, $manifestPath, $indexPath, $auditLogPath, $truthGraphPath, $closurePath, $architecturePath, $deadCodePath, $healthPath, $contradictionsPath, $auditExplanationsPath, $reportPath)) {
        if (Test-Path -LiteralPath $path) {
            Write-Status "  - $path"
        }
    }
    if ($DRY_RUN -and (Test-Path -LiteralPath $dryRunReportPath)) {
        Write-Status "  - $dryRunReportPath"
    }
}

return [pscustomobject]@{
    exit_code = $exitCode
    run_id = $RunId
    repo_id = $repoId
    run_dir = $RUN_OUTPUT_DIR
    run_metadata_path = $runMetadataPath
}