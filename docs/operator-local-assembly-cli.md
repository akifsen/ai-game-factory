# Local V0.7 assembly operator commands

These commands retain an authored GLB as an immutable local source package,
verify its provenance pin, and create a local-only V0.7 assembly workflow. They
do not call an asset provider. The `assembly create` parser deliberately has no
provider or paid-generation options.

The V0.7 specification must bind to a V0.7 assembly profile already available
in the built-in profile registry. At present the public registry has no
available V0.7 assembly profile, so `assembly ingest` and `assembly create`
will stop before retaining files or allocating workflow state until one is
accepted into that registry. Tests can inject a private fixture registry; this
does not enable custom profile activation for operators.

## Retain and verify a source

Keep the specification and source under the initialized project directory.
Choose a new package directory that does not already exist:

```powershell
gamefactory --project . assembly ingest `
  --spec specs/tank-v07.yaml `
  --source authored/tank.glb `
  --package sources/tank-r001 `
  --actor "Operator Name" `
  --reason "Initial authored assembly import" `
  --authoring-tool Blender `
  --authoring-tool-version 5.2.1 `
  --source-front=-Z
```

Save the returned `provenance_sha256` outside the package through the
organization's normal review process. Every verification or workflow creation
requires that explicit pin:

```powershell
gamefactory --project . assembly verify `
  --spec specs/tank-v07.yaml `
  --package sources/tank-r001 `
  --expected-provenance-sha256 <reviewed-sha256>
```

`verify` checks the retained source bytes, provenance sidecar, publication
marker, and specification fingerprint against the supplied digest. A changed
source is a new package and source revision; it must not overwrite an existing
package.

## Create and advance a workflow

Supply a concept PNG and its provenance document. An optional workflow ID
allows an exact retry to find the existing workflow; reusing that ID with a
changed source/specification/profile/concept binding is rejected.

```powershell
gamefactory --project . assembly create `
  --spec specs/tank-v07.yaml `
  --package sources/tank-r001 `
  --expected-provenance-sha256 <reviewed-sha256> `
  --concept concepts/tank.png `
  --concept-provenance concepts/tank-provenance.json `
  --workflow-id tank-assembly-r001
```

Creation runs only to the first required human checkpoint. Inspect the
workflow's pending approval, then approve as a named reviewer:

```powershell
gamefactory --project . approvals --workflow tank-assembly-r001
gamefactory --project . approve <approval-id> --actor "Reviewer Name" --comment "Reviewed"
gamefactory --project . resume tank-assembly-r001
```

Concept, authored-source, and final visual-review checkpoints remain mandatory.
Each invocation rebuilds the local handlers from persisted immutable
specification, profile, source-package, concept, and task-graph pins; a mismatch
stops the command. Once the final gate is approved, advance through the
assembly evidence task with:

```powershell
gamefactory --project . assembly export tank-assembly-r001
```

The command reports a usable evidence manifest only after the separate cold
export verification completes successfully. Repeating `assembly export` after
the workflow has completed is read-only: it checks the latest processing,
validation, runtime and evidence attempts across all statuses and requires each
to be completed. It rechecks current human approval fingerprints and pinned
project artifacts, then verifies the existing bundle with the packaged cold
verifier and the checkout copy when available. It does not rerun tasks or
replace the bundle. Any changed or missing project artifact, approval, attempt,
manifest, or bundle file makes the
revalidation fail. Approval records are never created automatically.
