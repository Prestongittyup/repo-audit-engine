[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [bool]$FailOnInvalid = $false
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $GraphPath -PathType Leaf)) {
    throw "Graph file not found: $GraphPath"
}

$doc = Get-Content -LiteralPath $GraphPath -Raw | ConvertFrom-Json -Depth 40
$graph = $doc.graph
if ($null -eq $graph) { $graph = $doc }

if ($null -eq $graph -or $null -eq $graph.nodes -or $null -eq $graph.edges) {
    throw 'Invalid graph format. Expected graph.nodes and graph.edges.'
}

function Get-NodeId {
    param([string]$Id)
    if ([string]::IsNullOrWhiteSpace($Id)) { return '' }
    return $Id.Trim()
}

function Get-Namespace {
    param([string]$NodeId)
    if ([string]::IsNullOrWhiteSpace($NodeId)) { return '' }

    $value = $NodeId.Trim()
    if (-not $value.StartsWith('canonical://')) { return '' }

    $tail = $value.Substring('canonical://'.Length)
    $idx = $tail.IndexOf('/')
    if ($idx -le 0) { return '' }

    return $tail.Substring(0, $idx)
}

function New-Issue {
    param(
        [string]$Type,
        [string]$Severity,
        [string]$Message,
        [int]$Count,
        [string[]]$Sample
    )

    return [ordered]@{
        type = $Type
        severity = $Severity
        message = $Message
        count = [int]$Count
        sample = @($Sample | Sort-Object -Unique | Select-Object -First 25)
    }
}

$nodes = @($graph.nodes)
$edges = @($graph.edges)

$nodeById = @{}
$malformedNodeIds = New-Object System.Collections.Generic.List[string]
$duplicateNodeIds = New-Object System.Collections.Generic.List[string]
$invalidNamespaces = New-Object System.Collections.Generic.List[string]

foreach ($n in $nodes) {
    $id = Get-NodeId -Id ([string]$n.id)
    if ([string]::IsNullOrWhiteSpace($id)) {
        $malformedNodeIds.Add([string]$n.id)
        continue
    }

    if ($nodeById.ContainsKey($id)) {
        $duplicateNodeIds.Add($id)
        continue
    }

    $nodeById[$id] = $n
    if ([string]::IsNullOrWhiteSpace((Get-Namespace -NodeId $id))) {
        $invalidNamespaces.Add($id)
    }
}

$malformedEdges = New-Object System.Collections.Generic.List[string]
$invalidEdgeTypes = New-Object System.Collections.Generic.List[string]
$unresolvedEdges = New-Object System.Collections.Generic.List[string]
$crossNamespaceEdges = New-Object System.Collections.Generic.List[string]

foreach ($e in $edges) {
    $from = Get-NodeId -Id ([string]$e.from)
    $to = Get-NodeId -Id ([string]$e.to)
    $type = Get-NodeId -Id ([string]$e.type)
    $confidence = $e.confidence

    $signature = "$from|$to|$type"

    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to) -or [string]::IsNullOrWhiteSpace($type)) {
        $malformedEdges.Add($signature)
        continue
    }

    if ($type -notin @('IMPORT', 'DI', 'CONFIG', 'DYNAMIC')) {
        $invalidEdgeTypes.Add($signature)
        continue
    }

    if ($null -eq $confidence -or -not ($confidence -is [int] -or $confidence -is [double] -or $confidence -is [float])) {
        $malformedEdges.Add($signature)
        continue
    }

    if (-not $nodeById.ContainsKey($from) -or -not $nodeById.ContainsKey($to)) {
        $unresolvedEdges.Add($signature)
        continue
    }

    $fromNs = Get-Namespace -NodeId $from
    $toNs = Get-Namespace -NodeId $to
    if (-not [string]::IsNullOrWhiteSpace($fromNs) -and -not [string]::IsNullOrWhiteSpace($toNs) -and $fromNs -ne $toNs) {
        $crossNamespaceEdges.Add($signature)
    }
}

$issues = New-Object System.Collections.Generic.List[object]
$warnings = New-Object System.Collections.Generic.List[string]

if ($malformedNodeIds.Count -gt 0) {
    $issues.Add((New-Issue -Type 'MALFORMED_NODE_ID' -Severity 'HIGH' -Message 'Node ids must be non-empty strings.' -Count $malformedNodeIds.Count -Sample @($malformedNodeIds.ToArray())))
}
if ($duplicateNodeIds.Count -gt 0) {
    $issues.Add((New-Issue -Type 'DUPLICATE_NODE_ID' -Severity 'HIGH' -Message 'Duplicate node ids detected.' -Count $duplicateNodeIds.Count -Sample @($duplicateNodeIds.ToArray())))
}
if ($invalidNamespaces.Count -gt 0) {
    $issues.Add((New-Issue -Type 'INVALID_NODE_NAMESPACE' -Severity 'MEDIUM' -Message 'Node ids should use canonical namespace format.' -Count $invalidNamespaces.Count -Sample @($invalidNamespaces.ToArray())))
}
if ($malformedEdges.Count -gt 0) {
    $issues.Add((New-Issue -Type 'MALFORMED_EDGE_SCHEMA' -Severity 'HIGH' -Message 'Edges must include valid from/to/type/confidence values.' -Count $malformedEdges.Count -Sample @($malformedEdges.ToArray())))
}
if ($invalidEdgeTypes.Count -gt 0) {
    $issues.Add((New-Issue -Type 'INVALID_EDGE_TYPE' -Severity 'MEDIUM' -Message 'Edge type is not supported.' -Count $invalidEdgeTypes.Count -Sample @($invalidEdgeTypes.ToArray())))
}
if ($unresolvedEdges.Count -gt 0) {
    $issues.Add((New-Issue -Type 'UNRESOLVED_EDGE_REFERENCE' -Severity 'HIGH' -Message 'Edges must reference known nodes.' -Count $unresolvedEdges.Count -Sample @($unresolvedEdges.ToArray())))
}
if ($crossNamespaceEdges.Count -gt 0) {
    $warnings.Add('Cross-namespace edges detected. This is allowed, but should be reviewed for ownership boundaries.')
}

$criticalFailure = ($nodes.Count -eq 0) -or ($nodeById.Keys.Count -eq 0)
$status = if ($criticalFailure) { 'INVALID_STRUCTURAL' } elseif ($issues.Count -gt 0) { 'DEGRADED_STRUCTURAL' } else { 'VALID' }

$result = [ordered]@{
    status = $status
    critical_failure = [bool]$criticalFailure
    metrics = [ordered]@{
        node_count = $nodes.Count
        edge_count = $edges.Count
        valid_node_count = $nodeById.Keys.Count
        malformed_node_count = $malformedNodeIds.Count
        duplicate_node_count = $duplicateNodeIds.Count
        invalid_namespace_count = $invalidNamespaces.Count
        malformed_edge_count = $malformedEdges.Count
        invalid_edge_type_count = $invalidEdgeTypes.Count
        unresolved_edge_count = $unresolvedEdges.Count
        cross_namespace_edge_count = $crossNamespaceEdges.Count
    }
    issues = @($issues.ToArray())
    warnings = @($warnings.ToArray())
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 20 | Out-File -LiteralPath $outFull -Encoding UTF8

if ($criticalFailure -and $FailOnInvalid) {
    throw "Layer5 structural validation encountered a critical failure. See report: $outFull"
}

return $result
