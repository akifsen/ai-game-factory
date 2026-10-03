extends SceneTree

## V0.8-12b ACTUAL acceptance: dual SubViewport compare oracle on trusted review-set overlay.

const BONES := [
	"Hips", "Spine", "Chest", "Neck", "Head", "LeftUpperArm", "LeftLowerArm", "LeftHand",
	"RightUpperArm", "RightLowerArm", "RightHand", "LeftUpperLeg",
]
const BONE_PARENTS := {
	"Hips": "",
	"Spine": "Hips",
	"Chest": "Spine",
	"Neck": "Chest",
	"Head": "Neck",
	"LeftUpperArm": "Chest",
	"LeftLowerArm": "LeftUpperArm",
	"LeftHand": "LeftLowerArm",
	"RightUpperArm": "Chest",
	"RightLowerArm": "RightUpperArm",
	"RightHand": "RightLowerArm",
	"LeftUpperLeg": "Hips",
}
const COLLIDER_JSON_PATH := "res://collider.json"
const VIEWPORT_EDGE_MARGIN_PX := 14.0
const SKIN_MATCH_EPS := 0.01
const CLIP_A := "arm_wave_01"
const CLIP_B := "arm_reverse_02"
const CLIP_C := "spine_turn_03"
const DURATION_A := 1.5
const DURATION_B := 2.0
const DURATION_C := 1.25
const EPS := 0.0001
const ANGLE_EPS := 0.002
const VERT_EPS := 0.008
const DEFORM_MIN := 0.012
const DEFORM_MAX := 0.35
const READY_FRAMES := 240
const VIEWPORT_SIZES := [Vector2i(1280, 720), Vector2i(720, 1280)]
const ALLOWED_SPEEDS := [0.25, 0.5, 1.0, 2.0]
const CONTEXT_PREFIX := "--compare-context-file="
const ARG_RESULT := "--gf-compare-inspect-result="

var _last_subviewport_oracle_diagnostic: String = ""


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var context_path := _compare_context_path()
	if context_path.is_empty():
		_fail("compare context path missing")
		return
	var result_path := _result_path()
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
	var right_side: Node3D = scene.get_node("%RightSide")
	if not _validate_initial_selection(scene):
		_fail("initial compare selection must be A left and C right")
		return
	if not _validate_dual_pane_rig(left_side, right_side):
		_fail("dual pane rig oracle failed")
		return
	if not await _validate_subviewport_oracles(scene):
		_fail("subviewport render oracle failed: %s" % _last_subviewport_oracle_diagnostic)
		return
	if not await _validate_rest_pose_resets(scene, left_side, right_side):
		_fail("rest pose reset contract failed")
		return
	if not await _validate_weighted_deformation_oracle(scene, left_side, right_side):
		_fail("weighted mesh deformation oracle failed")
		return
	if not await _validate_synchronized_transport(scene):
		_fail("synchronized transport contract failed")
		return
	if not await _validate_selector_and_ui_rejections(scene):
		_fail("clip selector rejection contract failed")
		return
	if not await _validate_end_hold_no_drift(scene):
		_fail("end hold drift detected")
		return
	if not result_path.is_empty() and not _write_result(result_path, {"ok": true}):
		_fail("compare inspect result write failed")
		return
	print("PASS: animation_review_compare_inspect")
	quit(0)


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


func _result_path() -> String:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(ARG_RESULT):
			return text.substr(ARG_RESULT.length())
	return ""


func _write_result(path: String, payload: Dictionary) -> bool:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_string(JSON.stringify(payload))
	file.close()
	return true


func _wait_compare_ready(scene: Control) -> bool:
	for _i in range(READY_FRAMES):
		var snap: Dictionary = scene.call("compare_snapshot")
		if snap.get("ready", false):
			return true
		await process_frame
	return false


func _validate_initial_selection(scene: Control) -> bool:
	var snap: Dictionary = scene.call("compare_snapshot")
	return snap.get("left_clip_id") == CLIP_A and snap.get("right_clip_id") == CLIP_C


func _character(side: Node3D) -> Node3D:
	return side.get_node_or_null("AnimationPreview/Character") as Node3D


func _preview_player(side: Node3D) -> AnimationPlayer:
	var preview := side.get_node_or_null("AnimationPreview")
	if preview == null:
		return null
	return preview.get_node_or_null("AnimationPlayer") as AnimationPlayer


func _validate_dual_pane_rig(left_side: Node3D, right_side: Node3D) -> bool:
	var left_player := _preview_player(left_side)
	var right_player := _preview_player(right_side)
	if left_player == null or right_player == null or left_player == right_player:
		return false
	var collider := _load_collider_contract()
	if collider.is_empty():
		return false
	var reference_capsule := Vector2.ZERO
	for side in [left_side, right_side]:
		var character := _character(side)
		if character == null:
			return false
		var skeleton := _find_skeleton(character)
		var mesh := _find_mesh(character)
		if skeleton == null or mesh == null or mesh.skin == null:
			return false
		if mesh.get_node_or_null(mesh.skeleton) != skeleton:
			return false
		if skeleton.get_bone_count() != BONES.size():
			return false
		for bone_name in BONES:
			var bone_idx := skeleton.find_bone(bone_name)
			if bone_idx < 0:
				return false
			var parent_name: String = BONE_PARENTS.get(bone_name, bone_name)
			var expected_parent := -1 if parent_name.is_empty() else skeleton.find_bone(parent_name)
			if expected_parent < 0 and not parent_name.is_empty():
				return false
			if skeleton.get_bone_parent(bone_idx) != expected_parent:
				return false
		if not _mesh_has_twelve_bone_skin_binding(mesh, skeleton):
			return false
		var body := character.get_node_or_null("PhysicsBody") as StaticBody3D
		var capsule_node := character.get_node_or_null("PhysicsBody/CollisionShape3D") as CollisionShape3D
		if body == null or capsule_node == null or not (capsule_node.shape is CapsuleShape3D):
			return false
		var capsule := capsule_node.shape as CapsuleShape3D
		if not _capsule_matches_collider_contract(capsule, capsule_node, collider):
			return false
		if reference_capsule == Vector2.ZERO:
			reference_capsule = Vector2(capsule.radius, capsule.height)
		elif Vector2(capsule.radius, capsule.height).distance_to(reference_capsule) > EPS:
			return false
		var baked := mesh.bake_mesh_from_current_skeleton_pose()
		if baked == null or baked.get_surface_count() != 1:
			return false
		var verts: PackedVector3Array = baked.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]
		if verts.is_empty():
			return false
	var left_ids := left_side.call("side_source_clip_ids") as PackedStringArray
	var right_ids := right_side.call("side_source_clip_ids") as PackedStringArray
	if left_ids.size() != 3 or right_ids.size() != 3:
		return false
	for i in range(3):
		if left_ids[i] != right_ids[i]:
			return false
	return left_ids.has(CLIP_A) and left_ids.has(CLIP_B) and left_ids.has(CLIP_C)


func _subviewport_for_side(scene: Control, is_left: bool) -> SubViewport:
	var path := (
		"ViewportSplit/LeftColumn/LeftViewportContainer/LeftViewport"
		if is_left
		else "ViewportSplit/RightColumn/RightViewportContainer/RightViewport"
	)
	return scene.get_node_or_null(path) as SubViewport


func _record_subviewport_oracle_fail(branch: String, detail: Dictionary) -> bool:
	var payload := {"branch": branch}
	payload.merge(detail, true)
	_last_subviewport_oracle_diagnostic = JSON.stringify(payload)
	print("FAIL_SUBVIEWPORT_ORACLE: ", _last_subviewport_oracle_diagnostic)
	return false


func _rect2_payload(rect: Rect2) -> Dictionary:
	return {
		"position": [rect.position.x, rect.position.y],
		"size": [rect.size.x, rect.size.y],
	}


func _vector2i_payload(value: Vector2i) -> Array:
	return [value.x, value.y]


func _validate_subviewport_oracles(scene: Control) -> bool:
	_last_subviewport_oracle_diagnostic = ""
	var prev_left_rect := Rect2()
	var prev_right_rect := Rect2()
	var prev_left_vp_size := Vector2i.ZERO
	var prev_right_vp_size := Vector2i.ZERO
	for viewport_size in VIEWPORT_SIZES:
		DisplayServer.window_set_size(viewport_size)
		for _i in range(12):
			await process_frame
		var left_vp := _subviewport_for_side(scene, true)
		var right_vp := _subviewport_for_side(scene, false)
		if left_vp == null or right_vp == null:
			return _record_subviewport_oracle_fail(
				"subviewport_missing",
				{
					"window_size": _vector2i_payload(viewport_size),
					"left_vp_null": left_vp == null,
					"right_vp_null": right_vp == null,
				},
			)
		if not left_vp.own_world_3d or not right_vp.own_world_3d:
			return _record_subviewport_oracle_fail(
				"own_world_3d",
				{
					"window_size": _vector2i_payload(viewport_size),
					"left_own_world_3d": left_vp.own_world_3d,
					"right_own_world_3d": right_vp.own_world_3d,
				},
			)
		if left_vp.size.x < 8 or left_vp.size.y < 8 or right_vp.size.x < 8 or right_vp.size.y < 8:
			return _record_subviewport_oracle_fail(
				"subviewport_size",
				{
					"window_size": _vector2i_payload(viewport_size),
					"left_vp_size": _vector2i_payload(left_vp.size),
					"right_vp_size": _vector2i_payload(right_vp.size),
				},
			)
		var left_rect := left_vp.get_visible_rect()
		var right_rect := right_vp.get_visible_rect()
		if prev_left_vp_size != Vector2i.ZERO:
			if left_vp.size == prev_left_vp_size or right_vp.size == prev_right_vp_size:
				return _record_subviewport_oracle_fail(
					"resize_vp_size_unchanged",
					{
						"window_size": _vector2i_payload(viewport_size),
						"left_vp_size": _vector2i_payload(left_vp.size),
						"right_vp_size": _vector2i_payload(right_vp.size),
						"prev_left_vp_size": _vector2i_payload(prev_left_vp_size),
						"prev_right_vp_size": _vector2i_payload(prev_right_vp_size),
					},
				)
			if left_rect.size == prev_left_rect.size or right_rect.size == prev_right_rect.size:
				return _record_subviewport_oracle_fail(
					"resize_visible_rect_unchanged",
					{
						"window_size": _vector2i_payload(viewport_size),
						"left_visible_rect": _rect2_payload(left_rect),
						"right_visible_rect": _rect2_payload(right_rect),
						"prev_left_visible_rect": _rect2_payload(prev_left_rect),
						"prev_right_visible_rect": _rect2_payload(prev_right_rect),
					},
				)
		prev_left_rect = left_rect
		prev_right_rect = right_rect
		prev_left_vp_size = left_vp.size
		prev_right_vp_size = right_vp.size
		if left_rect.size.x < 8.0 or left_rect.size.y < 8.0:
			return _record_subviewport_oracle_fail(
				"left_visible_rect",
				{
					"window_size": _vector2i_payload(viewport_size),
					"left_visible_rect": _rect2_payload(left_rect),
					"left_vp_size": _vector2i_payload(left_vp.size),
				},
			)
		if right_rect.size.x < 8.0 or right_rect.size.y < 8.0:
			return _record_subviewport_oracle_fail(
				"right_visible_rect",
				{
					"window_size": _vector2i_payload(viewport_size),
					"right_visible_rect": _rect2_payload(right_rect),
					"right_vp_size": _vector2i_payload(right_vp.size),
				},
			)
		var left_assigned := left_vp.world_3d
		var right_assigned := right_vp.world_3d
		var left_effective := left_vp.find_world_3d()
		var right_effective := right_vp.find_world_3d()
		var world_oracle := {
			"window_size": _vector2i_payload(viewport_size),
			"left_vp_size": _vector2i_payload(left_vp.size),
			"right_vp_size": _vector2i_payload(right_vp.size),
			"left_assigned_null": left_assigned == null,
			"right_assigned_null": right_assigned == null,
			"assigned_worlds_equal": left_assigned == right_assigned,
		}
		if left_effective == null or right_effective == null:
			world_oracle["left_effective_null"] = left_effective == null
			world_oracle["right_effective_null"] = right_effective == null
			return _record_subviewport_oracle_fail("shared_world_3d", world_oracle)
		world_oracle["left_effective_world_id"] = left_effective.get_instance_id()
		world_oracle["right_effective_world_id"] = right_effective.get_instance_id()
		world_oracle["left_scenario_rid"] = left_effective.scenario.get_id()
		world_oracle["right_scenario_rid"] = right_effective.scenario.get_id()
		if left_effective == right_effective:
			world_oracle["effective_same_object"] = true
			return _record_subviewport_oracle_fail("shared_world_3d", world_oracle)
		if left_effective.scenario == right_effective.scenario:
			world_oracle["scenario_rid_same"] = true
			return _record_subviewport_oracle_fail("shared_world_3d", world_oracle)
		var left_side: Node3D = scene.get_node("%LeftSide")
		var right_side: Node3D = scene.get_node("%RightSide")
		var scrub0: Variant = scene.call("request_scrub_normalized", 0.0)
		if scrub0.get("ok") != true:
			return _record_subviewport_oracle_fail(
				"scrub_normalized_0",
				{
					"window_size": _vector2i_payload(viewport_size),
					"scrub_result": scrub0,
				},
			)
		for _i in range(8):
			await process_frame
		left_side.call("side_refit_camera")
		right_side.call("side_refit_camera")
		for _i in range(12):
			await process_frame
		var left_framing := _camera_framing_report(left_side)
		if not bool(left_framing.get("ok", false)):
			return _record_subviewport_oracle_fail("camera_framing_left", left_framing)
		var right_framing := _camera_framing_report(right_side)
		if not bool(right_framing.get("ok", false)):
			return _record_subviewport_oracle_fail("camera_framing_right", right_framing)
		var scrub_half: Variant = scene.call("request_scrub_normalized", 0.5)
		if scrub_half.get("ok") != true:
			return _record_subviewport_oracle_fail(
				"scrub_normalized_half",
				{
					"window_size": _vector2i_payload(viewport_size),
					"scrub_result": scrub_half,
				},
			)
		for _i in range(8):
			await process_frame
		var left_image := await _viewport_image_oracle_report(left_vp)
		if not bool(left_image.get("ok", false)):
			return _record_subviewport_oracle_fail("viewport_image_left", left_image)
		var right_image := await _viewport_image_oracle_report(right_vp)
		if not bool(right_image.get("ok", false)):
			return _record_subviewport_oracle_fail("viewport_image_right", right_image)
	return true


func _color_payload(color: Color) -> Array:
	return [color.r, color.g, color.b, color.a]


func _viewport_image_oracle_report(viewport: SubViewport) -> Dictionary:
	await RenderingServer.frame_post_draw
	var report := {
		"viewport_size": _vector2i_payload(viewport.size),
		"side": viewport.get_parent().name if viewport.get_parent() != null else "",
	}
	var tex := viewport.get_texture()
	if tex == null:
		report["ok"] = false
		report["reason"] = "texture_null"
		return report
	var image := tex.get_image()
	if image == null:
		report["ok"] = false
		report["reason"] = "image_null"
		return report
	var width := image.get_width()
	var height := image.get_height()
	report["image_size"] = [width, height]
	if width < 8 or height < 8:
		report["ok"] = false
		report["reason"] = "image_too_small"
		return report
	const CORNER_COHERENCE_SQ := 0.01
	const DIFF_SQ := 0.0064
	const MIN_MEANINGFUL := 12
	const MAX_MEANINGFUL_FRAC := 0.82
	const MIN_CONTENT_SPAN_PX := 8
	var corner_pixels: Array[Color] = [
		image.get_pixel(0, 0),
		image.get_pixel(width - 1, 0),
		image.get_pixel(0, height - 1),
		image.get_pixel(width - 1, height - 1),
	]
	report["corner_pixels"] = [
		_color_payload(corner_pixels[0]),
		_color_payload(corner_pixels[1]),
		_color_payload(corner_pixels[2]),
		_color_payload(corner_pixels[3]),
	]
	var bg_r := 0.0
	var bg_g := 0.0
	var bg_b := 0.0
	for px in corner_pixels:
		if px.a <= 0.05:
			report["ok"] = false
			report["reason"] = "corner_alpha_low"
			report["background_rgb"] = [bg_r, bg_g, bg_b]
			return report
		bg_r += px.r
		bg_g += px.g
		bg_b += px.b
	var inv_corners := 1.0 / float(corner_pixels.size())
	bg_r *= inv_corners
	bg_g *= inv_corners
	bg_b *= inv_corners
	report["background_rgb"] = [bg_r, bg_g, bg_b]
	var max_corner_sq := 0.0
	for px in corner_pixels:
		var cr := px.r - bg_r
		var cg := px.g - bg_g
		var cb := px.b - bg_b
		max_corner_sq = maxf(max_corner_sq, cr * cr + cg * cg + cb * cb)
	report["corner_coherence_max_sq"] = max_corner_sq
	if max_corner_sq > CORNER_COHERENCE_SQ:
		report["ok"] = false
		report["reason"] = "corner_incoherent"
		return report
	var meaningful := 0
	var sampled := 0
	var min_x := width
	var max_x := 0
	var min_y := height
	var max_y := 0
	for y in range(0, height, 4):
		for x in range(0, width, 4):
			sampled += 1
			var px := image.get_pixel(x, y)
			if px.a <= 0.05:
				continue
			var dr := px.r - bg_r
			var dg := px.g - bg_g
			var db := px.b - bg_b
			if (dr * dr + dg * dg + db * db) <= DIFF_SQ:
				continue
			meaningful += 1
			min_x = mini(min_x, x)
			max_x = maxi(max_x, x)
			min_y = mini(min_y, y)
			max_y = maxi(max_y, y)
	report["sample_count"] = sampled
	report["meaningful_count"] = meaningful
	report["foreground_fraction"] = float(meaningful) / float(sampled) if sampled > 0 else 0.0
	report["content_span_px"] = maxi(max_x - min_x, max_y - min_y)
	report["content_bounds_px"] = [min_x, min_y, max_x, max_y]
	if meaningful < MIN_MEANINGFUL:
		report["ok"] = false
		report["reason"] = "meaningful_below_min"
		return report
	if sampled > 0 and float(meaningful) / float(sampled) > MAX_MEANINGFUL_FRAC:
		report["ok"] = false
		report["reason"] = "foreground_fraction_above_max"
		return report
	if maxi(max_x - min_x, max_y - min_y) < MIN_CONTENT_SPAN_PX:
		report["ok"] = false
		report["reason"] = "content_span_below_min"
		return report
	report["ok"] = true
	return report


func _validate_rest_pose_resets(scene: Control, left_side: Node3D, right_side: Node3D) -> bool:
	var left_char := _character(left_side)
	var right_char := _character(right_side)
	if left_char == null or right_char == null:
		return false
	var left_skel := _find_skeleton(left_char)
	var right_skel := _find_skeleton(right_char)
	if left_skel == null or right_skel == null:
		return false
	var arm_l := left_skel.find_bone("LeftUpperArm")
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	if scene.call("request_scrub_normalized", 0.75).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	if scene.call("request_set_left_clip", CLIP_B).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	if float(scene.call("compare_snapshot").get("normalized_progress", -1.0)) > EPS:
		return false
	if left_skel.get_bone_pose_rotation(arm_l).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		return false
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	for skel in [left_skel, right_skel]:
		if not _all_bone_rotations_rest(skel):
			return false
	return true


func _all_bone_rotations_rest(skeleton: Skeleton3D) -> bool:
	for bone_name in BONES:
		var idx := skeleton.find_bone(bone_name)
		if skeleton.get_bone_pose_rotation(idx).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
			return false
	return true


func _validate_weighted_deformation_oracle(
	scene: Control, left_side: Node3D, right_side: Node3D
) -> bool:
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	for _i in range(3):
		await process_frame
	if not await _validate_side_deformation(scene, left_side, CLIP_A, true):
		return false
	if not await _validate_side_deformation(scene, right_side, CLIP_C, false):
		return false
	return true


func _validate_side_deformation(
	scene: Control, side: Node3D, clip_id: String, expect_arm: bool
) -> bool:
	var character := _character(side)
	var player := _preview_player(side)
	var skeleton := _find_skeleton(character)
	var mesh := _find_mesh(character)
	if player == null or skeleton == null or mesh == null:
		return false
	scene.call("request_scrub_normalized", 0.0)
	for _i in range(3):
		await process_frame
	var rest := _vertices(mesh.bake_mesh_from_current_skeleton_pose())
	var clip := player.get_animation(clip_id)
	if clip == null:
		return false
	scene.call("request_scrub_normalized", 0.75)
	for _i in range(4):
		await process_frame
	var dur := float(
		scene.call("compare_snapshot").get(
			"left_duration" if side.name == "LeftSide" else "right_duration", 0.0
		)
	)
	var expected_dur := DURATION_A
	if clip_id == CLIP_C:
		expected_dur = DURATION_C
	elif clip_id == CLIP_B:
		expected_dur = DURATION_B
	if absf(dur - expected_dur) > EPS:
		return false
	var sample_time := 0.75 * dur
	var arm_bone := skeleton.find_bone("LeftUpperArm")
	var spine_bone := skeleton.find_bone("Spine")
	if expect_arm:
		var expected_arm := _rotation_at_track_time(clip, "LeftUpperArm", sample_time)
		if skeleton.get_bone_pose_rotation(arm_bone).angle_to(expected_arm) > ANGLE_EPS:
			return false
		if skeleton.get_bone_pose_rotation(spine_bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
			return false
	else:
		var expected_spine := _rotation_at_track_time(clip, "Spine", sample_time)
		if skeleton.get_bone_pose_rotation(spine_bone).angle_to(expected_spine) > ANGLE_EPS:
			return false
		if skeleton.get_bone_pose_rotation(arm_bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
			return false
	var posed := _vertices(mesh.bake_mesh_from_current_skeleton_pose())
	if rest.is_empty() or rest.size() != posed.size():
		return false
	var groups := _weighted_vertex_groups(mesh, skeleton)
	if groups["arm"].is_empty() or groups["spine"].is_empty():
		return false
	var primary_delta := 0.0
	var secondary_delta := 0.0
	var primary_indices: PackedInt32Array = groups["arm"] if expect_arm else groups["spine"]
	var secondary_indices: PackedInt32Array = groups["spine"] if expect_arm else groups["arm"]
	for idx in primary_indices:
		primary_delta = maxf(primary_delta, rest[idx].distance_to(posed[idx]))
		if not _vertex_matches_expected_skin(
			mesh, skeleton, idx, posed[idx], clip, expect_arm, sample_time
		):
			return false
	for idx in secondary_indices:
		secondary_delta = maxf(secondary_delta, rest[idx].distance_to(posed[idx]))
	if primary_delta < DEFORM_MIN or primary_delta > DEFORM_MAX:
		return false
	if expect_arm:
		return secondary_delta <= VERT_EPS
	return secondary_delta >= DEFORM_MIN


func _validate_synchronized_transport(scene: Control) -> bool:
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	if scene.call("request_set_playback_speed", 1.0).get("ok") != true:
		return false
	for _i in range(3):
		await process_frame
	var duration_snap: Dictionary = scene.call("compare_snapshot")
	if absf(float(duration_snap.get("left_duration", -1.0)) - DURATION_A) > EPS:
		return false
	if absf(float(duration_snap.get("right_duration", -1.0)) - DURATION_C) > EPS:
		return false
	if not await _reject_invalid_transport(scene):
		return false
	if scene.call("request_scrub_normalized", 0.25).get("ok") != true:
		return false
	for _i in range(3):
		await process_frame
	var snap: Dictionary = scene.call("compare_snapshot")
	if absf(float(snap.get("normalized_progress", -1.0)) - 0.25) > EPS:
		return false
	var left_d := float(snap.get("left_duration"))
	var right_d := float(snap.get("right_duration"))
	if absf(float(snap.get("left_position")) - 0.25 * left_d) > EPS:
		return false
	if absf(float(snap.get("right_position")) - 0.25 * right_d) > EPS:
		return false
	var play_button: Button = scene.get_node_or_null("%PlayButton") as Button
	var pause_button: Button = scene.get_node_or_null("%PauseButton") as Button
	if play_button == null or pause_button == null:
		return false
	play_button.pressed.emit()
	var start_p := float(scene.call("compare_snapshot").get("normalized_progress"))
	for _i in range(10):
		await process_frame
	var mid_snap: Dictionary = scene.call("compare_snapshot")
	if float(mid_snap.get("normalized_progress", 0.0)) <= start_p + 0.002:
		return false
	pause_button.pressed.emit()
	for _i in range(6):
		await process_frame
	var paused_snap: Dictionary = scene.call("compare_snapshot")
	if paused_snap.get("playing"):
		return false
	if paused_snap.get("normalized_progress") != mid_snap.get("normalized_progress"):
		return false
	if paused_snap.get("left_position") != mid_snap.get("left_position"):
		return false
	if paused_snap.get("right_position") != mid_snap.get("right_position"):
		return false
	for speed in ALLOWED_SPEEDS:
		if scene.call("request_set_playback_speed", speed).get("ok") != true:
			return false
		if float(scene.call("compare_snapshot").get("speed")) != speed:
			return false
	var speed_option: OptionButton = scene.get_node_or_null("%SpeedOption") as OptionButton
	if speed_option != null:
		speed_option.select(1)
		speed_option.item_selected.emit(1)
		await process_frame
		if float(scene.call("compare_snapshot").get("speed")) != 0.5:
			return false
	if scene.call("request_set_playback_speed", 2.0).get("ok") != true:
		return false
	if scene.call("request_scrub_normalized", 0.75).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	snap = scene.call("compare_snapshot")
	if absf(float(snap.get("normalized_progress")) - 0.75) > EPS:
		return false
	if absf(float(snap.get("left_position")) - 0.75 * left_d) > EPS:
		return false
	if absf(float(snap.get("right_position")) - 0.75 * right_d) > EPS:
		return false
	if scene.call("request_restart").get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	var restarted: Dictionary = scene.call("compare_snapshot")
	if float(restarted.get("normalized_progress", -1.0)) > EPS:
		return false
	if float(restarted.get("left_position", -1.0)) > EPS or float(restarted.get("right_position", -1.0)) > EPS:
		return false
	if restarted.get("left_clip_id") != CLIP_A or restarted.get("right_clip_id") != CLIP_C:
		return false
	if absf(float(restarted.get("speed")) - 2.0) > EPS:
		return false
	if scene.call("request_set_left_clip", CLIP_B).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	var left_side: Node3D = scene.get_node("%LeftSide")
	var left_char := _character(left_side)
	var left_skel := _find_skeleton(left_char)
	var arm := left_skel.find_bone("LeftUpperArm")
	if left_skel.get_bone_pose_rotation(arm).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		return false
	if float(scene.call("compare_snapshot").get("normalized_progress", -1.0)) > EPS:
		return false
	if scene.call("request_scrub_normalized", 0.75).get("ok") != true:
		return false
	for _i in range(4):
		await process_frame
	var player := _preview_player(left_side)
	var clip_b := player.get_animation(CLIP_B)
	var spine := left_skel.find_bone("Spine")
	var left_b_dur := float(scene.call("compare_snapshot").get("left_duration", 0.0))
	if absf(left_b_dur - DURATION_B) > EPS:
		return false
	var expected := _rotation_at_track_time(clip_b, "Spine", 0.75 * left_b_dur)
	if left_skel.get_bone_pose_rotation(spine).angle_to(expected) > ANGLE_EPS:
		return false
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	for _i in range(3):
		await process_frame
	return true


func _reject_invalid_transport(scene: Control) -> bool:
	var before: Dictionary = scene.call("compare_snapshot")
	if scene.call("request_set_playback_speed", true).get("ok") != false:
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
	if scene.call("request_scrub_normalized", NAN).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_scrub_normalized", INF).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_scrub_normalized", -1.0).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_scrub_normalized", 1.01).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if scene.call("request_scrub_normalized", "bad").get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	return true


func _validate_selector_and_ui_rejections(scene: Control) -> bool:
	var left_option: OptionButton = scene.get_node_or_null("%LeftClipOption") as OptionButton
	var right_option: OptionButton = scene.get_node_or_null("%RightClipOption") as OptionButton
	if left_option == null or right_option == null:
		return false
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	await process_frame
	var before: Dictionary = scene.call("compare_snapshot")
	var left_idx_before := left_option.selected
	var right_idx_before := right_option.selected
	if scene.call("request_set_left_clip", CLIP_C).get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if left_option.selected != left_idx_before or right_option.selected != right_idx_before:
		return false
	if scene.call("request_set_left_clip", "missing_clip").get("ok") != false:
		return false
	if not _snapshots_equal(scene.call("compare_snapshot"), before):
		return false
	if left_option.selected != left_idx_before:
		return false
	var duplicate_idx := -1
	for i in range(left_option.item_count):
		if left_option.get_item_text(i) == CLIP_C:
			duplicate_idx = i
			break
	if duplicate_idx >= 0:
		left_option.select(duplicate_idx)
		left_option.item_selected.emit(duplicate_idx)
		await process_frame
		if not _snapshots_equal(scene.call("compare_snapshot"), before):
			return false
		if left_option.selected != left_idx_before or right_option.selected != right_idx_before:
			return false
	return true


func _validate_end_hold_no_drift(scene: Control) -> bool:
	if scene.call("request_set_left_clip", CLIP_A).get("ok") != true:
		return false
	if scene.call("request_set_right_clip", CLIP_C).get("ok") != true:
		return false
	if scene.call("request_scrub_normalized", 1.0).get("ok") != true:
		return false
	scene.call("request_pause")
	for _i in range(6):
		await process_frame
	var snap: Dictionary = scene.call("compare_snapshot")
	if bool(snap.get("playing", true)):
		return false
	if absf(float(snap.get("normalized_progress")) - 1.0) > EPS:
		return false
	var hold := snap.duplicate()
	for _i in range(8):
		await process_frame
	if not _snapshots_equal(scene.call("compare_snapshot"), hold):
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
		var av: Variant = a.get(key)
		var bv: Variant = b.get(key)
		if typeof(av) == TYPE_FLOAT or typeof(bv) == TYPE_FLOAT:
			if absf(float(av) - float(bv)) > EPS:
				return false
		elif av != bv:
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


func _load_collider_contract() -> Dictionary:
	var file := FileAccess.open(COLLIDER_JSON_PATH, FileAccess.READ)
	if file == null:
		return {}
	var parsed: Variant = JSON.parse_string(file.get_as_text())
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}


func _capsule_matches_collider_contract(
	capsule: CapsuleShape3D, shape_node: CollisionShape3D, collider: Dictionary
) -> bool:
	if capsule.radius <= EPS or capsule.height <= EPS:
		return false
	var expected_radius := float(collider.get("radius_m", 0.0))
	var expected_height := float(collider.get("height_m", 0.0))
	if absf(capsule.radius - expected_radius) > EPS:
		return false
	if absf(capsule.height - expected_height) > EPS:
		return false
	var center_arr: Array = collider.get("center_m", [])
	if center_arr.size() != 3:
		return false
	var pos := shape_node.position
	if absf(pos.x - float(center_arr[0])) > EPS:
		return false
	if absf(pos.y - float(center_arr[1])) > EPS:
		return false
	if absf(pos.z - float(center_arr[2])) > EPS:
		return false
	return true


func _mesh_skin_influence_stride(mesh: MeshInstance3D, arrays: Array) -> int:
	if mesh.mesh == null:
		return 0
	var verts: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
	var vert_count := verts.size()
	if vert_count == 0:
		return 0
	var bone_indices: PackedInt32Array = arrays[Mesh.ARRAY_BONES]
	var weights: PackedFloat32Array = arrays[Mesh.ARRAY_WEIGHTS]
	if bone_indices.is_empty() or weights.is_empty():
		return 0
	if bone_indices.size() != weights.size():
		return 0
	if weights.size() % vert_count != 0:
		return 0
	var stride := weights.size() / vert_count
	if stride < 1 or stride > 8:
		return 0
	return stride


func _skeleton_bone_from_skin_bind(
	mesh: MeshInstance3D, skeleton: Skeleton3D, bind_index: int
) -> int:
	if bind_index < 0:
		return -1
	var skin := mesh.skin
	if skin == null:
		return -1
	if bind_index >= skin.get_bind_count():
		return -1
	var bind_name := String(skin.get_bind_name(bind_index))
	if not bind_name.is_empty():
		var named_bone := skeleton.find_bone(bind_name)
		if named_bone < 0 or named_bone >= skeleton.get_bone_count():
			return -1
		return named_bone
	var skel_bone := skin.get_bind_bone(bind_index)
	if skel_bone < 0 or skel_bone >= skeleton.get_bone_count():
		return -1
	return skel_bone


func _mesh_has_twelve_bone_skin_binding(mesh: MeshInstance3D, skeleton: Skeleton3D) -> bool:
	if mesh.mesh == null or mesh.mesh.get_surface_count() != 1:
		return false
	var arrays: Array = mesh.mesh.surface_get_arrays(0)
	var stride := _mesh_skin_influence_stride(mesh, arrays)
	if stride <= 0:
		return false
	var bone_indices: PackedInt32Array = arrays[Mesh.ARRAY_BONES]
	var weights: PackedFloat32Array = arrays[Mesh.ARRAY_WEIGHTS]
	var arm_idx := skeleton.find_bone("LeftUpperArm")
	var spine_idx := skeleton.find_bone("Spine")
	var has_arm := false
	var has_spine := false
	var vert_count := weights.size() / stride
	for vi in range(vert_count):
		for slot in range(stride):
			var weight := weights[vi * stride + slot]
			if weight <= EPS:
				continue
			var bind_index := bone_indices[vi * stride + slot]
			var bone_idx := _skeleton_bone_from_skin_bind(mesh, skeleton, bind_index)
			if bone_idx < 0:
				return false
			if bone_idx == arm_idx:
				has_arm = true
			if bone_idx == spine_idx:
				has_spine = true
	return has_arm and has_spine


func _weighted_vertex_groups(mesh: MeshInstance3D, skeleton: Skeleton3D) -> Dictionary:
	var arrays: Array = mesh.mesh.surface_get_arrays(0)
	var stride := _mesh_skin_influence_stride(mesh, arrays)
	if stride <= 0:
		return {"arm": PackedInt32Array(), "spine": PackedInt32Array()}
	var bone_indices: PackedInt32Array = arrays[Mesh.ARRAY_BONES]
	var weights: PackedFloat32Array = arrays[Mesh.ARRAY_WEIGHTS]
	var arm_idx := skeleton.find_bone("LeftUpperArm")
	var spine_idx := skeleton.find_bone("Spine")
	var arm := PackedInt32Array()
	var spine := PackedInt32Array()
	var vert_count := weights.size() / stride
	for vi in range(vert_count):
		var dominant_bind := -1
		var dominant_w := 0.0
		for slot in range(stride):
			var weight := weights[vi * stride + slot]
			if weight > dominant_w:
				dominant_w = weight
				dominant_bind = bone_indices[vi * stride + slot]
		var dominant_bone := _skeleton_bone_from_skin_bind(mesh, skeleton, dominant_bind)
		if dominant_bone == arm_idx:
			arm.append(vi)
		elif dominant_bone == spine_idx:
			spine.append(vi)
	return {"arm": arm, "spine": spine}


func _vertex_matches_expected_skin(
	mesh: MeshInstance3D,
	skeleton: Skeleton3D,
	vertex_index: int,
	posed_local: Vector3,
	clip: Animation,
	expect_arm: bool,
	sample_time: float,
) -> bool:
	var saved: Array[Quaternion] = []
	for bone_name in BONES:
		var idx := skeleton.find_bone(bone_name)
		saved.append(skeleton.get_bone_pose_rotation(idx))
	for bone_name in BONES:
		skeleton.reset_bone_pose(skeleton.find_bone(bone_name))
	if expect_arm:
		var arm_idx := skeleton.find_bone("LeftUpperArm")
		var rot := _rotation_at_track_time(clip, "LeftUpperArm", sample_time)
		skeleton.set_bone_pose_rotation(arm_idx, rot)
	else:
		var spine_idx := skeleton.find_bone("Spine")
		var rot := _rotation_at_track_time(clip, "Spine", sample_time)
		skeleton.set_bone_pose_rotation(spine_idx, rot)
	for bone_idx in range(skeleton.get_bone_count()):
		skeleton.force_update_bone_child_transform(bone_idx)
	var expected_mesh := mesh.bake_mesh_from_current_skeleton_pose()
	var expected_verts: PackedVector3Array = expected_mesh.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]
	var expected_local: Vector3 = expected_verts[vertex_index]
	for i in range(BONES.size()):
		var idx := skeleton.find_bone(BONES[i])
		skeleton.set_bone_pose_rotation(idx, saved[i])
	for bone_idx in range(skeleton.get_bone_count()):
		skeleton.force_update_bone_child_transform(bone_idx)
	return posed_local.distance_to(expected_local) <= SKIN_MATCH_EPS


func _camera_framing_oracle(side: Node3D) -> bool:
	return bool(_camera_framing_report(side).get("ok", false))


func _camera_framing_report(side: Node3D) -> Dictionary:
	var report := {"side": side.name}
	var camera := side.get_node_or_null("ReviewCamera") as Camera3D
	var mesh := _find_mesh(_character(side))
	if camera == null or mesh == null:
		report["ok"] = false
		report["reason"] = "camera_or_mesh_missing"
		report["camera_null"] = camera == null
		report["mesh_null"] = mesh == null
		return report
	var world_points := _baked_mesh_world_vertices(mesh)
	if world_points.is_empty():
		report["ok"] = false
		report["reason"] = "world_vertices_empty"
		return report
	var safe := _viewport_safe_rect(side)
	var visible := side.get_viewport().get_visible_rect()
	report["viewport_size"] = _vector2i_payload(side.get_viewport().size)
	report["visible_rect"] = _rect2_payload(visible)
	report["safe_rect"] = _rect2_payload(safe)
	report["vertex_count"] = world_points.size()
	var behind_count := 0
	var min_screen := Vector2(INF, INF)
	var max_screen := Vector2(-INF, -INF)
	var first_violation := ""
	for point in world_points:
		if camera.is_position_behind(point):
			behind_count += 1
			if first_violation.is_empty():
				first_violation = "vertex_behind_camera"
			continue
		var screen := camera.unproject_position(point)
		min_screen.x = minf(min_screen.x, screen.x)
		min_screen.y = minf(min_screen.y, screen.y)
		max_screen.x = maxf(max_screen.x, screen.x)
		max_screen.y = maxf(max_screen.y, screen.y)
		if first_violation.is_empty():
			if screen.x < safe.position.x or screen.y < safe.position.y:
				first_violation = "vertex_before_safe_min"
			elif screen.x > safe.position.x + safe.size.x or screen.y > safe.position.y + safe.size.y:
				first_violation = "vertex_after_safe_max"
	report["behind_count"] = behind_count
	report["projected_bounds"] = [
		min_screen.x if min_screen.x != INF else 0.0,
		min_screen.y if min_screen.y != INF else 0.0,
		max_screen.x if max_screen.x != -INF else 0.0,
		max_screen.y if max_screen.y != -INF else 0.0,
	]
	if first_violation.is_empty() and behind_count == 0:
		report["ok"] = true
		return report
	report["ok"] = false
	report["reason"] = first_violation if not first_violation.is_empty() else "behind_camera"
	return report


func _viewport_safe_rect(side: Node3D) -> Rect2:
	var visible := side.get_viewport().get_visible_rect()
	var margin := VIEWPORT_EDGE_MARGIN_PX
	return Rect2(
		visible.position.x + margin,
		visible.position.y + margin,
		maxf(8.0, visible.size.x - margin * 2.0),
		maxf(8.0, visible.size.y - margin * 2.0),
	)


func _world_points_fit_screen(
	camera: Camera3D, world_points: PackedVector3Array, safe: Rect2
) -> bool:
	for point in world_points:
		if camera.is_position_behind(point):
			return false
		var screen := camera.unproject_position(point)
		if screen.x < safe.position.x or screen.y < safe.position.y:
			return false
		if screen.x > safe.position.x + safe.size.x or screen.y > safe.position.y + safe.size.y:
			return false
	return true


func _baked_mesh_world_vertices(mesh: MeshInstance3D) -> PackedVector3Array:
	var baked := mesh.bake_mesh_from_current_skeleton_pose()
	if baked == null or baked.get_surface_count() != 1:
		return PackedVector3Array()
	var local: PackedVector3Array = baked.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]
	var world := PackedVector3Array()
	world.resize(local.size())
	var xf := mesh.global_transform
	for i in range(local.size()):
		world[i] = xf * local[i]
	return world


func _vertices(mesh: Mesh) -> PackedVector3Array:
	if mesh == null or mesh.get_surface_count() != 1:
		return PackedVector3Array()
	return mesh.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]


func _find_skeleton(root: Node) -> Skeleton3D:
	if root is Skeleton3D:
		return root
	for child in root.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null


func _find_mesh(root: Node) -> MeshInstance3D:
	if root is MeshInstance3D and root.name == "SM_HumanoidSkin":
		return root
	for child in root.get_children():
		var found := _find_mesh(child)
		if found != null:
			return found
	return null


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
