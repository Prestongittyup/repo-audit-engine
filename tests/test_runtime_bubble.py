from __future__ import annotations

import base64
import json
from pathlib import Path

from repo_audit_engine.runtime.bubble_executor import execute_runtime_bubble


def _encode_probe(spec: dict) -> str:
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"scenario:auto:{token}"


def test_runtime_bubble_disabled_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    output = tmp_path / "out"
    result = execute_runtime_bubble(
        repo_path=repo,
        output_dir=output,
        entrypoints=[],
        bubble_mode=False,
    )

    summary = result.get("flow_graph", {}).get("summary", {})
    assert (output / "runtime_trace.jsonl").exists()
    assert int(summary.get("run_count", 0)) == 0


def test_runtime_bubble_streaming_trace_and_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text(
        "def helper():\n"
        "    return 1\n\n"
        "def main():\n"
        "    helper()\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )

    output = tmp_path / "out"
    result = execute_runtime_bubble(
        repo_path=repo,
        output_dir=output,
        entrypoints=["main.py"],
        bubble_mode=True,
        timeout_seconds=15,
        memory_cap_mb=128,
        max_events=5000,
        max_depth=120,
    )

    flow_summary = result.get("flow_graph", {}).get("summary", {})
    assert int(flow_summary.get("run_count", 0)) >= 1
    assert int(flow_summary.get("call_event_count", 0)) >= 1

    trace_path = output / "runtime_trace.jsonl"
    rows = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "runtime_trace.jsonl should contain streamed runtime events."

    sample = json.loads(rows[0])
    assert isinstance(sample.get("event"), str)
    assert isinstance(sample.get("timestamp"), str)

    # Bubble execution must not pollute the source repo environment.
    assert not (repo / "__pycache__").exists()


def test_runtime_bubble_labels_run_outcomes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "import_only.py").write_text("import math\n", encoding="utf-8")
    (repo / "crash.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    output = tmp_path / "out"
    result = execute_runtime_bubble(
        repo_path=repo,
        output_dir=output,
        entrypoints=["import_only.py", "crash.py"],
        bubble_mode=True,
        timeout_seconds=10,
        memory_cap_mb=128,
        max_events=2000,
        max_depth=120,
    )

    runs = result.get("flow_graph", {}).get("entrypoint_runs", [])
    assert isinstance(runs, list) and runs

    by_entrypoint = {str(item.get("entrypoint", "")): item for item in runs if isinstance(item, dict)}
    assert str(by_entrypoint.get("import_only.py", {}).get("scenario_result", "")) in {"IMPORT_ONLY", "NO_CALL_ACTIVITY"}
    assert str(by_entrypoint.get("crash.py", {}).get("scenario_result", "")) == "CRASHED"

    summary = result.get("flow_graph", {}).get("summary", {})
    assert int(summary.get("crashed_runs", 0) or 0) >= 1


def test_runtime_bubble_continues_from_discovered_nodes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "main.py").write_text(
        "from worker import run_worker\n\n"
        "def entry():\n"
        "    run_worker()\n\n"
        "if __name__ == '__main__':\n"
        "    entry()\n",
        encoding="utf-8",
    )
    (repo / "worker.py").write_text(
        "def run_worker():\n"
        "    return 1\n",
        encoding="utf-8",
    )

    worker_probe = _encode_probe(
        {
            "scenario_id": "probe-worker",
            "kind": "function",
            "path": "worker.py",
            "module": "worker",
            "name": "run_worker",
        }
    )

    runtime_plan = {
        "seed_entrypoints": ["main.py"],
        "call_adjacency": {
            "function:main.py:entry": ["function:worker.py:run_worker"],
        },
        "node_probe_map": {
            "function:worker.py:run_worker": {
                "entrypoint": worker_probe,
                "node_id": "function:worker.py:run_worker",
                "priority_score": 180.0,
            }
        },
        "node_metrics": {
            "function:worker.py:run_worker": {
                "inbound_edges": 1,
                "outbound_edges": 0,
                "graph_centrality": 1.0,
                "low_inbound_score": 0.5,
                "unexplored_neighbors": 0,
            }
        },
        "summary": {
            "max_expansion_depth": 3,
            "max_followups_per_node": 2,
        },
    }

    output = tmp_path / "out"
    result = execute_runtime_bubble(
        repo_path=repo,
        output_dir=output,
        entrypoints=["main.py"],
        bubble_mode=True,
        timeout_seconds=10,
        memory_cap_mb=128,
        max_events=3000,
        max_depth=120,
        runtime_plan=runtime_plan,
    )

    runs = result.get("flow_graph", {}).get("entrypoint_runs", [])
    assert isinstance(runs, list)
    assert len(runs) >= 2

    continuation_runs = [row for row in runs if str(row.get("schedule_source", "")) == "continuation"]
    assert continuation_runs

    summary = result.get("flow_graph", {}).get("summary", {})
    assert int(summary.get("continuation_scheduled_targets", 0) or 0) >= 1


def test_runtime_bubble_timeout_keeps_partial_trace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "hang.py").write_text(
        "def spin():\n"
        "    while True:\n"
        "        pass\n\n"
        "spin()\n",
        encoding="utf-8",
    )

    output = tmp_path / "out"
    result = execute_runtime_bubble(
        repo_path=repo,
        output_dir=output,
        entrypoints=["hang.py"],
        bubble_mode=True,
        timeout_seconds=1,
        memory_cap_mb=128,
        max_events=2000,
        max_depth=120,
    )

    runs = result.get("flow_graph", {}).get("entrypoint_runs", [])
    assert isinstance(runs, list) and runs
    first_run = runs[0]

    assert str(first_run.get("scenario_result", "")) in {"PARTIAL_SUCCESS", "TIMEOUT"}
    if str(first_run.get("scenario_result", "")) == "PARTIAL_SUCCESS":
        assert bool(first_run.get("partial_trace_committed", False))

    summary = result.get("flow_graph", {}).get("summary", {})
    assert int(summary.get("timeout_count", 0) or 0) >= 1
