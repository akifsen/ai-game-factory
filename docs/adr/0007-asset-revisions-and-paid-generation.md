# ADR 0007: Asset revisions, paid operations and evidence boundaries

## Status

Accepted design for V0.4; implementation verification is recorded separately in
`docs/reports/v0.4-completion-report.md`.

## Context

The first asset pipeline crosses a local image provider, human review, a paid
Meshy request, Blender and Godot. A process can die between provider acceptance and
receipt of its task ID. Provider success cannot establish structural or visual
acceptance. A second asset lifecycle beside WorkflowEngine would create competing
authorities, while overwriting files would invalidate review provenance.

## Decision

1. WorkflowEngine and its tasks remain the only lifecycle authority. A monotonically
   allocated asset revision identifies its owning workflow, validated specification
   and immutable artifacts. User-visible asset phases are derived from that workflow.
2. Specification, concept image and provenance are canonical/hash-bound. Concept
   review, paid generation and final visual review use separate existing approval
   records. All content required for a decision is included in its fingerprint.
   A changed artifact requires a new decision. Test actors do not supply production
   human approvals. Rejection never submits a generation request.
3. Persist invocation intent before crossing the provider boundary: revision,
   provider, operation, concept hash, request fingerprint, approval reference and
   estimated cost (including UNKNOWN). Persist the accepted external ID immediately.
   Known ID recovery queries the same operation. Possible submission without a
   known ID remains UNCERTAIN and requires reconciliation, never automatic submit.
   This is duplicate resistance, not a provider exactly-once guarantee.
4. Costs unknown to the provider stay UNKNOWN. A free balance inquiry does not
   establish operation cost. Unknown estimates need an explicit budget decision;
   they are never implicitly zero or replaced with invented credits.
5. Raw model, processed model, executable validation, runtime observations, captures
   and review HTML are distinct durable artifacts. Scratch stages are expendable.
   DCC/engine retries reuse verified upstream artifacts and do not regenerate.
6. Blender runs a bounded background script through the existing process boundary.
   External GLB is untrusted: reject unsafe/external references before import, bound
   size/time, inspect real geometry and decode textures. Support the smallest safe
   static-prop GLB subset explicitly; unsupported features fail rather than silently
   pass. Process isolation here is not an OS security sandbox.
7. Stage a controlled Godot project. Actual runtime mesh, transform, bounds and
   physics observations plus actual rendered PNGs are required. Local processing
   output is not Godot evidence. Offline review and portable verification bind
   all resources and their relationships through a manifest.

## Consequences

Real asset approval may remain pending while implementation tests pass. Phase A
can exercise fake provider + real Blender/Godot without spending Meshy credits.
Phase B requires explicit human concept and paid decisions and a separate resume.
The CLI reuses approve/reject/resume/inspect/artifacts/report; no second approval
store or scheduler is introduced. The real game remains unchanged during staging.

## Alternatives rejected

- Auto-retry after ambiguous paid submission: duplicate credit consumption risk.
- Provider-reported success or Blender stdout as final acceptance: no independent oracle.
- One approval for concept, spending and runtime visuals: changes decision meaning.
- Independent mutable asset state machine: contradictory state/recovery authorities.
- General repair/remesh/rigging system: outside the first static-prop profile.
