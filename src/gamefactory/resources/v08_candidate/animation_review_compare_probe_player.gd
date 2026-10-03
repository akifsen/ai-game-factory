extends AnimationPlayer

## In-memory sparse review-set player for fast compare runtime probes.

const CLIP_A := "probe_clip_a"
const CLIP_B := "probe_clip_b"
const CLIP_C := "probe_clip_c"

var _review_ready: bool = false
var _source_clip_ids: PackedStringArray = PackedStringArray()


func _ready() -> void:
	_review_ready = false
	_source_clip_ids = PackedStringArray()
	var preview := get_parent()
	if preview == null:
		return
	var skeleton := preview.get_node_or_null("Skeleton3D") as Skeleton3D
	if skeleton == null:
		return
	if skeleton.find_bone("LeftUpperArm") < 0 or skeleton.find_bone("Spine") < 0:
		return
	var duration := 2.0
	var library := AnimationLibrary.new()
	library.add_animation(
		CLIP_A,
		_build_sparse_rotation_clip(preview, skeleton, duration, "LeftUpperArm", 0.0, 30.0)
	)
	library.add_animation(
		CLIP_B,
		_build_sparse_rotation_clip(preview, skeleton, duration, "Spine", 0.0, 45.0)
	)
	library.add_animation(CLIP_C, _build_identity_clip(preview, skeleton, duration))
	add_animation_library("", library)
	_source_clip_ids = PackedStringArray([CLIP_A, CLIP_B, CLIP_C])
	_review_ready = true
	assigned_animation = CLIP_A
	play(CLIP_A)


func review_set_player_ready() -> bool:
	return _review_ready


func review_set_source_clip_ids() -> PackedStringArray:
	return _source_clip_ids.duplicate()


func _build_identity_clip(preview: Node, skeleton: Skeleton3D, duration: float) -> Animation:
	var anim := Animation.new()
	anim.length = duration
	anim.loop_mode = Animation.LOOP_NONE
	var track := anim.add_track(Animation.TYPE_ROTATION_3D)
	var skel_path := preview.get_path_to(skeleton)
	anim.track_set_path(track, NodePath(str(skel_path) + ":Spine"))
	anim.rotation_track_insert_key(track, 0.0, Quaternion.IDENTITY)
	return anim


func _build_sparse_rotation_clip(
	preview: Node,
	skeleton: Skeleton3D,
	duration: float,
	bone_name: String,
	deg_start: float,
	deg_end: float,
) -> Animation:
	var anim := Animation.new()
	anim.length = duration
	anim.loop_mode = Animation.LOOP_NONE
	var track := anim.add_track(Animation.TYPE_ROTATION_3D)
	var skel_path := preview.get_path_to(skeleton)
	anim.track_set_path(track, NodePath(str(skel_path) + ":" + bone_name))
	var rot_start := Quaternion.from_euler(Vector3(deg_to_rad(deg_start), 0.0, 0.0))
	var rot_end := Quaternion.from_euler(Vector3(deg_to_rad(deg_end), 0.0, 0.0))
	anim.rotation_track_insert_key(track, 0.0, rot_start)
	anim.rotation_track_insert_key(track, duration * 0.75, rot_end)
	return anim
