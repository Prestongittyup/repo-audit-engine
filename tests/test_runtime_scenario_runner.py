from __future__ import annotations

import json
from pathlib import Path

from repo_audit_engine.runtime.scenario_runner import (
    build_runtime_scenario_plan,
    encode_scenario_entrypoint,
    run_encoded_scenario,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")


def _encoded_spec(entrypoint: str) -> str:
    return str(entrypoint).split("scenario:auto:", 1)[1]


def test_build_runtime_scenario_plan_filters_and_scores_nodes(tmp_path: Path) -> None:
    dependency_graph = {
        "nodes": [
            {"id": "file:src/app.py", "kind": "file"},
            {"id": "function:src/app.py:main", "kind": "function"},
            {"id": "function:src/app.py:worker", "kind": "function"},
            {"id": "file:apps/api/router.py", "kind": "file"},
            {"id": "function:apps/api/router.py:create_router", "kind": "function"},
            {"id": "file:scripts/cli_tool.py", "kind": "file"},
            {"id": "function:scripts/cli_tool.py:main", "kind": "function"},
            {"id": "class:src/runner.py:Runner", "kind": "class"},
            {"id": "function:tests/test_app.py:test_worker", "kind": "function"},
        ],
        "edges": [
            {"source": "function:src/app.py:main", "target": "function:src/app.py:worker", "type": "CALL"},
            {"source": "function:src/app.py:main", "target": "class:src/runner.py:Runner", "type": "CALL"},
            {"source": "function:src/app.py:main", "target": "function:apps/api/router.py:create_router", "type": "CALL"},
            {"source": "function:scripts/cli_tool.py:main", "target": "function:src/app.py:worker", "type": "CALL"},
            {"source": "class:src/runner.py:Runner", "target": "function:src/app.py:worker", "type": "CALL"},
        ],
    }
    manifest_summary = {"entrypoints": ["src/app.py", "apps/api/router.py"]}
    existing_flow = {
        "node_hits": {
            "function:src/app.py:main": 1,
        },
        "edges": [
            {
                "source": "function:src/app.py:main",
                "target": "function:src/app.py:worker",
                "type": "RUNTIME_CALL",
            }
        ],
    }

    dependency_graph_path = tmp_path / "dependency_graph.json"
    manifest_summary_path = tmp_path / "manifest_summary.json"
    manifest_path = tmp_path / "manifest.jsonl"
    existing_flow_path = tmp_path / "execution_flow_graph.json"

    _write_json(dependency_graph_path, dependency_graph)
    _write_json(manifest_summary_path, manifest_summary)
    _write_jsonl(
        manifest_path,
        [
            {
                "path": "src/app.py",
                "language": "python",
                "module": "src.app",
                "imports": ["fastapi"],
                "entrypoint_reasons": ["main_guard"],
            },
            {
                "path": "apps/api/router.py",
                "language": "python",
                "module": "apps.api.router",
                "imports": ["fastapi"],
                "entrypoint_reasons": [],
            },
            {
                "path": "scripts/cli_tool.py",
                "language": "python",
                "module": "scripts.cli_tool",
                "imports": ["argparse"],
                "entrypoint_reasons": [],
            },
            {
                "path": "tests/test_app.py",
                "language": "python",
                "module": "tests.test_app",
                "imports": [],
                "entrypoint_reasons": [],
            },
        ],
    )
    _write_json(existing_flow_path, existing_flow)

    plan = build_runtime_scenario_plan(
        dependency_graph_path=dependency_graph_path,
        manifest_summary_path=manifest_summary_path,
        manifest_path=manifest_path,
        output_dir=tmp_path,
        execution_flow_graph_path=existing_flow_path,
        max_entrypoints=4,
        max_seed_scenarios=3,
    )

    assert (tmp_path / "runtime_scenario_plan.json").exists()

    entrypoints = plan.get("entrypoints")
    assert isinstance(entrypoints, list)
    assert 3 <= len(entrypoints) <= 5
    assert "src/app.py" in entrypoints
    assert "scripts/cli_tool.py" in entrypoints
    assert all(not str(item).startswith("scenario:auto:") for item in entrypoints)

    forced_probes = plan.get("forced_probes")
    assert isinstance(forced_probes, list)
    assert forced_probes
    assert all(str(item).startswith("scenario:auto:") for item in forced_probes)

    call_adjacency = plan.get("call_adjacency")
    assert isinstance(call_adjacency, dict)
    assert call_adjacency

    scenarios = plan.get("scenarios")
    assert isinstance(scenarios, list)
    assert len(scenarios) <= 3
    assert all(not str(item.get("path", "")).startswith("tests/") for item in scenarios)

    summary = plan.get("summary")
    assert isinstance(summary, dict)
    assert str(summary.get("strategy", "")) == "focused_expansion"
    assert int(summary.get("selected_entrypoint_count", 0) or 0) >= 3
    assert int(summary.get("max_expansion_depth", 0) or 0) >= 3
    assert float(summary.get("baseline_coverage_ratio", 0.0)) > 0.0


def test_run_encoded_scenario_executes_function(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    module_path = repo / "appmod.py"
    module_path.write_text(
        "from pathlib import Path\n\n"
        "def ping():\n"
        "    Path(__file__).with_name('called_function.marker').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    entrypoint = encode_scenario_entrypoint(
        {
            "scenario_id": "scenario-0001",
            "kind": "function",
            "path": "appmod.py",
            "module": "appmod",
            "name": "ping",
        }
    )

    result = run_encoded_scenario(repo_path=repo, encoded_spec=_encoded_spec(entrypoint))

    assert bool(result.get("ok", False))
    assert (repo / "called_function.marker").exists()


def test_run_encoded_scenario_executes_class_method(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    module_path = repo / "worker.py"
    module_path.write_text(
        "from pathlib import Path\n\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        Path(__file__).with_name('called_class.marker').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    entrypoint = encode_scenario_entrypoint(
        {
            "scenario_id": "scenario-0002",
            "kind": "class",
            "path": "worker.py",
            "module": "worker",
            "name": "Worker",
        }
    )

    result = run_encoded_scenario(repo_path=repo, encoded_spec=_encoded_spec(entrypoint))

    assert bool(result.get("ok", False))
    assert (repo / "called_class.marker").exists()
