from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def load_runner_class():
    module_path = Path(__file__).with_name("verification_runner.py")
    spec = importlib.util.spec_from_file_location("verification_runner", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load verification_runner module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VerificationRunner


def run_fixture(
    runner_cls,
    name: str,
    graph: Dict[str, Any],
    entrypoints: List[str],
    resolver_data: Dict[str, List[Dict[str, Any]]],
    expect_valid: bool,
    expect_failure_domain: str | None,
) -> Dict[str, Any]:
    result = runner_cls(graph=graph, entrypoints=entrypoints, resolver_data=resolver_data).run()

    ok = result.get("system_valid") is expect_valid
    if expect_failure_domain is not None:
        ok = ok and (expect_failure_domain in result.get("failure_domains", []))

    return {
        "name": name,
        "ok": ok,
        "result": result,
    }


def main() -> int:
    VerificationRunner = load_runner_class()

    fixtures: List[Dict[str, Any]] = []

    fixtures.append(
        run_fixture(
            VerificationRunner,
            name="happy_path",
            graph={
                "nodes": [
                    {"id": "canonical://repo/_root:main.py", "metadata": {"type": "ENTRYPOINT"}},
                    {"id": "canonical://repo/_root:svc.py", "metadata": {"type": "DI_WIRED"}},
                    {"id": "canonical://repo/_root:util.py", "metadata": {}},
                ],
                "edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:svc.py",
                        "type": "DI",
                        "confidence": 0.9,
                    },
                    {
                        "from": "canonical://repo/_root:svc.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                        "confidence": 0.8,
                    },
                ],
            },
            entrypoints=["canonical://repo/_root:main.py"],
            resolver_data={
                "ast_edges": [
                    {
                        "from": "canonical://repo/_root:svc.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                    }
                ],
                "di_edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:svc.py",
                        "type": "DI",
                    }
                ],
                "config_edges": [],
                "heuristic_edges": [],
            },
            expect_valid=True,
            expect_failure_domain=None,
        )
    )

    fixtures.append(
        run_fixture(
            VerificationRunner,
            name="orphan_node_graph",
            graph={
                "nodes": [
                    {"id": "canonical://repo/_root:main.py", "metadata": {"type": "ENTRYPOINT"}},
                    {"id": "canonical://repo/_root:util.py", "metadata": {}},
                    {"id": "canonical://repo/_root:orphan_di.py", "metadata": {"type": "DI_WIRED"}},
                ],
                "edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                        "confidence": 0.8,
                    }
                ],
            },
            entrypoints=["canonical://repo/_root:main.py"],
            resolver_data={
                "ast_edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                    }
                ],
                "di_edges": [],
                "config_edges": [],
                "heuristic_edges": [],
            },
            expect_valid=False,
            expect_failure_domain="semantic",
        )
    )

    fixtures.append(
        run_fixture(
            VerificationRunner,
            name="di_node_missing_from_graph",
            graph={
                "nodes": [
                    {"id": "canonical://repo/_root:main.py", "metadata": {"type": "ENTRYPOINT"}},
                    {"id": "canonical://repo/_root:util.py", "metadata": {}},
                ],
                "edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                        "confidence": 0.8,
                    }
                ],
            },
            entrypoints=["canonical://repo/_root:main.py"],
            resolver_data={
                "ast_edges": [],
                "di_edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:missing_di.py",
                        "type": "DI",
                    }
                ],
                "config_edges": [],
                "heuristic_edges": [],
            },
            expect_valid=False,
            expect_failure_domain="resolver",
        )
    )

    fixtures.append(
        run_fixture(
            VerificationRunner,
            name="resolver_mismatch_ast_only_edge",
            graph={
                "nodes": [
                    {"id": "canonical://repo/_root:main.py", "metadata": {"type": "ENTRYPOINT"}},
                    {"id": "canonical://repo/_root:util.py", "metadata": {}},
                ],
                "edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                        "confidence": 0.8,
                    }
                ],
            },
            entrypoints=["canonical://repo/_root:main.py"],
            resolver_data={
                "ast_edges": [
                    {
                        "from": "canonical://repo/_root:util.py",
                        "to": "canonical://repo/_root:main.py",
                        "type": "IMPORT",
                    }
                ],
                "di_edges": [],
                "config_edges": [],
                "heuristic_edges": [],
            },
            expect_valid=False,
            expect_failure_domain="resolver",
        )
    )

    fixtures.append(
        run_fixture(
            VerificationRunner,
            name="disconnected_island_subgraph",
            graph={
                "nodes": [
                    {"id": "canonical://repo/_root:main.py", "metadata": {"type": "ENTRYPOINT"}},
                    {"id": "canonical://repo/_root:util.py", "metadata": {}},
                    {"id": "canonical://repo/_root:x.py", "metadata": {}},
                    {"id": "canonical://repo/_root:y.py", "metadata": {}},
                ],
                "edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                        "confidence": 0.8,
                    },
                    {
                        "from": "canonical://repo/_root:x.py",
                        "to": "canonical://repo/_root:y.py",
                        "type": "IMPORT",
                        "confidence": 0.7,
                    },
                    {
                        "from": "canonical://repo/_root:y.py",
                        "to": "canonical://repo/_root:x.py",
                        "type": "IMPORT",
                        "confidence": 0.7,
                    },
                ],
            },
            entrypoints=["canonical://repo/_root:main.py"],
            resolver_data={
                "ast_edges": [
                    {
                        "from": "canonical://repo/_root:main.py",
                        "to": "canonical://repo/_root:util.py",
                        "type": "IMPORT",
                    },
                    {
                        "from": "canonical://repo/_root:x.py",
                        "to": "canonical://repo/_root:y.py",
                        "type": "IMPORT",
                    },
                    {
                        "from": "canonical://repo/_root:y.py",
                        "to": "canonical://repo/_root:x.py",
                        "type": "IMPORT",
                    },
                ],
                "di_edges": [],
                "config_edges": [],
                "heuristic_edges": [],
            },
            expect_valid=False,
            expect_failure_domain="semantic",
        )
    )

    report = {
        "all_passed": all(item["ok"] for item in fixtures),
        "fixtures": fixtures,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
