# Integration status

This page describes the development implementation, not a claim that a service
was invoked on this machine. `factory providers list --json` reports static
configuration and credential/tool availability without calling a model.
Live execution requires the normal operation-specific approval and evidence.

| Integration | Implemented path | Qualification and limits |
| --- | --- | --- |
| Godot | Project discovery/scaffold, staged validation, rendered captures, bounded gameplay/performance harness, native editor/run, export and release manifests | Requires a configured executable and export templates for the selected platform. Unsupported metrics remain missing. Release does not publish. |
| Blender | Bounded asset processing and validation in the existing profile pipelines | Requires a configured executable. Supported profile contracts determine geometry, LOD and collider behavior. |
| Meshy | Real `meshy-cli` generation, query and download transport with durable request snapshots and intent | Credentials and the compatible CLI are required. Unknown submissions reconcile rather than blindly repeat. No automatic paid validation. |
| Codex | Existing static-prop planner plus general bounded code/design proposals | Requires an explicit model and authenticated supported CLI. General proposals use read-only execution and versioned prompt/schema assets; Factory applies files only after gates and approval. |
| OpenAI images | Fixed official Images generation endpoint with bounded validated PNG output | Explicit model, credential environment-variable name, approval and cost ceiling required. Text context only; unsupported binary conditioning is rejected. |
| OpenAI speech | Fixed official speech endpoint with validated bounded WAV output | Explicit model and voice required. This capability synthesizes speech; it does not advertise music or sound-effects generation. |
| OpenAI vision | Fixed official Responses endpoint with approved PNG/JPEG and text inputs, structured advisory findings | Explicit vision-capable model required. No model tools, remote image URL fetching, authoritative pass decision or automatic retry. |
| Operator process providers | Fixed operator-owned executable/arguments with bounded JSON stdin/stdout contracts | The executable must implement the published protocol. A registered capability alone does not prove that it works. No OS egress sandbox is claimed; explicit process/network permissions are required. |
| Fake providers | Deterministic injected test/demo implementations | Their outputs are explicitly fake and do not qualify a production integration. |

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
Rigged-character and animation review tooling is experimental; it does not
qualify rigged-character production. Unity, Unreal, cloud workers, marketplaces
and autonomous expensive fallback are not supplied by these adapters.

See [the master usage guide](../guides/master-factory.md),
[asset production](../pipelines/generalized-asset-production.md) and
[Godot verification](godot-headless-verification.md).