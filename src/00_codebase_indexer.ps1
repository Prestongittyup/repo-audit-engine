[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$RunOutputDir,
    [string]$EngineStateDir,
    [string]$RunMetadataPath,
    [string]$IndexPath,
    [string]$ManifestPath,
    [string]$IgnorePatternsPath,
    [switch]$CI_MODE = $false
)

$ErrorActionPreference = "Stop"

$runtimeCommon = Join-Path $EngineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon)) {
    throw "Missing script: $runtimeCommon"
}
. $runtimeCommon
Set-AuditCiMode -Enabled ([bool]$CI_MODE)

if ([string]::IsNullOrWhiteSpace($EngineRoot)) {
    throw "EngineRoot parameter is required"
}
if ([string]::IsNullOrWhiteSpace($TargetRepoPath)) {
    throw "TargetRepoPath parameter is required"
}

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path

if ([string]::IsNullOrWhiteSpace($RunOutputDir)) {
    throw "RunOutputDir parameter is required"
}
if ([string]::IsNullOrWhiteSpace($EngineStateDir)) {
    $EngineStateDir = Join-Path $TargetRepoPath "engine_state"
}

$RUN_OUTPUT_DIR = [System.IO.Path]::GetFullPath($RunOutputDir)
$ENGINE_STATE_DIR = [System.IO.Path]::GetFullPath($EngineStateDir)

if ([string]::IsNullOrWhiteSpace($IndexPath)) {
    $IndexPath = Join-Path $RUN_OUTPUT_DIR "index.json"
}
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $RUN_OUTPUT_DIR "manifest.json"
}
if ([string]::IsNullOrWhiteSpace($IgnorePatternsPath)) {
    $IgnorePatternsPath = Join-Path $EngineRoot "config\ignore_patterns.txt"
}
if ([string]::IsNullOrWhiteSpace($RunMetadataPath)) {
    $RunMetadataPath = Join-Path $RUN_OUTPUT_DIR 'run_metadata.json'
}

New-Item -ItemType Directory -Path $RUN_OUTPUT_DIR -Force | Out-Null
New-Item -ItemType Directory -Path $ENGINE_STATE_DIR -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $IndexPath) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $ManifestPath) -Force | Out-Null

$runMetadata = Read-RunMetadata -Path $RunMetadataPath

function Get-ModuleType {
    param([string]$RelativePath, [string]$Extension)

    $lower = $RelativePath.ToLowerInvariant()
    if ($lower -match '(^|/)(test|tests|__tests__|spec)(/|$)' -or $lower -match '(\.test\.|\.spec\.)') { return 'test' }
    if ($lower -match '(^|/)(config|configs|settings|routes|routing)(/|$)' -or $Extension -in @('.json', '.yaml', '.yml', '.toml', '.ini', '.env', '.xml', '.properties')) { return 'config' }
    if ($lower -match '(^|/)(src/)?(main|app|core|engine|program|startup|bootstrap)(\.|/|$)' -or $lower -like '*/index.*') { return 'core' }
    if ($Extension -in @('.ps1', '.py', '.js', '.ts', '.tsx', '.jsx', '.cs', '.java', '.go', '.cpp', '.c', '.rb', '.php')) { return 'utility' }
    return 'unknown'
}

function Get-AnalysisKey {
    param([string]$RelativePath)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($RelativePath.ToLowerInvariant())
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $hashBytes = $sha1.ComputeHash($bytes)
    }
    finally {
        $sha1.Dispose()
    }
    return ([System.BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant()
}

$skipDirs = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
$skipExt = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
foreach ($dir in @('.git', 'node_modules', 'bin', 'obj', 'dist', 'build', 'out', 'target', '.cache', '.next', '.nuxt', '.venv', 'venv', '__pycache__', '.idea', '.vscode', 'repo_audit_output', 'engine_state')) {
    [void]$skipDirs.Add($dir)
}
foreach ($ext in @('.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.pdf', '.zip', '.tar', '.gz', '.7z', '.dll', '.exe', '.so', '.dylib', '.class', '.jar', '.pdb', '.mp3', '.mp4', '.mov', '.avi', '.pyc', '.pyd', '.obj')) {
    [void]$skipExt.Add($ext)
}

if (Test-Path -LiteralPath $IgnorePatternsPath) {
    foreach ($rawLine in [System.IO.File]::ReadLines($IgnorePatternsPath)) {
        $line = $rawLine.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        if ($line.StartsWith('.')) {
            [void]$skipExt.Add($line)
        }
        else {
            [void]$skipDirs.Add($line)
        }
    }
}

$currentIndexStatePath = Join-Path $ENGINE_STATE_DIR 'current_index.json'
$previousEntries = @{}
$hasPreviousIndex = $false
if (Test-Path -LiteralPath $currentIndexStatePath) {
    try {
        $previousIndex = Read-JsonArtifact -Path $currentIndexStatePath -Depth 12
        if ($previousIndex -and $previousIndex.files) {
            $hasPreviousIndex = $true
            foreach ($entry in $previousIndex.files) {
                $previousEntries[$entry.file] = $entry
            }
        }
    }
    catch {
        Write-AuditWarning 'Could not parse previous codebase index; rebuilding from scratch'
    }
}

$entries = New-Object System.Collections.Generic.List[object]
$changedFiles = New-Object System.Collections.Generic.List[string]
$currentFiles = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
$skippedFiles = New-Object System.Collections.Generic.List[string]

$opts = [System.IO.EnumerationOptions]::new()
$opts.RecurseSubdirectories = $true
$opts.IgnoreInaccessible = $true
$opts.ReturnSpecialDirectories = $false

foreach ($fullPath in [System.IO.Directory]::EnumerateFiles($TargetRepoPath, '*', $opts)) {
    $relativePath = [System.IO.Path]::GetRelativePath($TargetRepoPath, $fullPath).Replace('\', '/')
    $segments = $relativePath -split '/'

    $skip = $false
    foreach ($segment in $segments) {
        if ($skipDirs.Contains($segment)) {
            $skip = $true
            break
        }
    }
    if ($skip) { continue }

    $extension = [System.IO.Path]::GetExtension($fullPath).ToLowerInvariant()
    if ($skipExt.Contains($extension)) { continue }

    $fileInfo = [System.IO.FileInfo]::new($fullPath)
    if (($fileInfo.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
    $lastWriteUtc = $fileInfo.LastWriteTimeUtc.ToString('o')
    $hash = $null
    $isChanged = $true

    if ($previousEntries.ContainsKey($relativePath)) {
        $prev = $previousEntries[$relativePath]
        if (($prev.size -eq $fileInfo.Length) -and ($prev.last_write_utc -eq $lastWriteUtc)) {
            $hash = $prev.hash
            $isChanged = $false
        }
    }

    if (-not $hash) {
        try {
            $hash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        catch {
            $skippedFiles.Add($relativePath)
            Write-AuditWarning "Indexer skipped inaccessible file '$relativePath': $($_.Exception.Message)"
            continue
        }

        if ($previousEntries.ContainsKey($relativePath)) {
            $isChanged = ($previousEntries[$relativePath].hash -ne $hash)
        }
    }

    if ($isChanged) {
        $changedFiles.Add($relativePath)
    }

    $entry = [ordered]@{
        file = $relativePath
        size = [int64]$fileInfo.Length
        extension = $extension
        hash = $hash
        module_type = Get-ModuleType -RelativePath $relativePath -Extension $extension
        last_write_utc = $lastWriteUtc
        analysis_key = Get-AnalysisKey -RelativePath $relativePath
    }

    $entries.Add($entry)
    [void]$currentFiles.Add($relativePath)
}

$removedFiles = New-Object System.Collections.Generic.List[string]
foreach ($previousPath in $previousEntries.Keys) {
    if (-not $currentFiles.Contains($previousPath)) {
        $removedFiles.Add($previousPath)
    }
}

$sortedEntries = @($entries | Sort-Object file)
$generatedAt = (Get-Date).ToUniversalTime().ToString('o')
$scanMode = if ($hasPreviousIndex) { 'incremental' } else { 'full' }

$index = [ordered]@{
    generated_at = $generatedAt
    target_repo = $TargetRepoPath
    total_files = $sortedEntries.Count
    scan_mode = $scanMode
    changed_files = @($changedFiles | Sort-Object)
    removed_files = @($removedFiles | Sort-Object)
    skipped_files = @($skippedFiles | Sort-Object)
    files = $sortedEntries
}

Write-JsonArtifact -Path $IndexPath -RunMetadata $runMetadata -Data $index -ArtifactName 'index' -ExtraMetadata @{ file_count = $sortedEntries.Count; scan_mode = $scanMode } -Timestamp $generatedAt -Depth 10
Write-JsonArtifact -Path $currentIndexStatePath -RunMetadata $runMetadata -Data $index -ArtifactName 'current_index_state' -ExtraMetadata @{ file_count = $sortedEntries.Count; scan_mode = $scanMode } -Timestamp $generatedAt -Depth 10

$manifest = [ordered]@{
    generated_at = $generatedAt
    target_repo = $TargetRepoPath
    run_output_dir = $RUN_OUTPUT_DIR
    scan_mode = $scanMode
    total_files = $sortedEntries.Count
    files = @($sortedEntries | ForEach-Object { $_.file })
}

Write-JsonArtifact -Path $ManifestPath -RunMetadata $runMetadata -Data $manifest -ArtifactName 'manifest' -ExtraMetadata @{ file_count = $sortedEntries.Count; scan_mode = $scanMode } -Timestamp $generatedAt -Depth 8

Write-Status "Indexed $($sortedEntries.Count) files"
Write-Status "Changed files: $($changedFiles.Count)"
Write-Status "Removed files: $($removedFiles.Count)"
Write-Status "Codebase index: $IndexPath"
Write-Status "Manifest: $ManifestPath"
