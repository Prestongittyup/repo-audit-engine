from __future__ import annotations

from pathlib import Path

from repo_audit_engine.manifest.builder import build_manifest


def test_build_manifest_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    output = tmp_path / "out"
    result = build_manifest(repo_path=repo, output_dir=output)

    summary = result.get("summary", {})
    assert (output / "manifest.jsonl").exists()
    assert (output / "manifest_summary.json").exists()
    assert int(summary.get("python_file_count", 0)) >= 1
