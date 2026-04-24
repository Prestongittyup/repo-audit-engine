function Resolve-Entrypoints {
    param(
        [Parameter(Mandatory = $true)]
        [object]$CanonicalNodesDoc,
        [string[]]$ExplicitEntrypoints
    )

    if ($null -eq $CanonicalNodesDoc -or $null -eq $CanonicalNodesDoc.nodes) {
        throw 'Canonical nodes document is missing nodes array.'
    }

    $nodeIds = New-Object System.Collections.Generic.HashSet[string]([System.StringComparer]::Ordinal)
    foreach ($node in @($CanonicalNodesDoc.nodes)) {
        $nodeId = [string]$node.id
        if (-not [string]::IsNullOrWhiteSpace($nodeId)) {
            $null = $nodeIds.Add($nodeId.Trim())
        }
    }

    $explicit = @($ExplicitEntrypoints | ForEach-Object { [string]$_ } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_.Trim() } | Sort-Object -Unique)
    if ($explicit.Count -gt 0) {
        $missing = @($explicit | Where-Object { -not $nodeIds.Contains($_) })
        if ($missing.Count -gt 0) {
            throw "Explicit entrypoints not found in canonical nodes: $($missing -join ', ')"
        }

        return [ordered]@{
            source = 'EXPLICIT'
            entrypoints = $explicit
        }
    }

    $resolved = New-Object System.Collections.Generic.List[string]
    foreach ($node in @($CanonicalNodesDoc.nodes)) {
        $nodeId = [string]$node.id
        if ([string]::IsNullOrWhiteSpace($nodeId)) {
            continue
        }
        $nodeId = $nodeId.Trim()

        $isEntrypoint = $false
        if ($node.PSObject.Properties.Name -contains 'is_entrypoint') {
            try {
                if ([bool]$node.is_entrypoint) { $isEntrypoint = $true }
            }
            catch { }
        }

        if (-not $isEntrypoint -and $node.PSObject.Properties.Name -contains 'role') {
            $role = [string]$node.role
            if ($role -match '(?i)entrypoint') {
                $isEntrypoint = $true
            }
        }

        if (-not $isEntrypoint -and $node.PSObject.Properties.Name -contains 'tags' -and $null -ne $node.tags) {
            foreach ($tag in @($node.tags)) {
                if (([string]$tag) -match '^(?i)ENTRYPOINT$') {
                    $isEntrypoint = $true
                    break
                }
            }
        }

        if ($isEntrypoint) {
            $resolved.Add($nodeId) | Out-Null
        }
    }

    if ($resolved.Count -eq 0) {
        foreach ($node in @($CanonicalNodesDoc.nodes)) {
            $nodeId = [string]$node.id
            $filePath = [string]$node.file_path
            if ([string]::IsNullOrWhiteSpace($nodeId) -or [string]::IsNullOrWhiteSpace($filePath)) {
                continue
            }

            $leaf = [System.IO.Path]::GetFileName($filePath).ToLowerInvariant()
            if ($leaf -in @('main.py', '__main__.py', 'app.py', 'index.py', 'run.py', 'run.ps1', 'program.cs', 'startup.cs')) {
                $resolved.Add($nodeId.Trim()) | Out-Null
            }
        }
    }

    $finalEntrypoints = @($resolved.ToArray() | Sort-Object -Unique)
    if ($finalEntrypoints.Count -eq 0) {
        throw 'No entrypoints resolved. Set explicit entrypoints or annotate canonical nodes with entrypoint metadata.'
    }

    return [ordered]@{
        source = if ($explicit.Count -gt 0) { 'EXPLICIT' } elseif ($resolved.Count -gt 0) { 'CANONICAL_METADATA_OR_FILENAME' } else { 'UNRESOLVED' }
        entrypoints = $finalEntrypoints
    }
}
