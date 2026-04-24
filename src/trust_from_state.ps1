function Get-TrustFromSystemState {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
    )

    if ($null -eq $State -or $null -eq $State.data) {
        throw 'SystemState is missing data payload.'
    }

    $data = $State.data

    $authority = $data.authority_verdict
    $diagnostic = $data.diagnostic_synthesis
    $verification = $null
    if ($null -ne $authority -and $authority.PSObject.Properties.Name -contains 'verification') {
        $verification = $authority.verification
    }

    $criticalFailure = $false
    $systemValid = $false
    $trustScore = 0.0
    $failureDomains = @()
    $penalties = [ordered]@{}
    $failureAnalysis = [ordered]@{}
    $policyDecision = [ordered]@{}

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

    if ($null -ne $verification) {
        if ($verification.PSObject.Properties.Name -contains 'critical_failure') {
            $criticalFailure = [bool]$verification.critical_failure
        }

        if ($verification.PSObject.Properties.Name -contains 'policy_decision' -and $null -ne $verification.policy_decision) {
            $policyDecision = $verification.policy_decision
            if ($policyDecision.PSObject.Properties.Name -contains 'critical_failure') {
                $criticalFailure = [bool]$policyDecision.critical_failure
            }
            if ($policyDecision.PSObject.Properties.Name -contains 'system_valid') {
                $systemValid = [bool]$policyDecision.system_valid
            }
        }

        if ($verification.PSObject.Properties.Name -contains 'trust' -and $null -ne $verification.trust) {
            $trustBlock = $verification.trust
            if ($trustBlock.PSObject.Properties.Name -contains 'penalties' -and $null -ne $trustBlock.penalties) {
                $penalties = $trustBlock.penalties
            }
            if ($trustBlock.PSObject.Properties.Name -contains 'scores' -and $null -ne $trustBlock.scores) {
                foreach ($k in @('structural_integrity', 'dependency_consistency', 'topology_validation', 'semantic_observations', 'structural', 'reachability', 'resolver', 'semantic', 'trust')) {
                    if ($trustBlock.scores.PSObject.Properties.Name -contains $k) {
                        $scores[$k] = [double]$trustBlock.scores.$k
                    }
                }
            }
        }

        if ($verification.PSObject.Properties.Name -contains 'scores' -and $null -ne $verification.scores) {
            foreach ($k in @('structural_integrity', 'dependency_consistency', 'topology_validation', 'semantic_observations', 'structural', 'reachability', 'resolver', 'semantic', 'trust')) {
                if ($verification.scores.PSObject.Properties.Name -contains $k) {
                    $scores[$k] = [double]$verification.scores.$k
                }
            }
        }

        if ($verification.PSObject.Properties.Name -contains 'trust_score') {
            $trustScore = [double]$verification.trust_score
            $scores.trust = $trustScore
        }
        else {
            $trustScore = [double]$scores.trust
        }

        if ($verification.PSObject.Properties.Name -contains 'failure_domains' -and $null -ne $verification.failure_domains) {
            $failureDomains = @($verification.failure_domains | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        }

        if ($verification.PSObject.Properties.Name -contains 'system_valid') {
            $systemValid = [bool]$verification.system_valid
        }

        if ($verification.PSObject.Properties.Name -contains 'failure_analysis' -and $null -ne $verification.failure_analysis) {
            $failureAnalysis = $verification.failure_analysis
        }
    }

    if ($null -eq $verification) {
        $authorityValid = ($null -ne $authority -and $authority.PSObject.Properties.Name -contains 'authority_valid' -and [bool]$authority.authority_valid)
        $graphStatus = if ($null -ne $data.graph_validation -and $data.graph_validation.PSObject.Properties.Name -contains 'status') { [string]$data.graph_validation.status } else { '' }
        $isGraphCritical = $graphStatus -eq 'INVALID_STRUCTURAL'

        if (-not $authorityValid) { $failureDomains += 'authority_gate' }
        if ($isGraphCritical) { $failureDomains += 'graph_structural' }

        $criticalFailure = $isGraphCritical
        $trustScore = if ($authorityValid -and -not $isGraphCritical) { 0.55 } else { 0.35 }
        $scores.trust = [double]$trustScore
        $scores.structural_integrity = $scores.trust
        $scores.dependency_consistency = $scores.trust
        $scores.topology_validation = $scores.trust
        $scores.semantic_observations = $scores.trust
        $systemValid = (-not $criticalFailure -and $trustScore -ge 0.50)
    }

    if (($null -eq $failureAnalysis -or $failureAnalysis.PSObject.Properties.Count -eq 0) -and $null -ne $diagnostic) {
        $primary = ''
        $ranked = @()
        $chain = @()

        if ($diagnostic.PSObject.Properties.Name -contains 'root_causes' -and $null -ne $diagnostic.root_causes) {
            $roots = @($diagnostic.root_causes)
            if ($roots.Count -gt 0) {
                $first = $roots[0]
                $primary = "{0}:{1}" -f [string]$first.domain, [string]$first.description
            }

            $ranked = @(
                $roots |
                    ForEach-Object {
                        [ordered]@{
                            domain = [string]$_.domain
                            reason = [string]$_.description
                            impact_score = [double]$_.impact_score
                        }
                    }
            )

            if ($failureDomains.Count -eq 0) {
                $failureDomains = @(
                    $roots |
                        ForEach-Object { [string]$_.domain } |
                        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
                        Sort-Object -Unique
                )
            }
        }

        if ($diagnostic.PSObject.Properties.Name -contains 'causal_chains' -and $null -ne $diagnostic.causal_chains) {
            $chain = @($diagnostic.causal_chains)
        }

        $failureAnalysis = [ordered]@{
            primary_cause = $primary
            ranked_causes = $ranked
            causal_chain = $chain
            source = 'diagnostic_synthesis_layer'
        }
    }

    if (-not $systemValid) {
        # Keep continuous trust scoring even when policy is invalid.
        $trustScore = [Math]::Max(0.0, [Math]::Min(1.0, [double]$trustScore))
        $scores.trust = [double]$trustScore
    }

    return [ordered]@{
        trust_model = 'TRUST_FROM_SYSTEM_STATE_WEIGHTED'
        critical_failure = [bool]$criticalFailure
        system_valid = $systemValid
        trust_score = [double]$trustScore
        failure_domains = @($failureDomains | Sort-Object -Unique)
        scores = $scores
        penalties = $penalties
        failure_analysis = $failureAnalysis
        policy_decision = $policyDecision
    }
}
