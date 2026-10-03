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
	if not session.has_method("_run_session_panel_regression_probe"):
		_fail("session controller missing panel regression probe")
		return
	var ok: bool = session.call("_run_session_panel_regression_probe")
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
