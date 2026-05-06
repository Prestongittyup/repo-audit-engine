from __future__ import annotations

from pathlib import Path

from repo_audit_engine.pipeline.orchestrator import run_staged_pipeline


def test_full_pipeline_manifest_only_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('ok')\n", encoding="utf-8")

    output = tmp_path / "out"
    payload = run_staged_pipeline(
        repo_path=repo,
        output_dir=output,
        bubble_mode=False,
        mode="manifest-only",
    )

    assert payload.get("summary", {}).get("status") == "PASSED"
    assert (output / "manifest.jsonl").exists()


def test_full_pipeline_static_only_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("import util\n\nutil.run()\n", encoding="utf-8")
    (repo / "util.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    output = tmp_path / "out"
    payload = run_staged_pipeline(
        repo_path=repo,
        output_dir=output,
        bubble_mode=False,
        mode="static-only",
    )

    assert payload.get("summary", {}).get("status") == "PASSED"
    assert (output / "manifest.jsonl").exists()
    assert (output / "static_analysis.jsonl").exists()
    assert (output / "dependency_graph.json").exists()


def test_full_pipeline_no_bubble_still_completes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("import util\n\nutil.run()\n", encoding="utf-8")
    (repo / "util.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")

    output = tmp_path / "out"
    payload = run_staged_pipeline(
        repo_path=repo,
        output_dir=output,
        bubble_mode=False,
        mode="full-pipeline",
    )

    root_cause = str(payload.get("summary", {}).get("root_cause", ""))
    assert "Runtime signal validation failed" not in root_cause
    assert (output / "heat_classification.json").exists()
    assert (output / "dead_code_report.json").exists()


def test_full_pipeline_bubble_treats_runtime_as_non_fatal_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    for index in range(25):
        (repo / f"module_{index}.py").write_text(f"def fn_{index}():\n    return {index}\n", encoding="utf-8")

    output = tmp_path / "out"
    payload = run_staged_pipeline(
        repo_path=repo,
        output_dir=output,
        bubble_mode=True,
        mode="full-pipeline",
        timeout_seconds=10,
        max_events=2000,
    )

    root_cause = str(payload.get("summary", {}).get("root_cause", ""))
    assert "Runtime signal validation failed" not in root_cause
    assert (output / "heat_classification.json").exists()
