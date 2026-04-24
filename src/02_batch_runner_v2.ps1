[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$ManifestPath,
    [string]$ProgressPath,
    [string]$AuditLogPath,
    [ValidateRange(10, 20)][int]$BatchSize = 10
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
if ([string]::IsNullOrWhiteSpace($ProgressPath)) {
    $ProgressPath = Join-Path $EngineRoot "state\progress.txt"
}
if ([string]::IsNullOrWhiteSpace($AuditLogPath)) {
    $AuditLogPath = Join-Path $EngineRoot "state\audit_log.jsonl"
}

if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Manifest missing: $ManifestPath"
}

New-Item -ItemType Directory -Path (Split-Path -Parent $ProgressPath) -Force | Out-Null
if (-not (Test-Path -LiteralPath $ProgressPath)) {
    [System.IO.File]::WriteAllText($ProgressPath, "", [System.Text.UTF8Encoding]::new($false))
}
if (-not (Test-Path -LiteralPath $AuditLogPath)) {
    [System.IO.File]::WriteAllText($AuditLogPath, "", [System.Text.UTF8Encoding]::new($false))
}

$processed = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::OrdinalIgnoreCase)
foreach ($line in [System.IO.File]::ReadLines($ProgressPath)) {
    $p = $line.Trim()
    if ($p.Length -gt 0) {
        [void]$processed.Add($p)
    }
}

$batch = New-Object System.Collections.Generic.List[string]

# PROBABILISTIC DEPENDENCY DETECTION WITH CONFIDENCE SCORING
function Get-Imports {
    param([string]$Text, [string]$FilePath)
    
    # Return: { direct: [], dynamic: [], template: [], reflection: [], confidence: {} }
    $directDeps = New-Object System.Collections.Generic.List[string]
    $dynamicDeps = New-Object System.Collections.Generic.List[string]
    $templateDeps = New-Object System.Collections.Generic.List[string]
    $reflectionDeps = New-Object System.Collections.Generic.List[string]
    
    $confidence = @{}  # Track confidence for each dependency
    
    # === STATIC PATTERNS (HIGH CONFIDENCE: 100) ===
    $staticPatterns = @(
        '(?m)^\s*import\s+([A-Za-z0-9_\.\-/]+)'
        '(?m)^\s*from\s+([A-Za-z0-9_\.\-/]+)\s+import\s+'
        'require\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'
        'import\s+.*from\s+["\x27]([^"\x27]+)["\x27]'
        '(?m)^\s*#include\s+[<"]([^>"]+)[>"]'
        '(?m)^\s*using\s+([A-Za-z0-9_\.]+);'
        '(?m)Import-Module\s+["\x27]?([A-Za-z0-9_\.\-/\\]+)'
    )
    
    foreach ($pattern in $staticPatterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
            if ($m.Groups.Count -gt 1) {
                $v = $m.Groups[1].Value.Trim()
                if ($v.Length -gt 0 -and -not $directDeps.Contains($v)) {
                    $directDeps.Add($v)
                    $confidence[$v] = 100
                }
            }
        }
    }
    
    # === DYNAMIC PATTERNS (MEDIUM CONFIDENCE: 75) ===
    $dynamicPatterns = @(
        '__import__\(\s*["\x27]([^"\x27]*)["\x27]'
        'importlib\.import_module\(\s*["\x27]([^"\x27]*)["\x27]'
        'import\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'
        'require\.resolve\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'
    )
    
    foreach ($pattern in $dynamicPatterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
            if ($m.Groups.Count -gt 1) {
                $v = $m.Groups[1].Value.Trim()
                if ($v.Length -gt 0 -and -not $dynamicDeps.Contains($v)) {
                    $dynamicDeps.Add($v)
                    $confidence[$v] = 75
                }
            }
        }
    }
    
    # === REFLECTION/RUNTIME PATTERNS (LOW CONFIDENCE: 40) ===
    $reflectionPatterns = @(
        'GetType\(\s*["\x27]([^"\x27]+)["\x27]'
        'getattr\(\s*\w+\s*,\s*["\x27]([^"\x27]+)["\x27]'
        'eval\(\s*["\x27]([^"\x27]+)["\x27]'
        'exec\(\s*["\x27]([^"\x27]+)["\x27]'
    )
    
    foreach ($pattern in $reflectionPatterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
            if ($m.Groups.Count -gt 1) {
                $v = $m.Groups[1].Value.Trim()
                if ($v.Length -gt 0 -and -not $reflectionDeps.Contains($v)) {
                    $reflectionDeps.Add($v)
                    $confidence[$v] = 40
                }
            }
        }
    }
    
    # === TEMPLATE/FRONTEND PATTERNS (CONFIDENCE: 60) ===
    if ($FilePath -match '\.(html|jsx?|tsx?|vue|svelte)$') {
        $templatePatterns = @(
            '<\s*script\s+src=["\x27]([^"\x27]+)["\x27]'
            '<\s*link\s+rel=["\x27]stylesheet["\x27]\s+href=["\x27]([^"\x27]+)["\x27]'
            'import\s+.*from\s+["\x27]@?([^"\x27]+)["\x27]'
        )
        
        foreach ($pattern in $templatePatterns) {
            foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
                if ($m.Groups.Count -gt 1) {
                    $v = $m.Groups[1].Value.Trim()
                    if ($v.Length -gt 0 -and -not $templateDeps.Contains($v)) {
                        $templateDeps.Add($v)
                        $confidence[$v] = 60
                    }
                }
            }
        }
    }
    
    return @{
        direct = @($directDeps | Sort-Object -Unique)
        dynamic = @($dynamicDeps | Sort-Object -Unique)
        template = @($templateDeps | Sort-Object -Unique)
        reflection = @($reflectionDeps | Sort-Object -Unique)
        confidence = $confidence
    }
}

# DETECT CONDITIONAL EXECUTION PATTERNS
function Get-ConditionalPatterns {
    param([string]$Text)
    
    $conditionalPatterns = @{
        feature_flags = 0
        env_checks = 0
        conditional_imports = 0
        detected = $false
    }
    
    if ($Text -match '\b(featureFlag|feature_flag|isFeatureEnabled|enableFeature|FEATURE_|FF_)') {
        $conditionalPatterns.feature_flags = 1
        $conditionalPatterns.detected = $true
    }
    
    if ($Text -match '\b(ENV|ENVIRONMENT|NODE_ENV|process\.env|os\.environ|%ENV%|\$env:)') {
        $conditionalPatterns.env_checks = 1
        $conditionalPatterns.detected = $true
    }
    
    if ($Text -match '(?m)(if|switch|try)\s*\{[^}]*(?:import|require)') {
        $conditionalPatterns.conditional_imports = 1
        $conditionalPatterns.detected = $true
    }
    
    return $conditionalPatterns
}

# MULTI-FACTOR DEEPER COMPLEXITY SCORING
function Get-ComplexityScores {
    param(
        [string]$Text,
        [hashtable]$Imports,
        [int]$FileSize
    )
    
    # STRUCTURAL COMPLEXITY (0–3)
    $functionCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:def|function|class)').Count)
    $classCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*class\s+').Count)
    
    $structuralScore = 0
    if ($FileSize -gt 100) { $structuralScore += 1 }
    if ($functionCount -gt 10) { $structuralScore += 1 }
    if ($classCount -gt 3) { $structuralScore += 1 }
    $structuralScore = [Math]::Min(3, $structuralScore)
    
    # COUPLING SCORE (0–3)
    $totalDeps = @($Imports.direct).Count + @($Imports.dynamic).Count + @($Imports.template).Count
    $couplingScore = 0
    if ($totalDeps -gt 5) { $couplingScore += 1 }
    if ($totalDeps -gt 10) { $couplingScore += 1 }
    if ($totalDeps -gt 20) { $couplingScore += 1 }
    $couplingScore = [Math]::Min(3, $couplingScore)
    
    # BRANCHING SCORE (0–2)
    $ifCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*(?:if|else)\s*').Count)
    $switchCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*switch\s*').Count)
    $tryCount = ([System.Text.RegularExpressions.Regex]::Matches($Text, '(?m)^\s*try\s*').Count)
    
    $branchingScore = 0
    if ($ifCount -gt 10) { $branchingScore += 1 }
    if ($switchCount -gt 2 -or $tryCount -gt 3) { $branchingScore += 1 }
    $branchingScore = [Math]::Min(2, $branchingScore)
    
    # RISK SURFACE SCORE (0–2)
    $riskSurfaceScore = 0
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)\b(eval|exec)\b')) { $riskSurfaceScore += 1 }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)Invoke-Expression')) { $riskSurfaceScore += 1 }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(file|directory|delete|remove)\s*\(')) { $riskSurfaceScore += 1 }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(http|tcp|socket|connect)\s*\(')) { $riskSurfaceScore += 1 }
    $riskSurfaceScore = [Math]::Min(2, [Math]::Ceiling($riskSurfaceScore / 2))
    
    return @{
        structural_complexity = $structuralScore
        coupling_score = $couplingScore
        branching_score = $branchingScore
        risk_surface_score = $riskSurfaceScore
        total = $structuralScore + $couplingScore + $branchingScore + $riskSurfaceScore
    }
}

function Get-Exports {
    param([string]$Text, [string]$FilePath)
    
    $exports = New-Object System.Collections.Generic.List[string]
    
    $patterns = @(
        @{ pattern = '(?m)^\s*(?:def|async\s+def|function)\s+([A-Za-z_][A-Za-z0-9_]*)'; name = "function" }
        @{ pattern = '(?m)^\s*(?:class|interface)\s+([A-Za-z_][A-Za-z0-9_]*)'; name = "class" }
        @{ pattern = '(?m)^export\s+(?:default\s+)?(?:class|function|const)\s+([A-Za-z_][A-Za-z0-9_]*)'; name = "export" }
    )

    foreach ($p in $patterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Text, $p.pattern)) {
            $v = $m.Groups[1].Value.Trim()
            if ($v.Length -gt 0 -and -not $exports.Contains($v)) {
                $exports.Add($v)
            }
        }
    }

    return @($exports | Sort-Object)
}

function Get-Classification {
    param(
        [string]$Path,
        [string]$Extension,
        [string]$Text
    )

    $lower = $Path.ToLowerInvariant()
    if ($lower -match '(^|/)(test|tests|__tests__|spec)(/|$)' -or $lower -match '(\.test\.|\.spec\.)') { return "test" }
    if ($lower -match '(^|/)(config|configs|settings)(/|$)' -or $Extension -in @('.json', '.yaml', '.yml', '.toml', '.ini', '.env', '.xml')) { return "config" }
    if ($lower -match '(^|/)(core|engine|main|app)(/|$)' -or $lower -like '*/index.*') { return "core" }

    $hasLogic = [System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?m)^\s*(function|class|def|interface|module)\b')
    if (-not $hasLogic -and $Extension -in @('.ps1', '.py', '.js', '.ts', '.cs', '.java', '.go', '.cpp', '.c')) { return "dead_candidate" }
    return "utility"
}

function Get-RiskFlags {
    param([string]$Text)

    $flags = New-Object System.Collections.Generic.List[string]
    
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)\b(eval|exec)\b')) {
        $flags.Add("unsafe_eval_exec")
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)Invoke-Expression')) {
        $flags.Add("dynamic_execution")
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(file|directory|delete|remove)\s*\(')) {
        $flags.Add("filesystem_operations")
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(http|tcp|socket|connect)\s*\(')) {
        $flags.Add("network_operations")
    }

    return @($flags)
}

function Analyze-And-Append {
    param([string]$RelativePath)

    $fullPath = Join-Path $TargetRepoPath ($RelativePath.Replace('/', '\'))

    $text = ""
    if (Test-Path -LiteralPath $fullPath) {
        try {
            $text = [System.IO.File]::ReadAllText($fullPath)
        }
        catch {
            $text = ""
        }
    }

    $ext = [System.IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
    $imports = Get-Imports -Text $text -FilePath $RelativePath
    $exports = Get-Exports -Text $text -FilePath $RelativePath
    $classification = Get-Classification -Path $RelativePath -Extension $ext -Text $text
    $conditionalPatterns = Get-ConditionalPatterns -Text $text
    $complexity = Get-ComplexityScores -Text $text -Imports $imports -FileSize ([int]($text.Length / 1024))
    $riskFlags = Get-RiskFlags -Text $text

    $record = [ordered]@{
        file = $RelativePath
        classification = $classification
        direct_dependencies = @($imports.direct)
        dynamic_dependencies = @($imports.dynamic)
        template_dependencies = @($imports.template)
        reflection_dependencies = @($imports.reflection)
        dependency_confidence = $imports.confidence
        exports = @($exports)
        conditional_execution = $conditionalPatterns.detected
        conditional_flags = $conditionalPatterns
        structural_complexity = $complexity.structural_complexity
        coupling_score = $complexity.coupling_score
        branching_score = $complexity.branching_score
        risk_surface_score = $complexity.risk_surface_score
        complexity_total = $complexity.total
        risk_flags = @($riskFlags)
        file_size_kb = [Math]::Round(($text.Length / 1024), 2)
        function_count = ([System.Text.RegularExpressions.Regex]::Matches($text, '(?m)^\s*(?:def|function)').Count)
    }

    $json = $record | ConvertTo-Json -Compress
    Add-Content -LiteralPath $AuditLogPath -Value $json -Encoding UTF8
    Add-Content -LiteralPath $ProgressPath -Value $RelativePath -Encoding UTF8
}

$manifestLines = [System.IO.File]::ReadLines($ManifestPath)

foreach ($line in $manifestLines) {
    $relativePath = $line.Trim()
    if ($relativePath.Length -eq 0 -or $processed.Contains($relativePath)) {
        continue
    }

    $batch.Add($relativePath)

    if ($batch.Count -ge $BatchSize) {
        foreach ($filePath in $batch) {
            Analyze-And-Append -RelativePath $filePath
        }
        $batch.Clear()
    }
}

if ($batch.Count -gt 0) {
    foreach ($filePath in $batch) {
        Analyze-And-Append -RelativePath $filePath
    }
}

Write-Host "Batch analysis complete."
