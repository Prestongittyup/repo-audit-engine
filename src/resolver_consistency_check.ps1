[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [Parameter(Mandatory = $true)]
    [string]$EdgesPath,
    [int]$HeuristicOnlyThreshold = 0,
    [double]$DriftThreshold = 1.0,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [bool]$FailOnInvalid = $false
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $GraphPath -PathType Leaf)) {
    throw "Graph file not found: $GraphPath"
}
if (-not (Test-Path -LiteralPath $EdgesPath -PathType Leaf)) {
    throw "Edges file not found: $EdgesPath"
}

$graphDoc = Get-Content -LiteralPath $GraphPath -Raw | ConvertFrom-Json -Depth 40
$edgeDoc = Get-Content -LiteralPath $EdgesPath -Raw | ConvertFrom-Json -Depth 40

if ($null -eq $graphDoc.graph -or $null -eq $graphDoc.graph.nodes -or $null -eq $graphDoc.graph.edges) {
    throw 'Invalid graph format. Expected graph.nodes and graph.edges.'
}
if ($null -eq $edgeDoc.edges) {
    throw 'Invalid resolver edges format. Expected edges array.'
}

function Add-Disagreement {
    param(
        [System.Collections.Generic.List[object]]$List,
        [string]$Node,
        [string]$Issue,
        [string]$Severity,
        [string]$Edge = ''
    )

    $List.Add([ordered]@{
        node = $Node
        issue = $Issue
        severity = $Severity
        edge = $Edge
    })
}

function Get-EdgeSources {
    param([object]$Edge)

    if ($Edge.PSObject.Properties.Name -contains 'source_metadata' -and $null -ne $Edge.source_metadata) {
        return @($Edge.source_metadata | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
    }

    if ($Edge.PSObject.Properties.Name -contains 'source' -and -not [string]::IsNullOrWhiteSpace([string]$Edge.source)) {
        return @([string]$Edge.source)
    }

    return @()
}

function Get-EdgeTriple {
    param([object]$Edge)

    $from = [string]$Edge.from
    $to = [string]$Edge.to
    $type = [string]$Edge.type

    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to) -or [string]::IsNullOrWhiteSpace($type)) {
        return ''
    }
    return ("{0}|{1}|{2}" -f $from.Trim(), $to.Trim(), $type.Trim())
}

function Get-EdgePair {
    param([object]$Edge)

    $from = [string]$Edge.from
    $to = [string]$Edge.to
    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to)) {
        return ''
    }
    return ("{0}|{1}" -f $from.Trim(), $to.Trim())
}

$disagreements = New-Object System.Collections.Generic.List[object]
$graphEdges = @($graphDoc.graph.edges)
$resolverEdges = @($edgeDoc.edges)

$astEdges = 0
$diEdges = 0
$configEdges = 0
$heuristicEdges = 0

$heuristicOnlyNodes = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)

$graphTriples = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$graphAstTriples = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$graphDiTriples = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$graphAstPairs = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$graphDiPairs = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)

foreach ($edge in $graphEdges) {
    $triple = Get-EdgeTriple -Edge $edge
    if (-not [string]::IsNullOrWhiteSpace($triple)) {
        $null = $graphTriples.Add($triple)
    }

    $sources = @(Get-EdgeSources -Edge $edge)
    $pair = Get-EdgePair -Edge $edge

    if ($sources -contains 'AST') {
        $astEdges += 1
        if (-not [string]::IsNullOrWhiteSpace($triple)) { $null = $graphAstTriples.Add($triple) }
        if (-not [string]::IsNullOrWhiteSpace($pair)) { $null = $graphAstPairs.Add($pair) }
    }
    if ($sources -contains 'DI') {
        $diEdges += 1
        if (-not [string]::IsNullOrWhiteSpace($triple)) { $null = $graphDiTriples.Add($triple) }
        if (-not [string]::IsNullOrWhiteSpace($pair)) { $null = $graphDiPairs.Add($pair) }
    }
    if ($sources -contains 'CONFIG') {
        $configEdges += 1
    }
    if ($sources -contains 'HEURISTIC') {
        $heuristicEdges += 1
    }

    if ($sources.Count -ne 1) {
        continue
    }

    switch ($sources[0]) {
        'HEURISTIC' {
            if (-not [string]::IsNullOrWhiteSpace([string]$edge.from)) { $null = $heuristicOnlyNodes.Add([string]$edge.from) }
            if (-not [string]::IsNullOrWhiteSpace([string]$edge.to)) { $null = $heuristicOnlyNodes.Add([string]$edge.to) }
            break
        }
    }
}

foreach ($n in @($heuristicOnlyNodes | Sort-Object)) {
    Add-Disagreement -List $disagreements -Node $n -Issue 'NODE_ONLY_IN_HEURISTIC_RESOLVER' -Severity 'MEDIUM'
}

$resolverAstTriples = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$resolverDiTriples = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$resolverAstPairs = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$resolverDiPairs = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)

foreach ($e in $resolverEdges) {
    $source = [string]$e.source
    $triple = Get-EdgeTriple -Edge $e
    $pair = Get-EdgePair -Edge $e

    if ($source -eq 'AST') {
        if (-not [string]::IsNullOrWhiteSpace($triple)) { $null = $resolverAstTriples.Add($triple) }
        if (-not [string]::IsNullOrWhiteSpace($pair)) { $null = $resolverAstPairs.Add($pair) }
        if (-not [string]::IsNullOrWhiteSpace($triple) -and -not $graphTriples.Contains($triple)) {
            Add-Disagreement -List $disagreements -Node ([string]$e.from) -Issue 'AST_EDGE_NOT_REPRESENTED_IN_GRAPH' -Severity 'HIGH' -Edge $triple
        }
        continue
    }

    if ($source -eq 'DI') {
        if (-not [string]::IsNullOrWhiteSpace($triple)) { $null = $resolverDiTriples.Add($triple) }
        if (-not [string]::IsNullOrWhiteSpace($pair)) { $null = $resolverDiPairs.Add($pair) }
        if (-not [string]::IsNullOrWhiteSpace($triple) -and -not $graphTriples.Contains($triple)) {
            Add-Disagreement -List $disagreements -Node ([string]$e.from) -Issue 'DI_EDGE_NOT_REPRESENTED_IN_GRAPH' -Severity 'HIGH' -Edge $triple
        }
        if (-not [string]::IsNullOrWhiteSpace($pair) -and -not $resolverAstPairs.Contains($pair)) {
            Add-Disagreement -List $disagreements -Node ([string]$e.from) -Issue 'DI_RESOLVER_EDGE_NOT_DERIVED_FROM_AST_RESOLVER' -Severity 'HIGH' -Edge $triple
        }
    }
}

foreach ($diTriple in @($graphDiTriples)) {
    if (-not $resolverDiTriples.Contains($diTriple)) {
        $parts = $diTriple.Split('|')
        Add-Disagreement -List $disagreements -Node $parts[0] -Issue 'DI_EDGE_PRESENT_IN_GRAPH_BUT_MISSING_FROM_DI_RESOLVER' -Severity 'HIGH' -Edge $diTriple
    }
}

foreach ($pair in @($graphDiPairs)) {
    if (-not $graphAstPairs.Contains($pair)) {
        $parts = $pair.Split('|')
        Add-Disagreement -List $disagreements -Node $parts[0] -Issue 'DI_GRAPH_EDGE_NOT_DERIVED_FROM_AST_GRAPH' -Severity 'HIGH' -Edge $pair
    }
}

$astDiSymmetricDiff = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($pair in @($graphAstPairs)) {
    if (-not $graphDiPairs.Contains($pair)) { $null = $astDiSymmetricDiff.Add($pair) }
}
foreach ($pair in @($graphDiPairs)) {
    if (-not $graphAstPairs.Contains($pair)) { $null = $astDiSymmetricDiff.Add($pair) }
}

$driftDenominator = [Math]::Max(1, $graphAstPairs.Count)
$driftScore = [Math]::Round(($astDiSymmetricDiff.Count / $driftDenominator), 3)
if ($driftScore -gt $DriftThreshold) {
    Add-Disagreement -List $disagreements -Node '' -Issue ("AST_DI_DRIFT_THRESHOLD_EXCEEDED({0}>{1})" -f $driftScore, $DriftThreshold) -Severity 'MEDIUM'
}

$graphNodes = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($n in @($graphDoc.graph.nodes)) {
    if (-not [string]::IsNullOrWhiteSpace([string]$n.id)) {
        $null = $graphNodes.Add([string]$n.id)
    }
}

$diWiredNodes = @($edgeDoc.edges | Where-Object { [string]$_.source -eq 'DI' } | ForEach-Object { [string]$_.from; [string]$_.to } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
$missingDiWired = New-Object System.Collections.Generic.List[string]
foreach ($id in $diWiredNodes) {
    if (-not $graphNodes.Contains($id)) {
        $missingDiWired.Add($id)
        Add-Disagreement -List $disagreements -Node $id -Issue 'DI_WIRED_NODE_MISSING_FROM_FINAL_GRAPH' -Severity 'HIGH'
    }
}

if ($heuristicOnlyNodes.Count -gt $HeuristicOnlyThreshold) {
    Add-Disagreement -List $disagreements -Node '' -Issue ("HEURISTIC_ONLY_NODE_THRESHOLD_EXCEEDED({0}>{1})" -f $heuristicOnlyNodes.Count, $HeuristicOnlyThreshold) -Severity 'MEDIUM'
}

$highCount = @($disagreements | Where-Object { [string]$_.severity -eq 'HIGH' }).Count
$mediumCount = @($disagreements | Where-Object { [string]$_.severity -eq 'MEDIUM' }).Count

$result = [ordered]@{
    status = if ($highCount -eq 0 -and $mediumCount -eq 0) { 'ANALYZED' } elseif ($highCount -eq 0) { 'DEGRADED' } else { 'HIGH_RISK' }
    ast_edges = $astEdges
    di_edges = $diEdges
    config_edges = $configEdges
    heuristic_edges = $heuristicEdges
    drift_score = [double]$driftScore
    drift_threshold = [double]$DriftThreshold
    ast_di_pair_delta_count = $astDiSymmetricDiff.Count
    disagreements = @($disagreements | Sort-Object severity, node, issue)
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 20 | Out-File -LiteralPath $outFull -Encoding UTF8

$hasHighDisagreement = $highCount -gt 0
$fail = $hasHighDisagreement -or ($missingDiWired.Count -gt 0) -or ($heuristicOnlyNodes.Count -gt $HeuristicOnlyThreshold) -or ($driftScore -gt $DriftThreshold)
if ($fail -and $FailOnInvalid) {
    throw "Resolver consistency check failed. See report: $outFull"
}

return $result
