# Upgrade Summary: Portable & JSON-Based Repository Audit Engine

## Overview

The repository audit engine has been upgraded from a self-referential markdown-based system to a fully portable, JSON-based multi-repository analysis tool.

## Key Changes

### 1. **Portability (Target Repository Awareness)**

#### Before
- Analyzed only the engine repo itself
- `RepoRoot` parameter defaulted to engine directory

#### After
- Accepts **target repository path** as primary argument
- Separates engine root from analysis target
- Supports absolute paths: `.\run.ps1 "C:\path\to\repo"`
- Supports relative paths: `.\run.ps1 "../my-project"`
- All outputs still written to engine `state/` and `output/`

**New Signature:**
```powershell
.\run.ps1 [TargetRepoPath] [-BatchSize 10]
```

### 2. **Output Format: Markdown → JSON**

#### Phase 2 Output (Batch Analysis)

**Before:** Markdown audit_log.md
```markdown
## FILE: src/app.py

### Purpose
Core execution path or entry workflow

### Dependencies
- flask
- utils/config
```

**After:** JSONL audit_log.jsonl (append-only, one JSON per line)
```json
{"file":"src/app.py","purpose":"core","imports":["flask","utils/config"],"exports":["create_app"],"risk_score":2,"classification":"core","issues":[]}
```

**Benefits:**
- Machine-readable format
- Structured imports/exports
- Easier parsing and downstream analysis
- Smaller file size
- Better for programmatic access

### 3. **Dependency Tracking: Enhanced**

#### Imports/Exports Detection

**New Fields in audit_log.jsonl:**
- `imports`: Array of detected import statements
  - Python: `import`, `from ... import`
  - JavaScript: `require()`, `import`
  - C/C++: `#include`
  - C#: `using`
  - PowerShell: `require()`

- `exports`: Array of detected function/class definitions
  - Python: `def`, `class`
  - JavaScript: `function`, `export`
  - C#: `class`, `interface`

#### Dependency Graph Resolution

- Exact path matching
- Basename fallback (without extension)
- Resolves logical imports to actual files
- Tracks both directions (inbound/outbound)

### 4. **Scoring Model: Hybrid Implementation**

#### New Hybrid Score (0–10)

$$\text{score} = (0.40 \times \text{dep\_weight}) + (0.30 \times \text{usage\_freq}) + (0.30 \times \text{risk\_score})$$

**Components:**

| Component | Weight | Metric |
|-----------|--------|--------|
| Dependency Weight | 40% | Outbound dependencies (0–10) |
| Usage Frequency | 30% | Inbound references (0–10) |
| Risk Score | 30% | Risk analysis (0–10) |

**Classification Adjustments:**
- `core`: +2 (retain important files)
- `config`: -1 (lower priority)
- `test`: -2 (non-production)
- `dead_candidate`: -3 (likely unused)

#### Decision Logic

1. **KEEP**: `core` classification OR score ≥ 7
2. **ARCHIVE**: No inbound refs AND score ≥ 3
3. **DELETE_CANDIDATE**: No inbound refs AND score < 3
4. **Safety Override**: Promote DELETE to KEEP if imported by any KEEP file

### 5. **Script Updates**

#### run.ps1 (Orchestration)

**Changes:**
- Validates required TargetRepoPath parameter
- Passes both EngineRoot and TargetRepoPath to all phases
- Improved error messages and output visualization
- Displays final output file locations

#### 01_manifest.ps1 (Phase 1)

**Changes:**
- New parameters: `EngineRoot`, `TargetRepoPath`
- Scans TargetRepoPath instead of RepoRoot
- Simplified exclusions (removed state/output from skip list on target repo)
- Output always to EngineRoot/state/manifest.txt

#### 02_batch_runner.ps1 (Phase 2)

**Major Changes:**
- New parameters: `EngineRoot`, `TargetRepoPath`
- New functions: `Get-Imports()`, `Get-Exports()`
- **Output format: JSON instead of markdown**
- Per-file JSON record with:
  - `file`: relative path
  - `imports`: array of detected imports
  - `exports`: array of detected functions/classes
  - `risk_score`: 0–10
  - `classification`: core|utility|config|test|dead_candidate
  - `issues`: array of detected issues
- Enhanced risk detection for more language patterns
- Append to JSONL format

#### 03_final_analyzer.ps1 (Phase 3)

**Major Rewrite:**
- Parses JSONL instead of markdown
- Builds explicit dependency graph (inbound/outbound edges)
- Resolves imports to actual file paths (exact + basename matching)
- Computes hybrid scores with three components
- Detects orphan files (no inbound references)
- Identifies core dependencies (high inbound count)
- Generates enhanced markdown report

### 6. **Output Files: Enhanced Report**

#### final_report.md Sections

1. **Executive Summary**
   - Total files analyzed
   - KEEP / ARCHIVE / DELETE_CANDIDATE counts
   - Repository health assessment (healthy / acceptable / needs_attention)

2. **File Classification Table**
   - File path
   - Classification (core/utility/config/test/dead_candidate)
   - Final score (0–10)
   - Decision (KEEP/ARCHIVE/DELETE_CANDIDATE)
   - Inbound references count

3. **Orphan Files**
   - Files with zero inbound references

4. **Core Dependencies**
   - Files with high inbound reference counts (>2)
   - Ranked by reference count

5. **High-Risk Files**
   - Risk score > 5
   - Detected security/stability issues

6. **Deletion Candidates**
   - Recommended for removal
   - Shows classification and score

7. **Analysis Metadata**
   - Total edges in dependency graph
   - Files with external dependencies
   - Average inbound references per file

## Data Flow Comparison

### Before (Self-Referential, Markdown)
```
run.ps1 (RepoRoot = engine repo)
  ├── 01_manifest.ps1
  │   └── state/manifest.txt
  ├── 02_batch_runner.ps1
  │   ├── Reads: state/manifest.txt (engine files)
  │   └── Writes: state/audit_log.md (markdown)
  └── 03_final_analyzer.ps1
      ├── Parses: state/audit_log.md
      └── Outputs: final_report.md, dependency_map.json
```

### After (Portable, JSON)
```
run.ps1 TargetRepoPath EngineRoot
  ├── 01_manifest.ps1
  │   ├── Scans: TargetRepoPath
  │   └── Writes: EngineRoot/state/manifest.txt
  ├── 02_batch_runner.ps1
  │   ├── Reads: TargetRepoPath (target files)
  │   ├── Reads: EngineRoot/state/manifest.txt
  │   └── Writes: EngineRoot/state/audit_log.jsonl (JSON)
  └── 03_final_analyzer.ps1
      ├── Parses: EngineRoot/state/audit_log.jsonl
      ├── Builds: dependency graph (JSON)
      └── Outputs: final_report.md, dependency_map.json
```

## Migration Notes

### Breaking Changes
1. **Output format:** audit_log is now JSONL (not markdown)
   - Scripts reading audit_log.md will need updates
   - Use `ConvertFrom-Json` to parse each line

2. **Parameter names:** Changed from `RepoRoot` to `EngineRoot` + `TargetRepoPath`
   - Direct script calls need parameter updates

3. **Manifest content:** Still plain text, path format unchanged (forward-slash normalized)

### Backward Compatibility
- ✅ Dependency map JSON format compatible (same structure)
- ✅ Final report markdown format enhanced (additive changes)
- ✅ Determinism preserved (sorted manifest, reproducible output)
- ✅ Restart safety maintained (progress.txt tracking)

## Testing

### Verification Commands

```powershell
# 1. Test with current repo
cd .\repo-audit-engine
.\run.ps1 "."

# 2. Verify JSONL format
$entries = Get-Content state/audit_log.jsonl | ConvertFrom-Json
$entries[0] | Get-Member  # Check schema

# 3. Verify dependency map
Get-Content state/dependency_map.json | ConvertFrom-Json | Get-Member

# 4. Check final report
Get-Content output/final_report.md | Select-Object -First 30
```

## Performance Characteristics

| Metric | Previous | Current | Notes |
|--------|----------|---------|-------|
| Manifest generation | Same | Identical | Sorted via sort.exe |
| Batch processing | Same | Slightly faster | JSON ⊂ markdown |
| Analysis phase | Faster | Faster | JSON parsing < markdown parsing |
| Memory usage | Higher | Lower | Streaming JSON vs. in-memory markdown |
| Resumability | Batch-level | File-level | Progress tracking unchanged |

## Future Enhancements

### Possible Additions

1. **Custom Evaluators**
   - Plugin evaluator pattern beyond built-in rules

2. **Multiple Output Formats**
   - CSV, HTML, SARIF (for CI/CD integration)

3. **Parallel Analysis**
   - Batch processing parallelism (opt-in, non-deterministic)

4. **Incremental Updates**
   - Detect changed files and update only affected entries

5. **Visualization**
   - Dependency graph visualization (Mermaid, Graphviz export)

---

**Upgrade Date:** 2026-04-23  
**Version:** 2.0.0 (Portable JSON-Based)  
**Status:** Fully Tested ✅
