[CmdletBinding()]
param(
    [ValidateSet('status', 'init', 'layer1-inventory', 'layer2-canonical', 'layer3-resolve', 'layer4-graph', 'layer5-validate', 'verify-authority', 'layer6-query', 'layer7-classify', 'layer8-report', 'validate-graph-structure', 'compare-resolvers', 'semantic-validate', 'aggregate-trust')][string]$Command = 'status',
    [string]$WorkspacePath = $PSScriptRoot,
    [string]$RepoPath,
    [string]$InventoryPath,
    [string]$CanonicalPath,
    [string]$EdgesPath,
    [string]$GraphPath,
    [int]$HeuristicOnlyThreshold = 0,
    [string]$StructuralValidationPath,
    [string]$ReachabilityValidationPath,
    [string]$ResolverConsistencyPath,
    [string]$SemanticValidationPath,
    [string]$ValidationPath,
    [string]$AuthorityPath,
    [string]$ClassificationPath,
    [string]$Query,
    [string[]]$Entrypoints,
    [string]$ReachableQueryPath,
    [string]$OrphanQueryPath,
    [string]$DeadQueryPath,
    [string]$SuspiciousQueryPath,
    [string]$DisconnectedClustersQueryPath,
    [string]$ExemptQueryPath,
    [string]$OutputPath,
    [switch]$FailOnInvalid = $true,
    [switch]$DebugMode
)

$ErrorActionPreference = 'Stop'

$engineRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeCommon = Join-Path $engineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon)) {
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
        Write-Status "Initialized minimal baseline at: $workspace"
        break
    }
    'status' {
        $summary = [ordered]@{
            phase = 'phase_0_cleanup_baseline'
            workspace = $workspace
            entrypoint = 'run.ps1'
            utility_module = 'src/runtime_common.ps1'
            next_action = 'Implement v3 architecture from clean baseline'
        }
        Write-Host ($summary | ConvertTo-Json -Depth 6)
        break
    }
    'layer1-inventory' {
        if ([string]::IsNullOrWhiteSpace($RepoPath)) {
            throw 'RepoPath is required for layer1-inventory.'
        }

        $layer1Script = Join-Path $engineRoot 'src\layer1_file_inventory.ps1'
        if (-not (Test-Path -LiteralPath $layer1Script)) {
            throw "Missing layer1 script: $layer1Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\layer1_inventory.json'
        }

        $result = & $layer1Script -RepoPath $RepoPath -OutputPath $resolvedOutputPath -DebugMode:([bool]$DebugMode)
        Write-Host ($result | ConvertTo-Json -Depth 8)
        break
    }
    'layer2-canonical' {
        if ([string]::IsNullOrWhiteSpace($InventoryPath)) {
            throw 'InventoryPath is required for layer2-canonical.'
        }

        $layer2Script = Join-Path $engineRoot 'src\layer2_canonical_identity.ps1'
        if (-not (Test-Path -LiteralPath $layer2Script)) {
            throw "Missing layer2 script: $layer2Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\canonical_nodes.json'
        }

        $result = & $layer2Script -InventoryPath $InventoryPath -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 8)
        break
    }
    'layer3-resolve' {
        if ([string]::IsNullOrWhiteSpace($InventoryPath)) {
            throw 'InventoryPath is required for layer3-resolve.'
        }
        if ([string]::IsNullOrWhiteSpace($CanonicalPath)) {
            throw 'CanonicalPath is required for layer3-resolve.'
        }

        $layer3Script = Join-Path $engineRoot 'src\layer3_multi_resolver.ps1'
        if (-not (Test-Path -LiteralPath $layer3Script)) {
            throw "Missing layer3 script: $layer3Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\edges.json'
        }

        $result = & $layer3Script -InventoryPath $InventoryPath -CanonicalPath $CanonicalPath -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 8)
        break
    }
    'layer4-graph' {
        if ([string]::IsNullOrWhiteSpace($CanonicalPath)) {
            throw 'CanonicalPath is required for layer4-graph.'
        }
        if ([string]::IsNullOrWhiteSpace($EdgesPath)) {
            throw 'EdgesPath is required for layer4-graph.'
        }

        $layer4Script = Join-Path $engineRoot 'src\layer4_unified_graph.ps1'
        if (-not (Test-Path -LiteralPath $layer4Script)) {
            throw "Missing layer4 script: $layer4Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\unified_graph.json'
        }

        $result = & $layer4Script -CanonicalPath $CanonicalPath -EdgesPath $EdgesPath -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 10)
        break
    }
    'layer5-validate' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for layer5-validate.'
        }

        $layer5Script = Join-Path $engineRoot 'src\layer5_graph_validation.ps1'
        if (-not (Test-Path -LiteralPath $layer5Script)) {
            throw "Missing layer5 script: $layer5Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\graph_validation.json'
        }

        $result = & $layer5Script -GraphPath $GraphPath -OutputPath $resolvedOutputPath -FailOnInvalid:([bool]$FailOnInvalid)
        Write-Host ($result | ConvertTo-Json -Depth 10)
        break
    }
    'verify-authority' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for verify-authority.'
        }
        if ([string]::IsNullOrWhiteSpace($EdgesPath)) {
            throw 'EdgesPath is required for verify-authority.'
        }
        if ([string]::IsNullOrWhiteSpace($ValidationPath)) {
            throw 'ValidationPath is required for verify-authority.'
        }

        $authorityScript = Join-Path $engineRoot 'src\verification_authority_gate.py'
        if (-not (Test-Path -LiteralPath $authorityScript)) {
            throw "Missing authority gate script: $authorityScript"
        }

        $pythonExe = $null
        foreach ($candidate in @('python', 'py', 'python3')) {
            try {
                $versionOut = & $candidate --version 2>&1
                if ("$versionOut" -match 'Python 3') {
                    $pythonExe = $candidate
                    break
                }
            }
            catch { }
        }
        if ([string]::IsNullOrWhiteSpace($pythonExe)) {
            throw 'Python 3 runtime is required for verify-authority.'
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\authority_verdict.json'
        }

        $invokeArgs = @(
            $authorityScript,
            '--graph-path', $GraphPath,
            '--edges-path', $EdgesPath,
            '--validation-path', $ValidationPath,
            '--output-path', $resolvedOutputPath
        )

        foreach ($entrypoint in @($Entrypoints)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$entrypoint)) {
                $invokeArgs += @('--entrypoint', [string]$entrypoint)
            }
        }

        & $pythonExe @invokeArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Verification authority gate failed. See report: $resolvedOutputPath"
        }

        $result = Get-Content -LiteralPath $resolvedOutputPath -Raw | ConvertFrom-Json -Depth 20
        Write-Host ($result | ConvertTo-Json -Depth 20)
        break
    }
    'layer6-query' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for layer6-query.'
        }
        if ([string]::IsNullOrWhiteSpace($ValidationPath)) {
            throw 'ValidationPath is required for layer6-query.'
        }
        if ([string]::IsNullOrWhiteSpace($AuthorityPath)) {
            $AuthorityPath = Join-Path $workspace 'output\authority_verdict.json'
        }
        if ([string]::IsNullOrWhiteSpace($Query)) {
            throw 'Query is required for layer6-query.'
        }

        $layer6Script = Join-Path $engineRoot 'src\layer6_graph_query.ps1'
        if (-not (Test-Path -LiteralPath $layer6Script)) {
            throw "Missing layer6 script: $layer6Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\graph_query_result.json'
        }

        $result = & $layer6Script -GraphPath $GraphPath -ValidationPath $ValidationPath -AuthorityPath $AuthorityPath -Query $Query -Entrypoints $Entrypoints -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 10)
        break
    }
    'layer7-classify' {
        if ([string]::IsNullOrWhiteSpace($ValidationPath)) {
            throw 'ValidationPath is required for layer7-classify.'
        }
        if ([string]::IsNullOrWhiteSpace($AuthorityPath)) {
            $AuthorityPath = Join-Path $workspace 'output\authority_verdict.json'
        }
        if ([string]::IsNullOrWhiteSpace($ReachableQueryPath)) {
            throw 'ReachableQueryPath is required for layer7-classify.'
        }
        if ([string]::IsNullOrWhiteSpace($OrphanQueryPath)) {
            throw 'OrphanQueryPath is required for layer7-classify.'
        }
        if ([string]::IsNullOrWhiteSpace($DeadQueryPath)) {
            throw 'DeadQueryPath is required for layer7-classify.'
        }
        if ([string]::IsNullOrWhiteSpace($SuspiciousQueryPath)) {
            throw 'SuspiciousQueryPath is required for layer7-classify.'
        }
        if ([string]::IsNullOrWhiteSpace($DisconnectedClustersQueryPath)) {
            throw 'DisconnectedClustersQueryPath is required for layer7-classify.'
        }

        $layer7Script = Join-Path $engineRoot 'src\layer7_query_classification.ps1'
        if (-not (Test-Path -LiteralPath $layer7Script)) {
            throw "Missing layer7 script: $layer7Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\classification.json'
        }

        $result = & $layer7Script `
            -ValidationPath $ValidationPath `
            -AuthorityPath $AuthorityPath `
            -ReachableQueryPath $ReachableQueryPath `
            -OrphanQueryPath $OrphanQueryPath `
            -DeadQueryPath $DeadQueryPath `
            -SuspiciousQueryPath $SuspiciousQueryPath `
            -DisconnectedClustersQueryPath $DisconnectedClustersQueryPath `
            -ExemptQueryPath $ExemptQueryPath `
            -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 10)
        break
    }
    'layer8-report' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for layer8-report.'
        }
        if ([string]::IsNullOrWhiteSpace($ValidationPath)) {
            throw 'ValidationPath is required for layer8-report.'
        }
        if ([string]::IsNullOrWhiteSpace($ClassificationPath)) {
            throw 'ClassificationPath is required for layer8-report.'
        }

        $layer8Script = Join-Path $engineRoot 'src\layer8_final_report.ps1'
        if (-not (Test-Path -LiteralPath $layer8Script)) {
            throw "Missing layer8 script: $layer8Script"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\final_structured_report.json'
        }

        $result = & $layer8Script -GraphPath $GraphPath -ValidationPath $ValidationPath -ClassificationPath $ClassificationPath -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 10)
        break
    }
    'validate-graph-structure' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for validate-graph-structure.'
        }
        if ([string]::IsNullOrWhiteSpace($InventoryPath)) {
            throw 'InventoryPath is required for validate-graph-structure.'
        }

        $structuralValidationScript = Join-Path $engineRoot 'src\graph_structural_validation.ps1'
        if (-not (Test-Path -LiteralPath $structuralValidationScript)) {
            throw "Missing structural validation script: $structuralValidationScript"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\graph_structural_validation.json'
        }

        $result = & $structuralValidationScript -GraphPath $GraphPath -InventoryPath $InventoryPath -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 12)
        break
    }
    'compare-resolvers' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for compare-resolvers.'
        }
        if ([string]::IsNullOrWhiteSpace($EdgesPath)) {
            throw 'EdgesPath is required for compare-resolvers.'
        }

        $resolverCompareScript = Join-Path $engineRoot 'src\resolver_consistency_check.ps1'
        if (-not (Test-Path -LiteralPath $resolverCompareScript)) {
            throw "Missing resolver consistency script: $resolverCompareScript"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\resolver_consistency.json'
        }

        $result = & $resolverCompareScript -GraphPath $GraphPath -EdgesPath $EdgesPath -HeuristicOnlyThreshold $HeuristicOnlyThreshold -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 12)
        break
    }
    'semantic-validate' {
        if ([string]::IsNullOrWhiteSpace($GraphPath)) {
            throw 'GraphPath is required for semantic-validate.'
        }

        $semanticValidationScript = Join-Path $engineRoot 'src\semantic_graph_validation.ps1'
        if (-not (Test-Path -LiteralPath $semanticValidationScript)) {
            throw "Missing semantic validation script: $semanticValidationScript"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\semantic_validation.json'
        }

        $result = & $semanticValidationScript -GraphPath $GraphPath -ClassificationPath $ClassificationPath -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 12)
        break
    }
    'aggregate-trust' {
        if ([string]::IsNullOrWhiteSpace($StructuralValidationPath)) {
            throw 'StructuralValidationPath is required for aggregate-trust.'
        }
        if ([string]::IsNullOrWhiteSpace($ReachabilityValidationPath)) {
            throw 'ReachabilityValidationPath is required for aggregate-trust.'
        }
        if ([string]::IsNullOrWhiteSpace($ResolverConsistencyPath)) {
            throw 'ResolverConsistencyPath is required for aggregate-trust.'
        }
        if ([string]::IsNullOrWhiteSpace($SemanticValidationPath)) {
            throw 'SemanticValidationPath is required for aggregate-trust.'
        }

        $trustScript = Join-Path $engineRoot 'src\system_trust_aggregation.ps1'
        if (-not (Test-Path -LiteralPath $trustScript)) {
            throw "Missing trust aggregation script: $trustScript"
        }

        $resolvedOutputPath = $OutputPath
        if ([string]::IsNullOrWhiteSpace($resolvedOutputPath)) {
            $resolvedOutputPath = Join-Path $workspace 'output\system_trust.json'
        }

        $result = & $trustScript `
            -StructuralValidationPath $StructuralValidationPath `
            -ReachabilityValidationPath $ReachabilityValidationPath `
            -ResolverConsistencyPath $ResolverConsistencyPath `
            -SemanticValidationPath $SemanticValidationPath `
            -OutputPath $resolvedOutputPath
        Write-Host ($result | ConvertTo-Json -Depth 12)
        break
    }
}
