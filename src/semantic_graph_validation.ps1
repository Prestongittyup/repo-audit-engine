[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [Parameter(Mandatory = $true)]
    [string[]]$Entrypoints,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [bool]$FailOnInvalid = $false
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $GraphPath -PathType Leaf)) {
    throw "Graph file not found: $GraphPath"
}

$doc = Get-Content -LiteralPath $GraphPath -Raw | ConvertFrom-Json -Depth 50
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

function Test-NodeDi {
    param([object]$Node)

    if ($null -eq $Node) { return $false }

    if ($Node.PSObject.Properties.Name -contains 'type' -and [string]$Node.type -eq 'DI_WIRED') { return $true }
    if ($Node.PSObject.Properties.Name -contains 'role' -and [string]$Node.role -eq 'DI_WIRED') { return $true }

    if ($Node.PSObject.Properties.Name -contains 'tags' -and $null -ne $Node.tags) {
        foreach ($tag in @($Node.tags)) {
            if ([string]$tag -eq 'DI_WIRED') { return $true }
        }
    }

    if ($Node.PSObject.Properties.Name -contains 'metadata' -and $null -ne $Node.metadata) {
        $md = $Node.metadata
        if ($md.PSObject.Properties.Name -contains 'type' -and [string]$md.type -eq 'DI_WIRED') { return $true }
        if ($md.PSObject.Properties.Name -contains 'role' -and [string]$md.role -eq 'DI_WIRED') { return $true }
        if ($md.PSObject.Properties.Name -contains 'tags' -and $null -ne $md.tags) {
            foreach ($tag in @($md.tags)) {
                if ([string]$tag -eq 'DI_WIRED') { return $true }
            }
        }
    }

    return $false
}

function Test-NodeCycleAllowed {
    param([object]$Node)

    if ($null -eq $Node) { return $false }

    foreach ($key in @('allow_cycle', 'cycle_allowed')) {
        if ($Node.PSObject.Properties.Name -contains $key -and $Node.$key -is [bool] -and [bool]$Node.$key) {
            return $true
        }
    }

    if ($Node.PSObject.Properties.Name -contains 'metadata' -and $null -ne $Node.metadata) {
        $md = $Node.metadata
        foreach ($key in @('allow_cycle', 'cycle_allowed')) {
            if ($md.PSObject.Properties.Name -contains $key -and $md.$key -is [bool] -and [bool]$md.$key) {
                return $true
            }
        }
    }

    return $false
}

function Get-Reachable {
    param(
        [string[]]$Start,
        [hashtable]$Outbound
    )

    $seen = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    $queue = New-Object System.Collections.Generic.Queue[string]

    foreach ($id in @($Start | Sort-Object -Unique)) {
        if ($seen.Add($id)) { $queue.Enqueue($id) }
    }

    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        foreach ($nxt in @($Outbound[$cur])) {
            if ($seen.Add([string]$nxt)) { $queue.Enqueue([string]$nxt) }
        }
    }

    return @($seen | Sort-Object)
}

function Find-CycleNodes {
    param(
        [string[]]$NodeIds,
        [hashtable]$Outbound
    )

    $inbound = @{}
    foreach ($id in $NodeIds) { $inbound[$id] = 0 }

    foreach ($id in $NodeIds) {
        foreach ($nxt in @($Outbound[$id])) {
            if ($inbound.ContainsKey([string]$nxt)) {
                $inbound[[string]$nxt] = [int]$inbound[[string]$nxt] + 1
            }
        }
    }

    $queue = New-Object System.Collections.Generic.Queue[string]
    foreach ($id in @($NodeIds | Sort-Object)) {
        if ([int]$inbound[$id] -eq 0) { $queue.Enqueue($id) }
    }

    $processed = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        $null = $processed.Add($cur)

        foreach ($nxt in @($Outbound[$cur] | Sort-Object)) {
            $inbound[$nxt] = [int]$inbound[$nxt] - 1
            if ([int]$inbound[$nxt] -eq 0) {
                $queue.Enqueue($nxt)
            }
        }
    }

    $cycleNodes = New-Object System.Collections.Generic.List[string]
    foreach ($id in @($NodeIds | Sort-Object)) {
        if (-not $processed.Contains($id)) {
            $cycleNodes.Add($id)
        }
    }

    return @($cycleNodes.ToArray())
}

$nodes = @($graph.nodes)
$edges = @($graph.edges)
$nodeById = @{}
$outbound = @{}
$inboundCount = @{}

foreach ($n in $nodes) {
    $id = Get-NodeId -Id ([string]$n.id)
    if ([string]::IsNullOrWhiteSpace($id)) { continue }
    if (-not $nodeById.ContainsKey($id)) {
        $nodeById[$id] = $n
        $outbound[$id] = New-Object System.Collections.Generic.List[string]
        $inboundCount[$id] = 0
    }
}

foreach ($e in $edges) {
    $from = Get-NodeId -Id ([string]$e.from)
    $to = Get-NodeId -Id ([string]$e.to)
    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to)) { continue }
    if (-not $nodeById.ContainsKey($from) -or -not $nodeById.ContainsKey($to)) { continue }

    if (-not $outbound[$from].Contains($to)) { $outbound[$from].Add($to) }
    $inboundCount[$to] = [int]$inboundCount[$to] + 1
}

$nodeIds = @($nodeById.Keys | Sort-Object)
$entrypointSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($ep in @($Entrypoints | ForEach-Object { Get-NodeId -Id ([string]$_) } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)) {
    if ($nodeById.ContainsKey($ep)) { $null = $entrypointSet.Add($ep) }
}

$reachable = @(Get-Reachable -Start @($entrypointSet | Sort-Object) -Outbound $outbound)
$reachableSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($id in $reachable) { $null = $reachableSet.Add($id) }

$unreachable = New-Object System.Collections.Generic.List[string]
foreach ($id in $nodeIds) {
    if (-not $reachableSet.Contains($id)) { $unreachable.Add($id) }
}

$diNodes = New-Object System.Collections.Generic.List[string]
$brokenDiNodes = New-Object System.Collections.Generic.List[string]
foreach ($id in $nodeIds) {
    $node = $nodeById[$id]
    if (-not (Test-NodeDi -Node $node)) { continue }

    $diNodes.Add($id)
    if ($outbound[$id].Count -eq 0 -and [int]$inboundCount[$id] -eq 0) {
        $brokenDiNodes.Add($id)
    }
}

$cycleNodes = @(Find-CycleNodes -NodeIds $nodeIds -Outbound $outbound)
$cycleViolations = New-Object System.Collections.Generic.List[string]
foreach ($id in @($cycleNodes | Sort-Object -Unique)) {
    if (-not (Test-NodeCycleAllowed -Node $nodeById[$id])) {
        $cycleViolations.Add($id)
    }
}

$adj = @{}
foreach ($id in $nodeIds) { $adj[$id] = New-Object System.Collections.Generic.List[string] }
foreach ($id in $nodeIds) {
    foreach ($nxt in @($outbound[$id])) {
        if (-not $adj[$id].Contains($nxt)) { $adj[$id].Add($nxt) }
        if (-not $adj[$nxt].Contains($id)) { $adj[$nxt].Add($id) }
    }
}

$unreachableSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($id in @($unreachable.ToArray())) { $null = $unreachableSet.Add($id) }

$visited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$isolationReport = New-Object System.Collections.Generic.List[object]
$islandFailures = New-Object System.Collections.Generic.List[object]

foreach ($start in @($unreachableSet | Sort-Object)) {
    if ($visited.Contains($start)) { continue }

    $cluster = New-Object System.Collections.Generic.List[string]
    $queue = New-Object System.Collections.Generic.Queue[string]
    $queue.Enqueue($start)
    $null = $visited.Add($start)

    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        $cluster.Add($cur)
        foreach ($nxt in @($adj[$cur] | Sort-Object)) {
            if ($unreachableSet.Contains($nxt) -and $visited.Add($nxt)) {
                $queue.Enqueue($nxt)
            }
        }
    }

    $clusterNodes = @($cluster.ToArray() | Sort-Object -Unique)
    $clusterEntry = @($clusterNodes | Where-Object { $entrypointSet.Contains($_) })
    $clusterBrokenDi = @($clusterNodes | Where-Object { $_ -in @($brokenDiNodes.ToArray()) })

    $record = [ordered]@{
        size = $clusterNodes.Count
        nodes = @($clusterNodes | Select-Object -First 25)
        contains_entrypoint = ($clusterEntry.Count -gt 0)
        broken_di_nodes = @($clusterBrokenDi | Select-Object -First 25)
    }

    $isolationReport.Add($record)

    if ($record.contains_entrypoint -or $record.broken_di_nodes.Count -gt 0) {
        $islandFailures.Add($record)
    }
}

$anomalies = New-Object System.Collections.Generic.List[object]
if ($brokenDiNodes.Count -gt 0) {
    $anomalies.Add([ordered]@{
        type = 'BROKEN_DI_WIRING'
        severity = 'HIGH'
        count = $brokenDiNodes.Count
        sample_nodes = @($brokenDiNodes.ToArray() | Sort-Object -Unique | Select-Object -First 25)
    })
}
if ($cycleViolations.Count -gt 0) {
    $anomalies.Add([ordered]@{
        type = 'CYCLE_POLICY_VIOLATION'
        severity = 'MEDIUM'
        count = $cycleViolations.Count
        sample_nodes = @($cycleViolations.ToArray() | Sort-Object -Unique | Select-Object -First 25)
    })
}
if ($islandFailures.Count -gt 0) {
    $anomalies.Add([ordered]@{
        type = 'STRUCTURAL_ISLAND_POLICY_BREAK'
        severity = 'HIGH'
        count = $islandFailures.Count
    })
}

$warnings = New-Object System.Collections.Generic.List[string]
if ($isolationReport.Count -gt 0) {
    $warnings.Add('Unreachable structural islands were detected. See isolation_report for details.')
}

$semanticValid = ($anomalies.Count -eq 0)
$result = [ordered]@{
    semantic_valid = [bool]$semanticValid
    status = if ($semanticValid) { 'ANALYZED' } else { 'DEGRADED' }
    metrics = [ordered]@{
        node_count = $nodeIds.Count
        di_wired_count = $diNodes.Count
        broken_di_count = $brokenDiNodes.Count
        cycle_node_count = @($cycleNodes | Sort-Object -Unique).Count
        cycle_policy_violation_count = $cycleViolations.Count
        structural_island_count = $isolationReport.Count
        island_failure_count = $islandFailures.Count
    }
    anomalies = @($anomalies.ToArray())
    warnings = @($warnings.ToArray())
    isolation_report = @($isolationReport.ToArray() | Sort-Object { -1 * [int]$_.size }, { [string]$_.nodes[0] })
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 20 | Out-File -LiteralPath $outFull -Encoding UTF8

if (-not $semanticValid -and $FailOnInvalid) {
    throw "Semantic validation detected policy anomalies. See report: $outFull"
}

return $result
