# ADR 0003: Workflow Execution Model

## Status
Accepted

## Context
Factory Core V0.1 orchestrates multi-step development tasks represented as Directed Acyclic Graphs (DAGs). Workflows involve dependencies, state transitions, human approval gates, policy evaluations, and execution attempts across separate CLI invocations.
A critical safety requirement is cost safety: paid external operations must never be triggered without explicit approval, and crashes occurring during or after external operations must never blindly reinvoke paid APIs. Furthermore, running workflows must be protected from competing concurrent runners.

## Decision
We implement a **bounded, sequential, deterministic DAG workflow engine** with durable state persistence. Controlled command tasks run in child processes with timeouts; Python handlers and the local fake provider run in the orchestrator process.

Key design points:
1. **DAG Representation & Validation**:
   - Workflows define tasks with explicit dependencies (`depends_on: list[str]`).
   - Cycles are detected upfront using topological sort (`kahn` / DFS cycle check) before execution begins.
   - Fan-out, fan-in, and independent subgraphs are modeled.
2. **State Machine Invariants**:
   - Tasks follow strict states: `PENDING` -> `RUNNING` -> `COMPLETED` | `FAILED` | `BLOCKED`.
   - Workflows follow strict states: `PENDING` -> `RUNNING` -> `COMPLETED` | `FAILED` | `BLOCKED`.
   - Transitions are enforced centrally in `TaskStateMachine` and `WorkflowStateMachine`.
3. **Approval Gates & Input Binding**:
   - An approval requirement blocks the task (`BLOCKED` state) and persists an `ApprovalRequest`.
   - The approval record immutably binds the operation inputs and fingerprint (`operation_hash`).
   - If inputs change between approval and execution, the approval is invalidated.
4. **Crash Reconciliation & Paid Operation Safety**:
   - A task claim and `RUNNING` execution attempt are committed atomically before dispatch. The append-only invocation ledger is written immediately before entering the provider adapter.
   - If a paid call may have started and the process fails before a durable result, the attempt becomes `UNCERTAIN`; subsequent `resume` or `retry` **must block for reconciliation** rather than blindly re-executing it.
   - The CLI demonstrator uses only a fake provider. Its durable invocation ledger is verified as 0 before approval, 1 after approved resume, and still 1 after reopening the project in a new process.
5. **Execution Locks**:
   - To prevent multiple local CLI runners from executing the same workflow simultaneously, the runner acquires an OS-backed advisory lock in `.gamefactory/locks/`. Task claims are conditional database transactions; the OS lock is not a database lease.
   - There is no lease refresh or heartbeat. The operating system releases the advisory lock on process death; persistent workflow and attempt state are reconciled before resume.
6. **Retry Semantics**:
   - Retries create a new execution attempt with incremented `attempt_number`.
   - Failed attempts and evidence remain preserved in the audit history.

## Consequences
### Positive
- Strict determinism and auditability: every state transition is recorded and verified.
- Policy checks block paid tasks until approval, and uncertain calls block for explicit reconciliation. V0.1 demonstrates this with a fake provider; it does not guarantee exactly-once behavior from an external service.
- Recovery across CLI processes uses persisted state and execution history.

### Negative / Trade-offs
- Sequential execution in V0.1 does not execute independent branches concurrently.
- Local SQLite and OS locks do not provide a distributed worker or remote-host coordination service.

## Alternatives Considered
1. **Adopting Celery / Temporal / Airflow**:
   - *Pros*: Out-of-the-box distributed workflow management and web UI.
   - *Cons*: Heavyweight daemon architectures requiring external brokers (Redis/RabbitMQ/PostgreSQL); directly violates V0.1 local-first and zero-server architecture rules.
2. **In-Memory Async Event Loop (asyncio-only state)**:
   - *Pros*: Low latency, simple code for single-process runs.
   - *Cons*: Does not survive process termination; cannot safely handle CLI command restarts, resume, or multi-process command coordination.
