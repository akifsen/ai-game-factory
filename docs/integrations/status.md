# Integration status in V0.1

| Integration | Implemented | Boundary / limitation |
| --- | --- | --- |
| Godot | Executable discovery, version check, `project.godot` metadata inspection | No editor launch, headless run, test harness, export, or screenshot capability |
| Blender | Executable discovery and version check | No scripted asset processing |
| Meshy | Provider boundary, PAID classification, credential-presence check | Generation transport is unavailable; credentials never make it available |
| Fake asset provider | Deterministic local test/demo provider | Only for built-in workflows and tests; output is explicitly fake |
| AI coding agents | No production adapter in V0.1 | Planned extension point only |
| Image/audio/vision providers | Not implemented | No external calls |

Executable overrides can be given by `--godot-path` and `--blender-path` or stored in project configuration. Environment variables `GAMEFACTORY_GODOT_PATH` and `GAMEFACTORY_BLENDER_PATH` are fallback discovery hints when no explicit config/CLI override is set. If an explicit path is invalid, the result is `MISCONFIGURED`; discovery does not silently use PATH instead.

Capabilities describe implemented operations. Detection does not imply execution: `engine.godot.detect` exists, while `engine.godot.run` is not advertised. Meshy remains `UNAVAILABLE` in V0.1 even if `MESHY_API_KEY` is present.
