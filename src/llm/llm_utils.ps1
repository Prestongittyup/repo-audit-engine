# LLM Utilities - Strict Prompting and Safety Layer
# This module provides safe, auditable LLM interaction for post-processing static analysis
# 
# CRITICAL RULES:
# - LLM only receives structured JSON (no raw source code)
# - LLM output is advisory, never overrides static scores
# - All LLM calls include explicit guardrails in system prompt
# - Fail-safe: if LLM unavailable, system completes with static-only results

param()

# Feature flags for LLM layers
$script:EnableSemanticLayer = $false
$script:EnableDecisionLayer = $false

# LLM configuration
$script:LLMProvider = "openai"  # Can extend to other providers
$script:LLMModel = "gpt-4-mini"
$script:LLMEndpoint = $null
$script:LLMTimeout = 30  # seconds

function Set-LLMMode {
    <#
    .SYNOPSIS
    Configure which LLM layers are enabled
    
    .PARAMETER Mode
    static = only deterministic engine (no LLM)
    semantic = static + semantic layer
    full = static + semantic + decision layer
    #>
    param(
        [ValidateSet('static', 'semantic', 'full')][string]$Mode = 'static'
    )

    switch ($Mode) {
        'static' {
            $script:EnableSemanticLayer = $false
            $script:EnableDecisionLayer = $false
        }
        'semantic' {
            $script:EnableSemanticLayer = $true
            $script:EnableDecisionLayer = $false
        }
        'full' {
            $script:EnableSemanticLayer = $true
            $script:EnableDecisionLayer = $true
        }
    }
}

function Get-LLMMode {
    if ($script:EnableDecisionLayer) { return 'full' }
    if ($script:EnableSemanticLayer) { return 'semantic' }
    return 'static'
}

function Test-LLMAvailable {
    <#
    .SYNOPSIS
    Check if LLM service is available (without blocking)
    
    .OUTPUTS
    $true if available, $false if not
    #>
    
    if (-not $script:LLMEndpoint) {
        # Try to detect from environment or config
        $script:LLMEndpoint = $env:OPENAI_API_KEY -or $env:AZURE_OPENAI_ENDPOINT
    }
    
    if (-not $script:LLMEndpoint) {
        return $false
    }
    
    try {
        # Quick connectivity check (non-blocking)
        $testPayload = @{
            model = $script:LLMModel
            messages = @(
                @{ role = "user"; content = "test" }
            )
            max_tokens = 1
        } | ConvertTo-Json
        
        # This is a placeholder - actual implementation depends on LLM provider
        return $true
    }
    catch {
        return $false
    }
}

function Invoke-LLMWithGuardrails {
    <#
    .SYNOPSIS
    Safely invoke LLM with strict system prompt guardrails
    
    .PARAMETER SystemPrompt
    The system instructions (includes guardrails)
    
    .PARAMETER UserMessage
    The user/analysis question
    
    .PARAMETER MaxTokens
    Maximum response tokens (default 2000)
    
    .OUTPUTS
    LLM response or $null if failed/unavailable
    #>
    param(
        [string]$SystemPrompt,
        [string]$UserMessage,
        [int]$MaxTokens = 2000
    )
    
    # Guardrails embedded in system prompt (non-negotiable)
    $guardrails = @"
CRITICAL RULES FOR THIS INTERACTION:

1. You are analyzing structured static analysis output ONLY
2. You MUST NOT:
   - Assume code content beyond provided data
   - Invent files or dependencies not in the data
   - Override or contradict static analysis scores
   - Reference source code files directly
   
3. You MUST:
   - Reason ONLY from provided JSON structures
   - Cite metrics and data points as justification
   - Include confidence scores (0-100) in recommendations
   - Flag assumptions explicitly
   
4. If data is insufficient, state it clearly instead of speculating

================================
USER CONTEXT (NON-NEGOTIABLE):
================================
"@

    $fullSystemPrompt = $guardrails + "`n" + $SystemPrompt
    
    try {
        # Placeholder for actual LLM API call
        # In production, this would call OpenAI, AzureOpenAI, etc.
        
        $payload = @{
            model = $script:LLMModel
            messages = @(
                @{ role = "system"; content = $fullSystemPrompt }
                @{ role = "user"; content = $UserMessage }
            )
            max_tokens = $MaxTokens
            temperature = 0.3  # Lower temperature for more deterministic output
        } | ConvertTo-Json -Depth 10
        
        # STUB: Replace with actual provider call
        Write-Verbose "LLM prompt length: $($fullSystemPrompt.Length) chars"
        Write-Verbose "User message tokens: ~$($UserMessage.Length / 4) tokens"
        
        # For now, return null (fail-safe)
        return $null
        
    }
    catch {
        Write-Warning "LLM invocation failed: $_"
        return $null
    }
}

function Format-StaticDataForLLM {
    <#
    .SYNOPSIS
    Safely extract and format static analysis outputs for LLM consumption
    
    .PARAMETER DependencyGraph
    dependency_truth_graph.json content
    
    .PARAMETER ArchitectureAnalysis
    architecture_analysis.json content
    
    .PARAMETER HealthScore
    system_health_score.json content
    
    .OUTPUTS
    Formatted summary safe for LLM analysis
    #>
    param(
        [object]$DependencyGraph,
        [object]$ArchitectureAnalysis,
        [object]$HealthScore
    )
    
    $summary = @{
        files_indexed = 0
        dependencies = @()
        violations = @()
        health_metrics = @()
    }
    
    # Extract only metrics, never raw code
    if ($DependencyGraph) {
        $summary.files_indexed = $DependencyGraph.data.nodes.Count
        $summary.dependencies = @($DependencyGraph.data.edges | Select-Object -Property source, target, weight)
    }
    
    if ($ArchitectureAnalysis) {
        $summary.violations = @($ArchitectureAnalysis.data.violations | Select-Object -Property type, severity, description)
    }
    
    if ($HealthScore) {
        $summary.health_metrics = @{
            cohesion = $HealthScore.data.cohesion
            coupling = $HealthScore.data.coupling
            modularity = $HealthScore.data.modularity
            risk_level = $HealthScore.data.risk_level
        }
    }
    
    return $summary | ConvertTo-Json -Depth 10
}

function Write-LLMTrace {
    <#
    .SYNOPSIS
    Write audit trail of LLM interaction for transparency
    
    .PARAMETER Layer
    semantic or decision
    
    .PARAMETER Input
    LLM input (for audit)
    
    .PARAMETER Output
    LLM output (for audit)
    
    .PARAMETER TraceFile
    File to append trace to
    #>
    param(
        [string]$Layer,
        [string]$Input,
        [string]$Output,
        [string]$TraceFile
    )
    
    $trace = @{
        timestamp = Get-Date -Format "o"
        layer = $Layer
        input_length = $Input.Length
        output_length = $Output.Length
        input_hash = (Get-StringHash $Input -Algorithm SHA256 -Length 12)
        output_hash = (Get-StringHash $Output -Algorithm SHA256 -Length 12)
    } | ConvertTo-Json
    
    Add-Content -Path $TraceFile -Value $trace
}


