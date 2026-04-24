[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [string]$GraphValidationPath,
    [string]$GraphStructuralValidationPath,
    [string]$ResolverConsistencyPath,
    [string]$SemanticValidationPath,
    [string]$ClassificationPath,
    [string]$OrphanQueryPath,
    [string]$ClustersQueryPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

function Test-Field {
    param(
        [object]$Object,
        [string]$Name
    )

    if ($null -eq $Object) { return $false }
    if ($Object -is [System.Collections.IDictionary]) {
        return $Object.Contains($Name)
    }

    return ($Object.PSObject.Properties.Name -contains $Name)
}

function Get-Field {
    param(
        [object]$Object,
        [string]$Name,
        [object]$Default = $null
    )

    if (-not (Test-Field -Object $Object -Name $Name)) {
        return $Default
    }

    if ($Object -is [System.Collections.IDictionary]) {
        return $Object[$Name]
    }

    return $Object.$Name
}

function Read-OptionalJson {
    param(
        [string]$Path,
        [int]$Depth = 80
    )

    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }

    try {
        return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth $Depth)
    }
    catch {
        return $null
    }
}

function Normalize-NodeId {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    return $Value.Trim()
}

function To-SortedUniqueStrings {
    param([object[]]$Values)

    if ($null -eq $Values) { return @() }
    return @(
        $Values |
            ForEach-Object { [string]$_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { $_.Trim() } |
            Sort-Object -Unique
    )
}

function Clamp01 {
    param([double]$Value)

    if ($Value -lt 0.0) { return 0.0 }
    if ($Value -gt 1.0) { return 1.0 }
    return [double]$Value
}

function Round3 {
    param([double]$Value)

    return [Math]::Round([double]$Value, 3)
}

function To-Int {
    param(
        [object]$Value,
        [int]$Default = 0
    )

    try {
        return [int]$Value
    }
    catch {
        return $Default
    }
}

function To-Double {
    param(
        [object]$Value,
        [double]$Default = 0.0
    )

    try {
        $parsed = [double]$Value
        if ([double]::IsNaN($parsed)) { return $Default }
        return $parsed
    }
    catch {
        return $Default
    }
}

function Test-NodeExplicitIsolation {
    param([object]$Node)

    if ($null -eq $Node) { return $false }

    foreach ($key in @('allow_isolated', 'isolated_allowed', 'expected_isolation')) {
        if (Test-Field -Object $Node -Name $key) {
            $flag = Get-Field -Object $Node -Name $key -Default $false
            if ($flag -is [bool] -and [bool]$flag) {
                return $true
            }
        }
    }

    $metadata = Get-Field -Object $Node -Name 'metadata'
    if ($null -ne $metadata) {
        foreach ($key in @('allow_isolated', 'isolated_allowed', 'expected_isolation')) {
            if (Test-Field -Object $metadata -Name $key) {
                $flag = Get-Field -Object $metadata -Name $key -Default $false
                if ($flag -is [bool] -and [bool]$flag) {
                    return $true
                }
            }
        }
    }

    $tags = @((Get-Field -Object $Node -Name 'tags' -Default @()))
    foreach ($tag in $tags) {
        $normalized = [string]$tag
        if ([string]::IsNullOrWhiteSpace($normalized)) { continue }
        $candidate = $normalized.Trim().ToLowerInvariant()
        if ($candidate -in @('isolated_module', 'expected_isolation')) {
            return $true
        }
    }

    return $false
}

function Get-NormalizedDepthMap {
    param(
        [string[]]$NodeIds,
        [hashtable]$OutboundSet,
        [hashtable]$InboundSet
    )

    $depth = @{}
    $inboundWork = @{}

    foreach ($id in $NodeIds) {
        $depth[$id] = 0
        $inboundWork[$id] = To-Int -Value @($InboundSet[$id]).Count -Default 0
    }

    $queue = New-Object System.Collections.Generic.Queue[string]
    foreach ($id in @($NodeIds | Sort-Object)) {
        if ([int]$inboundWork[$id] -eq 0) {
            $queue.Enqueue($id)
        }
    }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        $currentDepth = [int]$depth[$current]

        foreach ($next in @($OutboundSet[$current] | Sort-Object)) {
            $candidateDepth = $currentDepth + 1
            if ($candidateDepth -gt [int]$depth[$next]) {
                $depth[$next] = $candidateDepth
            }

            $inboundWork[$next] = [int]$inboundWork[$next] - 1
            if ([int]$inboundWork[$next] -eq 0) {
                $queue.Enqueue([string]$next)
            }
        }
    }

    $maxDepth = 0
    foreach ($id in $NodeIds) {
        if ([int]$depth[$id] -gt $maxDepth) {
            $maxDepth = [int]$depth[$id]
        }
    }

    $normalized = @{}
    foreach ($id in $NodeIds) {
        if ($maxDepth -gt 0) {
            $normalized[$id] = [double]([int]$depth[$id] / [double]$maxDepth)
        }
        else {
            $normalized[$id] = 0.0
        }
    }

    return $normalized
}

function Get-DistanceMap {
    param(
        [string[]]$StartNodes,
        [hashtable]$AdjacencySet
    )

    $distance = @{}
    $queue = New-Object System.Collections.Generic.Queue[string]

    foreach ($id in @($StartNodes | Sort-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($id)) { continue }
        if (-not $AdjacencySet.ContainsKey($id)) { continue }
        if (-not $distance.ContainsKey($id)) {
            $distance[$id] = 0
            $queue.Enqueue($id)
        }
    }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        $currentDistance = [int]$distance[$current]

        foreach ($next in @($AdjacencySet[$current] | Sort-Object)) {
            if (-not $distance.ContainsKey($next)) {
                $distance[$next] = $currentDistance + 1
                $queue.Enqueue([string]$next)
            }
        }
    }

    return $distance
}

function Get-ComponentSizeFromSeed {
    param(
        [string[]]$SeedNodes,
        [hashtable]$AdjacencySet
    )

    $visited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    $queue = New-Object System.Collections.Generic.Queue[string]

    foreach ($id in @($SeedNodes | Sort-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($id)) { continue }
        if (-not $AdjacencySet.ContainsKey($id)) { continue }
        if ($visited.Add($id)) {
            $queue.Enqueue($id)
        }
    }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        foreach ($next in @($AdjacencySet[$current] | Sort-Object)) {
            if ($visited.Add($next)) {
                $queue.Enqueue([string]$next)
            }
        }
    }

    return $visited.Count
}

function Get-ClusterSeverity {
    param(
        [int]$ClusterNodeCount,
        [int]$SystemNodeCount,
        [double]$ImpactScore = 0.0,
        [bool]$HasBrokenDependencySignal = $false
    )

    $ratio = if ($SystemNodeCount -gt 0) { $ClusterNodeCount / [double]$SystemNodeCount } else { 0.0 }

    if ($HasBrokenDependencySignal -or $ratio -ge 0.35 -or $ImpactScore -ge 0.80) { return 'CRITICAL' }
    if ($ratio -ge 0.15 -or $ImpactScore -ge 0.55 -or $ClusterNodeCount -ge 75) { return 'HIGH' }
    if ($ratio -ge 0.05 -or $ImpactScore -ge 0.25 -or $ClusterNodeCount -ge 20) { return 'MEDIUM' }
    return 'LOW'
}

function Get-SeverityRank {
    param([string]$Severity)

    switch ($Severity) {
        'CRITICAL' { return 0 }
        'HIGH' { return 1 }
        'MEDIUM' { return 2 }
        default { return 3 }
    }
}

function Get-ClusterEdges {
    param(
        [string[]]$NodeIds,
        [object[]]$GraphEdges,
        [bool]$InternalOnly,
        [int]$Limit = 120
    )

    $nodeSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    foreach ($id in @($NodeIds | Sort-Object -Unique)) {
        if (-not [string]::IsNullOrWhiteSpace($id)) {
            $null = $nodeSet.Add($id)
        }
    }

    $selected = New-Object System.Collections.Generic.List[object]
    foreach ($edge in @($GraphEdges | Sort-Object from, to, type)) {
        $from = Normalize-NodeId -Value ([string](Get-Field -Object $edge -Name 'from' -Default ''))
        $to = Normalize-NodeId -Value ([string](Get-Field -Object $edge -Name 'to' -Default ''))
        $type = Normalize-NodeId -Value ([string](Get-Field -Object $edge -Name 'type' -Default ''))

        if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to) -or [string]::IsNullOrWhiteSpace($type)) {
            continue
        }

        if ($InternalOnly) {
            if (-not ($nodeSet.Contains($from) -and $nodeSet.Contains($to))) { continue }
        }
        else {
            if (-not ($nodeSet.Contains($from) -or $nodeSet.Contains($to))) { continue }
        }

        $selected.Add([ordered]@{
            from = $from
            to = $to
            type = $type
        })

        if ($selected.Count -ge $Limit) {
            break
        }
    }

    return @($selected.ToArray())
}

$graphDoc = Read-OptionalJson -Path $GraphPath -Depth 120
if ($null -eq $graphDoc) {
    throw "Unable to load graph payload: $GraphPath"
}

$graph = Get-Field -Object $graphDoc -Name 'graph' -Default $graphDoc
if ($null -eq $graph -or -not (Test-Field -Object $graph -Name 'nodes') -or -not (Test-Field -Object $graph -Name 'edges')) {
    throw 'Invalid graph document: expected graph.nodes and graph.edges.'
}

$nodes = @((Get-Field -Object $graph -Name 'nodes' -Default @()))
$edges = @((Get-Field -Object $graph -Name 'edges' -Default @()))

$nodeById = @{}
$nodeIds = New-Object System.Collections.Generic.List[string]
$outboundSet = @{}
$inboundSet = @{}
$undirectedSet = @{}
$explicitIsolationSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)

foreach ($node in @($nodes | Sort-Object id)) {
    $id = Normalize-NodeId -Value ([string](Get-Field -Object $node -Name 'id' -Default ''))
    if ([string]::IsNullOrWhiteSpace($id)) { continue }
    if ($nodeById.ContainsKey($id)) { continue }

    $nodeById[$id] = $node
    $nodeIds.Add($id)
    $outboundSet[$id] = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    $inboundSet[$id] = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    $undirectedSet[$id] = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)

    if (Test-NodeExplicitIsolation -Node $node) {
        $null = $explicitIsolationSet.Add($id)
    }
}

foreach ($edge in @($edges | Sort-Object from, to, type)) {
    $from = Normalize-NodeId -Value ([string](Get-Field -Object $edge -Name 'from' -Default ''))
    $to = Normalize-NodeId -Value ([string](Get-Field -Object $edge -Name 'to' -Default ''))
    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to)) { continue }
    if (-not $nodeById.ContainsKey($from) -or -not $nodeById.ContainsKey($to)) { continue }

    $null = $outboundSet[$from].Add($to)
    $null = $inboundSet[$to].Add($from)
    $null = $undirectedSet[$from].Add($to)
    $null = $undirectedSet[$to].Add($from)
}

$nodeCount = $nodeIds.Count
$edgeCount = @($edges).Count

$degreeByNode = @{}
$maxDegree = 0
foreach ($id in @($nodeIds | Sort-Object)) {
    $degree = @($outboundSet[$id]).Count + @($inboundSet[$id]).Count
    $degreeByNode[$id] = $degree
    if ($degree -gt $maxDegree) { $maxDegree = $degree }
}

$degreeNormByNode = @{}
foreach ($id in @($nodeIds | Sort-Object)) {
    if ($maxDegree -gt 0) {
        $degreeNormByNode[$id] = [double]($degreeByNode[$id] / [double]$maxDegree)
    }
    else {
        $degreeNormByNode[$id] = 0.0
    }
}

$depthNormByNode = Get-NormalizedDepthMap -NodeIds @($nodeIds | Sort-Object) -OutboundSet $outboundSet -InboundSet $inboundSet

$graphValidationDoc = Read-OptionalJson -Path $GraphValidationPath
$graphStructuralDoc = Read-OptionalJson -Path $GraphStructuralValidationPath
$resolverDoc = Read-OptionalJson -Path $ResolverConsistencyPath
$semanticDoc = Read-OptionalJson -Path $SemanticValidationPath
$classificationDoc = Read-OptionalJson -Path $ClassificationPath
$orphanDoc = Read-OptionalJson -Path $OrphanQueryPath
$clustersDoc = Read-OptionalJson -Path $ClustersQueryPath

$classificationRoot = Get-Field -Object $classificationDoc -Name 'classification'

$orphanNodes = @()
if ($null -ne $orphanDoc -and (Test-Field -Object $orphanDoc -Name 'results')) {
    $orphanNodes = To-SortedUniqueStrings -Values @((Get-Field -Object $orphanDoc -Name 'results' -Default @()))
}
elseif ($null -ne $classificationRoot) {
    $orphanNodes = To-SortedUniqueStrings -Values @((Get-Field -Object $classificationRoot -Name 'ISOLATED' -Default @()))
}

$deadNodes = @()
$suspiciousNodes = @()
if ($null -ne $classificationRoot) {
    $deadNodes = To-SortedUniqueStrings -Values @((Get-Field -Object $classificationRoot -Name 'DEAD' -Default @()))
    $suspiciousNodes = To-SortedUniqueStrings -Values @((Get-Field -Object $classificationRoot -Name 'SUSPICIOUS' -Default @()))
}

$normalizedClusters = New-Object System.Collections.Generic.List[object]
foreach ($cluster in @((Get-Field -Object $clustersDoc -Name 'results' -Default @()))) {
    $clusterNodes = To-SortedUniqueStrings -Values @((Get-Field -Object $cluster -Name 'nodes' -Default @()))
    if ($clusterNodes.Count -eq 0) { continue }

    $clusterSize = To-Int -Value (Get-Field -Object $cluster -Name 'size' -Default $clusterNodes.Count) -Default $clusterNodes.Count
    if ($clusterSize -le 0) { $clusterSize = $clusterNodes.Count }

    $normalizedClusters.Add([ordered]@{
        size = [int]$clusterSize
        nodes = @($clusterNodes)
    })
}

$normalizedClusters = @($normalizedClusters.ToArray() | Sort-Object { -1 * [int]$_.size }, { [string]$_.nodes[0] })

$clusterNodeSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($cluster in $normalizedClusters) {
    foreach ($id in @($cluster.nodes)) {
        if ($nodeById.ContainsKey($id)) {
            $null = $clusterNodeSet.Add($id)
        }
    }
}

$unmarkedUnreachableNodes = New-Object System.Collections.Generic.List[string]
foreach ($id in @($clusterNodeSet | Sort-Object)) {
    if (-not $explicitIsolationSet.Contains($id)) {
        $unmarkedUnreachableNodes.Add($id)
    }
}
$unmarkedUnreachableNodes = @($unmarkedUnreachableNodes.ToArray() | Sort-Object -Unique)

$resolverDisagreements = @((Get-Field -Object $resolverDoc -Name 'disagreements' -Default @()))
$resolverHighCount = @($resolverDisagreements | Where-Object { ([string](Get-Field -Object $_ -Name 'severity' -Default '')).ToUpperInvariant() -eq 'HIGH' }).Count
$resolverTotalCount = @($resolverDisagreements).Count
$resolverDrift = Clamp01 -Value (To-Double -Value (Get-Field -Object $resolverDoc -Name 'drift_score' -Default 0.0) -Default 0.0)

$diMissingNodeSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($d in @($resolverDisagreements | Sort-Object node, issue)) {
    $issue = ([string](Get-Field -Object $d -Name 'issue' -Default '')).ToUpperInvariant()
    $nodeId = Normalize-NodeId -Value ([string](Get-Field -Object $d -Name 'node' -Default ''))
    $isDiSignal = ($issue -match 'MISSING_DI|DI_EDGE|DI_GRAPH_EDGE|DI_RESOLVER_EDGE|DI_WIRED')

    if ($isDiSignal -and -not [string]::IsNullOrWhiteSpace($nodeId) -and $nodeById.ContainsKey($nodeId)) {
        $null = $diMissingNodeSet.Add($nodeId)
    }
}

foreach ($record in @((Get-Field -Object $semanticDoc -Name 'isolation_report' -Default @()))) {
    foreach ($nodeId in @((Get-Field -Object $record -Name 'broken_di_nodes' -Default @()))) {
        $normalized = Normalize-NodeId -Value ([string]$nodeId)
        if (-not [string]::IsNullOrWhiteSpace($normalized) -and $nodeById.ContainsKey($normalized)) {
            $null = $diMissingNodeSet.Add($normalized)
        }
    }
}

$diMissingNodes = @($diMissingNodeSet | Sort-Object)
$diNodesMissingEdgesCount = $diMissingNodes.Count

$cyclePolicyViolations = To-Int -Value (Get-Field -Object (Get-Field -Object $semanticDoc -Name 'metrics' -Default $null) -Name 'cycle_policy_violation_count' -Default 0) -Default 0
if ($cyclePolicyViolations -le 0) {
    $cyclePolicyViolations = 0
    foreach ($anomaly in @((Get-Field -Object $semanticDoc -Name 'anomalies' -Default @()))) {
        $type = ([string](Get-Field -Object $anomaly -Name 'type' -Default '')).ToUpperInvariant()
        if ($type -eq 'CYCLE_POLICY_VIOLATION') {
            $count = To-Int -Value (Get-Field -Object $anomaly -Name 'count' -Default 1) -Default 1
            $cyclePolicyViolations += [Math]::Max(1, $count)
        }
    }
}

$invalidEdgeCount = 0
$graphMetrics = Get-Field -Object $graphValidationDoc -Name 'metrics' -Default $null
if ($null -ne $graphMetrics) {
    $invalidEdgeCount = To-Int -Value (Get-Field -Object $graphMetrics -Name 'invalid_edge_count' -Default 0) -Default 0
    if ($invalidEdgeCount -le 0) {
        $invalidEdgeCount = (
            (To-Int -Value (Get-Field -Object $graphMetrics -Name 'unresolved_edge_count' -Default 0) -Default 0) +
            (To-Int -Value (Get-Field -Object $graphMetrics -Name 'malformed_edge_count' -Default 0) -Default 0) +
            (To-Int -Value (Get-Field -Object $graphMetrics -Name 'invalid_edge_type_count' -Default 0) -Default 0) +
            (To-Int -Value (Get-Field -Object $graphMetrics -Name 'invalid_type_edge_count' -Default 0) -Default 0)
        )
    }
}

if ($invalidEdgeCount -le 0) {
    $structuralStats = Get-Field -Object $graphStructuralDoc -Name 'stats' -Default $null
    $invalidEdgeCount = To-Int -Value (Get-Field -Object $structuralStats -Name 'invalid_edges' -Default 0) -Default 0
}

$structuralErrorCount = @((Get-Field -Object $graphStructuralDoc -Name 'errors' -Default @())).Count + @((Get-Field -Object $graphValidationDoc -Name 'issues' -Default @())).Count

$structuralNodeSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($entry in @((Get-Field -Object $graphStructuralDoc -Name 'errors' -Default @()))) {
    $nodeId = Normalize-NodeId -Value ([string](Get-Field -Object $entry -Name 'node' -Default ''))
    if (-not [string]::IsNullOrWhiteSpace($nodeId) -and $nodeById.ContainsKey($nodeId)) {
        $null = $structuralNodeSet.Add($nodeId)
    }
}

foreach ($issue in @((Get-Field -Object $graphValidationDoc -Name 'issues' -Default @()))) {
    foreach ($sampleValue in @((Get-Field -Object $issue -Name 'sample' -Default @()))) {
        $candidate = Normalize-NodeId -Value ([string]$sampleValue)
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and $nodeById.ContainsKey($candidate)) {
            $null = $structuralNodeSet.Add($candidate)
        }
    }

    foreach ($sampleValue in @((Get-Field -Object $issue -Name 'sample_nodes' -Default @()))) {
        $candidate = Normalize-NodeId -Value ([string]$sampleValue)
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and $nodeById.ContainsKey($candidate)) {
            $null = $structuralNodeSet.Add($candidate)
        }
    }
}

if ($structuralNodeSet.Count -eq 0 -and $invalidEdgeCount -gt 0) {
    foreach ($id in @($nodeIds | Sort-Object { -1 * [int]$degreeByNode[$_] }, { $_ } | Select-Object -First 5)) {
        $null = $structuralNodeSet.Add($id)
    }
}

$resolverNodeSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($entry in @($resolverDisagreements | Sort-Object node, issue)) {
    $nodeId = Normalize-NodeId -Value ([string](Get-Field -Object $entry -Name 'node' -Default ''))
    if (-not [string]::IsNullOrWhiteSpace($nodeId) -and $nodeById.ContainsKey($nodeId)) {
        $null = $resolverNodeSet.Add($nodeId)
    }
}
foreach ($id in $suspiciousNodes) {
    if ($nodeById.ContainsKey($id)) {
        $null = $resolverNodeSet.Add($id)
    }
}

$semanticNodeSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($anomaly in @((Get-Field -Object $semanticDoc -Name 'anomalies' -Default @()))) {
    $type = ([string](Get-Field -Object $anomaly -Name 'type' -Default '')).ToUpperInvariant()
    if ($type -ne 'CYCLE_POLICY_VIOLATION') { continue }
    foreach ($id in @((Get-Field -Object $anomaly -Name 'sample_nodes' -Default @()))) {
        $normalized = Normalize-NodeId -Value ([string]$id)
        if (-not [string]::IsNullOrWhiteSpace($normalized) -and $nodeById.ContainsKey($normalized)) {
            $null = $semanticNodeSet.Add($normalized)
        }
    }
}

$symptomSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($id in @($orphanNodes + $unmarkedUnreachableNodes + $deadNodes)) {
    if ($nodeById.ContainsKey($id)) {
        $null = $symptomSet.Add($id)
    }
}
$symptomNodes = @($symptomSet | Sort-Object)

$invalidEdgeRatio = if ($edgeCount -gt 0) { $invalidEdgeCount / [double]$edgeCount } else { 0.0 }
$structuralErrorRatio = if ($nodeCount -gt 0) { $structuralErrorCount / [double]$nodeCount } else { 0.0 }
$diMissingRatio = if ($nodeCount -gt 0) { $diNodesMissingEdgesCount / [double]$nodeCount } else { 0.0 }
$resolverHighRatio = if ($resolverTotalCount -gt 0) { $resolverHighCount / [double]$resolverTotalCount } else { 0.0 }
$resolverNodeRatio = if ($nodeCount -gt 0) { $resolverHighCount / [double]$nodeCount } else { 0.0 }
$unmarkedUnreachableRatio = if ($nodeCount -gt 0) { $unmarkedUnreachableNodes.Count / [double]$nodeCount } else { 0.0 }
$clusterRatio = if ($nodeCount -gt 0) { $normalizedClusters.Count / [double]$nodeCount } else { 0.0 }
$cycleRatio = if ($nodeCount -gt 0) { $cyclePolicyViolations / [double]$nodeCount } else { 0.0 }

$structuralImpact = Round3 -Value ([Math]::Min(0.45, ($invalidEdgeRatio * 0.30) + ($structuralErrorRatio * 0.15)))
$dependencyImpact = Round3 -Value ([Math]::Min(0.35, ($diMissingRatio * 0.25) + ($resolverHighRatio * 0.10)))
$resolverImpact = Round3 -Value ([Math]::Min(0.25, (Clamp01 -Value $resolverDrift) * 0.20 + ($resolverNodeRatio * 0.05)))
$topologyImpact = Round3 -Value ([Math]::Min(0.35, ($unmarkedUnreachableRatio * 0.20) + ($clusterRatio * 0.10) + ($cycleRatio * 0.05)))

$healthScore = Round3 -Value (Clamp01 -Value (1.0 - $structuralImpact - $dependencyImpact - $resolverImpact - $topologyImpact))
$healthState = if ($healthScore -ge 0.85) { 'HEALTHY' } elseif ($healthScore -ge 0.60) { 'DEGRADED' } else { 'CRITICAL' }

$hasStructuralSignal = ($invalidEdgeCount -gt 0 -or $structuralErrorCount -gt 0)
$hasDependencySignal = ($diNodesMissingEdgesCount -gt 0)
$hasResolverSignal = ($resolverDrift -gt 0.0 -or $resolverHighCount -gt 0)
$hasTopologySignal = ($unmarkedUnreachableNodes.Count -gt 0 -or $normalizedClusters.Count -gt 0)
$hasSemanticSignal = ($cyclePolicyViolations -gt 0)

$candidateList = New-Object System.Collections.Generic.List[object]

if ($hasStructuralSignal) {
    $candidateList.Add([ordered]@{
        domain = 'structural'
        description = 'Unresolved or malformed graph references corrupted structural integrity and propagated downstream reachability symptoms.'
        nodes = @($structuralNodeSet | Sort-Object)
        base_impact = [double]$structuralImpact
        cap = 0.45
        cluster_type = 'structural_corruption'
    })
}

if ($hasDependencySignal) {
    $dependencyNodes = @($diMissingNodes)
    if ($dependencyNodes.Count -eq 0) {
        $dependencyNodes = @($suspiciousNodes | Select-Object -First 25)
    }

    $candidateList.Add([ordered]@{
        domain = 'dependency'
        description = 'Missing dependency edges in DI wiring broke upstream dependency chains and produced downstream unreachable/orphan symptoms.'
        nodes = @($dependencyNodes)
        base_impact = [double]$dependencyImpact
        cap = 0.35
        cluster_type = 'dependency_break'
    })
}

if ($hasResolverSignal) {
    $resolverNodes = @($resolverNodeSet | Sort-Object)
    if ($resolverNodes.Count -eq 0) {
        $resolverNodes = @($suspiciousNodes | Select-Object -First 25)
    }

    $candidateList.Add([ordered]@{
        domain = 'resolver'
        description = 'AST and DI resolver drift created conflicting dependency interpretations that amplified downstream topology instability.'
        nodes = @($resolverNodes)
        base_impact = [double]$resolverImpact
        cap = 0.25
        cluster_type = 'resolver_drift'
    })
}

if ($hasSemanticSignal) {
    $semanticNodes = @($semanticNodeSet | Sort-Object)
    $candidateList.Add([ordered]@{
        domain = 'semantic'
        description = 'Cycle policy violations introduced unstable semantic paths and increased propagation risk across connected components.'
        nodes = @($semanticNodes)
        base_impact = [double]$topologyImpact
        cap = 0.35
        cluster_type = 'isolation'
    })
}

if ($hasTopologySignal -and -not ($hasStructuralSignal -or $hasDependencySignal -or $hasResolverSignal)) {
    $candidateList.Add([ordered]@{
        domain = 'topology'
        description = 'Unmarked unreachable subgraphs indicate an upstream path break that disconnected entrypoint flow across components.'
        nodes = @($unmarkedUnreachableNodes)
        base_impact = [double]$topologyImpact
        cap = 0.35
        cluster_type = 'isolation'
    })
}

$rootCauseRecords = New-Object System.Collections.Generic.List[object]
foreach ($candidate in $candidateList) {
    $domain = [string]$candidate.domain
    $candidateNodes = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    foreach ($id in @($candidate.nodes)) {
        if ($nodeById.ContainsKey($id)) {
            $null = $candidateNodes.Add([string]$id)
        }
    }

    $affectedNodes = @($candidateNodes | Sort-Object)
    $affectedCount = $affectedNodes.Count

    $breadth = if ($nodeCount -gt 0) { $affectedCount / [double]$nodeCount } else { 0.0 }

    $centrality = 0.0
    if ($affectedCount -gt 0) {
        $sum = 0.0
        foreach ($id in $affectedNodes) {
            $sum += To-Double -Value $degreeNormByNode[$id] -Default 0.0
        }
        $centrality = $sum / [double]$affectedCount
    }

    $depth = 0.0
    if ($affectedCount -gt 0) {
        $sumDepth = 0.0
        foreach ($id in $affectedNodes) {
            $sumDepth += To-Double -Value $depthNormByNode[$id] -Default 0.0
        }
        $depth = $sumDepth / [double]$affectedCount
    }

    $propagation = $breadth
    if ($affectedCount -gt 0 -and $symptomNodes.Count -gt 0) {
        $distanceMap = Get-DistanceMap -StartNodes $affectedNodes -AdjacencySet $undirectedSet
        $maxDistance = 1
        foreach ($value in $distanceMap.Values) {
            $d = To-Int -Value $value -Default 0
            if ($d -gt $maxDistance) { $maxDistance = $d }
        }

        $distanceSum = 0.0
        foreach ($symptom in $symptomNodes) {
            if ($distanceMap.ContainsKey($symptom)) {
                $distanceSum += To-Int -Value $distanceMap[$symptom] -Default ($maxDistance + 1)
            }
            else {
                $distanceSum += ($maxDistance + 1)
            }
        }

        $avgDistance = $distanceSum / [double][Math]::Max(1, $symptomNodes.Count)
        $propagation = Clamp01 -Value (1.0 - ($avgDistance / [double][Math]::Max(1, ($maxDistance + 1))))
    }

    $baseCap = To-Double -Value $candidate.cap -Default 1.0
    if ($baseCap -le 0.0) { $baseCap = 1.0 }
    $baseImpact = Clamp01 -Value ((To-Double -Value $candidate.base_impact -Default 0.0) / $baseCap)

    $impactScore = Round3 -Value (Clamp01 -Value (
        ($baseImpact * 0.40) +
        ($breadth * 0.25) +
        ($centrality * 0.15) +
        ($depth * 0.10) +
        ($propagation * 0.10)
    ))

    $subgraphSize = Get-ComponentSizeFromSeed -SeedNodes $affectedNodes -AdjacencySet $undirectedSet

    $rootCauseRecords.Add([ordered]@{
        domain = $domain
        description = [string]$candidate.description
        impact_score = [double]$impactScore
        affected_nodes = @($affectedNodes)
        affected_subgraph_size = [int]$subgraphSize
        cluster_type = [string]$candidate.cluster_type
    })
}

$rankedRootCauses = @(
    $rootCauseRecords.ToArray() |
        Sort-Object { -1 * [double]$_.impact_score }, { -1 * [int]$_.affected_subgraph_size }, { [string]$_.domain }, { [string]$_.description }
)

$maxRootCauses = if ($nodeCount -gt 3000) { 15 } else { 10 }
$selectedRootCauses = @($rankedRootCauses | Select-Object -First $maxRootCauses)

$rootCauses = New-Object System.Collections.Generic.List[object]
for ($i = 0; $i -lt $selectedRootCauses.Count; $i++) {
    $item = $selectedRootCauses[$i]
    $rootCauses.Add([ordered]@{
        cause_id = ('cause-{0}-{1:d3}' -f ([string]$item.domain), ($i + 1))
        domain = [string]$item.domain
        description = [string]$item.description
        impact_score = [double](Round3 -Value (To-Double -Value $item.impact_score -Default 0.0))
        affected_nodes = @($item.affected_nodes | Select-Object -First 200)
        affected_subgraph_size = [int]$item.affected_subgraph_size
    })
}

$clusterCounters = [ordered]@{
    isolation = 0
    dependency_break = 0
    resolver_drift = 0
    structural_corruption = 0
}

$failureClusters = New-Object System.Collections.Generic.List[object]

foreach ($cluster in $normalizedClusters) {
    $clusterCounters.isolation = [int]$clusterCounters.isolation + 1
    $brokenSignal = $false
    foreach ($id in @($cluster.nodes)) {
        if ($diMissingNodeSet.Contains($id)) {
            $brokenSignal = $true
            break
        }
    }

    $severity = Get-ClusterSeverity -ClusterNodeCount $cluster.nodes.Count -SystemNodeCount $nodeCount -ImpactScore 0.0 -HasBrokenDependencySignal:$brokenSignal
    $failureClusters.Add([ordered]@{
        cluster_id = ('cluster-isolation-{0:d3}' -f $clusterCounters.isolation)
        type = 'isolation'
        nodes = @($cluster.nodes)
        edges = @(Get-ClusterEdges -NodeIds @($cluster.nodes) -GraphEdges $edges -InternalOnly:$true -Limit 120)
        severity = $severity
    })
}

$rootCauseByDomain = @{}
foreach ($cause in $rootCauses) {
    if (-not $rootCauseByDomain.ContainsKey([string]$cause.domain)) {
        $rootCauseByDomain[[string]$cause.domain] = $cause
    }
}

if ($rootCauseByDomain.ContainsKey('structural')) {
    $cause = $rootCauseByDomain['structural']
    if (@($cause.affected_nodes).Count -gt 0) {
        $clusterCounters.structural_corruption = [int]$clusterCounters.structural_corruption + 1
        $severity = Get-ClusterSeverity -ClusterNodeCount @($cause.affected_nodes).Count -SystemNodeCount $nodeCount -ImpactScore (To-Double -Value $cause.impact_score -Default 0.0)
        $failureClusters.Add([ordered]@{
            cluster_id = ('cluster-structural_corruption-{0:d3}' -f $clusterCounters.structural_corruption)
            type = 'structural_corruption'
            nodes = @($cause.affected_nodes)
            edges = @(Get-ClusterEdges -NodeIds @($cause.affected_nodes) -GraphEdges $edges -InternalOnly:$false -Limit 120)
            severity = $severity
        })
    }
}

if ($rootCauseByDomain.ContainsKey('dependency')) {
    $cause = $rootCauseByDomain['dependency']
    if (@($cause.affected_nodes).Count -gt 0) {
        $clusterCounters.dependency_break = [int]$clusterCounters.dependency_break + 1
        $severity = Get-ClusterSeverity -ClusterNodeCount @($cause.affected_nodes).Count -SystemNodeCount $nodeCount -ImpactScore (To-Double -Value $cause.impact_score -Default 0.0) -HasBrokenDependencySignal:$true
        $failureClusters.Add([ordered]@{
            cluster_id = ('cluster-dependency_break-{0:d3}' -f $clusterCounters.dependency_break)
            type = 'dependency_break'
            nodes = @($cause.affected_nodes)
            edges = @(Get-ClusterEdges -NodeIds @($cause.affected_nodes) -GraphEdges $edges -InternalOnly:$false -Limit 120)
            severity = $severity
        })
    }
}

if ($rootCauseByDomain.ContainsKey('resolver')) {
    $cause = $rootCauseByDomain['resolver']
    if (@($cause.affected_nodes).Count -gt 0) {
        $clusterCounters.resolver_drift = [int]$clusterCounters.resolver_drift + 1
        $severity = Get-ClusterSeverity -ClusterNodeCount @($cause.affected_nodes).Count -SystemNodeCount $nodeCount -ImpactScore (To-Double -Value $cause.impact_score -Default 0.0)
        $failureClusters.Add([ordered]@{
            cluster_id = ('cluster-resolver_drift-{0:d3}' -f $clusterCounters.resolver_drift)
            type = 'resolver_drift'
            nodes = @($cause.affected_nodes)
            edges = @(Get-ClusterEdges -NodeIds @($cause.affected_nodes) -GraphEdges $edges -InternalOnly:$false -Limit 120)
            severity = $severity
        })
    }
}

$failureClusters = @(
    $failureClusters.ToArray() |
        Sort-Object { Get-SeverityRank -Severity ([string]$_.severity) }, { [string]$_.type }, { [string]$_.cluster_id }
)

$majorClusters = @($failureClusters | Where-Object { ([string]$_.severity) -in @('HIGH', 'CRITICAL') })
if ($majorClusters.Count -eq 0 -and $failureClusters.Count -gt 0) {
    $majorClusters = @($failureClusters[0])
}

$causalChains = New-Object System.Collections.Generic.List[object]
$chainIndex = 0
foreach ($cluster in $majorClusters) {
    $chainIndex += 1
    $clusterType = [string]$cluster.type
    $sequence = @()

    switch ($clusterType) {
        'dependency_break' {
            $sequence = @(
                [ordered]@{ stage = 'layer3-resolve'; event = 'Dependency resolution missed required DI edges'; effect = 'Upstream dependency chain lost continuity' },
                [ordered]@{ stage = 'compare-resolvers'; event = 'Resolver disagreement confirmed missing/shifted dependency wiring'; effect = 'Downstream dependency confidence degraded' },
                [ordered]@{ stage = 'semantic-validate'; event = 'Propagation produced unreachable dependency consumers'; effect = 'Dependency-break cluster formed' }
            )
        }
        'resolver_drift' {
            $sequence = @(
                [ordered]@{ stage = 'layer3-resolve'; event = 'AST and DI resolver outputs diverged'; effect = 'Competing dependency interpretations emerged' },
                [ordered]@{ stage = 'compare-resolvers'; event = 'Drift and disagreement evidence accumulated'; effect = 'Resolver stability dropped' },
                [ordered]@{ stage = 'semantic-validate'; event = 'Reachability behavior shifted under inconsistent edges'; effect = 'Resolver-drift cluster formed' }
            )
        }
        'structural_corruption' {
            $sequence = @(
                [ordered]@{ stage = 'layer4-graph'; event = 'Unresolved or malformed references persisted into unified graph'; effect = 'Graph structure reliability dropped' },
                [ordered]@{ stage = 'layer5-validate'; event = 'Structural validation reported corrupted references'; effect = 'Downstream dependency interpretation became unstable' },
                [ordered]@{ stage = 'semantic-validate'; event = 'Topology anomalies amplified under corrupted structure'; effect = 'Structural-corruption cluster formed' }
            )
        }
        default {
            $sequence = @(
                [ordered]@{ stage = 'layer3-resolve'; event = 'Dependency linkage weakened at resolution stage'; effect = 'Reachability pathways contracted' },
                [ordered]@{ stage = 'layer4-graph'; event = 'Disconnected relationships persisted in the graph'; effect = 'Subgraphs detached from entrypoint flow' },
                [ordered]@{ stage = 'semantic-validate'; event = 'Detached nodes remained unmarked for isolation'; effect = 'Isolation cluster formed' }
            )
        }
    }

    $causalChains.Add([ordered]@{
        chain_id = ('chain-{0:d3}' -f $chainIndex)
        sequence = @($sequence)
        resulting_failure = ('{0}:{1}' -f $clusterType, [string]$cluster.cluster_id)
    })
}

$anomalyList = New-Object System.Collections.Generic.List[object]

if ($invalidEdgeCount -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'invalid_edges'
        description = 'Graph contains unresolved, malformed, or unsupported edges that reduce structural integrity.'
        frequency = [int]$invalidEdgeCount
    })
}
if ($orphanNodes.Count -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'orphan_nodes'
        description = 'Nodes with no effective connectivity surfaced as downstream symptoms of upstream dependency or structural breaks.'
        frequency = [int]$orphanNodes.Count
    })
}
if ($normalizedClusters.Count -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'disconnected_clusters'
        description = 'Disconnected subgraphs indicate propagated reachability degradation across the dependency graph.'
        frequency = [int]$normalizedClusters.Count
    })
}
if ($diNodesMissingEdgesCount -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'di_nodes_missing_edges'
        description = 'DI-linked nodes are missing dependency edges, indicating upstream dependency breakpoints.'
        frequency = [int]$diNodesMissingEdgesCount
    })
}
if ($unmarkedUnreachableNodes.Count -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'unmarked_unreachable_nodes'
        description = 'Unreachable nodes without explicit isolation markers represent unapproved propagation of topology failure.'
        frequency = [int]$unmarkedUnreachableNodes.Count
    })
}
if ($cyclePolicyViolations -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'cycle_policy_violations'
        description = 'Cycle-policy violations increase semantic instability and can amplify downstream propagation.'
        frequency = [int]$cyclePolicyViolations
    })
}
if ($resolverHighCount -gt 0) {
    $anomalyList.Add([ordered]@{
        type = 'resolver_high_disagreements'
        description = 'High-severity resolver disagreements indicate dependency interpretation instability.'
        frequency = [int]$resolverHighCount
    })
}

$anomalyList = @(
    $anomalyList.ToArray() |
        Sort-Object { -1 * [int]$_.frequency }, { [string]$_.type }
)

$result = [ordered]@{
    diagnostic_model_version = '1.0'
    system_health = [ordered]@{
        health_state = $healthState
        health_score = [double]$healthScore
    }
    root_causes = @($rootCauses.ToArray())
    failure_clusters = @($failureClusters)
    causal_chains = @($causalChains.ToArray())
    system_anomalies = @($anomalyList)
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 80 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result