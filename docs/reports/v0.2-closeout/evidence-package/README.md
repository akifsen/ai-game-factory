# V0.2 portable closeout evidence

Run from any empty directory with only this copied bundle and Python:

    python -I /path/to/bundle/verify_closeout_evidence.py verify /path/to/bundle

No source checkout, original Temp paths, gamefactory installation, Godot or network
is read by the verifier. manifest.json inventories every required byte/hash/size.
251 raw registered artifacts preserve original bytes under raw-artifacts/.
5 live-process sidecars are under live-process-evidence/. Acceptance and recovery
JSON are records/acceptance.json and records/recovery.json. Final source identity
is metadata/source-identity.json; wheel/executable hashes and command/results,
Windows live-crash, V0.1 comparison, tests and skips are under supporting/.
Remote run 36287573467 evidence is under remote-ci/. closeout-report.md and
work-plan.md are source documentation copies; their repo-relative links describe
the original docs layout, not extra required files for this verifier.

Semantic validation covers the selected positive workflow WF-GODOT-0f9e853f:
actual acceptance run membership, task/execution DAG, observation scenario and
execution ID, inspect artifact hashes and raw validation report association.
Other selected files receive hash/size validation. This is not a signature or
external attestation, and does not re-run gameplay or validate every historical
workflow's semantics. The final local result is APPROVED WITH COMMENTS / PARTIAL:
new Linux/CI revision results remain required. See supporting/README.md for scope.
