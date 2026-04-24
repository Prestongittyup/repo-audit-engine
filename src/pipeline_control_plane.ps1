[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EngineRoot,
    [Parameter(Mandatory = $true)]
    [string]$RepoPath,
    [Parameter(Mandatory = $true)]
    [string]$RunOutputDir,
    [string[]]$Entrypoints,
    [ValidateRange(0, 5000)][int]$HeuristicOnlyThreshold = 0,
    [double]$DriftThreshold = 1.0,
    [switch]$DebugMode
)

$ErrorActionPreference = 'Stop'

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
$RunOutputDir = [System.IO.Path]::GetFullPath($RunOutputDir)

$runtimeCommon = Join-Path $EngineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon -PathType Leaf)) {
    throw "Missing utility module: $runtimeCommon"
}
. $runtimeCommon

$systemStateModule = Join-Path $EngineRoot 'src\system_state.ps1'
$entrypointResolverModule = Join-Path $EngineRoot 'src\entrypoint_resolver.ps1'
$trustModule = Join-Path $EngineRoot 'src\trust_from_state.ps1'
$finalReportModule = Join-Path $EngineRoot 'src\final_report_emitter.ps1'
foreach ($modulePath in @($systemStateModule, $entrypointResolverModule, $trustModule, $finalReportModule)) {
    if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
        throw "Missing required module: $modulePath"
    }
    . $modulePath
}

Ensure-Directory -Path $RunOutputDir | Out-Null

$internalOutputDir = Join-Path $RunOutputDir '.internal'
Ensure-Directory -Path $internalOutputDir | Out-Null

function Resolve-RequiredScript {
    param([string]$RelativePath)

    $fullPath = Join-Path $EngineRoot $RelativePath
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Missing required script: $fullPath"
    }
    return $fullPath
}

function Get-Python3Executable {
    foreach ($candidate in @('python', 'py', 'python3')) {
        try {
            $versionOut = & $candidate --version 2>&1
            if ("$versionOut" -match 'Python 3') {
                return $candidate
            }
        }
        catch { }
    }

    throw 'Python 3 runtime is required for pipeline control plane.'
}

$artifactPaths = [ordered]@{
    inventory = Join-Path $internalOutputDir 'inventory.json'
    canonical_nodes = Join-Path $internalOutputDir 'canonical_nodes.json'
    edges = Join-Path $internalOutputDir 'edges.json'
    unified_graph = Join-Path $internalOutputDir 'unified_graph.json'
    graph_validation = Join-Path $internalOutputDir 'graph_validation.json'
    graph_structural_validation = Join-Path $internalOutputDir 'graph_structural_validation.json'
    resolver_consistency = Join-Path $internalOutputDir 'resolver_consistency.json'
    semantic_validation = Join-Path $internalOutputDir 'semantic_validation.json'
    authority_verdict = Join-Path $internalOutputDir 'authority_verdict.json'
    q_reachable = Join-Path $internalOutputDir 'q_reachable.json'
    q_orphan = Join-Path $internalOutputDir 'q_orphan.json'
    q_dead = Join-Path $internalOutputDir 'q_dead.json'
    q_suspicious = Join-Path $internalOutputDir 'q_suspicious.json'
    q_clusters = Join-Path $internalOutputDir 'q_clusters.json'
    classification = Join-Path $internalOutputDir 'classification.json'
    diagnostic_synthesis = Join-Path $internalOutputDir 'diagnostic_synthesis.json'
    system_state = Join-Path $RunOutputDir 'system_state.json'
    final_report = Join-Path $RunOutputDir 'final_report.json'
}

$state = New-SystemState -EngineRoot $EngineRoot -RepoPath $RepoPath -OutputDir $RunOutputDir -DebugMode:([bool]$DebugMode)
$state.artifacts.system_state = $artifactPaths.system_state
$state.artifacts.final_report = $artifactPaths.final_report
Save-SystemState -State $state -Path $artifactPaths.system_state

$layer1Script = Resolve-RequiredScript -RelativePath 'src\layer1_file_inventory.ps1'
$layer2Script = Resolve-RequiredScript -RelativePath 'src\layer2_canonical_identity.ps1'
$layer3Script = Resolve-RequiredScript -RelativePath 'src\layer3_multi_resolver.ps1'
$layer4Script = Resolve-RequiredScript -RelativePath 'src\layer4_unified_graph.ps1'
$structuralScript = Resolve-RequiredScript -RelativePath 'src\graph_structural_validation.ps1'
$resolverScript = Resolve-RequiredScript -RelativePath 'src\resolver_consistency_check.ps1'
$semanticScript = Resolve-RequiredScript -RelativePath 'src\semantic_graph_validation.ps1'
$layer6Script = Resolve-RequiredScript -RelativePath 'src\layer6_graph_query.ps1'
$layer7Script = Resolve-RequiredScript -RelativePath 'src\layer7_query_classification.ps1'
$diagnosticSynthesisScript = Resolve-RequiredScript -RelativePath 'src\diagnostic_synthesis_layer.ps1'
$pythonPhase1CliScript = Resolve-RequiredScript -RelativePath 'repo_audit_engine\cli.py'
$authorityScript = Resolve-RequiredScript -RelativePath 'src\verification_authority_gate.py'
$pythonExe = Get-Python3Executable

$pipelineFailed = $false
$errorMessage = ''

try {
    $state = Invoke-SystemStateTransition -State $state -StageName 'layer1-inventory' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.inventory = & $layer1Script -RepoPath $RepoPath -OutputPath $artifactPaths.inventory -DebugMode:([bool]$DebugMode)
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'layer2-canonical' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.canonical_nodes = & $layer2Script -InventoryPath $artifactPaths.inventory -OutputPath $artifactPaths.canonical_nodes
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'entrypoint-resolve' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.entrypoint_resolution = Resolve-Entrypoints -CanonicalNodesDoc $workingState.data.canonical_nodes -ExplicitEntrypoints $Entrypoints
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'layer3-resolve' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.edges = & $layer3Script -InventoryPath $artifactPaths.inventory -CanonicalPath $artifactPaths.canonical_nodes -OutputPath $artifactPaths.edges
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'layer4-graph' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.unified_graph = & $layer4Script -CanonicalPath $artifactPaths.canonical_nodes -EdgesPath $artifactPaths.edges -OutputPath $artifactPaths.unified_graph
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'layer5-validate' -StatePath $artifactPaths.system_state -Action {
        param($workingState)

        $validationArgs = @(
            $pythonPhase1CliScript,
            'validate',
            '--graph-path', $artifactPaths.unified_graph,
            '--resolver-path', $artifactPaths.edges,
            '--output', $artifactPaths.graph_validation,
            '--min-trust', '0.40'
        )

        foreach ($entrypoint in @($workingState.data.entrypoint_resolution.entrypoints)) {
            $validationArgs += @('--entrypoint', [string]$entrypoint)
        }

        & $pythonExe @validationArgs | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Python validation CLI failed with exit code $LASTEXITCODE"
        }

        if (-not (Test-Path -LiteralPath $artifactPaths.graph_validation -PathType Leaf)) {
            throw "Python validation CLI did not produce output: $($artifactPaths.graph_validation)"
        }

        $workingState.data.graph_validation = Read-JsonFile -Path $artifactPaths.graph_validation -Depth 50
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'validate-graph-structure' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.graph_structural_validation = & $structuralScript -GraphPath $artifactPaths.unified_graph -InventoryPath $artifactPaths.inventory -OutputPath $artifactPaths.graph_structural_validation -FailOnInvalid:$false
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'compare-resolvers' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.resolver_consistency = & $resolverScript -GraphPath $artifactPaths.unified_graph -EdgesPath $artifactPaths.edges -HeuristicOnlyThreshold $HeuristicOnlyThreshold -DriftThreshold $DriftThreshold -OutputPath $artifactPaths.resolver_consistency -FailOnInvalid:$false
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'semantic-validate' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $semanticEntrypoints = @($workingState.data.entrypoint_resolution.entrypoints)
        $workingState.data.semantic_validation = & $semanticScript -GraphPath $artifactPaths.unified_graph -Entrypoints $semanticEntrypoints -OutputPath $artifactPaths.semantic_validation -FailOnInvalid:$false
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'classify' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $effectiveEntrypoints = @($workingState.data.entrypoint_resolution.entrypoints)

        & $layer6Script -GraphPath $artifactPaths.unified_graph -ValidationPath $artifactPaths.graph_validation -Query 'REACHABLE_FROM(entrypoints)' -Entrypoints $effectiveEntrypoints -OutputPath $artifactPaths.q_reachable | Out-Null
        & $layer6Script -GraphPath $artifactPaths.unified_graph -ValidationPath $artifactPaths.graph_validation -Query 'ORPHAN_NODES' -Entrypoints $effectiveEntrypoints -OutputPath $artifactPaths.q_orphan | Out-Null
        & $layer6Script -GraphPath $artifactPaths.unified_graph -ValidationPath $artifactPaths.graph_validation -Query 'DEAD_NODES' -Entrypoints $effectiveEntrypoints -OutputPath $artifactPaths.q_dead | Out-Null
        & $layer6Script -GraphPath $artifactPaths.unified_graph -ValidationPath $artifactPaths.graph_validation -Query 'SUSPICIOUS_DI_NODES' -Entrypoints $effectiveEntrypoints -OutputPath $artifactPaths.q_suspicious | Out-Null
        & $layer6Script -GraphPath $artifactPaths.unified_graph -ValidationPath $artifactPaths.graph_validation -Query 'DISCONNECTED_CLUSTERS' -Entrypoints $effectiveEntrypoints -OutputPath $artifactPaths.q_clusters | Out-Null

        $workingState.data.classification = & $layer7Script `
            -ValidationPath $artifactPaths.graph_validation `
            -ReachableQueryPath $artifactPaths.q_reachable `
            -OrphanQueryPath $artifactPaths.q_orphan `
            -DeadQueryPath $artifactPaths.q_dead `
            -SuspiciousQueryPath $artifactPaths.q_suspicious `
            -DisconnectedClustersQueryPath $artifactPaths.q_clusters `
            -OutputPath $artifactPaths.classification
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'diagnostic-synthesis' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.diagnostic_synthesis = & $diagnosticSynthesisScript `
            -GraphPath $artifactPaths.unified_graph `
            -GraphValidationPath $artifactPaths.graph_validation `
            -GraphStructuralValidationPath $artifactPaths.graph_structural_validation `
            -ResolverConsistencyPath $artifactPaths.resolver_consistency `
            -SemanticValidationPath $artifactPaths.semantic_validation `
            -ClassificationPath $artifactPaths.classification `
            -OrphanQueryPath $artifactPaths.q_orphan `
            -ClustersQueryPath $artifactPaths.q_clusters `
            -OutputPath $artifactPaths.diagnostic_synthesis
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'verify-authority' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $invokeArgs = @(
            $authorityScript,
            '--graph-path', $artifactPaths.unified_graph,
            '--edges-path', $artifactPaths.edges,
            '--validation-path', $artifactPaths.graph_validation,
            '--output-path', $artifactPaths.authority_verdict,
            '--min-trust', '0.40'
        )

        foreach ($entrypoint in @($workingState.data.entrypoint_resolution.entrypoints)) {
            $invokeArgs += @('--entrypoint', [string]$entrypoint)
        }

        & $pythonExe @invokeArgs
        $gateExitCode = $LASTEXITCODE
        if (-not (Test-Path -LiteralPath $artifactPaths.authority_verdict -PathType Leaf)) {
            throw "Verification authority gate did not produce output: $($artifactPaths.authority_verdict)"
        }

        $workingState.data.authority_verdict = Read-JsonFile -Path $artifactPaths.authority_verdict -Depth 40

        $criticalFailure = $false
        if ($workingState.data.authority_verdict.PSObject.Properties.Name -contains 'verification' -and $null -ne $workingState.data.authority_verdict.verification) {
            if ($workingState.data.authority_verdict.verification.PSObject.Properties.Name -contains 'critical_failure') {
                $criticalFailure = [bool]$workingState.data.authority_verdict.verification.critical_failure
            }
        }

        if ($criticalFailure) {
            Write-Warn 'Critical policy signal detected by VerificationRunner. Continuing to emit full diagnostics and final report.'
        }

        if ($gateExitCode -ne 0 -and -not [bool]$workingState.data.authority_verdict.authority_valid) {
            Write-Warn "Authority policy returned degraded status (exit code $gateExitCode). Continuing pipeline for full reporting."
        }
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'compute-trust' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        $workingState.data.trust = Get-TrustFromSystemState -State $workingState
        return $workingState
    }

    $state = Invoke-SystemStateTransition -State $state -StageName 'emit-final-report' -StatePath $artifactPaths.system_state -Action {
        param($workingState)
        Write-FinalReportFromSystemState -State $workingState -OutputPath $artifactPaths.final_report | Out-Null
        return $workingState
    }

    $authorityValid = $false
    if ($null -ne $state.data.authority_verdict -and $state.data.authority_verdict.PSObject.Properties.Name -contains 'authority_valid') {
        $authorityValid = [bool]$state.data.authority_verdict.authority_valid
    }

    $criticalFailure = $false
    if ($null -ne $state.data.authority_verdict -and $state.data.authority_verdict.PSObject.Properties.Name -contains 'verification' -and $null -ne $state.data.authority_verdict.verification) {
        if ($state.data.authority_verdict.verification.PSObject.Properties.Name -contains 'critical_failure') {
            $criticalFailure = [bool]$state.data.authority_verdict.verification.critical_failure
        }
    }

    $finalStatus = if ($criticalFailure) { 'FAILED' } elseif ($authorityValid) { 'SUCCESS' } else { 'DEGRADED' }
    $finalMessage = if ($criticalFailure) {
        'Pipeline completed with critical policy findings. Review authority verdict and final report before promotion.'
    }
    elseif ($authorityValid) {
        'Pipeline completed successfully.'
    }
    else {
        'Pipeline completed with degraded authority confidence. Review final report issues and recommendations.'
    }

    $state.summary = [ordered]@{
        status = $finalStatus
        message = $finalMessage
        failed_stage = $null
    }

    Write-FinalReportFromSystemState -State $state -OutputPath $artifactPaths.final_report | Out-Null
}
catch {
    $pipelineFailed = $true
    $errorMessage = $_.Exception.Message
    $state.summary = [ordered]@{
        status = 'FAILED'
        message = $errorMessage
        failed_stage = if ($state.stages.Count -gt 0) { [string]$state.stages[-1].stage } else { '' }
    }
}
finally {
    $state.run.ended_utc = [DateTime]::UtcNow.ToString('o')
    Save-SystemState -State $state -Path $artifactPaths.system_state

    if (Test-Path -LiteralPath $internalOutputDir -PathType Container) {
        Remove-Item -LiteralPath $internalOutputDir -Recurse -Force
    }
}

if ($pipelineFailed) {
    throw $errorMessage
}

if ($null -ne $state.data.trust -and $state.data.trust -is [System.Collections.IDictionary] -and $state.data.trust.Contains('trust_score')) {
    $trustScoreOut = [double]$state.data.trust['trust_score']
}
elseif ($null -ne $state.data.trust -and $state.data.trust.PSObject.Properties.Name -contains 'trust_score') {
    $trustScoreOut = [double]$state.data.trust.trust_score
}
else {
    $trustScoreOut = 0.0
}

return [ordered]@{
    status = [string]$state.summary.status
    message = [string]$state.summary.message
    system_state_artifact = $artifactPaths.system_state
    final_report_artifact = $artifactPaths.final_report
    trust_score = [double]$trustScoreOut
    state_version = [int]$state.state_version
}
