"""Standard workflow definitions for demo, failure, and paid safety testing."""

from gamefactory.core.domain.models import (
    CostClass,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
)


def create_demo_workflow(project_id: str) -> tuple[Workflow, list[Task]]:
    """Create the standard 6-task V0.1 demo workflow.

    1. inspect_project (local read)
    2. generate_concept (fake generation, creates artifact)
    3. validate_concept (artifact integrity check)
    4. fake paid_asset_generation (PAID cost class, blocks for approval)
    5. controlled_execution (runs local command safely)
    6. record_evidence (evaluates quality gate and completes)
    """
    wf_id = generate_id("WF-DEMO")
    wf = Workflow(
        id=wf_id,
        project_id=project_id,
        name="Demo End-to-End Workflow",
        status=WorkflowStatus.PENDING,
    )

    tasks = [
        Task(
            id=f"{wf_id}-T1",
            workflow_id=wf_id,
            name="Inspect Project Structure",
            task_type="inspect_project",
            cost_class=CostClass.LOCAL,
            depends_on=[],
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T2",
            workflow_id=wf_id,
            name="Generate Asset Concept",
            task_type="generate_concept",
            cost_class=CostClass.LOCAL,
            depends_on=[f"{wf_id}-T1"],
            parameters={"concept_name": "Heavy Mech Enemy", "poly_budget": 15000},
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T3",
            workflow_id=wf_id,
            name="Validate Concept Artifact",
            task_type="validate_artifact",
            cost_class=CostClass.LOCAL,
            depends_on=[f"{wf_id}-T2"],
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T4",
            workflow_id=wf_id,
            name="Paid 3D Asset Generation",
            task_type="paid_generation",
            cost_class=CostClass.PAID,
            depends_on=[f"{wf_id}-T3"],
            parameters={
                "prompt": "Heavy Mech Enemy 3D Model",
                "format": "glb",
                "cost": 5.0,
                "cost_unit": "fake_credits",
            },
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T5",
            workflow_id=wf_id,
            name="Controlled Subprocess Execution",
            task_type="controlled_command",
            cost_class=CostClass.LOCAL,
            depends_on=[f"{wf_id}-T4"],
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T6",
            workflow_id=wf_id,
            name="Record Evidence & Quality Gate",
            task_type="record_evidence",
            cost_class=CostClass.LOCAL,
            depends_on=[f"{wf_id}-T5"],
            status=TaskStatus.PENDING,
        ),
    ]

    return wf, tasks


def create_failure_workflow(project_id: str) -> tuple[Workflow, list[Task]]:
    """Create a workflow with a deterministic failing task to test failure propagation and retry."""
    wf_id = generate_id("WF-FAIL")
    wf = Workflow(
        id=wf_id,
        project_id=project_id,
        name="Deterministic Failure Workflow",
        status=WorkflowStatus.PENDING,
    )

    tasks = [
        Task(
            id=f"{wf_id}-T1",
            workflow_id=wf_id,
            name="Pre-check Task",
            task_type="inspect_project",
            cost_class=CostClass.LOCAL,
            depends_on=[],
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T2",
            workflow_id=wf_id,
            name="Deterministic Failing Task",
            task_type="simulated_failure",
            cost_class=CostClass.LOCAL,
            depends_on=[f"{wf_id}-T1"],
            parameters={"fail_attempts": 1, "error": "Simulated upstream transient failure"},
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T3",
            workflow_id=wf_id,
            name="Downstream Dependent Task",
            task_type="inspect_project",
            cost_class=CostClass.LOCAL,
            depends_on=[f"{wf_id}-T2"],
            status=TaskStatus.PENDING,
        ),
    ]

    return wf, tasks


def create_paid_safety_workflow(project_id: str) -> tuple[Workflow, list[Task]]:
    """Create a workflow with a paid task to verify 0 calls without approval and 1 call with approval."""
    wf_id = generate_id("WF-PAID")
    wf = Workflow(
        id=wf_id, project_id=project_id, name="Paid Safety Workflow", status=WorkflowStatus.PENDING
    )

    tasks = [
        Task(
            id=f"{wf_id}-T1",
            workflow_id=wf_id,
            name="Preparation Task",
            task_type="inspect_project",
            cost_class=CostClass.LOCAL,
            depends_on=[],
            status=TaskStatus.PENDING,
        ),
        Task(
            id=f"{wf_id}-T2",
            workflow_id=wf_id,
            name="Paid Generation Task",
            task_type="paid_generation",
            cost_class=CostClass.PAID,
            depends_on=[f"{wf_id}-T1"],
            parameters={"prompt": "Space Cruiser", "cost": 10.0, "cost_unit": "fake_credits"},
            status=TaskStatus.PENDING,
        ),
    ]

    return wf, tasks
