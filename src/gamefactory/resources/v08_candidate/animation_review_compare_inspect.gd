extends SceneTree

const CLIP_A := "arm_wave_01"
const CLIP_B := "arm_reverse_02"
const CLIP_C := "arm_review_03"
const EPS := 0.0001
const ANGLE_EPS := 0.002
const READY_FRAMES := 240


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var context_path := _compare_context_path()
	if context_path.is_empty():
		_fail("compare context path missing")
		return
	var packed := load("res://animation_review_compare.tscn") as PackedScene
	if packed == null:
		_fail("compare scene missing")
		return
	var scene := packed.instantiate() as Control
	if scene == null:
		_fail("compare root invalid")
		return
	root.add_child(scene)
	if not await _wait_compare_ready(scene):
		_fail("compare controller not ready")
		return
	var left_side: Node3D = scene.get_node("%LeftSide")
	var preview := left_side.get_node_or_null("AnimationPreview")
	if preview == null:
		_fail("preview missing")
		return
	var player := preview.get_node_or_null("AnimationPlayer") as AnimationPlayer
	var skeleton := _find_skeleton(preview)
	if player == null or skeleton == null:
		_fail("player or skeleton missing")
		return
	if not await _validate_transport_rejections(scene):
		_fail("transport rejection contract failed")
		return
	if not await _validate_sparse_clip_reset(scene, skeleton, left_side, preview):
		_fail("sparse clip bone reset contract failed")
		return
	print("PASS: animation_review_compare_inspect")
	quit(0)


func _compare_context_path() -> String:
	const PREFIX := "--compare-context-file="
	for arg in OS.get_cmdline_args():
		var text := str(arg)
		if text.begins_with(PREFIX):
			return text.substr(PREFIX.length())
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(PREFIX):
			return text.substr(PREFIX.length())
	return ""


func _wait_compare_ready(scene: Control) -> bool:
	for _i in range(READY_FRAMES):
		var snap: Dictionary = scene.call("compare_snapshot")
		if snap.get("ready", false):
			return true
		await process_frame
	return false


func _validate_sparse_clip_reset(
	scene: Control, skeleton: Skeleton3D, left_side: Node3D, preview: Node
) -> bool:
	var arm_bone := skeleton.find_bone("LeftUpperArm")
	var spine_bone := skeleton.find_bone("Spine")
	if arm_bone < 0 or spine_bone < 0:
		return false
	var player := preview.get_node_or_null("AnimationPlayer") as AnimationPlayer
	if player == null:
		return false
	var clip_ids := left_side.call("side_source_clip_ids") as PackedStringArray
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	scene.call("request_scrub_normalized", 0.75)
	for _i in range(4):
		await process_frame
	var clip_a := player.get_animation(CLIP_A)
	var dur_a := float(scene.call("compare_snapshot").get("left_duration", 0.0))
	var expected_arm := _rotation_at_track_time(clip_a, "LeftUpperArm", 0.75 * dur_a)
	if skeleton.get_bone_pose_rotation(arm_bone).angle_to(expected_arm) > ANGLE_EPS:
		return false
	if skeleton.get_bone_pose_rotation(spine_bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		return false
	if clip_ids.has(CLIP_C):
		if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
			return false
		for _i in range(4):
			await process_frame
	if scene.call("request_set_left_clip", CLIP_B).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	if skeleton.get_bone_pose_rotation(arm_bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		return false
	scene.call("request_scrub_normalized", 0.75)
	for _i in range(4):
		await process_frame
	var clip_b := player.get_animation(CLIP_B)
	var dur_b := float(scene.call("compare_snapshot").get("left_duration", 0.0))
	var expected_spine := _rotation_at_track_time(clip_b, "Spine", 0.75 * dur_b)
	if skeleton.get_bone_pose_rotation(spine_bone).angle_to(expected_spine) > ANGLE_EPS:
		return false
	return true


func _validate_transport_rejections(scene: Control) -> bool:
	var before: Dictionary = scene.call("compare_snapshot")
	if scene.call("request_set_left_clip", CLIP_B).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_set_playback_speed", 9.0).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_scrub_normalized", true).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_pause").get("ok") != true:
		return false
	if scene.call("request_restart").get("ok") != true:
		return false
	var after_restart: Dictionary = scene.call("compare_snapshot")
	if float(after_restart.get("normalized_progress", -1.0)) > EPS:
		return false
	if bool(after_restart.get("playing", true)):
		return false
	return true


func _snapshots_equal(a: Dictionary, b: Dictionary) -> bool:
	for key in [
		"normalized_progress",
		"playing",
		"speed",
		"left_clip_id",
		"right_clip_id",
		"left_position",
		"right_position",
	]:
		if a.get(key) != b.get(key):
			return false
	return true


func _rotation_at_track_time(clip: Animation, bone_name: String, time_s: float) -> Quaternion:
	for t in range(clip.get_track_count()):
		if clip.track_get_type(t) != Animation.TYPE_ROTATION_3D:
			continue
		if str(clip.track_get_path(t).get_subname(0)) != bone_name:
			continue
		return clip.rotation_track_interpolate(t, time_s)
	return Quaternion.IDENTITY


func _find_skeleton(root: Node) -> Skeleton3D:
	if root is Skeleton3D:
		return root
	for child in root.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
