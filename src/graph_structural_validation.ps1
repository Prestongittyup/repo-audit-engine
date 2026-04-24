[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$GraphPath,
    [Parameter(Mandatory = $true)]
    [string]$InventoryPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [bool]$FailOnInvalid = $false
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $GraphPath -PathType Leaf)) {
    throw "Graph file not found: $GraphPath"
}
if (-not (Test-Path -LiteralPath $InventoryPath -PathType Leaf)) {
    throw "Inventory file not found: $InventoryPath"
}

$graphDoc = Get-Content -LiteralPath $GraphPath -Raw | ConvertFrom-Json -Depth 40
$inventoryDoc = Get-Content -LiteralPath $InventoryPath -Raw | ConvertFrom-Json -Depth 20

if ($null -eq $graphDoc.graph -or $null -eq $graphDoc.graph.nodes -or $null -eq $graphDoc.graph.edges) {
    throw 'Invalid graph format. Expected graph.nodes and graph.edges.'
}
if ($null -eq $inventoryDoc.files) {
    throw 'Invalid ingestion format. Expected files array in inventory.'
}

function Add-ValidationError {
    param(
        [System.Collections.Generic.List[object]]$ErrorList,
        [string]$Type,
        [string]$Node,
        [string]$Edge,
        [string]$Reason
    )

    $ErrorList.Add([ordered]@{
        type = $Type
        node = $Node
        edge = $Edge
        reason = $Reason
    })
}

function Get-IdValue {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ''
    }
    return $Value.Trim()
}

$errors = New-Object System.Collections.Generic.List[object]
$invalidEdgeCount = 0

$nodes = @($graphDoc.graph.nodes)
$edges = @($graphDoc.graph.edges)

$nodeById = @{}
foreach ($n in $nodes) {
    $id = Get-IdValue -Value ([string]$n.id)

    if ([string]::IsNullOrWhiteSpace($id)) {
        Add-ValidationError -ErrorList $errors -Type 'NULL_NODE_ID' -Node '' -Edge '' -Reason 'Node has null/empty id.'
        continue
    }

    if ($nodeById.ContainsKey($id)) {
        Add-ValidationError -ErrorList $errors -Type 'DUPLICATE_NODE_ID' -Node $id -Edge '' -Reason 'Duplicate canonical node id detected.'
        continue
    }

    $nodeById[$id] = $n
}

$nodeCount = $nodes.Count
$ingestionCount = @($inventoryDoc.files).Count
if ($nodeCount -ne $ingestionCount) {
    Add-ValidationError -ErrorList $errors -Type 'NODE_COUNT_MISMATCH' -Node '' -Edge '' -Reason "Graph node count ($nodeCount) does not equal ingestion file count ($ingestionCount)."
}

$edgeSignatures = New-Object System.Collections.Generic.List[string]
foreach ($e in $edges) {
    $from = Get-IdValue -Value ([string]$e.from)
    $to = Get-IdValue -Value ([string]$e.to)
    $type = [string]$e.type

    $edgeToken = "$from|$to|$type"

    if ([string]::IsNullOrWhiteSpace($from) -or [string]::IsNullOrWhiteSpace($to) -or [string]::IsNullOrWhiteSpace($type)) {
        $invalidEdgeCount += 1
        Add-ValidationError -ErrorList $errors -Type 'NULL_OR_UNRESOLVED_EDGE_REFERENCE' -Node '' -Edge $edgeToken -Reason 'Edge has null/empty from, to, or type.'
        continue
    }

    if (-not $nodeById.ContainsKey($from)) {
        $invalidEdgeCount += 1
        Add-ValidationError -ErrorList $errors -Type 'UNRESOLVED_FROM_NODE' -Node $from -Edge $edgeToken -Reason 'Edge from node does not exist in canonical nodes.'
        continue
    }

    if (-not $nodeById.ContainsKey($to)) {
        $invalidEdgeCount += 1
        Add-ValidationError -ErrorList $errors -Type 'UNRESOLVED_TO_NODE' -Node $to -Edge $edgeToken -Reason 'Edge to node does not exist in canonical nodes.'
        continue
    }

    $normalizedSignature = "$from|$to|$type"
    $edgeSignatures.Add($normalizedSignature)
}

$sortedSignatures = @($edgeSignatures | Sort-Object)
for ($i = 0; $i -lt $edgeSignatures.Count; $i++) {
    if ($edgeSignatures[$i] -ne $sortedSignatures[$i]) {
        Add-ValidationError -ErrorList $errors -Type 'NON_DETERMINISTIC_EDGE_ORDER' -Node '' -Edge $edgeSignatures[$i] -Reason 'Edge list ordering is not stable sorted order.'
        break
    }
}

$result = [ordered]@{
    valid = ($errors.Count -eq 0)
    status = if ($errors.Count -eq 0) { 'ANALYZED' } else { 'DEGRADED' }
    errors = @($errors.ToArray())
    stats = [ordered]@{
        nodes = $nodeCount
        edges = $edges.Count
        invalid_edges = $invalidEdgeCount
    }
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 20 | Out-File -LiteralPath $outFull -Encoding UTF8

if (-not $result.valid -and $FailOnInvalid) {
    throw "Graph structural validation failed. See report: $outFull"
}

return $result
