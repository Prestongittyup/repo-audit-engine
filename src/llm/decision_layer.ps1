# Decision Layer - Ranks risks and prioritizes refactoring
# 
# INPUT: structured JSON outputs from static engine (no raw code)
# OUTPUT: prioritized refactor recommendations with confidence scores
# 
# RULE: NEVER marks files as "delete" - only suggests:
# - HIGH PRIORITY REFACTOR
# - MEDIUM PRIORITY REVIEW  
# - LOW PRIORITY OBSERVATION

param()

function Invoke-DecisionLayer {
    <#
    .SYNOPSIS
    Generate prioritized refactoring recommendations
    
    .PARAMETER RunDir
    Directory containing static analysis outputs
    
    .PARAMETER OutputDir
    Directory to write decision outputs
    
    .OUTPUTS
    refactor_priorities.json, risk_ranking.json, architecture_recommendations.md
    #>
    param(
        [string]$RunDir,
        [string]$OutputDir
    )
    
    Write-Host "🎯 Decision Layer starting..."
    
    # Load static analysis outputs
    $dependencyGraphPath = Join-Path $RunDir "dependency_truth_graph.json"
    $deadCodePath = Join-Path $RunDir "dead_code_report.json"
    $contradictionsPath = Join-Path $RunDir "contradictions.json"
    $healthScorePath = Join-Path $RunDir "system_health_score.json"
    $auditLogPath = Join-Path $RunDir "audit_log.jsonl"
    
    if (-not (Test-Path $dependencyGraphPath)) {
        Write-Warning "dependency_truth_graph.json not found. Skipping decision layer."
        return
    }
    
    $dependencyGraph = Get-Content $dependencyGraphPath | ConvertFrom-Json
    $deadCodeReport = if (Test-Path $deadCodePath) { Get-Content $deadCodePath | ConvertFrom-Json } else { $null }
    $contradictions = if (Test-Path $contradictionsPath) { Get-Content $contradictionsPath | ConvertFrom-Json } else { $null }
    $healthScore = if (Test-Path $healthScorePath) { Get-Content $healthScorePath | ConvertFrom-Json } else { $null }
    
    # Generate recommendations
    $refactorPriorities = Rank-RefactorPriorities $dependencyGraph $deadCodeReport $contradictions $healthScore
    $riskRanking = Rank-RiskItems $dependencyGraph $deadCodeReport $contradictions
    
    # Write outputs
    $refactorPriorities | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $OutputDir "refactor_priorities.json")
    $riskRanking | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $OutputDir "risk_ranking.json")
    
    # Generate markdown recommendations
    $markdown = Generate-RecommendationsMarkdown $refactorPriorities $riskRanking
    $markdown | Set-Content (Join-Path $OutputDir "architecture_recommendations.md")
    
    Write-Host "✓ Decision layer complete"
}

function Rank-RefactorPriorities {
    <#
    .SYNOPSIS
    Rank files/components by refactoring priority
    
    .OUTPUTS
    Array of refactor recommendations with priority levels
    #>
    param(
        [object]$DependencyGraph,
        [object]$DeadCodeReport,
        [object]$Contradictions,
        [object]$HealthScore
    )
    
    $recommendations = @()
    
    # Analyze nodes for refactoring
    foreach ($node in $DependencyGraph.data.nodes) {
        $priority = 'LOW'
        $reasons = @()
        $confidence = 50
        
        # HIGH priority: high coupling + high inbound deps
        if ($node.outbound_count -gt 15 -and $node.inbound_count -gt 10) {
            $priority = 'HIGH'
            $reasons += "High coupling (exports: $($node.outbound_count)) with high criticality (imports: $($node.inbound_count))"
            $confidence = 85
        }
        
        # HIGH priority: central hub with many dependencies
        if ($node.inbound_count -gt 20) {
            $priority = 'HIGH'
            $reasons += "Critical hub component (imported by $($node.inbound_count) files)"
            $confidence = 90
        }
        
        # MEDIUM priority: moderate coupling
        if ($node.outbound_count -gt 10 -and $priority -ne 'HIGH') {
            $priority = 'MEDIUM'
            $reasons += "Moderate coupling (depends on $($node.outbound_count) components)"
            $confidence = 75
        }
        
        # MEDIUM priority: cyclic dependencies
        if ($node.has_cycles -eq $true) {
            $priority = 'MEDIUM'
            $reasons += "Participates in dependency cycles"
            $confidence = 80
        }
        
        $recommendations += @{
            file = $node.name
            priority = $priority
            reasons = $reasons
            confidence = $confidence
            metrics = @{
                inbound_dependencies = $node.inbound_count
                outbound_dependencies = $node.outbound_count
                importance_score = $node.importance
            }
        }
    }
    
    # Sort by priority
    $priorityOrder = @{ 'HIGH' = 1; 'MEDIUM' = 2; 'LOW' = 3 }
    $recommendations = $recommendations | Sort-Object { $priorityOrder[$_.priority] } | Select-Object -First 50
    
    @{
        generated_at = Get-Date -Format "o"
        engine_version = "1.0.0"
        schema_version = "v3"
        total_recommendations = $recommendations.Count
        by_priority = @{
            HIGH = ($recommendations | Where-Object { $_.priority -eq 'HIGH' }).Count
            MEDIUM = ($recommendations | Where-Object { $_.priority -eq 'MEDIUM' }).Count
            LOW = ($recommendations | Where-Object { $_.priority -eq 'LOW' }).Count
        }
        recommendations = $recommendations
    }
}

function Rank-RiskItems {
    <#
    .SYNOPSIS
    Rank files by risk exposure
    
    .OUTPUTS
    Array of risk items with severity and justification
    #>
    param(
        [object]$DependencyGraph,
        [object]$DeadCodeReport,
        [object]$Contradictions
    )
    
    $risks = @()
    
    # Dead code candidates that are heavily imported (risky to remove)
    if ($DeadCodeReport -and $DeadCodeReport.data.candidates) {
        foreach ($candidate in $DeadCodeReport.data.candidates) {
            $node = $DependencyGraph.data.nodes | Where-Object { $_.name -eq $candidate.file }
            
            if ($node -and $node.inbound_count -gt 0) {
                $severity = 'HIGH'
                $reason = "Dead code candidate but imported by $($node.inbound_count) files - removing could break dependents"
            }
            else {
                $severity = 'MEDIUM'
                $reason = "Dead code candidate with confidence $($candidate.confidence)"
            }
            
            $risks += @{
                file = $candidate.file
                risk_type = 'dead_code_candidate'
                severity = $severity
                justification = $reason
                confidence = $candidate.confidence
            }
        }
    }
    
    # Contradictions (high risk)
    if ($Contradictions -and $Contradictions.data.issues) {
        foreach ($issue in $Contradictions.data.issues) {
            $risks += @{
                file = $issue.file
                risk_type = 'contradiction'
                severity = 'HIGH'
                justification = $issue.description
                confidence = 95
            }
        }
    }
    
    # Sort by severity
    $severityOrder = @{ 'HIGH' = 1; 'MEDIUM' = 2; 'LOW' = 3 }
    $risks = $risks | Sort-Object { $severityOrder[$_.severity] } | Select-Object -First 50
    
    @{
        generated_at = Get-Date -Format "o"
        engine_version = "1.0.0"
        schema_version = "v3"
        total_risks = $risks.Count
        by_severity = @{
            HIGH = ($risks | Where-Object { $_.severity -eq 'HIGH' }).Count
            MEDIUM = ($risks | Where-Object { $_.severity -eq 'MEDIUM' }).Count
            LOW = ($risks | Where-Object { $_.severity -eq 'LOW' }).Count
        }
        risks = $risks
    }
}

function Generate-RecommendationsMarkdown {
    <#
    .SYNOPSIS
    Generate markdown recommendations document
    #>
    param(
        [object]$RefactorPriorities,
        [object]$RiskRanking
    )
    
    $highPriorityCount = $RefactorPriorities.by_priority.HIGH
    $mediumPriorityCount = $RefactorPriorities.by_priority.MEDIUM
    
    $highRiskCount = $RiskRanking.by_severity.HIGH
    $mediumRiskCount = $RiskRanking.by_severity.MEDIUM
    
    $md = @"
# Architecture Recommendations

Generated: $($RefactorPriorities.generated_at)

## Executive Summary

- **HIGH Priority Refactors**: $highPriorityCount
- **MEDIUM Priority Reviews**: $mediumPriorityCount  
- **HIGH Severity Risks**: $highRiskCount
- **MEDIUM Severity Issues**: $mediumRiskCount

## High Priority Refactoring Targets

These components have the highest impact on system architecture and should be prioritized:

$($RefactorPriorities.recommendations | Where-Object { $_.priority -eq 'HIGH' } | ForEach-Object {
    "### $($_.file)
Confidence: $($_.confidence)%

**Metrics:**
- Inbound dependencies: $($_.metrics.inbound_dependencies)
- Outbound dependencies: $($_.metrics.outbound_dependencies)
- Importance score: $($_.metrics.importance_score)

**Reasons:**
$($_.reasons | ForEach-Object { "- $_" })

"
})

## Medium Priority Reviews

These components should be reviewed for potential improvements:

$($RefactorPriorities.recommendations | Where-Object { $_.priority -eq 'MEDIUM' } | Select-Object -First 10 | ForEach-Object {
    "- **$($_.file)** (Confidence: $($_.confidence)%): $($_.reasons[0])"
})

## High Severity Risks

Immediate attention required for these issues:

$($RiskRanking.risks | Where-Object { $_.severity -eq 'HIGH' } | ForEach-Object {
    "- **$($_.file)** [$($_.risk_type)]: $($_.justification) (Confidence: $($_.confidence)%)"
})

## Decision Methodology

1. **Refactoring Priority** is determined by:
   - Inbound dependency count (criticality)
   - Outbound dependency count (coupling)
   - Participation in cycles (complexity)

2. **Risk Severity** is determined by:
   - Dead code confidence (with consideration for who depends on it)
   - Architectural contradictions
   - Violation patterns

3. **Confidence Scores** reflect:
   - Statistical strength of the recommendation
   - Availability of supporting data

---
*Recommendations are suggestions based on static analysis. All changes should be validated through testing.*
"@
    
    return $md
}


