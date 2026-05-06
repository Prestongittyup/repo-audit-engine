from __future__ import annotations

from pathlib import Path

from repo_audit_engine.pipeline.orchestrator import _default_bubble_entrypoints


def test_default_bubble_entrypoints_prefers_manifest_entrypoints(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")

    entrypoints = _default_bubble_entrypoints(
        repo_root=repo,
        manifest_summary={"entrypoints": ["app.py"]},
    )

    assert entrypoints == ["app.py"]


def test_default_bubble_entrypoints_falls_back_to_repo_main_module(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "__main__.py").write_text("print('ok')\n", encoding="utf-8")

    entrypoints = _default_bubble_entrypoints(repo_root=repo, manifest_summary={})

    assert entrypoints == ["__main__.py"]


def test_default_bubble_entrypoints_uses_probe_when_no_entrypoints_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    entrypoints = _default_bubble_entrypoints(repo_root=repo, manifest_summary={})

    assert entrypoints == ["scenario:depth-probe"]
