[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$AuditLogPath,
    [string]$DependencyMapPath,
    [string]$ReportPath,
    [string]$ClosurePath = "",
    [string]$SafeDeletePath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($EngineRoot)) {
    throw "EngineRoot parameter is required"
}
if ([string]::IsNullOrWhiteSpace($TargetRepoPath)) {
    throw "TargetRepoPath parameter is required"
}

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path

if ([string]::IsNullOrWhiteSpace($AuditLogPath)) {
    $AuditLogPath = Join-Path $EngineRoot "state\audit_log.jsonl"
}
if ([string]::IsNullOrWhiteSpace($DependencyMapPath)) {
    $DependencyMapPath = Join-Path $EngineRoot "state\dependency_map.json"
}
if ([string]::IsNullOrWhiteSpace($ReportPath)) {
    $ReportPath = Join-Path $EngineRoot "output\final_report.md"
}
if ([string]::IsNullOrWhiteSpace($ClosurePath)) {
    $ClosurePath = Join-Path $EngineRoot "state\dependency_closure.json"
}
if ([string]::IsNullOrWhiteSpace($SafeDeletePath)) {
    $SafeDeletePath = Join-Path $EngineRoot "output\safe_delete_order.json"
}

if (-not (Test-Path -LiteralPath $AuditLogPath)) {
    throw "Audit log missing: $AuditLogPath"
}

New-Item -ItemType Directory -Path (Split-Path -Parent $DependencyMapPath) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $ReportPath) -Force | Out-Null

# Parse JSONL audit log
$records = @()
foreach ($line in [System.IO.File]::ReadLines($AuditLogPath)) {
    $line = $line.Trim()
    if ($line.Length -eq 0) { continue }
    
    try {
        $record = $line | ConvertFrom-Json
        $records += $record
    }
    catch {
        Write-Warning "Failed to parse JSON line: $line"
    }
}

Write-Host "Parsed $($records.Count) audit records"

# Build dependency graph with both direct and dynamic dependencies
$fileSet = @{}
$inboundEdges = @{}
$outboundEdges = @{}

foreach ($record in $records) {
    $file = $record.file
    $fileSet[$file] = $record
    if (-not $inboundEdges.ContainsKey($file)) { $inboundEdges[$file] = @() }
    if (-not $outboundEdges.ContainsKey($file)) { $outboundEdges[$file] = @() }
}

# Resolve imports (direct + dynamic) to actual files
$resolvedDeps = @{}
foreach ($record in $records) {
    $file = $record.file
    $allImports = @($record.direct_dependencies) + @($record.suspected_dynamic_dependencies)
    $resolved = @()
    
    foreach ($imp in $allImports) {
        # Try exact match
        $target = $fileSet.Keys | Where-Object { $_ -eq $imp } | Select-Object -First 1
        
        # Try basename match
        if (-not $target) {
            $impBasename = [System.IO.Path]::GetFileName($imp)
            $target = $fileSet.Keys | Where-Object { [System.IO.Path]::GetFileName($_) -eq $impBasename } | Select-Object -First 1
        }
        
        # Try partial path match
        if (-not $target) {
            $target = $fileSet.Keys | Where-Object { $_ -match [regex]::Escape($imp) } | Select-Object -First 1
        }
        
        if ($target -and $resolved -notcontains $target) {
            $resolved += $target
        }
    }
    
    $resolvedDeps[$file] = $resolved
    $outboundEdges[$file] = $resolved
}

# Build inbound edges
foreach ($file in $resolvedDeps.Keys) {
    foreach ($dep in $resolvedDeps[$file]) {
        if (-not ($inboundEdges[$dep] -contains $file)) {
            $inboundEdges[$dep] += $file
        }
    }
}

# Compute transitive closure and blast radius
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

function Get-InboundTransitiveClosure {
    param([string]$StartFile, [hashtable]$InboundGraph)
    
    $visited = @{}
    $queue = @($StartFile)
    
    while ($queue.Count -gt 0) {
        $current = $queue[0]
        $queue = $queue[1..$queue.Count]
        
        if ($visited.ContainsKey($current)) { continue }
        $visited[$current] = $true
        
        if ($InboundGraph.ContainsKey($current)) {
            foreach ($dep in $InboundGraph[$current]) {
                if (-not $visited.ContainsKey($dep)) {
                    $queue += $dep
                }
            }
        }
    }
    
    return $visited
}

# Entry points (main, index, app, startup patterns)
$entryPoints = @()
foreach ($file in $fileSet.Keys) {
    $lower = $file.ToLowerInvariant()
    if ($lower -match '(^|/)(main|index|app|startup|bootstrap)(\.|/)') {
        $entryPoints += $file
    }
}

# Compute closure for each file
$closure = @{}
foreach ($file in $fileSet.Keys) {
    $directDeps = $resolvedDeps[$file].Count
    $blastRadius = (Get-TransitiveClosure -StartFile $file -OutboundGraph $outboundEdges).Count - 1
    
    # Check if reachable from KEEP (assuming core files are entry points)
    $reachableFromKeep = $false
    foreach ($entry in $entryPoints) {
        $entryTransitive = Get-TransitiveClosure -StartFile $entry -OutboundGraph $outboundEdges
        if ($entryTransitive.ContainsKey($file)) {
            $reachableFromKeep = $true
            break
        }
    }
    
    $onCriticalPath = $false
    if ($entryPoints -contains $file) {
        $onCriticalPath = $true
    } else {
        foreach ($entry in $entryPoints) {
            $entryTransitive = Get-TransitiveClosure -StartFile $entry -OutboundGraph $outboundEdges
            if ($entryTransitive.ContainsKey($file)) {
                $onCriticalPath = $true
                break
            }
        }
    }
    
    $closure[$file] = @{
        reachable_from_keep = $reachableFromKeep
        blast_radius = $blastRadius
        critical_path = $onCriticalPath
        inbound_count = $inboundEdges[$file].Count
    }
}

# Save dependency_closure.json
$closureJson = @{}
foreach ($file in ($fileSet.Keys | Sort-Object)) {
    $closureJson[$file] = $closure[$file]
}
$closureJson | ConvertTo-Json | Out-File -LiteralPath $ClosurePath -Encoding UTF8

# Deterministic scoring: 0-4 deps + 0-3 usage + 0-2 complexity + 0-1 risk
$fileScores = @{}
foreach ($file in $fileSet.Keys) {
    $record = $fileSet[$file]
    $closeData = $closure[$file]
    
    # dependency_score (0–4): based on inbound edges (higher usage = higher score)
    $maxInbound = ($inboundEdges.Values | ForEach-Object { $_.Count } | Measure-Object -Maximum).Maximum
    $depScore = if ($maxInbound -gt 0) { [Math]::Min(4, ($closeData.inbound_count / $maxInbound) * 4) } else { 0 }
    
    # usage_score (0–3): based on outbound dependencies required
    $outDeps = $resolvedDeps[$file].Count
    $maxOutDeps = ($resolvedDeps.Values | ForEach-Object { $_.Count } | Measure-Object -Maximum).Maximum
    $usageScore = if ($maxOutDeps -gt 0) { [Math]::Min(3, ($outDeps / $maxOutDeps) * 3) } else { 0 }
    
    # complexity_score (0–2): file size + function count
    $fileSizeScore = if ($record.file_size_kb -gt 50) { 1 } else { 0 }
    $funcScore = if ($record.function_count -gt 10) { 1 } else { 0 }
    $complexityScore = [Math]::Min(2, $fileSizeScore + $funcScore)
    
    # risk_flags_score (0–1): hardcoded rules only
    $riskScore = $record.risk_flags_score
    
    # Deterministic final score
    $finalScore = $depScore + $usageScore + $complexityScore + $riskScore
    $finalScore = [Math]::Round($finalScore, 2)
    
    # Decision rules (DETERMINISTIC, NO SUBJECTIVITY)
    $decision = "DELETE_CANDIDATE"
    
    # KEEP if: core classification, on critical path, high dependence, or score >= 6
    if ($record.classification -eq "core" -or $closeData.critical_path -or $closeData.inbound_count -ge 3 -or $finalScore -ge 6) {
        $decision = "KEEP"
    }
    # ARCHIVE if: no inbound refs and score >= 2
    elseif ($closeData.inbound_count -eq 0 -and $finalScore -ge 2) {
        $decision = "ARCHIVE"
    }
    
    $fileScores[$file] = @{
        record = $record
        score = $finalScore
        decision = $decision
        inbound = $closeData.inbound_count
        blast_radius = $closeData.blast_radius
        critical_path = $closeData.critical_path
        reachable_from_keep = $closeData.reachable_from_keep
    }
}

# SAFETY CHECK: Ensure no DELETE_CANDIDATE that is imported by KEEP files
$keepFiles = @($fileScores.Keys | Where-Object { $fileScores[$_].decision -eq "KEEP" })
foreach ($file in $fileScores.Keys) {
    if ($fileScores[$file].decision -eq "DELETE_CANDIDATE") {
        # Check if any KEEP file depends on this (transitively)
        $isDepOfKeep = $false
        foreach ($keepFile in $keepFiles) {
            $keepClosure = Get-TransitiveClosure -StartFile $keepFile -OutboundGraph $outboundEdges
            if ($keepClosure.ContainsKey($file)) {
                $isDepOfKeep = $true
                break
            }
        }
        
        if ($isDepOfKeep) {
            $fileScores[$file].decision = "KEEP"
            $fileScores[$file].score = [Math]::Max($fileScores[$file].score, 3)
        }
    }
}

# Generate safe_delete_order.json - sorted for safe sequential deletion
$deleteOrder = @()
$candidates = $fileScores.Keys | Where-Object { $fileScores[$_].decision -eq "DELETE_CANDIDATE" }
$candidates = $candidates | Sort-Object { $fileScores[$_].inbound }, { $fileScores[$_].blast_radius }

foreach ($file in $candidates) {
    $deleteOrder += @{
        file = $file
        score = $fileScores[$file].score
        reason = "no_inbound_refs, low_blast_radius"
    }
}

@($deleteOrder) | ConvertTo-Json | Out-File -LiteralPath $SafeDeletePath -Encoding UTF8

# Generate dependency_map.json
$depMap = [ordered]@{}
foreach ($file in ($fileSet.Keys | Sort-Object)) {
    $depMap[$file] = @($resolvedDeps[$file] | Sort-Object)
}
$depMap | ConvertTo-Json | Out-File -LiteralPath $DependencyMapPath -Encoding UTF8

# Generate final_report.md
$report = @()
$report += "# Repository Audit Report (Deterministic Mode)"
$report += ""
$report += "**Target Repository:** $TargetRepoPath"
$report += "**Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$report += ""

# Executive summary
$keepCount = ($fileScores.Values | Where-Object { $_.decision -eq "KEEP" }).Count
$archiveCount = ($fileScores.Values | Where-Object { $_.decision -eq "ARCHIVE" }).Count
$deleteCount = ($fileScores.Values | Where-Object { $_.decision -eq "DELETE_CANDIDATE" }).Count

$report += "## Executive Summary"
$report += ""
$report += "| Metric | Value |"
$report += "|--------|-------|"
$report += "| Total Files Analyzed | $($fileSet.Count) |"
$report += "| KEEP | $keepCount |"
$report += "| ARCHIVE | $archiveCount |"
$report += "| DELETE_CANDIDATE | $deleteCount |"
$report += ""

# Health assessment
if ($deleteCount -gt ($fileSet.Count * 0.5)) {
    $health = "⚠️ needs_attention"
} elseif ($deleteCount -gt ($fileSet.Count * 0.2)) {
    $health = "⚠️ acceptable"
} else {
    $health = "✓ healthy"
}
$report += "**Repository Health:** $health"
$report += ""

$report += "## Scoring Model (Deterministic)"
$report += ""
$report += "- **dependency_score** (0–4): Inbound reference count"
$report += "- **usage_score** (0–3): Outbound imports required"
$report += "- **complexity_score** (0–2): File size + function count"
$report += "- **risk_flags_score** (0–1): Unsafe operations detected"
$report += "- **Final Score** = Sum of above (max 10.0)"
$report += ""

# File classification table
$report += "## File Classification"
$report += ""
$report += "| File | Score | Decision | Inbound | BlastRadius | CriticalPath |"
$report += "|------|-------|----------|---------|------|------|"

$sorted = $fileScores.GetEnumerator() | Sort-Object { $_.Value.score } -Descending
foreach ($entry in $sorted) {
    $file = $entry.Key
    $info = $entry.Value
    $score = $info.score
    $decision = $info.decision
    $inbound = $info.inbound
    $blastRad = $info.blast_radius
    $critPath = if ($info.critical_path) { "✓" } else { "" }
    
    $report += "| $file | $score | $decision | $inbound | $blastRad | $critPath |"
}

$report += ""

# Safe delete order
if ($deleteCount -gt 0) {
    $report += "## Safe Deletion Order"
    $report += ""
    $report += "Files can be safely deleted in this order (won't break dependencies):"
    $report += ""
    foreach ($item in $deleteOrder) {
        $report += "- **$($item.file)** (score: $($item.score))"
    }
    $report += ""
}

# Critical path files
$criticalFiles = $fileScores.GetEnumerator() | Where-Object { $_.Value.critical_path }
if ($criticalFiles) {
    $report += "## Critical Path Files"
    $report += ""
    $report += "Entry points and high-value dependency chains:"
    $report += ""
    foreach ($entry in ($criticalFiles | Sort-Object { $_.Value.blast_radius } -Descending)) {
        $file = $entry.Key
        $blastRad = $entry.Value.blast_radius
        $report += "- **$file** (blast radius: $blastRad)"
    }
    $report += ""
}

$report += "## Analysis Metadata"
$report += ""
$report += "- Scoring algorithm: Deterministic (no ML/LLM)"
$report += "- Entry points detected: $($entryPoints.Count)"
$report += "- Files on critical path: $(($fileScores.Values | Where-Object { $_.critical_path }).Count)"
$report += "- Total edges: $(($resolvedDeps.Values | ForEach-Object { $_.Count } | Measure-Object -Sum).Sum)"
$report += ""

$report -join "`n" | Out-File -LiteralPath $ReportPath -Encoding UTF8

Write-Host "Dependency map: $DependencyMapPath"
Write-Host "Closure data: $ClosurePath"
Write-Host "Safe delete order: $SafeDeletePath"
Write-Host "Final report: $ReportPath"
