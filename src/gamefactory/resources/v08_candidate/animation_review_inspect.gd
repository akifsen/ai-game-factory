extends SceneTree

const BONES := [
	"Hips", "Spine", "Chest", "Neck", "Head", "LeftUpperArm", "LeftLowerArm", "LeftHand",
	"RightUpperArm", "RightLowerArm", "RightHand", "LeftUpperLeg",
]
const CLIP := "arm_wave_01"
const DURATION := 1.5
const EPS := 0.0001
const ANGLE_EPS := 0.002
const CLIP_JSON_PATH := "res://animation_clip.json"
const READY_FRAMES := 240
const VIEWPORT_SIZES := [
	Vector2i(1152, 648),
	Vector2i(1280, 720),
	Vector2i(960, 640),
]
const ORACLE_EDGE_MARGIN_PX := 14.0
const ORACLE_PANEL_CLEARANCE_PX := 10.0


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var numerics_only := _wants_flag("--gf-controller-numerics-only")
	var screenshot_path := _screenshot_path()
	var clip_bytes := _read_clip_bytes()
	var packed := load("res://animation_review.tscn") as PackedScene
	if packed == null:
		_fail("review scene missing")
		return
	var scene := packed.instantiate() as Node3D
	if scene == null:
		_fail("review root invalid")
		return
	root.add_child(scene)
	if not await _wait_review_ready(scene):
		_fail("review controller not ready")
		return
	var character := scene.get_node_or_null("AnimationPreview/Character") as Node3D
	if character == null:
		_fail("character missing")
		return
	for viewport_size in VIEWPORT_SIZES:
		DisplayServer.window_set_size(viewport_size)
		for _i in range(4):
			await process_frame
		if scene.has_method("review_refit_camera"):
			await scene.call("review_refit_camera")
		for _i in range(3):
			await process_frame
		if not _validate_camera_oracle(scene, character):
			_fail("camera framing oracle failed at %s" % str(viewport_size))
			return
	if not _validate_ui_controls(scene):
		_fail("review ui controls missing")
		return
	var controller := scene
	var player: AnimationPlayer = controller.call("review_animation_player") as AnimationPlayer
	if player == null:
		_fail("player/character missing")
		return
	if not _validate_initial_paused(controller, player):
		_fail("initial paused/seek state invalid")
		return
	var scene_snap := _capture_scene_geometry_snapshot(scene, player, character)
	var source_snap := _capture_source_animation_snapshot(controller, player)
	if not await _validate_transport(controller, player, scene):
		_fail("transport contract mismatch")
		return
	if not await _validate_seek_contract(controller, player, character, scene):
		_fail("seek contract mismatch")
		return
	if not await _validate_resize_preserves_playback(controller, player, scene):
		_fail("resize must not reset playback or seek")
		return
	if not await _validate_speed_contract(controller, player):
		_fail("speed contract mismatch")
		return
	if not await _validate_loop_contract(controller, player, scene):
		_fail("loop clone contract mismatch")
		return
	if clip_bytes != _read_clip_bytes():
		_fail("animation_clip.json bytes changed")
		return
	if not _scene_geometry_matches_snapshot(scene_snap, scene, player, character):
		_fail("scene contract mismatch")
		return
	if not _source_animation_matches_snapshot(source_snap, controller, player):
		_fail("source animation contract mismatch")
		return
	if numerics_only:
		print("PASS: animation_review_controller_numerics")
		if not screenshot_path.is_empty():
			await _capture_screenshot(screenshot_path)
		quit(0)
		return
	if not await _validate_arm_wave_deformation(player, character):
		_fail("weighted deformation mismatch")
		return
	print("PASS: animation_review_interactive")
	if not screenshot_path.is_empty():
		await _capture_screenshot(screenshot_path)
	quit(0)


func _wants_flag(flag: String) -> bool:
	for arg in OS.get_cmdline_user_args():
		if str(arg) == flag:
			return true
	return false


func _screenshot_path() -> String:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with("--gf-screenshot="):
			return text.substr("--gf-screenshot=".length())
	return ""


func _read_clip_bytes() -> PackedByteArray:
	return FileAccess.get_file_as_bytes(CLIP_JSON_PATH)


func _validate_camera_oracle(scene: Node3D, character: Node3D) -> bool:
	var camera := scene.get_node_or_null("ReviewCamera") as Camera3D
	var mesh := _find_mesh(character)
	var panel := scene.get_node_or_null("UI/Root/Margin/Panel") as Control
	if camera == null or mesh == null or panel == null:
		return false
	var baked := mesh.bake_mesh_from_current_skeleton_pose()
	if baked == null or baked.get_surface_count() != 1:
		return false
	var local_vertices: PackedVector3Array = baked.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]
	if local_vertices.is_empty():
		return false
	var mesh_xf := mesh.global_transform
	var world_min := Vector3(INF, INF, INF)
	var world_max := Vector3(-INF, -INF, -INF)
	var screen_min := Vector2(INF, INF)
	var screen_max := Vector2(-INF, -INF)
	var vp_size := get_root().get_viewport().get_visible_rect().size
	var panel_top := panel.get_global_rect().position.y
	var safe := Rect2(
		ORACLE_EDGE_MARGIN_PX,
		ORACLE_EDGE_MARGIN_PX,
		maxf(8.0, vp_size.x - ORACLE_EDGE_MARGIN_PX * 2.0),
		maxf(8.0, panel_top - ORACLE_PANEL_CLEARANCE_PX - ORACLE_EDGE_MARGIN_PX)
	)
	for i in range(local_vertices.size()):
		var world := mesh_xf * local_vertices[i]
		world_min = world_min.min(world)
		world_max = world_max.max(world)
		if camera.is_position_behind(world):
			print(
				"GF_REVIEW_CAMERA_ORACLE behind vertex index=%d world=%s camera=%s"
				% [i, str(world), str(camera.global_position)]
			)
			return false
		var screen := camera.unproject_position(world)
		screen_min.x = minf(screen_min.x, screen.x)
		screen_min.y = minf(screen_min.y, screen.y)
		screen_max.x = maxf(screen_max.x, screen.x)
		screen_max.y = maxf(screen_max.y, screen.y)
		if screen.x < safe.position.x or screen.y < safe.position.y:
			return false
		if screen.x > safe.position.x + safe.size.x or screen.y > safe.position.y + safe.size.y:
			return false
	print(
		"GF_REVIEW_CAMERA_ORACLE viewport=%s panel_top=%.2f safe=%s world_aabb_min=%s world_aabb_max=%s screen_min=%s screen_max=%s camera_pos=%s fov=%.2f"
		% [
			str(vp_size),
			panel_top,
			str(safe),
			str(world_min),
			str(world_max),
			str(screen_min),
			str(screen_max),
			str(camera.global_position),
			camera.fov,
		]
	)
	return true


func _wait_review_ready(scene: Node) -> bool:
	for _i in range(READY_FRAMES):
		if scene.has_method("review_is_ready") and scene.call("review_is_ready"):
			return true
		await process_frame
	return false


func _validate_ui_controls(scene: Node) -> bool:
	var names := [
		"PlayButton", "PauseButton", "RestartButton", "SeekSlider", "LoopCheck", "SpeedOption",
		"TimeLabel", "DurationLabel",
	]
	for node_name in names:
		if scene.get_node_or_null("%" + node_name) == null:
			return false
	return true


func _validate_initial_paused(controller: Node, player: AnimationPlayer) -> bool:
	if player.is_playing():
		return false
	if absf(player.current_animation_position) > EPS:
		return false
	if not controller.has_method("review_duration_seconds"):
		return false
	return absf(float(controller.call("review_duration_seconds")) - DURATION) <= EPS


func _validate_transport(controller: Node, player: AnimationPlayer, scene: Node) -> bool:
	var play_button: Button = scene.get_node_or_null("%PlayButton") as Button
	var pause_button: Button = scene.get_node_or_null("%PauseButton") as Button
	var restart_button: Button = scene.get_node_or_null("%RestartButton") as Button
	if play_button == null or pause_button == null or restart_button == null:
		return false
	if player.is_playing():
		return false
	var start := player.current_animation_position
	play_button.pressed.emit()
	for _i in range(8):
		await process_frame
	var advanced := player.current_animation_position
	if advanced <= start + 0.01:
		return false
	var paused_at := player.current_animation_position
	pause_button.pressed.emit()
	for _i in range(4):
		await process_frame
	if absf(player.current_animation_position - paused_at) > EPS:
		return false
	play_button.pressed.emit()
	for _i in range(4):
		await process_frame
	if player.current_animation_position <= paused_at + 0.005:
		return false
	pause_button.pressed.emit()
	await process_frame
	restart_button.pressed.emit()
	if absf(player.current_animation_position) > EPS:
		return false
	if not player.is_playing():
		return false
	for _i in range(8):
		await process_frame
	if player.current_animation_position <= 0.01:
		return false
	return true


func _validate_seek_contract(
	controller: Node, player: AnimationPlayer, character: Node3D, scene: Node
) -> bool:
	var skeleton := _find_skeleton(character)
	if skeleton == null:
		return false
	var seek_slider: HSlider = scene.get_node_or_null("%SeekSlider") as HSlider
	if seek_slider == null:
		return false
	var bone := skeleton.find_bone("LeftUpperArm")
	var targets := [0.0, 0.75, 1.5]
	for target in targets:
		seek_slider.set_value_no_signal(target)
		seek_slider.value_changed.emit(target)
		for _i in range(3):
			await process_frame
		if absf(player.current_animation_position - target) > EPS:
			return false
		var expected := Quaternion.IDENTITY if target <= EPS else Quaternion(Vector3.UP, deg_to_rad(30.0 if absf(target - 0.75) < EPS else 0.0))
		if target >= DURATION - EPS:
			expected = Quaternion.IDENTITY
		if skeleton.get_bone_pose_rotation(bone).angle_to(expected) > ANGLE_EPS:
			return false
	if controller.call("request_seek", true).get("ok") != false:
		return false
	if controller.call("request_seek", 2.0).get("ok") != false:
		return false
	return true


func _validate_resize_preserves_playback(
	controller: Node, player: AnimationPlayer, scene: Node
) -> bool:
	var seek_slider: HSlider = scene.get_node_or_null("%SeekSlider") as HSlider
	var time_label: Label = scene.get_node_or_null("%TimeLabel") as Label
	var restart_button: Button = scene.get_node_or_null("%RestartButton") as Button
	if seek_slider == null or time_label == null or restart_button == null:
		return false
	controller.call("request_seek", 0.75)
	for _i in range(4):
		await process_frame
	controller.call("request_pause")
	for _i in range(4):
		await process_frame
	if absf(player.current_animation_position - 0.75) > EPS:
		return false
	if player.is_playing():
		return false
	if absf(seek_slider.value - 0.75) > EPS:
		return false
	if not time_label.text.begins_with("0.750"):
		return false
	DisplayServer.window_set_size(Vector2i(840, 560))
	for _i in range(6):
		await process_frame
	if scene.has_method("review_refit_camera"):
		await scene.call("review_refit_camera")
	for _i in range(4):
		await process_frame
	if absf(player.current_animation_position - 0.75) > EPS:
		return false
	if player.is_playing():
		return false
	if absf(seek_slider.value - 0.75) > EPS:
		return false
	if not time_label.text.begins_with("0.750"):
		return false
	restart_button.pressed.emit()
	for _i in range(8):
		await process_frame
	if not player.is_playing():
		return false
	var advanced_from_start := player.current_animation_position
	if advanced_from_start <= 0.01:
		return false
	DisplayServer.window_set_size(Vector2i(1180, 700))
	for _i in range(6):
		await process_frame
	if scene.has_method("review_refit_camera"):
		await scene.call("review_refit_camera")
	for _i in range(10):
		await process_frame
	if not player.is_playing():
		return false
	if player.current_animation_position <= advanced_from_start + 0.005:
		return false
	return true


func _validate_speed_contract(controller: Node, player: AnimationPlayer) -> bool:
	var speed_option: OptionButton = controller.get_node_or_null("%SpeedOption") as OptionButton
	if speed_option == null:
		return false
	var allowed := [0.25, 0.5, 1.0, 2.0]
	for speed in allowed:
		var before := player.speed_scale
		var result: Dictionary = controller.call("request_set_playback_speed", speed)
		if result.get("ok") != true:
			return false
		if player.speed_scale != speed:
			return false
		var expected_index := allowed.find(speed)
		if speed_option.selected != expected_index:
			return false
	var near_bad := [0.25000001, 1.000001]
	for speed in near_bad:
		var before := player.speed_scale
		var result: Dictionary = controller.call("request_set_playback_speed", speed)
		if result.get("ok") != false:
			return false
		if player.speed_scale != before:
			return false
	var bad := [true, 0, -1.0, INF, NAN, 3.7]
	for speed in bad:
		var before := player.speed_scale
		var result: Dictionary = controller.call("request_set_playback_speed", speed)
		if result.get("ok") != false:
			return false
		if player.speed_scale != before:
			return false
	speed_option.select(1)
	speed_option.item_selected.emit(1)
	await process_frame
	if player.speed_scale != 0.5:
		return false
	return true


func _validate_loop_contract(controller: Node, player: AnimationPlayer, scene: Node) -> bool:
	var source_id := str(controller.call("review_source_clip_id"))
	var source_anim := player.get_animation(source_id)
	var clone_anim := controller.call("review_clone_animation") as Animation
	if source_anim == null or clone_anim == null:
		return false
	var source_loop := source_anim.loop_mode
	var source_length := source_anim.length
	var source_tracks := source_anim.get_track_count()
	var loop_check: CheckButton = scene.get_node_or_null("%LoopCheck") as CheckButton
	if loop_check == null:
		return false
	controller.call("request_seek", 0.75)
	await process_frame
	controller.call("request_pause")
	loop_check.set_pressed_no_signal(true)
	loop_check.toggled.emit(true)
	await process_frame
	if clone_anim.loop_mode != Animation.LOOP_LINEAR:
		return false
	if source_anim.loop_mode != source_loop:
		return false
	if controller.call("request_set_loop", 1).get("ok") != false:
		return false
	if controller.call("request_set_loop", true).get("ok") != true:
		return false
	await process_frame
	if not loop_check.button_pressed:
		return false
	if clone_anim.loop_mode != Animation.LOOP_LINEAR:
		return false
	if controller.call("request_set_loop", false).get("ok") != true:
		return false
	await process_frame
	if loop_check.button_pressed:
		return false
	if clone_anim.loop_mode != Animation.LOOP_NONE:
		return false
	if source_anim.length != source_length or source_anim.get_track_count() != source_tracks:
		return false
	return true


func _capture_scene_geometry_snapshot(
	scene: Node3D, player: AnimationPlayer, character: Node3D
) -> Dictionary:
	var skeleton := _find_skeleton(character)
	var mesh := _find_mesh(character)
	var body := character.get_node_or_null("PhysicsBody") as StaticBody3D
	var capsule_node := character.get_node_or_null("PhysicsBody/CollisionShape3D") as CollisionShape3D
	var capsule := capsule_node.shape as CapsuleShape3D if capsule_node != null else null
	var fixed_nodes: Array[Node3D] = [scene, character, body, capsule_node, skeleton, mesh]
	var transforms: Array[Transform3D] = []
	for node in fixed_nodes:
		transforms.append(node.global_transform if node != null else Transform3D())
	var capsule_size := Vector2.ZERO
	if capsule != null:
		capsule_size = Vector2(capsule.radius, capsule.height)
	var bone_ids: PackedInt32Array = PackedInt32Array()
	for bone_name in BONES:
		bone_ids.append(skeleton.find_bone(bone_name) if skeleton != null else -1)
	return {
		"transforms": transforms,
		"capsule_size": capsule_size,
		"bone_ids": bone_ids,
		"mesh_skeleton": mesh.skeleton if mesh != null else NodePath(),
		"bone_count": skeleton.get_bone_count() if skeleton != null else 0,
	}


func _scene_geometry_matches_snapshot(
	snap: Dictionary, scene: Node3D, player: AnimationPlayer, character: Node3D
) -> bool:
	var skeleton := _find_skeleton(character)
	var mesh := _find_mesh(character)
	if skeleton == null or mesh == null or mesh.skin == null:
		return false
	if mesh.get_node_or_null(mesh.skeleton) != skeleton:
		return false
	if skeleton.get_bone_count() != int(snap["bone_count"]):
		return false
	var bone_ids: PackedInt32Array = snap["bone_ids"]
	for i in range(BONES.size()):
		if skeleton.find_bone(BONES[i]) != bone_ids[i]:
			return false
	var clip := player.get_animation(CLIP)
	if clip == null or clip.get_track_count() != 2 or absf(clip.length - DURATION) > EPS:
		return false
	if clip.loop_mode != Animation.LOOP_NONE:
		return false
	if not _validate_arm_wave_tracks(player, skeleton, clip):
		return false
	var body := character.get_node_or_null("PhysicsBody") as StaticBody3D
	var capsule_node := character.get_node_or_null("PhysicsBody/CollisionShape3D") as CollisionShape3D
	if body == null or capsule_node == null or not (capsule_node.shape is CapsuleShape3D):
		return false
	var capsule := capsule_node.shape as CapsuleShape3D
	var fixed_nodes: Array[Node3D] = [scene, character, body, capsule_node, skeleton, mesh]
	var transforms: Array = snap["transforms"]
	for i in range(fixed_nodes.size()):
		if not fixed_nodes[i].global_transform.is_equal_approx(transforms[i]):
			return false
	var capsule_size: Vector2 = snap["capsule_size"]
	if Vector2(capsule.radius, capsule.height).distance_to(capsule_size) > EPS:
		return false
	return true


func _capture_source_animation_snapshot(controller: Node, player: AnimationPlayer) -> Dictionary:
	var source_id := str(controller.call("review_source_clip_id"))
	var clip := player.get_animation(source_id)
	if clip == null:
		return {}
	var track_data: Array = []
	for t in range(clip.get_track_count()):
		var keys: Array = []
		for k in range(clip.track_get_key_count(t)):
			keys.append(
				{
					"time": clip.track_get_key_time(t, k),
					"value": clip.track_get_key_value(t, k),
				}
			)
		track_data.append(
			{
				"path": str(clip.track_get_path(t)),
				"type": clip.track_get_type(t),
				"keys": keys,
			}
		)
	return {
		"length": clip.length,
		"loop_mode": clip.loop_mode,
		"track_count": clip.get_track_count(),
		"tracks": track_data,
	}


func _source_animation_matches_snapshot(snap: Dictionary, controller: Node, player: AnimationPlayer) -> bool:
	if snap.is_empty():
		return false
	var source_id := str(controller.call("review_source_clip_id"))
	var clip := player.get_animation(source_id)
	if clip == null:
		return false
	if clip.length != snap["length"]:
		return false
	if clip.loop_mode != snap["loop_mode"]:
		return false
	if clip.get_track_count() != snap["track_count"]:
		return false
	var tracks: Array = snap["tracks"]
	for t in range(clip.get_track_count()):
		var stored: Dictionary = tracks[t]
		if str(clip.track_get_path(t)) != str(stored["path"]):
			return false
		if clip.track_get_type(t) != int(stored["type"]):
			return false
		var keys: Array = stored["keys"]
		if clip.track_get_key_count(t) != keys.size():
			return false
		for k in range(keys.size()):
			var key_info: Dictionary = keys[k]
			if clip.track_get_key_time(t, k) != float(key_info["time"]):
				return false
			var observed = clip.track_get_key_value(t, k)
			var expected = key_info["value"]
			if observed is Quaternion and expected is Quaternion:
				if observed.angle_to(expected) > ANGLE_EPS:
					return false
			elif observed != expected:
				return false
	return true


func _validate_arm_wave_deformation(player: AnimationPlayer, character: Node3D) -> bool:
	var skeleton := _find_skeleton(character)
	var mesh := _find_mesh(character)
	if skeleton == null or mesh == null:
		return false
	var bone := skeleton.find_bone("LeftUpperArm")
	player.pause()
	player.seek(0.0, true)
	for _i in range(3):
		await process_frame
	if skeleton.get_bone_pose_rotation(bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		return false
	var rest := _vertices(mesh.bake_mesh_from_current_skeleton_pose())
	player.seek(0.75, true)
	for _i in range(3):
		await process_frame
	if skeleton.get_bone_pose_rotation(bone).angle_to(Quaternion(Vector3.UP, deg_to_rad(30.0))) > ANGLE_EPS:
		return false
	var posed := _vertices(mesh.bake_mesh_from_current_skeleton_pose())
	if rest.is_empty() or rest.size() != posed.size():
		return false
	var affected_delta := 0.0
	var unaffected_delta := 0.0
	var affected_count := 0
	var unaffected_count := 0
	for i in range(rest.size()):
		var delta := rest[i].distance_to(posed[i])
		if _inside(rest[i], Vector3(-0.65, 1.25, -0.12), Vector3(-0.15, 1.55, 0.12)):
			affected_count += 1
			affected_delta = maxf(affected_delta, delta)
		if _inside(rest[i], Vector3(-0.12, 0.85, -0.12), Vector3(0.12, 1.15, 0.12)):
			unaffected_count += 1
			unaffected_delta = maxf(unaffected_delta, delta)
	return affected_count > 0 and unaffected_count > 0 and affected_delta >= 0.012 and affected_delta <= 0.35 and unaffected_delta <= 0.008


func _validate_arm_wave_tracks(player: AnimationPlayer, skeleton: Skeleton3D, clip: Animation) -> bool:
	var mixer_root := player.get_node_or_null(player.root_node)
	if mixer_root == null:
		return false
	var left_track := -1
	var spine_track := -1
	for t in range(clip.get_track_count()):
		if clip.track_get_type(t) != Animation.TYPE_ROTATION_3D:
			return false
		var track_path := clip.track_get_path(t)
		if track_path.get_subname_count() != 1:
			return false
		var bone_name := str(track_path.get_subname(0))
		if mixer_root.get_node_or_null(NodePath(str(track_path).get_slice(":", 0))) != skeleton:
			return false
		if bone_name == "LeftUpperArm":
			left_track = t
		elif bone_name == "Spine":
			spine_track = t
		else:
			return false
	if left_track < 0 or spine_track < 0:
		return false
	if clip.track_get_key_count(left_track) != 3:
		return false
	var left_times := [0.0, 0.75, 1.5]
	for i in range(3):
		if absf(clip.track_get_key_time(left_track, i) - left_times[i]) > EPS:
			return false
		var expected := Quaternion(Vector3.UP, deg_to_rad(30.0 if i == 1 else 0.0))
		var observed: Quaternion = clip.track_get_key_value(left_track, i)
		if observed.angle_to(expected) > ANGLE_EPS:
			return false
	if clip.track_get_key_count(spine_track) != 1:
		return false
	if absf(clip.track_get_key_time(spine_track, 0)) > EPS:
		return false
	var spine_rot: Quaternion = clip.track_get_key_value(spine_track, 0)
	return spine_rot.angle_to(Quaternion.IDENTITY) <= ANGLE_EPS


func _inside(p: Vector3, mins: Vector3, maxs: Vector3) -> bool:
	return p.x >= mins.x - EPS and p.x <= maxs.x + EPS and p.y >= mins.y - EPS and p.y <= maxs.y + EPS and p.z >= mins.z - EPS and p.z <= maxs.z + EPS


func _vertices(mesh: Mesh) -> PackedVector3Array:
	if mesh == null or mesh.get_surface_count() != 1:
		return PackedVector3Array()
	return mesh.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]


func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null


func _find_mesh(node: Node) -> MeshInstance3D:
	if node is MeshInstance3D and node.name == "SM_HumanoidSkin":
		return node
	for child in node.get_children():
		var found := _find_mesh(child)
		if found != null:
			return found
	return null


func _capture_screenshot(path: String) -> void:
	for _i in range(6):
		await process_frame
	var image := root.get_viewport().get_texture().get_image()
	if image == null:
		_fail("screenshot image unavailable")
		return
	var err := image.save_png(path)
	if err != OK:
		_fail("screenshot save failed")


func _fail(message: String) -> void:
	print("FAIL: ", message)
	quit(1)
