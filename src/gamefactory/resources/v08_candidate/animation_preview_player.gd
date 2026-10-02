extends AnimationPlayer

## Builds the canonical rig_smoke_01 clip from animation_spec.json at runtime.

const SPEC_PATH := "res://animation_spec.json"
const CLIP_ID := "rig_smoke_01"

func _ready() -> void:
	if has_animation(CLIP_ID):
		return
	var spec := _load_spec()
	if spec.is_empty():
		push_error("animation_spec.json missing or invalid")
		return
	var skeleton := _find_skeleton(_get_character_root())
	if skeleton == null:
		push_error("Skeleton3D not found for animation preview")
		return
	var anim := _build_animation(skeleton, spec)
	if anim == null:
		push_error("failed to build rig_smoke_01")
		return
	var library := AnimationLibrary.new()
	library.add_animation(CLIP_ID, anim)
	add_animation_library("", library)
	assigned_animation = CLIP_ID
	play(CLIP_ID)

func _get_character_root() -> Node:
	var parent_node := get_parent()
	if parent_node == null:
		return self
	var character := parent_node.get_node_or_null("Character")
	return character if character != null else parent_node

func _load_spec() -> Dictionary:
	var file := FileAccess.open(SPEC_PATH, FileAccess.READ)
	if file == null or file.get_length() > 65536:
		return {}
	var parsed: Variant = JSON.parse_string(file.get_as_text())
	file.close()
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	if JSON.stringify(parsed, "", true) != JSON.stringify(_canonical_spec(), "", true):
		return {}
	return parsed

func _canonical_spec() -> Dictionary:
	return {
		"schema_version": "candidate-character-animation-spec-0.8.0",
		"clip_id": CLIP_ID, "duration_s": 1.0, "loop": true,
		"bone_name": "LeftUpperArm", "root_motion": false,
		"keyframes": [
			{"time_s": 0.0, "euler_deg": [0.0, 0.0, 0.0]},
			{"time_s": 0.5, "euler_deg": [0.0, 20.0, 0.0]},
			{"time_s": 1.0, "euler_deg": [0.0, 0.0, 0.0]},
		],
		"min_affected_displacement": 0.012, "max_affected_displacement": 0.35,
		"production_eligible": false, "promotion_eligible": false,
	}

func _build_animation(skeleton: Skeleton3D, spec: Dictionary) -> Animation:
	var bone_name := str(spec.get("bone_name", ""))
	if skeleton.find_bone(bone_name) < 0:
		return null
	var duration := float(spec.get("duration_s", 0.0))
	if duration <= 0.0:
		return null
	var keyframes: Variant = spec.get("keyframes")
	if typeof(keyframes) != TYPE_ARRAY or keyframes.is_empty():
		return null
	var anim := Animation.new()
	anim.length = duration
	anim.loop_mode = Animation.LOOP_LINEAR
	var track := anim.add_track(Animation.TYPE_ROTATION_3D)
	var scene_root := get_parent()
	if scene_root == null:
		return null
	var skel_path := scene_root.get_path_to(skeleton)
	anim.track_set_path(track, NodePath(str(skel_path) + ":" + bone_name))
	for entry in keyframes:
		if typeof(entry) != TYPE_DICTIONARY:
			return null
		var euler: Variant = entry.get("euler_deg")
		if typeof(euler) != TYPE_ARRAY or euler.size() != 3:
			return null
		var time_s := float(entry.get("time_s", -1.0))
		if time_s < 0.0:
			return null
		var rot := Quaternion.from_euler(
			Vector3(
				deg_to_rad(float(euler[0])),
				deg_to_rad(float(euler[1])),
				deg_to_rad(float(euler[2]))
			)
		)
		anim.rotation_track_insert_key(track, time_s, rot)
	return anim

func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node as Skeleton3D
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null
