# V0.7 character CLI candidate

`character@1` is the packaged V0.7 provider-generated, single-mesh character profile. The public CLI workflow uses the typed `character_test.yml` specification, a PNG concept, and its hashed provenance sidecar. Candidate catalog availability does not by itself claim an installed-wheel or cross-platform release qualification.

Start with the packaged profile and specification, or equivalent project-local copies:

```text
gamefactory asset profiles --contract-version 0.7.0
gamefactory asset create --spec specs/character.yml --concept concepts/character.png --provenance concepts/character-provenance.json --provider fake
```

The concept sidecar must contain the SHA-256 digest of the PNG. The fake provider has a five-`fake_credits` estimate and still uses the same separate human approval gate as provider generation. The public workflow performs the configured production-readiness checks, Blender processing, independent GLB validation, and real Godot capture checks. The nine declared views must all be present and current before final visual review.

The workflow pauses for human concept review, paid-generation approval, and final visual review. Inspect the exact pending approval and the current artifacts before deciding. After each decision, resume the workflow:

```text
gamefactory approvals --workflow <workflow-id>
gamefactory approve <approval-id> --actor <reviewer> --comment <reason>
gamefactory resume <workflow-id>
```

Use `gamefactory artifacts --workflow <workflow-id>` to locate the runtime captures while final review is pending. They provide evidence for a human decision; their presence does not approve the workflow.

For a completed workflow, `gamefactory report --workflow <workflow-id>` and `gamefactory asset export <workflow-id>` run the current read-only evidence gate. They report the registered manifest, review page, verification timestamp, and cold-verifier result only when the current approvals, profile and specification pins, latest attempts are completed, all nine captures, and published bundle still match. They do not resume tasks, rewrite evidence, or use the legacy exporter. A changed or missing capture, changed profile binding, or newer failed attempt produces a controlled failure; inspect the current workflow state before taking another action.

SQLite may create its `factory.db-wal` and `factory.db-shm` auxiliary files while a read-only command opens a live WAL database. The database contents, evidence artifacts, and reports remain unchanged.

The cold-verifier result describes package consistency and readiness. Its authentication fields remain false; a verified bundle does not independently authenticate a human identity or provider, runtime, or capture origin.

On Windows, use a short project directory. Planned Blender paths, including generated staging filenames, must fit within 259 UTF-16 code units. An overlong plan is rejected before Blender starts or output directories are created; shorten the project, workflow, or asset path shown by the error.
