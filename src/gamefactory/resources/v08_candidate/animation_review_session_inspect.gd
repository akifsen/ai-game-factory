extends SceneTree

const CLIP_A := "arm_wave_01"
const CLIP_B := "arm_reverse_02"
const NOTE_A := "review A"
const NOTE_B_EXTERNAL := "external B note"
const SEEK_TARGET := 0.75
const EPS := 0.0001
const READY_TIMEOUT_MS := 600_000
const BRIDGE_IDLE_TIMEOUT_MS := 600_000
const EXTERNAL_WAIT_TIMEOUT_MS := 600_000
const HANDSHAKE_NAME := "gf_session_inspect_handshake.json"
const EXTERNAL_DONE_NAME := "gf_session_inspect_external_complete.json"

const ARG_PHASE := "--gf-session-inspect-phase="
const ARG_RESULT := "--gf-session-inspect-result="
const _STALE_SESSION_BANNER_FRAGMENTS := [
	"No review session on disk",
	"No session loaded",
	"Loaded historical session (read-only)",
	"Session changed elsewhere",
	"Review set changed",
]


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var phase := _phase_name()
	var result_path := _result_path()
	if phase.is_empty() or result_path.is_empty():
		_fail("missing phase or result path")
		return
	var packed := load("res://animation_review_session.tscn") as PackedScene
	if packed == null:
		_fail("session scene missing")
		return
	var scene := packed.instantiate() as Node3D
	if scene == null:
		_fail("session root invalid")
		return
	root.add_child(scene)
	if not await _wait_playback_ready(scene):
		_fail("playback not ready")
		return
	if not _validate_skeleton_and_player(scene):
		_fail("skeleton or animation player missing")
		return
	var outcome: Dictionary = {}
	match phase:
		"write":
			outcome = await _phase_write(scene)
		"reopen":
			outcome = await _phase_reopen(scene)
		"conflict":
			outcome = await _phase_conflict(scene)
		"stale":
			outcome = await _phase_stale(scene)
		"db_offline":
			outcome = await _phase_db_offline(scene)
		"recovered":
			outcome = await _phase_recovered(scene)
		_:
			_fail("unknown phase %s" % phase)
			return
	if not outcome.get("ok", false):
		_fail(str(outcome.get("error", "phase failed")))
		return
	if not _write_result(result_path, outcome):
		_fail("result write failed")
		return
	print("PASS: animation_review_session_%s" % phase)
	quit(0)


func _phase_write(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_idle(scene, false):
		return {"ok": false, "error": "bridge idle timeout after bootstrap"}
	var create_btn: Button = scene.get_node_or_null("%CreateSessionButton") as Button
	if create_btn != null and not create_btn.disabled:
		create_btn.pressed.emit()
		if not await _wait_bridge_idle(scene, true):
			return {"ok": false, "error": "create session timeout"}
		if not _banner_coherent_for_live_session(scene):
			return {"ok": false, "error": "banner stale after create"}
	if not await _apply_clip_a_annotations(scene):
		return {"ok": false, "error": "clip A annotations failed"}
	if not await _apply_clip_b_keep(scene):
		return {"ok": false, "error": "clip B keep failed"}
	var snap := _session_snapshot(scene)
	if int(snap.get("stored_revision", -1)) != 4:
		return {"ok": false, "error": "expected revision 4 after session write"}
	if not _banner_coherent_for_live_session(scene):
		return {"ok": false, "error": "banner stale after session write"}
	if not _triage_matches_post_write(snap):
		return {"ok": false, "error": "triage counts mismatch after session write"}
	return {"ok": true, "phase": "write", "snapshot": snap}


func _phase_reopen(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_idle(scene, true):
		return {"ok": false, "error": "bridge idle timeout on reopen"}
	if not await _ui_matches_clip_a(scene):
		return {"ok": false, "error": "clip A ui mismatch on reopen"}
	var seek_btn: Button = scene.get_node_or_null("%SeekBookmarkButton") as Button
	if seek_btn == null or seek_btn.disabled:
		return {"ok": false, "error": "bookmark seek unavailable"}
	seek_btn.pressed.emit()
	for _i in range(8):
		await process_frame
	var playback := _playback(scene)
	var player: AnimationPlayer = playback.call("review_animation_player") as AnimationPlayer
	if player == null or absf(player.current_animation_position - SEEK_TARGET) > EPS:
		return {"ok": false, "error": "bookmark seek position mismatch"}
	var verified_seek_position := player.current_animation_position
	if not await _verify_clip_b_keep(scene):
		return {"ok": false, "error": "clip B keep mismatch on reopen"}
	var snap := _session_snapshot(scene)
	if int(snap.get("stored_revision", -1)) != 4:
		return {"ok": false, "error": "expected revision 4 on reopen"}
	if not _banner_coherent_for_live_session(scene):
		return {"ok": false, "error": "banner stale on reopen"}
	return {
		"ok": true,
		"phase": "reopen",
		"snapshot": snap,
		"playback_position": verified_seek_position,
	}


func _phase_conflict(scene: Node3D) -> Dictionary:
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	reload_btn.pressed.emit()
	if not await _wait_bridge_idle(scene, true):
		return {"ok": false, "error": "reload timeout"}
	var before := _session_snapshot(scene)
	var sha: String = str(before.get("raw_sha256", ""))
	if sha.length() != 64:
		return {"ok": false, "error": "cached sha missing"}
	if int(before.get("stored_revision", -1)) != 4:
		return {"ok": false, "error": "expected revision 4 before conflict"}
	var exchange := _exchange_dir()
	if exchange.is_empty():
		return {"ok": false, "error": "exchange dir missing"}
	var handshake_path := exchange.path_join(HANDSHAKE_NAME)
	var done_path := exchange.path_join(EXTERNAL_DONE_NAME)
	if FileAccess.file_exists(done_path):
		DirAccess.remove_absolute(done_path)
	var handshake := {"raw_sha256": sha, "clip_id": CLIP_B, "note": "external B note"}
	var hf := FileAccess.open(handshake_path, FileAccess.WRITE)
	if hf == null:
		return {"ok": false, "error": "handshake write failed"}
	hf.store_string(JSON.stringify(handshake))
	hf.close()
	var deadline_ms := Time.get_ticks_msec() + EXTERNAL_WAIT_TIMEOUT_MS
	while Time.get_ticks_msec() < deadline_ms:
		if FileAccess.file_exists(done_path):
			break
		await process_frame
	if not FileAccess.file_exists(done_path):
		return {"ok": false, "error": "external writer timeout"}
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	var save_btn: Button = scene.get_node_or_null("%SaveNoteButton") as Button
	if note_field == null or save_btn == null:
		return {"ok": false, "error": "note controls missing"}
	note_field.text = "stale ui save attempt"
	note_field.text_changed.emit(note_field.text)
	if save_btn.disabled:
		return {"ok": false, "error": "save note still disabled after text change"}
	save_btn.pressed.emit()
	if not await _wait_bridge_idle(scene, true):
		return {"ok": false, "error": "stale save bridge timeout"}
	var after := _session_snapshot(scene)
	if not after.get("conflict_active", false):
		return {"ok": false, "error": "conflict not active"}
	if not after.get("reload_required", false):
		return {"ok": false, "error": "reload not required"}
	if after.get("mutations_enabled", true):
		return {"ok": false, "error": "mutations still enabled"}
	if str(after.get("raw_sha256", "")) != sha:
		return {"ok": false, "error": "ui cached sha should remain pre-external"}
	if not _conflict_banner_honest(after):
		return {"ok": false, "error": "conflict banner claims saved after rejected write"}
	return {"ok": true, "phase": "conflict", "snapshot": after, "cached_sha": sha}


func _phase_stale(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_idle(scene, true):
		return {"ok": false, "error": "stale bootstrap timeout"}
	var snap := _session_snapshot(scene)
	if snap.get("authority_current", true):
		return {"ok": false, "error": "expected historical authority"}
	if snap.get("mutations_enabled", true):
		return {"ok": false, "error": "mutations enabled when stale"}
	if not _write_controls_disabled(scene):
		return {"ok": false, "error": "write controls enabled when stale"}
	if not await _ui_matches_clip_a(scene):
		return {"ok": false, "error": "clip A ui mismatch when stale"}
	var save_result: Dictionary = scene.call("request_save_note")
	if save_result.get("ok", false):
		return {"ok": false, "error": "save should fail when stale"}
	if str(save_result.get("error_code", "")) != "mutations_disabled":
		return {"ok": false, "error": "unexpected save error code"}
	return {"ok": true, "phase": "stale", "snapshot": snap, "session_sha": snap.get("raw_sha256", "")}


func _phase_db_offline(scene: Node3D) -> Dictionary:
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	reload_btn.pressed.emit()
	if not await _wait_bridge_idle(scene, true):
		return {"ok": false, "error": "db offline reload timeout"}
	var snap := _session_snapshot(scene)
	if snap.get("authority_current", true):
		return {"ok": false, "error": "expected readonly when db offline"}
	if snap.get("mutations_enabled", true):
		return {"ok": false, "error": "mutations enabled when db offline"}
	if int(snap.get("stored_revision", -1)) != 5:
		return {"ok": false, "error": "expected revision 5 historical session when db offline"}
	var sha: String = str(snap.get("raw_sha256", ""))
	if sha.length() != 64:
		return {"ok": false, "error": "historical sha missing when db offline"}
	if not _write_controls_disabled(scene):
		return {"ok": false, "error": "write controls enabled when db offline"}
	if not await _ui_matches_clip_a(scene):
		return {"ok": false, "error": "clip A ui mismatch when db offline"}
	return {"ok": true, "phase": "db_offline", "snapshot": snap, "session_sha": sha}


func _phase_recovered(scene: Node3D) -> Dictionary:
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	reload_btn.pressed.emit()
	if not await _wait_bridge_idle(scene, true):
		return {"ok": false, "error": "recovered reload timeout"}
	var snap := _session_snapshot(scene)
	if not snap.get("authority_current", false):
		return {"ok": false, "error": "authority not current after recovery"}
	if not snap.get("mutations_enabled", false):
		return {"ok": false, "error": "mutations disabled after recovery"}
	if int(snap.get("stored_revision", -1)) != 5:
		return {"ok": false, "error": "expected revision 5 after recovery"}
	var sha: String = str(snap.get("raw_sha256", ""))
	if sha.length() != 64:
		return {"ok": false, "error": "recovered sha missing"}
	if not await _ui_matches_clip_a(scene):
		return {"ok": false, "error": "clip A ui mismatch after recovery"}
	if not await _verify_clip_b_keep_and_note(scene):
		return {"ok": false, "error": "clip B keep or note mismatch after recovery"}
	if not _banner_coherent_for_live_session(scene):
		return {"ok": false, "error": "banner stale after recovery reload"}
	return {"ok": true, "phase": "recovered", "snapshot": snap, "recovered_sha": sha}


func _triage_matches_post_write(snap: Dictionary) -> bool:
	var triage: Variant = snap.get("triage", {})
	if typeof(triage) != TYPE_DICTIONARY:
		return false
	if not triage.get("derived_ok", false):
		return false
	if triage.get("provisional", true):
		return false
	var counts: Variant = triage.get("counts", {})
	if typeof(counts) != TYPE_DICTIONARY:
		return false
	if int(counts.get("reviewed", -1)) != 2:
		return false
	if int(counts.get("total", -1)) != 2:
		return false
	if int(counts.get("unreviewed", -1)) != 0:
		return false
	if int(counts.get("revise", -1)) != 1:
		return false
	if int(counts.get("keep", -1)) != 1:
		return false
	var revise_ids: Variant = triage.get("revise_clip_ids", [])
	var revise_type := typeof(revise_ids)
	if revise_type != TYPE_ARRAY and revise_type != TYPE_PACKED_STRING_ARRAY:
		return false
	if revise_ids.size() != 1:
		return false
	return str(revise_ids[0]) == CLIP_A


func _banner_coherent_for_live_session(scene: Node3D) -> bool:
	return _banner_coherent_for_live_snapshot(_session_snapshot(scene))


func _banner_coherent_for_live_snapshot(snap: Dictionary) -> bool:
	if not snap.get("authority_current", false):
		return true
	if not snap.get("mutations_enabled", false):
		return true
	if int(snap.get("stored_revision", -1)) < 0:
		return true
	var msg := str(snap.get("status_message", ""))
	for fragment in _STALE_SESSION_BANNER_FRAGMENTS:
		if msg.contains(fragment):
			return false
	return true


func _conflict_banner_honest(snap: Dictionary) -> bool:
	if not snap.get("conflict_active", false):
		return true
	var msg := str(snap.get("status_message", "")).to_lower()
	return not msg.contains("saved")


func _write_controls_disabled(scene: Node3D) -> bool:
	var save_btn: Button = scene.get_node_or_null("%SaveNoteButton") as Button
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	var add_btn: Button = scene.get_node_or_null("%AddBookmarkButton") as Button
	if save_btn == null or note_field == null or status_option == null or add_btn == null:
		return false
	if not save_btn.disabled or not status_option.disabled or not add_btn.disabled:
		return false
	return not note_field.editable


func _apply_clip_a_annotations(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_A):
		return false
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	var save_btn: Button = scene.get_node_or_null("%SaveNoteButton") as Button
	if note_field == null or save_btn == null:
		return false
	note_field.text = NOTE_A
	note_field.text_changed.emit(NOTE_A)
	if save_btn.disabled:
		return false
	save_btn.pressed.emit()
	if not await _wait_bridge_idle(scene, true):
		return false
	if not _banner_coherent_for_live_session(scene):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null:
		return false
	status_option.select(2)
	status_option.item_selected.emit(2)
	if not await _wait_bridge_idle(scene, true):
		return false
	if not _banner_coherent_for_live_session(scene):
		return false
	var seek_slider: HSlider = _playback(scene).get_node_or_null("%SeekSlider") as HSlider
	if seek_slider == null:
		return false
	seek_slider.set_value_no_signal(SEEK_TARGET)
	seek_slider.value_changed.emit(SEEK_TARGET)
	for _i in range(6):
		await process_frame
	var add_btn: Button = scene.get_node_or_null("%AddBookmarkButton") as Button
	if add_btn == null:
		return false
	add_btn.pressed.emit()
	if not await _wait_bridge_idle(scene, true):
		return false
	return _banner_coherent_for_live_session(scene)


func _apply_clip_b_keep(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_B):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null:
		return false
	status_option.select(1)
	status_option.item_selected.emit(1)
	if not await _wait_bridge_idle(scene, true):
		return false
	return status_option.selected == 1


func _verify_clip_b_keep(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_B):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null:
		return false
	return status_option.selected == 1


func _verify_clip_b_keep_and_note(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_B):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null or status_option.selected != 1:
		return false
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	if note_field == null or note_field.text != NOTE_B_EXTERNAL:
		return false
	return true


func _ui_matches_clip_a(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_A):
		return false
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	if note_field == null or note_field.text != NOTE_A:
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null or status_option.selected != 2:
		return false
	var bookmark_option: OptionButton = scene.get_node_or_null("%BookmarkOption") as OptionButton
	if bookmark_option == null or bookmark_option.item_count < 1:
		return false
	var meta: Variant = bookmark_option.get_item_metadata(0)
	return absf(float(meta) - SEEK_TARGET) <= EPS


func _select_clip(scene: Node3D, clip_id: String) -> bool:
	var playback := _playback(scene)
	var clip_option: OptionButton = _playback(scene).get_node_or_null("%ClipOption") as OptionButton
	if clip_option != null:
		for index in range(clip_option.item_count):
			if clip_option.get_item_text(index) == clip_id:
				clip_option.select(index)
				clip_option.item_selected.emit(index)
				for _i in range(12):
					await process_frame
				return str(playback.call("review_source_clip_id")) == clip_id
	if playback.call("request_select_clip", clip_id).get("ok") != true:
		return false
	for _i in range(12):
		await process_frame
	return str(playback.call("review_source_clip_id")) == clip_id


func _session_snapshot(scene: Node3D) -> Dictionary:
	if not scene.has_method("session_state_snapshot"):
		return {}
	return scene.call("session_state_snapshot")


func _playback(scene: Node3D) -> Node:
	return scene.get_node("Playback")


func _wait_playback_ready(scene: Node3D) -> bool:
	var playback := _playback(scene)
	var deadline_ms := Time.get_ticks_msec() + READY_TIMEOUT_MS
	while Time.get_ticks_msec() < deadline_ms:
		if playback.has_method("review_is_ready") and playback.call("review_is_ready"):
			return true
		await process_frame
	return false


func _wait_bridge_idle(scene: Node3D, require_session: bool) -> bool:
	await process_frame
	var saw_busy := false
	var deadline_ms := Time.get_ticks_msec() + BRIDGE_IDLE_TIMEOUT_MS
	while Time.get_ticks_msec() < deadline_ms:
		var snap := _session_snapshot(scene)
		if snap.get("bridge_busy", false):
			saw_busy = true
		elif _bridge_idle_snapshot_ok(snap, require_session, saw_busy):
			return true
		await process_frame
	return false


func _bridge_idle_snapshot_ok(snap: Dictionary, require_session: bool, saw_busy: bool) -> bool:
	if not _snapshot_playback_ready(snap):
		return false
	if not snap.get("bridge_configured", false):
		return true
	if require_session:
		if not saw_busy:
			return false
		return (
			int(snap.get("stored_revision", -1)) >= 0
			and str(snap.get("raw_sha256", "")).length() == 64
		)
	if not saw_busy:
		return false
	return true


func _snapshot_playback_ready(snap: Dictionary) -> bool:
	var playback: Variant = snap.get("playback", {})
	if typeof(playback) != TYPE_DICTIONARY:
		return false
	return bool(playback.get("ready", false))


func _validate_skeleton_and_player(scene: Node3D) -> bool:
	var character := scene.get_node_or_null("Playback/AnimationPreview/Character") as Node3D
	if character == null:
		return false
	if _find_skeleton(character) == null:
		return false
	var playback := _playback(scene)
	var player: AnimationPlayer = playback.call("review_animation_player") as AnimationPlayer
	return player != null


func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null


func _exchange_dir() -> String:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with("--session-exchange-dir="):
			return text.substr("--session-exchange-dir=".length()).strip_edges()
	return ""


func _phase_name() -> String:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(ARG_PHASE):
			return text.substr(ARG_PHASE.length()).strip_edges()
	return ""


func _result_path() -> String:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(ARG_RESULT):
			return text.substr(ARG_RESULT.length()).strip_edges()
	return ""


func _write_result(path: String, payload: Dictionary) -> bool:
	var encoded := JSON.stringify(payload)
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_string(encoded)
	file.close()
	return true


func _fail(message: String) -> void:
	print("FAIL: ", message)
	quit(1)
