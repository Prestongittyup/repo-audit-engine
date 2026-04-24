[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$RunOutputDir,
    [string]$EngineStateDir,
    [string]$RunMetadataPath,
    [string]$IndexPath,
    [string]$AuditLogPath,
    [string]$ConfigDepsPath,
    [switch]$CI_MODE = $false
)

$ErrorActionPreference = "Stop"

$runtimeCommon = Join-Path $EngineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon)) {
    throw "Missing script: $runtimeCommon"
}
. $runtimeCommon
Set-AuditCiMode -Enabled ([bool]$CI_MODE)

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path

if ([string]::IsNullOrWhiteSpace($RunOutputDir)) {
    throw "RunOutputDir parameter is required"
}
if ([string]::IsNullOrWhiteSpace($EngineStateDir)) {
    $EngineStateDir = Join-Path $TargetRepoPath "engine_state"
}

$RUN_OUTPUT_DIR = [System.IO.Path]::GetFullPath($RunOutputDir)
$ENGINE_STATE_DIR = [System.IO.Path]::GetFullPath($EngineStateDir)

if ([string]::IsNullOrWhiteSpace($IndexPath)) {
    $IndexPath = Join-Path $RUN_OUTPUT_DIR "index.json"
}
if ([string]::IsNullOrWhiteSpace($AuditLogPath)) {
    $AuditLogPath = Join-Path $RUN_OUTPUT_DIR "audit_log.jsonl"
}
if ([string]::IsNullOrWhiteSpace($ConfigDepsPath)) {
    $ConfigDepsPath = Join-Path $RUN_OUTPUT_DIR "config_dependencies.json"
}
if ([string]::IsNullOrWhiteSpace($RunMetadataPath)) {
    $RunMetadataPath = Join-Path $RUN_OUTPUT_DIR 'run_metadata.json'
}

New-Item -ItemType Directory -Path $RUN_OUTPUT_DIR -Force | Out-Null
New-Item -ItemType Directory -Path $ENGINE_STATE_DIR -Force | Out-Null

$truthGraphPath = Join-Path $RUN_OUTPUT_DIR "dependency_truth_graph.json"
$closurePath = Join-Path $RUN_OUTPUT_DIR "dependency_closure.json"
$architecturePath = Join-Path $RUN_OUTPUT_DIR "architecture_analysis.json"
$deadCodePath = Join-Path $RUN_OUTPUT_DIR "dead_code_report.json"
$healthPath = Join-Path $RUN_OUTPUT_DIR "system_health_score.json"
$contradictionsPath = Join-Path $RUN_OUTPUT_DIR "contradictions.json"
$explanationsPath = Join-Path $RUN_OUTPUT_DIR "audit_explanations.json"
$reportPath = Join-Path $RUN_OUTPUT_DIR "final_report.md"

if (-not (Test-Path -LiteralPath $IndexPath)) { throw "Codebase index missing: $IndexPath" }
if (-not (Test-Path -LiteralPath $AuditLogPath)) { throw "Audit log missing: $AuditLogPath" }

$runMetadata = Read-RunMetadata -Path $RunMetadataPath
$index = Read-JsonArtifact -Path $IndexPath -Depth 12
$fileIndex = @{}
$pathLookup = @{}
$stemLookup = @{}
foreach ($entry in @($index.files)) {
    $fileIndex[$entry.file] = $entry
    $pathLookup[$entry.file.ToLowerInvariant()] = $entry.file
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($entry.file).ToLowerInvariant()
    if (-not $stemLookup.ContainsKey($stem)) { $stemLookup[$stem] = New-Object System.Collections.Generic.List[string] }
    $stemLookup[$stem].Add($entry.file)
}

$records = @{}
$exportLookup = @{}
foreach ($line in [System.IO.File]::ReadLines($AuditLogPath)) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0) { continue }
    $record = $trimmed | ConvertFrom-Json -Depth 8
    $records[$record.file] = $record
    foreach ($export in @($record.exports)) {
        $key = [string]$export
        if ($key.Length -eq 0) { continue }
        if (-not $exportLookup.ContainsKey($key)) { $exportLookup[$key] = New-Object System.Collections.Generic.List[string] }
        $exportLookup[$key].Add($record.file)
    }
}

$configEdges = @()
if (Test-Path -LiteralPath $ConfigDepsPath) {
    try {
        $configRaw = Read-JsonArtifact -Path $ConfigDepsPath -Depth 12
        if ($null -ne $configRaw) {
            $configEdges = $configRaw
            if ($configEdges -isnot [array]) { $configEdges = @($configEdges) }
        }
    }
    catch {
        $configEdges = @()
    }
}

function Add-ResolvedEdge {
    param(
        [hashtable]$Store,
        [string]$Layer,
        [string]$Source,
        [string]$Target,
        [int]$Confidence,
        [string]$Evidence
    )

    if ([string]::IsNullOrWhiteSpace($Target)) { return }
    if ($Source -eq $Target) { return }

    $layerStore = $Store[$Layer]
    if (-not $layerStore.ContainsKey($Source)) {
        $layerStore[$Source] = New-Object 'System.Collections.Generic.Dictionary[string, object]'
    }

    $sourceStore = $layerStore[$Source]
    if (-not $sourceStore.ContainsKey($Target)) {
        $sourceStore[$Target] = [ordered]@{
            target = $Target
            confidence = $Confidence
            evidence = New-Object System.Collections.Generic.List[string]
        }
    }

    $edge = $sourceStore[$Target]
    if ($Confidence -gt $edge.confidence) {
        $edge.confidence = $Confidence
    }
    if (-not $edge.evidence.Contains($Evidence)) {
        $edge.evidence.Add($Evidence)
    }
}

function Resolve-Reference {
    param(
        [string]$SourceFile,
        [string]$Reference,
        [hashtable]$FileIndex,
        [hashtable]$StemLookup,
        [hashtable]$ExportLookup
    )

    if ([string]::IsNullOrWhiteSpace($Reference)) { return $null }

    $normalized = $Reference.Trim().Replace('\\', '/').TrimStart('./').Trim()
    if ($normalized.Length -eq 0) { return $null }

    $sourceAbsolute = Join-Path $TargetRepoPath ($SourceFile.Replace('/', '\'))
    $sourceDirAbsolute = [System.IO.Path]::GetDirectoryName($sourceAbsolute)
    if (-not [string]::IsNullOrWhiteSpace($sourceDirAbsolute)) {
        $candidateAbsolute = [System.IO.Path]::GetFullPath((Join-Path $sourceDirAbsolute $normalized))
        $repoRootWithSep = $TargetRepoPath.TrimEnd('\') + '\'
        if ($candidateAbsolute.StartsWith($repoRootWithSep, [System.StringComparison]::OrdinalIgnoreCase) -or $candidateAbsolute.Equals($TargetRepoPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            $combined = [System.IO.Path]::GetRelativePath($TargetRepoPath, $candidateAbsolute).Replace('\\', '/')
            if ($FileIndex.ContainsKey($combined)) { return $combined }
        }
    }

    if ($FileIndex.ContainsKey($normalized)) { return $normalized }

    $stem = [System.IO.Path]::GetFileNameWithoutExtension($normalized).ToLowerInvariant()
    if ($StemLookup.ContainsKey($stem)) {
        return ($StemLookup[$stem] | Select-Object -First 1)
    }

    if ($ExportLookup.ContainsKey($Reference)) {
        return ($ExportLookup[$Reference] | Select-Object -First 1)
    }

    return $null
}

function Get-DirectoryDistance {
    param([string]$Left, [string]$Right)

    $leftSegments = [System.IO.Path]::GetDirectoryName($Left.Replace('/', '\')) -split '\\'
    $rightSegments = [System.IO.Path]::GetDirectoryName($Right.Replace('/', '\')) -split '\\'
    $shared = 0
    $limit = [Math]::Min($leftSegments.Count, $rightSegments.Count)
    for ($i = 0; $i -lt $limit; $i++) {
        if ($leftSegments[$i] -ieq $rightSegments[$i]) {
            $shared++
        }
        else {
            break
        }
    }
    return ($leftSegments.Count + $rightSegments.Count - (2 * $shared))
}

$layeredEdges = @{
    static = @{}
    config = @{}
    dynamic = @{}
    heuristic = @{}
}

foreach ($file in $records.Keys) {
    $record = $records[$file]

    foreach ($signal in @($record.static_candidates)) {
        $target = Resolve-Reference -SourceFile $file -Reference $signal.reference -FileIndex $fileIndex -StemLookup $stemLookup -ExportLookup $exportLookup
        if ($target) {
            Add-ResolvedEdge -Store $layeredEdges -Layer 'static' -Source $file -Target $target -Confidence ([int]$signal.confidence) -Evidence ([string]$signal.evidence)
        }
    }

    foreach ($signal in @($record.dynamic_candidates)) {
        $target = Resolve-Reference -SourceFile $file -Reference $signal.reference -FileIndex $fileIndex -StemLookup $stemLookup -ExportLookup $exportLookup
        if ($target) {
            Add-ResolvedEdge -Store $layeredEdges -Layer 'dynamic' -Source $file -Target $target -Confidence ([int]$signal.confidence) -Evidence ([string]$signal.evidence)
        }
    }

    foreach ($signal in @($record.heuristic_signals)) {
        $target = Resolve-Reference -SourceFile $file -Reference $signal.reference -FileIndex $fileIndex -StemLookup $stemLookup -ExportLookup $exportLookup
        if (-not $target -and $stemLookup.ContainsKey(([System.IO.Path]::GetFileNameWithoutExtension($signal.reference).ToLowerInvariant()))) {
            $candidate = $stemLookup[[System.IO.Path]::GetFileNameWithoutExtension($signal.reference).ToLowerInvariant()] | Sort-Object { Get-DirectoryDistance -Left $file -Right $_ } | Select-Object -First 1
            $target = $candidate
        }
        if ($target) {
            $distance = Get-DirectoryDistance -Left $file -Right $target
            $confidence = [Math]::Max(10, [Math]::Min(40, [int]$signal.confidence - ($distance * 3)))
            Add-ResolvedEdge -Store $layeredEdges -Layer 'heuristic' -Source $file -Target $target -Confidence $confidence -Evidence ([string]$signal.evidence)
        }
    }
}

foreach ($configEdge in @($configEdges)) {
    $source = $configEdge.source
    if (-not $fileIndex.ContainsKey($source)) { continue }
    $target = Resolve-Reference -SourceFile $source -Reference $configEdge.target -FileIndex $fileIndex -StemLookup $stemLookup -ExportLookup $exportLookup
    if ($target) {
        Add-ResolvedEdge -Store $layeredEdges -Layer 'config' -Source $source -Target $target -Confidence ([int]$configEdge.confidence) -Evidence ([string]$configEdge.kind)
    }
}

function Convert-LayerStoreToArray {
    param([hashtable]$LayerStore, [string]$File)

    if (-not $LayerStore.ContainsKey($File)) { return @() }
    return @($LayerStore[$File].Values | Sort-Object target | ForEach-Object {
        [pscustomobject]@{
            target = $_.target
            confidence = $_.confidence
            evidence = @($_.evidence)
        }
    })
}

function Get-LayerAverageConfidence {
    param([object[]]$Edges)

    $values = @($Edges | ForEach-Object { [int]$_.confidence })
    if ($values.Count -eq 0) { return 0 }
    return [int]([Math]::Round((($values | Measure-Object -Average).Average), 0))
}

$inboundByLayer = @{
    static = @{}
    config = @{}
    dynamic = @{}
    heuristic = @{}
}
foreach ($layer in $layeredEdges.Keys) {
    foreach ($source in $layeredEdges[$layer].Keys) {
        foreach ($target in $layeredEdges[$layer][$source].Keys) {
            if (-not $inboundByLayer[$layer].ContainsKey($target)) {
                $inboundByLayer[$layer][$target] = New-Object System.Collections.Generic.List[string]
            }
            if (-not $inboundByLayer[$layer][$target].Contains($source)) {
                $inboundByLayer[$layer][$target].Add($source)
            }
        }
    }
}

$entryPoints = @($index.files | Where-Object {
    $_.module_type -eq 'core' -or $_.file -match '(^|/)(main|app|index|program|startup|bootstrap|run)\.'
} | ForEach-Object { $_.file })

function Get-ReachabilityState {
    param(
        [string]$File,
        [hashtable]$Records,
        [hashtable]$LayeredEdges,
        [string[]]$EntryPoints
    )

    if ($EntryPoints -contains $File) { return 'always' }

    $alwaysVisited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
    $queue = New-Object System.Collections.Generic.Queue[string]
    foreach ($entry in $EntryPoints) { $queue.Enqueue($entry) }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if ($alwaysVisited.Contains($current)) { continue }
        [void]$alwaysVisited.Add($current)
        foreach ($layer in @('static', 'config')) {
            foreach ($edge in @(Convert-LayerStoreToArray -LayerStore $LayeredEdges[$layer] -File $current)) {
                if (-not $alwaysVisited.Contains($edge.target)) {
                    $queue.Enqueue($edge.target)
                }
            }
        }
    }

    if ($alwaysVisited.Contains($File)) { return 'always' }

    $conditionalVisited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
    $conditionalQueue = New-Object System.Collections.Generic.Queue[string]
    foreach ($entry in $alwaysVisited) { $conditionalQueue.Enqueue($entry) }

    while ($conditionalQueue.Count -gt 0) {
        $current = $conditionalQueue.Dequeue()
        if ($conditionalVisited.Contains($current)) { continue }
        [void]$conditionalVisited.Add($current)
        foreach ($layer in @('dynamic', 'heuristic')) {
            foreach ($edge in @(Convert-LayerStoreToArray -LayerStore $LayeredEdges[$layer] -File $current)) {
                if (-not $conditionalVisited.Contains($edge.target)) {
                    $conditionalQueue.Enqueue($edge.target)
                }
            }
        }
        if ($Records.ContainsKey($current)) {
            $flags = $Records[$current].conditional_flags
            if ($flags -and ($flags.feature_flags -or $flags.env_checks -or $flags.conditional_loading)) {
                foreach ($layer in @('static', 'config', 'dynamic', 'heuristic')) {
                    foreach ($edge in @(Convert-LayerStoreToArray -LayerStore $LayeredEdges[$layer] -File $current)) {
                        if (-not $conditionalVisited.Contains($edge.target)) {
                            $conditionalQueue.Enqueue($edge.target)
                        }
                    }
                }
            }
        }
    }

    if ($conditionalVisited.Contains($File)) { return 'conditional' }
    return 'unreachable'
}

function Get-BlastRadius {
    param([string]$File, [hashtable]$LayeredEdges)

    $visited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
    $queue = New-Object System.Collections.Generic.Queue[string]
    $queue.Enqueue($File)

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if ($visited.Contains($current)) { continue }
        [void]$visited.Add($current)
        foreach ($layer in @('static', 'config', 'dynamic')) {
            foreach ($edge in @(Convert-LayerStoreToArray -LayerStore $LayeredEdges[$layer] -File $current)) {
                if (-not $visited.Contains($edge.target)) {
                    $queue.Enqueue($edge.target)
                }
            }
        }
    }

    return [Math]::Max(0, $visited.Count - 1)
}

$truthGraph = [ordered]@{}
$closure = [ordered]@{}
$allStrongOutbound = @{}
$allStrongInbound = @{}
foreach ($file in $fileIndex.Keys) {
    $staticEdges = @(Convert-LayerStoreToArray -LayerStore $layeredEdges.static -File $file)
    $configLayerEdges = @(Convert-LayerStoreToArray -LayerStore $layeredEdges.config -File $file)
    $dynamicEdges = @(Convert-LayerStoreToArray -LayerStore $layeredEdges.dynamic -File $file)
    $heuristicEdges = @(Convert-LayerStoreToArray -LayerStore $layeredEdges.heuristic -File $file)

    $confidenceProfile = [ordered]@{
        static = Get-LayerAverageConfidence -Edges $staticEdges
        config = Get-LayerAverageConfidence -Edges $configLayerEdges
        dynamic = Get-LayerAverageConfidence -Edges $dynamicEdges
        heuristic = Get-LayerAverageConfidence -Edges $heuristicEdges
    }

    $nonZero = @($confidenceProfile.GetEnumerator() | Where-Object { $_.Value -gt 0 } | ForEach-Object { $_.Value })
    $overallConfidence = if ($nonZero.Count -gt 0) { [int](($nonZero | Measure-Object -Average).Average) } else { 0 }

    $truthGraph[$file] = [ordered]@{
        file = $file
        edges = [ordered]@{
            static = @($staticEdges)
            config = @($configLayerEdges)
            dynamic = @($dynamicEdges)
            heuristic = @($heuristicEdges)
        }
        confidence_profile = $confidenceProfile
        overall_confidence = $overallConfidence
    }

    $reachability = Get-ReachabilityState -File $file -Records $records -LayeredEdges $layeredEdges -EntryPoints $entryPoints
    $blastRadius = Get-BlastRadius -File $file -LayeredEdges $layeredEdges
    $criticalPath = ($reachability -ne 'unreachable') -and (($confidenceProfile.static -gt 0) -or ($confidenceProfile.config -gt 0) -or ($entryPoints -contains $file))
    $closure[$file] = [ordered]@{
        file = $file
        reachability = $reachability
        blast_radius = $blastRadius
        critical_path = $criticalPath
    }

    $strongTargets = New-Object System.Collections.Generic.List[string]
    foreach ($edge in @($staticEdges + $configLayerEdges + $dynamicEdges)) {
        if (-not $strongTargets.Contains($edge.target)) { $strongTargets.Add($edge.target) }
        if (-not $allStrongInbound.ContainsKey($edge.target)) { $allStrongInbound[$edge.target] = New-Object System.Collections.Generic.List[string] }
        if (-not $allStrongInbound[$edge.target].Contains($file)) { $allStrongInbound[$edge.target].Add($file) }
    }
    $allStrongOutbound[$file] = @($strongTargets)
}

foreach ($file in $fileIndex.Keys) {
    if (-not $allStrongInbound.ContainsKey($file)) {
        $allStrongInbound[$file] = New-Object System.Collections.Generic.List[string]
    }
}

function Get-SccClusters {
    param([hashtable]$Graph)

    $script:tarjanIndexCounter = 0
    $script:tarjanStack = New-Object System.Collections.Stack
    $script:tarjanOnStack = @{}
    $script:tarjanIndexMap = @{}
    $script:tarjanLowLink = @{}
    $script:tarjanComponents = @()

    function Invoke-Tarjan {
        param([string]$Node)

        $script:tarjanIndexMap[$Node] = $script:tarjanIndexCounter
        $script:tarjanLowLink[$Node] = $script:tarjanIndexCounter
        $script:tarjanIndexCounter++
        $script:tarjanStack.Push($Node)
        $script:tarjanOnStack[$Node] = $true

        foreach ($next in @($Graph[$Node])) {
            if (-not $script:tarjanIndexMap.ContainsKey($next)) {
                Invoke-Tarjan -Node $next
                $script:tarjanLowLink[$Node] = [Math]::Min($script:tarjanLowLink[$Node], $script:tarjanLowLink[$next])
            }
            elseif ($script:tarjanOnStack.ContainsKey($next) -and $script:tarjanOnStack[$next]) {
                $script:tarjanLowLink[$Node] = [Math]::Min($script:tarjanLowLink[$Node], $script:tarjanIndexMap[$next])
            }
        }

        if ($script:tarjanLowLink[$Node] -eq $script:tarjanIndexMap[$Node]) {
            $members = New-Object System.Collections.Generic.List[string]
            while ($script:tarjanStack.Count -gt 0) {
                $popped = [string]$script:tarjanStack.Pop()
                $script:tarjanOnStack[$popped] = $false
                $members.Add($popped)
                if ($popped -eq $Node) { break }
            }
            if ($members.Count -gt 1) {
                $script:tarjanComponents += ,(@($members | Sort-Object))
            }
        }
    }

    foreach ($node in $Graph.Keys) {
        if (-not $script:tarjanIndexMap.ContainsKey($node)) {
            Invoke-Tarjan -Node $node
        }
    }

    return @($script:tarjanComponents)
}

$clusters = Get-SccClusters -Graph $allStrongOutbound

$architecture = [ordered]@{
    summary = [ordered]@{
        total_files = $index.total_files
        total_entrypoints = $entryPoints.Count
        orphan_modules = @($fileIndex.Keys | Where-Object { $allStrongInbound[$_].Count -eq 0 -and $allStrongOutbound[$_].Count -eq 0 } | Sort-Object)
    }
    layers = [ordered]@{
        core_modules = @($index.files | Where-Object { $_.module_type -eq 'core' } | ForEach-Object { $_.file })
        utility_layers = @($index.files | Where-Object { $_.module_type -eq 'utility' } | ForEach-Object { $_.file })
        config_layers = @($index.files | Where-Object { $_.module_type -eq 'config' } | ForEach-Object { $_.file })
        test_layers = @($index.files | Where-Object { $_.module_type -eq 'test' } | ForEach-Object { $_.file })
        unknown_layers = @($index.files | Where-Object { $_.module_type -eq 'unknown' } | ForEach-Object { $_.file })
    }
    circular_dependency_clusters = @($clusters | ForEach-Object {
        [ordered]@{
            members = $_
            size = $_.Count
        }
    })
    high_coupling_hotspots = @($fileIndex.Keys | ForEach-Object {
        [ordered]@{
            file = $_
            inbound = $allStrongInbound[$_].Count
            outbound = $allStrongOutbound[$_].Count
            total = $allStrongInbound[$_].Count + $allStrongOutbound[$_].Count
        }
    } | Sort-Object total -Descending | Select-Object -First 20)
    architectural_bottlenecks = @($fileIndex.Keys | Where-Object { $allStrongInbound[$_].Count -ge 5 -and $allStrongOutbound[$_].Count -ge 5 } | ForEach-Object {
        [ordered]@{
            file = $_
            inbound = $allStrongInbound[$_].Count
            outbound = $allStrongOutbound[$_].Count
            reason = 'high inbound and outbound dependency concentration'
        }
    })
    god_modules = @($fileIndex.Keys | Where-Object { $allStrongInbound[$_].Count -ge [Math]::Max(5, [int]([Math]::Ceiling($index.total_files * 0.02))) } | ForEach-Object {
        [ordered]@{
            file = $_
            inbound = $allStrongInbound[$_].Count
            reason = 'excessive inbound dependencies'
        }
    })
}

$deadCode = @()
$contradictions = @()
$explanations = @()
$highRiskFiles = @()
$decisionCounts = @{ KEEP = 0; ARCHIVE = 0; DELETE_CANDIDATE = 0 }

foreach ($file in ($fileIndex.Keys | Sort-Object)) {
    $record = $records[$file]
    $truth = $truthGraph[$file]
    $closureData = $closure[$file]
    $strongInboundCount = $allStrongInbound[$file].Count
    $staticInbound = if ($inboundByLayer.static.ContainsKey($file)) { $inboundByLayer.static[$file].Count } else { 0 }
    $configInbound = if ($inboundByLayer.config.ContainsKey($file)) { $inboundByLayer.config[$file].Count } else { 0 }
    $dynamicInbound = if ($inboundByLayer.dynamic.ContainsKey($file)) { $inboundByLayer.dynamic[$file].Count } else { 0 }
    $heuristicInboundEdges = @()
    foreach ($source in $layeredEdges.heuristic.Keys) {
        if ($layeredEdges.heuristic[$source].ContainsKey($file)) {
            $heuristicInboundEdges += $layeredEdges.heuristic[$source][$file]
        }
    }
    $maxHeuristicInbound = if ($heuristicInboundEdges.Count -gt 0) { ($heuristicInboundEdges | Measure-Object -Property confidence -Maximum).Maximum } else { 0 }

    $absenceConfidence = 100
    if ($closureData.reachability -ne 'unreachable') { $absenceConfidence -= 40 }
    if ($fileIndex[$file].module_type -in @('core', 'config', 'test')) { $absenceConfidence -= 25 }
    if ($record.conditional_flags.feature_flags -or $record.conditional_flags.env_checks -or $record.conditional_flags.conditional_loading) { $absenceConfidence -= 15 }
    if ($record.complexity_total -gt 5) { $absenceConfidence -= 10 }
    if (@($record.dynamic_candidates).Count -gt 0) { $absenceConfidence -= 10 }
    $absenceConfidence = [Math]::Max(0, [Math]::Min(100, $absenceConfidence))

    $isDead = ($staticInbound -eq 0) -and ($configInbound -eq 0) -and ($dynamicInbound -eq 0) -and ($maxHeuristicInbound -lt 25) -and ($absenceConfidence -gt 85) -and ($fileIndex[$file].module_type -notin @('core', 'config', 'test'))

    if ($staticInbound -eq 0 -and ($configInbound -gt 0 -or $dynamicInbound -gt 0)) {
        $contradictions += ,([pscustomobject]@{
            file = $file
            type = 'strong_runtime_contradiction'
            detail = 'No inbound static dependencies but runtime/config layers indicate usage'
            evidence = [ordered]@{
                static_inbound = $staticInbound
                config_inbound = $configInbound
                dynamic_inbound = $dynamicInbound
            }
        })
    }
    if (($staticInbound + $configInbound + $dynamicInbound) -eq 0 -and $maxHeuristicInbound -ge 25) {
        $contradictions += ,([pscustomobject]@{
            file = $file
            type = 'heuristic_only_usage'
            detail = 'Only heuristic layer indicates usage; manual review recommended'
            evidence = [ordered]@{
                max_heuristic_inbound = $maxHeuristicInbound
            }
        })
    }

    $decision = 'ARCHIVE'
    $decisionConfidence = [Math]::Max(55, $truth.overall_confidence)
    $supportingLayers = New-Object System.Collections.Generic.List[string]
    foreach ($layerName in @('static', 'config', 'dynamic', 'heuristic')) {
        if ($truth.confidence_profile[$layerName] -gt 0) { $supportingLayers.Add($layerName) }
    }

    if ($fileIndex[$file].module_type -in @('core', 'config', 'test') -or $strongInboundCount -gt 0 -or $closureData.reachability -ne 'unreachable') {
        $decision = 'KEEP'
        $decisionConfidence = [Math]::Max(80, $truth.overall_confidence)
    }
    elseif ($isDead) {
        $decision = 'DELETE_CANDIDATE'
        $decisionConfidence = $absenceConfidence
    }

    $decisionCounts[$decision]++

    $whyNot = New-Object System.Collections.Generic.List[string]
    if ($decision -ne 'DELETE_CANDIDATE') {
        if ($strongInboundCount -gt 0) { $whyNot.Add('Not DELETE_CANDIDATE because strong inbound dependencies exist') }
        if ($closureData.reachability -ne 'unreachable') { $whyNot.Add('Not DELETE_CANDIDATE because file is reachable in the closure model') }
        if ($fileIndex[$file].module_type -in @('core', 'config', 'test')) { $whyNot.Add('Not DELETE_CANDIDATE because module type is protected') }
    }
    if ($decision -eq 'KEEP') {
        if ($strongInboundCount -eq 0 -and $closureData.reachability -eq 'unreachable') { $whyNot.Add('Not ARCHIVE because the file is protected by type classification') }
    }
    if ($decision -eq 'DELETE_CANDIDATE') {
        $whyNot.Add('Not KEEP because no strong inbound dependencies or reachable paths remain')
        $whyNot.Add('Not ARCHIVE because absence confidence exceeded the deletion threshold')
    }

    $riskIfWrong = if ($strongInboundCount -gt 0) {
        'Downstream imports, registry bindings, or runtime resolution may fail.'
    }
    elseif ($supportingLayers.Contains('heuristic')) {
        'A low-confidence implicit consumer may still exist and require manual inspection.'
    }
    else {
        'The largest risk is an unmodeled runtime entrypoint outside the indexed repository.'
    }

    $evidenceChain = [ordered]@{
        inbound = [ordered]@{
            static = $staticInbound
            config = $configInbound
            dynamic = $dynamicInbound
            heuristic = $heuristicInboundEdges.Count
        }
        outbound = [ordered]@{
            static = @($truth.edges.static | ForEach-Object { $_.target })
            config = @($truth.edges.config | ForEach-Object { $_.target })
            dynamic = @($truth.edges.dynamic | ForEach-Object { $_.target })
            heuristic = @($truth.edges.heuristic | ForEach-Object { $_.target })
        }
    }

    $explanations += ,([pscustomobject]@{
        file = $file
        decision = $decision
        confidence = $decisionConfidence
        dependency_evidence_chain = $evidenceChain
        supporting_graph_layers = @($supportingLayers)
        confidence_breakdown = $truth.confidence_profile
        reachability = $closureData.reachability
        blast_radius = $closureData.blast_radius
        why_not_classified_differently = @($whyNot)
        risk_if_classification_is_wrong = $riskIfWrong
    })

    $riskFlags = @($record.risk_flags)
    if ($riskFlags.Count -gt 0 -or $record.complexity_total -ge 12 -or $closureData.blast_radius -ge 5) {
        $highRiskFiles += ,([pscustomobject]@{
            file = $file
            risk_flags = $riskFlags
            complexity_total = $record.complexity_total
            blast_radius = $closureData.blast_radius
        })
    }

    if ($isDead) {
        $deadCode += ,([pscustomobject]@{
            file = $file
            confidence = $absenceConfidence
            why_considered_dead = @(
                'No inbound static dependencies',
                'No config references',
                'No dynamic references',
                'No heuristic references above threshold',
                'Absence confidence exceeded 85'
            )
            what_would_break_if_wrong = @($riskIfWrong)
        })
    }
}

$totalFiles = [Math]::Max(1, $index.total_files)
$totalStrongEdges = [Math]::Max(1, (@($allStrongOutbound.Values | ForEach-Object { $_.Count } | Measure-Object -Sum).Sum))
$totalHeuristicEdges = @($layeredEdges.heuristic.Keys | ForEach-Object { $layeredEdges.heuristic[$_].Count } | Measure-Object -Sum).Sum
$filesInCycles = @($architecture.circular_dependency_clusters | ForEach-Object { $_.members } | Select-Object -ExpandProperty * -ErrorAction SilentlyContinue)
$cycleFileCount = @($filesInCycles | Sort-Object -Unique).Count

$dependencyCleanliness = [Math]::Max(0, 100 - [int](($contradictions.Count / $totalFiles) * 100))
$couplingDensity = [Math]::Max(0, 100 - [int](($totalStrongEdges / $totalFiles) * 10))
$deadCodeRatioMetric = [Math]::Max(0, 100 - [int]((@($deadCode).Count / $totalFiles) * 100))
$circularMetric = [Math]::Max(0, 100 - [int](($cycleFileCount / $totalFiles) * 100))
$layeringPenalty = 0
foreach ($source in $allStrongOutbound.Keys) {
    foreach ($target in @($allStrongOutbound[$source])) {
        if ($fileIndex[$source].module_type -eq 'utility' -and $fileIndex[$target].module_type -eq 'test') { $layeringPenalty += 5 }
        if ($fileIndex[$source].module_type -eq 'core' -and $fileIndex[$target].module_type -eq 'test') { $layeringPenalty += 10 }
    }
}
$layeringQuality = [Math]::Max(0, 100 - [Math]::Min(100, $layeringPenalty))
$overallHealth = [int]([Math]::Round((($dependencyCleanliness + $couplingDensity + $deadCodeRatioMetric + $circularMetric + $layeringQuality) / 5.0), 0))

$sortedDeadCode = @($deadCode | Sort-Object confidence -Descending)
$sortedContradictions = @($contradictions | Sort-Object file, type)
$sortedExplanations = @($explanations | Sort-Object @{ Expression = 'confidence'; Descending = $true }, file)
$sortedHighRiskFiles = @($highRiskFiles | Sort-Object @{ Expression = 'blast_radius'; Descending = $true }, @{ Expression = 'complexity_total'; Descending = $true }, file)
$ciIssuesDetected = ($sortedDeadCode.Count -gt 0) -or ($sortedContradictions.Count -gt 0) -or ($sortedHighRiskFiles.Count -gt 0)

$health = [ordered]@{
    score = $overallHealth
    metrics = [ordered]@{
        dependency_cleanliness = [ordered]@{
            score = $dependencyCleanliness
            explanation = 'Penalizes contradiction count relative to file count.'
            contributing_factors = [ordered]@{ contradictions = $contradictions.Count; total_files = $totalFiles }
        }
        coupling_density = [ordered]@{
            score = $couplingDensity
            explanation = 'Penalizes dense strong dependency concentration.'
            contributing_factors = [ordered]@{ strong_edges = $totalStrongEdges; total_files = $totalFiles }
        }
        dead_code_ratio = [ordered]@{
            score = $deadCodeRatioMetric
            explanation = 'Rewards lower ratio of confidently dead code.'
            contributing_factors = [ordered]@{ dead_code_files = @($deadCode).Count; total_files = $totalFiles }
        }
        circular_dependency_ratio = [ordered]@{
            score = $circularMetric
            explanation = 'Penalizes files participating in circular dependency clusters.'
            contributing_factors = [ordered]@{ files_in_cycles = $cycleFileCount; total_files = $totalFiles }
        }
        architectural_layering_quality = [ordered]@{
            score = $layeringQuality
            explanation = 'Penalizes strong edges that violate basic layer expectations.'
            contributing_factors = [ordered]@{ layering_penalty = $layeringPenalty }
        }
    }
    issue_summary = [ordered]@{
        dead_code_count = $sortedDeadCode.Count
        contradictions_count = $sortedContradictions.Count
        delete_candidate_count = $decisionCounts.DELETE_CANDIDATE
        high_risk_count = $sortedHighRiskFiles.Count
        high_risk_files = $sortedHighRiskFiles
        ci_status = if ($ciIssuesDetected) { 'issues_detected' } else { 'clean' }
    }
}

$artifactTimestamp = (Get-Date).ToUniversalTime().ToString('o')
Write-JsonArtifact -Path $truthGraphPath -RunMetadata $runMetadata -Data $truthGraph -ArtifactName 'dependency_truth_graph' -Timestamp $artifactTimestamp -Depth 12
Write-JsonArtifact -Path $closurePath -RunMetadata $runMetadata -Data $closure -ArtifactName 'dependency_closure' -Timestamp $artifactTimestamp -Depth 10
Write-JsonArtifact -Path $architecturePath -RunMetadata $runMetadata -Data $architecture -ArtifactName 'architecture_analysis' -Timestamp $artifactTimestamp -Depth 12
Write-JsonArtifact -Path $deadCodePath -RunMetadata $runMetadata -Data $sortedDeadCode -ArtifactName 'dead_code_report' -ExtraMetadata @{ issue_count = $sortedDeadCode.Count } -Timestamp $artifactTimestamp -Depth 10
Write-JsonArtifact -Path $healthPath -RunMetadata $runMetadata -Data $health -ArtifactName 'system_health_score' -ExtraMetadata @{ ci_status = $health.issue_summary.ci_status } -Timestamp $artifactTimestamp -Depth 12
Write-JsonArtifact -Path $contradictionsPath -RunMetadata $runMetadata -Data $sortedContradictions -ArtifactName 'contradictions' -ExtraMetadata @{ issue_count = $sortedContradictions.Count } -Timestamp $artifactTimestamp -Depth 10
Write-JsonArtifact -Path $explanationsPath -RunMetadata $runMetadata -Data $sortedExplanations -ArtifactName 'audit_explanations' -ExtraMetadata @{ delete_candidate_count = $decisionCounts.DELETE_CANDIDATE } -Timestamp $artifactTimestamp -Depth 12

$report = @()
$report += '# Supercharged Codebase Audit Report'
$report += ''
$report += "**Target Repository:** $TargetRepoPath"
$report += "**Generated:** $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$report += ''
$report += '## Executive Summary'
$report += ''
$report += "- Total files indexed: $($index.total_files)"
$report += "- KEEP decisions: $($decisionCounts.KEEP)"
$report += "- ARCHIVE decisions: $($decisionCounts.ARCHIVE)"
$report += "- DELETE_CANDIDATE decisions: $($decisionCounts.DELETE_CANDIDATE)"
$report += "- System health score: $overallHealth"
$report += "- Contradictions detected: $($contradictions.Count)"
$report += ''
$report += '## Health Metrics'
$report += ''
$report += "- Dependency cleanliness: $dependencyCleanliness"
$report += "- Coupling density: $couplingDensity"
$report += "- Dead code ratio: $deadCodeRatioMetric"
$report += "- Circular dependency ratio: $circularMetric"
$report += "- Architectural layering quality: $layeringQuality"
$report += ''
$report += '## Key Risk Hotspots'
$report += ''
foreach ($hotspot in @($architecture.high_coupling_hotspots | Select-Object -First 10)) {
    $report += "- $($hotspot.file): inbound=$($hotspot.inbound), outbound=$($hotspot.outbound), total=$($hotspot.total)"
}
$report += ''
$report += '## Output Files'
$report += ''
$report += "- $truthGraphPath"
$report += "- $architecturePath"
$report += "- $deadCodePath"
$report += "- $healthPath"
$report += "- $contradictionsPath"
$report += "- $explanationsPath"
$report += "- $closurePath"

if (-not $CI_MODE) {
    $report -join "`n" | Out-File -LiteralPath $reportPath -Encoding UTF8
}

Write-Status "Dependency truth graph: $truthGraphPath"
Write-Status "Architecture analysis: $architecturePath"
Write-Status "Dead code report: $deadCodePath"
Write-Status "System health score: $healthPath"
Write-Status "Contradictions: $contradictionsPath"
Write-Status "Audit explanations: $explanationsPath"
Write-Status "Closure data: $closurePath"
if (-not $CI_MODE) {
    Write-Status "Final report: $reportPath"
}
