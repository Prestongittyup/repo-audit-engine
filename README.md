# Repository Audit Engine

A portable, dependency-aware repository analysis tool that produces deterministic audit reports for any codebase.

## 🎯 Quick Start

```powershell
# Basic analysis (outputs to $AuditWorkspacePath/<repo_id>/<run_id>/)
.\run.ps1 "C:\path\to\repo"

# Static mode explicitly (deterministic pipeline only)
.\run.ps1 "C:\path\to\repo" -Mode static

# Semantic mode (static + interpretation layer)
.\run.ps1 "C:\path\to\repo" -Mode semantic

# Full mode (static + semantic + decision layers)
.\run.ps1 "C:\path\to\repo" -Mode full

# Custom audit workspace location
.\run.ps1 "C:\path\to\repo" -AuditWorkspacePath "C:\my\audits"

# Use custom run identifier (instead of deterministic hash)
.\run.ps1 "C:\path\to\repo" -RunId "initial_scan"

# CI/CD mode: structured JSON output, no noise, exit code indicates issues (0=clean, 1=issues found)
.\run.ps1 "C:\path\to\repo" -CI_MODE

# Combined: custom workspace, custom run ID, CI/CD mode
.\run.ps1 "C:\path\to\repo" -AuditWorkspacePath "C:\audits" -RunId "prod_scan" -CI_MODE
```

## 🏗️ Architecture

The engine runs a **6-phase deterministic pipeline** that can restart safely without duplicate work. All outputs are written to an **external audit workspace** (never inside the target repository).

## 🧠 Optional Interpretation Layers

The runtime supports layered execution modes:


Critical boundary rules:


```
## 🔄 Determinism & Reproducibility
                ↓                    ↓                         ↓                      ↓
             manifest.txt         audit_log.jsonl      config_dependencies.json   dependency_truth_graph.json
                                  index.json                                       architecture_analysis.json
                                                                                    run_metadata.json
```

### Phase 1: Codebase Indexer (`src/00_codebase_indexer.ps1`)

**Purpose:** Build deterministic sorted manifest of all indexable files.

**Input:** Target repository path  
**Output:** `<RUN_DIR>/index.json`, `<RUN_DIR>/manifest.json`

**Features:**
- Excludes: `__pycache__`, `node_modules`, `.git`, `.venv`, `venv`, `dist`, `build`, `.next`, `out`, `.nuget`
- Excludes: binary files (`.exe`, `.pyc`, `.pyd`, `.obj`, `.dll`, `.so`)
- Excludes: audit workspace directories and previous run outputs
- Deterministically sorted
- Generates both JSON index and flat manifest

### Phase 2: Batch Analysis (`src/02_batch_runner_v3.ps1`)

**Purpose:** Analyze each file for imports, exports, risk patterns, and append-only audit log.

**Input:** `<RUN_DIR>/manifest.json`, target repository files  
**Output:** `<RUN_DIR>/audit_log.jsonl` (JSONL format, one JSON object per line)

**Per-file Analysis:**

```json
{
  "file": "src/app.py",
  "purpose": "core",
  "imports": ["flask", "utils.config"],
  "exports": ["create_app", "main"],
  "risk_score": 2,
  "classification": "core",
  "issues": [],
  "timestamp": "2026-04-24T10:15:30Z"
}
```

**Features:**
- Detects imports/exports via regex patterns (Python, JavaScript, Go, Rust support)
- Classifies files: `core`, `utility`, `config`, `test`, `dead_candidate`, `vendor`
- Scores risk 0–10 based on secrets, dynamic execution, path traversal
- All entries include schema headers (`engine_version`, `schema_version`)
- Resumable via progress tracking

### Phase 3: Config Truth Builder (`src/02_5_config_truth_builder.ps1`)

**Purpose:** Analyze configuration files and build dependency truth for config-driven patterns.

**Input:** `<RUN_DIR>/audit_log.jsonl`, config files  
**Output:** `<RUN_DIR>/config_dependencies.json`

**Features:**
- Extracts dependencies from `.yaml`, `.json`, `.toml`, `.env` files
- Identifies config-driven service dependencies
- Maps plugin/module registration patterns
- Includes all schema headers

### Phase 4: Truth Engine (`src/03_truth_engine.ps1`)

**Purpose:** Build complete dependency graph, compute hybrid health scores, and generate comprehensive analysis.

**Input:** `<RUN_DIR>/audit_log.jsonl`, `<RUN_DIR>/config_dependencies.json`  
**Output:** Multiple JSON files with full analysis

**Output Files:**

1. **dependency_truth_graph.json** — Complete dependency graph with computed importance scores
2. **architecture_analysis.json** — Architectural violations, coupling analysis, layer violations
3. **dead_code_report.json** — Dead code candidates ranked by confidence
4. **contradictions.json** — Dependency contradictions and unsafe patterns
5. **system_health_score.json** — Overall health metrics (cohesion, coupling, modularity, risk)
6. **run_metadata.json** — Execution metadata (timing, file counts, reproducibility info)

**All files include schema headers:**
```json
{
  "run_id": "abc123def456",
  "repo_id": "xyz789",
  "timestamp": "2026-04-24T10:15:30Z",
  "engine_version": "1.0.0",
  "schema_version": "v3",
  "data": { ... }
}
```

**Hybrid Scoring Model:**

- **Dependency Weight (40%):** Outbound dependencies relative to max
- **Usage Frequency (30%):** Inbound references relative to max
- **Risk Score (30%):** Risk analysis from Phase 2

**Classification Adjustments:**
- `core` files: +2 bonus
- `config` files: -1
- `test` files: -2
- `dead_candidate` files: -3

**Decision Rules:**

| Criteria | Decision |
|----------|----------|
| `core` classification OR score ≥ 7 | **KEEP** |
| No inbound refs AND score ≥ 3 | **ARCHIVE** |
| No inbound refs AND score < 3 | **DELETE_CANDIDATE** |

**Safety Check:** Files marked DELETE_CANDIDATE are automatically promoted to KEEP if imported by any KEEP file.

## 📊 Output Structure

All outputs are written to an **external audit workspace**, leaving the target repository completely untouched.

```
$AUDIT_WORKSPACE/
└── <repo_id>/                           # Hash of absolute repo path
    ├── <run_id>/                        # Custom run ID or deterministic hash
    │   ├── index.json                   # Indexed files
    │   ├── manifest.json                # File manifest with metadata
    │   ├── audit_log.jsonl              # Per-file analysis (one JSON per line)
    │   ├── config_dependencies.json     # Config-driven dependencies
    │   ├── dependency_truth_graph.json  # Complete dependency graph
    │   ├── architecture_analysis.json   # Architectural patterns & violations
    │   ├── dead_code_report.json        # Dead code candidates
    │   ├── contradictions.json          # Dependency contradictions
    │   ├── system_health_score.json     # Overall health metrics
    │   └── run_metadata.json            # Execution metadata
    ├── run_latest/                      # Symlink/marker to latest run
    └── [previous runs]/                 # Historical audit runs
```

**Default `$AUDIT_WORKSPACE` location:**
```
repo-audit-engine/
└── ../audit_workspace/                 # Sibling to engine repo
```

**Override with `-AuditWorkspacePath` parameter:**
```powershell
.\run.ps1 "C:\code\myapp" -AuditWorkspacePath "D:\audits"
```

### Identifiers

- **`repo_id`**: Deterministic hash of absolute repository path. Same repo always maps to same `repo_id` across different machines.
- **`run_id`**: Either custom (via `-RunId` parameter) or automatically generated via deterministic hash of (repo_path + git_commit + engine_version + config_hash). Ensures reproducibility: **same repo state → same run_id → identical outputs**.

### Schema Versioning

All JSON output files include consistent headers for tooling integration:

```json
{
  "run_id": "abc123def456",
  "repo_id": "xyz789",
  "timestamp": "2026-04-24T10:15:30Z",
  "engine_version": "1.0.0",
  "schema_version": "v3",
  "data": { /* actual output */ }
}
```

## 🔄 Restart Safety

All phases are **deterministic and restart-safe**:

- **Phase 1** generates sorted manifest (deterministic sort via `sort.exe`)
- **Phase 2** tracks progress in `state/progress.txt`, skips already-processed files
- **Phase 3** reads final JSON log (idempotent)

Rerunning the pipeline against the same target repository produces identical results.

## 🎛️ Parameters

### run.ps1

```powershell
.\run.ps1 [TargetRepoPath] [-AuditWorkspacePath <path>] [-RunId <id>] [-CI_MODE] [-Mode <static|semantic|full>]
```

#### Required Parameters

- **TargetRepoPath** (position 0)
  - Absolute or relative path to target repository
  - Example: `"C:\code\myapp"`, `"../src"`, `"."`

#### Optional Parameters

- **-AuditWorkspacePath** `<path>`
  - Location for audit workspace (default: `../audit_workspace` relative to engine)
  - Use to centralize audits across multiple repositories
  - Example: `"D:\audits"`, `"\\network\shared\audits"`

- **-RunId** `<string>`
  - Custom identifier for this run
  - If omitted, deterministically generated from repo state (reproducible)
  - Useful for labeling scans: `"prod_baseline"`, `"after_refactor"`, `"daily_scan"`
  - Example: `.\run.ps1 "C:\repo" -RunId "v2.0_analysis"`

- **-CI_MODE**
  - Enable CI/CD mode: structured JSON output, minimal console noise
  - Exit codes: 0 = clean, 1 = issues detected (contradictions, dead code, high risk)
  - Suitable for CI/CD pipelines, automated testing
  - Example: `.\run.ps1 "C:\repo" -CI_MODE`

- **-Mode** `<static|semantic|full>`
  - `static` (default): deterministic core only
  - `semantic`: deterministic core + semantic outputs under `semantic/`
  - `full`: deterministic core + semantic outputs + decision outputs under `decisions/`
  - Example: `.\run.ps1 "C:\repo" -Mode full`

## 🔄 Determinism & Reproducibility

The engine guarantees **perfect reproducibility**:

- **Phase 1** generates sorted index (deterministic)
- **Phase 2** analyzes files in sorted order (deterministic)
- **Phase 3** computes config dependencies deterministically
- **Phase 4** generates graph and scores from deterministic input

**Key property:** Same repository state (files + content + git commit) always produces:
- Same `run_id` (if not custom)
- Same dependency graph
- Same dead code candidates
- Same scores and classifications
- Identical JSON outputs (byte-for-byte)

This enables:
- Reproducible audits across machines
- Meaningful diffs when comparing runs
- Validation of bug fixes (same input → same output)
- Baseline comparisons for CI/CD

### Restart Safety

All phases are **restart-safe**:
- **Phase 2** tracks progress, skips already-analyzed files
- **Phase 4** reads final JSONL log (idempotent)
- Interrupted runs can resume without duplicate work

## 📋 Output Files Reference

### run_metadata.json

Execution metadata for traceability and reproducibility:

```json
{
  "run_id": "abc123def456",
  "repo_id": "xyz789",
  "timestamp": "2026-04-24T10:15:30Z",
  "engine_version": "1.0.0",
  "schema_version": "v3",
  "start_time": "2026-04-24T10:15:30Z",
  "end_time": "2026-04-24T10:15:45Z",
  "repo_path": "C:\\code\\myapp",
  "git_commit": "abc123def456...",
  "file_count": 245,
  "scan_mode": "full"
}
```

### audit_log.jsonl

Append-only JSONL (one JSON object per line), with schema headers:

```json
{"run_id":"abc123","repo_id":"xyz789","timestamp":"2026-04-24T10:15:30Z","engine_version":"1.0.0","schema_version":"v3","file":"src/app.py","purpose":"core","imports":["flask"],"exports":["create_app"],"risk_score":2,"classification":"core","issues":[]}
{"run_id":"abc123","repo_id":"xyz789","timestamp":"2026-04-24T10:15:30Z","engine_version":"1.0.0","schema_version":"v3","file":"tests/test_app.py","purpose":"test","imports":["src/app"],"exports":[],"risk_score":0,"classification":"test","issues":[]}
```

### dependency_truth_graph.json

Complete dependency graph with computed importance scores:

```json
{
  "run_id": "...",
  "repo_id": "...",
  "timestamp": "...",
  "engine_version": "1.0.0",
  "schema_version": "v3",
  "data": {
    "nodes": [...],
    "edges": [...],
    "importance_scores": {...}
  }
}
```

### CI/CD Mode Return Codes

When using `-CI_MODE`:

- **Exit 0** — Success, no issues found
- **Exit 1** — Issues detected (contradictions, dead code candidates, high-risk files)

## 🔍 Classification Logic

Files are classified based on:

1. **Path patterns**
   - `test|tests|__tests__|spec` → `test`
   - `config|settings` → `config`
   - `core|engine|main|app|index` → `core`

2. **File extensions**
   - `.json`, `.yaml`, `.toml`, `.ini`, `.env`, `.xml` → `config`

3. **Content signals**
   - Presence of function/class/def → not `dead_candidate`
   - Absence of logic + source code extension → `dead_candidate`

4. **Default** → `utility`

## ⚠️ Risk Scoring

Risk scores (0–10) are computed per file:

- Hardcoded secrets (password, api_key, secret, token): +3
- Dynamic execution (eval, Invoke-Expression, exec): +3
- Relative path traversal (`../` or `..\`): +2
- High import count (>15): +2
- Medium import count (8–15): +1

## 🛡️ Safety Guarantees

1. **No target repo modification:** Engine performs read-only analysis, never modifies target repository
2. **No contamination:** Outputs isolated in external workspace; target repo remains completely untouched
3. **Deterministic:** Same repo state always produces identical outputs
4. **Reproducible:** Run audits across different machines, get identical results
5. **Safe deletion candidates:** Files marked DELETE_CANDIDATE are never in KEEP set if imported elsewhere
6. **No external writes:** Only writes to audit workspace and local engine state

## 🚀 Advanced Usage

### Analyze Multiple Repositories to Shared Workspace

```powershell
$AuditPath = "D:\central_audits"

@("C:\repo1", "C:\repo2", "C:\repo3") | ForEach-Object {
    Write-Host "Analyzing $_"
    .\run.ps1 $_ -AuditWorkspacePath $AuditPath
}

# All audits now in D:\central_audits/<repo_id>/<run_id>/
```

### CI/CD Pipeline Integration

```powershell
# Run with CI/CD mode and capture exit code
.\run.ps1 "C:\code\myapp" -AuditWorkspacePath "D:\audits" -CI_MODE
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host "Issues detected in codebase"
    exit 1
}
```

### Compare Consecutive Runs

```powershell
# First baseline scan
.\run.ps1 "C:\repo" -RunId "baseline_v1" -AuditWorkspacePath "D:\audits"

# After refactor - compare to baseline
.\run.ps1 "C:\repo" -RunId "after_refactor" -AuditWorkspacePath "D:\audits"

# Results in:
# D:\audits\<repo_id>\baseline_v1\
# D:\audits\<repo_id>\after_refactor\
```

### Extract Analysis Results

```powershell
# Get dead code candidates
$runDir = "D:\audits\<repo_id>\<run_id>"
$deadCode = Get-Content "$runDir/dead_code_report.json" | ConvertFrom-Json
$deadCode.data.candidates | Where-Object { $_.confidence -gt 0.7 }

# Get contradictions
$contradictions = Get-Content "$runDir/contradictions.json" | ConvertFrom-Json
$contradictions.data.issues

# Get health score
$health = Get-Content "$runDir/system_health_score.json" | ConvertFrom-Json
$health.data
```

## ✅ Testing & Validation

### Self-Test: Audit the Engine Repository

Test the engine against itself:

```powershell
cd repo-audit-engine
.\run.ps1 "." -RunId "self_test"
```

### Verify Output Files

Check outputs in the workspace:

```powershell
$runDir = "..\audit_workspace\<repo_id>\self_test"

# Verify schema headers
$metadata = Get-Content "$runDir/run_metadata.json" | ConvertFrom-Json
Write-Host "Run ID: $($metadata.run_id)"
Write-Host "Schema: $($metadata.schema_version)"
Write-Host "Engine: $($metadata.engine_version)"

# Check dead code report
$deadCode = Get-Content "$runDir/dead_code_report.json" | ConvertFrom-Json
Write-Host "Dead code candidates: $($deadCode.data.candidates.Count)"

# Check contradictions
$contradictions = Get-Content "$runDir/contradictions.json" | ConvertFrom-Json
Write-Host "Issues found: $($contradictions.data.issues.Count)"
```

### Validate Reproducibility

Run the same scan twice and verify outputs are identical:

```powershell
.\run.ps1 "." -RunId "test_repro_1"
.\run.ps1 "." -RunId "test_repro_2"

# Compare outputs
$run1 = Get-Content "..\audit_workspace\<repo_id>\test_repro_1\dependency_truth_graph.json"
$run2 = Get-Content "..\audit_workspace\<repo_id>\test_repro_2\dependency_truth_graph.json"

if ($run1 -eq $run2) {
    Write-Host "✓ Reproducibility verified"
} else {
    Write-Host "✗ Outputs differ (unexpected)"
}
```

## 📋 Requirements

- **PowerShell 5.0+** (or PowerShell Core)
- **Windows** (uses `sort.exe` for deterministic sorting)
- **Git** (optional, for automatic commit hash detection; gracefully degrades to `N/A`)
- No external dependencies (pure .NET APIs)

## 🔗 Consuming Outputs

### In CI/CD Pipelines

```yaml
# Example: GitHub Actions
- name: Run Audit Engine
  run: |
    .\run.ps1 "${{ github.workspace }}" -AuditWorkspacePath "D:\audits" -CI_MODE
  shell: pwsh

- name: Check Results
  run: |
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
      Write-Host "Code quality issues detected"
      exit 1
    }
```

### Dashboard Integration

All outputs include schema headers with:
- `run_id`: Unique run identifier
- `repo_id`: Stable repository identifier
- `timestamp`: ISO8601 execution time
- `engine_version`: Engine version for compatibility
- `schema_version`: Output format version

This enables integration with monitoring/dashboard tools that consume the workspace.

## 📄 License

Internal use only.

---

**Engine Version:** 1.0.0  
**Schema Version:** v3  
**Last Updated:** 2026-04-24

