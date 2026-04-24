$script:ToolName = 'repo-audit-engine'
$script:ToolVersion = 'phase0'

function Get-ToolInfo {
    return [ordered]@{
        name = $script:ToolName
        version = $script:ToolVersion
    }
}

function Write-Status {
    param([string]$Message)
    Write-Host $Message
}

function Write-Warn {
    param([string]$Message)
    Write-Warning $Message
}

function Ensure-Directory {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Path is required.'
    }
    return (New-Item -ItemType Directory -Path $Path -Force)
}

function Write-JsonFile {
    param(
        [string]$Path,
        [object]$Data,
        [int]$Depth = 12
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Path is required.'
    }

    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        Ensure-Directory -Path $parent | Out-Null
    }

    $Data | ConvertTo-Json -Depth $Depth | Out-File -LiteralPath $Path -Encoding UTF8
}

function Read-JsonFile {
    param(
        [string]$Path,
        [int]$Depth = 12
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "File not found: $Path"
    }

    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth $Depth)
}
