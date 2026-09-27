# Integration status in V0.2

| Integration | Implemented | Boundary / limitation |
| --- | --- | --- |
| Godot | Discovery, project inspection, staged headless import/runtime and independent assertions | Trusted local scenes implementing the bounded verification contract; no exports or visual verification |
| Blender | Executable discovery and version check | No scripted asset processing |
| Meshy | Provider boundary, PAID classification, credential-presence check | Generation transport is unavailable; credentials never make it available |
| Fake asset provider | Deterministic local test/demo provider | Only for built-in workflows and tests; output is explicitly fake |
| AI coding agents | No production adapter in V0.1 | Planned extension point only |
| Image/audio/vision providers | Not implemented | No external calls |

Executable overrides can be given by `--godot-path` and `--blender-path` or stored in project configuration. Environment variables `GAMEFACTORY_GODOT_PATH` and `GAMEFACTORY_BLENDER_PATH` are fallback discovery hints when no explicit config/CLI override is set. If an explicit path is invalid, the result is `MISCONFIGURED`; discovery does not silently use PATH instead.

Capabilities distinguish detection from the implemented verification workflow. General exports, screenshots and arbitrary engine automation are not advertised. Meshy remains `UNAVAILABLE` even if `MESHY_API_KEY` is present. See [Godot verification](godot-headless-verification.md) for the CLI, scenario contract and limitations.
