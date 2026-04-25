# Repository Audit Report

## System Integrity
- Overall: FAIL
- Critical issues: 3
- Warnings: 1

## Structure Audit
- Misplaced files: 0
- Orphaned scripts: 6
- Duplicate functionality findings: 0
- Legacy PowerShell files: 0
- Legacy command references (pwsh/run.ps1): 0
- Circular imports: 0
- Cross-layer violations: 0

## Pipeline Execution Health
- Pipeline success: PASS
- Stage order exact match: PASS
- Critical sections non-null: PASS
- Stage timings (seconds):
  - bubble: 4.9939
  - classification: 0.1142
  - diagnostics: 0.0066
  - graph: 0.0278
  - manifest: 0.1671
  - report: 0.0053
  - static: 0.1736
  - total: 5.5023
  - verification: 0.0082
- Schema validation:
  - dead_code_report_json: PASS
  - dependency_graph_json: PASS
  - execution_flow_graph_json: PASS
  - final_report_json: PASS
  - heat_classification_json: PASS
  - manifest_jsonl: PASS
  - manifest_summary_json: PASS
  - pipeline_events_jsonl: PASS
  - static_analysis_jsonl: PASS
  - validation_result_json: PASS

## Runtime Validation
- Bubble mode executed: PASS
- Runtime event stream present: PASS
- Execution graph generated: PASS
- Runtime event count: 20710
- Traced entrypoints: ['scenario:cli-smoke', 'scenario:core-flow']

## Truth Validation Layer
- Truth validation passed: PASS
- Runtime meaningfulness passed: PASS
- Runtime/static reconciliation passed: PASS
- Graph sanity passed: PASS
- Classification quality passed: PASS
- Runtime richness metrics: modules=34, local_modules=34, unique_functions=76, unique_local_functions=42, max_call_depth=86
- Runtime/static edge overlap: shared=24, runtime_only=60, static_only=363, overlap_ratio=0.0537
- Graph sanity metrics: reachable_ratio=0.3490, isolated_ratio=0.1967, runtime_confirmed_edge_ratio=0.0620
- Classification metrics: HOT=76, WARM=0, dead_referenced=0, warm_unreachable=0

## Determinism
- Deterministic: PASS
- Run 1 hash: 119e827d4a47194df222c67c7112ff90ecc20e14783d27b69c9d246d36c436d6
- Run 2 hash: 119e827d4a47194df222c67c7112ff90ecc20e14783d27b69c9d246d36c436d6
- Semantic deterministic: PASS
- Run 1 semantic hash: 5bc2ca9bd38b39c117f896db79016f315366701e4061e1146d251f1d98e45904
- Run 2 semantic hash: 5bc2ca9bd38b39c117f896db79016f315366701e4061e1146d251f1d98e45904
- Differences: []
- Semantic differences: []

## Issues
### Critical
- runtime_graph_isolation: repo_audit_engine/runtime/tracer.py: found forbidden token 'build_dependency_graph('
- runtime_graph_isolation: repo_audit_engine/runtime/tracer.py: found forbidden token 'resolver_data'
- runtime_graph_isolation: repo_audit_engine/runtime/tracer.py: found forbidden token 'validation_graph'
### Warnings
- Orphaned scripts detected: 6
