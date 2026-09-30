# V0.7 delivery and product readiness

This is the active continuation plan for the accepted ADR 0013–0018 architecture.
It does not advertise unimplemented profiles as available.

## Verified starting point

The September 30, 2026 local audit started at `68e32e9`. Tracked files were clean;
the untracked `.verify-pytest/` directory is outside this task's changes.
Compared with accepted Phase B `f79eb08`, the merge added only a second golden
dataset, generator, test suite and README. Production domain files were identical.
The authoritative compatibility snapshot remains
`tests/golden/v06_asset_profile_behavior.json`, exercised by
`tests/unit/test_v06_behavior_goldens.py`. The redundant suite is removed; its
skin/animation rejection coverage already exists in focused unit tests.

Initial focused verification: 218 tests passed (105 compatibility goldens,
107 V0.7 foundation tests, six forbidden-content tests). Local executable probes
found Godot 4.7.2 and Blender 5.2.1, including Blender dependency preflight.
Detection alone does not establish runtime or rendering acceptance.

Historical V0.4 `profile_version: null` acceptance remains intentional: non-null
values are rejected, and the field is absent from its dump and fingerprint.

## Delivery gates

Each implementation slice requires an independently inspected diff, meaningful
regression coverage, relevant runtime checks and a recorded quality decision.
Existing accepted goldens and historical evidence are never regenerated to hide
drift.

1. **Validator composition (Step 9):** closed typed rules, one decoded context,
   explicit legacy finding order, unchanged parser/preflight/public behavior.
   Advanced groups fail closed until implemented. Prove order sensitivity with an
   isolated mutation experiment and restore the implementation.
2. **Review placements:** implement all ADR 0014 directions in Python and Godot;
   remove unknown-view fallbacks, preserve `side`, test parity and real captures.
3. **Geometry contracts:** implement shared semantic part tree, pivot, socket,
   capsule and orientation rules with deterministic positive/negative fixtures.
4. **Authored source processing:** retain and hash-bind source/provenance;
   explicitly normalize only declared +Z sources once about +Y; preserve
   hierarchy and local transforms in Blender.
5. **Godot verification:** preserve parts and Marker3D sockets, build declared
   collision shapes and independently verify articulation/restoration.
6. **Production integration:** introduce vehicle, weapon, aircraft and character
   data only as their complete processing/runtime/evidence path becomes verified.
   Meshy assemblies fail before snapshot/intent creation. Rigged character stays
   unsupported.
7. **Evidence:** version new processing/evidence contracts independently; verify
   retained source, normalization, groups, captures and tampering offline. Keep
   V0.4–V0.6 bundles verifiable without rewriting them.
8. **Release candidate:** clean wheel install and packaged resources, documented
   CLI journeys, full lint/format/type/test checks, real Blender/Godot acceptance,
   supported-platform verification and accurate release/operator documentation.

## Definition of product ready

Readiness requires the advertised CLI journeys to work from an installed wheel,
the supported profiles to complete their production path with current-attempt
evidence, recovery and accounting invariants to hold, all meaningful negative
cases to fail explicitly, and the declared platform checks to pass. Tests skipped
for missing tools are not runtime evidence. Remaining limits and unsupported
features must be visible to operators.

Concept, paid-operation and final visual decisions remain human checkpoints.
Development acceptance uses deterministic local fixtures and fake providers:
zero paid submissions, zero consumed credits, no live production database or
production artifact mutations. Software readiness never substitutes for a
pending human asset review.

The user authorized continued development and consultation with the shared
ChatGPT conversation. Advice from that conversation is input to engineering
review, not proof that repository code or external CI passes.

## Integration constraints from the continuation audit

The existing Blender processor rejects nested mesh transforms, joins meshes and
fits dimensions with independent axis scaling. Authored assemblies require a
separate processing path: no cross-part joining, preserved local pivots and
hierarchy, declared source-front normalization, and dimension mismatch failure
instead of nonuniform resizing. A hierarchy that imports successfully is not
sufficient evidence; a displaced pivot or socket must fail its own rule.

The first advanced production profile is vehicle. Weapon, aircraft and character
each require their own complete acceptance fixture before availability changes.
Character uses the accepted static/area runtime body contract and capsule policy;
this milestone does not add gameplay CharacterBody3D or rigging support.

The current workflow/exporter/cold verifier only implement the historical
provider path. Local-source evidence needs a mutually exclusive role set and
must never fabricate provider, paid approval or cost receipts. Parts-declaring
inputs must be rejected before any paid snapshot or provider intent is created.

The signed-in ChatGPT consultation on September 30 confirmed this order and
emphasized independent negative fixtures, articulation invariants and separate
availability gates. Repository review remains the acceptance authority.

Release acceptance additionally includes semantic reproducibility across two
fresh fixture runs, resource-limit/malformed-transform/extras rejection, and
operator diagnostics that identify the failed rule, source hash, profile and
execution attempt. Byte-identical Blender exports are required only if that
export path is demonstrated deterministic; semantic reports must agree.

Repeat the V0.6 paid snapshot, readiness, exactly-once, accounting, recovery,
concept-replacement and approval regressions after production integration.
For a local assembly, assert zero paid approvals, reservations, intents and POSTs.
Final real-provider production validation remains a separate human-controlled
operation; offline product readiness does not require spending credits.

## Semantic source trust boundary

The continuation design review confirmed that a processed mesh's centroid or
bounds cannot prove that an authored part's geometry was baked in the wrong
frame: valid off-center parts can have the same measurements. V0.7 therefore
uses `pivot.collapsed` for the provable nonzero-declaration/zero-observation case,
with separate position, basis and motion-axis findings. It adds no geometric
heuristic and performs no automatic re-pivoting or resizing.

Human source review must bind the retained source hash and declared semantic
contracts. Automated acceptance compares source, processed LOD0 and Godot
geometry/transforms to prove preservation through the permitted front
normalization. It does not infer artistic or physical correctness of the
original authored pivot. A later independent landmark schema would be needed
for that stronger claim; it is outside V0.7. Source review is never automatic.

Role identifiers remain unique within an assembly, preserving Phase B behavior.
Generic repeated-role cardinality is deferred; profiles may define distinct
roles only for demonstrated requirements. All assembly dimensions must already
conform to the bound specification tolerance.

The first source slice is standalone bounded ingestion and verification, with
no database or workflow mutation. The later workflow bridge reuses the existing
write-once revision raw hash and artifact records: source version equals asset
revision, and changed source requires a new workflow/revision. The assembly
graph includes an explicit human `source_review` gate and omits every paid task.
Replay of malformed persisted tasks must also be rejected before paid snapshot,
approval, intent, reservation or provider side effects. Filesystem and SQLite
operations are not one transaction; retry must accept only identical pinned
source/provenance records and reject ambiguity or overwrite.

## Continuation checkpoint: verified slices and open integration gates

Local checkpoints: `308d1d1` legacy validation composition, `796ddf7` camera
placements, `dccb395` retained authored source, `e5fdaa2` geometry preservation,
`d13dacd` Blender assembly processing, and `94ede3a` paid-stage rejection.
These are independently verified slices, not a V0.7 release designation.

The local assembly workflow, atomic attempt publication and portable evidence
have passed independent review. Fresh real Blender/Godot runs reach COMPLETED
only after explicit fixture concept, source and final approvals and a portable
cold-verifier PASS. All five paid/accounting tables remain empty. Completed CLI
re-export is read-only and rejects changed bundle captures, current project
artifacts, newer failed attempts and changed approvals. Cold verification now
recomputes actual LOD/material/embedded-texture budgets and accepts a fully
rebound textured bundle. Byte-pinned fixtures retain exact bytes on Windows Git
checkouts through a scoped `.gitattributes` rule.

Additional local checkpoints: `108b153` atomic assembly graph, `9504568`
standalone character Blender processing, `50bcb18` static capsule Godot runtime,
`05b58df` closed vehicle candidate and `4a77bb3` local assembly evidence.
Independent verification includes 95 exporter/CLI/workflow tests, 236 runtime,
profile and frozen-golden tests, a fresh full assembly workflow, and a fresh Git
checkout cold-verifier PASS. These results approve bounded slices, not the
complete V0.7 release.

The legacy catalog remains frozen; an explicit independent V0.7 registry and
`asset profiles --contract-version 0.7.0` catalog are initially closed. Vehicle
has passed two isolated installed-wheel CLI journeys with matching semantic
model/capture hashes and read-only tamper rejection. Closed weapon and aircraft
candidates (`7c7336a`) passed 221 independent unit/frozen-golden tests and two
fresh real Blender/Godot human-gated workflows through 29-file cold evidence,
with zero rows in all five paid/accounting tables. Their public activation and
final installed-wheel acceptance remain open.
The provider-character bridge is undergoing security and current-attempt
revisions. Its evidence task remains fail-closed until a separate provider
evidence branch binds the current paid snapshot, readiness, approvals, provider
intent, raw output, processing, runtime and terminal settled accounting.
UNKNOWN-cost reservations keep their historical behavior; the new V0.7
provider evidence gate must refuse completion while liability remains unresolved.

Registry availability remains closed until each advertised path passes
installed-wheel, real DCC, human-gate and evidence acceptance. Final verification
also requires semantic agreement between fresh runs, complete regression checks
and the supported Windows/Linux CI matrix. No remote CI success for these local
commits is claimed; permission to push and run remote CI is still pending.
