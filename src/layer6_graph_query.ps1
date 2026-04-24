[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [Parameter(Mandatory = $false)]
    [string]$ValidationPath,
    [Parameter(Mandatory = $false)]
    [string]$AuthorityPath,
    [Parameter(Mandatory = $true)]
    [string]$Query,
    [string[]]$Entrypoints,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $GraphPath -PathType Leaf)) {
    throw "Graph file not found: $GraphPath"
}

$graphDoc = Get-Content -LiteralPath $GraphPath -Raw | ConvertFrom-Json -Depth 40

if ($null -eq $graphDoc.graph -or $null -eq $graphDoc.graph.nodes -or $null -eq $graphDoc.graph.edges) {
    throw 'Invalid graph format. Expected graph.nodes and graph.edges.'
}

function Normalize-QueryName {
    param([string]$Name)

    if ([string]::IsNullOrWhiteSpace($Name)) { return '' }
    $n = $Name.Trim().ToUpperInvariant()
    if ($n -eq 'REACHABLE_FROM(ENTRYPOINTS)') { return 'REACHABLE_FROM' }
    return $n
}

function Normalize-Id {
    param([string]$Id)

    if ([string]::IsNullOrWhiteSpace($Id)) { return '' }
    return $Id.Trim()
}

function Get-IsDiNode {
    param([object]$Node)

    if ($Node.PSObject.Properties.Name -contains 'type' -and [string]$Node.type -eq 'DI_WIRED') {
        return $true
    }

    if ($Node.PSObject.Properties.Name -contains 'role' -and [string]$Node.role -eq 'DI_WIRED') {
        return $true
    }

    if ($Node.PSObject.Properties.Name -contains 'tags' -and $null -ne $Node.tags) {
        foreach ($tag in @($Node.tags)) {
            if ([string]$tag -eq 'DI_WIRED') {
                return $true
            }
        }
    }

    return $false
}

$normalizedQuery = Normalize-QueryName -Name $Query
$allowed = @('REACHABLE_FROM', 'ORPHAN_NODES', 'DEAD_NODES', 'SUSPICIOUS_DI_NODES', 'DISCONNECTED_CLUSTERS')
if ($normalizedQuery -notin $allowed) {
    throw "Unsupported query: $Query"
}

$nodes = @($graphDoc.graph.nodes)
$edges = @($graphDoc.graph.edges)

$nodeById = @{}
$nodeIds = New-Object System.Collections.Generic.List[string]
$outbound = @{}
$inbound = @{}
$adj = @{}

foreach ($n in @($nodes | Sort-Object id)) {
    $id = Normalize-Id -Id ([string]$n.id)
    if ([string]::IsNullOrWhiteSpace($id)) { continue }
    if (-not $nodeById.ContainsKey($id)) {
        $nodeById[$id] = $n
        $nodeIds.Add($id)
        $outbound[$id] = New-Object System.Collections.Generic.List[string]
        $inbound[$id] = New-Object System.Collections.Generic.List[string]
        $adj[$id] = New-Object System.Collections.Generic.List[string]
    }
}

foreach ($e in @($edges | Sort-Object from, to, type)) {
    $from = Normalize-Id -Id ([string]$e.from)
    $to = Normalize-Id -Id ([string]$e.to)

    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to)) { continue }
    if (-not $nodeById.ContainsKey($from) -or -not $nodeById.ContainsKey($to)) { continue }

    if (-not $outbound[$from].Contains($to)) {
        $outbound[$from].Add($to)
    }
    if (-not $inbound[$to].Contains($from)) {
        $inbound[$to].Add($from)
    }

    if (-not $adj[$from].Contains($to)) {
        $adj[$from].Add($to)
    }
    if (-not $adj[$to].Contains($from)) {
        $adj[$to].Add($from)
    }
}

$effectiveEntrypoints = @($Entrypoints | ForEach-Object { Normalize-Id -Id ([string]$_) } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)

function Get-ReachableSet {
    param(
        [string[]]$StartNodes,
        [hashtable]$OutboundMap,
        [hashtable]$KnownNodeMap
    )

    $visited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    $queue = New-Object System.Collections.Generic.Queue[string]

    foreach ($s in @($StartNodes | Sort-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($s)) { continue }
        if (-not $KnownNodeMap.ContainsKey($s)) { continue }
        if ($visited.Add($s)) {
            $queue.Enqueue($s)
        }
    }

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        foreach ($next in @($OutboundMap[$current] | Sort-Object)) {
            if ($visited.Add($next)) {
                $queue.Enqueue($next)
            }
        }
    }

    return @($visited | Sort-Object)
}

$results = @()

switch ($normalizedQuery) {
    'REACHABLE_FROM' {
        if ($null -eq $Entrypoints -or @($Entrypoints).Count -eq 0) {
            throw 'Entrypoints are required for REACHABLE_FROM.'
        }

        $starts = @($Entrypoints | ForEach-Object { Normalize-Id -Id ([string]$_) } | Sort-Object -Unique)
        $reachable = Get-ReachableSet -StartNodes $starts -OutboundMap $outbound -KnownNodeMap $nodeById
        $results = @($reachable)
    }
    'ORPHAN_NODES' {
        $orphans = New-Object System.Collections.Generic.List[string]
        foreach ($id in @($nodeIds | Sort-Object)) {
            if ($inbound[$id].Count -eq 0 -and $outbound[$id].Count -eq 0) {
                $orphans.Add($id)
            }
        }
        $results = @($orphans.ToArray() | Sort-Object)
    }
    'DEAD_NODES' {
        if ($effectiveEntrypoints.Count -eq 0) {
            throw 'Entrypoints are required for DEAD_NODES.'
        }

        $starts = @($effectiveEntrypoints)
        $reachableSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
        foreach ($id in @(Get-ReachableSet -StartNodes $starts -OutboundMap $outbound -KnownNodeMap $nodeById)) {
            $null = $reachableSet.Add($id)
        }

        $dead = New-Object System.Collections.Generic.List[string]
        foreach ($id in @($nodeIds | Sort-Object)) {
            if (-not $reachableSet.Contains($id)) {
                $dead.Add($id)
            }
        }
        $results = @($dead.ToArray() | Sort-Object)
    }
    'SUSPICIOUS_DI_NODES' {
        $diNodes = New-Object System.Collections.Generic.List[string]
        foreach ($id in @($nodeIds | Sort-Object)) {
            if (Get-IsDiNode -Node $nodeById[$id]) {
                $diNodes.Add($id)
            }
        }

        $suspicious = New-Object System.Collections.Generic.List[string]
        foreach ($diId in @($diNodes.ToArray() | Sort-Object)) {
            $hasDiConfigEdge = $false
            foreach ($e in @($edges)) {
                $from = Normalize-Id -Id ([string]$e.from)
                $to = Normalize-Id -Id ([string]$e.to)
                if ($from -ne $diId -and $to -ne $diId) { continue }

                $source = [string]$e.source
                $sourceMeta = @()
                if ($e.PSObject.Properties.Name -contains 'source_metadata' -and $null -ne $e.source_metadata) {
                    $sourceMeta = @($e.source_metadata | ForEach-Object { [string]$_ })
                }

                if ($source -in @('DI', 'CONFIG') -or ($sourceMeta -contains 'DI') -or ($sourceMeta -contains 'CONFIG')) {
                    $hasDiConfigEdge = $true
                    break
                }
            }

            if (-not $hasDiConfigEdge) {
                $suspicious.Add($diId)
            }
        }

        $results = @($suspicious.ToArray() | Sort-Object)
    }
    'DISCONNECTED_CLUSTERS' {
        if ($effectiveEntrypoints.Count -eq 0) {
            throw 'Entrypoints are required for DISCONNECTED_CLUSTERS.'
        }

        $reachableSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
        foreach ($id in @(Get-ReachableSet -StartNodes $effectiveEntrypoints -OutboundMap $outbound -KnownNodeMap $nodeById)) {
            $null = $reachableSet.Add($id)
        }

        $unreachableSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
        foreach ($id in @($nodeIds | Sort-Object)) {
            if (-not $reachableSet.Contains($id)) {
                $null = $unreachableSet.Add($id)
            }
        }

        $visited = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
        $clusters = New-Object System.Collections.Generic.List[object]

        foreach ($id in @($unreachableSet | Sort-Object)) {
            if ($visited.Contains($id)) { continue }

            $clusterNodes = New-Object System.Collections.Generic.List[string]
            $queue = New-Object System.Collections.Generic.Queue[string]
            $queue.Enqueue($id)
            $null = $visited.Add($id)

            while ($queue.Count -gt 0) {
                $current = $queue.Dequeue()
                $clusterNodes.Add($current)

                foreach ($next in @($adj[$current] | Sort-Object)) {
                    if ($unreachableSet.Contains($next) -and $visited.Add($next)) {
                        $queue.Enqueue($next)
                    }
                }
            }

            $clusters.Add([ordered]@{
                size = $clusterNodes.Count
                nodes = @($clusterNodes.ToArray() | Sort-Object)
            })
        }

        $results = @($clusters.ToArray() | Sort-Object { -1 * [int]$_.size }, { [string]$_.nodes[0] })
    }
}

$result = [ordered]@{
    query = $Query
    results = $results
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 30 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
