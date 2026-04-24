[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$ManifestPath,
    [string]$ConfigDepsPath
)

$ErrorActionPreference = "Stop"

$EngineRoot = (Resolve-Path -LiteralPath $EngineRoot).Path
$TargetRepoPath = (Resolve-Path -LiteralPath $TargetRepoPath).Path

if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $EngineRoot "state\manifest.txt"
}

if ([string]::IsNullOrWhiteSpace($ConfigDepsPath)) {
    $ConfigDepsPath = Join-Path $EngineRoot "state\config_dependencies.json"
}

if (-not (Test-Path -LiteralPath $ManifestPath)) {
    Write-Host "Config analyzer: manifest file not found, skipping"
    "[]" | Out-File -LiteralPath $ConfigDepsPath -Encoding UTF8
    exit 0
}

New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigDepsPath) -Force | Out-Null

# PARSE JSON CONFIG FILES
function Parse-JsonConfig {
    param([string]$FilePath, [string]$Content)
    
    $deps = New-Object System.Collections.Generic.List[object]
    
    try {
        $json = $Content | ConvertFrom-Json -ErrorAction SilentlyContinue
        if (-not $json) { return @() }
        
        # Check for common dependency patterns in JSON
        $patterns = @(
            "dependencies", "devDependencies", "optionalDependencies",
            "plugins", "extensions", "modules", "imports",
            "services", "providers", "handlers", "middleware",
            "routes", "controllers", "components", "directives"
        )
        
        foreach ($pattern in $patterns) {
            if ($json.PSObject.Properties.Name -contains $pattern) {
                $value = $json.$pattern
                
                if ($value -is [hashtable]) {
                    foreach ($key in $value.Keys) {
                        $deps.Add(@{
                            source = $FilePath
                            type = "config_$pattern"
                            target = $key
                            confidence = 80
                        })
                    }
                }
                elseif ($value -is [array]) {
                    foreach ($item in $value) {
                        if ($item -is [string]) {
                            $deps.Add(@{
                                source = $FilePath
                                type = "config_$pattern"
                                target = $item
                                confidence = 80
                            })
                        }
                        elseif ($item -is [hashtable] -and $item.PSObject.Properties.Name -contains "name") {
                            $deps.Add(@{
                                source = $FilePath
                                type = "config_$pattern"
                                target = $item.name
                                confidence = 80
                            })
                        }
                    }
                }
            }
        }
    }
    catch {
        # Silent fail on JSON parse errors
    }
    
    return @($deps)
}

# PARSE YAML CONFIG FILES (SIMPLIFIED)
function Parse-YamlConfig {
    param([string]$FilePath, [string]$Content)
    
    $deps = New-Object System.Collections.Generic.List[object]
    
    # Simple YAML key-value extraction (basic patterns)
    $patterns = @(
        'dependencies:\s*(\[[^\]]+\]|[\w\s,]+)',
        'plugins:\s*(\[[^\]]+\]|[\w\s,]+)',
        'services:\s*(\[[^\]]+\]|[\w\s,]+)',
        'modules:\s*(\[[^\]]+\]|[\w\s,]+)'
    )
    
    foreach ($pattern in $patterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Content, $pattern)) {
            if ($m.Groups.Count -gt 1) {
                $value = $m.Groups[1].Value.Trim()
                $items = $value -replace '\[|\]|"' -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_.Length -gt 0 }
                
                foreach ($item in $items) {
                    $deps.Add(@{
                        source = $FilePath
                        type = "config_yaml"
                        target = $item
                        confidence = 60
                    })
                }
            }
        }
    }
    
    return @($deps)
}

# DETECT ENVIRONMENT VARIABLE DEPENDENCIES
function Parse-EnvDependencies {
    param([string]$FilePath, [string]$Content)
    
    $deps = New-Object System.Collections.Generic.List[object]
    
    $patterns = @(
        'process\.env\.(\w+)',
        'os\.environ\[?["\x27]?(\w+)',
        '\$env:(\w+)',
        '%(\w+)%',
        'ENV\["?(\w+)'
    )
    
    foreach ($pattern in $patterns) {
        foreach ($m in [System.Text.RegularExpressions.Regex]::Matches($Content, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
            if ($m.Groups.Count -gt 1) {
                $varName = $m.Groups[1].Value.Trim()
                if ($varName.Length -gt 0) {
                    $deps.Add(@{
                        source = $FilePath
                        type = "env_variable"
                        target = $varName
                        confidence = 50
                    })
                }
            }
        }
    }
    
    return @($deps)
}

# AGGREGATE CONFIG DEPENDENCIES
$allConfigDeps = @{}

try {
    $manifestLines = @([System.IO.File]::ReadLines($ManifestPath) | Where-Object { $_.Trim().Length -gt 0 })
} catch {
    Write-Host "Warning: Could not read manifest file"
    "[]" | Out-File -LiteralPath $ConfigDepsPath -Encoding UTF8
    exit 0
}

foreach ($line in $manifestLines) {
    $relativePath = $line.Trim()
    if ($relativePath.Length -eq 0) { continue }
    
    $fullPath = Join-Path $TargetRepoPath ($relativePath.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $fullPath)) { continue }
    
    $ext = [System.IO.Path]::GetExtension($fullPath).ToLowerInvariant()
    
    try {
        $content = [System.IO.File]::ReadAllText($fullPath)
    }
    catch {
        continue
    }
    
    $configDeps = @()
    
    if ($ext -eq '.json') {
        $configDeps += Parse-JsonConfig -FilePath $relativePath -Content $content
    }
    elseif ($ext -in @('.yml', '.yaml')) {
        $configDeps += Parse-YamlConfig -FilePath $relativePath -Content $content
        $configDeps += Parse-EnvDependencies -FilePath $relativePath -Content $content
    }
    
    if ($ext -in @('.env', '.xml', '.ini', '.properties', '.toml')) {
        $configDeps += Parse-EnvDependencies -FilePath $relativePath -Content $content
    }
    
    foreach ($dep in $configDeps) {
        $key = "$($dep.source)::$($dep.target)"
        if (-not $allConfigDeps.ContainsKey($key)) {
            $allConfigDeps[$key] = $dep
        }
    }
}

# Save config dependencies
$configOutput = @()
foreach ($dep in $allConfigDeps.Values | Sort-Object { $_.source }) {
    $configOutput += $dep
}

@($configOutput) | ConvertTo-Json | Out-File -LiteralPath $ConfigDepsPath -Encoding UTF8

Write-Host "Config analyzer: $($configOutput.Count) config-driven dependencies detected"
