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

function Get-Imports {
    param([string]$Text, [string]$FilePath)
    
    $directDeps = New-Object System.Collections.Generic.List[string]
    $dynamicDeps = New-Object System.Collections.Generic.List[string]
    
    # Python patterns
    $patterns = @(
        # Python - static
        '(?m)^\s*import\s+([A-Za-z0-9_\.\-/]+)'
        '(?m)^\s*from\s+([A-Za-z0-9_\.\-/]+)\s+import\s+'
        # Python - dynamic (simplified: just detect __import__ and importlib calls)
        '__import__\(\s*["\x27]([^"\x27]*)["\x27]'
        'importlib\.import_module\(\s*["\x27]([^"\x27]*)["\x27]'
        
        # JavaScript/TypeScript - static
        'require\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'
        'import\s+.*from\s+["\x27]([^"\x27]+)["\x27]'
        # JavaScript/TypeScript - dynamic
        'import\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'
        'require\.resolve\(\s*["\x27]([^"\x27]+)["\x27]\s*\)'
        
        # C/C++
        '(?m)^\s*#include\s+[<"]([^>"]+)[>"]'
        
        # C#
        '(?m)^\s*using\s+([A-Za-z0-9_\.]+);'
        
        # PowerShell - static
        '(?m)Import-Module\s+["\x27]?([A-Za-z0-9_\.\-/\\]+)'
        # PowerShell - dot-sourcing
        '(?m)^\s*\.\s+["\x27]([^"\x27]+)["\x27]'
    )

    foreach ($pattern in $patterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Text, $pattern)) {
            if ($m.Groups.Count -gt 1) {
                $v = $m.Groups[1].Value.Trim()
                if ($v.Length -gt 0) {
                    # Classify as dynamic if from __import__, importlib, dynamic import(), etc
                    if ($pattern -match '(__import__|importlib|import\(|require\.resolve)') {
                        if (-not $dynamicDeps.Contains($v)) { $dynamicDeps.Add($v) }
                    } else {
                        if (-not $directDeps.Contains($v)) { $directDeps.Add($v) }
                    }
                }
            }
        }
    }

    return @{
        direct = @($directDeps | Sort-Object -Unique)
        dynamic = @($dynamicDeps | Sort-Object -Unique)
    }
}

function Get-Exports {
    param([string]$Text, [string]$FilePath)
    
    $exports = New-Object System.Collections.Generic.List[string]
    
    # Extract function, class, or module definitions that could be exported
    $patterns = @(
        @{ pattern = '(?m)^\s*(?:def|async\s+def|function)\s+([A-Za-z_][A-Za-z0-9_]*)'; lang = @('py', 'js', 'ts'); name = "function" }
        @{ pattern = '(?m)^\s*(?:class|interface)\s+([A-Za-z_][A-Za-z0-9_]*)'; lang = @('py', 'js', 'ts', 'cs'); name = "class" }
        @{ pattern = '(?m)^export\s+(?:default\s+)?(?:class|function|const)\s+([A-Za-z_][A-Za-z0-9_]*)'; lang = @('js', 'ts'); name = "export" }
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

function Get-RiskScore {
    param(
        [string]$Text,
        [hashtable]$Imports
    )

    $score = 0
    $allDeps = @($Imports.direct) + @($Imports.dynamic)

    # Hardcoded risk flags (deterministic)
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)\b(eval|exec)\b')) { $score += 1 }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)Invoke-Expression')) { $score += 1 }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(file|directory|delete|remove)\s*\(')) { $score += 1 }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(http|tcp|socket|connect)\s*\(')) { $score += 1 }

    if ($score -gt 1) { $score = 1 }  # Cap at 1 for deterministic model
    return $score
}

function Get-Issues {
    param(
        [string]$Text,
        [int]$RiskScore,
        [string]$Classification
    )

    $issues = New-Object System.Collections.Generic.List[string]
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)\b(eval|exec)\b')) {
        $issues.Add("unsafe eval/exec usage")
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)Invoke-Expression')) {
        $issues.Add("dynamic execution detected")
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(file|directory|delete|remove)\s*\(')) {
        $issues.Add("filesystem operations detected")
    }
    if ([System.Text.RegularExpressions.Regex]::IsMatch($Text, '(?i)(http|tcp|socket|connect)\s*\(')) {
        $issues.Add("network operations detected")
    }

    return @($issues)
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
    $riskScore = Get-RiskScore -Text $text -Imports $imports
    $issues = Get-Issues -Text $text -RiskScore $riskScore -Classification $classification

    # Complexity score: file size + function count
    $fileSizeKB = ($text.Length / 1024)
    $functionCount = ([System.Text.RegularExpressions.Regex]::Matches($text, '(?m)^\s*(?:def|function|class|\w+\s+\w+\s*\()') | Measure-Object).Count

    $record = [ordered]@{
        file = $RelativePath
        classification = $classification
        direct_dependencies = @($imports.direct)
        suspected_dynamic_dependencies = @($imports.dynamic)
        exports = @($exports)
        risk_flags_score = $riskScore
        file_size_kb = [Math]::Round($fileSizeKB, 2)
        function_count = $functionCount
        issues = @($issues)
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
