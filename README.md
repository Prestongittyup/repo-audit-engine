# Repo Audit Engine

Deterministic repository audit engine with a Python-first execution path.

## Current Stage Pipeline

The staged orchestrator executes in this order:

1. `manifest`
2. `static`
3. `graph`
4. `bubble`
5. `classification`
6. `verification`
7. `diagnostics`
8. `report`

Source of truth: `repo_audit_engine/pipeline/stages.py` and `repo_audit_engine/pipeline/orchestrator.py`.

## CLI Commands

Run commands through the module entrypoint:

```bash
python -m repo_audit_engine <command> [options]
```

Available commands:

- `run-pipeline`
- `run`
- `validate`
- `analyze`
- `demo`

## Common Workflows

### 1) Full Contract + Artifacts (`run-pipeline`)

```bash
python -m repo_audit_engine run-pipeline \
    --repo <repoRoot> \
    --output output/contract.json \
    --bubble-mode true
```

Behavior:

- Writes the compact contract to `output/contract.json`.
- Writes stage artifacts to `output/contract_artifacts/`.

### 2) Staged Artifact Execution (`run`)

```bash
python -m repo_audit_engine run \
    --repo <repoRoot> \
    --output output/full_run \
    --mode full-pipeline \
    --bubble-mode true
```

Mode options:

- `manifest-only`
- `static-only` (alias: `static-analysis`)
- `bubble-run`
- `full-pipeline`

### 3) Validation and Analysis

Validate only:

```bash
python -m repo_audit_engine validate \
    --graph-path repo_audit_engine/examples/mock_graph.json \
    --resolver-path repo_audit_engine/examples/mock_resolver.json \
    --entrypoint canonical://service/App \
    --output output/validation.json \
    --pretty
```

Validation + diagnostics:

```bash
python -m repo_audit_engine analyze \
    --graph-path repo_audit_engine/examples/mock_graph.json \
    --resolver-path repo_audit_engine/examples/mock_resolver.json \
    --entrypoint canonical://service/App \
    --output output/analysis.json \
    --include-validation \
    --pretty
```

Demo:

```bash
python -m repo_audit_engine demo --output output/demo.json --pretty
```

## Runtime Bubble Truth

`bubble` mode executes selected entrypoints in a sandbox and streams runtime events. It is not a replacement for static analysis.

- Static stages build structural dependency evidence (`manifest`, `static`, `graph`).
- Bubble stage captures observed runtime behavior (`runtime_trace.jsonl`, `execution_flow_graph.json`).
- Verification computes trust and `system_valid` from validation outputs.
- Diagnostics annotate trust context and do not modify the trust score or `system_valid`.

## Evidence-Based Classification (v2)

The `classification` stage uses `EvidenceClassifier` and is deterministic and evidence-driven.

Evidence signals used per node:

- Runtime call hits from `runtime_trace.jsonl` (`event == "call"` only).
- Runtime reachability from BFS over `execution_flow_graph.json` edges of type `RUNTIME_CALL`.
- Executable references from dependency edges of type `CALL`.
- Non-executable references from dependency edges of all other types.

Score model:

- Start at `0.0`.
- Add `1.0` if `runtime_hits > 0`.
- Add `0.7` if `reachable_from_runtime` is true.
- Add `0.5` if `executable_references > 0`.
- Add `0.2` if `non_executable_references > 0`.
- Clamp to `[0.0, 1.0]` and round to 3 decimals.

Score thresholds:

- `HOT` if score `>= 0.8`
- `WARM` if score `>= 0.3`
- `COLD` if score `>= 0.1`
- `DEAD` otherwise

Hard deterministic guards:

- If `runtime_hits == 0` and node is not runtime-reachable, force `DEAD`.
- If node is `DEAD` but has executable references, reclassify to `COLD`.
- If node is `HOT` but has zero runtime hits, downgrade to `WARM`.
- `DEAD` nodes may still carry non-executable references; this is tracked as weak signal and not treated as executable contradiction.

Output contract (`heat_classification.json`):

- Top-level: `classifier`, `schema_version`, `distribution`, `nodes`
- Node-level: `node_id`/`id`, `classification`/`heat`, `score`, `evidence`
- Compatibility fields retained for downstream tooling: `runtime_hits`, `inbound_edges`, `outbound_edges`, `executable_references`, `non_executable_references`, `ast_references`

## Artifact Outputs

For full staged runs, the output directory contains:

- `manifest.jsonl`
- `manifest_summary.json`
- `static_analysis.jsonl`
- `static_analysis_summary.json`
- `dependency_graph.json`
- `dependency_graph_summary.json`
- `runtime_trace.jsonl`
- `execution_flow_graph.json`
- `heat_classification.json`
- `dead_code_report.json`
- `validation_result.json`
- `final_report.json`
- `pipeline_events.jsonl`
- `pipeline_contract.json`

## Streaming Architecture

Runtime tracing is streamed as JSONL, then aggregated:

- `repo_audit_engine/runtime/tracer.py` emits line-oriented event records.
- `repo_audit_engine/runtime/bubble_executor.py` streams and aggregates events into execution flow artifacts.
- `pipeline_events.jsonl` records deterministic stage transitions.

## Deterministic Repository Audit

Run repository-level architecture and determinism checks:

```bash
python tools/repo_structure_audit.py \
    --repo . \
    --output-json output/repo_audit_report.json \
    --output-md output/repo_audit_report.md \
    --bubble-mode true
```

Classification-focused validation checks:

```bash
python -m pytest -q tests/test_classification_engine_v2.py --import-mode=importlib
python -m pytest -q tests --ignore=output --import-mode=importlib
```

In `output/repo_audit_report.json`, classification consistency is reported under:

- `truth_validation_layer.classification_quality.passed`
- `truth_validation_layer.classification_quality.dead_referenced_count`
- `truth_validation_layer.classification_quality.dead_non_executable_count`

## Known Limitations

- Runtime tracing is bounded by `--timeout-seconds`, `--max-events`, `--max-depth`, and `--memory-cap-mb`.
- Entrypoint quality influences runtime coverage: missing or narrow entrypoints reduce observed execution breadth.
- Static resolution is conservative for dynamic import/call patterns.
