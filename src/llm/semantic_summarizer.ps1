# Semantic Summarizer - Interprets static analysis for human understanding
# 
# INPUT: structured JSON outputs from static engine (no raw code)
# OUTPUT: human-readable system understanding
# 
# This layer NEVER modifies static results, only interprets them

param()

function Invoke-SemanticSummarizer {
    <#
    .SYNOPSIS
    Generate semantic understanding of the analyzed system
    
    .PARAMETER RunDir
    Directory containing static analysis outputs
    
    .PARAMETER OutputDir
    Directory to write semantic outputs
    
    .OUTPUTS
    semantic_summary.md, architecture_narrative.json, system_overview.json
    #>
    param(
        [string]$RunDir,
        [string]$OutputDir
    )
    
    Write-Host "🔄 Semantic Summarizer starting..."
    
    # Load static analysis outputs
    $dependencyGraphPath = Join-Path $RunDir "dependency_truth_graph.json"
    $architectureAnalysisPath = Join-Path $RunDir "architecture_analysis.json"
    $healthScorePath = Join-Path $RunDir "system_health_score.json"
    $manifestPath = Join-Path $RunDir "manifest.json"
    
    if (-not (Test-Path $dependencyGraphPath)) {
        Write-Warning "dependency_truth_graph.json not found. Skipping semantic layer."
        return
    }
    
    $dependencyGraph = Get-Content $dependencyGraphPath | ConvertFrom-Json
    $archAnalysis = if (Test-Path $architectureAnalysisPath) { Get-Content $architectureAnalysisPath | ConvertFrom-Json } else { $null }
    $healthScore = if (Test-Path $healthScorePath) { Get-Content $healthScorePath | ConvertFrom-Json } else { $null }
    $manifest = if (Test-Path $manifestPath) { Get-Content $manifestPath | ConvertFrom-Json } else { $null }
    
    # Build semantic understanding from metrics
    $architecture = Build-ArchitectureNarrative $dependencyGraph $archAnalysis $healthScore
    $overview = Build-SystemOverview $dependencyGraph $manifest $healthScore
    
    # Write outputs
    $architecture | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $OutputDir "architecture_narrative.json")
    $overview | ConvertTo-Json -Depth 10 | Set-Content (Join-Path $OutputDir "system_overview.json")
    
    # Generate markdown summary
    $markdown = Generate-SemanticMarkdown $architecture $overview
    $markdown | Set-Content (Join-Path $OutputDir "semantic_summary.md")
    
    Write-Host "✓ Semantic summarizer complete"
}

function Build-ArchitectureNarrative {
    <#
    .SYNOPSIS
    Infer architecture patterns from dependency graph
    
    .PARAMETER DependencyGraph
    Static dependency graph JSON
    
    .PARAMETER ArchAnalysis
    Static architecture analysis JSON
    
    .PARAMETER HealthScore
    Static health metrics
    
    .OUTPUTS
    Architecture narrative object with confidence scores
    #>
    param(
        [object]$DependencyGraph,
        [object]$ArchAnalysis,
        [object]$HealthScore
    )
    
    # Extract metrics from static data
    $nodes = $DependencyGraph.data.nodes
    $edges = $DependencyGraph.data.edges
    
    # Infer architecture style from graph structure
    $architectureStyle = Infer-ArchitectureStyle $nodes $edges
    
    # Identify core components (high importance)
    $coreComponents = @($nodes | Where-Object { $_.importance -ge 0.75 } | Select-Object -Property name, importance, inbound_count, outbound_count)
    
    # Identify data flows from edges
    $dataFlows = @($edges | Where-Object { $_.weight -gt 0.5 } | Select-Object -Property source, target | Get-Unique)
    
    # Risk summary from health metrics
    $riskSummary = "Unknown"
    $confidence = 50
    
    if ($HealthScore) {
        $riskLevel = $HealthScore.data.risk_level
        $riskSummary = "System risk level: $riskLevel. Cohesion: {0:P}" -f $HealthScore.data.cohesion
        $confidence = [Math]::Min(95, $HealthScore.data.confidence)
    }
    
    # Build narrative
    @{
        system_purpose = "Analyzed system with $($nodes.Count) components"
        architecture_style = $architectureStyle
        core_components = $coreComponents
        execution_flow = @()  # Would infer from call patterns
        data_flow = $dataFlows
        risk_summary = $riskSummary
        confidence = $confidence
        generated_at = Get-Date -Format "o"
    }
}

function Build-SystemOverview {
    <#
    .SYNOPSIS
    Create high-level overview of system purpose and structure
    
    .OUTPUTS
    System overview object
    #>
    param(
        [object]$DependencyGraph,
        [object]$Manifest,
        [object]$HealthScore
    )
    
    $nodes = $DependencyGraph.data.nodes
    $edges = $DependencyGraph.data.edges
    
    # Statistics
    @{
        total_files = $nodes.Count
        total_dependencies = $edges.Count
        avg_dependencies_per_file = if ($nodes.Count -gt 0) { $edges.Count / $nodes.Count } else { 0 }
        hub_files = @($nodes | Sort-Object -Property inbound_count -Descending | Select-Object -First 5 -Property name, inbound_count)
        leaves = @($nodes | Where-Object { $_.inbound_count -eq 0 } | Measure-Object | Select-Object -ExpandProperty Count)
        health_status = if ($HealthScore) { $HealthScore.data.risk_level } else { "Unknown" }
        generated_at = Get-Date -Format "o"
    }
}

function Infer-ArchitectureStyle {
    <#
    .SYNOPSIS
    Infer architecture style from dependency graph structure
    
    .DESCRIPTION
    Identifies patterns:
    - Layered: clear vertical dependency chains
    - Modular: weakly coupled components
    - Hub-and-spoke: central coordinator
    - Monolithic: high interconnection
    #>
    param(
        [object[]]$Nodes,
        [object[]]$Edges
    )
    
    if (-not $Nodes -or $Nodes.Count -eq 0) {
        return "unknown"
    }
    
    # Calculate metrics
    $avgInbound = ($Nodes | Measure-Object -Property inbound_count -Average).Average
    $avgOutbound = ($Nodes | Measure-Object -Property outbound_count -Average).Average
    $hubCount = @($Nodes | Where-Object { $_.inbound_count -gt ($avgInbound * 3) }).Count
    
    # Infer style
    if ($hubCount -gt 0 -and $hubCount -lt $Nodes.Count * 0.1) {
        return "hub-and-spoke"
    }
    elseif ($avgInbound -lt 2 -and $avgOutbound -lt 2) {
        return "modular"
    }
    elseif ($avgInbound -gt 5 -and $avgOutbound -gt 5) {
        return "monolithic"
    }
    else {
        return "layered"
    }
}

function Generate-SemanticMarkdown {
    <#
    .SYNOPSIS
    Generate markdown summary from architecture narrative
    #>
    param(
        [object]$Architecture,
        [object]$Overview
    )
    
    $md = @"
# System Architecture Summary

Generated: $($Architecture.generated_at)  
Confidence: $($Architecture.confidence)%

## System Purpose
$($Architecture.system_purpose)

## Architecture Style
**$($Architecture.architecture_style.ToUpper())**

## Core Components
$($Architecture.core_components | ForEach-Object {
    "- **$($_.name)**: $($_.inbound_count) inbound, $($_.outbound_count) outbound"
})

## System Metrics
- **Total Files**: $($Overview.total_files)
- **Total Dependencies**: $($Overview.total_dependencies)
- **Avg Dependencies/File**: $([Math]::Round($Overview.avg_dependencies_per_file, 2))
- **Hub Files**: $($Overview.hub_files.Count)
- **Orphaned Files**: $($Overview.leaves)
- **Health Status**: $($Overview.health_status)

## Risk Assessment
$($Architecture.risk_summary)

---
*This summary is inferred from static dependency analysis. See dependency_truth_graph.json for authoritative data.*
"@
    
    return $md
}


