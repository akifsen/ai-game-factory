# Integration status

This page describes the development implementation, not a claim that a service
was invoked on this machine. `factory providers list --json` reports static
configuration and credential/tool availability without calling a model.
Live execution requires the normal operation-specific approval and evidence.

| Integration | Implemented path | Qualification and limits | Tide Bastion'da gerçek kullanım |
| --- | --- | --- | --- |
| Godot | Project discovery/scaffold, staged validation, rendered captures, bounded gameplay/performance harness, native editor/run, export and release manifests | Requires a configured executable and export templates for the selected platform. Unsupported metrics remain missing. Release does not publish. | `WF-REUSE-230ee7ad` / [`commit0a0d834`](https://github.com/akifsen/tide-bastion/commit/0a0d834) — static-prop reuse/install; `WF-GAMEPLAY-6fc2f444` / customer `ee2fb5a1ea7339d85fffe829faf3be0fda3bf87c` — first-wave battle preparation, archer build, pause/resume and rendered capture; `WF-PROJECT-4c53361f` / customer `b1fb5ecb824162f5271f0a7a99e0dab36978c23b` — Windows x86_64 build, pack audit and native startup without Factory; other platforms, performance and other scenarios remain experimental |
| Blender | Bounded asset processing and validation in the existing profile pipelines | Requires a configured executable. Supported profile contracts determine geometry, LOD and collider behavior. | `WF-REUSE-230ee7ad` / [`commit0a0d834`](https://github.com/akifsen/tide-bastion/commit/0a0d834) — static-prop reuse processing only; not general asset production |
| Meshy | Real `meshy-cli` generation, query and download transport with durable request snapshots and intent | Credentials and the compatible CLI are required. Unknown submissions reconcile rather than blindly repeat. No automatic paid validation. | YOK — Tide Bastion's historical manual chest source is not Factory production |
| Codex | Existing static-prop planner plus general bounded code/design proposals | Requires an explicit model and authenticated supported CLI. General proposals use read-only execution and versioned prompt/schema assets; Factory applies files only after gates and approval. | `WF-M1-TEXT-SCALE-RETRY-20261005` / customer commit `1cbf6bafdd255928f08770422230ca9f86ce314e` — bounded two-line settings code proposal, real code gates and approved application only; planner and other proposal families remain **experimental** |
| OpenAI images | Fixed official Images generation endpoint with bounded validated PNG output | Explicit model, credential environment-variable name, approval and cost ceiling required. Text context only; unsupported binary conditioning is rejected. | YOK |
| OpenAI speech | Fixed official speech endpoint with validated bounded WAV output | Explicit model and voice required. This capability synthesizes speech; it does not advertise music or sound-effects generation. | YOK |
| OpenAI vision | Fixed official Responses endpoint with approved PNG/JPEG and text inputs, structured advisory findings | Explicit vision-capable model required. No model tools, remote image URL fetching, authoritative pass decision or automatic retry. | YOK |
| Operator process providers | Fixed operator-owned executable/arguments with bounded JSON stdin/stdout contracts | The executable must implement the published protocol. A registered capability alone does not prove that it works. No OS egress sandbox is claimed; explicit process/network permissions are required. | YOK |
| Fake providers | Deterministic injected test/demo implementations | Their outputs are explicitly fake and do not qualify a production integration. | YOK |

Real-use checkpoint (2026-10-06): saved-manifest reconstruction and project-context
script parsing were repaired. Original `WF-FACTORY-7356d61d` remains FAILED.
The approved second Codex call `EXEC-1b0b1e40` in
`WF-M1-TEXT-SCALE-RETRY-20261005` produced the same two-line settings proposal.
Both durable code gates passed all 76 scripts, and independent SettingsScreen
checks passed. After the human's direct response to the concrete application
question, game-write approval `APP-56d10c91` was recorded and Factory applied the
file. Workflow COMPLETED; customer commit `1cbf6ba` contains only the two lines.
This qualifies M1's bounded code path, not every general code workflow.
See [the M1 checkpoint](../reports/2026-10-04-real-use-milestones.md).

Provider readiness distinguishes `UNAVAILABLE`, `MISCONFIGURED`,
`CREDENTIAL_MISSING`, `NOT_VERIFIED` and `AVAILABLE`. A configured but unprobed
adapter may be selected explicitly; its report still does not claim a successful
live invocation. Configuration stores credential names only. Model pricing and
actual charges are not inferred from an installed executable or API key.

Godot and Blender executable overrides use `--godot-path` and `--blender-path`
or project configuration. `GAMEFACTORY_GODOT_PATH` and
`GAMEFACTORY_BLENDER_PATH` are discovery hints when no explicit selection exists.
An invalid explicit selection is reported instead of silently switching to
another executable.

Production asset profiles remain narrower than general workflow families.
Any integration whose Tide Bastion column is **YOK** is **experimental** until a
real customer workflow ID and commit are recorded here. Rigged-character and
animation review tooling is experimental; it does not qualify rigged-character
production. Unity, Unreal, cloud workers, marketplaces and autonomous expensive
fallback are not supplied by these adapters.

See [the master usage guide](../guides/master-factory.md),
[asset production](../pipelines/generalized-asset-production.md) and
[Godot verification](godot-headless-verification.md).
