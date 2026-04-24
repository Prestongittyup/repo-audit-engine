[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$RunOutputDir,
    [string]$EngineStateDir,
    [string]$RunMetadataPath,
    [string]$IndexPath,
    [string]$AuditLogPath,
    [string]$AnalysisCacheDir,
    [ValidateRange(10, 5000)][int]$BatchSize = 200,
    [switch]$CI_MODE = $false
)

$ErrorActionPreference = "Stop"

$runtimeCommon = Join-Path $EngineRoot 'src\runtime_common.ps1'
if (-not (Test-Path -LiteralPath $runtimeCommon)) {
    throw "Missing script: $runtimeCommon"
}
. $runtimeCommon
Set-AuditCiMode -Enabled ([bool]$CI_MODE)

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
if ([string]::IsNullOrWhiteSpace($AuditLogPath)) {
    $AuditLogPath = Join-Path $RUN_OUTPUT_DIR "audit_log.jsonl"
}
if ([string]::IsNullOrWhiteSpace($AnalysisCacheDir)) {
    $AnalysisCacheDir = Join-Path $ENGINE_STATE_DIR "file_analysis"
}
if ([string]::IsNullOrWhiteSpace($RunMetadataPath)) {
    $RunMetadataPath = Join-Path $RUN_OUTPUT_DIR 'run_metadata.json'
}

if (-not (Test-Path -LiteralPath $IndexPath)) {
    throw "Codebase index missing: $IndexPath"
}

New-Item -ItemType Directory -Path $RUN_OUTPUT_DIR -Force | Out-Null
New-Item -ItemType Directory -Path $AnalysisCacheDir -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $AuditLogPath) -Force | Out-Null

$null = Read-RunMetadata -Path $RunMetadataPath
$index = Read-JsonArtifact -Path $IndexPath -Depth 12
$changedSet = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
foreach ($changedFile in @($index.changed_files)) {
    [void]$changedSet.Add($changedFile)
}

function Get-AnalysisCachePath {
    param([string]$AnalysisKey)
    return (Join-Path $AnalysisCacheDir ($AnalysisKey + '.json'))
}

function Get-StaticDependencies {
    param([string]$Text, [string]$FilePath)

    $deps = New-Object System.Collections.Generic.List[object]
    $patterns = @(
        @{ pattern = '(?m)^\s*import\s+([A-Za-z0-9_\.\-/]+)'; confidence = 100; evidence = 'python import' }
        @{ pattern = '(?m)^\s*from\s+([A-Za-z0-9_\.\-/]+)\s+import\s+'; confidence = 100; evidence = 'python from import' }
        @{ pattern = 'require\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'; confidence = 98; evidence = 'require' }
        @{ pattern = 'import\s+.*from\s+["\x27]([^"\x27]+)["\x27]'; confidence = 98; evidence = 'es import' }
        @{ pattern = '(?m)^\s*#include\s+[<"]([^>"]+)[>"]'; confidence = 95; evidence = 'include' }
        @{ pattern = '(?m)^\s*using\s+([A-Za-z0-9_\.]+);'; confidence = 95; evidence = 'using' }
        @{ pattern = '(?m)Import-Module\s+["\x27]?([A-Za-z0-9_\.\-/\\]+)'; confidence = 92; evidence = 'Import-Module' }
        @{ pattern = '(?m)^\s*\.\s+["\x27]([^"\x27]+)["\x27]'; confidence = 92; evidence = 'dot sourcing' }
        @{ pattern = '<\s*script\s+src=["\x27]([^"\x27]+)["\x27]'; confidence = 90; evidence = 'template script include' }
        @{ pattern = '<\s*link\s+rel=["\x27]stylesheet["\x27][^>]*href=["\x27]([^"\x27]+)["\x27]'; confidence = 90; evidence = 'template style include' }
    )

    foreach ($entry in $patterns) {
        foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Text, $entry.pattern)) {
            if ($match.Groups.Count -le 1) { continue }
            $value = $match.Groups[1].Value.Trim()
            if ($value.Length -eq 0) { continue }
            $deps.Add([pscustomobject]@{
                reference = $value
                confidence = $entry.confidence
                evidence = $entry.evidence
            })
        }
    }

    return [object[]]$deps.ToArray()
}

function Get-DynamicDependencies {
    param([string]$Text)

    $deps = New-Object System.Collections.Generic.List[object]
    $patterns = @(
        @{ pattern = '__import__\(\s*["\x27]([^"\x27]+)["\x27]'; confidence = 70; evidence = 'python __import__' }
        @{ pattern = 'importlib\.import_module\(\s*["\x27]([^"\x27]+)["\x27]'; confidence = 68; evidence = 'importlib.import_module' }
        @{ pattern = 'import\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'; confidence = 65; evidence = 'dynamic import()' }
        @{ pattern = 'require\.resolve\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'; confidence = 62; evidence = 'require.resolve' }
        @{ pattern = 'getattr\([^,]+,\s*["\x27]([^"\x27]+)["\x27]'; confidence = 50; evidence = 'getattr reflection' }
        @{ pattern = 'GetType\(\s*["\x27]([^"\x27]+)["\x27]'; confidence = 45; evidence = 'GetType reflection' }
        @{ pattern = 'Assembly\.Load\(\s*["\x27]([^"\x27]+)["\x27]'; confidence = 52; evidence = 'assembly load' }
    )

    foreach ($entry in $patterns) {
        foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Text, $entry.pattern)) {
            if ($match.Groups.Count -le 1) { continue }
            $value = $match.Groups[1].Value.Trim()
            if ($value.Length -eq 0) { continue }
            $deps.Add([pscustomobject]@{
                reference = $value
                confidence = $entry.confidence
                evidence = $entry.evidence
            })
        }
    }

    return [object[]]$deps.ToArray()
}

function Get-HeuristicSignals {
    param([string]$Text)

    $signals = New-Object System.Collections.Generic.List[object]
    $patterns = @(
        @{ pattern = '["\x27](\.?\.?/[^"\x27]+)["\x27]'; confidence = 35; evidence = 'path-like string' }
        @{ pattern = '["\x27]([A-Za-z0-9_\-/]+\.(?:js|ts|tsx|jsx|py|ps1|cs|json|yaml|yml))["\x27]'; confidence = 30; evidence = 'file-like string' }
        @{ pattern = '["\x27]([A-Z][A-Za-z0-9]+(?:Service|Controller|Provider|Handler|Module|Component))["\x27]'; confidence = 25; evidence = 'symbolic service/component string' }
    )

    foreach ($entry in $patterns) {
        foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Text, $entry.pattern)) {
            if ($match.Groups.Count -le 1) { continue }
            $value = $match.Groups[1].Value.Trim()
            if ($value.Length -eq 0) { continue }
            $signals.Add([pscustomobject]@{
                reference = $value
                confidence = $entry.confidence
                evidence = $entry.evidence
            })
        }
    }

    return [object[]]$signals.ToArray()
}

function Get-Exports {
    param([string]$Text)

    $exports = New-Object System.Collections.Generic.List[string]
    $patterns = @(
        '(?m)^\s*(?:def|async\s+def|function|class|interface)\s+([A-Za-z_][A-Za-z0-9_]*)',
        '(?m)^export\s+(?:default\s+)?(?:class|function|const)\s+([A-Za-z_][A-Za-z0-9_]*)'
    )

    foreach ($pattern in $patterns) {
        foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
            $value = $match.Groups[1].Value.Trim()
            if ($value.Length -gt 0 -and -not $exports.Contains($value)) {
                $exports.Add($value)
            }
        }
    }

    return @($exports | Sort-Object)
}

function Get-ConditionalFlags {
    param([string]$Text)

    return [ordered]@{
        feature_flags = [System.Text.RegularExpressions.Regex]::IsMatch($Text, '\b(featureFlag|feature_flag|isFeatureEnabled|enableFeature|FEATURE_|FF_)')
        env_checks = [System.Text.RegularExpressions.Regex]::IsMatch($Text, '\b(ENV|ENVIRONMENT|NODE_ENV|process\.env|os\.environ|%ENV%|\$env:)')
        conditional_loading = [System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?is)(if|switch|try).{0,300}(import|require|Import-Module|Assembly\.Load)')
    }
}

function Get-RiskFlags {
    param([string]$Text)

    $flags = New-Object System.Collections.Generic.List[string]
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)\b(eval|exec)\b')) { $flags.Add('unsafe_eval_exec') }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)Invoke-Expression')) { $flags.Add('dynamic_execution') }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(Remove-Item|Delete\(|File\.Delete|Directory\.Delete|unlink\()')) { $flags.Add('filesystem_mutation') }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(HttpClient|fetch\(|requests\.|Invoke-WebRequest|socket\()')) { $flags.Add('network_surface') }
    return @($flags)
}

function Get-ComplexityModel {
    param([string]$Text, [int64]$Size, [object[]]$StaticSignals, [object[]]$DynamicSignals)

    $functionCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:def|async\s+def|function)\s+').Count)
    $classCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:class|interface)\s+').Count)
    $ifCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:if|else\s+if|elseif|elif)\b').Count)
    $switchCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:switch|case)\b').Count)
    $tryCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:try|catch|except|finally)\b').Count)

    $structural = 0
    if ($Size -gt 32768) { $structural += 1 }
    if (($functionCount + $classCount) -gt 10) { $structural += 1 }
    if (($functionCount + $classCount) -gt 30 -or $Size -gt 131072) { $structural += 1 }

    $coupling = 0
    $signalCount = @($StaticSignals).Count + @($DynamicSignals).Count
    if ($signalCount -gt 4) { $coupling += 1 }
    if ($signalCount -gt 12) { $coupling += 1 }
    if ($signalCount -gt 24) { $coupling += 1 }

    $branching = 0
    if (($ifCount + $switchCount) -gt 8) { $branching += 1 }
    if (($ifCount + $switchCount + $tryCount) -gt 20) { $branching += 1 }

    $riskFlags = Get-RiskFlags -Text $Text
    $riskSurface = [Math]::Min(2, [Math]::Ceiling(@($riskFlags).Count / 2.0))

    return [ordered]@{
        structural_complexity = [Math]::Min(3, $structural)
        coupling_score = [Math]::Min(3, $coupling)
        branching_score = [Math]::Min(2, $branching)
        risk_surface_score = $riskSurface
        total = [Math]::Min(10, $structural + $coupling + $branching + $riskSurface)
        function_count = $functionCount
        class_count = $classCount
        branch_nodes = $ifCount + $switchCount + $tryCount
        risk_flags = @($riskFlags)
    }
}

function Analyze-File {
    param($Entry)

    $fullPath = Join-Path $TargetRepoPath ($Entry.file.Replace('/', '\'))
    $text = ''
    try {
        $text = [System.IO.File]::ReadAllText($fullPath)
    }
    catch {
        $text = ''
    }

    $staticSignals = Get-StaticDependencies -Text $text -FilePath $Entry.file
    $dynamicSignals = Get-DynamicDependencies -Text $text
    $heuristicSignals = Get-HeuristicSignals -Text $text
    $conditional = Get-ConditionalFlags -Text $text
    $complexity = Get-ComplexityModel -Text $text -Size $Entry.size -StaticSignals $staticSignals -DynamicSignals $dynamicSignals
    $exports = Get-Exports -Text $text

    return [ordered]@{
        file = $Entry.file
        hash = $Entry.hash
        size = $Entry.size
        extension = $Entry.extension
        module_type = $Entry.module_type
        analysis_key = $Entry.analysis_key
        static_candidates = @($staticSignals)
        dynamic_candidates = @($dynamicSignals)
        heuristic_signals = @($heuristicSignals)
        exports = @($exports)
        conditional_flags = $conditional
        structural_complexity = $complexity.structural_complexity
        coupling_score = $complexity.coupling_score
        branching_score = $complexity.branching_score
        risk_surface_score = $complexity.risk_surface_score
        complexity_total = $complexity.total
        function_count = $complexity.function_count
        class_count = $complexity.class_count
        branch_nodes = $complexity.branch_nodes
        risk_flags = @($complexity.risk_flags)
    }
}

$removedCount = 0
foreach ($removedPath in @($index.removed_files)) {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($removedPath.ToLowerInvariant())
    $sha1 = [System.Security.Cryptography.SHA1]::Create()
    try {
        $key = ([System.BitConverter]::ToString($sha1.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha1.Dispose()
    }
    $cachePath = Get-AnalysisCachePath -AnalysisKey $key
    if (Test-Path -LiteralPath $cachePath) {
        Remove-Item -LiteralPath $cachePath -Force
        $removedCount++
    }
}

$processedChanged = 0
foreach ($entry in @($index.files)) {
    $cachePath = Get-AnalysisCachePath -AnalysisKey $entry.analysis_key
    if ($changedSet.Contains($entry.file) -or -not (Test-Path -LiteralPath $cachePath)) {
        $record = Analyze-File -Entry $entry
        $record | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $cachePath -Encoding UTF8
        $processedChanged++
    }
}

$writer = [System.IO.StreamWriter]::new($AuditLogPath, $false, [System.Text.UTF8Encoding]::new($false))
try {
    foreach ($entry in @($index.files)) {
        $cachePath = Get-AnalysisCachePath -AnalysisKey $entry.analysis_key
        if (-not (Test-Path -LiteralPath $cachePath)) {
            $record = Analyze-File -Entry $entry
            $record | ConvertTo-Json -Depth 8 | Out-File -LiteralPath $cachePath -Encoding UTF8
        }
        $json = (Get-Content -LiteralPath $cachePath -Raw | ConvertFrom-Json -Depth 8 | ConvertTo-Json -Compress -Depth 8)
        $writer.WriteLine($json)
    }
}
finally {
    $writer.Dispose()
}

Write-Status 'Incremental analysis complete'
Write-Status "Changed files analyzed: $processedChanged"
Write-Status "Removed cache entries: $removedCount"
Write-Status "Audit log: $AuditLogPath"
