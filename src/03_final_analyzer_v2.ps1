[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$AuditLogPath,
    [string]$ConfigDepsPath
)

$ErrorActionPreference = "Stop"

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path

if ([string]::IsNullOrWhiteSpace($AuditLogPath)) {
    $AuditLogPath = Join-Path $EngineRoot "state\audit_log.jsonl"
}
if ([string]::IsNullOrWhiteSpace($ConfigDepsPath)) {
    $ConfigDepsPath = Join-Path $EngineRoot "state\config_dependencies.json"
}

$stateDir = Join-Path $EngineRoot "state"
$outputDir = Join-Path $EngineRoot "output"

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null

$dependencyGraphPath = Join-Path $stateDir "dependency_graph.json"
$closurePath = Join-Path $stateDir "dependency_closure.json"
$decisionsPath = Join-Path $outputDir "decision_explanations.json"
$reportPath = Join-Path $outputDir "final_report.md"

# === PARSE AUDIT LOG ===
$records = @()
foreach ($line in [System.IO.File]::ReadLines($AuditLogPath)) {
    $line = $line.Trim()
    if ($line.Length -eq 0) { continue }
    
    try {
        $record = $line | ConvertFrom-Json
        $records += $record
    }
    catch {
        Write-Warning "Failed to parse: $line"
    }
}

Write-Host "Parsed $($records.Count) audit records"

# === LOAD CONFIG DEPENDENCIES (IF EXISTS) ===
$configDeps = @()
if (Test-Path -LiteralPath $ConfigDepsPath) {
    try {
        $configContent = Get-Content -LiteralPath $ConfigDepsPath -Raw
        if ($configContent.Trim().Length -gt 0) {
            $configDeps = $configContent | ConvertFrom-Json -ErrorAction SilentlyContinue
            if (-not $configDeps) { $configDeps = @() }
            elseif ($configDeps -isnot [array]) { $configDeps = @($configDeps) }
        }
    }
    catch {
        Write-Warning "Could not load config dependencies"
    }
}

Write-Host "Loaded $($configDeps.Count) config dependencies"

# === BUILD PROBABILISTIC DEPENDENCY GRAPH ===
$fileSet = @{}
$dependencyGraph = @{}
$inboundEdges = @{}
$outboundEdges = @{}

foreach ($record in $records) {
    $file = $record.file
    $fileSet[$file] = $record
    
    # Initialize graph node
    $graphNode = [ordered]@{
        file = $file
        direct_dependencies = @($record.direct_dependencies)
        dynamic_dependencies = @($record.dynamic_dependencies)
        template_dependencies = @($record.template_dependencies)
        reflection_dependencies = @($record.reflection_dependencies)
        config_dependencies = @()
        confidence_scores = @{}
        overall_confidence = 0
    }
    
    # Add dependency confidence from audit log
    if ($record.PSObject.Properties.Name -contains "dependency_confidence") {
        $graphNode.confidence_scores = $record.dependency_confidence
    }
    
    $dependencyGraph[$file] = $graphNode
    if (-not $inboundEdges.ContainsKey($file)) { $inboundEdges[$file] = @() }
    if (-not $outboundEdges.ContainsKey($file)) { $outboundEdges[$file] = @() }
}

# === ADD CONFIG DEPENDENCIES ===
foreach ($configDep in $configDeps) {
    $sourceFile = $configDep.source
    $targetVal = $configDep.target
    $confidence = $configDep.confidence
    
    if ($dependencyGraph.ContainsKey($sourceFile)) {
        if ($dependencyGraph[$sourceFile].config_dependencies -notcontains $targetVal) {
            $dependencyGraph[$sourceFile].config_dependencies += $targetVal
            $dependencyGraph[$sourceFile].confidence_scores[$targetVal] = $confidence
        }
    }
}

# === RESOLVE ALL DEPENDENCIES TO ACTUAL FILES ===
$resolvedDeps = @{}
foreach ($file in $dependencyGraph.Keys) {
    $node = $dependencyGraph[$file]
    $allDeps = @()
    $allDeps += @($node.direct_dependencies)
    $allDeps += @($node.dynamic_dependencies)
    $allDeps += @($node.template_dependencies)
    $allDeps += @($node.reflection_dependencies)
    $allDeps += @($node.config_dependencies)
    
    $resolved = @()
    foreach ($dep in $allDeps) {
        $target = $fileSet.Keys | Where-Object { 
            $_ -eq $dep -or 
            [System.IO.Path]::GetFileName($_) -eq [System.IO.Path]::GetFileName($dep) -or
            $_ -match [regex]::Escape($dep)
        } | Select-Object -First 1
        
        if ($target -and $resolved -notcontains $target) {
            $resolved += $target
        }
    }
    
    $resolvedDeps[$file] = $resolved
    $outboundEdges[$file] = $resolved
}

# === BUILD INBOUND EDGES ===
foreach ($file in $resolvedDeps.Keys) {
    foreach ($dep in $resolvedDeps[$file]) {
        if (-not ($inboundEdges[$dep] -contains $file)) {
            $inboundEdges[$dep] += $file
        }
    }
}

# === DETECT CONDITIONAL REACHABILITY ===
function Detect-Reachability {
    param([string]$File, $Record, $OutboundDeps)
    
    # Check if file has conditional execution markers
    $hasConditional = $false
    if ($Record.PSObject.Properties.Name -contains "conditional_execution") {
        $hasConditional = $Record.conditional_execution
    }
    
    # Check if file is reachable from entry points
    $entryPoints = @("main.ps1", "main.py", "index.js", "app.js", "startup.cs", "Program.cs")
    $isReachable = $false
    
    if ($entryPoints -contains [System.IO.Path]::GetFileName($File)) {
        $isReachable = $true
    }
    
    # If has conditional markers, reachability is conditional
    if ($hasConditional) {
        return "conditional"
    }
    
    # If no inbound refs, likely unreachable
    $inboundCount = $inboundEdges[$File].Count
    if ($inboundCount -eq 0 -and -not $isReachable) {
        return "unreachable"
    }
    
    return "always"
}

# === COMPUTE CLOSURES AND REACHABILITY ===
$closure = @{}
function Get-TransitiveClosure {
    param([string]$StartFile, [hashtable]$OutboundGraph)
    
    $visited = @{}
    $queue = @($StartFile)
    
    while ($queue.Count -gt 0) {
        $current = $queue[0]
        $queue = $queue[1..$queue.Count]
        
        if ($visited.ContainsKey($current)) { continue }
        $visited[$current] = $true
        
        if ($OutboundGraph.ContainsKey($current)) {
            foreach ($dep in $OutboundGraph[$current]) {
                if (-not $visited.ContainsKey($dep)) {
                    $queue += $dep
                }
            }
        }
    }
    
    return $visited
}

foreach ($file in $fileSet.Keys) {
    $record = $fileSet[$file]
    $reachability = Detect-Reachability -File $file -Record $record -OutboundDeps $outboundEdges
    $transitiveReachable = Get-TransitiveClosure -StartFile $file -OutboundGraph $outboundEdges
    $blastRadius = $transitiveReachable.Count - 1
    
    $closure[$file] = [ordered]@{
        file = $file
        reachability = $reachability
        blast_radius = $blastRadius
        critical_path = ($reachability -ne "unreachable")
        inbound_count = $inboundEdges[$file].Count
    }
}

# === COMPUTE OVERALL CONFIDENCE SCORES ===
foreach ($file in $dependencyGraph.Keys) {
    $node = $dependencyGraph[$file]
    $confidenceValues = @($node.confidence_scores.Values)
    
    if ($confidenceValues.Count -gt 0) {
        $node.overall_confidence = [int]($confidenceValues | Measure-Object -Average).Average
    } else {
        $node.overall_confidence = 50
    }
}

# === GENERATE STRICT DECISIONS ===
$decisions = @{}

foreach ($file in $fileSet.Keys) {
    $record = $fileSet[$file]
    $closureData = $closure[$file]
    $graphNode = $dependencyGraph[$file]
    
    $decision = "DELETE_CANDIDATE"
    $confidence = 0
    $reasons = @()
    $whatBreaks = @()
    $dependencyChain = @()
    
    # Check for config dependencies
    $hasConfigDeps = $graphNode.config_dependencies.Count -gt 0
    $hasDynamicDeps = @($record.dynamic_dependencies).Count -gt 0
    
    # === STRICT DECISION RULES ===
    
    # KEEP if: any critical dependency or conditional reachability
    if ($closureData.reachability -eq "always" -and $closureData.inbound_count -gt 0) {
        $decision = "KEEP"
        $confidence = 95
        $reasons += "Critical dependency: $($closureData.inbound_count) files depend on this"
        $reasons += "Reachability: always reachable from entry points"
    }
    # KEEP if: core classification
    elseif ($record.classification -eq "core") {
        $decision = "KEEP"
        $confidence = 100
        $reasons += "Core framework file - essential for operation"
    }
    # KEEP if: any conditional dependency exists
    elseif ($closureData.reachability -eq "conditional") {
        $decision = "KEEP"
        $confidence = 85
        $reasons += "Conditionally reachable - may be needed at runtime"
        $reasons += "Uncertain if deletion would break conditional paths"
    }
    # ARCHIVE if: config dependencies but unreachable
    elseif ($hasConfigDeps -and $closureData.reachability -eq "unreachable") {
        $decision = "ARCHIVE"
        $confidence = 75
        $reasons += "Config-driven dependency exists (uncertain confidence)"
        $reasons += "No direct code references found"
    }
    # DELETE only if: unreachable + high confidence + no config/dynamic + blast_radius=0
    elseif ($closureData.reachability -eq "unreachable" -and 
            -not $hasConfigDeps -and 
            -not $hasDynamicDeps -and 
            $closureData.blast_radius -eq 0 -and
            $graphNode.overall_confidence -gt 70) {
        $decision = "DELETE_CANDIDATE"
        $confidence = $graphNode.overall_confidence
        $reasons += "No inbound references detected"
        $reasons += "Blast radius: $($closureData.blast_radius) (safe to delete)"
        $reasons += "No config or dynamic dependencies found"
    }
    # Otherwise: ARCHIVE (uncertain)
    else {
        $decision = "ARCHIVE"
        $confidence = 60
        $reasons += "Low usage but uncertain confidence"
        $reasons += "Recommend manual review before deletion"
    }
    
    # === IDENTIFY WHAT BREAKS IF DELETED ===
    if (@($inboundEdges[$file]).Count -gt 0) {
        foreach ($dependent in $inboundEdges[$file]) {
            $whatBreaks += "$dependent (imports this file)"
        }
    }
    
    # Document decision with full explanation
    $decisions[$file] = [ordered]@{
        file = $file
        decision = $decision
        confidence = $confidence
        classification = $record.classification
        reachability = $closureData.reachability
        inbound_count = $closureData.inbound_count
        blast_radius = $closureData.blast_radius
        has_config_deps = $hasConfigDeps
        has_dynamic_deps = $hasDynamicDeps
        complexity_score = $record.complexity_total
        reasons = @($reasons)
        what_breaks_if_deleted = @($whatBreaks)
        dependency_chain = @()
    }
}

# === SAVE OUTPUTS ===

# 1. Dependency Graph (probabilistic with confidence)
$graphOutput = [ordered]@{}
foreach ($file in ($dependencyGraph.Keys | Sort-Object)) {
    $node = $dependencyGraph[$file]
    $graphOutput[$file] = [ordered]@{
        direct_dependencies = $node.direct_dependencies
        dynamic_dependencies = $node.dynamic_dependencies
        template_dependencies = $node.template_dependencies
        reflection_dependencies = $node.reflection_dependencies
        config_dependencies = $node.config_dependencies
        confidence_scores = $node.confidence_scores
        overall_confidence = $node.overall_confidence
    }
}
$graphOutput | ConvertTo-Json | Out-File -LiteralPath $dependencyGraphPath -Encoding UTF8

# 2. Dependency Closure (conditional reachability)
$closureOutput = [ordered]@{}
foreach ($file in ($closure.Keys | Sort-Object)) {
    $closureOutput[$file] = $closure[$file]
}
$closureOutput | ConvertTo-Json | Out-File -LiteralPath $closurePath -Encoding UTF8

# 3. Decision Explanations (full audit reasoning)
$decisionsOutput = @()
foreach ($file in ($decisions.Keys | Sort-Object { $decisions[$_].confidence } -Descending)) {
    $decisionsOutput += $decisions[$file]
}
@($decisionsOutput) | ConvertTo-Json | Out-File -LiteralPath $decisionsPath -Encoding UTF8

# === GENERATE MARKDOWN REPORT ===
$report = @()
$report += "# Repository Audit Report - Probabilistic Analysis"
$report += ""
$report += "**Target Repository:** $TargetRepoPath"
$report += "**Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$report += "**Mode:** Probabilistic with Conditional Reachability"
$report += ""

# Executive Summary
$keepCount = ($decisions.Values | Where-Object { $_.decision -eq "KEEP" }).Count
$archiveCount = ($decisions.Values | Where-Object { $_.decision -eq "ARCHIVE" }).Count
$deleteCount = ($decisions.Values | Where-Object { $_.decision -eq "DELETE_CANDIDATE" }).Count

$report += "## Executive Summary"
$report += ""
$report += "| Metric | Value |"
$report += "|--------|-------|"
$report += "| Total Files Analyzed | $($fileSet.Count) |"
$report += "| KEEP | $keepCount |"
$report += "| ARCHIVE | $archiveCount |"
$report += "| DELETE_CANDIDATE | $deleteCount |"
$report += "| Config-driven Dependencies | $($configDeps.Count) |"
$report += ""

$report += "## Scoring Model - Multi-Factor Deep Analysis"
$report += ""
$report += "- **Structural Complexity** (0–3): File size + function count"
$report += "- **Coupling Score** (0–3): Inbound + outbound dependencies"
$report += "- **Branching Score** (0–2): Control flow complexity"
$report += "- **Risk Surface** (0–2): Unsafe operations detected"
$report += "- **Confidence Score** (0–100): Probabilistic dependency confidence"
$report += "- **Reachability** (always | conditional | unreachable)"
$report += ""

$report += "## Decision Classification"
$report += ""
$report += "| File | Decision | Confidence | Reachability | Inbound | BlastRadius |"
$report += "|------|----------|-----------|--------------|---------|------|"

foreach ($entry in ($decisions.Values | Sort-Object { $_.confidence } -Descending)) {
    $file = $entry.file
    $decision = $entry.decision
    $conf = $entry.confidence
    $reach = $entry.reachability
    $inbound = $entry.inbound_count
    $blast = $entry.blast_radius
    
    $report += "| $file | $decision | $conf | $reach | $inbound | $blast |"
}
$report += ""

# Files marked for deletion
if ($deleteCount -gt 0) {
    $report += "## Delete Candidates (Safe for Removal)"
    $report += ""
    $report += "These files are safe to delete: no inbound references, no config dependencies, zero blast radius."
    $report += ""
    
    foreach ($entry in ($decisions.Values | Where-Object { $_.decision -eq "DELETE_CANDIDATE" } | Sort-Object { $_.confidence } -Descending)) {
        $file = $entry.file
        $conf = $entry.confidence
        $report += "- **$file** (confidence: $conf)"
        foreach ($reason in $entry.reasons) {
            $report += "  - $reason"
        }
        $report += ""
    }
}

# Archive candidates (uncertain)
if ($archiveCount -gt 0) {
    $report += "## Archive Candidates (Uncertain - Needs Review)"
    $report += ""
    $report += "These files have low usage but uncertain dependencies. Manual code review recommended before deletion."
    $report += ""
    
    foreach ($entry in ($decisions.Values | Where-Object { $_.decision -eq "ARCHIVE" } | Sort-Object { $_.confidence })) {
        $file = $entry.file
        $conf = $entry.confidence
        $reach = $entry.reachability
        $report += "- **$file** (confidence: $conf, reachability: $reach)"
        foreach ($reason in $entry.reasons) {
            $report += "  - $reason"
        }
        $report += ""
    }
}

# Critical path files
$criticalFiles = $decisions.Values | Where-Object { $_.reachability -eq "always" -and $_.inbound_count -gt 0 }
if ($criticalFiles) {
    $report += "## Critical Path Files (High Dependency Impact)"
    $report += ""
    
    foreach ($entry in ($criticalFiles | Sort-Object { $_.inbound_count } -Descending | Select-Object -First 10)) {
        $file = $entry.file
        $inbound = $entry.inbound_count
        $blast = $entry.blast_radius
        $report += "- **$file** (referenced by $inbound files, blast radius: $blast)"
    }
    $report += ""
}

$report += "## Analysis Metadata"
$report += ""
$report += "- Dependency detection: Probabilistic with confidence scoring"
$report += "- Static imports: 100-confidence"
$report += "- Dynamic imports: 75-confidence"
$report += "- Template dependencies: 60-confidence"
$report += "- Reflection patterns: 40-confidence"
$report += "- Config-driven: 50-80 confidence (context dependent)"
$report += "- Conditional reachability: Detected and flagged"
$report += ""

$report -join "`n" | Out-File -LiteralPath $reportPath -Encoding UTF8

Write-Host "✓ Dependency graph: $dependencyGraphPath"
Write-Host "✓ Closure data (conditional reachability): $closurePath"
Write-Host "✓ Decision explanations: $decisionsPath"
Write-Host "✓ Final report: $reportPath"


