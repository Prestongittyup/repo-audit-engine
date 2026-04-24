[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$ManifestPath
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($EngineRoot)) {
    throw "EngineRoot parameter is required"
}
if ([string]::IsNullOrWhiteSpace($TargetRepoPath)) {
    throw "TargetRepoPath parameter is required"
}

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path

if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $EngineRoot "state\manifest.txt"
}

New-Item -ItemType Directory -Path (Split-Path -Parent $ManifestPath) -Force | Out-Null

$skipDirs = @(
    "__pycache__",
    "node_modules",
    ".git",
    "dist",
    "build"
)

$skipExt = @(
    ".exe",
    ".pyc",
    ".pyd",
    ".obj"
)

$tempUnsorted = Join-Path ([System.IO.Path]::GetTempPath()) ("manifest_unsorted_{0}.txt" -f ([System.Guid]::NewGuid().ToString("N")))
$tempSorted = Join-Path ([System.IO.Path]::GetTempPath()) ("manifest_sorted_{0}.txt" -f ([System.Guid]::NewGuid().ToString("N")))

try {
    $writer = [System.IO.StreamWriter]::new($tempUnsorted, $false, [System.Text.UTF8Encoding]::new($false))
    try {
        $opts = [System.IO.EnumerationOptions]::new()
        $opts.RecurseSubdirectories = $true
        $opts.IgnoreInaccessible = $true
        $opts.ReturnSpecialDirectories = $false

        foreach ($fullPath in [System.IO.Directory]::EnumerateFiles($TargetRepoPath, "*", $opts)) {
            $relativePath = [System.IO.Path]::GetRelativePath($TargetRepoPath, $fullPath)
            $relativePath = $relativePath.Replace('\', '/')

            $segments = $relativePath -split '/'
            $skip = $false
            foreach ($segment in $segments) {
                if ($skipDirs -contains $segment) {
                    $skip = $true
                    break
                }
            }
            if ($skip) {
                continue
            }

            $ext = [System.IO.Path]::GetExtension($fullPath).ToLowerInvariant()
            if ($skipExt -contains $ext) {
                continue
            }

            $writer.WriteLine($relativePath)
        }
    }
    finally {
        $writer.Dispose()
    }

    & sort.exe $tempUnsorted /O $tempSorted
    Move-Item -LiteralPath $tempSorted -Destination $ManifestPath -Force
}
finally {
    if (Test-Path -LiteralPath $tempUnsorted) {
        Remove-Item -LiteralPath $tempUnsorted -Force
    }
    if (Test-Path -LiteralPath $tempSorted) {
        Remove-Item -LiteralPath $tempSorted -Force
    }
}
