[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ValidationPath,
    [Parameter(Mandatory = $false)]
    [string]$AuthorityPath,
    [Parameter(Mandatory = $true)]
    [string]$ReachableQueryPath,
    [Parameter(Mandatory = $true)]
    [string]$OrphanQueryPath,
    [Parameter(Mandatory = $true)]
    [string]$DeadQueryPath,
    [Parameter(Mandatory = $true)]
    [string]$SuspiciousQueryPath,
    [Parameter(Mandatory = $true)]
    [string]$DisconnectedClustersQueryPath,
    [string]$ExemptQueryPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

foreach ($p in @($ValidationPath, $ReachableQueryPath, $OrphanQueryPath, $DeadQueryPath, $SuspiciousQueryPath, $DisconnectedClustersQueryPath)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw "Required input file not found: $p"
    }
}

if (-not [string]::IsNullOrWhiteSpace($ExemptQueryPath) -and -not (Test-Path -LiteralPath $ExemptQueryPath -PathType Leaf)) {
    throw "Exempt query file not found: $ExemptQueryPath"
}

$validation = Get-Content -LiteralPath $ValidationPath -Raw | ConvertFrom-Json -Depth 10

function Get-QueryResults {
    param(
        [string]$Path,
        [string]$ExpectedQueryName
    )

    $doc = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth 40
    if ($null -eq $doc.query -or $null -eq $doc.results) {
        throw "Invalid query output format: $Path"
    }

    $actual = ([string]$doc.query).Trim().ToUpperInvariant()
    $expected = $ExpectedQueryName.Trim().ToUpperInvariant()
    if ($actual -ne $expected) {
        throw "Unexpected query type in $Path. Expected '$ExpectedQueryName', got '$($doc.query)'."
    }

    return $doc.results
}

function To-SortedUniqueStrings {
    param([object[]]$Values)

    if ($null -eq $Values) { return @() }
    return @($Values | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
}

function Flatten-ClusterNodes {
    param([object[]]$Clusters)

    $flattened = New-Object System.Collections.Generic.List[string]
    foreach ($cluster in @($Clusters)) {
        if ($null -eq $cluster) { continue }
        if (-not ($cluster.PSObject.Properties.Name -contains 'nodes')) { continue }
        foreach ($n in @($cluster.nodes)) {
            $v = [string]$n
            if (-not [string]::IsNullOrWhiteSpace($v)) {
                $flattened.Add($v)
            }
        }
    }
    return @($flattened.ToArray() | Sort-Object -Unique)
}

$reachable = To-SortedUniqueStrings -Values @(Get-QueryResults -Path $ReachableQueryPath -ExpectedQueryName 'REACHABLE_FROM(entrypoints)')
$isolated = To-SortedUniqueStrings -Values @(Get-QueryResults -Path $OrphanQueryPath -ExpectedQueryName 'ORPHAN_NODES')
$dead = To-SortedUniqueStrings -Values @(Get-QueryResults -Path $DeadQueryPath -ExpectedQueryName 'DEAD_NODES')
$suspicious = To-SortedUniqueStrings -Values @(Get-QueryResults -Path $SuspiciousQueryPath -ExpectedQueryName 'SUSPICIOUS_DI_NODES')
$clusters = @(Get-QueryResults -Path $DisconnectedClustersQueryPath -ExpectedQueryName 'DISCONNECTED_CLUSTERS')
$referencedRaw = Flatten-ClusterNodes -Clusters $clusters

$exempt = @()
if (-not [string]::IsNullOrWhiteSpace($ExemptQueryPath)) {
    $exemptDoc = Get-Content -LiteralPath $ExemptQueryPath -Raw | ConvertFrom-Json -Depth 20
    if ($null -eq $exemptDoc.results) {
        throw "Invalid exempt query format: $ExemptQueryPath"
    }
    $exempt = To-SortedUniqueStrings -Values @($exemptDoc.results)
}

$exemptSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($id in $exempt) { $null = $exemptSet.Add($id) }

$assigned = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)

function Assign-Exclusive {
    param(
        [object[]]$Candidates,
        [System.Collections.Generic.HashSet[string]]$ExemptSet,
        [System.Collections.Generic.HashSet[string]]$Assigned
    )

    $out = New-Object System.Collections.Generic.List[string]
    foreach ($id in @($Candidates | Sort-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace([string]$id)) { continue }
        if ($ExemptSet.Contains([string]$id)) { continue }
        if ($Assigned.Contains([string]$id)) { continue }
        $null = $Assigned.Add([string]$id)
        $out.Add([string]$id)
    }
    return @($out.ToArray())
}

# EXEMPT has highest priority and removes IDs from all other classes.
$exemptFinal = @($exempt | Sort-Object -Unique)

# Exclusive ordering is deterministic and stable across runs.
$suspiciousFinal = Assign-Exclusive -Candidates $suspicious -ExemptSet $exemptSet -Assigned $assigned
$isolatedFinal = Assign-Exclusive -Candidates $isolated -ExemptSet $exemptSet -Assigned $assigned
$deadFinal = Assign-Exclusive -Candidates $dead -ExemptSet $exemptSet -Assigned $assigned
$reachableFinal = Assign-Exclusive -Candidates $reachable -ExemptSet $exemptSet -Assigned $assigned
$referencedFinal = Assign-Exclusive -Candidates $referencedRaw -ExemptSet $exemptSet -Assigned $assigned

$result = [ordered]@{
    classification = [ordered]@{
        REACHABLE = @($reachableFinal)
        REFERENCED = @($referencedFinal)
        ISOLATED = @($isolatedFinal)
        SUSPICIOUS = @($suspiciousFinal)
        DEAD = @($deadFinal)
        EXEMPT = @($exemptFinal)
    }
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 20 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
