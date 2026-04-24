[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$AuditLogPath,
    [string]$DependencyMapPath
)

$ErrorActionPreference = "Stop"

function Resolve-DefaultPath {
    param(
        [string]$InputPath,
        [string]$FallbackPath
    )

    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        return $FallbackPath
    }

    return (Resolve-Path -LiteralPath $InputPath).Path
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$defaultRepoRoot = (Resolve-Path (Join-Path $scriptRoot "..")).Path
$RepoRoot = Resolve-DefaultPath -InputPath $RepoRoot -FallbackPath $defaultRepoRoot

if ([string]::IsNullOrWhiteSpace($AuditLogPath)) {
    $AuditLogPath = Join-Path $RepoRoot "state\audit_log.md"
}
if ([string]::IsNullOrWhiteSpace($DependencyMapPath)) {
    $DependencyMapPath = Join-Path $RepoRoot "state\dependency_map.json"
}

if (-not (Test-Path -LiteralPath $AuditLogPath)) {
    throw "Audit log not found at: $AuditLogPath"
}

New-Item -ItemType Directory -Path (Split-Path -Parent $DependencyMapPath) -Force | Out-Null

$map = [ordered]@{}

foreach ($line in [System.IO.File]::ReadLines($AuditLogPath)) {
    if (-not $line.StartsWith("AUDIT|")) {
        continue
    }

    $entry = ($line.Substring(6) | ConvertFrom-Json)
    $path = [string]$entry.path
    if ([string]::IsNullOrWhiteSpace($path)) {
        continue
    }

    $deps = @()
    if ($entry.dependencies) {
        $deps = @($entry.dependencies | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    }
    $map[$path] = $deps
}

$map | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $DependencyMapPath -Encoding UTF8
Write-Host ("Dependency map written: {0} ({1} files)" -f $DependencyMapPath, $map.Keys.Count)
