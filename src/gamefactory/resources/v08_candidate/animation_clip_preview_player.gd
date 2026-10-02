extends AnimationPlayer

## Builds an authored rig-animation-clip-0.8.0 clip from animation_clip.json at runtime.

const CLIP_PATH := "res://animation_clip.json"
const MANIFEST_PATH := "res://animation_clip_manifest.json"
const MAX_CLIP_BYTES := 65536
const MAX_MANIFEST_BYTES := 65536
const SCHEMA := "rig-animation-clip-0.8.0"
const MANIFEST_SCHEMA := "candidate-character-animation-clip-preview-manifest-0.8.0"
const QUAT_EPS := 0.00001
const MIN_DURATION_S := 0.001
const MAX_DURATION_S := 10.0
const MESH_NAME := "SM_HumanoidSkin"

const CONTRACT_BONES: Array[String] = [
	"Hips", "Spine", "Chest", "Neck", "Head", "LeftUpperArm", "LeftLowerArm", "LeftHand",
	"RightUpperArm", "RightLowerArm", "RightHand", "LeftUpperLeg",
]

const CLIP_ROOT_KEYS: Array[String] = [
	"schema_version", "clip_id", "duration_seconds", "loop", "tracks",
]
const TRACK_KEYS: Array[String] = ["bone", "keyframes"]
const KEYFRAME_KEYS: Array[String] = ["time", "rotation_xyzw"]

@export var expected_clip_sha256: String = ""
@export var verified_weighted_bone_names: PackedStringArray = PackedStringArray()

var _clip_id_re: RegEx


func _init() -> void:
	_clip_id_re = RegEx.new()
	_clip_id_re.compile("^[A-Za-z][A-Za-z0-9_]{0,63}$")


func _ready() -> void:
	if expected_clip_sha256.length() != 64 or not _is_hex64(expected_clip_sha256):
		push_error("scene expected_clip_sha256 missing or invalid")
		return
	var manifest := _load_manifest()
	if manifest.is_empty():
		push_error("animation_clip_manifest.json missing or invalid")
		return
	var manifest_sha := str(manifest.get("clip_sha256", ""))
	if manifest_sha.length() != 64 or not _is_hex64(manifest_sha):
		push_error("manifest clip_sha256 missing or invalid")
		return
	if manifest_sha != expected_clip_sha256:
		push_error("manifest clip_sha256 does not match scene binding")
		return
	var clip_doc := _load_clip(manifest_sha)
	if clip_doc.is_empty():
		push_error("animation_clip.json missing, tampered, or invalid")
		return
	var skeleton := _find_skeleton(_get_character_root())
	if skeleton == null:
		push_error("Skeleton3D not found for animation clip preview")
		return
	if not _validate_skeleton_contract(skeleton):
		push_error("skeleton contract mismatch")
		return
	if not _validate_mesh_binding(_get_character_root(), skeleton):
		push_error("mesh skin binding mismatch")
		return
	if not _validate_weighted_bone_export(skeleton):
		push_error("verified weighted bone capability mismatch")
		return
	var clip_id := str(clip_doc.get("clip_id", ""))
	if clip_id.is_empty() or has_animation(clip_id):
		return
	var anim := _build_animation(skeleton, clip_doc)
	if anim == null:
		push_error("failed to build authored clip")
		return
	var library := AnimationLibrary.new()
	library.add_animation(clip_id, anim)
	add_animation_library("", library)
	assigned_animation = clip_id
	play(clip_id)


func _is_hex64(value: String) -> bool:
	if value.length() != 64:
		return false
	for i in value.length():
		var c := value[i]
		if not (("0" <= c and c <= "9") or ("a" <= c and c <= "f") or ("A" <= c and c <= "F")):
			return false
	return true


func _sha256_hex(data: PackedByteArray) -> String:
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(data)
	return ctx.finish().hex_encode()


func _read_bounded_file(path: String, max_bytes: int) -> PackedByteArray:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return PackedByteArray()
	var original_length := file.get_length()
	if original_length > max_bytes:
		file.close()
		return PackedByteArray()
	var raw := file.get_buffer(max_bytes + 1)
	file.close()
	if raw.size() > max_bytes:
		return PackedByteArray()
	if raw.size() != original_length:
		return PackedByteArray()
	return raw


func _get_character_root() -> Node:
	var parent_node := get_parent()
	if parent_node == null:
		return self
	var character := parent_node.get_node_or_null("Character")
	return character if character != null else parent_node


func _load_manifest() -> Dictionary:
	var raw := _read_bounded_file(MANIFEST_PATH, MAX_MANIFEST_BYTES)
	if raw.is_empty():
		return {}
	var parsed: Variant = JSON.parse_string(raw.get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	if not _validate_manifest_document(parsed):
		return {}
	return parsed


func _validate_manifest_document(doc: Dictionary) -> bool:
	if doc.get("schema_version") != MANIFEST_SCHEMA:
		return false
	if typeof(doc.get("clip_sha256")) != TYPE_STRING:
		return false
	if typeof(doc.get("file_digests")) != TYPE_DICTIONARY:
		return false
	var digests: Dictionary = doc.get("file_digests")
	if typeof(digests.get("animation_clip.json")) != TYPE_STRING:
		return false
	return true


func _load_clip(expected_sha: String) -> Dictionary:
	var raw := _read_bounded_file(CLIP_PATH, MAX_CLIP_BYTES)
	if raw.is_empty() or _sha256_hex(raw) != expected_sha:
		return {}
	var parsed: Variant = JSON.parse_string(raw.get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	if not _validate_clip_document(parsed):
		return {}
	return parsed


func _dict_has_exact_keys(doc: Dictionary, allowed: Array[String]) -> bool:
	if doc.size() != allowed.size():
		return false
	for key in allowed:
		if not doc.has(key):
			return false
	for key in doc.keys():
		if not allowed.has(str(key)):
			return false
	return true


func _validate_clip_document(doc: Dictionary) -> bool:
	if not _dict_has_exact_keys(doc, CLIP_ROOT_KEYS):
		return false
	if doc.get("schema_version") != SCHEMA:
		return false
	if typeof(doc.get("clip_id")) != TYPE_STRING:
		return false
	var clip_id := str(doc.get("clip_id"))
	if clip_id.is_empty() or _clip_id_re.search(clip_id) == null:
		return false
	var duration: Variant = doc.get("duration_seconds")
	if typeof(duration) != TYPE_FLOAT and typeof(duration) != TYPE_INT:
		return false
	var duration_s := float(duration)
	if not is_finite(duration_s) or duration_s < MIN_DURATION_S or duration_s > MAX_DURATION_S:
		return false
	if typeof(doc.get("loop")) != TYPE_BOOL:
		return false
	var tracks: Variant = doc.get("tracks")
	if typeof(tracks) != TYPE_ARRAY or tracks.is_empty() or tracks.size() > 8:
		return false
	var weighted: Dictionary = {}
	for name in verified_weighted_bone_names:
		weighted[str(name)] = true
	var seen_bones: Dictionary = {}
	for entry in tracks:
		if typeof(entry) != TYPE_DICTIONARY:
			return false
		if not _dict_has_exact_keys(entry, TRACK_KEYS):
			return false
		var bone := str(entry.get("bone"))
		if bone.is_empty() or bone == "HumanoidRoot" or seen_bones.has(bone):
			return false
		if not CONTRACT_BONES.has(bone):
			return false
		if not weighted.has(bone):
			return false
		seen_bones[bone] = true
		var keyframes: Variant = entry.get("keyframes")
		if typeof(keyframes) != TYPE_ARRAY or keyframes.is_empty() or keyframes.size() > 64:
			return false
		var previous_time := -1.0
		for key_entry in keyframes:
			if typeof(key_entry) != TYPE_DICTIONARY:
				return false
			if not _dict_has_exact_keys(key_entry, KEYFRAME_KEYS):
				return false
			var time_v: Variant = key_entry.get("time")
			if typeof(time_v) != TYPE_FLOAT and typeof(time_v) != TYPE_INT:
				return false
			var time_s := float(time_v)
			if not is_finite(time_s) or time_s < 0.0 or time_s > duration_s or time_s <= previous_time:
				return false
			var rot: Variant = key_entry.get("rotation_xyzw")
			if typeof(rot) != TYPE_ARRAY or rot.size() != 4:
				return false
			var comps: Array[float] = []
			for i in range(4):
				var comp: Variant = rot[i]
				if typeof(comp) != TYPE_FLOAT and typeof(comp) != TYPE_INT:
					return false
				var f := float(comp)
				if not is_finite(f):
					return false
				comps.append(f)
			var quat := Quaternion(comps[0], comps[1], comps[2], comps[3])
			if quat.length_squared() <= 0.0 or absf(quat.length() - 1.0) > QUAT_EPS:
				return false
			previous_time = time_s
	return true


func _validate_skeleton_contract(skeleton: Skeleton3D) -> bool:
	if skeleton.get_bone_count() != CONTRACT_BONES.size():
		return false
	for bone_name in CONTRACT_BONES:
		if skeleton.find_bone(bone_name) < 0:
			return false
	return true


func _find_mesh(node: Node) -> MeshInstance3D:
	if node is MeshInstance3D and node.name == MESH_NAME:
		return node
	for child in node.get_children():
		var found := _find_mesh(child)
		if found != null:
			return found
	return null


func _validate_mesh_binding(character_root: Node, skeleton: Skeleton3D) -> bool:
	var mesh := _find_mesh(character_root)
	if mesh == null or mesh.skin == null:
		return false
	return mesh.get_node_or_null(mesh.skeleton) == skeleton


func _validate_weighted_bone_export(skeleton: Skeleton3D) -> bool:
	if verified_weighted_bone_names.is_empty():
		return false
	var seen: Dictionary = {}
	for name in verified_weighted_bone_names:
		var bone := str(name)
		if bone.is_empty() or seen.has(bone) or not CONTRACT_BONES.has(bone):
			return false
		if skeleton.find_bone(bone) < 0:
			return false
		seen[bone] = true
	return true


func _build_animation(skeleton: Skeleton3D, doc: Dictionary) -> Animation:
	var duration := float(doc.get("duration_seconds"))
	var loop_flag: bool = doc.get("loop")
	var tracks: Variant = doc.get("tracks")
	var anim := Animation.new()
	anim.length = duration
	anim.loop_mode = Animation.LOOP_LINEAR if loop_flag else Animation.LOOP_NONE
	var scene_root := get_parent()
	if scene_root == null:
		return null
	var skel_path := scene_root.get_path_to(skeleton)
	for entry in tracks:
		if typeof(entry) != TYPE_DICTIONARY:
			return null
		var bone_name := str(entry.get("bone"))
		if skeleton.find_bone(bone_name) < 0:
			return null
		var track := anim.add_track(Animation.TYPE_ROTATION_3D)
		anim.track_set_path(track, NodePath(str(skel_path) + ":" + bone_name))
		var keyframes: Variant = entry.get("keyframes")
		for key_entry in keyframes:
			if typeof(key_entry) != TYPE_DICTIONARY:
				return null
			var time_s := float(key_entry.get("time"))
			var rot: Variant = key_entry.get("rotation_xyzw")
			var quat := Quaternion(float(rot[0]), float(rot[1]), float(rot[2]), float(rot[3]))
			anim.rotation_track_insert_key(track, time_s, quat)
	return anim


func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node as Skeleton3D
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null
