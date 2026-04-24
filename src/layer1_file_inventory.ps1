[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoPath,
    [string]$OutputPath,
    [switch]$DebugMode
)

$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath($RepoPath)
if (-not (Test-Path -LiteralPath $repoRoot -PathType Container)) {
    throw "Repository path not found: $repoRoot"
}

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

$enumerationOptions = [System.IO.EnumerationOptions]::new()
$enumerationOptions.RecurseSubdirectories = $true
$enumerationOptions.IgnoreInaccessible = $false
$enumerationOptions.ReturnSpecialDirectories = $false
$enumerationOptions.AttributesToSkip = [System.IO.FileAttributes]0

$walkIterations = 0
$relativeFiles = New-Object System.Collections.Generic.List[string]
foreach ($absolute in [System.IO.Directory]::EnumerateFiles($repoRoot, '*', $enumerationOptions)) {
    $walkIterations++
    $relative = [System.IO.Path]::GetRelativePath($repoRoot, $absolute).Replace('\', '/')
    $relativeFiles.Add($relative)
}

$sortedRelativeFiles = @($relativeFiles | Sort-Object)

$items = New-Object System.Collections.Generic.List[object]
$totalLines = 0L

function Test-IsBinaryFile {
    param([string]$FilePath)

    $maxBytes = 8192
    $fs = [System.IO.File]::OpenRead($FilePath)
    try {
        $buffer = New-Object byte[] $maxBytes
        $read = $fs.Read($buffer, 0, $maxBytes)
    }
    finally {
        $fs.Dispose()
    }

    for ($i = 0; $i -lt $read; $i++) {
        if ($buffer[$i] -eq 0) {
            return $true
        }
    }

    return $false
}

function Get-LineCount {
    param([string]$FilePath)

    $count = 0L
    $reader = [System.IO.StreamReader]::new($FilePath)
    try {
        while ($null -ne $reader.ReadLine()) {
            $count++
        }
    }
    finally {
        $reader.Dispose()
    }

    return $count
}

foreach ($relativePath in $sortedRelativeFiles) {
    $absolutePath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $relativePath.Replace('/', '\')))
    $fileInfo = [System.IO.FileInfo]::new($absolutePath)

    $isBinary = Test-IsBinaryFile -FilePath $absolutePath
    $lineCount = if ($isBinary) { 0L } else { Get-LineCount -FilePath $absolutePath }

    $totalLines += $lineCount

    $items.Add([ordered]@{
        file_id = $relativePath
        absolute_path = $absolutePath
        extension = [System.IO.Path]::GetExtension($absolutePath).ToLowerInvariant()
        size_bytes = [int64]$fileInfo.Length
        line_count = [int64]$lineCount
        is_binary = [bool]$isBinary
    })
}

$stopwatch.Stop()

$result = [ordered]@{}
$result['files'] = @($items.ToArray())
$result['stats'] = [ordered]@{
    total_files = $items.Count
    total_lines = [int64]$totalLines
}

if ($DebugMode) {
    $result['debug'] = [ordered]@{
        scan_duration_ms = [int64]$stopwatch.ElapsedMilliseconds
        walk_iterations = [int64]$walkIterations
    }
}

if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $outFull = [System.IO.Path]::GetFullPath($OutputPath)
    $outDir = Split-Path -Parent $outFull
    if (-not [string]::IsNullOrWhiteSpace($outDir)) {
        New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    }

    $result | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $outFull -Encoding UTF8
}

return $result
