extends "res://animation_clip_preview_player.gd"

## Installs 2–8 bounded rig-animation-clip-0.8.0 sources into one AnimationPlayer library.

const MIN_CLIP_COUNT := 2
const MAX_CLIP_COUNT := 8
const CLIP_FILE_NAME := "animation_clip.json"

@export var clip_paths: PackedStringArray = PackedStringArray()
@export var clip_sha256s: PackedStringArray = PackedStringArray()
@export var clip_ids: PackedStringArray = PackedStringArray()

var _review_set_ready: bool = false
var _source_clip_ids: PackedStringArray = PackedStringArray()


func _ready() -> void:
	_review_set_ready = false
	_source_clip_ids = PackedStringArray()

	var count: int = clip_paths.size()
	if count < MIN_CLIP_COUNT or count > MAX_CLIP_COUNT:
		push_error("review set requires 2 to 8 clips")
		return
	if clip_sha256s.size() != count or clip_ids.size() != count:
		push_error("review set clip_paths, clip_sha256s, and clip_ids must match length")
		return

	var seen_ids: Dictionary = {}
	for i in range(count):
		var bound_path: String = str(clip_paths[i])
		var expected_path: String = _expected_clip_path(i)
		if bound_path != expected_path:
			push_error("review set clip_paths[%d] must be %s" % [i, expected_path])
			return
		var bound_sha: String = str(clip_sha256s[i])
		if not _is_lowercase_hex64(bound_sha):
			push_error("review set clip_sha256s[%d] must be lowercase 64-hex" % i)
			return
		var bound_id: String = str(clip_ids[i])
		if bound_id.is_empty() or _clip_id_re.search(bound_id) == null:
			push_error("review set clip_ids[%d] is not a safe clip id" % i)
			return
		if seen_ids.has(bound_id):
			push_error("review set clip_ids must be unique")
			return
		seen_ids[bound_id] = true

	var character_root: Node = _get_character_root()
	var skeleton: Skeleton3D = _find_skeleton(character_root)
	if skeleton == null:
		push_error("Skeleton3D not found for animation review set")
		return
	if not _validate_skeleton_contract(skeleton):
		push_error("skeleton contract mismatch")
		return
	if not _validate_mesh_binding(character_root, skeleton):
		push_error("mesh skin binding mismatch")
		return
	if not _validate_weighted_bone_export(skeleton):
		push_error("verified weighted bone capability mismatch")
		return

	var clip_docs: Array = []
	for i in range(count):
		var path: String = str(clip_paths[i])
		var expected_sha: String = str(clip_sha256s[i])
		var expected_id: String = str(clip_ids[i])
		var clip_doc: Dictionary = _load_bounded_clip(path, expected_sha)
		if clip_doc.is_empty():
			push_error("clip at index %d missing, tampered, or failed validation" % i)
			return
		var doc_id: String = str(clip_doc.get("clip_id", ""))
		if doc_id != expected_id:
			push_error("clip document clip_id does not match clip_ids[%d]" % i)
			return
		if has_animation(doc_id):
			push_error("animation already installed for clip_id %s" % doc_id)
			return
		clip_docs.append(clip_doc)

	var library: AnimationLibrary = AnimationLibrary.new()
	for i in range(count):
		var doc: Dictionary = clip_docs[i] as Dictionary
		var anim_id: String = str(doc.get("clip_id", ""))
		var anim: Animation = _build_animation(skeleton, doc)
		if anim == null:
			push_error("failed to build authored clip at index %d" % i)
			return
		library.add_animation(anim_id, anim)

	add_animation_library("", library)
	_source_clip_ids = clip_ids.duplicate()
	_review_set_ready = true
	var first_id: String = str(clip_ids[0])
	assigned_animation = first_id
	play(first_id)


func review_set_player_ready() -> bool:
	return _review_set_ready


func review_set_source_clip_ids() -> PackedStringArray:
	return _source_clip_ids.duplicate()


func _expected_clip_path(index: int) -> String:
	return "res://clips/%03d/%s" % [index, CLIP_FILE_NAME]


func _is_lowercase_hex64(value: String) -> bool:
	if value.length() != 64:
		return false
	for i in value.length():
		var c: String = value[i]
		if not (("0" <= c and c <= "9") or ("a" <= c and c <= "f")):
			return false
	return true


func _load_bounded_clip(path: String, expected_sha: String) -> Dictionary:
	var raw: PackedByteArray = _read_bounded_file(path, MAX_CLIP_BYTES)
	if raw.is_empty():
		return {}
	if _sha256_hex(raw) != expected_sha:
		return {}
	var parsed: Variant = JSON.parse_string(raw.get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	var doc: Dictionary = parsed
	if not _validate_clip_document(doc):
		return {}
	return doc
