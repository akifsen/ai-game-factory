"""Directed Acyclic Graph (DAG) validator and scheduler for workflows.

Handles dependency resolution, cycle detection, topological sorting,
fan-out, fan-in, and downstream failure propagation.
"""

from collections import deque

from gamefactory.core.domain.errors import WorkflowError
from gamefactory.core.domain.models import Task


class WorkflowDAG:
    """Manages task dependency relationships within a workflow."""

    def __init__(self, tasks: list[Task]) -> None:
        self.tasks: dict[str, Task] = {t.id: t for t in tasks}
        if len(self.tasks) != len(tasks):
            raise WorkflowError("Duplicate task IDs detected in workflow definition")

        self.adj_list: dict[str, list[str]] = {t.id: [] for t in tasks}
        self.in_degree: dict[str, int] = {t.id: 0 for t in tasks}
        self._build_graph()
        self.topological_order: list[str] = self._validate_and_sort()

    def _build_graph(self) -> None:
        for task_id, task in self.tasks.items():
            for dep_id in task.depends_on:
                if dep_id == task_id:
                    raise WorkflowError(
                        f"Task '{task_id}' cannot depend on itself",
                        details={"task_id": task_id},
                    )
                if dep_id not in self.tasks:
                    raise WorkflowError(
                        f"Task '{task_id}' depends on non-existent task '{dep_id}'",
                        details={"task_id": task_id, "missing_dependency": dep_id},
                    )
                self.adj_list[dep_id].append(task_id)
                self.in_degree[task_id] += 1

    def _validate_and_sort(self) -> list[str]:
        """Perform topological sort via Kahn's algorithm and detect cycles."""
        in_deg = dict(self.in_degree)
        queue: deque[str] = deque([t_id for t_id, deg in in_deg.items() if deg == 0])
        order: list[str] = []

        while queue:
            curr = queue.popleft()
            order.append(curr)
            for neighbor in self.adj_list[curr]:
                in_deg[neighbor] -= 1
                if in_deg[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(self.tasks):
            # A cycle exists. Find cycle components.
            unvisited = [t_id for t_id, deg in in_deg.items() if deg > 0]
            raise WorkflowError(
                f"Dependency cycle detected in workflow involving tasks: {', '.join(unvisited)}",
                details={"cyclic_tasks": unvisited},
            )

        return order

    def get_roots(self) -> list[Task]:
        """Return tasks with no dependencies."""
        return [self.tasks[t_id] for t_id, deg in self.in_degree.items() if deg == 0]

    def get_ready_tasks(
        self, completed_task_ids: set[str], in_progress_task_ids: set[str]
    ) -> list[Task]:
        """Return tasks whose dependencies are all satisfied and are not yet completed or running."""
        ready: list[Task] = []
        for t_id in self.topological_order:
            if t_id in completed_task_ids or t_id in in_progress_task_ids:
                continue
            task = self.tasks[t_id]
            if all(dep in completed_task_ids for dep in task.depends_on):
                ready.append(task)
        return ready

    def get_dependents(self, task_id: str) -> list[Task]:
        """Return immediate downstream tasks depending on task_id."""
        return [self.tasks[dep_id] for dep_id in self.adj_list.get(task_id, [])]

    def get_all_downstream(self, task_id: str) -> set[str]:
        """Return all transitive downstream task IDs."""
        downstream: set[str] = set()
        queue: deque[str] = deque(self.adj_list.get(task_id, []))
        while queue:
            curr = queue.popleft()
            if curr not in downstream:
                downstream.add(curr)
                queue.extend(self.adj_list.get(curr, []))
        return downstream
