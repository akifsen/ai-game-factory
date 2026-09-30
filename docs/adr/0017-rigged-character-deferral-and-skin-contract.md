# ADR 0017: rigged_character deferral and skin contract

## Status

Accepted for V0.7 (Step 2 architecture decision). The deferral is in effect in V0.7.0. The skin contract is recorded for a later milestone and is **not implemented** as a production capability.

## Context

A V0.7 spike built a 12-bone skinned humanoid in Blender and imported it into Godot 4.7.2. Bone names, hierarchy, rest transforms and the skin binding all survived. That shows feasibility. It does not validate weights, deformation quality, a humanoid bone map, retargeting or animation, and V0.7 has no oracle for any of them.

## Decision

1. **`rigged_character` remains UNSUPPORTED in V0.7.**
   - It stays in `UNSUPPORTED_PROFILE_IDS`, and `gamefactory asset profiles` lists it as UNSUPPORTED.
   - It never appears in `ProfileRegistry.available`, and the registry refuses to bind it.
   - No production specification can select it.
2. Every V0.7 production profile, including `character@1`, forbids rigs and animation. The validator keeps rejecting `skins`, `animations`, `JOINTS_0` and `WEIGHTS_0`. The existence of an internal fixture never permits skins or animations anywhere else.
3. **Internal spike contract.** An internal `skin_internal` validator group (ADR 0018) and its fixture may exist so that the spike stays reproducible. No profile loaded through the registry can select it, because no production profile schema field enables it. It is reachable only from tests.
4. **Skin contract for a later milestone.** This is documentation only.
   - one skeleton root, a child of the asset root; bone names come from a declared map onto Godot `SkeletonProfileHumanoid`
   - rest pose declared (`T` or `A`), in the ADR 0014 frame (+Y up, −Z front)
   - at most 4 influences per vertex, weights normalized within 1e-3, and no vertex without weights
   - inverse bind matrices consistent with the rest pose
   - a joint-count bound set by the profile
   - sockets (ADR 0013) may be parented to bones
   - a runtime oracle that poses named bones and checks the expected mesh deformation bounds
5. Promotion to AVAILABLE requires its own ADR, a production validator group for every rule in item 4, a runtime oracle, and negative fixtures.

## Consequences

Characters in V0.7 are static review models (`character@1`, ADR 0015). The spike result is kept as design input and is not presented as a capability.

## Implementation (V0.7.0)

- `rigged_character` stays in `UNSUPPORTED_PROFILE_IDS`; `get` and `get_v07` refuse it, and a production specification that selects it is rejected. `character@1` rejects `skins`, `animations`, `JOINTS_0` and `WEIGHTS_0` (negative fixtures cover a skinned and an animated GLB).
- The optional internal `skin_internal` validator group and its spike fixture were not added; no V0.7 behavior depends on them.
