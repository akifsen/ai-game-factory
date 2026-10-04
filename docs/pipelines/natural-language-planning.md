# Natural-language planning (product pilot)

Bounded pilot: turn a short natural-language description into a strict `asset-spec-0.4.0` JSON document for **`static_prop@1`** (Tide Bastion static props). Planning does **not** create workflows, submit provider jobs, write game content, or bypass `asset create` approvals.

## Command

```powershell
gamefactory plan "rusted tide buoy for the dock" --output plans/prop_tide_buoy_01.json
```

Optional flags:

- `--codex-path` — explicit Codex CLI executable (batch `.cmd` launchers are rejected on Windows; prefer `codex.exe` or a direct binary path).
- `--timeout` — finite subprocess timeout in seconds (default `180`).
- `-p` / `--project` — directory used to resolve relative `--output` paths (defaults to the current directory). **Factory `init` is not required.**

The command invokes the installed **Codex CLI** with:

- `exec --sandbox read-only --ephemeral --ignore-user-config --skip-git-repo-check`
- `--output-schema` (strict draft schema), `--output-last-message <message.json>`, and stdin prompt `-`
- strict JSON read from the message file only (stdout/stderr are diagnostics)

There is **no fallback planner** and no fake provider path in this pilot.

## Agent draft vs derived specification

Codex returns only a small JSON draft:

| Draft field | Purpose |
| --- | --- |
| `asset_id` | Safe lowercase identifier |
| `intent` | One-line production intent |
| `dimensions` | `width_m`, `depth_m`, `height_m` |
| `style_constraints` | `silhouette` and `detail_density` enums |

Factory derives fixed pilot values locally: `schema_version` `0.4.0`, `category` `prop`, `profile` `static_prop`, orientation, budgets, collider/LOD policies, `target_engine` `godot`, and `target_import_path` `assets/generated/props/<asset_id>/`. The assembled document is validated with `parse_any_asset_specification`.

## Output safety

- `--output` must resolve under the project directory (`PathGuard`); traversal and unsafe path spellings are rejected.
- Existing output files are never overwritten; successful writes use a staged temp file and exclusive hard link (no clobber).
- Credentials, environment secrets, and raw subprocess stderr are not echoed to the console.

## Next step (out of scope for `plan`)

Feed the JSON into the normal gated asset workflow when ready:

```powershell
gamefactory --project . asset create --spec plans/prop_tide_buoy_01.json --concept concept.png --provenance provenance.json --provider meshy
```

Supply your actual concept PNG and hash-bound provenance. Real Meshy production
retains concept, paid-request/budget and final human approval gates; this command
does not itself authorize spending. An existing manually generated source needs a
separately verified reuse route; it must not be represented as fake generation.

## Limitations

- Profile support is **`static_prop@1` only**; pickups, modular pieces, and V0.7 assembly specs are out of scope.
- Requires a working Codex CLI and whatever authentication Codex expects in the host environment; this repository does not bundle Codex.
- Model output quality is not guaranteed; domain validation fails closed on malformed or extra fields.
- Output publication requires a filesystem supporting same-directory hard links.
