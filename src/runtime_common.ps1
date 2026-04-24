$script:AuditEngineVersion = '1.0.0'
$script:AuditSchemaVersion = 'v3'
$script:AuditCiMode = $false

function Get-AuditEngineVersion {
    return $script:AuditEngineVersion
}

function Get-AuditSchemaVersion {
    return $script:AuditSchemaVersion
}

function Set-AuditCiMode {
    param([bool]$Enabled)

    $script:AuditCiMode = $Enabled
}

function Write-Status {
    param([string]$Message)

    if (-not $script:AuditCiMode) {
        Write-Host $Message
    }
}

function Write-AuditWarning {
    param([string]$Message)

    if (-not $script:AuditCiMode) {
        Write-Warning $Message
    }
}

function Get-StringHash {
    param(
        [string]$Value,
        [ValidateSet('SHA1', 'SHA256')][string]$Algorithm = 'SHA256',
        [int]$Length = 0
    )

    if ($null -eq $Value) {
        $Value = ''
    }

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hasher = [System.Security.Cryptography.HashAlgorithm]::Create($Algorithm)
    if ($null -eq $hasher) {
        throw "Unsupported hash algorithm: $Algorithm"
    }

    try {
        $hash = ([System.BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $hasher.Dispose()
    }

    if ($Length -gt 0 -and $hash.Length -gt $Length) {
        return $hash.Substring(0, $Length)
    }

    return $hash
}

function Get-RepoId {
    param([string]$RepoPath)

    $normalized = [System.IO.Path]::GetFullPath($RepoPath)
    return ('repo_{0}' -f (Get-StringHash -Value $normalized.ToLowerInvariant() -Length 8))
}

function Get-GitCommitHash {
    param([string]$RepoPath)

    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $gitCommand) {
        return $null
    }

    try {
        $result = & $gitCommand.Source -C $RepoPath rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) {
            return ([string]$result[0]).Trim()
        }
    }
    catch {
        return $null
    }

    return $null
}

function Get-ConfigHash {
    param([string]$ConfigRoot)

    if ([string]::IsNullOrWhiteSpace($ConfigRoot) -or -not (Test-Path -LiteralPath $ConfigRoot -PathType Container)) {
        return (Get-StringHash -Value 'no-config' -Length 12)
    }

    $entries = New-Object System.Collections.Generic.List[string]
    $options = [System.IO.EnumerationOptions]::new()
    $options.RecurseSubdirectories = $true
    $options.IgnoreInaccessible = $true
    $options.ReturnSpecialDirectories = $false

    foreach ($path in [System.IO.Directory]::EnumerateFiles($ConfigRoot, '*', $options) | Sort-Object) {
        $relative = [System.IO.Path]::GetRelativePath($ConfigRoot, $path).Replace('\\', '/')
        $contentHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        $entries.Add("$relative|$contentHash")
    }

    return (Get-StringHash -Value ($entries -join "`n") -Length 12)
}

function Get-DeterministicRunId {
    param(
        [string]$RepoPath,
        [string]$GitCommit,
        [string]$EngineVersion,
        [string]$ConfigHash
    )

    $material = [ordered]@{
        repo_path = [System.IO.Path]::GetFullPath($RepoPath)
        git_commit = if ([string]::IsNullOrWhiteSpace($GitCommit)) { 'no-git' } else { $GitCommit }
        engine_version = $EngineVersion
        config_hash = $ConfigHash
    } | ConvertTo-Json -Compress -Depth 4

    return ('run_{0}' -f (Get-StringHash -Value $material -Length 16))
}

function Write-JsonFile {
    param(
        [string]$Path,
        [object]$Data,
        [int]$Depth = 12
    )

    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    $Data | ConvertTo-Json -Depth $Depth | Out-File -LiteralPath $Path -Encoding UTF8
}

function Read-JsonFile {
    param(
        [string]$Path,
        [int]$Depth = 12
    )

    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -Depth $Depth)
}

function New-ArtifactEnvelope {
    param(
        [object]$RunMetadata,
        [object]$Data,
        [string]$ArtifactName,
        [hashtable]$ExtraMetadata,
        [string]$Timestamp
    )

    if ([string]::IsNullOrWhiteSpace($Timestamp)) {
        $Timestamp = (Get-Date).ToUniversalTime().ToString('o')
    }

    $envelope = [ordered]@{
        run_id = $RunMetadata.run_id
        repo_id = $RunMetadata.repo_id
        timestamp = $Timestamp
        engine_version = $RunMetadata.engine_version
        schema_version = $RunMetadata.schema_version
    }

    if (-not [string]::IsNullOrWhiteSpace($ArtifactName)) {
        $envelope.artifact = $ArtifactName
    }

    if ($ExtraMetadata) {
        foreach ($key in $ExtraMetadata.Keys) {
            $envelope[$key] = $ExtraMetadata[$key]
        }
    }

    $envelope.data = $Data
    return $envelope
}

function Write-JsonArtifact {
    param(
        [string]$Path,
        [object]$RunMetadata,
        [object]$Data,
        [string]$ArtifactName,
        [hashtable]$ExtraMetadata,
        [string]$Timestamp,
        [int]$Depth = 12
    )

    $envelope = New-ArtifactEnvelope -RunMetadata $RunMetadata -Data $Data -ArtifactName $ArtifactName -ExtraMetadata $ExtraMetadata -Timestamp $Timestamp
    Write-JsonFile -Path $Path -Data $envelope -Depth $Depth
}

function Read-JsonArtifact {
    param(
        [string]$Path,
        [int]$Depth = 12
    )

    $json = Read-JsonFile -Path $Path -Depth $Depth
    if ($null -ne $json -and $json.PSObject.Properties.Name -contains 'data' -and $json.PSObject.Properties.Name -contains 'schema_version') {
        return $json.data
    }

    return $json
}

function Read-RunMetadata {
    param([string]$Path)

    return (Read-JsonFile -Path $Path -Depth 12)
}

    # ========================================================
    # LLM LAYER FEATURE FLAGS & CONFIGURATION
    # ========================================================

    $script:LLMMode = 'static'  # static | semantic | full

    function Set-LLMMode {
        <#
        .SYNOPSIS
        Configure LLM layer execution mode
    
        .PARAMETER Mode
        static    = only deterministic engine (no LLM)
        semantic  = static + semantic interpretation layer
        full      = static + semantic + decision prioritization layer
        #>
        param(
            [ValidateSet('static', 'semantic', 'full')][string]$Mode = 'static'
        )
        $script:LLMMode = $Mode
    }

    function Get-LLMMode {
        return $script:LLMMode
    }

    function Test-LLMConfigured {
        <#
        .SYNOPSIS
        Check if LLM is configured and available
        #>
        # Check for environment variables or config
        return (-not [string]::IsNullOrEmpty($env:OPENAI_API_KEY) -or -not [string]::IsNullOrEmpty($env:AZURE_OPENAI_ENDPOINT))
    }

