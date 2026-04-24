# Repo Audit Engine (Deterministic Layered Pipeline)

This repository now implements a deterministic, layer-based audit pipeline.

The flow is:

1. Deterministic file inventory
2. Canonical identity graph nodes
3. Multi-resolver dependency extraction
4. Unified graph build
5. Graph validation gate
6. Deterministic graph queries
7. Query-derived classification
8. Final structured audit report
9. Structural/semantic/resolver verification and trust aggregation

## Entrypoint

- CLI router: `run.ps1`

All operations are invoked through:

```powershell
.\run.ps1 <command> [parameters]
```

## Implemented Commands

### Layer 1: Inventory

Build deterministic repository inventory.

```powershell
.\run.ps1 layer1-inventory -RepoPath <repoRoot> -OutputPath <inventory.json> [-DebugMode]
```

Script: `src/layer1_file_inventory.ps1`

### Layer 2: Canonical Identity

Create canonical FILE nodes from Layer 1 inventory.

```powershell
.\run.ps1 layer2-canonical -InventoryPath <inventory.json> -OutputPath <canonical_nodes.json>
```

Script: `src/layer2_canonical_identity.ps1`

### Layer 3: Multi-Resolver Dependencies

Extract dependency edges using AST, DI, CONFIG, and HEURISTIC resolvers.

```powershell
.\run.ps1 layer3-resolve -InventoryPath <inventory.json> -CanonicalPath <canonical_nodes.json> -OutputPath <edges.json>
```

Scripts:

- `src/layer3_multi_resolver.ps1`
- `src/layer3_py_ast_resolver.py`

### Layer 4: Unified Graph

Merge nodes + edges into one canonical graph.

```powershell
.\run.ps1 layer4-graph -CanonicalPath <canonical_nodes.json> -EdgesPath <edges.json> -OutputPath <unified_graph.json>
```

Script: `src/layer4_unified_graph.ps1`

### Layer 5: Graph Validation

Validate graph structure and block downstream stages when invalid.

```powershell
.\run.ps1 layer5-validate -GraphPath <unified_graph.json> -OutputPath <graph_validation.json> [-FailOnInvalid:$true|$false]
```

Script: `src/layer5_graph_validation.ps1`

### Verification Authority Gate (Final Truth)

Run VerificationRunner as the explicit authority gate before interpretation layers.

```powershell
.\run.ps1 verify-authority -GraphPath <unified_graph.json> -EdgesPath <edges.json> -ValidationPath <graph_validation.json> -OutputPath <authority_verdict.json> [-Entrypoints <canonicalId1>,<canonicalId2>]
```

Script: `src/verification_authority_gate.py`

### Layer 6: Graph Query Engine

Run deterministic graph traversal queries against a VALID graph.

```powershell
.\run.ps1 layer6-query -GraphPath <unified_graph.json> -ValidationPath <graph_validation.json> -AuthorityPath <authority_verdict.json> -Query <queryName> -OutputPath <query_result.json> [-Entrypoints <canonicalId1>,<canonicalId2>]
```

Supported query names:

- `REACHABLE_FROM(entrypoints)`
- `ORPHAN_NODES`
- `DEAD_NODES`
- `SUSPICIOUS_DI_NODES`
- `DISCONNECTED_CLUSTERS`

Script: `src/layer6_graph_query.ps1`

### Layer 7: Query-Based Classification

Build classification strictly from Layer 6 query outputs.

```powershell
.\run.ps1 layer7-classify \
  -ValidationPath <graph_validation.json> \
  -AuthorityPath <authority_verdict.json> \
  -ReachableQueryPath <reachable.json> \
  -OrphanQueryPath <orphan.json> \
  -DeadQueryPath <dead.json> \
  -SuspiciousQueryPath <suspicious.json> \
  -DisconnectedClustersQueryPath <clusters.json> \
  [-ExemptQueryPath <exempt.json>] \
  -OutputPath <classification.json>
```

Script: `src/layer7_query_classification.ps1`

### Layer 8: Final Structured Report

Aggregate graph, classification, validation, and resolver contribution counts into final report.

```powershell
.\run.ps1 layer8-report -GraphPath <unified_graph.json> -ValidationPath <graph_validation.json> -ClassificationPath <classification.json> -OutputPath <final_structured_report.json>
```

Script: `src/layer8_final_report.ps1`

### Structural Graph Validation

Run strict structural checks over a pre-built graph against ingestion inventory.

```powershell
.\run.ps1 validate-graph-structure -GraphPath <unified_graph.json> -InventoryPath <inventory.json> -OutputPath <graph_structural_validation.json>
```

Script: `src/graph_structural_validation.ps1`

### Resolver Consistency Comparison

Compare resolver outputs against final graph and enforce hard fail conditions.

```powershell
.\run.ps1 compare-resolvers -GraphPath <unified_graph.json> -EdgesPath <edges.json> [-HeuristicOnlyThreshold 0] -OutputPath <resolver_consistency.json>
```

Script: `src/resolver_consistency_check.ps1`

### Semantic Graph Validation

Validate entrypoint/DI/core semantic constraints over final graph.

```powershell
.\run.ps1 semantic-validate -GraphPath <unified_graph.json> [-ClassificationPath <classification.json>] -OutputPath <semantic_validation.json>
```

Script: `src/semantic_graph_validation.ps1`

### System Trust Aggregation

Aggregate all validation layers into final system trust status and score.

```powershell
.\run.ps1 aggregate-trust \
  -StructuralValidationPath <graph_structural_validation.json> \
  -ReachabilityValidationPath <reachability_validation.json> \
  -ResolverConsistencyPath <resolver_consistency.json> \
  -SemanticValidationPath <semantic_validation.json> \
  -OutputPath <system_trust.json>
```

Script: `src/system_trust_aggregation.ps1`

## Python Verification Module

In addition to PowerShell layer commands, this repo includes a deterministic Python verifier:

- Class: `VerificationRunner`
- File: `src/verification_runner.py`

`VerificationRunner` runs four hard-gate checks:

1. Structural integrity
2. Reachability
3. Resolver consistency
4. Semantic sanity

Enhancements implemented in `VerificationRunner`:

- Edge-based DI validation (DI is validated from graph and resolver edges, not only node flags)
- Resolver coverage/divergence metrics and dropped-edge detection for AST/DI/CONFIG
- Resolver enforcement constraints (hard fail): AST coverage floor, DI missing-edge zero tolerance, drift-score ceiling
- Disconnected island detection including cyclic islands and false-healthy subgraphs
- Unified island semantics: disconnected clusters are defined as unreachable subgraphs from entrypoints
- Weighted trust scoring:
  - structural: `0.35`
  - reachability: `0.35`
  - resolver: `0.20`
  - semantic: `0.10`
- Critical gate behavior: structural/reachability/resolver failures force trust score to `0.0` (not averaged)

And returns:

- `system_valid`
- `failure_domains`
- `trust_score`
- per-layer `results`

### VerificationRunner Input Contract

```python
graph = {
  "nodes": [
    {"id": "canonical://repo/path:file.py", "metadata": {}}
  ],
  "edges": [
    {
      "from": "canonical://repo/path:a.py",
      "to": "canonical://repo/path:b.py",
      "type": "IMPORT",  # IMPORT | DI | CONFIG | DYNAMIC
      "confidence": 0.9
    }
  ]
}

entrypoints = [
  "canonical://repo/path:main.py"
]

resolver_data = {
  "ast_edges": [],
  "di_edges": [],
  "config_edges": [],
  "heuristic_edges": []
}
```

### VerificationRunner Example

```python
from src.verification_runner import VerificationRunner

runner = VerificationRunner(
  graph=graph,
  entrypoints=entrypoints,
  resolver_data=resolver_data,
)

result = runner.run()
print(result["system_valid"], result["trust_score"])
```

### VerificationRunner Output Contract

```json
{
  "system_valid": true,
  "failure_domains": [],
  "trust_score": 1.0,
  "results": {
    "structural": {
      "invalid_edges": [],
      "node_count": 0,
      "edge_count": 0
    },
    "reachability": {
      "reachable_count": 0,
      "unreachable_count": 0,
      "false_dead_nodes": [],
      "entrypoints_used": [],
      "entrypoints_without_outgoing": [],
      "di_unreachable_edges": []
    },
    "resolver": {
      "ast_edges": 0,
      "di_edges": 0,
      "config_edges": 0,
      "heuristic_edges": 0,
      "missing_di_nodes": [],
      "heuristic_only_nodes": [],
      "missing_references": [],
      "missing_ast_edges": [],
      "missing_di_edges": [],
      "missing_config_edges": [],
      "coverage": {
        "ast": 1.0,
        "di": 1.0,
        "config": 1.0,
        "heuristic": 1.0
      },
      "divergence": {
        "ast": 0.0,
        "di": 0.0,
        "config": 0.0,
        "heuristic": 0.0
      },
      "drift_score": 0.0,
      "enforcement": {
        "min_ast_coverage": 0.9,
        "max_drift_score": 0.1,
        "violations": []
      }
    },
    "semantic": {
      "entrypoint_isolated": [],
      "di_wired_missing_edges": [],
      "disconnected_islands": [],
      "cyclic_islands": [],
      "false_healthy_subgraphs": []
    }
  }
}
```

### VerificationRunner Fixture Suite

The repo includes deterministic fixture coverage for the verifier:

- `src/verification_runner_negative_fixtures.py`

Run:

```powershell
python .\src\verification_runner_negative_fixtures.py
```

Covered fixtures:

1. happy path
2. orphan node graph
3. DI node missing from graph
4. resolver mismatch (AST-only edge)
5. disconnected island subgraph

## Core Output Contracts

### Layer 4 (`unified_graph.json`)

```json
{
  "graph": {
    "nodes": [],
    "edges": []
  },
  "stats": {
    "node_count": 0,
    "edge_count": 0,
    "deduplicated_edges": 0
  }
}
```

### Layer 5 (`graph_validation.json`)

```json
{
  "status": "VALID | INVALID",
  "issues": [],
  "metrics": {
    "orphan_nodes": 0,
    "disconnected_clusters": 0,
    "di_nodes_missing_edges": 0
  }
}
```

Note: `disconnected_clusters` is counted as unreachable subgraphs from entrypoints.

### Layer 6 (`query_result.json`)

```json
{
  "query": "...",
  "results": []
}
```

### Layer 7 (`classification.json`)

```json
{
  "classification": {
    "REACHABLE": [],
    "REFERENCED": [],
    "ISOLATED": [],
    "SUSPICIOUS": [],
    "DEAD": [],
    "EXEMPT": []
  }
}
```

### Layer 8 (`final_structured_report.json`)

```json
{
  "graph_summary": {},
  "classification": {},
  "validation": {},
  "resolver_metrics": {}
}
```

### Authority Gate (`authority_verdict.json`)

```json
{
  "authority": "VERIFICATION_RUNNER",
  "authority_valid": true,
  "preconditions": {
    "layer5_validation_status": "VALID",
    "layer5_precondition_ok": true
  },
  "entrypoints_used": [],
  "verification": {}
}
```

### Structural Validation (`graph_structural_validation.json`)

```json
{
  "valid": true,
  "errors": [],
  "stats": {
    "nodes": 0,
    "edges": 0,
    "invalid_edges": 0
  }
}
```

### Resolver Consistency (`resolver_consistency.json`)

```json
{
  "ast_edges": 0,
  "di_edges": 0,
  "config_edges": 0,
  "heuristic_edges": 0,
  "disagreements": []
}
```

### Semantic Validation (`semantic_validation.json`)

```json
{
  "semantic_valid": true,
  "anomalies": []
}
```

### Trust Aggregation (`system_trust.json`)

```json
{
  "system_valid": false,
  "failure_domains": [],
  "trust_score": 0.0
}
```

## Quick End-to-End Example

```powershell
$repo = "C:\path\to\target-repo"

.\run.ps1 layer1-inventory -RepoPath $repo -OutputPath .\output\layer1_inventory.json
.\run.ps1 layer2-canonical -InventoryPath .\output\layer1_inventory.json -OutputPath .\output\canonical_nodes.json
.\run.ps1 layer3-resolve -InventoryPath .\output\layer1_inventory.json -CanonicalPath .\output\canonical_nodes.json -OutputPath .\output\edges.json
.\run.ps1 layer4-graph -CanonicalPath .\output\canonical_nodes.json -EdgesPath .\output\edges.json -OutputPath .\output\unified_graph.json
.\run.ps1 layer5-validate -GraphPath .\output\unified_graph.json -OutputPath .\output\graph_validation.json
.\run.ps1 verify-authority -GraphPath .\output\unified_graph.json -EdgesPath .\output\edges.json -ValidationPath .\output\graph_validation.json -OutputPath .\output\authority_verdict.json
```

Then run the required Layer 6 queries (with `-AuthorityPath .\output\authority_verdict.json`), build Layer 7 classification from those query outputs, and generate Layer 8 final report.

For hard-gate verification, run structural/reachability/resolver/semantic validation and then `aggregate-trust` to compute the final trust status.

## Notes

- The pipeline is deterministic by design (sorted outputs, stable deduplication behavior).
- Layer 6 and Layer 7 require a VALID Layer 5 status.
- Layer 5 can be configured with `-FailOnInvalid:$false` for diagnostics-only runs.
