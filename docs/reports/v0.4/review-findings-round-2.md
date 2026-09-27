# V0.4 independent review — round 2

Decision at review: **CHANGES REQUIRED**. This record preserves findings, not a
claim that every correction below has passed its final verification.

The lead ran 87 targeted tests successfully, including real Blender processing
and geometry mutations (`lead-review02.xml`). Source inspection and independent
review then found additional gaps that those tests did not cover.

| Area | Finding | Required correction |
|---|---|---|
| Blender axes | Equal depth and height concealed an internal Y/Z normalization swap | Unequal dimensions verified through actual Blender output and decoded glTF bounds |
| Blender artifact safety | Output/report could overwrite existing files; report could alias raw input | Reject aliases, existing destinations and symlinks before execution; defensive script checks |
| GLB textures | Embedded image decode and non-base-color texture reference checks incomplete | Fully decode bounded images; validate every supported material texture slot |
| Paid authorization | Caller supplied approved fingerprint without binding current request parameters | Recompute shared canonical approval scope from authoritative persisted task/artifacts; compare derived request parameters |
| Paid operation identity | Different tasks could submit for the same asset revision | Atomic uniqueness for asset/revision/provider/operation as well as task/fingerprint |
| Provider completion | Canceled/expired task treated as submitted | Persist terminal failure and prohibit resubmission |
| Download manifest | Completion state and per-file written status ignored | Require real CLI completed/written envelope, one GLB, strict size and hash checks |
| Cold review bundle | Approval receipts, attempt linkage and file-set completeness insufficient | Require and validate receipts, runtime/capture/GLB relationships, every on-disk file |
| Offline HTML | Remote and active references not rejected consistently | Restricted static HTML, no remote resources or unmanifested references |
| Runtime collision | Collider transformation omitted ancestors | Transform geometry into asset-root space using full hierarchy |
| Godot diagnostics | Zero exit code alone could hide engine errors | Reuse strict error diagnostics and retain actual command evidence |
| Recovery accounting | Query-only retry risked another budget reservation | One unresolved liability per durable generation operation, tested at budget boundary |

No real paid Meshy operation was used to investigate these findings. Fake
provider tests and actual local Blender/Godot processes are separate evidence
classes. The completion report records final verification and remaining gates.
