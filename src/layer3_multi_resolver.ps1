[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InventoryPath,
    [Parameter(Mandatory = $true)]
    [string]$CanonicalPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $InventoryPath -PathType Leaf)) {
    throw "Inventory file not found: $InventoryPath"
}
if (-not (Test-Path -LiteralPath $CanonicalPath -PathType Leaf)) {
    throw "Canonical nodes file not found: $CanonicalPath"
}

$inventory = Get-Content -LiteralPath $InventoryPath -Raw | ConvertFrom-Json -Depth 20
$canonical = Get-Content -LiteralPath $CanonicalPath -Raw | ConvertFrom-Json -Depth 20
if ($null -eq $inventory.files) {
    throw 'Invalid inventory format: missing files[]'
}
if ($null -eq $canonical.nodes) {
    throw 'Invalid canonical format: missing nodes[]'
}

function Normalize-PathValue {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) { return '' }
    $p = $PathValue.Replace('\', '/')
    while ($p.Contains('//')) { $p = $p.Replace('//', '/') }
    return $p.Trim().Trim('/')
}

function Add-UniqueRef {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) { return }
    if (-not $List.Contains($Value)) { $List.Add($Value) }
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

$invByRel = @{}
foreach ($f in @($inventory.files)) {
    $rel = Normalize-PathValue -PathValue ([string]$f.file_id)
    if ($rel.Length -gt 0) {
        $invByRel[$rel] = $f
    }
}

$nodes = @($canonical.nodes | Sort-Object file_path)
$nodeByFilePath = @{}
$nodeByModulePath = @{}
$nodeByBaseName = @{}
foreach ($n in $nodes) {
    $filePath = Normalize-PathValue -PathValue ([string]$n.file_path)
    $modulePath = [string]$n.module_path
    $nodeByFilePath[$filePath] = $n

    if (-not $nodeByModulePath.ContainsKey($modulePath)) {
        $nodeByModulePath[$modulePath] = New-Object System.Collections.Generic.List[object]
    }
    $nodeByModulePath[$modulePath].Add($n)

    $base = [System.IO.Path]::GetFileNameWithoutExtension($filePath).ToLowerInvariant()
    if (-not $nodeByBaseName.ContainsKey($base)) {
        $nodeByBaseName[$base] = New-Object System.Collections.Generic.List[object]
    }
    $nodeByBaseName[$base].Add($n)
}

foreach ($k in @($nodeByModulePath.Keys)) {
    $nodeByModulePath[$k] = @($nodeByModulePath[$k] | Sort-Object id)
}
foreach ($k in @($nodeByBaseName.Keys)) {
    $nodeByBaseName[$k] = @($nodeByBaseName[$k] | Sort-Object id)
}

$pythonExe = $null
foreach ($candidate in @('python', 'py', 'python3')) {
    try {
        $v = & $candidate --version 2>&1
        if (("$v") -match 'Python 3') {
            $pythonExe = $candidate
            break
        }
    }
    catch { }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$astHelper = Join-Path $scriptDir 'layer3_py_ast_resolver.py'

function Resolve-ReferenceToNode {
    param(
        [string]$FromFilePath,
        [string]$FromNodeId,
        [string]$Reference
    )

    if ([string]::IsNullOrWhiteSpace($Reference)) { return $null }
    $r = Normalize-PathValue -PathValue $Reference
    if ($r.Length -eq 0) { return $null }

    $sourceNamespace = Get-NodeNamespace -CanonicalId $FromNodeId

    if ($nodeByModulePath.ContainsKey($r)) {
        $candidate = $nodeByModulePath[$r][0]
        if ((Get-NodeNamespace -CanonicalId ([string]$candidate.id)) -eq $sourceNamespace) {
            return $candidate
        }
    }

    if ($nodeByFilePath.ContainsKey($r)) {
        $candidate = $nodeByFilePath[$r]
        if ((Get-NodeNamespace -CanonicalId ([string]$candidate.id)) -eq $sourceNamespace) {
            return $candidate
        }
    }

    $fromDir = Normalize-PathValue -PathValue ([System.IO.Path]::GetDirectoryName($FromFilePath.Replace('/', '\')))
    if ($r.StartsWith('.')) {
        $combined = [System.IO.Path]::GetFullPath((Join-Path ($fromDir.Replace('/', '\')) ($r.Replace('/', '\'))))
        $combinedNorm = Normalize-PathValue -PathValue $combined
        if ($nodeByFilePath.ContainsKey($combinedNorm)) {
            $candidate = $nodeByFilePath[$combinedNorm]
            if ((Get-NodeNamespace -CanonicalId ([string]$candidate.id)) -eq $sourceNamespace) { return $candidate }
        }
        if ($nodeByFilePath.ContainsKey($combinedNorm + '.py')) {
            $candidate = $nodeByFilePath[$combinedNorm + '.py']
            if ((Get-NodeNamespace -CanonicalId ([string]$candidate.id)) -eq $sourceNamespace) { return $candidate }
        }
    }

    $pathFromDot = $r.Replace('.', '/')
    foreach ($ext in @('.py', '.ps1', '.js', '.ts', '.tsx', '.jsx', '.json', '.yml', '.yaml')) {
        $candidate = $pathFromDot + $ext
        if ($nodeByFilePath.ContainsKey($candidate)) {
            $resolved = $nodeByFilePath[$candidate]
            if ((Get-NodeNamespace -CanonicalId ([string]$resolved.id)) -eq $sourceNamespace) {
                return $resolved
            }
        }
    }

    if ($nodeByFilePath.ContainsKey($pathFromDot)) {
        $candidate = $nodeByFilePath[$pathFromDot]
        if ((Get-NodeNamespace -CanonicalId ([string]$candidate.id)) -eq $sourceNamespace) {
            return $candidate
        }
    }

    $leaf = [System.IO.Path]::GetFileNameWithoutExtension($r).ToLowerInvariant()
    if ($nodeByBaseName.ContainsKey($leaf)) {
        foreach ($candidate in @($nodeByBaseName[$leaf])) {
            if ((Get-NodeNamespace -CanonicalId ([string]$candidate.id)) -eq $sourceNamespace) {
                return $candidate
            }
        }
    }

    return $null
}

$edgeMap = @{}

function Add-Edge {
    param(
        [string]$FromId,
        [string]$ToId,
        [string]$Type,
        [double]$Confidence,
        [string]$Source
    )

    if ([string]::IsNullOrWhiteSpace($FromId) -or [string]::IsNullOrWhiteSpace($ToId)) { return }
    if ($FromId -eq $ToId) { return }

    $key = "$FromId|$ToId|$Type|$Source"
    if (-not $edgeMap.ContainsKey($key)) {
        $edgeMap[$key] = [ordered]@{
            from = $FromId
            to = $ToId
            type = $Type
            confidence = [Math]::Round($Confidence, 3)
            source = $Source
        }
    }
    elseif ($Confidence -gt [double]$edgeMap[$key].confidence) {
        $edgeMap[$key].confidence = [Math]::Round($Confidence, 3)
    }
}

foreach ($node in $nodes) {
    $fromId = [string]$node.id
    $filePath = Normalize-PathValue -PathValue ([string]$node.file_path)
    if (-not $invByRel.ContainsKey($filePath)) {
        continue
    }

    $fileRecord = $invByRel[$filePath]
    $absolutePath = [System.IO.Path]::GetFullPath([string]$fileRecord.absolute_path)
    $ext = [System.IO.Path]::GetExtension($absolutePath).ToLowerInvariant()

    $text = ''
    try {
        $text = [System.IO.File]::ReadAllText($absolutePath)
    }
    catch {
        $text = ''
    }

    $fileHasPrimaryEdges = $false

    # 1) AST resolver (Python primary)
    if ($ext -eq '.py' -and $pythonExe -and (Test-Path -LiteralPath $astHelper)) {
        try {
            $astOut = & $pythonExe $astHelper $absolutePath 2>$null
            if ($LASTEXITCODE -eq 0 -and $astOut) {
                $astParsed = $astOut | ConvertFrom-Json
                foreach ($r in @($astParsed.imports)) {
                    $target = Resolve-ReferenceToNode -FromFilePath $filePath -FromNodeId $fromId -Reference ([string]$r)
                    if ($target) {
                        Add-Edge -FromId $fromId -ToId ([string]$target.id) -Type 'IMPORT' -Confidence 0.95 -Source 'AST'
                        $fileHasPrimaryEdges = $true
                    }
                }
                foreach ($r in @($astParsed.from_imports)) {
                    $target = Resolve-ReferenceToNode -FromFilePath $filePath -FromNodeId $fromId -Reference ([string]$r)
                    if ($target) {
                        Add-Edge -FromId $fromId -ToId ([string]$target.id) -Type 'IMPORT' -Confidence 0.9 -Source 'AST'
                        $fileHasPrimaryEdges = $true
                    }
                }
            }
        }
        catch { }
    }

    # 2) DI resolver
    $diRefs = New-Object System.Collections.Generic.List[string]
    foreach ($m in [regex]::Matches($text, 'importlib\.import_module\(\s*["\x27]([^"\x27]+)["\x27]\s*\)')) {
        Add-UniqueRef -List $diRefs -Value $m.Groups[1].Value
    }
    foreach ($m in [regex]::Matches($text, '__import__\(\s*["\x27]([^"\x27]+)["\x27]\s*\)')) {
        Add-UniqueRef -List $diRefs -Value $m.Groups[1].Value
    }
    foreach ($m in [regex]::Matches($text, 'include_router\(\s*([A-Za-z_][A-Za-z0-9_\.]*)')) {
        Add-UniqueRef -List $diRefs -Value ($m.Groups[1].Value.Split('.')[0])
    }
    foreach ($m in [regex]::Matches($text, 'Depends\(\s*([A-Za-z_][A-Za-z0-9_\.]*)')) {
        Add-UniqueRef -List $diRefs -Value ($m.Groups[1].Value.Split('.')[0])
    }
    foreach ($m in [regex]::Matches($text, 'Session\s*=\s*Depends\(\s*([A-Za-z_][A-Za-z0-9_\.]*)')) {
        Add-UniqueRef -List $diRefs -Value ($m.Groups[1].Value.Split('.')[0])
    }

    foreach ($r in @($diRefs | Sort-Object -Unique)) {
        $target = Resolve-ReferenceToNode -FromFilePath $filePath -FromNodeId $fromId -Reference ([string]$r)
        if ($target) {
            $type = if ($r -match '\.|/|import_module|__import__') { 'DYNAMIC' } else { 'DI' }
            $conf = if ($type -eq 'DYNAMIC') { 0.75 } else { 0.6 }
            Add-Edge -FromId $fromId -ToId ([string]$target.id) -Type $type -Confidence $conf -Source 'DI'
            $fileHasPrimaryEdges = $true
        }
    }

    # 3) CONFIG resolver
    $configRefs = New-Object System.Collections.Generic.List[string]
    foreach ($m in [regex]::Matches($text, '(?im)(plugin|module|handler|provider|service|registry|class_path)\s*[:=]\s*["\x27]([A-Za-z0-9_\./-]+)["\x27]')) {
        Add-UniqueRef -List $configRefs -Value $m.Groups[2].Value
    }
    foreach ($m in [regex]::Matches($text, '(?im)(?:os\.getenv|os\.environ\.get|process\.env|\$env:)(?:\(|\[)?["\x27]?[A-Za-z0-9_]+["\x27]?(?:\)|\])?\s*[,)]\s*["\x27]([A-Za-z0-9_\./-]+)["\x27]')) {
        Add-UniqueRef -List $configRefs -Value $m.Groups[1].Value
    }
    foreach ($m in [regex]::Matches($text, '(?im)["\x27]([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)["\x27]')) {
        if ($m.Groups[1].Value -notmatch '^https?\.') {
            Add-UniqueRef -List $configRefs -Value $m.Groups[1].Value
        }
    }

    foreach ($r in @($configRefs | Sort-Object -Unique)) {
        $target = Resolve-ReferenceToNode -FromFilePath $filePath -FromNodeId $fromId -Reference ([string]$r)
        if ($target) {
            Add-Edge -FromId $fromId -ToId ([string]$target.id) -Type 'CONFIG' -Confidence 0.45 -Source 'CONFIG'
            $fileHasPrimaryEdges = $true
        }
    }

    # 4) HEURISTIC resolver (fallback only)
    if (-not $fileHasPrimaryEdges) {
        $heuristicRefs = New-Object System.Collections.Generic.List[string]
        foreach ($m in [regex]::Matches($text, '["\x27]([A-Za-z0-9_\-/]+\.(?:py|ps1|js|ts|tsx|jsx|json|yml|yaml))["\x27]')) {
            Add-UniqueRef -List $heuristicRefs -Value $m.Groups[1].Value
        }
        foreach ($m in [regex]::Matches($text, '["\x27]([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)["\x27]')) {
            Add-UniqueRef -List $heuristicRefs -Value $m.Groups[1].Value
        }

        foreach ($r in @($heuristicRefs | Sort-Object -Unique)) {
            $target = Resolve-ReferenceToNode -FromFilePath $filePath -FromNodeId $fromId -Reference ([string]$r)
            if ($target) {
                Add-Edge -FromId $fromId -ToId ([string]$target.id) -Type 'DYNAMIC' -Confidence 0.2 -Source 'HEURISTIC'
            }
        }
    }
}

$allEdges = @($edgeMap.Values)
$astPairKeys = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
foreach ($edge in $allEdges) {
    if ([string]$edge.source -eq 'AST') {
        $null = $astPairKeys.Add("$([string]$edge.from)|$([string]$edge.to)")
    }
}

$removedDiEdges = @()
$strictEdges = @()
foreach ($edge in $allEdges) {
    if ([string]$edge.source -ne 'DI') {
        $strictEdges += $edge
        continue
    }

    $pairKey = "$([string]$edge.from)|$([string]$edge.to)"
    if ($astPairKeys.Contains($pairKey)) {
        $strictEdges += $edge
        continue
    }

    $removedDiEdges += [ordered]@{
        from = [string]$edge.from
        to = [string]$edge.to
        type = [string]$edge.type
        source = 'DI'
        reason = 'DI_NOT_DERIVED_FROM_AST'
    }
}

$edges = @($strictEdges | Sort-Object from, to, type, source)
$result = [ordered]@{
    edges = $edges
    stats = [ordered]@{
        total_edges_before_strict_di = @($allEdges).Count
        total_edges_after_strict_di = @($edges).Count
        removed_di_edges = $removedDiEdges
    }
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 10 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
