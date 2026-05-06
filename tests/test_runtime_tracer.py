from __future__ import annotations

from repo_audit_engine.runtime.tracer import _resolve_return_stack_state


def test_return_stack_state_pops_only_matching_top_frame() -> None:
    call_stack = ["file:src/app.py", "function:src/app.py:main"]

    caller_node_id, depth, popped = _resolve_return_stack_state(call_stack, "function:src/app.py:main")

    assert popped is True
    assert caller_node_id == "file:src/app.py"
    assert depth == 2
    assert call_stack == ["file:src/app.py"]


def test_return_stack_state_preserves_stack_on_mismatch() -> None:
    call_stack = ["function:src/app.py:main"]

    caller_node_id, depth, popped = _resolve_return_stack_state(call_stack, "function:src/app.py:helper")

    assert popped is False
    assert caller_node_id == "function:src/app.py:main"
    assert depth == 1
    assert call_stack == ["function:src/app.py:main"]


def test_return_stack_state_handles_empty_stack() -> None:
    call_stack: list[str] = []

    caller_node_id, depth, popped = _resolve_return_stack_state(call_stack, "function:src/app.py:main")

    assert popped is False
    assert caller_node_id == ""
    assert depth == 0
    assert call_stack == []
