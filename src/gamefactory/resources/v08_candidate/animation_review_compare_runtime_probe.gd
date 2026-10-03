extends SceneTree

## Bounded SceneTree probe: in-memory sparse clips, compare side seam, controller requests.

const EPS := 0.0001
const ANGLE_EPS := 0.02
const READY_FRAMES := 120
const CLIP_A := "probe_clip_a"
const CLIP_B := "probe_clip_b"
const CLIP_C := "probe_clip_c"
const CONTEXT_PREFIX := "--compare-context-file="


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var context_path := _compare_context_path()
	if context_path.is_empty():
		_fail("compare context path missing")
		return
	var controller_script := load("res://animation_review_compare_controller.gd") as Script
	if controller_script == null:
		_fail("failed to load compare controller script")
		return
	var context_probe := Control.new()
	context_probe.set_script(controller_script)
	var load_err: String = context_probe.call("_load_compare_context_at_path", context_path)
	context_probe.free()
	if load_err != "":
		_fail(load_err)
		return
	if not _validate_context_file_contract():
		_fail("compare context rejection contract failed")
		return
	var controller := _build_compare_controller()
	if controller == null:
		_fail("failed to build compare controller harness")
		return
	root.add_child(controller)
	if not await _wait_compare_ready(controller):
		_fail("compare controller not ready")
		return
	var left_side: Node3D = controller.get_node("%LeftSide")
	var preview := left_side.get_node_or_null("AnimationPreview")
	if preview == null:
		_fail("preview missing")
		return
	var skeleton := _find_skeleton(preview)
	if skeleton == null:
		_fail("skeleton missing")
		return
	if not await _validate_side_sampling(controller, skeleton, preview):
		_fail("side manual sampling contract failed")
		return
	if not await _validate_controller_requests(controller, skeleton, preview):
		_fail("controller request contract failed")
		return
	print("PASS: animation_review_compare_runtime_probe")
	quit(0)


func _validate_context_file_contract() -> bool:
	var controller_script := load("res://animation_review_compare_controller.gd") as Script
	if controller_script == null:
		return false
	var probe := Control.new()
	probe.set_script(controller_script)
	var probe_dir := ProjectSettings.globalize_path("res://")
	var oversize_path := probe_dir.path_join("gf_compare_probe_oversize_context.json")
	var malformed_path := probe_dir.path_join("gf_compare_probe_malformed_context.json")
	DirAccess.remove_absolute(oversize_path)
	DirAccess.remove_absolute(malformed_path)
	var oversize_data := PackedByteArray()
	oversize_data.resize(8193)
	oversize_data.fill(0x20)
	if not _write_probe_bytes(oversize_path, oversize_data):
		DirAccess.remove_absolute(oversize_path)
		DirAccess.remove_absolute(malformed_path)
		probe.free()
		return false
	if probe.call("_load_compare_context_at_path", oversize_path) != "compare context exceeds allowed size":
		DirAccess.remove_absolute(oversize_path)
		DirAccess.remove_absolute(malformed_path)
		probe.free()
		return false
	if not _write_probe_bytes(malformed_path, "[1,2,3]".to_utf8_buffer()):
		DirAccess.remove_absolute(oversize_path)
		DirAccess.remove_absolute(malformed_path)
		probe.free()
		return false
	if probe.call("_load_compare_context_at_path", malformed_path) != "compare context is not an object":
		DirAccess.remove_absolute(oversize_path)
		DirAccess.remove_absolute(malformed_path)
		probe.free()
		return false
	DirAccess.remove_absolute(oversize_path)
	DirAccess.remove_absolute(malformed_path)
	probe.free()
	return true


func _write_probe_bytes(path: String, data: PackedByteArray) -> bool:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_buffer(data)
	file.close()
	return true


func _compare_context_path() -> String:
	for arg in OS.get_cmdline_args():
		var text := str(arg)
		if text.begins_with(CONTEXT_PREFIX):
			return text.substr(CONTEXT_PREFIX.length())
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(CONTEXT_PREFIX):
			return text.substr(CONTEXT_PREFIX.length())
	return ""


func _build_compare_controller() -> Control:
	var scene := Control.new()
	scene.set_script(load("res://animation_review_compare_controller.gd"))
	scene.set_anchors_preset(Control.PRESET_FULL_RECT)
	_add_unique(scene, scene, Label.new(), "ReadinessLabel")
	var left := _build_side("LeftSide")
	var right := _build_side("RightSide")
	_mount_side_in_viewport(scene, left, "LeftSide")
	_mount_side_in_viewport(scene, right, "RightSide")
	_add_unique(scene, scene, OptionButton.new(), "LeftClipOption")
	_add_unique(scene, scene, OptionButton.new(), "RightClipOption")
	_add_unique(scene, scene, Label.new(), "LeftTimeLabel")
	_add_unique(scene, scene, Label.new(), "RightTimeLabel")
	_add_unique(scene, scene, Label.new(), "NormalizedTimeLabel")
	_add_unique(scene, scene, Button.new(), "PlayButton")
	_add_unique(scene, scene, Button.new(), "PauseButton")
	_add_unique(scene, scene, Button.new(), "RestartButton")
	var slider := HSlider.new()
	slider.min_value = 0.0
	slider.max_value = 1.0
	_add_unique(scene, scene, slider, "SeekSlider")
	_add_unique(scene, scene, OptionButton.new(), "SpeedOption")
	return scene


func _add_unique(scene: Node, owner: Node, node: Node, node_name: String) -> void:
	node.name = node_name
	node.unique_name_in_owner = true
	owner.add_child(node)
	node.owner = scene


func _mount_side_in_viewport(scene: Control, side: Node3D, side_name: String) -> void:
	side.name = side_name
	side.unique_name_in_owner = true
	var viewport := SubViewport.new()
	viewport.size = Vector2i(640, 480)
	viewport.own_world_3d = true
	scene.add_child(viewport)
	viewport.add_child(side)
	side.owner = scene
	viewport.owner = scene


func _build_side(side_name: String) -> Node3D:
	var side := Node3D.new()
	side.set_script(load("res://animation_review_compare_side.gd"))
	var preview := Node3D.new()
	preview.name = "AnimationPreview"
	side.add_child(preview)
	var skeleton := Skeleton3D.new()
	skeleton.name = "Skeleton3D"
	var root_bone := skeleton.add_bone("Root")
	var spine := skeleton.add_bone("Spine")
	skeleton.set_bone_parent(spine, root_bone)
	var arm := skeleton.add_bone("LeftUpperArm")
	skeleton.set_bone_parent(arm, spine)
	preview.add_child(skeleton)
	var mesh := MeshInstance3D.new()
	mesh.name = "SM_HumanoidSkin"
	mesh.mesh = _make_skinned_box_mesh(skeleton, "LeftUpperArm")
	var skin := Skin.new()
	for bone_idx in range(skeleton.get_bone_count()):
		skin.add_bind(bone_idx, skeleton.get_bone_rest(bone_idx))
	mesh.skin = skin
	preview.add_child(mesh)
	mesh.skeleton = mesh.get_path_to(skeleton)
	var player := AnimationPlayer.new()
	player.name = "AnimationPlayer"
	player.set_script(load("res://animation_review_compare_probe_player.gd"))
	preview.add_child(player)
	var camera := Camera3D.new()
	camera.name = "ReviewCamera"
	side.add_child(camera)
	var light := DirectionalLight3D.new()
	light.name = "ReviewDirectionalLight"
	side.add_child(light)
	return side


func _wait_compare_ready(scene: Control) -> bool:
	for _i in range(READY_FRAMES):
		var snap: Dictionary = scene.call("compare_snapshot")
		if snap.get("ready", false):
			return true
		await process_frame
	return false


func _validate_side_sampling(scene: Control, skeleton: Skeleton3D, preview: Node) -> bool:
	var arm_bone := skeleton.find_bone("LeftUpperArm")
	var spine_bone := skeleton.find_bone("Spine")
	if arm_bone < 0 or spine_bone < 0:
		return false
	var player := preview.get_node_or_null("AnimationPlayer") as AnimationPlayer
	if player == null:
		return false
	var clip_a := player.get_animation(CLIP_A)
	if clip_a == null:
		return false
	if scene.call("request_scrub_normalized", 0.75).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	var dur_a := float(scene.call("compare_snapshot").get("left_duration", 0.0))
	var expected_arm := _rotation_at_track_time(clip_a, "LeftUpperArm", 0.75 * dur_a)
	if skeleton.get_bone_pose_rotation(arm_bone).angle_to(expected_arm) > ANGLE_EPS:
		return false
	if skeleton.get_bone_pose_rotation(spine_bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		return false
	return true


func _validate_controller_requests(scene: Control, skeleton: Skeleton3D, preview: Node) -> bool:
	var arm_bone := skeleton.find_bone("LeftUpperArm")
	var spine_bone := skeleton.find_bone("Spine")
	var player := preview.get_node_or_null("AnimationPlayer") as AnimationPlayer
	if player == null or arm_bone < 0 or spine_bone < 0:
		return false
	var before: Dictionary = scene.call("compare_snapshot")
	if scene.call("request_set_left_clip", CLIP_B).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
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
	if scene.call("request_scrub_normalized", 0.75).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	var clip_b := player.get_animation(CLIP_B)
	var dur_b := float(scene.call("compare_snapshot").get("left_duration", 0.0))
	var expected_spine := _rotation_at_track_time(clip_b, "Spine", 0.75 * dur_b)
	if skeleton.get_bone_pose_rotation(spine_bone).angle_to(expected_spine) > ANGLE_EPS:
		return false
	if scene.call("request_set_playback_speed", 9.0).get("ok") != false:
		return false
	if scene.call("request_restart").get("ok") != true:
		return false
	var after_restart: Dictionary = scene.call("compare_snapshot")
	if float(after_restart.get("normalized_progress", -1.0)) > EPS:
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


func _make_skinned_box_mesh(skeleton: Skeleton3D, bone_name: String) -> ArrayMesh:
	var bone_idx := skeleton.find_bone(bone_name)
	var half := Vector3(0.125, 0.25, 0.06)
	var corners := PackedVector3Array(
		[
			Vector3(-half.x, -half.y, -half.z),
			Vector3(half.x, -half.y, -half.z),
			Vector3(half.x, half.y, -half.z),
			Vector3(-half.x, half.y, -half.z),
		]
	)
	var st := SurfaceTool.new()
	st.begin(Mesh.PRIMITIVE_TRIANGLES)
	for idx in [0, 1, 2, 0, 2, 3]:
		st.set_bones(PackedInt32Array([bone_idx, 0, 0, 0]))
		st.set_weights(PackedFloat32Array([1.0, 0.0, 0.0, 0.0]))
		st.add_vertex(corners[idx])
	return st.commit()


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
