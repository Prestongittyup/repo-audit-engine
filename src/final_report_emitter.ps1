function Write-FinalReportFromSystemState {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [Parameter(Mandatory = $true)]
        [string]$OutputPath
    )

    if ($null -eq $State -or $null -eq $State.data) {
        throw 'SystemState is missing data payload.'
    }

    function Test-Field {
        param(
            [object]$Object,
            [string]$Name
        )

        if ($null -eq $Object) { return $false }
        if ($Object -is [System.Collections.IDictionary]) {
            return $Object.Contains($Name)
        }

        return ($Object.PSObject.Properties.Name -contains $Name)
    }

    function Get-Field {
        param(
            [object]$Object,
            [string]$Name,
            [object]$Default = $null
        )

        if (-not (Test-Field -Object $Object -Name $Name)) {
            return $Default
        }

        if ($Object -is [System.Collections.IDictionary]) {
            return $Object[$Name]
        }

        return $Object.$Name
    }

    $data = $State.data

    $graph = $null
    $unifiedGraph = Get-Field -Object $data -Name 'unified_graph'
    if ($null -ne $unifiedGraph) {
        $graph = Get-Field -Object $unifiedGraph -Name 'graph' -Default $unifiedGraph
    }

    $nodes = @()
    $edges = @()
    if ($null -ne $graph) {
        $nodes = @((Get-Field -Object $graph -Name 'nodes' -Default @()))
        $edges = @((Get-Field -Object $graph -Name 'edges' -Default @()))
    }

    $classificationBlock = [ordered]@{
        REACHABLE = @()
        REFERENCED = @()
        ISOLATED = @()
        SUSPICIOUS = @()
        DEAD = @()
        EXEMPT = @()
    }

    $classification = Get-Field -Object $data -Name 'classification'
    $classificationRoot = Get-Field -Object $classification -Name 'classification'
    if ($null -ne $classificationRoot) {
        foreach ($key in @('REACHABLE', 'REFERENCED', 'ISOLATED', 'SUSPICIOUS', 'DEAD', 'EXEMPT')) {
            $classificationBlock[$key] = @((Get-Field -Object $classificationRoot -Name $key -Default @()))
        }
    }

    $authority = Get-Field -Object $data -Name 'authority_verdict'
    $verification = Get-Field -Object $authority -Name 'verification'
    $diagnosticSynthesis = Get-Field -Object $data -Name 'diagnostic_synthesis'

    $metrics = [ordered]@{
        node_count = $nodes.Count
        edge_count = $edges.Count
    }

    $scores = [ordered]@{
        structural_integrity = 0.0
        dependency_consistency = 0.0
        topology_validation = 0.0
        semantic_observations = 0.0
        structural = 0.0
        reachability = 0.0
        resolver = 0.0
        semantic = 0.0
        trust = 0.0
    }

    $issues = @()
    $warnings = @()
    $recommendations = @()
    $criticalFailure = $false
    $failureAnalysis = [ordered]@{}
    $policyDecision = [ordered]@{}
    $penalties = [ordered]@{}
    $validationFacts = [ordered]@{}

    $verificationMetrics = Get-Field -Object $verification -Name 'metrics'
    if ($null -ne $verificationMetrics) {
        foreach ($p in $verificationMetrics.PSObject.Properties) {
            $metrics[$p.Name] = $p.Value
        }
    }

    $verificationScores = Get-Field -Object $verification -Name 'scores'
    if ($null -ne $verificationScores) {
        foreach ($key in @('structural_integrity', 'dependency_consistency', 'topology_validation', 'semantic_observations', 'structural', 'reachability', 'resolver', 'semantic', 'trust')) {
            if (Test-Field -Object $verificationScores -Name $key) {
                $scores[$key] = [double](Get-Field -Object $verificationScores -Name $key)
            }
        }
    }

    $issues = @((Get-Field -Object $verification -Name 'issues' -Default @()))
    $warnings = @((Get-Field -Object $verification -Name 'warnings' -Default @()))
    $recommendations = @((Get-Field -Object $verification -Name 'recommendations' -Default @()))
    $criticalFailure = [bool](Get-Field -Object $verification -Name 'critical_failure' -Default $false)
    $failureAnalysis = Get-Field -Object $verification -Name 'failure_analysis' -Default [ordered]@{}
    $policyDecision = Get-Field -Object $verification -Name 'policy_decision' -Default [ordered]@{}
    $validationFacts = Get-Field -Object $verification -Name 'validation_facts' -Default [ordered]@{}

    $trust = Get-Field -Object $data -Name 'trust'
    if ($null -ne $trust) {
        if (Test-Field -Object $trust -Name 'trust_score') {
            $scores.trust = [double](Get-Field -Object $trust -Name 'trust_score' -Default 0.0)
        }

        $trustScores = Get-Field -Object $trust -Name 'scores'
        if ($null -ne $trustScores) {
            foreach ($key in @('structural_integrity', 'dependency_consistency', 'topology_validation', 'semantic_observations', 'structural', 'reachability', 'resolver', 'semantic', 'trust')) {
                if (Test-Field -Object $trustScores -Name $key) {
                    $scores[$key] = [double](Get-Field -Object $trustScores -Name $key)
                }
            }
        }

        $trustPenalties = Get-Field -Object $trust -Name 'penalties'
        if ($null -ne $trustPenalties) {
            $penalties = $trustPenalties
        }

        $trustFailureAnalysis = Get-Field -Object $trust -Name 'failure_analysis'
        if ($null -ne $trustFailureAnalysis -and $trustFailureAnalysis.PSObject.Properties.Count -gt 0) {
            $failureAnalysis = $trustFailureAnalysis
        }

        $trustPolicyDecision = Get-Field -Object $trust -Name 'policy_decision'
        if ($null -ne $trustPolicyDecision -and $trustPolicyDecision.PSObject.Properties.Count -gt 0) {
            $policyDecision = $trustPolicyDecision
        }

        if (Test-Field -Object $trust -Name 'critical_failure') {
            $criticalFailure = [bool](Get-Field -Object $trust -Name 'critical_failure' -Default $criticalFailure)
        }
    }

    if ($penalties.PSObject.Properties.Count -eq 0) {
        $verificationTrust = Get-Field -Object $verification -Name 'trust'
        if ($null -ne $verificationTrust) {
            $verificationPenalties = Get-Field -Object $verificationTrust -Name 'penalties'
            if ($null -ne $verificationPenalties) {
                $penalties = $verificationPenalties
            }
        }
    }

    $entrypointResolution = Get-Field -Object $data -Name 'entrypoint_resolution'
    $entrypoints = @((Get-Field -Object $entrypointResolution -Name 'entrypoints' -Default @()))

    $summaryStatus = [string](Get-Field -Object $State.summary -Name 'status' -Default 'IN_PROGRESS')
    $summaryMessage = [string](Get-Field -Object $State.summary -Name 'message' -Default 'Pipeline started.')

    $finalReport = [ordered]@{
        metrics = $metrics
        scores = $scores
        issues = $issues
        critical_failure = [bool]$criticalFailure
        warnings = $warnings
        recommendations = $recommendations
        trust_model = [ordered]@{
            penalties = $penalties
            failure_analysis = $failureAnalysis
            policy_decision = $policyDecision
        }
        summary = [ordered]@{
            status = $summaryStatus
            message = $summaryMessage
            authority_valid = [bool](Get-Field -Object $authority -Name 'authority_valid' -Default $false)
            system_valid = [bool](Get-Field -Object $trust -Name 'system_valid' -Default $false)
        }
        context = [ordered]@{
            report_version = 'FINAL_REPORT.v2'
            generated_utc = [DateTime]::UtcNow.ToString('o')
            repo_path = [string](Get-Field -Object $State.run -Name 'repo_path' -Default '')
            system_state_version = [int](Get-Field -Object $State -Name 'state_version' -Default 0)
            entrypoints = $entrypoints
        }
        classification = $classificationBlock
        diagnostic_synthesis = $diagnosticSynthesis
        validation_facts = $validationFacts
    }

    Write-JsonFile -Path $OutputPath -Data $finalReport -Depth 80
    return $finalReport
}
