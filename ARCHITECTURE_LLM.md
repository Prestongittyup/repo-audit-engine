# Repository Audit Engine - Layered LLM Architecture

## Overview

The audit engine now features a **layered architecture** with clear separation between:

1. **Static Core** (always runs, deterministic, no LLM)
2. **Semantic Layer** (optional, LLM-powered interpretation)
3. **Decision Layer** (optional, LLM-powered prioritization)

This design ensures **analysis correctness is never compromised** while optionally adding natural language interpretation and prioritization.

---

## Architecture Principle

```
┌─────────────────────────────────────────────────────┐
│       STATIC DETERMINISTIC PIPELINE                 │
│  (dependency graph, risk scores, health metrics)    │
│       ALWAYS EXECUTES, NEVER MODIFIED               │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│    OPTIONAL SEMANTIC LAYER (MODE: semantic/full)    │
│  (natural language interpretation of outputs)       │
│  NEVER overrides static scores, only interprets     │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│     OPTIONAL DECISION LAYER (MODE: full)            │
│  (prioritization & recommendations from metrics)    │
│  NEVER marks files as delete, only suggests review  │
└─────────────────────────────────────────────────────┘
```

---

## Execution Modes

### Mode: `static` (DEFAULT)

- ✅ Run deterministic static analysis pipeline
- ✅ Produce dependency graph, risk scores, health metrics
- ❌ No LLM layers
- **Use case:** Pure deterministic analysis, no external dependencies

```powershell
.\run.ps1 "C:\repo"  # -Mode static (default)
```

### Mode: `semantic`

- ✅ Run static pipeline
- ✅ Run semantic layer (interpret outputs for humans)
- ❌ No decision layer
- **Output:** `semantic_summary.md`, `architecture_narrative.json`, `system_overview.json`
- **Use case:** Get human-readable system understanding from metrics

```powershell
.\run.ps1 "C:\repo" -Mode semantic
```

### Mode: `full`

- ✅ Run static pipeline
- ✅ Run semantic layer
- ✅ Run decision layer (prioritize refactoring)
- **Output:** All semantic files + `refactor_priorities.json`, `risk_ranking.json`, `architecture_recommendations.md`
- **Use case:** Get actionable recommendations prioritized by impact

```powershell
.\run.ps1 "C:\repo" -Mode full
```

---

## Static Core Pipeline (Unchanged)

Phase 1-4 remain completely deterministic:

1. **Phase 1: Indexer** → `index.json`, `manifest.json`
2. **Phase 2: Batch Analyzer** → `audit_log.jsonl`
3. **Phase 3: Config Truth** → `config_dependencies.json`
4. **Phase 4: Truth Engine** → dependency graph, architecture, health, contradictions

**Guarantee:** Same repo state always produces identical static outputs.

---

## Semantic Layer Module

**File:** `src/llm/semantic_summarizer.ps1`

### Responsibilities

- Read static JSON outputs (no raw code)
- Infer system purpose and architecture style
- Generate human-readable narratives
- Explain component relationships
- Summarize risk areas

### Key Properties

- **Non-invasive:** Reads outputs, never modifies them
- **Deterministic reasoning:** Infers from metrics, no LLM hallucination
- **Safe fallback:** If unavailable, system completes with static-only results

### Outputs

```
<RUN_DIR>/semantic/
  ├── semantic_summary.md           # Human-readable markdown
  ├── architecture_narrative.json   # System purpose, style, components
  └── system_overview.json          # High-level metrics summary
```

### Example Output

```json
{
  "system_purpose": "Analyzed system with 245 components",
  "architecture_style": "hub-and-spoke",
  "core_components": [
    {
      "name": "api.service",
      "importance": 0.92,
      "inbound_count": 18,
      "outbound_count": 8
    }
  ],
  "risk_summary": "System risk level: medium. Cohesion: 0.72",
  "confidence": 92,
  "generated_at": "2026-04-24T10:15:30Z"
}
```

---

## Decision Layer Module

**File:** `src/llm/decision_layer.ps1`

### Responsibilities

- Rank files by refactoring priority
- Identify high-risk components
- Suggest improvement order
- Justify recommendations with metrics

### Key Properties

- **Confidence scores:** Every recommendation includes 0-100 confidence
- **No deletions:** Only suggests REVIEW, never DELETE
- **Data-driven:** All suggestions reference static metrics
- **Safe fallback:** If unavailable, system completes with static-only results

### Priority Levels

- **HIGH:** Files with high coupling AND high inbound dependencies (critical to architecture)
- **MEDIUM:** Moderately coupled files or circular dependencies
- **LOW:** Low-risk or isolated components

### Outputs

```
<RUN_DIR>/decisions/
  ├── refactor_priorities.json            # Ranked files by priority
  ├── risk_ranking.json                   # Risk items by severity
  └── architecture_recommendations.md     # Markdown recommendations
```

### Example Output

```json
{
  "priority": "HIGH",
  "file": "src/core/database.py",
  "confidence": 85,
  "reasons": [
    "High coupling (exports: 18) with high criticality (imports: 23)",
    "Critical hub component (imported by 23 files)"
  ],
  "metrics": {
    "inbound_dependencies": 23,
    "outbound_dependencies": 18,
    "importance_score": 0.88
  }
}
```

---

## LLM Utilities Module

**File:** `src/llm/llm_utils.ps1`

Provides safe, auditable LLM interaction:

### Functions

- `Set-LLMMode -Mode 'static'|'semantic'|'full'` — Configure execution mode
- `Get-LLMMode` — Get current execution mode
- `Test-LLMAvailable` — Check if LLM service is reachable
- `Invoke-LLMWithGuardrails` — Safe LLM calls with embedded guardrails
- `Format-StaticDataForLLM` — Extract only metrics (no code)
- `Write-LLMTrace` — Audit trail of LLM interactions

### Guardrails (Built-in)

Every LLM call includes strict system instructions:

```
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
```

---

## Feature Flag Integration

**File:** `src/runtime_common.ps1`

Added global configuration:

```powershell
$script:LLMMode = 'static'  # static | semantic | full
```

Functions:

- `Set-LLMMode -Mode $mode` — Set execution mode
- `Get-LLMMode` — Get current mode
- `Test-LLMConfigured` — Check if LLM is available

---

## Orchestration Flow

**File:** `run_runtime.ps1`

```powershell
# Phase 1-4: Static pipeline (always executes)
. $indexScript ...
. $batchScript ...
. $configScript ...
. $analyzerScript ...

# Optional: Semantic layer
IF mode == 'semantic' OR mode == 'full':
    . semantic_summarizer.ps1
    Invoke-SemanticSummarizer

# Optional: Decision layer
IF mode == 'full':
    . decision_layer.ps1
    Invoke-DecisionLayer

# Exit with issue-based code
exit 0 if clean, 1 if issues
```

---

## Fail-Safe Behavior

**Critical Guarantee:** System ALWAYS completes with static-only results.

```powershell
try {
    # LLM layer execution
}
catch {
    Write-AuditWarning "LLM layer failed (non-fatal): $_"
    # Continue - static results already preserved
}
```

If semantic layer crashes → static pipeline preserved, decision layer still runs  
If decision layer crashes → static pipeline preserved, user gets partial results  
If both crash → static-only results available, system exit successfully

---

## Usage Examples

### Static Analysis Only

```powershell
.\run.ps1 "C:\repo"
# Output: dependency_truth_graph.json, architecture_analysis.json, system_health_score.json
```

### Static + Semantic Interpretation

```powershell
.\run.ps1 "C:\repo" -Mode semantic
# Output: Above + semantic_summary.md, architecture_narrative.json
```

### Full Stack: Static + Interpretation + Prioritization

```powershell
.\run.ps1 "C:\repo" -Mode full
# Output: Above + refactor_priorities.json, risk_ranking.json, architecture_recommendations.md
```

### Combined with Existing Parameters

```powershell
.\run.ps1 "C:\repo" `
    -Mode full `
    -AuditWorkspacePath "D:\audits" `
    -RunId "prod_scan" `
    -CI_MODE
```

---

## Output Directory Structure

```
$AUDIT_WORKSPACE/
└── <repo_id>/
    └── <run_id>/
        ├── [static outputs]
        │   ├── dependency_truth_graph.json
        │   ├── architecture_analysis.json
        │   ├── dead_code_report.json
        │   ├── system_health_score.json
        │   └── run_metadata.json
        │
        ├── semantic/                          (if mode: semantic/full)
        │   ├── semantic_summary.md
        │   ├── architecture_narrative.json
        │   └── system_overview.json
        │
        └── decisions/                          (if mode: full)
            ├── refactor_priorities.json
            ├── risk_ranking.json
            └── architecture_recommendations.md
```

---

## Design Rationale

### Why Layered?

✅ **Separation of Concerns** — Static analysis (deterministic) ≠ interpretation (LLM)  
✅ **Fail-Safe** — If LLM fails, results aren't lost  
✅ **Progressive Enhancement** — Use as much or as little as needed  
✅ **No Compromise** — Static results are never modified  

### Why Optional?

✅ **External Dependencies** — LLM may not be available/configured  
✅ **Determinism** — Some users need 100% reproducibility  
✅ **Cost Control** — LLM calls have latency/cost implications  
✅ **Offline Mode** — Static mode works without network  

### Why Guardrails?

✅ **Prevent Hallucination** — Explicit rules prevent making up files/dependencies  
✅ **Audit Trail** — All LLM interactions are traceable  
✅ **Trust** — Users can verify LLM isn't overriding static scores  
✅ **Safety** — No "magic" that users can't understand  

---

## Future Extensions

This architecture supports future additions:

- **Custom Evaluators** — User-provided metrics in decision layer
- **Multi-Model Support** — Different LLM providers
- **Real-time Streaming** — Stream results as layers complete
- **Batch Evaluations** — Run multiple semantic/decision configurations
- **Caching** — Cache LLM responses for reproducibility
- **Tracing** — Full observability of LLM reasoning

---

## Non-Negotiable Principles

1. **Static core is inviolable** — Static outputs are never modified by LLM layers
2. **No raw code in LLM** — LLM only sees metrics and JSON structures
3. **Explicit guardrails** — Every LLM call includes safety rules
4. **Confidence scores** — Every LLM recommendation includes justification
5. **Fail-safe degradation** — Missing LLM doesn't break static results
6. **User control** — User explicitly enables/disables LLM layers
7. **Transparent audit** — All LLM interactions are logged

---

**Engine Version:** 1.0.0  
**Architecture Version:** Layered (Static + Optional LLM)  
**Last Updated:** 2026-04-24
