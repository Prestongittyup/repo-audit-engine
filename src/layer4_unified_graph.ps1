[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$CanonicalPath,
    [Parameter(Mandatory = $true)]
    [string]$EdgesPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $CanonicalPath -PathType Leaf)) {
    throw "Canonical nodes file not found: $CanonicalPath"
}
if (-not (Test-Path -LiteralPath $EdgesPath -PathType Leaf)) {
    throw "Edges file not found: $EdgesPath"
}

$canonical = Get-Content -LiteralPath $CanonicalPath -Raw | ConvertFrom-Json -Depth 20
$resolved = Get-Content -LiteralPath $EdgesPath -Raw | ConvertFrom-Json -Depth 20

if ($null -eq $canonical.nodes) {
    throw 'Invalid canonical format: missing nodes[]'
}
if ($null -eq $resolved.edges) {
    throw 'Invalid edges format: missing edges[]'
}

function Get-NodeNamespace {
    param([string]$CanonicalId)

    if ([string]::IsNullOrWhiteSpace($CanonicalId)) {
        return ''
    }
    if ($CanonicalId -match '^canonical://([^/]+)/') {
        return [string]$Matches[1]
    }
    return ''
}

function Add-HardViolation {
    param(
        [System.Collections.Generic.List[object]]$List,
        [string]$Code,
        [string]$From,
        [string]$To,
        [string]$Detail
    )

    $List.Add([ordered]@{
        code = $Code
        from = $From
        to = $To
        detail = $Detail
    })
}

$nodeList = @($canonical.nodes | Sort-Object id)
$nodeById = @{}
$identityByNamespacePath = @{}
$graphNodes = New-Object System.Collections.Generic.List[object]

foreach ($n in $nodeList) {
    $id = [string]$n.id
    if ([string]::IsNullOrWhiteSpace($id)) {
        throw 'Canonical node is missing id.'
    }
    if (-not $id.StartsWith('canonical://')) {
        throw "Non-canonical node ID detected: $id"
    }
    if ($nodeById.ContainsKey($id)) {
        throw "Duplicate canonical node ID detected: $id"
    }

    $namespace = Get-NodeNamespace -CanonicalId $id
    if ([string]::IsNullOrWhiteSpace($namespace)) {
        throw "Canonical node has invalid namespace: $id"
    }

    $filePath = [string]$n.file_path
    if ([string]::IsNullOrWhiteSpace($filePath)) {
        throw "Canonical node is missing file_path: $id"
    }

    $identityKey = "$namespace|$filePath"
    if ($identityByNamespacePath.ContainsKey($identityKey)) {
        throw "Duplicate canonical identity detected for namespace+file_path '$identityKey'"
    }
    $identityByNamespacePath[$identityKey] = $id

    $node = [ordered]@{
        id = $id
        file_path = $filePath
        module_path = [string]$n.module_path
        type = [string]$n.type
    }
    $nodeById[$id] = $node
    $graphNodes.Add($node)
}

$rawEdges = @($resolved.edges)
$edgeByTriple = @{}
$rawValidEdgeCount = 0
$hardViolations = New-Object System.Collections.Generic.List[object]
$validRawEdges = New-Object System.Collections.Generic.List[object]

foreach ($e in $rawEdges) {
    $from = [string]$e.from
    $to = [string]$e.to
    $type = [string]$e.type
    $source = [string]$e.source
    $confidence = 0.0

    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to) -or [string]::IsNullOrWhiteSpace($type)) {
        Add-HardViolation -List $hardViolations -Code 'MALFORMED_EDGE' -From $from -To $to -Detail 'from/to/type must be present'
        continue
    }
    if ($from -eq $to) {
        Add-HardViolation -List $hardViolations -Code 'SELF_LOOP_EDGE' -From $from -To $to -Detail 'self-loop edges are not allowed in unified graph'
        continue
    }
    if (-not $nodeById.ContainsKey($from)) {
        Add-HardViolation -List $hardViolations -Code 'MISSING_FROM_NODE' -From $from -To $to -Detail 'from_node does not exist in node registry'
        continue
    }
    if (-not $nodeById.ContainsKey($to)) {
        Add-HardViolation -List $hardViolations -Code 'MISSING_TO_NODE' -From $from -To $to -Detail 'to_node does not exist in node registry'
        continue
    }

    $fromNamespace = Get-NodeNamespace -CanonicalId $from
    $toNamespace = Get-NodeNamespace -CanonicalId $to
    if ([string]::IsNullOrWhiteSpace($fromNamespace) -or [string]::IsNullOrWhiteSpace($toNamespace)) {
        Add-HardViolation -List $hardViolations -Code 'INVALID_EDGE_NAMESPACE' -From $from -To $to -Detail 'edge endpoint namespace could not be resolved'
        continue
    }

    if ($fromNamespace -ne $toNamespace) {
        Add-HardViolation -List $hardViolations -Code 'CROSS_NAMESPACE_RESOLUTION' -From $from -To $to -Detail 'cross-namespace resolution is forbidden'
        continue
    }

    if (($fromNamespace -eq 'repo' -and $toNamespace -match '^scenario_') -or ($toNamespace -eq 'repo' -and $fromNamespace -match '^scenario_')) {
        Add-HardViolation -List $hardViolations -Code 'FORBIDDEN_REPO_SCENARIO_RESOLUTION' -From $from -To $to -Detail 'repo namespace cannot resolve to scenario namespace'
        continue
    }

    try {
        $confidence = [double]$e.confidence
    }
    catch {
        $confidence = 0.0
    }

    $rawValidEdgeCount += 1
    $validRawEdges.Add([ordered]@{
        from = $from
        to = $to
        type = $type
        source = $source
    })

    $triple = "$from|$to|$type"

    if (-not $edgeByTriple.ContainsKey($triple)) {
        $sourceMeta = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
        if (-not [string]::IsNullOrWhiteSpace($source)) {
            $null = $sourceMeta.Add($source)
        }

        $edgeByTriple[$triple] = [ordered]@{
            from = $from
            to = $to
            type = $type
            confidence = [Math]::Round($confidence, 3)
            source = $source
            source_metadata = $sourceMeta
        }
        continue
    }

    $existing = $edgeByTriple[$triple]
    if (-not [string]::IsNullOrWhiteSpace($source)) {
        $null = $existing.source_metadata.Add($source)
    }

    $existingConfidence = [double]$existing.confidence
    $roundedConfidence = [Math]::Round($confidence, 3)

    if ($roundedConfidence -gt $existingConfidence) {
        $existing.confidence = $roundedConfidence
        $existing.source = $source
    }
    elseif ($roundedConfidence -eq $existingConfidence -and -not [string]::IsNullOrWhiteSpace($source) -and [string]::CompareOrdinal($source, [string]$existing.source) -lt 0) {
        # Deterministic tie-break when confidence is equal.
        $existing.source = $source
    }
}

$astPairs = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($edge in $validRawEdges) {
    if ([string]$edge.source -eq 'AST') {
        $null = $astPairs.Add("$($edge.from)|$($edge.to)")
    }
}

foreach ($edge in $validRawEdges) {
    if ([string]$edge.source -ne 'DI') {
        continue
    }
    $pairKey = "$($edge.from)|$($edge.to)"
    if (-not $astPairs.Contains($pairKey)) {
        Add-HardViolation -List $hardViolations -Code 'DI_NOT_DERIVED_FROM_AST' -From ([string]$edge.from) -To ([string]$edge.to) -Detail 'DI edges must be derived from AST source edges'
    }
}

if ($hardViolations.Count -gt 0) {
    $preview = @($hardViolations.ToArray() | Select-Object -First 25)
    throw ("Hard graph invariant violation(s) detected: " + (($preview | ConvertTo-Json -Depth 8 -Compress)))
}

$finalEdges = New-Object System.Collections.Generic.List[object]
foreach ($entry in @($edgeByTriple.Values | Sort-Object from, to, type)) {
    $sources = @($entry.source_metadata | Sort-Object)
    $finalEdges.Add([ordered]@{
        from = [string]$entry.from
        to = [string]$entry.to
        type = [string]$entry.type
        confidence = [double]$entry.confidence
        source = [string]$entry.source
        source_metadata = $sources
    })
}

$deduplicatedEdges = $rawValidEdgeCount - $finalEdges.Count
if ($deduplicatedEdges -lt 0) {
    $deduplicatedEdges = 0
}

$result = [ordered]@{
    graph = [ordered]@{
        nodes = @($graphNodes.ToArray())
        edges = @($finalEdges.ToArray())
    }
    stats = [ordered]@{
        node_count = $graphNodes.Count
        edge_count = $finalEdges.Count
        deduplicated_edges = $deduplicatedEdges
    }
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 12 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
