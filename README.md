# Repo Audit Engine

Deterministic repository audit engine with a strict pipeline control plane.

## What This Enforces

1. Single state model only.
Every stage performs a deterministic transition from SystemState vN to SystemState vN+1.

2. One executable path.
The orchestration pipeline is the only supported CLI execution path.

3. Centralized trust.
Trust is computed once from the final SystemState, not incrementally across layers.

4. Centralized entrypoint resolution.
Entrypoints are resolved by one module only.

5. Single report emitter.
Only one final report is emitted for each successful run.

## CLI Surface

The router intentionally exposes only:

- status
- init
- run-pipeline

### Standard Run

```powershell
.\run.ps1 run-pipeline -RepoPath <repoRoot>
```

### Run With Explicit Options

```powershell
.\run.ps1 run-pipeline `
    -RepoPath <repoRoot> `
    -OutputPath <runDir> `
    -Entrypoints <canonicalId1>,<canonicalId2> `
    -HeuristicOnlyThreshold 0 `
    -DriftThreshold 0.0
```

Direct stage commands are intentionally blocked in the router.

## Output Contract

Each successful pipeline run writes exactly two public artifacts in the run directory:

- system_state.json
- final_report.json

No layer-level final report artifacts are emitted.

## Core Control-Plane Modules

- src/pipeline_control_plane.ps1
Pipeline orchestrator and stage transitions.

- src/system_state.ps1
SystemState contract, versioning, transition helper, and persistence.

- src/entrypoint_resolver.ps1
Canonical entrypoint resolution in one place.

- src/trust_from_state.ps1
Single trust function based on the final SystemState.

- src/final_report_emitter.ps1
Single final report writer.

## Entrypoint Resolution Policy

Entrypoints are resolved in src/entrypoint_resolver.ps1 only, using this order:

1. Explicit entrypoints provided to run-pipeline.
2. Canonical node metadata (is_entrypoint, role, tags).

No fallback inference is permitted in downstream semantic, query, or authority layers.

## Trust Policy

Trust is computed once, at the end of orchestration:

- Trust = function(final SystemState)
- Implementation: src/trust_from_state.ps1

## Validation Workflow

### Smoke Test

```powershell
.\run.ps1 run-pipeline -RepoPath .\.tmp\authority_pass_repo -OutputPath .\output\runs\smoke_state_model -DebugMode
```

Expected result:

- command returns SUCCESS
- output\runs\smoke_state_model contains only final_report.json and system_state.json

### Deterministic Stress Harness

```powershell
python .\src\deterministic_stress_harness.py
```

Report output:

- output\deterministic_stress_harness_report.json

## What Is Intentionally Disabled

- Direct CLI execution of internal graph/resolver/semantic/trust stage scripts through run.ps1.
- Layer-level report emitters.
- Parallel JSON state models outside SystemState.

## Python Phase 1 Validation And Diagnostics

The migration now includes a Python package entrypoint for deterministic validation and diagnostics:

- repo_audit_engine/cli.py
- repo_audit_engine/pipeline/validation.py
- repo_audit_engine/pipeline/diagnostics.py

### Validate Only (Layer5-Compatible Output)

```powershell
python .\repo_audit_engine\cli.py validate `
    --graph-path .\repo_audit_engine\examples\mock_graph.json `
    --resolver-path .\repo_audit_engine\examples\mock_resolver.json `
    --entrypoint canonical://service/App `
    --output .\output\phase1_validation.json `
    --pretty
```

### Combined Validation + Diagnostics

```powershell
python .\repo_audit_engine\cli.py analyze `
    --graph-path .\repo_audit_engine\examples\mock_graph.json `
    --resolver-path .\repo_audit_engine\examples\mock_resolver.json `
    --entrypoint canonical://service/App `
    --output .\output\phase1_analysis.json `
    --include-validation `
    --pretty
```

### Demo Command

```powershell
python .\repo_audit_engine\cli.py demo --output .\output\phase1_demo.json --pretty
```
