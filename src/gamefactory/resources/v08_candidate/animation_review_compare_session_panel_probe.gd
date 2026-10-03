extends SceneTree
class_name CompareSessionPanelProbeEntry

## Fast headless regression probe for compare-session panel integrity (probe-player compare harness).

const CLIP_A := "probe_clip_a"
const CLIP_B := "probe_clip_b"
const CLIP_C := "probe_clip_c"
const BRIDGE_SCHEMA_VERSION := "animation-review-session-bridge-0.8.0"

const CONTEXT_PREFIX := "--compare-context-file="
const READY_FRAMES := 300


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var context_path := _compare_context_path()
	if context_path.is_empty():
		_fail("compare context path missing")
		return
	var session := _build_session_harness()
	root.add_child(session)
	var compare := session.get_node("PlaybackLayer/ComparePlayback") as Control
	if compare == null:
		_fail("compare harness missing")
		return
	if not await _wait_compare_ready(compare):
		_fail("compare harness not ready")
		return
	if not session.has_method("_wait_compare_ready"):
		_fail("session controller missing compare ready wait")
		return
	var probe_script: GDScript = load("res://animation_review_compare_session_panel_probe.gd")
	if probe_script == null:
		_fail("panel probe script missing")
		return
	var ok: bool = await probe_script.call("run_session_panel_regression_probe_async", session)
	quit(0 if ok else 1)


func _build_session_harness() -> Node3D:
	var session := Node3D.new()
	session.set_script(load("res://animation_review_compare_session_controller.gd"))
	var playback_layer := CanvasLayer.new()
	playback_layer.name = "PlaybackLayer"
	session.add_child(playback_layer)
	var compare := _build_compare_controller()
	compare.name = "ComparePlayback"
	compare.unique_name_in_owner = true
	playback_layer.add_child(compare)
	compare.owner = session
	playback_layer.owner = session
	var session_ui := CanvasLayer.new()
	session_ui.name = "SessionUI"
	session.add_child(session_ui)
	var root_control := Control.new()
	root_control.name = "Root"
	root_control.set_anchors_preset(Control.PRESET_FULL_RECT)
	session_ui.add_child(root_control)
	root_control.owner = session
	var margin := MarginContainer.new()
	margin.name = "SessionMargin"
	margin.unique_name_in_owner = true
	root_control.add_child(margin)
	margin.owner = session
	_add_session_controls(margin, session)
	session_ui.owner = session
	return session


func _add_session_controls(margin: MarginContainer, owner: Node) -> void:
	var banner := Label.new()
	banner.name = "SessionStatusBanner"
	banner.unique_name_in_owner = true
	margin.add_child(banner)
	banner.owner = owner
	var reload := Button.new()
	reload.name = "ReloadSessionButton"
	reload.unique_name_in_owner = true
	margin.add_child(reload)
	reload.owner = owner
	_add_pane_controls(margin, owner, "Left")
	_add_pane_controls(margin, owner, "Right")


func _add_pane_controls(parent: Node, owner: Node, side: String) -> void:
	var prefix := side
	var status := OptionButton.new()
	status.name = "%sClipStatusOption" % prefix
	status.unique_name_in_owner = true
	parent.add_child(status)
	status.owner = owner
	var keep := Button.new()
	keep.name = "%sKeepButton" % prefix
	keep.unique_name_in_owner = true
	parent.add_child(keep)
	keep.owner = owner
	var revise := Button.new()
	revise.name = "%sReviseButton" % prefix
	revise.unique_name_in_owner = true
	parent.add_child(revise)
	revise.owner = owner
	var note := LineEdit.new()
	note.name = "%sClipNoteField" % prefix
	note.unique_name_in_owner = true
	parent.add_child(note)
	note.owner = owner
	var save := Button.new()
	save.name = "%sSaveNoteButton" % prefix
	save.unique_name_in_owner = true
	parent.add_child(save)
	save.owner = owner
	var bookmark := Button.new()
	bookmark.name = "%sAddBookmarkButton" % prefix
	bookmark.unique_name_in_owner = true
	parent.add_child(bookmark)
	bookmark.owner = owner
	var bookmarks := Label.new()
	bookmarks.name = "%sBookmarksLabel" % prefix
	bookmarks.unique_name_in_owner = true
	parent.add_child(bookmarks)
	bookmarks.owner = owner
	var row := HBoxContainer.new()
	row.name = "%sBookmarkRow" % prefix
	parent.add_child(row)
	row.owner = owner
	var prev := Button.new()
	prev.name = "%sPrevBookmarkButton" % prefix
	prev.unique_name_in_owner = true
	row.add_child(prev)
	prev.owner = owner
	var bookmark_option := OptionButton.new()
	bookmark_option.name = "%sBookmarkOption" % prefix
	bookmark_option.unique_name_in_owner = true
	row.add_child(bookmark_option)
	bookmark_option.owner = owner
	var next := Button.new()
	next.name = "%sNextBookmarkButton" % prefix
	next.unique_name_in_owner = true
	row.add_child(next)
	next.owner = owner
	var seek := Button.new()
	seek.name = "%sSeekBookmarkButton" % prefix
	seek.unique_name_in_owner = true
	row.add_child(seek)
	seek.owner = owner


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


func _fail(message: String) -> void:
	push_error(message)
	quit(1)


static func run_session_panel_regression_probe(session: Node) -> bool:
	return false


static func run_session_panel_regression_probe_async(session: Node) -> bool:
	var failures: Array[String] = []
	var controller_script: Script = session.get_script()
	if controller_script == null:
		return false

	session.set(
		"_stored_session",
		controller_script.call("_minimal_valid_session_dict_for_compare")
	)
	session.set("_raw_sha256", "f".repeat(64))
	session.set("_authority_current", true)
	session.set("_reload_required", false)
	session.set("_conflict_active", false)
	session.set("_bridge_busy", false)
	session.set("_draft_notes", {})
	session.set("_left_note_dirty", false)
	session.set("_right_note_dirty", false)
	session.set("_panel_probe_bridge_intercept", false)
	session.set("_panel_probe_captured_action", "")
	session.set("_panel_probe_captured_request", {})

	var exchange_path := OS.get_cache_dir().path_join("gf_compare_session_panel_probe_exchange")
	DirAccess.make_dir_recursive_absolute(exchange_path)
	var python_stub := exchange_path.path_join("python_stub")
	var stub_file := FileAccess.open(python_stub, FileAccess.WRITE)
	if stub_file != null:
		stub_file.store_string("")
		stub_file.close()
	var context_stub := exchange_path.path_join("context_stub.json")
	var context_file := FileAccess.open(context_stub, FileAccess.WRITE)
	if context_file != null:
		context_file.store_string("{}")
		context_file.close()
	session.set("_exchange_dir", exchange_path)
	session.set("_python_executable", python_stub)
	session.set("_context_file", context_stub)
	session.set("_bridge_configured", true)
	session.set("_panel_probe_bridge_intercept", true)

	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_apply_pane_annotations", "left")
	session.call("_apply_pane_annotations", "right")

	var playback: Node = session.get("_playback")
	if playback != null and playback.has_method("request_scrub_normalized"):
		playback.call("request_scrub_normalized", 0.5)
	var snap_before: Dictionary = session.call("_compare_snapshot")
	var expected_bookmark_ts := _bookmark_ts_from_snapshot(snap_before, "right", CLIP_B, controller_script)
	if expected_bookmark_ts < 0.0:
		failures.append("bookmark uses captured snapshot timing")
	elif abs(
		float(
			controller_script.call(
				"_bookmark_seconds_from_snapshot", snap_before, "right", CLIP_B
			)
		) - expected_bookmark_ts
	) > 0.0001:
		failures.append("bookmark uses captured snapshot timing")

	if playback != null and playback.has_method("request_set_right_clip"):
		if playback.call("request_set_right_clip", CLIP_A).get("ok", true):
			failures.append("reject same-as-left right clip")
		if not playback.call("request_set_right_clip", CLIP_C).get("ok", false):
			failures.append("accept right clip c")
	session.call("_sync_pane_clip_targets_from_compare")
	if str(session.call("_pane_clip_id", "right")) != CLIP_C:
		failures.append("sync right clip after compare select")

	var snap_after_c: Dictionary = session.call("_compare_snapshot")
	_type_note_draft(session, "right", "c-pane-note")
	session.call("_on_quick_status_pressed", "right", "keep")
	var status_capture: Dictionary = session.get("_panel_probe_captured_request")
	if status_capture.is_empty():
		failures.append("status bridge capture missing")
	else:
		var op: Variant = status_capture.get("operation", {})
		if typeof(op) != TYPE_DICTIONARY or op.get("clip_id", "") != CLIP_C:
			failures.append("status capture targets right clip c")

	session.call("_on_save_note_pressed", "right")
	var note_capture: Dictionary = session.get("_panel_probe_captured_request")
	if typeof(note_capture.get("operation", {})) == TYPE_DICTIONARY:
		if note_capture.get("operation", {}).get("clip_id", "") != CLIP_C:
			failures.append("note capture targets right clip c")

	var expected_capture_ts := float(
		controller_script.call("_bookmark_seconds_from_snapshot", snap_after_c, "right", CLIP_C)
	)
	session.call("_on_add_bookmark_pressed", "right")
	var bookmark_capture: Dictionary = session.get("_panel_probe_captured_request")
	if typeof(bookmark_capture.get("operation", {})) == TYPE_DICTIONARY:
		var bookmark_op: Dictionary = bookmark_capture.get("operation", {})
		if bookmark_op.get("clip_id", "") != CLIP_C:
			failures.append("bookmark capture targets right clip c")
		elif bookmark_op.has("time") or bookmark_op.has("time_seconds"):
			failures.append("bookmark capture wrong timestamp domain key")
		elif abs(float(bookmark_op.get("timestamp", -1.0)) - expected_capture_ts) > 0.0001:
			failures.append("bookmark capture uses snapshot timing")

	var snap_after_ops: Dictionary = session.call("_compare_snapshot")
	if not _playback_fields_equal(snap_after_c, snap_after_ops):
		failures.append("playback snapshot changed across bridge capture")

	_type_note_draft(session, "left", "left-draft")
	_type_note_draft(session, "right", "right-draft")
	var snap_mid: Dictionary = session.call("_compare_snapshot")

	var session_saved: Dictionary = _session_with_clip_note(
		controller_script,
		session.get("_stored_session"),
		CLIP_A,
		"left-draft",
		2,
		"e".repeat(64)
	)
	var update_ack := _bridge_ok_envelope(true, true, session_saved, "e".repeat(64))
	session.call(
		"_apply_bridge_response", "update", update_ack, CLIP_A, "left-draft", "SetNote"
	)
	if str(session.get("_right_note_field").text) != "right-draft":
		failures.append("cross-pane draft preserved after left ACK")
	if not _playback_fields_equal(snap_mid, session.call("_compare_snapshot")):
		failures.append("playback snapshot changed after left ACK")

	_type_note_draft(session, "right", "newer-right")
	var invalid_read_ack := _bridge_ok_envelope(true, true, session_saved, "e".repeat(64))
	session.call("_apply_bridge_response", "read", invalid_read_ack, "", "", "")
	if session.get("_authority_current"):
		failures.append("read committed=true rejected")

	session.set("_authority_current", true)
	session.set("_reload_required", false)
	session.set("_conflict_active", false)
	var stale_read_session: Dictionary = _session_with_clip_note(
		controller_script,
		session_saved.get("session"),
		CLIP_B,
		"stale-server-note",
		3,
		"a".repeat(64)
	)
	var read_ack := _bridge_ok_envelope(false, true, stale_read_session, "a".repeat(64))
	session.call("_apply_bridge_response", "read", read_ack, "", "", "")
	if str(session.get("_right_note_field").text) != "newer-right":
		failures.append("newer draft preserved after reload")
	if not _playback_fields_equal(snap_mid, session.call("_compare_snapshot")):
		failures.append("playback snapshot changed after reload")

	var older_ack_session: Dictionary = _session_with_clip_note(
		controller_script,
		session.get("_stored_session"),
		CLIP_B,
		"older-ack-note",
		4,
		"9".repeat(64)
	)
	var older_ack := _bridge_ok_envelope(true, true, older_ack_session, "9".repeat(64))
	session.call(
		"_apply_bridge_response", "update", older_ack, CLIP_B, "older-ack-note", "SetNote"
	)
	if str(session.get("_right_note_field").text) != "newer-right":
		failures.append("newer submitted clip text survives older ACK")

	if playback != null and playback.has_method("request_set_left_clip"):
		playback.call("request_set_left_clip", CLIP_B)
	session.call("_sync_pane_clip_targets_from_compare")
	var left_dirty_before: bool = session.get("_left_note_dirty")
	session.call(
		"_handle_bridge_error_response",
		"update",
		false,
		false,
		{"code": "session_conflict", "message": "conflict"},
		CLIP_A,
		"SetNote",
		"orphan-note",
	)
	if (
		str(session.get("_tracked_left_clip_id")) != CLIP_A
		and not left_dirty_before
		and session.get("_left_note_dirty")
	):
		failures.append("conflict marked wrong pane dirty")

	if playback != null:
		if playback.has_method("request_set_left_clip"):
			playback.call("request_set_left_clip", CLIP_A)
		if playback.has_method("request_set_right_clip"):
			playback.call("request_set_right_clip", CLIP_B)
	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_on_quick_status_pressed", "right", "keep")
	session.call(
		"_handle_bridge_error_response",
		"update",
		false,
		false,
		{"code": "session_conflict", "message": "conflict"},
		CLIP_B,
		"SetStatus",
		"",
	)
	var right_status: OptionButton = session.get("_right_status_option")
	if right_status.get_item_text(right_status.selected).to_lower() != "unreviewed":
		failures.append("rejected status restored")

	session.set("_authority_current", true)
	session.set("_reload_required", false)
	session.set("_conflict_active", false)
	session.set("_draft_notes", {})
	session.set("_left_note_dirty", false)
	session.set("_right_note_dirty", false)
	var undo_base: Dictionary = controller_script.call("_minimal_valid_session_dict_for_compare")
	var old_note_session: Dictionary = _session_with_clip_note(
		controller_script, undo_base, CLIP_A, "old", 10, ""
	)
	session.set("_stored_session", old_note_session.get("session"))
	session.set("_raw_sha256", "f".repeat(64))
	if playback != null:
		if playback.has_method("request_set_left_clip"):
			playback.call("request_set_left_clip", CLIP_A)
		if playback.has_method("request_set_right_clip"):
			playback.call("request_set_right_clip", CLIP_B)
	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_apply_pane_annotations", "left")
	session.call("_apply_pane_annotations", "right")
	_type_note_draft(session, "left", "draft")
	_type_note_draft(session, "left", "old")
	if (session.get("_draft_notes") as Dictionary).has(CLIP_A):
		failures.append("stale draft map after undo to authority note")
	var external_new_session: Dictionary = _session_with_clip_note(
		controller_script, old_note_session.get("session"), CLIP_A, "new", 11, ""
	)
	var undo_reload_read := _bridge_ok_envelope(false, true, external_new_session, "1".repeat(64))
	session.call("_apply_bridge_response", "read", undo_reload_read, "", "", "")
	if str(session.get("_left_note_field").text) != "new":
		failures.append("read after undo shows authority note not stale draft")
	if session.get("_left_note_dirty"):
		failures.append("read after undo left note still dirty")

	var race_session: Dictionary = controller_script.call("_minimal_valid_session_dict_for_probe_clips", 0)
	var race_records: Array = race_session.get("clip_records", [])
	for i in range(race_records.size()):
		var race_entry: Variant = race_records[i]
		if typeof(race_entry) != TYPE_DICTIONARY:
			continue
		var updated: Dictionary = race_entry.duplicate(true)
		if updated.get("clip_id", "") == CLIP_B:
			updated["bookmarks"] = [1.0, 1.25]
		elif updated.get("clip_id", "") == CLIP_C:
			updated["bookmarks"] = [1.0, 1.5]
		race_records[i] = updated
	race_session["clip_records"] = race_records
	session.set("_stored_session", race_session)
	session.set("_authority_current", true)
	session.set("_reload_required", false)
	session.set("_conflict_active", false)
	if playback != null:
		if playback.has_method("request_set_left_clip"):
			playback.call("request_set_left_clip", CLIP_A)
		if playback.has_method("request_set_right_clip"):
			playback.call("request_set_right_clip", CLIP_B)
	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_apply_pane_annotations", "left")
	session.call("_apply_pane_annotations", "right")
	if playback != null and playback.has_method("request_set_right_clip"):
		playback.call("request_set_right_clip", CLIP_C)
	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_apply_pane_annotations", "right")
	if not await session.call("_wait_compare_ready"):
		failures.append("nav race compare not ready on clip c")
	var race_sha := str(session.get("_raw_sha256"))
	var race_rev := int(session.call("_stored_revision"))
	var right_bookmark_option: OptionButton = session.get("_right_bookmark_option")
	var stale_c_idx := 1
	if right_bookmark_option.item_count <= stale_c_idx:
		failures.append("nav race missing second c bookmark")
		stale_c_idx = 0
	session.set("_ui_syncing", true)
	right_bookmark_option.select(stale_c_idx)
	session.set("_ui_syncing", false)
	var left_note_race := str(session.get("_left_note_field").text)
	var right_note_race := str(session.get("_right_note_field").text)
	if playback != null and playback.has_method("request_set_right_clip"):
		playback.call("request_set_right_clip", CLIP_B)
	var snap_after_switch: Dictionary = session.call("_compare_snapshot")
	var stale_seek: Dictionary = session.call("request_seek_stored_bookmark", "right", stale_c_idx)
	if stale_seek.get("ok", true):
		failures.append("nav race seek accepted stale c bookmark on b")
	if stale_seek.get("error_code", "") != "clip_target_changed":
		failures.append("nav race seek error code")
	if not _playback_fields_equal(snap_after_switch, session.call("_compare_snapshot")):
		failures.append("nav race seek changed playback")
	if str(session.get("_left_note_field").text) != left_note_race:
		failures.append("nav race seek changed left note")
	if str(session.get("_right_note_field").text) != right_note_race:
		failures.append("nav race seek changed right note")
	if str(session.get("_raw_sha256")) != race_sha:
		failures.append("nav race seek changed session sha")
	if int(session.call("_stored_revision")) != race_rev:
		failures.append("nav race seek changed session revision")
	if right_bookmark_option.selected != stale_c_idx:
		failures.append("nav race seek changed bookmark selector")
	var stale_step: Dictionary = session.call("request_step_stored_bookmark", "right", -1)
	if stale_step.get("ok", true) and not stale_step.get("noop", false):
		failures.append("nav race step accepted stale c bookmark on b")
	if stale_step.get("error_code", "") != "clip_target_changed":
		failures.append("nav race step error code")
	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_apply_pane_annotations", "right")
	if str(session.call("_pane_clip_id", "right")) != CLIP_B:
		failures.append("nav race sync right clip id")
	var idx_b := 0
	for j in range(right_bookmark_option.item_count):
		var meta_b: Variant = right_bookmark_option.get_item_metadata(j)
		if typeof(meta_b) == TYPE_FLOAT or typeof(meta_b) == TYPE_INT:
			if abs(float(meta_b) - 1.0) <= 0.0001:
				idx_b = j
				break
	if not session.call("request_seek_stored_bookmark", "right", idx_b).get("ok", false):
		failures.append("nav race seek b bookmark after sync")
	var snap_race_b: Dictionary = session.call("_compare_snapshot")
	if abs(float(snap_race_b.get("normalized_progress", -1.0)) - 0.5) > 0.0001:
		failures.append("nav race b bookmark normalized")

	var left_duration := 1.5
	var right_duration := 2.0
	var nav_session: Dictionary = controller_script.call(
		"_minimal_valid_session_dict_with_clips",
		0,
		CLIP_A,
		CLIP_B,
		left_duration,
		right_duration,
	)
	var nav_records: Array = nav_session.get("clip_records", [])
	nav_records[0] = nav_records[0].duplicate(true)
	nav_records[0]["bookmarks"] = [0.75, 1.125]
	nav_records[1] = nav_records[1].duplicate(true)
	nav_records[1]["bookmarks"] = [1.0]
	nav_session["clip_records"] = nav_records
	session.set("_stored_session", nav_session)
	session.set("_authority_current", true)
	session.set("_reload_required", false)
	session.set("_conflict_active", false)
	if playback != null:
		if playback.has_method("request_set_left_clip"):
			playback.call("request_set_left_clip", CLIP_A)
		if playback.has_method("request_set_right_clip"):
			playback.call("request_set_right_clip", CLIP_B)
	session.call("_sync_pane_clip_targets_from_compare")
	session.call("_apply_pane_annotations", "left")
	session.call("_apply_pane_annotations", "right")
	var nav_sha := str(session.get("_raw_sha256"))
	if playback != null and playback.has_method("request_scrub_normalized"):
		playback.call("request_scrub_normalized", 0.5)
	if not await session.call("_wait_compare_ready"):
		failures.append("nav compare not ready before first bookmark seek")
	session.call("request_seek_stored_bookmark", "left", 0)
	var snap_left_075: Dictionary = session.call("_compare_snapshot")
	var left_norm_075 := 0.75 / left_duration
	if abs(float(snap_left_075.get("normalized_progress", -1.0)) - left_norm_075) > 0.0001:
		failures.append("nav left bookmark normalized")
	var left_dur_snap := float(snap_left_075.get("left_duration", -1.0))
	if abs(float(snap_left_075.get("left_position", -1.0)) - left_norm_075 * left_dur_snap) > 0.0001:
		failures.append("nav left bookmark position")
	session.call("request_seek_stored_bookmark", "left", 1)
	var snap_left_1125: Dictionary = session.call("_compare_snapshot")
	var left_norm_1125 := 1.125 / left_duration
	if abs(float(snap_left_1125.get("normalized_progress", -1.0)) - left_norm_1125) > 0.0001:
		failures.append("nav left added bookmark normalized")
	if abs(left_norm_1125 - left_norm_075) <= 0.0001:
		failures.append("nav left bookmark norms distinct")
	if str(session.get("_raw_sha256")) != nav_sha:
		failures.append("nav changed session sha")
	session.call("request_seek_stored_bookmark", "right", 0)
	var snap_right_10: Dictionary = session.call("_compare_snapshot")
	var right_norm_10 := 1.0 / right_duration
	if abs(float(snap_right_10.get("normalized_progress", -1.0)) - right_norm_10) > 0.0001:
		failures.append("nav right bookmark normalized")
	var right_dur_snap := float(snap_right_10.get("right_duration", -1.0))
	if abs(float(snap_right_10.get("right_position", -1.0)) - right_norm_10 * right_dur_snap) > 0.0001:
		failures.append("nav right position at right bookmark")
	if abs(float(snap_right_10.get("left_position", -1.0)) - right_norm_10 * left_dur_snap) > 0.0001:
		failures.append("nav left position at right bookmark")
	var snap_before_invalid: Dictionary = session.call("_compare_snapshot")
	if session.call("request_seek_stored_bookmark", "middle", 0).get("ok", true):
		failures.append("nav invalid pane rejected")
	if not _playback_fields_equal(snap_before_invalid, session.call("_compare_snapshot")):
		failures.append("nav invalid pane left playback unchanged")
	if session.call("request_seek_stored_bookmark", "left", 0.0).get("ok", true):
		failures.append("nav float index rejected")
	if session.call("request_seek_stored_bookmark", "left", true).get("ok", true):
		failures.append("nav bool index rejected")
	if not session.call("request_seek_stored_bookmark", "left", 0).get("ok", false):
		failures.append("nav seek first before prev boundary")
	var left_bookmark_option: OptionButton = session.get("_left_bookmark_option")
	if left_bookmark_option.selected != 0:
		failures.append("nav at first bookmark index before prev boundary")
	var snap_before_step: Dictionary = session.call("_compare_snapshot")
	if not snap_before_step.get("ready", false):
		failures.append("nav compare not ready before prev boundary")
	if abs(float(snap_before_step.get("normalized_progress", -1.0)) - left_norm_075) > 0.0001:
		failures.append("nav playback at first bookmark before prev boundary")
	if not session.call("request_step_stored_bookmark", "left", -1).get("noop", false):
		failures.append("nav prev boundary")
	if not session.call("request_seek_stored_bookmark", "left", 1).get("ok", false):
		failures.append("nav seek last before next boundary")
	if left_bookmark_option.selected != 1:
		failures.append("nav at last bookmark index before next boundary")
	var snap_at_last: Dictionary = session.call("_compare_snapshot")
	if not snap_at_last.get("ready", false):
		failures.append("nav compare not ready before next boundary")
	if abs(float(snap_at_last.get("normalized_progress", -1.0)) - left_norm_1125) > 0.0001:
		failures.append("nav playback at last bookmark before next boundary")
	if not session.call("request_step_stored_bookmark", "left", 1).get("noop", false):
		failures.append("nav next boundary")
	if session.call("request_seek_stored_bookmark", "left", 99).get("ok", true):
		failures.append("nav invalid index rejected")
	if not session.call("request_seek_stored_bookmark", "left", 1).get("ok", false):
		failures.append("nav reselect same bookmark")
	if session.call("request_step_stored_bookmark", "left", 2).get("ok", true):
		failures.append("nav invalid step delta rejected")
	session.set("_authority_current", false)
	session.set("_reload_required", true)
	session.set("_conflict_active", true)
	if not session.call("request_seek_stored_bookmark", "left", 0).get("ok", false):
		failures.append("nav allowed during conflict")
	if str(session.get("_raw_sha256")) != nav_sha:
		failures.append("nav conflict changed cached sha")
	var empty_session: Dictionary = controller_script.call(
		"_minimal_valid_session_dict_with_clips",
		0,
		CLIP_A,
		CLIP_B,
		left_duration,
		right_duration,
	)
	session.set("_stored_session", empty_session)
	session.set("_authority_current", true)
	session.set("_reload_required", false)
	session.set("_conflict_active", false)
	session.call("_apply_pane_annotations", "left")
	if playback != null and playback.has_method("request_scrub_normalized"):
		playback.call("request_scrub_normalized", 0.25)
	if not await session.call("_wait_compare_ready"):
		failures.append("nav compare not ready before empty bookmarks")
	var snap_empty_nav: Dictionary = session.call("_compare_snapshot")
	if not snap_empty_nav.get("ready", false):
		failures.append("nav compare not ready before empty bookmarks")
	if not session.call("request_step_stored_bookmark", "left", 1).get("noop", false):
		failures.append("nav empty bookmarks noop")

	if failures.is_empty():
		print("PASS: animation_review_compare_session_panel_regression")
		return true
	for item in failures:
		print("FAIL: %s" % item)
	return false


static func _type_note_draft(session: Node, pane: String, text: String) -> void:
	var field: LineEdit = (
		session.get("_left_note_field") if pane == "left" else session.get("_right_note_field")
	)
	field.text = text
	session.call("_on_note_text_changed", pane)


static func _bookmark_ts_from_snapshot(
	snap: Dictionary, pane: String, clip_id: String, controller_script: Script
) -> float:
	if not snap.get("ready", false):
		return -1.0
	return float(controller_script.call("_bookmark_seconds_from_snapshot", snap, pane, clip_id))


static func _playback_fields_equal(a: Dictionary, b: Dictionary) -> bool:
	var keys := [
		"normalized_progress",
		"playing",
		"speed",
		"left_position",
		"right_position",
		"left_clip_id",
		"right_clip_id",
	]
	for key in keys:
		if a.get(key) != b.get(key):
			return false
	return true


static func _bridge_ok_envelope(
	committed: bool, current: bool, stored: Dictionary, raw_sha256: String
) -> Dictionary:
	return {
		"schema_version": BRIDGE_SCHEMA_VERSION,
		"ok": true,
		"committed": committed,
		"current": current,
		"stored": {"session": stored.get("session"), "raw_sha256": raw_sha256},
		"error": null,
	}


static func _session_with_clip_note(
	controller_script: Script,
	base_session: Variant,
	clip_id: String,
	note: String,
	revision: int,
	_raw_sha_unused: String = ""
) -> Dictionary:
	var session_doc: Dictionary = base_session.duplicate(true)
	session_doc["revision"] = revision
	var records: Variant = session_doc.get("clip_records", [])
	if typeof(records) == TYPE_ARRAY:
		for i in range(records.size()):
			var entry: Variant = records[i]
			if typeof(entry) != TYPE_DICTIONARY:
				continue
			if entry.get("clip_id", "") == clip_id:
				var updated: Dictionary = entry.duplicate(true)
				updated["note"] = note
				records[i] = updated
				break
	return {"session": session_doc}
