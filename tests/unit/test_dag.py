"""Unit tests for WorkflowDAG solver, validation, cycle detection, and scheduling."""

import pytest

from gamefactory.core.domain.dag import WorkflowDAG
from gamefactory.core.domain.errors import WorkflowError
from gamefactory.core.domain.models import Task


def make_task(task_id: str, depends_on: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        workflow_id="WF-TEST",
        name=f"Task {task_id}",
        task_type="test",
        depends_on=depends_on or [],
    )


class TestWorkflowDAG:
    def test_linear_dependencies(self) -> None:
        tasks = [
            make_task("A"),
            make_task("B", depends_on=["A"]),
            make_task("C", depends_on=["B"]),
        ]
        dag = WorkflowDAG(tasks)
        assert dag.topological_order == ["A", "B", "C"]
        assert [t.id for t in dag.get_roots()] == ["A"]

        # Scheduling
        ready = dag.get_ready_tasks(completed_task_ids=set(), in_progress_task_ids=set())
        assert [t.id for t in ready] == ["A"]

        ready_after_a = dag.get_ready_tasks(completed_task_ids={"A"}, in_progress_task_ids=set())
        assert [t.id for t in ready_after_a] == ["B"]

        ready_after_b = dag.get_ready_tasks(
            completed_task_ids={"A", "B"}, in_progress_task_ids=set()
        )
        assert [t.id for t in ready_after_b] == ["C"]

    def test_fan_out_and_fan_in(self) -> None:
        # A -> B, A -> C, (B, C) -> D
        tasks = [
            make_task("A"),
            make_task("B", depends_on=["A"]),
            make_task("C", depends_on=["A"]),
            make_task("D", depends_on=["B", "C"]),
        ]
        dag = WorkflowDAG(tasks)
        order = dag.topological_order
        assert order[0] == "A"
        assert set(order[1:3]) == {"B", "C"}
        assert order[3] == "D"

        ready = dag.get_ready_tasks(completed_task_ids={"A"}, in_progress_task_ids=set())
        assert {t.id for t in ready} == {"B", "C"}

        # If only B completed, D is not ready
        ready_b = dag.get_ready_tasks(completed_task_ids={"A", "B"}, in_progress_task_ids=set())
        assert [t.id for t in ready_b] == ["C"]

        # When both B and C completed, D is ready
        ready_bc = dag.get_ready_tasks(
            completed_task_ids={"A", "B", "C"}, in_progress_task_ids=set()
        )
        assert [t.id for t in ready_bc] == ["D"]

    def test_independent_tasks(self) -> None:
        tasks = [make_task("A"), make_task("B"), make_task("C")]
        dag = WorkflowDAG(tasks)
        assert len(dag.get_roots()) == 3
        ready = dag.get_ready_tasks(completed_task_ids=set(), in_progress_task_ids=set())
        assert {t.id for t in ready} == {"A", "B", "C"}

    def test_direct_cycle_detected(self) -> None:
        tasks = [
            make_task("A", depends_on=["B"]),
            make_task("B", depends_on=["A"]),
        ]
        with pytest.raises(WorkflowError, match="Dependency cycle detected"):
            WorkflowDAG(tasks)

    def test_indirect_cycle_detected(self) -> None:
        tasks = [
            make_task("A", depends_on=["C"]),
            make_task("B", depends_on=["A"]),
            make_task("C", depends_on=["B"]),
        ]
        with pytest.raises(WorkflowError, match="Dependency cycle detected"):
            WorkflowDAG(tasks)

    def test_self_dependency_detected(self) -> None:
        tasks = [make_task("A", depends_on=["A"])]
        with pytest.raises(WorkflowError, match="cannot depend on itself"):
            WorkflowDAG(tasks)

    def test_missing_dependency_detected(self) -> None:
        tasks = [make_task("A", depends_on=["NON_EXISTENT"])]
        with pytest.raises(WorkflowError, match="depends on non-existent task"):
            WorkflowDAG(tasks)

    def test_duplicate_task_id_detected(self) -> None:
        tasks = [make_task("A"), make_task("A")]
        with pytest.raises(WorkflowError, match="Duplicate task IDs"):
            WorkflowDAG(tasks)

    def test_downstream_query_and_failure_propagation(self) -> None:
        tasks = [
            make_task("A"),
            make_task("B", depends_on=["A"]),
            make_task("C", depends_on=["B"]),
            make_task("D", depends_on=["A"]),
        ]
        dag = WorkflowDAG(tasks)
        # Transitive downstream of A: B, C, D
        assert dag.get_all_downstream("A") == {"B", "C", "D"}
        # Transitive downstream of B: C
        assert dag.get_all_downstream("B") == {"C"}
        # Transitive downstream of D: empty
        assert dag.get_all_downstream("D") == set()
