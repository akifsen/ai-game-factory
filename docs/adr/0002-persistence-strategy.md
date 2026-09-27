# ADR 0002: Persistence Strategy

## Status
Accepted

## Context
AI Game Factory requires a durable local-first persistence layer to store:
- Projects and configuration states
- Workflows, tasks, and task execution attempts
- Human approvals and decision history
- Artifact metadata and independent SHA-256 hashes
- Verification evidence and gate evaluations
- Append-only audit events
- Cross-process workflow execution locks

The storage must survive process restarts, prevent multi-process race conditions, enforce relational integrity via foreign keys, support versioned schema migrations, and never expose database implementation details to the core domain layer.

## Decision
We select **embedded SQLite 3 via the Python standard library `sqlite3`** stored within the project's `.gamefactory/state/factory.db` directory.

Key design choices:
1. **Foreign Key Enforcement**: `PRAGMA foreign_keys = ON;` is enabled on every connection.
2. **Write-Ahead Logging (WAL)**: `PRAGMA journal_mode = WAL;` and `PRAGMA busy_timeout = 5000;` for concurrent read/write resilience.
3. **Versioned Migrations**: Explicit migration scripts are recorded in a dedicated `schema_migrations` table, applied in order, and future database versions newer than the application are rejected.
4. **Append-Only Audit & Attempt History**:
   - `executions` table stores every attempt separately (`attempt_number` integer), preserving failed attempts upon retry.
   - `audit_events` stores append-oriented lifecycle, approval, retry, and blocking decisions with actor/time/state context; execution and invocation records supply the related attempt evidence.
5. **Inward Boundary Isolation**:
   - The core domain entities (`Project`, `Workflow`, `Task`, etc.) are pure dataclasses with zero imports of `sqlite3`.
   - The V0.1 workflow façade composes concrete repositories in `gamefactory.adapters.persistence`; the domain never imports them. A separate repository Protocol per table is deliberately avoided until another storage implementation needs one.
6. **Execution Locks**: Workflow runners take an OS-backed advisory lock in `.gamefactory/locks/`. Lock metadata records PID, host, and claim timestamp. The OS releases ownership when a process exits; there is no heartbeat or expiring database lease in V0.1.

## Consequences
### Positive
- Zero external database server dependencies (Postgres, Redis, etc. are unnecessary for local-first developer tooling).
- Atomic ACID transactions for state transitions.
- Relational integrity preventing orphaned tasks, executions, or artifacts.
- Easy local inspection; backups must include/checkpoint active WAL state or use SQLite's backup API, rather than copying only an active database file.

### Negative / Trade-offs
- File-level locking requires managing busy timeouts when multiple CLI commands (e.g. status while run is active) access the database.
- Migration management must be rigorously tested to handle schema evolution cleanly.

## Alternatives Considered
1. **Flat JSON / YAML Files**:
   - *Pros*: Human-readable on disk, easily inspected with text editors.
   - *Cons*: High risk of corruption on process crashes during writes; lack of relational integrity (orphans); difficult atomic locks across concurrent CLI processes; poor query performance as execution history grows.
2. **Client-Server Database (PostgreSQL / MySQL)**:
   - *Pros*: Multi-user remote support, battle-tested concurrency.
   - *Cons*: Violates the non-negotiable local-first requirement; adds massive setup friction for a developer CLI tool.
3. **Object Document Store (DuckDB / TinyDB)**:
   - *Pros*: Embedded, light.
   - *Cons*: DuckDB is optimized for analytical OLAP rather than transactional ACID updates with foreign keys; TinyDB lacks relational integrity and ACID guarantees.
