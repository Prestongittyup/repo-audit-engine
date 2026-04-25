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
