"""Workflow definitions and execution engine package with lazy public exports."""

from typing import Any

__all__ = [
    "WorkflowEngine",
    "WorkflowExecutionResult",
    "create_demo_workflow",
    "create_failure_workflow",
    "create_paid_safety_workflow",
]


def __getattr__(name: str) -> Any:
    if name in {"WorkflowEngine", "WorkflowExecutionResult"}:
        from gamefactory.workflows.engine import WorkflowEngine, WorkflowExecutionResult

        return {
            "WorkflowEngine": WorkflowEngine,
            "WorkflowExecutionResult": WorkflowExecutionResult,
        }[name]
    if name in {"create_demo_workflow", "create_failure_workflow", "create_paid_safety_workflow"}:
        from gamefactory.workflows import definitions

        return getattr(definitions, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
