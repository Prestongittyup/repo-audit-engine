[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StructuralValidationPath,
    [Parameter(Mandatory = $true)]
    [string]$ReachabilityValidationPath,
    [Parameter(Mandatory = $true)]
    [string]$ResolverConsistencyPath,
    [Parameter(Mandatory = $true)]
    [string]$SemanticValidationPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

foreach ($p in @($StructuralValidationPath, $ReachabilityValidationPath, $ResolverConsistencyPath, $SemanticValidationPath)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
        throw "Required validation input not found: $p"
    }
}

$structural = Get-Content -LiteralPath $StructuralValidationPath -Raw | ConvertFrom-Json -Depth 20
$reachability = Get-Content -LiteralPath $ReachabilityValidationPath -Raw | ConvertFrom-Json -Depth 20
$resolver = Get-Content -LiteralPath $ResolverConsistencyPath -Raw | ConvertFrom-Json -Depth 20
$semantic = Get-Content -LiteralPath $SemanticValidationPath -Raw | ConvertFrom-Json -Depth 20

$layers = @(
    [ordered]@{
        name = 'structural_validation'
        pass = ($structural.PSObject.Properties.Name -contains 'valid' -and [bool]$structural.valid)
    },
    [ordered]@{
        name = 'reachability_validation'
        pass = (
            ($reachability.PSObject.Properties.Name -contains 'false_dead_nodes') -and
            ($reachability.PSObject.Properties.Name -contains 'false_reachable_nodes') -and
            (@($reachability.false_dead_nodes).Count -eq 0) -and
            (@($reachability.false_reachable_nodes).Count -eq 0)
        )
    },
    [ordered]@{
        name = 'resolver_consistency'
        pass = (
            ($resolver.PSObject.Properties.Name -contains 'disagreements') -and
            (@($resolver.disagreements | Where-Object { [string]$_.severity -eq 'HIGH' }).Count -eq 0)
        )
    },
    [ordered]@{
        name = 'semantic_validation'
        pass = ($semantic.PSObject.Properties.Name -contains 'semantic_valid' -and [bool]$semantic.semantic_valid)
    }
)

$failed = @($layers | Where-Object { -not [bool]$_.pass } | ForEach-Object { [string]$_.name } | Sort-Object -Unique)
$passedCount = @($layers | Where-Object { [bool]$_.pass }).Count
$total = $layers.Count
$score = if ($failed.Count -gt 0) { 0.0 } elseif ($total -eq 0) { 0.0 } else { [Math]::Round(($passedCount / $total), 3) }

$result = [ordered]@{
    system_valid = ($failed.Count -eq 0)
    failure_domains = $failed
    trust_score = [double]$score
}

$outFull = [System.IO.Path]::GetFullPath($OutputPath)
$outDir = Split-Path -Parent $outFull
if (-not [string]::IsNullOrWhiteSpace($outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$result | ConvertTo-Json -Depth 10 | Out-File -LiteralPath $outFull -Encoding UTF8

return $result
