extends SceneTree

## V0.8-13b ACTUAL acceptance: compare-session overlay with real review-session bridge.

const CLIP_A := "arm_wave_01"
const CLIP_B := "arm_reverse_02"
const CLIP_C := "spine_turn_03"
const NOTE_A_TERMINAL := "review A"
const NOTE_A_REVISE := "El hareketi fazla sert"
const SEEK_TARGET := 0.75
const SPEED_TARGET := 0.5
const BOOKMARK_A := 1.125
const EPS := 0.0001
const READY_TIMEOUT_MS := 600_000
const BRIDGE_IDLE_TIMEOUT_MS := 600_000
const EXTERNAL_WAIT_TIMEOUT_MS := 600_000
const HANDSHAKE_NAME := "gf_session_inspect_handshake.json"
const EXTERNAL_DONE_NAME := "gf_session_inspect_external_complete.json"
const OFFLINE_READY_NAME := "gf_compare_session_inspect_offline_ready.json"
const OFFLINE_DONE_NAME := "gf_compare_session_inspect_offline_done.json"
const STALE_COMPARE_SAVE_DRAFT := "stale compare save"
const CONTEXT_PREFIX := "--compare-context-file="
const ARG_SESSION_CONTEXT := "--session-context-file="
const ARG_PHASE := "--gf-compare-session-inspect-phase="
const ARG_RESULT := "--gf-compare-session-inspect-result="
const LEFT_UNSAVED_DRAFT := "left-unsaved-draft-probe"
const EXTERNAL_B_NOTE := "external compare-session B note"
const DURATION_A := 1.5
const DURATION_C := 1.25
const VIEWPORT_ORACLE_SIZE := Vector2i(1280, 720)
const RECT_DISPLAY_CONTAIN_TOLERANCE_PX := 1.0

var _last_bridge_wait_diagnostic: String = ""
var _last_viewport_oracle_diagnostic: String = ""
var _transport_baseline: Dictionary = {}
var _session_context_path: String = ""


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	_session_context_path = _session_context_path_from_args()
	var phase := _phase_name()
	var result_path := _result_path()
	if phase.is_empty() or result_path.is_empty():
		_fail("missing phase or result path")
		return
	var packed := load("res://animation_review_compare_session.tscn") as PackedScene
	if packed == null:
		_fail("compare session scene missing")
		return
	var scene := packed.instantiate() as Node3D
	if scene == null:
		_fail("compare session root invalid")
		return
	root.add_child(scene)
	if not await _wait_compare_session_ready(scene):
		_fail("compare session not ready: %s" % _last_viewport_oracle_diagnostic)
		return
	var outcome: Dictionary = {}
	match phase:
		"left_writes":
			outcome = await _phase_left_writes(scene)
		"right_writes":
			outcome = await _phase_right_writes(scene)
		"conflict":
			outcome = await _phase_conflict(scene)
		"stale":
			outcome = await _phase_stale(scene)
		"db_offline":
			outcome = await _phase_db_offline(scene)
		"recovered":
			outcome = await _phase_recovered(scene)
		"final_mutation":
			outcome = await _phase_final_mutation(scene)
		_:
			_fail("unknown phase %s" % phase)
			return
	if not outcome.get("ok", false):
		_fail(str(outcome.get("error", "phase failed")))
		return
	if not _write_result(result_path, outcome):
		_fail("result write failed")
		return
	print("PASS: animation_review_compare_session_%s" % phase)
	quit(0)


func _phase_left_writes(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "left_writes.bootstrap"):
		return {"ok": false, "error": "bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	if not await _configure_transport(scene):
		return {"ok": false, "error": "transport setup failed"}
	_transport_baseline = _transport_fingerprint(scene)
	if not _transport_pose_samples_valid(_transport_baseline.get("pose", [])):
		return {"ok": false, "error": "transport pose baseline empty"}
	if not _left_pane_matches_terminal_a(scene):
		return {"ok": false, "error": "left pane not terminal A state"}
	var before := _session_snapshot(scene)
	if int(before.get("stored_revision", -1)) != 6:
		return {"ok": false, "error": "expected revision 6 before left writes"}
	var note_field: LineEdit = scene.get_node_or_null("%LeftClipNoteField") as LineEdit
	var save_btn: Button = scene.get_node_or_null("%LeftSaveNoteButton") as Button
	var bookmark_btn: Button = scene.get_node_or_null("%LeftAddBookmarkButton") as Button
	var revise_btn: Button = scene.get_node_or_null("%LeftReviseButton") as Button
	if note_field == null or save_btn == null or bookmark_btn == null or revise_btn == null:
		return {"ok": false, "error": "left controls missing"}
	note_field.text = NOTE_A_REVISE
	note_field.text_changed.emit(note_field.text)
	var before_note := _session_snapshot(scene)
	save_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_note, "left_writes.note"):
		return {"ok": false, "error": "note bridge timeout: %s" % _last_bridge_wait_diagnostic}
	if not _verify_ack_matches_sidecar(scene, "left_writes.note"):
		return {"ok": false, "error": "note ack sha/revision mismatch"}
	if not _transport_unchanged(scene):
		return {"ok": false, "error": "transport changed after note"}
	var before_bookmark := _session_snapshot(scene)
	bookmark_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_bookmark, "left_writes.bookmark"):
		return {"ok": false, "error": "bookmark bridge timeout: %s" % _last_bridge_wait_diagnostic}
	if not _verify_ack_matches_sidecar(scene, "left_writes.bookmark"):
		return {"ok": false, "error": "bookmark ack sha/revision mismatch"}
	var bookmark_snap: Dictionary = _session_snapshot(scene).get("compare", {})
	var measured_ts := float(bookmark_snap.get("normalized_progress", -1.0)) * DURATION_A
	if abs(measured_ts - BOOKMARK_A) > EPS:
		return {"ok": false, "error": "bookmark not at measured transport time"}
	if not _provider_eligibility_false(scene):
		return {"ok": false, "error": "provider eligibility not false after bookmark"}
	if not _transport_unchanged(scene):
		return {"ok": false, "error": "transport changed after bookmark"}
	var before_revise := _session_snapshot(scene)
	revise_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_revise, "left_writes.revise"):
		return {"ok": false, "error": "revise bridge timeout: %s" % _last_bridge_wait_diagnostic}
	if not _verify_ack_matches_sidecar(scene, "left_writes.revise"):
		return {"ok": false, "error": "revise ack sha/revision mismatch"}
	if not _transport_unchanged(scene):
		return {"ok": false, "error": "transport changed after revise"}
	var after := _session_snapshot(scene)
	if int(after.get("stored_revision", -1)) <= 6:
		return {"ok": false, "error": "revision did not advance"}
	var sha_after: String = str(after.get("raw_sha256", ""))
	if sha_after.length() != 64 or sha_after == str(before.get("raw_sha256", "")):
		return {"ok": false, "error": "raw sha did not advance"}
	if not _left_ui_matches_revise_state(scene):
		return {"ok": false, "error": "left ui mismatch after writes"}
	if not _verify_ack_matches_sidecar(scene, "left_writes.final"):
		return {"ok": false, "error": "left writes final sha mismatch"}
	return {
		"ok": true,
		"phase": "left_writes",
		"snapshot": after,
		"session_sha": sha_after,
		"sidecar_sha": _sidecar_file_sha256(),
	}


func _phase_right_writes(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "right_writes.bootstrap"):
		return {"ok": false, "error": "bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	if not await _configure_transport(scene):
		return {"ok": false, "error": "transport setup failed"}
	_transport_baseline = _transport_fingerprint(scene)
	if not _transport_pose_samples_valid(_transport_baseline.get("pose", [])):
		return {"ok": false, "error": "transport pose baseline empty"}
	if not _left_ui_matches_revise_state(scene):
		return {"ok": false, "error": "left revise state missing on reopen"}
	if str(scene.get_node("%LeftClipNoteField").text) != NOTE_A_REVISE:
		return {"ok": false, "error": "left saved note missing before right ack"}
	var snap_saved := _session_snapshot(scene)
	if snap_saved.get("left_note_dirty", true):
		return {"ok": false, "error": "left should not be dirty for saved note case"}
	_type_note_draft(scene, "left", LEFT_UNSAVED_DRAFT)
	if not bool(_session_snapshot(scene).get("left_note_dirty", false)):
		return {"ok": false, "error": "left unsaved draft not dirty"}
	var right_draft := "right-pane-draft-probe"
	_type_note_draft(scene, "right", right_draft)
	var before_keep := _session_snapshot(scene)
	var keep_btn: Button = scene.get_node_or_null("%RightKeepButton") as Button
	if keep_btn == null:
		return {"ok": false, "error": "right keep missing"}
	keep_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_keep, "right_writes.keep"):
		return {"ok": false, "error": "right keep timeout: %s" % _last_bridge_wait_diagnostic}
	if not _verify_ack_matches_sidecar(scene, "right_writes.keep"):
		return {"ok": false, "error": "right keep ack sha/revision mismatch"}
	if not _transport_unchanged(scene):
		return {"ok": false, "error": "transport changed after right keep"}
	if str(scene.get_node("%LeftClipNoteField").text) != LEFT_UNSAVED_DRAFT:
		return {"ok": false, "error": "left unsaved draft lost after right keep"}
	if not bool(_session_snapshot(scene).get("left_note_dirty", false)):
		return {"ok": false, "error": "left draft no longer dirty after right keep"}
	if str(scene.get_node("%RightClipNoteField").text) != right_draft:
		return {"ok": false, "error": "right draft lost after right keep"}
	var after := _session_snapshot(scene)
	var right_id := str(after.get("right_clip_id", ""))
	if right_id != CLIP_C:
		return {"ok": false, "error": "right clip target drift"}
	if not _clip_record_note(scene, CLIP_A, NOTE_A_REVISE):
		return {"ok": false, "error": "sidecar left note overwritten by right keep"}
	return {
		"ok": true,
		"phase": "right_writes",
		"snapshot": after,
		"session_sha": after.get("raw_sha256", ""),
		"sidecar_sha": _sidecar_file_sha256(),
	}


func _phase_conflict(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "conflict.bootstrap"):
		return {"ok": false, "error": "bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	var before_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_reload, "conflict.reload"):
		return {"ok": false, "error": "reload timeout: %s" % _last_bridge_wait_diagnostic}
	var before := _session_snapshot(scene)
	var sha: String = str(before.get("raw_sha256", ""))
	if sha.length() != 64:
		return {"ok": false, "error": "cached sha missing"}
	var exchange := _exchange_dir()
	if exchange.is_empty():
		return {"ok": false, "error": "exchange dir missing"}
	var handshake_path := exchange.path_join(HANDSHAKE_NAME)
	var done_path := exchange.path_join(EXTERNAL_DONE_NAME)
	if FileAccess.file_exists(done_path):
		DirAccess.remove_absolute(done_path)
	var handshake := {"raw_sha256": sha, "clip_id": CLIP_B, "note": "external compare-session B note"}
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
	var external_sidecar_sha := _sidecar_file_sha256()
	if external_sidecar_sha.length() != 64:
		return {"ok": false, "error": "external sidecar sha missing after external commit"}
	if external_sidecar_sha == sha:
		return {"ok": false, "error": "sidecar did not advance after external commit"}
	if not _clip_record_note(scene, CLIP_A, NOTE_A_REVISE):
		return {"ok": false, "error": "stored left A note changed after external commit"}
	if not _clip_record_status(scene, CLIP_A, "revise"):
		return {"ok": false, "error": "stored left A status changed after external commit"}
	if not _clip_record_has_bookmark(scene, CLIP_A, BOOKMARK_A):
		return {"ok": false, "error": "stored left A bookmarks changed after external commit"}
	if not _clip_record_note(scene, CLIP_B, EXTERNAL_B_NOTE):
		return {"ok": false, "error": "external clip B note missing after external commit"}
	var note_field: LineEdit = scene.get_node_or_null("%LeftClipNoteField") as LineEdit
	var save_btn: Button = scene.get_node_or_null("%LeftSaveNoteButton") as Button
	if note_field == null or save_btn == null:
		return {"ok": false, "error": "left note controls missing"}
	note_field.text = STALE_COMPARE_SAVE_DRAFT
	note_field.text_changed.emit(note_field.text)
	var before_save := _session_snapshot(scene)
	save_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_save, "conflict.stale_save"):
		return {"ok": false, "error": "stale save timeout: %s" % _last_bridge_wait_diagnostic}
	var after := _session_snapshot(scene)
	if not after.get("conflict_active", false):
		return {"ok": false, "error": "conflict not active"}
	if not after.get("reload_required", false):
		return {"ok": false, "error": "reload not required"}
	if after.get("mutations_enabled", true):
		return {"ok": false, "error": "mutations still enabled"}
	if str(after.get("raw_sha256", "")) != sha:
		return {"ok": false, "error": "ui cached sha should remain pre-external"}
	if str(note_field.text) != STALE_COMPARE_SAVE_DRAFT:
		return {"ok": false, "error": "left draft lost after rejected stale save"}
	if not bool(after.get("left_note_dirty", false)):
		return {"ok": false, "error": "left draft not dirty after rejected stale save"}
	if _sidecar_file_sha256() != external_sidecar_sha:
		return {"ok": false, "error": "external sidecar sha changed after rejected stale save"}
	if not _clip_record_note(scene, CLIP_A, NOTE_A_REVISE):
		return {"ok": false, "error": "stored left A note changed after rejected stale save"}
	if not _clip_record_note(scene, CLIP_B, EXTERNAL_B_NOTE):
		return {"ok": false, "error": "stored external B note changed after rejected stale save"}
	_transport_baseline = _transport_fingerprint(scene)
	if not _transport_pose_samples_valid(_transport_baseline.get("pose", [])):
		return {"ok": false, "error": "transport pose baseline empty after conflict"}
	var before_fresh_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_fresh_reload, "conflict.fresh_reload"):
		return {"ok": false, "error": "fresh reload timeout: %s" % _last_bridge_wait_diagnostic}
	var reloaded := _session_snapshot(scene)
	if not _verify_ack_matches_sidecar(scene, "conflict.fresh_reload"):
		return {"ok": false, "error": "reload ack sha mismatch"}
	if str(reloaded.get("raw_sha256", "")) == sha:
		return {"ok": false, "error": "reload did not pick up external revision"}
	if not _clip_record_note(scene, CLIP_B, EXTERNAL_B_NOTE):
		return {"ok": false, "error": "external clip B note not visible after reload"}
	if str(note_field.text) != STALE_COMPARE_SAVE_DRAFT:
		return {"ok": false, "error": "left draft lost after fresh reload"}
	if not bool(reloaded.get("left_note_dirty", false)):
		return {"ok": false, "error": "left draft not dirty after fresh reload"}
	if not _clip_record_note(scene, CLIP_A, NOTE_A_REVISE):
		return {"ok": false, "error": "stored left A note changed after fresh reload"}
	if not _clip_record_status(scene, CLIP_A, "revise"):
		return {"ok": false, "error": "stored left A status changed after fresh reload"}
	if not _clip_record_has_bookmark(scene, CLIP_A, BOOKMARK_A):
		return {"ok": false, "error": "stored left A bookmarks changed after fresh reload"}
	if not _transport_unchanged(scene):
		return {"ok": false, "error": "transport changed across conflict reload"}
	if _sidecar_file_sha256() != str(reloaded.get("raw_sha256", "")):
		return {"ok": false, "error": "ui sha does not match sidecar after reload"}
	return {
		"ok": true,
		"phase": "conflict",
		"snapshot": reloaded,
		"cached_sha": sha,
		"external_sha": external_sidecar_sha,
	}


func _phase_stale(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "stale.bootstrap"):
		return {"ok": false, "error": "stale bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	var snap := _session_snapshot(scene)
	if snap.get("authority_current", true):
		return {"ok": false, "error": "expected historical authority"}
	if snap.get("mutations_enabled", true):
		return {"ok": false, "error": "mutations enabled when stale"}
	if not _write_controls_disabled(scene):
		return {"ok": false, "error": "write controls enabled when stale"}
	if not await _viewports_nonblank(scene):
		return {"ok": false, "error": "viewports blank when stale: %s" % _last_viewport_oracle_diagnostic}
	if not _left_ui_matches_revise_state(scene):
		return {"ok": false, "error": "saved annotations not visible when stale"}
	var sidecar_sha := _sidecar_file_sha256()
	if sidecar_sha.is_empty():
		return {"ok": false, "error": "sidecar missing when stale"}
	var note_field: LineEdit = scene.get_node_or_null("%RightClipNoteField") as LineEdit
	var save_btn: Button = scene.get_node_or_null("%RightSaveNoteButton") as Button
	if note_field == null or save_btn == null:
		return {"ok": false, "error": "stale write controls missing"}
	note_field.text = "stale rejected write"
	note_field.text_changed.emit(note_field.text)
	if not await _assert_forced_save_rejected(scene, save_btn, sidecar_sha):
		return {"ok": false, "error": "stale write was not rejected (bridge or sidecar changed)"}
	return {
		"ok": true,
		"phase": "stale",
		"snapshot": snap,
		"session_sha": snap.get("raw_sha256", ""),
		"sidecar_sha": sidecar_sha,
		"loaded_session": true,
	}


func _phase_db_offline(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "db_offline.bootstrap"):
		return {"ok": false, "error": "db offline bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	var sidecar_sha := _sidecar_file_sha256()
	if sidecar_sha.length() != 64:
		return {"ok": false, "error": "sidecar missing before db offline reload"}
	var boot_snap := _session_snapshot(scene)
	if not _session_snapshot_loaded(boot_snap):
		return {"ok": false, "error": "expected cached session loaded before db offline reload"}
	if not await _viewports_nonblank(scene):
		return {"ok": false, "error": "viewports blank when db offline: %s" % _last_viewport_oracle_diagnostic}
	if not _left_ui_matches_revise_state(scene):
		return {"ok": false, "error": "cached annotations not visible when db offline"}
	var exchange := _exchange_dir()
	if exchange.is_empty():
		return {"ok": false, "error": "exchange dir missing for db offline handshake"}
	var ready_path := exchange.path_join(OFFLINE_READY_NAME)
	var done_path := exchange.path_join(OFFLINE_DONE_NAME)
	if FileAccess.file_exists(done_path):
		DirAccess.remove_absolute(done_path)
	var ready_payload := {
		"ok": true,
		"raw_sha256": str(boot_snap.get("raw_sha256", "")),
		"sidecar_sha256": sidecar_sha,
		"stored_revision": int(boot_snap.get("stored_revision", -1)),
	}
	var ready_file := FileAccess.open(ready_path, FileAccess.WRITE)
	if ready_file == null:
		return {"ok": false, "error": "db offline ready marker write failed"}
	ready_file.store_string(JSON.stringify(ready_payload))
	ready_file.close()
	var handshake_deadline_ms := Time.get_ticks_msec() + EXTERNAL_WAIT_TIMEOUT_MS
	while Time.get_ticks_msec() < handshake_deadline_ms:
		if FileAccess.file_exists(done_path):
			break
		await process_frame
	if not FileAccess.file_exists(done_path):
		return {"ok": false, "error": "db offline python handshake timeout"}
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	var before_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_reload, "db_offline.reload"):
		return {"ok": false, "error": "db offline reload timeout: %s" % _last_bridge_wait_diagnostic}
	var snap := _session_snapshot(scene)
	if not snap.get("bridge_configured", false):
		return {"ok": false, "error": "bridge not configured when db offline"}
	if not _session_snapshot_loaded(snap):
		return {"ok": false, "error": "cached session not loaded after db offline reload"}
	if snap.get("authority_current", true):
		return {"ok": false, "error": "expected readonly when db offline"}
	if snap.get("mutations_enabled", true):
		return {"ok": false, "error": "mutations enabled when db offline"}
	if not _write_controls_disabled(scene):
		return {"ok": false, "error": "write controls enabled when db offline"}
	if not await _viewports_nonblank(scene):
		return {"ok": false, "error": "viewports blank after db offline reload: %s" % _last_viewport_oracle_diagnostic}
	if not _left_ui_matches_revise_state(scene):
		return {"ok": false, "error": "cached annotations lost after db offline reload"}
	if str(snap.get("raw_sha256", "")) != sidecar_sha:
		return {"ok": false, "error": "ui sha does not match sidecar when db offline"}
	if _sidecar_file_sha256() != sidecar_sha:
		return {"ok": false, "error": "sidecar changed when db offline"}
	var save_btn: Button = scene.get_node_or_null("%RightSaveNoteButton") as Button
	if save_btn == null:
		return {"ok": false, "error": "db offline save control missing"}
	if not await _assert_forced_save_rejected(scene, save_btn, sidecar_sha):
		return {"ok": false, "error": "db offline write was not rejected"}
	return {
		"ok": true,
		"phase": "db_offline",
		"snapshot": snap,
		"session_sha": snap.get("raw_sha256", ""),
		"sidecar_sha": sidecar_sha,
		"loaded_session": true,
	}


func _phase_recovered(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "recovered.bootstrap"):
		return {"ok": false, "error": "recovered bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	var before_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_reload, "recovered.reload"):
		return {"ok": false, "error": "recovered reload timeout: %s" % _last_bridge_wait_diagnostic}
	var snap := _session_snapshot(scene)
	if not snap.get("authority_current", false):
		return {"ok": false, "error": "authority not current after recovery"}
	if not snap.get("mutations_enabled", false):
		return {"ok": false, "error": "mutations disabled after recovery"}
	return {"ok": true, "phase": "recovered", "snapshot": snap, "recovered_sha": snap.get("raw_sha256", "")}


func _phase_final_mutation(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "final.bootstrap"):
		return {"ok": false, "error": "final bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
	if not _session_snapshot(scene).get("mutations_enabled", false):
		return {"ok": false, "error": "mutations disabled before final write"}
	var before := _session_snapshot(scene)
	var save_btn: Button = scene.get_node_or_null("%RightSaveNoteButton") as Button
	var note_field: LineEdit = scene.get_node_or_null("%RightClipNoteField") as LineEdit
	if save_btn == null or note_field == null:
		return {"ok": false, "error": "right note controls missing"}
	note_field.text = "final accepted note"
	note_field.text_changed.emit(note_field.text)
	save_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before, "final.note"):
		return {"ok": false, "error": "final note timeout: %s" % _last_bridge_wait_diagnostic}
	if not _verify_ack_matches_sidecar(scene, "final.note"):
		return {"ok": false, "error": "final note ack sha/revision mismatch"}
	var after := _session_snapshot(scene)
	if int(after.get("stored_revision", -1)) <= int(before.get("stored_revision", -1)):
		return {"ok": false, "error": "final revision did not advance"}
	var rev_after := int(after.get("stored_revision", -1))
	return {
		"ok": true,
		"phase": "final_mutation",
		"snapshot": after,
		"terminal_sha": after.get("raw_sha256", ""),
		"sidecar_sha": _sidecar_file_sha256(),
		"mutation_count_since_baseline": maxi(rev_after - 6, 0),
	}


func _configure_transport(scene: Node3D) -> bool:
	var playback := _compare_playback(scene)
	if playback == null:
		return false
	if playback.has_method("request_set_playback_speed"):
		if playback.call("request_set_playback_speed", SPEED_TARGET).get("ok") != true:
			return false
	if playback.has_method("request_scrub_normalized"):
		if playback.call("request_scrub_normalized", SEEK_TARGET).get("ok") != true:
			return false
	if playback.has_method("request_pause"):
		playback.call("request_pause")
	for _i in range(24):
		await process_frame
	var snap: Dictionary = _session_snapshot(scene).get("compare", {})
	if not snap.get("ready", false):
		return false
	if abs(float(snap.get("normalized_progress", -1.0)) - SEEK_TARGET) > EPS:
		return false
	if abs(float(snap.get("speed", -1.0)) - SPEED_TARGET) > EPS:
		return false
	if not _transport_pose_samples_valid(_weighted_pose_sample(scene)):
		return false
	return not bool(snap.get("playing", true))


func _transport_fingerprint(scene: Node3D) -> Dictionary:
	var snap: Dictionary = _session_snapshot(scene).get("compare", {})
	return {
		"progress": snap.get("normalized_progress"),
		"speed": snap.get("speed"),
		"playing": snap.get("playing"),
		"left_position": snap.get("left_position"),
		"right_position": snap.get("right_position"),
		"left_clip_id": snap.get("left_clip_id"),
		"right_clip_id": snap.get("right_clip_id"),
		"pose": _weighted_pose_sample(scene),
	}


func _transport_unchanged(scene: Node3D) -> bool:
	var current := _transport_fingerprint(scene)
	if not _transport_pose_samples_valid(_transport_baseline.get("pose", [])):
		return false
	if not _transport_pose_samples_valid(current.get("pose", [])):
		return false
	for key in _transport_baseline.keys():
		var before: Variant = _transport_baseline.get(key)
		var now: Variant = current.get(key)
		if typeof(before) == TYPE_FLOAT and typeof(now) == TYPE_FLOAT:
			if abs(float(before) - float(now)) > EPS:
				return false
		elif before != now:
			return false
	return true


func _weighted_pose_sample(scene: Node3D) -> Array:
	var samples: Array = []
	var playback := _compare_playback(scene)
	if playback == null:
		return samples
	for side_name in ["LeftSide", "RightSide"]:
		var side: Node3D = playback.get_node_or_null("%" + side_name) as Node3D
		if side == null:
			return []
		var character := _character(side)
		var skeleton := _find_skeleton(character)
		var mesh := _find_mesh(character)
		if skeleton == null or mesh == null:
			return []
		var baked := mesh.bake_mesh_from_current_skeleton_pose()
		if baked == null or baked.get_surface_count() != 1:
			return []
		var verts: PackedVector3Array = baked.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]
		if verts.is_empty():
			return []
		var indices := _pose_vertex_indices(verts.size())
		for idx in indices:
			if idx < 0 or idx >= verts.size():
				return []
			var v := verts[idx]
			samples.append([side_name, idx, v.x, v.y, v.z])
	return samples


func _transport_pose_samples_valid(pose: Variant) -> bool:
	return typeof(pose) == TYPE_ARRAY and not pose.is_empty()


func _viewports_nonblank(scene: Node3D) -> bool:
	return await _validate_compare_viewport_oracle(scene)


func _validate_compare_viewport_oracle(scene: Node3D) -> bool:
	_last_viewport_oracle_diagnostic = ""
	var playback := _compare_playback(scene)
	if playback == null:
		_last_viewport_oracle_diagnostic = "playback_missing"
		return false
	DisplayServer.window_set_size(VIEWPORT_ORACLE_SIZE)
	for _i in range(12):
		await process_frame
	var left_vp := _subviewport_for_side(playback, true)
	var right_vp := _subviewport_for_side(playback, false)
	if left_vp == null or right_vp == null:
		_last_viewport_oracle_diagnostic = "subviewport_missing"
		return false
	var left_container := _viewport_container_for_side(playback, true)
	var right_container := _viewport_container_for_side(playback, false)
	if left_container == null or right_container == null:
		_last_viewport_oracle_diagnostic = "viewport_container_missing"
		return false
	if not left_vp.own_world_3d or not right_vp.own_world_3d:
		_last_viewport_oracle_diagnostic = "own_world_3d_false"
		return false
	if left_vp.size.x < 8 or left_vp.size.y < 8 or right_vp.size.x < 8 or right_vp.size.y < 8:
		_last_viewport_oracle_diagnostic = "subviewport_size_too_small"
		return false
	var display_rect := scene.get_viewport().get_visible_rect()
	var left_rect := left_container.get_global_rect()
	var right_rect := right_container.get_global_rect()
	if not _global_rect_valid_on_display(left_rect, display_rect):
		_last_viewport_oracle_diagnostic = "left_container_not_on_display"
		return false
	if not _global_rect_valid_on_display(right_rect, display_rect):
		_last_viewport_oracle_diagnostic = "right_container_not_on_display"
		return false
	if left_rect.intersects(right_rect):
		_last_viewport_oracle_diagnostic = "viewport_containers_overlap"
		return false
	var session_panel := scene.get_node_or_null(
		"SessionUI/Root/SessionMargin/SessionScroll/SessionPanel"
	) as Control
	if session_panel != null:
		var panel_rect := session_panel.get_global_rect()
		if panel_rect.size.x >= 8.0 and panel_rect.size.y >= 8.0:
			if left_rect.intersects(panel_rect) or right_rect.intersects(panel_rect):
				_last_viewport_oracle_diagnostic = "viewport_overlaps_session_panel"
				return false
	var left_image := await _viewport_image_oracle_report(left_vp)
	if not bool(left_image.get("ok", false)):
		_last_viewport_oracle_diagnostic = JSON.stringify(left_image)
		return false
	var right_image := await _viewport_image_oracle_report(right_vp)
	if not bool(right_image.get("ok", false)):
		_last_viewport_oracle_diagnostic = JSON.stringify(right_image)
		return false
	return true


func _subviewport_for_side(playback: Control, is_left: bool) -> SubViewport:
	var path := (
		"ViewportSplit/LeftColumn/LeftViewportContainer/LeftViewport"
		if is_left
		else "ViewportSplit/RightColumn/RightViewportContainer/RightViewport"
	)
	return playback.get_node_or_null(path) as SubViewport


func _viewport_container_for_side(playback: Control, is_left: bool) -> SubViewportContainer:
	var path := (
		"ViewportSplit/LeftColumn/LeftViewportContainer"
		if is_left
		else "ViewportSplit/RightColumn/RightViewportContainer"
	)
	return playback.get_node_or_null(path) as SubViewportContainer


func _global_rect_valid_on_display(rect: Rect2, display: Rect2) -> bool:
	if rect.size.x < 8.0 or rect.size.y < 8.0:
		return false
	var tol := RECT_DISPLAY_CONTAIN_TOLERANCE_PX
	var display_max_x := display.position.x + display.size.x
	var display_max_y := display.position.y + display.size.y
	var rect_max_x := rect.position.x + rect.size.x
	var rect_max_y := rect.position.y + rect.size.y
	return (
		rect.position.x >= display.position.x - tol
		and rect.position.y >= display.position.y - tol
		and rect_max_x <= display_max_x + tol
		and rect_max_y <= display_max_y + tol
	)


func _assert_forced_save_rejected(
	scene: Node3D,
	save_btn: Button,
	expected_sidecar_sha: String,
) -> bool:
	if expected_sidecar_sha.length() != 64:
		return false
	var before := _session_snapshot(scene)
	var before_rev := int(before.get("stored_revision", -1))
	var before_sha := str(before.get("raw_sha256", ""))
	if before_sha.length() != 64:
		return false
	save_btn.pressed.emit()
	var deadline_ms := Time.get_ticks_msec() + 5_000
	while Time.get_ticks_msec() < deadline_ms:
		var snap := _session_snapshot(scene)
		if snap.get("bridge_busy", false):
			await process_frame
			continue
		break
	if _session_snapshot(scene).get("bridge_busy", false):
		return false
	var after := _session_snapshot(scene)
	if int(after.get("stored_revision", -1)) != before_rev:
		return false
	if str(after.get("raw_sha256", "")) != before_sha:
		return false
	return _sidecar_file_sha256() == expected_sidecar_sha


func _viewport_image_oracle_report(viewport: SubViewport) -> Dictionary:
	await RenderingServer.frame_post_draw
	var report := {"viewport_size": [viewport.size.x, viewport.size.y]}
	var tex := viewport.get_texture()
	if tex == null:
		return {"ok": false, "reason": "texture_null"}
	var image := tex.get_image()
	if image == null:
		return {"ok": false, "reason": "image_null"}
	var width := image.get_width()
	var height := image.get_height()
	if width < 8 or height < 8:
		return {"ok": false, "reason": "image_too_small"}
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
	var bg_r := 0.0
	var bg_g := 0.0
	var bg_b := 0.0
	for px in corner_pixels:
		if px.a <= 0.05:
			return {"ok": false, "reason": "corner_alpha_low"}
		bg_r += px.r
		bg_g += px.g
		bg_b += px.b
	var inv_corners := 1.0 / float(corner_pixels.size())
	bg_r *= inv_corners
	bg_g *= inv_corners
	bg_b *= inv_corners
	var max_corner_sq := 0.0
	for px in corner_pixels:
		var cr := px.r - bg_r
		var cg := px.g - bg_g
		var cb := px.b - bg_b
		max_corner_sq = maxf(max_corner_sq, cr * cr + cg * cg + cb * cb)
	if max_corner_sq > CORNER_COHERENCE_SQ:
		return {"ok": false, "reason": "corner_incoherent"}
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
	if meaningful < MIN_MEANINGFUL:
		return {"ok": false, "reason": "meaningful_below_min"}
	if sampled > 0 and float(meaningful) / float(sampled) > MAX_MEANINGFUL_FRAC:
		return {"ok": false, "reason": "foreground_fraction_above_max"}
	if maxi(max_x - min_x, max_y - min_y) < MIN_CONTENT_SPAN_PX:
		return {"ok": false, "reason": "content_span_below_min"}
	return {"ok": true}


func _character(side: Node3D) -> Node3D:
	return side.get_node_or_null("AnimationPreview") as Node3D


func _find_mesh(root: Node) -> MeshInstance3D:
	if root is MeshInstance3D and root.name == "SM_HumanoidSkin":
		return root
	for child in root.get_children():
		var found := _find_mesh(child)
		if found != null:
			return found
	return null


func _left_pane_matches_terminal_a(scene: Node3D) -> bool:
	var status: OptionButton = scene.get_node_or_null("%LeftClipStatusOption") as OptionButton
	var note_field: LineEdit = scene.get_node_or_null("%LeftClipNoteField") as LineEdit
	if status == null or note_field == null:
		return false
	if status.get_item_text(status.selected).to_lower() != "keep":
		return false
	if note_field.text != NOTE_A_TERMINAL:
		return false
	return str(_session_snapshot(scene).get("left_clip_id", "")) == CLIP_A


func _left_ui_matches_revise_state(scene: Node3D) -> bool:
	var status: OptionButton = scene.get_node_or_null("%LeftClipStatusOption") as OptionButton
	var note_field: LineEdit = scene.get_node_or_null("%LeftClipNoteField") as LineEdit
	var bookmarks: Label = scene.get_node_or_null("%LeftBookmarksLabel") as Label
	if status == null or note_field == null or bookmarks == null:
		return false
	if status.get_item_text(status.selected).to_lower() != "revise":
		return false
	if note_field.text != NOTE_A_REVISE:
		return false
	if bookmarks.text.find("1.125") < 0:
		return false
	return true


func _write_controls_disabled(scene: Node3D) -> bool:
	for path in [
		"%LeftSaveNoteButton",
		"%RightSaveNoteButton",
		"%LeftAddBookmarkButton",
		"%RightAddBookmarkButton",
		"%LeftKeepButton",
		"%RightKeepButton",
		"%LeftReviseButton",
		"%RightReviseButton",
	]:
		var btn: BaseButton = scene.get_node_or_null(path) as BaseButton
		if btn != null and not btn.disabled:
			return false
	for path in ["%LeftClipNoteField", "%RightClipNoteField"]:
		var field: LineEdit = scene.get_node_or_null(path) as LineEdit
		if field != null and field.editable:
			return false
	return true


func _type_note_draft(scene: Node3D, pane: String, text: String) -> void:
	var field: LineEdit = (
		scene.get_node("%LeftClipNoteField")
		if pane == "left"
		else scene.get_node("%RightClipNoteField")
	) as LineEdit
	field.text = text
	field.text_changed.emit(text)


func _session_snapshot(scene: Node3D) -> Dictionary:
	if not scene.has_method("session_state_snapshot"):
		return {}
	return scene.call("session_state_snapshot")


func _compare_playback(scene: Node3D) -> Control:
	return scene.get_node_or_null("%ComparePlayback") as Control


func _wait_compare_session_ready(scene: Node3D) -> bool:
	var deadline_ms := Time.get_ticks_msec() + READY_TIMEOUT_MS
	while Time.get_ticks_msec() < deadline_ms:
		var snap := _session_snapshot(scene)
		var compare: Variant = snap.get("compare", {})
		if typeof(compare) == TYPE_DICTIONARY and compare.get("ready", false):
			if str(compare.get("left_clip_id", "")) == CLIP_A and str(compare.get("right_clip_id", "")) == CLIP_C:
				if await _viewports_nonblank(scene):
					return true
		await process_frame
	return false


func _wait_bridge_bootstrap(scene: Node3D, require_session: bool, stage: String) -> bool:
	return await _wait_bridge_idle_inner(scene, stage, true, require_session, {})


func _wait_bridge_mutation(scene: Node3D, baseline: Dictionary, stage: String) -> bool:
	return await _wait_bridge_idle_inner(scene, stage, false, true, baseline)


func _wait_bridge_idle(scene: Node3D, stage: String, require_session: bool) -> bool:
	return await _wait_bridge_idle_inner(scene, stage, false, require_session, {})


func _wait_bridge_idle_inner(
	scene: Node3D,
	stage: String,
	bootstrap: bool,
	require_session: bool,
	baseline: Dictionary,
) -> bool:
	_last_bridge_wait_diagnostic = ""
	var saw_busy := bool(_session_snapshot(scene).get("bridge_busy", false))
	await process_frame
	var baseline_rev := int(baseline.get("stored_revision", -1))
	var baseline_sha := str(baseline.get("raw_sha256", ""))
	var last_snap := {}
	var deadline_ms := Time.get_ticks_msec() + BRIDGE_IDLE_TIMEOUT_MS
	while Time.get_ticks_msec() < deadline_ms:
		var snap := _session_snapshot(scene)
		last_snap = snap
		if snap.get("bridge_busy", false):
			saw_busy = true
		elif _bridge_idle_snapshot_ok(
			snap, bootstrap, require_session, saw_busy, baseline_rev, baseline_sha
		):
			return true
		await process_frame
	_last_bridge_wait_diagnostic = (
		"stage=%s bootstrap=%s rev=%s busy=%s"
		% [stage, bootstrap, str(last_snap.get("stored_revision", "?")), str(last_snap.get("bridge_busy", false))]
	)
	return false


func _bridge_idle_snapshot_ok(
	snap: Dictionary,
	bootstrap: bool,
	require_session: bool,
	saw_busy: bool,
	baseline_rev: int,
	baseline_sha: String,
) -> bool:
	if not _snapshot_compare_ready(snap):
		return false
	if not snap.get("bridge_configured", false):
		return true
	if bootstrap:
		if snap.get("bridge_busy", false):
			return false
		if require_session:
			return _session_snapshot_loaded(snap)
		return true
	if saw_busy:
		if require_session:
			return _session_snapshot_loaded(snap)
		return true
	if baseline_rev >= 0 and int(snap.get("stored_revision", -1)) > baseline_rev:
		return _session_snapshot_loaded(snap) if require_session else true
	if (
		not baseline_sha.is_empty()
		and str(snap.get("raw_sha256", "")) != baseline_sha
		and str(snap.get("raw_sha256", "")).length() == 64
	):
		return _session_snapshot_loaded(snap) if require_session else true
	if not require_session and saw_busy and not bootstrap:
		return not snap.get("bridge_busy", false)
	return false


func _session_snapshot_loaded(snap: Dictionary) -> bool:
	return (
		int(snap.get("stored_revision", -1)) >= 0
		and str(snap.get("raw_sha256", "")).length() == 64
	)


func _snapshot_compare_ready(snap: Dictionary) -> bool:
	var compare: Variant = snap.get("compare", {})
	if typeof(compare) != TYPE_DICTIONARY:
		return false
	return bool(compare.get("ready", false))


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


func _session_context_path_from_args() -> String:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(ARG_SESSION_CONTEXT):
			return text.substr(ARG_SESSION_CONTEXT.length()).strip_edges()
	return ""


func _sidecar_session_path() -> String:
	if _session_context_path.is_empty() or not FileAccess.file_exists(_session_context_path):
		return ""
	var raw := FileAccess.get_file_as_string(_session_context_path)
	var parsed: Variant = JSON.parse_string(raw)
	if typeof(parsed) != TYPE_DICTIONARY:
		return ""
	return str(parsed.get("session_path", "")).strip_edges()


func _sidecar_file_sha256() -> String:
	var path := _sidecar_session_path()
	if path.is_empty() or not FileAccess.file_exists(path):
		return ""
	return _sha256_hex(FileAccess.get_file_as_bytes(path))


func _sha256_hex(data: PackedByteArray) -> String:
	if data.is_empty():
		return ""
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(data)
	return ctx.finish().hex_encode()


func _verify_ack_matches_sidecar(scene: Node3D, stage: String) -> bool:
	var snap := _session_snapshot(scene)
	var ui_sha := str(snap.get("raw_sha256", ""))
	var sidecar_sha := _sidecar_file_sha256()
	if sidecar_sha.length() != 64:
		return false
	if ui_sha.length() != 64 or ui_sha != sidecar_sha:
		return false
	var rev := int(snap.get("stored_revision", -1))
	var doc_rev := _sidecar_revision()
	if rev < 0 or doc_rev < 0 or rev != doc_rev:
		return false
	print("STAGE: %s.accepted_rev=%d sha=%s" % [stage, int(snap.get("stored_revision", -1)), ui_sha])
	return true


func _sidecar_revision() -> int:
	var path := _sidecar_session_path()
	if path.is_empty() or not FileAccess.file_exists(path):
		return -1
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if typeof(parsed) != TYPE_DICTIONARY:
		return -1
	return int(parsed.get("revision", -1))


func _clip_record_note(scene: Node3D, clip_id: String, expected_note: String) -> bool:
	var entry := _clip_record_from_sidecar(clip_id)
	if entry.is_empty():
		return false
	return str(entry.get("note", "")) == expected_note


func _clip_record_status(_scene: Node3D, clip_id: String, expected_status: String) -> bool:
	var entry := _clip_record_from_sidecar(clip_id)
	if entry.is_empty():
		return false
	return str(entry.get("status", "")) == expected_status


func _clip_record_has_bookmark(_scene: Node3D, clip_id: String, timestamp: float) -> bool:
	var entry := _clip_record_from_sidecar(clip_id)
	if entry.is_empty():
		return false
	var bookmarks: Variant = entry.get("bookmarks", [])
	if typeof(bookmarks) != TYPE_ARRAY:
		return false
	for entry_ts in bookmarks:
		if typeof(entry_ts) == TYPE_FLOAT or typeof(entry_ts) == TYPE_INT:
			if abs(float(entry_ts) - timestamp) <= EPS:
				return true
	return false


func _clip_record_from_sidecar(clip_id: String) -> Dictionary:
	var path := _sidecar_session_path()
	if path.is_empty():
		return {}
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	var records: Variant = parsed.get("clip_records", [])
	if typeof(records) != TYPE_ARRAY:
		return {}
	for entry in records:
		if typeof(entry) == TYPE_DICTIONARY and str(entry.get("clip_id", "")) == clip_id:
			return entry
	return {}


func _provider_eligibility_false(scene: Node3D) -> bool:
	var snap := _session_snapshot(scene)
	return snap.get("production_eligible") == false and snap.get("promotion_eligible") == false


func _pose_vertex_indices(vert_count: int) -> Array:
	if vert_count < 3:
		return []
	return [0, int(vert_count / 2), vert_count - 1]


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
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_string(JSON.stringify(payload))
	file.close()
	return true


func _fail(message: String) -> void:
	print("FAIL: ", message)
	quit(1)
