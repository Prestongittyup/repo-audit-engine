from __future__ import annotations

import json
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


def test_manifest_ignore_patterns_are_loaded_from_engine_config_not_target_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text("def main():\n    return 1\n", encoding="utf-8")

    target_config = repo / "config"
    target_config.mkdir(parents=True, exist_ok=True)
    (target_config / "ignore_patterns.txt").write_text("# directories\ncustom_dir\n", encoding="utf-8")

    (repo / "custom_dir").mkdir(parents=True, exist_ok=True)
    (repo / "custom_dir" / "should_not_be_ignored.py").write_text("VALUE = 1\n", encoding="utf-8")

    output = tmp_path / "out"
    build_manifest(repo_path=repo, output_dir=output)

    manifest_rows = [
        json.loads(line)
        for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    paths = {str(row.get("path", "")) for row in manifest_rows if isinstance(row, dict)}

    assert "custom_dir/should_not_be_ignored.py" in paths


def test_main_module_filename_is_detected_as_entrypoint(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "__main__.py").write_text("print('hello')\n", encoding="utf-8")

    output = tmp_path / "out"
    result = build_manifest(repo_path=repo, output_dir=output)

    summary = result.get("summary", {}) if isinstance(result, dict) else {}
    entrypoints = summary.get("entrypoints") if isinstance(summary.get("entrypoints"), list) else []
    assert "__main__.py" in [str(item) for item in entrypoints]
