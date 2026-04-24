[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [Parameter(Mandatory = $true)]
    [string]$ValidationPath,
    [Parameter(Mandatory = $true)]
    [string]$ClassificationPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

foreach ($p in @($GraphPath, $ValidationPath, $ClassificationPath)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw "Required input file not found: $p"
    }
}

$graphDoc = Get-Content -LiteralPath $GraphPath -Raw | ConvertFrom-Json -Depth 40
$validationDoc = Get-Content -LiteralPath $ValidationPath -Raw | ConvertFrom-Json -Depth 20
$classDoc = Get-Content -LiteralPath $ClassificationPath -Raw | ConvertFrom-Json -Depth 20

if ($null -eq $graphDoc.graph -or $null -eq $graphDoc.graph.nodes -or $null -eq $graphDoc.graph.edges) {
    throw 'Invalid graph format. Expected graph.nodes and graph.edges.'
}
if ($null -eq $classDoc.classification) {
    throw 'Invalid classification format. Expected classification object.'
}
if ($null -eq $validationDoc.status) {
    throw 'Invalid validation format. Expected validation status.'
}

$graphStats = [ordered]@{
    node_count = if ($null -ne $graphDoc.stats -and $null -ne $graphDoc.stats.node_count) { [int]$graphDoc.stats.node_count } else { @($graphDoc.graph.nodes).Count }
    edge_count = if ($null -ne $graphDoc.stats -and $null -ne $graphDoc.stats.edge_count) { [int]$graphDoc.stats.edge_count } else { @($graphDoc.graph.edges).Count }
    deduplicated_edges = if ($null -ne $graphDoc.stats -and $null -ne $graphDoc.stats.deduplicated_edges) { [int]$graphDoc.stats.deduplicated_edges } else { 0 }
}

$classificationOut = [ordered]@{
    REACHABLE = @($classDoc.classification.REACHABLE)
    REFERENCED = @($classDoc.classification.REFERENCED)
    ISOLATED = @($classDoc.classification.ISOLATED)
    SUSPICIOUS = @($classDoc.classification.SUSPICIOUS)
    DEAD = @($classDoc.classification.DEAD)
    EXEMPT = @($classDoc.classification.EXEMPT)
}

$resolverCounts = @{}
foreach ($edge in @($graphDoc.graph.edges)) {
    $sources = @()

    if ($edge.PSObject.Properties.Name -contains 'source_metadata' -and $null -ne $edge.source_metadata) {
        $sources = @($edge.source_metadata | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
    }
    elseif ($edge.PSObject.Properties.Name -contains 'source' -and -not [string]::IsNullOrWhiteSpace([string]$edge.source)) {
        $sources = @([string]$edge.source)
    }

    foreach ($s in $sources) {
        if (-not $resolverCounts.ContainsKey($s)) {
            $resolverCounts[$s] = 0
        }
        $resolverCounts[$s] = [int]$resolverCounts[$s] + 1
    }
}

$resolverMetrics = [ordered]@{}
foreach ($key in @($resolverCounts.Keys | Sort-Object)) {
    $resolverMetrics[$key] = [int]$resolverCounts[$key]
}

$validationOut = [ordered]@{
    status = [string]$validationDoc.status
    issues = if ($null -ne $validationDoc.issues) { @($validationDoc.issues) } else { @() }
    metrics = if ($null -ne $validationDoc.metrics) { $validationDoc.metrics } else { [ordered]@{} }
}

$result = [ordered]@{
    graph_summary = $graphStats
    classification = $classificationOut
    validation = $validationOut
    resolver_metrics = $resolverMetrics
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 30 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
