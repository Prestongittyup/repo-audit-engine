from __future__ import annotations

import json
from pathlib import Path

from repo_audit_engine.runtime.bubble_executor import execute_runtime_bubble


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
    assert int(flow_summary.get("run_count", 0)) == 1
    assert int(flow_summary.get("call_event_count", 0)) >= 1

    trace_path = output / "runtime_trace.jsonl"
    rows = [line for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows, "runtime_trace.jsonl should contain streamed runtime events."

    sample = json.loads(rows[0])
    assert isinstance(sample.get("event"), str)
    assert isinstance(sample.get("timestamp"), str)

    # Bubble execution must not pollute the source repo environment.
    assert not (repo / "__pycache__").exists()
