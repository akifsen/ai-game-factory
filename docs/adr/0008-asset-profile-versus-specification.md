# ADR 0008: Asset profile versus asset specification

## Status

Accepted for V0.5. Verification is recorded in `docs/reports/v0.5-completion-report.md`.

## Context

V0.4 proved one static prop, `prop_energy_crate_01` r001, through concept review, paid generation, Blender, validation, Godot, and a production receipt. The processing rules were mixed into the specification schema and into a few constants (review angles, framing fractions, physics body, LOD requirement). A second asset class would either copy those constants or special-case the crate.

A plugin loader, marketplace, or executable profile document would let a YAML file become code. That is not required to support three asset classes.

## Decision

1. An **asset profile** answers how a class of asset is processed: budgets, origin, collider, LOD, Godot scene contract, runtime assertions, and review views. Built-in profiles are `static_prop@1`, `pickup@1`, and `modular_piece@1`. They are explicit registry entries loaded from strict YAML. Unknown fields, unsupported enums, negative budgets, and unimplemented views are rejected. The documents do not execute caller code.
2. An **asset specification** answers what one asset is: identity, dimensions, budgets within the profile, style, and import path. Schema `asset-spec-0.4.0` stays fingerprint-compatible and binds implicitly to `static_prop@1`. Schema `asset-spec-0.5.0` records `profile_version` explicitly.
3. A revision stores the profile id and version it was allocated with. Changing profile requires a new revision. Approval fingerprints include the profile id and version.
4. Historical V0.4 evidence is read through an adapter. Those files are not rewritten into the 0.5 receipt schema.
5. Profiles do not own paid-cost policy. A paid approval still binds the revision, profile version, specification, concept, and provider request.

## Consequences

Blender, the GLB validator, Godot scene generation, runtime checks, and the review package take their contract from the bound profile. Adding character, weapon, vehicle, or similar classes is a later milestone and is reported as UNSUPPORTED. Collider choices that are not implemented (`convex`, `simple_mesh`) are not advertised.
