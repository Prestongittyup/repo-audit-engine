from __future__ import annotations

import json
from pathlib import Path

from repo_audit_engine.analysis.static_analyzer import run_static_analysis
from repo_audit_engine.manifest.builder import build_manifest


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def test_call_resolution_uses_import_context_for_ambiguous_symbol_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "app.py").write_text(
        "from pkg.first import run\n"
        "import pkg.second as second\n\n"
        "def main():\n"
        "    run()\n"
        "    second.run()\n",
        encoding="utf-8",
    )
    (repo / "pkg").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "first.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "second.py").write_text("def run():\n    return 2\n", encoding="utf-8")

    output = tmp_path / "out"
    manifest_result = build_manifest(repo_path=repo, output_dir=output)
    manifest_path = Path(str(manifest_result.get("manifest_path", "")))

    static_result = run_static_analysis(repo_path=repo, manifest_path=manifest_path, output_dir=output)
    analysis_path = Path(str(static_result.get("analysis_path", "")))

    rows = _load_jsonl(analysis_path)
    app_row = next(item for item in rows if str(item.get("file_path", "")) == "app.py")

    calls = app_row.get("calls") if isinstance(app_row.get("calls"), list) else []
    by_callee = {
        str(item.get("callee", "")): str(item.get("resolved_node_id", ""))
        for item in calls
        if isinstance(item, dict)
    }

    assert by_callee.get("run") == "function:pkg/first.py:run"
    assert by_callee.get("second.run") == "function:pkg/second.py:run"
