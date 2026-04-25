# Repo Audit Engine

Deterministic repository audit engine with a Python-first execution pipeline.

The project combines static graph construction, sandboxed runtime evidence, deterministic classification, and a strict verification policy to produce a trust score and a binary `system_valid` verdict.

## Stage Pipeline

The staged orchestrator runs in this order:

1. `manifest`
2. `static`
3. `graph`
4. `bubble`
5. `classification`
6. `verification`
7. `diagnostics`
8. `report`

Source of truth:

- `repo_audit_engine/pipeline/stages.py`
- `repo_audit_engine/pipeline/orchestrator.py`

## CLI Overview

Run commands with:

```bash
python -m repo_audit_engine <command> [options]
```

Available commands:

- `run-pipeline`
- `run`
- `validate`
- `analyze`
- `demo`

### `run-pipeline` (full contract + artifacts)

```bash
python -m repo_audit_engine run-pipeline \
    --repo <repoRoot> \
    --output output/contract.json \
    --bubble-mode true
```

Behavior:

- Writes compact contract JSON to `--output`.
- Writes full staged artifacts to `<output_stem>_artifacts/`.

### `run` (staged artifact execution)

```bash
python -m repo_audit_engine run \
    --repo <repoRoot> \
    --output output/full_run \
    --mode full-pipeline \
    --bubble-mode true
```

`--mode` options:

- `manifest-only`
- `static-only` (alias: `static-analysis`)
- `bubble-run`
- `full-pipeline`

### `validate` (verification only)

```bash
python -m repo_audit_engine validate \
    --graph-path repo_audit_engine/examples/mock_graph.json \
    --resolver-path repo_audit_engine/examples/mock_resolver.json \
    --entrypoint canonical://service/App \
    --output output/validation.json \
    --pretty
```

### `analyze` (verification + diagnostics)

```bash
python -m repo_audit_engine analyze \
    --graph-path repo_audit_engine/examples/mock_graph.json \
    --resolver-path repo_audit_engine/examples/mock_resolver.json \
    --entrypoint canonical://service/App \
    --output output/analysis.json \
    --include-validation \
    --pretty
```

### `demo`

```bash
python -m repo_audit_engine demo --output output/demo.json --pretty
```

## Runtime Bubble and Scenario Planning

Bubble mode executes selected entrypoints in a sandbox and captures runtime evidence.

- Runtime trace events are written to `runtime_trace.jsonl`.
- Runtime call graph is written to `execution_flow_graph.json`.
- A deterministic scenario plan is generated at `runtime_scenario_plan.json`.
- Scenario-plan quality checks are fed into verification through `runtime_scenarios` and `scenario_validation` execution evidence.

Runtime evidence supplements static evidence; it does not replace static graph integrity requirements.

## Verification Policy (Current Contract)

Verification is fail-closed and policy-driven, with explicit thresholds and surfaced reasons.

### Core hard thresholds

- Runtime coverage hard floor: `0.30`
- Entrypoint coverage completeness hard threshold: `0.30`
- Domain coverage completeness soft threshold: `0.34`
- Scenario coverage completeness soft threshold: `0.55`
- AST/DI architecture-drift threshold: `0.50`

### Trust model updates

- Coverage hard gate applies a trust multiplier of `0.50` when coverage is below `0.30`.
- Runtime authority weighting is applied when runtime signal exists:
  - `call_frequency_score` (50%)
  - `path_centrality_score` (30%)
  - `scenario_importance_score` (20%)
- `authority_adjusted_execution_confidence = min(execution_confidence, runtime_authority_score)`.
- AST/DI divergence above `0.50` triggers architecture drift and a trust penalty of `0.20`.

### First-class drift handling

- Dependency consistency escalates high resolver divergence with `AST_DI_DIVERGENCE_ESCALATED`.
- Policy includes architecture drift as an explicit soft-fail reason.
- Failure analysis promotes drift to `failure_domains: ["architecture_drift", ...]` when triggered.

### Coverage completeness gates

Execution-confidence layer now emits explicit issues when runtime signal is present:

- `ENTRYPOINT_COVERAGE_INCOMPLETE`
- `DOMAIN_COVERAGE_INCOMPLETE`
- `SCENARIO_COVERAGE_INCOMPLETE`

The policy layer hard-fails on coverage hard-gate and entrypoint hard-threshold violations, and soft-fails on domain/scenario completeness shortfalls.

## Classification and Dead-Code Guardrails

The classification stage uses deterministic evidence fusion and guardrails.

### Evidence scoring

Heat score is computed with weighted components:

- Runtime signal weight: `0.60`
- Reachability weight: `0.25`
- Reference/import weight: `0.15`

Thresholds:

- `HOT >= 0.8`
- `WARM >= 0.3`
- `COLD >= 0.1`
- `DEAD < 0.1`

### Hard consistency guards

- Nodes classified as `DEAD` are reclassified to `COLD` when contradictory evidence exists.
- `DEAD` with inbound edges is blocked by `dead_inbound_edge_hard_guardrail`.
- Runtime validation output includes `entrypoints`, `executed_entrypoints`, and `executed_entrypoint_count`.

### Dead code report defense-in-depth

Dead-code report generation independently enforces the same invariant:

- `DEAD` + inbound edges is reclassified to `COLD`.
- Guardrail annotation is emitted as `dead_reclassified_to_cold_due_to_inbound_edges`.

## Artifacts

For full staged runs, key artifacts include:

- `manifest.jsonl`
- `manifest_summary.json`
- `static_analysis.jsonl`
- `static_analysis_summary.json`
- `dependency_graph.json`
- `dependency_graph_summary.json`
- `runtime_scenario_plan.json`
- `runtime_trace.jsonl`
- `execution_flow_graph.json`
- `heat_classification.json`
- `dead_code_report.json`
- `architecture_constraints.json`
- `semantic_clusters.json`
- `causal_flow_report.json`
- `validation_result.json`
- `final_report.json`
- `pipeline_events.jsonl`
- `pipeline_contract.json`

In the run-pipeline contract payload, artifact paths are also surfaced under `artifacts.*`.

## Adversarial and Regression Tests

Focused verification regressions:

```bash
python -m pytest -q \
    tests/test_execution_confidence.py \
    tests/test_classification_engine_v2.py \
    tests/test_verification_intent_extensions.py \
    --ignore=output --import-mode=importlib
```

Adversarial truth stress suite:

```bash
python -m pytest -q tests/test_adversarial_truth_stress_suite.py --ignore=output --import-mode=importlib
```

Combined targeted suite:

```bash
python -m pytest -q \
    tests/test_execution_confidence.py \
    tests/test_classification_engine_v2.py \
    tests/test_verification_intent_extensions.py \
    tests/test_adversarial_truth_stress_suite.py \
    --ignore=output --import-mode=importlib
```

## Deterministic Repository Audit

Run repository-level structural and truth-validation checks:

```bash
python tools/repo_structure_audit.py \
    --repo . \
    --output-json output/repo_audit_report.json \
    --output-md output/repo_audit_report.md \
    --bubble-mode true
```

Useful truth-validation fields in `output/repo_audit_report.json`:

- `truth_validation_layer.runtime_meaningfulness.*`
- `truth_validation_layer.runtime_static_reconciliation.*`
- `truth_validation_layer.graph_sanity.*`
- `truth_validation_layer.classification_quality.*`

## Repository Hygiene

Generated artifacts are intentionally excluded through `.gitignore` to keep source diffs reviewable.

Dry-run cleanup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/cleanup_workspace.ps1
```

Apply cleanup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/cleanup_workspace.ps1 -Apply
```

Optional tracked cleanup actions:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/cleanup_workspace.ps1 -Apply -RestoreTrackedHarness
powershell -NoProfile -ExecutionPolicy Bypass -File tools/cleanup_workspace.ps1 -Apply -IncludeTracked
```

## Known Limitations

- Runtime tracing remains bounded by `--timeout-seconds`, `--max-events`, `--max-depth`, and `--memory-cap-mb`.
- Narrow or missing entrypoints can still reduce observed runtime breadth and scenario completeness.
- Static resolution remains conservative for heavily dynamic import/call patterns.
