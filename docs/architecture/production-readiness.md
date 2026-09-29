# Production readiness (`production-readiness-0.6.0`)

The readiness gate proves, before any spend, that everything downstream of the paid call can run. It is a workflow task between the paid request snapshot and the paid approval:

`PREPARE → CONCEPT-REVIEW → PAID-REQUEST → READINESS → PAID-GENERATION → PROCESS → VALIDATE → GODOT → FINAL-REVIEW → EVIDENCE`

## Checks (`DefaultReadinessProbes`)

| Category | Checks |
|---|---|
| Provider | the adapter implements the paid-request contract and can execute the snapshot; the credential is configured (`is_configured`, which is a local doctor for Meshy and **never** a paid call) |
| Blender | configured; exists and is a regular file; launches and reports a version; version ≥ `MIN_SUPPORTED_BLENDER_VERSION` (4.0.2); Python dependency preflight PASS |
| Godot | configured; exists, regular file, executable; launches and reports a version; major version 4 |
| Workspace | asset directory writable; managed scratch creatable; at least 256 MiB free |
| Profile | bound profile AVAILABLE; review views implemented (`PLACED_VIEWS`); runtime validations implemented |

A failed prerequisite marks the later checks for the same tool FAIL with `prerequisite failed` rather than probing them. Collaborators (adapters, preflight, thresholds) are injectable for tests.

## Report

The report is persisted as the artifact `asset-production-readiness-report` and in `production_readiness_reports`. It contains the schema, asset, revision, profile, snapshot hash, result, checks, tool identities (resolved path, size, version), passing capabilities, environment (platform and Python version only) and `generated_at`. It contains no environment variables and no secrets.

A result of `FAIL` raises `PRODUCTION_READINESS_FAILED`, a retryable tool error. No paid approval is created and nothing is spent. After fixing the configuration, `gamefactory retry <workflow> <workflow>-READINESS` re-evaluates. The report hash is bound into the paid approval, so a re-run requires a fresh approval.

## Freshness recheck before the paid POST

Immediately before a **new** submission (no provider intent yet), `recheck_critical` runs cheap checks that launch nothing: adapter compatibility, provider credential, Blender and Godot executables still present, workspace writable. A failure writes `PRODUCTION_READINESS_RECHECK_FAILED` and stops before the POST. The approval stays historical and the reservation is released through the no-submission path. Once the problem is fixed, `retry` proceeds with the same approval. Query-only recovery of an existing intent is never blocked by the recheck.

## Doctor

`gamefactory doctor` reports "Production Readiness Capabilities" (Provider, Blender, Godot, Workspace) from the same probes. The provider is marked as checked at workflow time against the approved snapshot.
