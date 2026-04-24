[CmdletBinding()]
param(
    [string]$EngineRoot,
    [string]$TargetRepoPath,
    [string]$RunOutputDir,
    [string]$EngineStateDir,
    [string]$RunMetadataPath,
    [string]$IndexPath,
    [string]$ConfigDepsPath,
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
if ([string]::IsNullOrWhiteSpace($ConfigDepsPath)) {
    $ConfigDepsPath = Join-Path $RUN_OUTPUT_DIR "config_dependencies.json"
}
if ([string]::IsNullOrWhiteSpace($RunMetadataPath)) {
    $RunMetadataPath = Join-Path $RUN_OUTPUT_DIR 'run_metadata.json'
}

if (-not (Test-Path -LiteralPath $IndexPath)) {
    throw "Codebase index missing: $IndexPath"
}

New-Item -ItemType Directory -Path $RUN_OUTPUT_DIR -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigDepsPath) -Force | Out-Null

$runMetadata = Read-RunMetadata -Path $RunMetadataPath
$index = Read-JsonArtifact -Path $IndexPath -Depth 12
$configFiles = @($index.files | Where-Object {
    $_.module_type -eq 'config' -or $_.extension -in @('.json', '.yaml', '.yml', '.toml', '.ini', '.env', '.xml', '.properties')
})

function Add-ConfigEdge {
    param(
        [System.Collections.Generic.List[object]]$Edges,
        [string]$Source,
        [string]$Target,
        [string]$Kind,
        [int]$Confidence,
        [string]$Evidence
    )

    if ([string]::IsNullOrWhiteSpace($Target)) { return }
    $Edges.Add([ordered]@{
        source = $Source
        target = $Target.Trim()
        kind = $Kind
        confidence = $Confidence
        evidence = $Evidence
    })
}

function Walk-JsonNode {
    param(
        [object]$Node,
        [string]$Source,
        [string]$Path,
        [System.Collections.Generic.List[object]]$Edges
    )

    if ($null -eq $Node) { return }

    if ($Node -is [System.Collections.IDictionary]) {
        foreach ($key in $Node.Keys) {
            $child = $Node[$key]
            $currentPath = if ($Path) { "$Path.$key" } else { [string]$key }
            if ($key -match '(?i)^(dependencies|devDependencies|plugins|modules|providers|services|handlers|middleware|controllers|routes|components|registry|registrations|bindings)$') {
                if ($child -is [System.Collections.IDictionary]) {
                    foreach ($childKey in $child.Keys) {
                        Add-ConfigEdge -Edges $Edges -Source $Source -Target ([string]$childKey) -Kind 'registry_key' -Confidence 82 -Evidence $currentPath
                        Add-ConfigEdge -Edges $Edges -Source $Source -Target ([string]$child[$childKey]) -Kind 'registry_value' -Confidence 78 -Evidence $currentPath
                    }
                }
                elseif ($child -is [System.Collections.IEnumerable] -and $child -isnot [string]) {
                    foreach ($item in $child) {
                        Add-ConfigEdge -Edges $Edges -Source $Source -Target ([string]$item) -Kind 'config_array' -Confidence 75 -Evidence $currentPath
                    }
                }
                else {
                    Add-ConfigEdge -Edges $Edges -Source $Source -Target ([string]$child) -Kind 'config_value' -Confidence 70 -Evidence $currentPath
                }
            }
            Walk-JsonNode -Node $child -Source $Source -Path $currentPath -Edges $Edges
        }
        return
    }

    if ($Node -is [System.Collections.IEnumerable] -and $Node -isnot [string]) {
        foreach ($child in $Node) {
            Walk-JsonNode -Node $child -Source $Source -Path $Path -Edges $Edges
        }
        return
    }

    if ($Node -is [string]) {
        if ($Node -match '^[A-Za-z0-9_./-]+$' -and $Node.Length -le 120) {
            if ($Path -match '(?i)(route|controller|handler|service|provider|plugin|module|component)') {
                Add-ConfigEdge -Edges $Edges -Source $Source -Target $Node -Kind 'config_string' -Confidence 68 -Evidence $Path
            }
        }
    }
}

function Parse-JsonConfig {
    param([string]$Source, [string]$Content, [System.Collections.Generic.List[object]]$Edges)

    try {
        $json = $Content | ConvertFrom-Json -AsHashtable -Depth 16 -ErrorAction Stop
        Walk-JsonNode -Node $json -Source $Source -Path '' -Edges $Edges
    }
    catch {
    }
}

function Parse-TextConfig {
    param([string]$Source, [string]$Content, [System.Collections.Generic.List[object]]$Edges)

    $patterns = @(
        @{ pattern = '(?im)^\s*(?:plugin|module|service|provider|handler|controller)s?\s*[:=]\s*["\x27]?([^"\x27#\r\n]+)'; kind = 'key_value'; confidence = 66 },
        @{ pattern = '(?im)^\s*route\s*[:=]\s*["\x27]?([^"\x27#\r\n]+)'; kind = 'route'; confidence = 64 },
        @{ pattern = '(?im)(?:bind|register|use)\s*\(\s*["\x27]([^"\x27]+)["\x27]'; kind = 'di_binding'; confidence = 72 },
        @{ pattern = '(?im)(?:process\.env|\$env:|ENV\[|%)([A-Za-z_][A-Za-z0-9_]*)'; kind = 'env_reference'; confidence = 60 }
    )

    foreach ($entry in $patterns) {
        foreach ($match in [System.Text.RegularExpressions.Regex]::Matches($Content, $entry.pattern)) {
            if ($match.Groups.Count -le 1) { continue }
            Add-ConfigEdge -Edges $Edges -Source $Source -Target $match.Groups[1].Value -Kind $entry.kind -Confidence $entry.confidence -Evidence $entry.pattern
        }
    }
}

$edges = New-Object System.Collections.Generic.List[object]
foreach ($entry in $configFiles) {
    $fullPath = Join-Path $TargetRepoPath ($entry.file.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $fullPath)) { continue }

    $content = ''
    try {
        $content = [System.IO.File]::ReadAllText($fullPath)
    }
    catch {
        continue
    }

    if ($entry.extension -eq '.json') {
        Parse-JsonConfig -Source $entry.file -Content $content -Edges $edges
    }
    else {
        Parse-TextConfig -Source $entry.file -Content $content -Edges $edges
    }
}

$deduped = @{}
foreach ($edge in $edges) {
    $key = '{0}|{1}|{2}|{3}' -f $edge.source, $edge.target, $edge.kind, $edge.evidence
    if (-not $deduped.ContainsKey($key)) {
        $deduped[$key] = $edge
    }
}

$output = @($deduped.Values | Sort-Object source, target, kind)
Write-JsonArtifact -Path $ConfigDepsPath -RunMetadata $runMetadata -Data $output -ArtifactName 'config_dependencies' -ExtraMetadata @{ edge_count = $output.Count } -Depth 10

Write-Status "Config truth edges: $($output.Count)"
Write-Status "Config truth file: $ConfigDepsPath"
