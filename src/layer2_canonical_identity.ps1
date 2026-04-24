[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InventoryPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $InventoryPath -PathType Leaf)) {
    throw "Inventory file not found: $InventoryPath"
}

$inventory = Get-Content -LiteralPath $InventoryPath -Raw | ConvertFrom-Json -Depth 10
if ($null -eq $inventory -or $null -eq $inventory.files) {
    throw 'Invalid Layer 1 inventory format. Expected root.files array.'
}

$files = @($inventory.files)

if ($files.Count -eq 0) {
    $emptyResult = [ordered]@{ nodes = @() }
    $outDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutputPath))
    if (-not [string]::IsNullOrWhiteSpace($outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }
    $emptyResult | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $OutputPath -Encoding UTF8
    return $emptyResult
}

function Normalize-RelPath {
    param([string]$PathValue)

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        return ''
    }

    $normalized = $PathValue.Replace('\\', '/')
    while ($normalized.Contains('//')) {
        $normalized = $normalized.Replace('//', '/')
    }
    return $normalized.Trim('/').Trim()
}

function Get-RepoRootFromFile {
    param([string]$AbsolutePath, [string]$RelativePath)

    $root = [System.IO.Path]::GetFullPath($AbsolutePath)
    $segments = @((Normalize-RelPath -PathValue $RelativePath).Split('/', [System.StringSplitOptions]::RemoveEmptyEntries))
    foreach ($segment in $segments) {
        $root = Split-Path -Parent $root
    }
    return [System.IO.Path]::GetFullPath($root)
}

# Infer a single repository root from all inventory entries.
$repoRootCandidates = New-Object System.Collections.Generic.List[string]
foreach ($f in $files) {
    $abs = [string]$f.absolute_path
    $rel = [string]$f.file_id
    $repoRootCandidates.Add((Get-RepoRootFromFile -AbsolutePath $abs -RelativePath $rel))
}

$distinctRoots = @($repoRootCandidates | Sort-Object -Unique)
if ($distinctRoots.Count -ne 1) {
    throw "Unable to infer single repo root. Found: $($distinctRoots -join ', ')"
}
$repoRoot = $distinctRoots[0]

$repoNameRaw = Split-Path -Leaf $repoRoot
$repoNameCanonical = ([regex]::Replace($repoNameRaw.ToLowerInvariant(), '[^a-z0-9._-]+', '-')).Trim('-')
if ([string]::IsNullOrWhiteSpace($repoNameCanonical)) {
    $repoNameCanonical = 'repo'
}

# Strip known redundant prefixes only when all files share that prefix.
$prefixCandidates = @('src', 'app')
$stripPrefix = $null
$normalizedRelPaths = @($files | ForEach-Object { Normalize-RelPath -PathValue ([string]$_.file_id) } | Sort-Object)
foreach ($prefix in $prefixCandidates) {
    $allMatch = $true
    foreach ($p in $normalizedRelPaths) {
        if ($p -notmatch "^$prefix/") {
            $allMatch = $false
            break
        }
    }
    if ($allMatch) {
        $stripPrefix = $prefix
        break
    }
}

$sortedFiles = @($files | Sort-Object { Normalize-RelPath -PathValue ([string]$_.file_id) })
$seenIds = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
$nodes = New-Object System.Collections.Generic.List[object]

foreach ($file in $sortedFiles) {
    $relativePath = Normalize-RelPath -PathValue ([string]$file.file_id)
    if ([string]::IsNullOrWhiteSpace($relativePath)) {
        continue
    }

    $canonicalRelPath = $relativePath
    if ($stripPrefix -and $canonicalRelPath -match "^$stripPrefix/(.+)$") {
        $canonicalRelPath = $Matches[1]
    }

    $absolutePath = [System.IO.Path]::GetFullPath([string]$file.absolute_path)

    $fileName = [System.IO.Path]::GetFileName($canonicalRelPath)
    $dirPath = [System.IO.Path]::GetDirectoryName($canonicalRelPath.Replace('/', '\'))
    $dirPath = if ([string]::IsNullOrWhiteSpace($dirPath)) { '_root' } else { $dirPath.Replace('\', '/') }

    $modulePath = [System.IO.Path]::GetFileNameWithoutExtension($canonicalRelPath)
    $modulePath = $modulePath.Replace('\', '/').Replace('/', '.')

    $id = "canonical://$repoNameCanonical/${dirPath}:$fileName"

    if (-not $seenIds.Add($id)) {
        throw "Duplicate canonical ID detected: $id"
    }

    $nodes.Add([ordered]@{
        id = $id
        file_path = $canonicalRelPath
        module_path = $modulePath
        type = 'FILE'
    })
}

$result = [ordered]@{
    nodes = @($nodes.ToArray())
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
