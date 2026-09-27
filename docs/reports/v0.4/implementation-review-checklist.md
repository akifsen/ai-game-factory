# V0.4 independent review constraints

This is a review checklist, not implementation acceptance. Read-only review of the
V0.3 foundation identified the following V0.4 extension hazards:

1. Existing provider invocation ledger records provider/hash/time only. V0.4 must
   persist operation, revision, concept hash, approval ID and estimated/unknown
   cost before submit; persist returned task ID before subsequent query/download.
2. Existing provider port has generate only. Known ID must query existing work;
   unknown ID after possible acceptance must remain UNCERTAIN without resubmit.
3. Existing cost floats default to zero and budget guard examines positive cost.
   UNKNOWN must not become free; explicitly reserve/cap unknown-cost operations.
4. Paid policy can be disabled in V0.3 configuration. Production V0.4 paid human
   gate must be mandatory independently of that optional setting.
5. Approval service accepts actor strings. Test actor is not human review;
   production and test evidence must be distinguishable and never conflated.
6. Existing CLI engine always chooses fake provider, including approval context.
   Create/approve/resume must resolve the same selected provider and fingerprints.
7. CLI 0.4.0 image-to-3d create supports --operation-id for a *local* journal,
   not server exactly-once. Prefer it as additional duplicate resistance only.
8. Actual Meshy create help defaults textures to 4k; the asset spec caps 2048.
   Explicit settings and decoded output validation are required. No second paid
   remesh/retexture operation is authorized in this task.
9. Godot baseline sandbox failed on Windows root certificate store access; host
   execution is being verified. Do not filter ERROR lines to hide that failure.

Read-only tools verified Node 24.13.0, meshy-cli 0.4.0, production Meshy API origin,
authenticated existing environment credential (value omitted), and free status
request. No paid call was made. Current balance is not an operation cost estimate.
