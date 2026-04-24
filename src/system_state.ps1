function New-SystemState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EngineRoot,
        [Parameter(Mandatory = $true)]
        [string]$RepoPath,
        [Parameter(Mandatory = $true)]
        [string]$OutputDir,
        [switch]$DebugMode
    )

    $startedUtc = [DateTime]::UtcNow
    return [ordered]@{
        schema = 'SystemState.v1'
        state_version = 0
        run = [ordered]@{
            started_utc = $startedUtc.ToString('o')
            ended_utc = $null
            engine_root = $EngineRoot
            repo_path = $RepoPath
            output_dir = $OutputDir
            debug_mode = [bool]$DebugMode
        }
        current_stage = $null
        artifacts = [ordered]@{
            system_state = ''
            final_report = ''
        }
        stages = @()
        data = [ordered]@{
            inventory = $null
            canonical_nodes = $null
            entrypoint_resolution = $null
            edges = $null
            unified_graph = $null
            graph_validation = $null
            graph_structural_validation = $null
            resolver_consistency = $null
            semantic_validation = $null
            authority_verdict = $null
            classification = $null
            diagnostic_synthesis = $null
            trust = $null
        }
        summary = [ordered]@{
            status = 'IN_PROGRESS'
            message = 'Pipeline started.'
            failed_stage = $null
        }
    }
}

function Save-SystemState {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$State,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    Write-JsonFile -Path $Path -Data $State -Depth 80
}

function Invoke-SystemStateTransition {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$State,
        [Parameter(Mandatory = $true)]
        [string]$StageName,
        [Parameter(Mandatory = $true)]
        [string]$StatePath,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    $startedUtc = [DateTime]::UtcNow
    $fromVersion = [int]$State.state_version

    # Persist stage-start metadata so long-running stages are observable while in flight.
    $State.current_stage = [ordered]@{
        stage = $StageName
        started_utc = $startedUtc.ToString('o')
        from_version = $fromVersion
    }
    $State.summary = [ordered]@{
        status = 'IN_PROGRESS'
        message = "Running stage: $StageName"
        failed_stage = $null
    }
    Save-SystemState -State $State -Path $StatePath

    try {
        $nextState = & $Action $State
        if ($null -eq $nextState) {
            throw "Stage '$StageName' returned null state."
        }

        $toVersion = $fromVersion + 1
        $nextState.state_version = $toVersion
        $endedUtc = [DateTime]::UtcNow

        $stageRecord = [ordered]@{
            stage = $StageName
            success = $true
            started_utc = $startedUtc.ToString('o')
            ended_utc = $endedUtc.ToString('o')
            duration_ms = [int]($endedUtc - $startedUtc).TotalMilliseconds
            from_version = $fromVersion
            to_version = $toVersion
            error = $null
        }

        $nextState.current_stage = $null
        $nextState.stages = @($nextState.stages + @($stageRecord))
        Save-SystemState -State $nextState -Path $StatePath
        return $nextState
    }
    catch {
        $endedUtc = [DateTime]::UtcNow
        $failureRecord = [ordered]@{
            stage = $StageName
            success = $false
            started_utc = $startedUtc.ToString('o')
            ended_utc = $endedUtc.ToString('o')
            duration_ms = [int]($endedUtc - $startedUtc).TotalMilliseconds
            from_version = $fromVersion
            to_version = $fromVersion
            error = $_.Exception.Message
        }

        $State.current_stage = $null
        $State.stages = @($State.stages + @($failureRecord))
        $State.summary = [ordered]@{
            status = 'FAILED'
            message = $_.Exception.Message
            failed_stage = $StageName
        }
        Save-SystemState -State $State -Path $StatePath
        throw
    }
}
