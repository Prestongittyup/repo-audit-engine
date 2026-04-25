param(
    # Use -Apply to execute deletions; otherwise the script runs as dry-run.
    [switch]$Apply,
    # By default, tracked paths are skipped. Use -IncludeTracked to allow deleting them.
    [switch]$IncludeTracked,
    # Restores tracked harness outputs back to HEAD when requested.
    [switch]$RestoreTrackedHarness,
    # Resets the tracked snapshot gitlink path back to HEAD when requested.
    [switch]$ResetSnapshotGitlink
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$repoRootResolved = (Resolve-Path -LiteralPath $repoRoot).Path

$literalTargets = @(
    "output/checklist_validation_run",
    "output/family_orchestration_bot_contract.json",
    "output/family_orchestration_bot_contract_artifacts",
    "output/repo_audit_runs",
    "output/runs",
    ".tmp/authority_pass_repo",
    ".tmp/clean_repo",
    ".tmp/family_orchestration_bot_snapshot_clean",
    ".tmp/semantic_debug"
)

$wildcardTargets = @(
    "output/family_orchestration_bot_fixcheck*",
    "output/*_artifacts",
    "output/tmp_*_events.jsonl",
    "output/tmp_*_result.json"
)

function Get-TransientTargets {
    $targets = New-Object System.Collections.Generic.List[string]

    foreach ($relativePath in $literalTargets) {
        $fullPath = Join-Path $repoRoot $relativePath
        if (Test-Path -LiteralPath $fullPath) {
            $targets.Add($fullPath)
        }
    }

    foreach ($pattern in $wildcardTargets) {
        $fullPattern = Join-Path $repoRoot $pattern
        $matches = Get-ChildItem -Path $fullPattern -Force -ErrorAction SilentlyContinue
        foreach ($match in $matches) {
            $targets.Add($match.FullName)
        }
    }

    return $targets | Sort-Object -Unique
}

function Remove-Target {
    param([string]$Path)

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    if ($resolvedPath.StartsWith($repoRootResolved, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relativePath = $resolvedPath.Substring($repoRootResolved.Length).TrimStart([char[]]@('\', '/'))
    }
    else {
        $relativePath = $Path
    }

    $relativePath = $relativePath.Replace("\\", "/")
    & git -C $repoRoot ls-files --error-unmatch -- $relativePath *> $null
    $isTracked = $LASTEXITCODE -eq 0

    if ($isTracked -and -not $IncludeTracked) {
        Write-Host "[skip-tracked] $Path"
        return
    }

    if ($Apply) {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[removed] $Path"
    }
    else {
        Write-Host "[dry-run] remove $Path"
    }
}

Write-Host "Repo root: $repoRoot"
$targets = Get-TransientTargets

if (-not $targets -or $targets.Count -eq 0) {
    Write-Host "No transient artifacts matched cleanup patterns."
}
else {
    Write-Host "Matched $($targets.Count) transient artifact path(s)."
    foreach ($target in $targets) {
        Remove-Target -Path $target
    }
}

if ($RestoreTrackedHarness) {
    if ($Apply) {
        & git -C $repoRoot restore --worktree -- "output/test_harness"
        Write-Host "[restored] output/test_harness"
    }
    else {
        Write-Host "[dry-run] git -C $repoRoot restore --worktree -- output/test_harness"
    }
}

if ($ResetSnapshotGitlink) {
    if ($Apply) {
        & git -C $repoRoot restore --worktree -- ".tmp/family_orchestration_bot_snapshot"
        Write-Host "[reset] .tmp/family_orchestration_bot_snapshot gitlink"
    }
    else {
        Write-Host "[dry-run] git -C $repoRoot restore --worktree -- .tmp/family_orchestration_bot_snapshot"
    }
}

Write-Host "Current git status:"
& git -C $repoRoot status --short
