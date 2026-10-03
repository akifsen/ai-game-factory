extends SceneTree

const CLIP_A := "arm_wave_01"
const CLIP_B := "arm_reverse_02"
const CLIP_C := "spine_turn_03"
const NOTE_A := "review A"
const DRAFT_NOTE_PROBE := "inspect-draft-probe"
const NOTE_B_EXTERNAL := "external B note"
const SEEK_TARGET := 0.75
const EPS := 0.0001
const READY_TIMEOUT_MS := 600_000
const BRIDGE_IDLE_TIMEOUT_MS := 600_000
const EXTERNAL_WAIT_TIMEOUT_MS := 600_000
const HANDSHAKE_NAME := "gf_session_inspect_handshake.json"
const EXTERNAL_DONE_NAME := "gf_session_inspect_external_complete.json"

var _last_bridge_wait_diagnostic: String = ""

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
		"write_begin":
			outcome = await _phase_write_begin(scene)
		"write_finish":
			outcome = await _phase_write_finish(scene)
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
		"terminal":
			outcome = await _phase_terminal(scene)
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


func _phase_write_begin(scene: Node3D) -> Dictionary:
	print("STAGE: write_begin.entry")
	if not await _wait_bridge_bootstrap(scene, false, "write_begin.bootstrap"):
		return {"ok": false, "error": "bridge idle timeout after bootstrap: %s" % _last_bridge_wait_diagnostic}
	var create_btn: Button = scene.get_node_or_null("%CreateSessionButton") as Button
	if create_btn != null and not create_btn.disabled:
		var before_create := _session_snapshot(scene)
		create_btn.pressed.emit()
		if not await _wait_bridge_mutation(scene, before_create, "write_begin.create_session"):
			return {"ok": false, "error": "create session timeout: %s" % _last_bridge_wait_diagnostic}
		_log_accepted_revision("write_begin.create_session", scene)
		if not _banner_coherent_for_live_session(scene):
			return {"ok": false, "error": "banner stale after create"}
	if not await _apply_clip_a_note_and_revise(scene):
		return {"ok": false, "error": "clip A note and revise failed"}
	var snap := _session_snapshot(scene)
	if not _assert_rev2_intermediate_authority(snap):
		return {"ok": false, "error": "revision 2 authority mismatch after note and revise"}
	if not await _verify_clip_a_rev2_intermediate(scene):
		return {"ok": false, "error": "clip A rev2 ui mismatch"}
	if not await _verify_clip_b_unreviewed(scene):
		return {"ok": false, "error": "clip B should stay unreviewed after rev2"}
	if not await _verify_clip_c_unreviewed(scene):
		return {"ok": false, "error": "clip C should stay unreviewed after rev2"}
	print("STAGE: write_begin.accepted_rev=%d" % int(snap.get("stored_revision", -1)))
	return {"ok": true, "phase": "write_begin", "snapshot": snap}


func _phase_write_finish(scene: Node3D) -> Dictionary:
	print("STAGE: write_finish.entry")
	if not await _wait_bridge_bootstrap(scene, true, "write_finish.bootstrap"):
		return {"ok": false, "error": "bridge idle timeout after bootstrap: %s" % _last_bridge_wait_diagnostic}
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing for write_finish"}
	var before_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_reload, "write_finish.reload_rev2"):
		return {"ok": false, "error": "reload rev2 timeout: %s" % _last_bridge_wait_diagnostic}
	_log_accepted_revision("write_finish.reload_rev2", scene)
	var reloaded := _session_snapshot(scene)
	if int(reloaded.get("stored_revision", -1)) != 2:
		return {"ok": false, "error": "expected revision 2 after reload at write_finish"}
	if not _assert_rev2_intermediate_authority(reloaded):
		return {"ok": false, "error": "revision 2 authority mismatch after reload"}
	if not await _verify_clip_a_rev2_intermediate(scene):
		return {"ok": false, "error": "clip A rev2 ui mismatch after reload"}
	if not await _verify_clip_b_unreviewed(scene):
		return {"ok": false, "error": "clip B should stay unreviewed before bookmark"}
	if not await _verify_clip_c_unreviewed(scene):
		return {"ok": false, "error": "clip C should stay unreviewed before bookmark"}
	if not await _apply_clip_a_bookmark(scene):
		return {"ok": false, "error": "clip A bookmark failed"}
	if not await _apply_clip_b_keep(scene):
		return {"ok": false, "error": "clip B keep failed"}
	var snap := _session_snapshot(scene)
	if int(snap.get("stored_revision", -1)) != 4:
		return {"ok": false, "error": "expected revision 4 after session write"}
	if not _banner_coherent_for_live_session(scene):
		return {"ok": false, "error": "banner stale after session write"}
	if not _triage_matches_post_write(snap):
		return {"ok": false, "error": "triage counts mismatch after session write"}
	if not await _verify_clip_c_unreviewed(scene):
		return {"ok": false, "error": "clip C should stay unreviewed after write"}
	if not await _exercise_triage_ui(scene, _triage_expect_rev4(), true):
		return {"ok": false, "error": "triage ui exercise failed after write"}
	print("STAGE: write_finish.accepted_rev=%d" % int(snap.get("stored_revision", -1)))
	return {"ok": true, "phase": "write_finish", "snapshot": snap}


func _phase_reopen(scene: Node3D) -> Dictionary:
	if not await _wait_bridge_bootstrap(scene, true, "reopen.bootstrap"):
		return {"ok": false, "error": "bridge idle timeout on reopen: %s" % _last_bridge_wait_diagnostic}
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
	if not await _exercise_triage_ui(scene, _triage_expect_rev4(), false):
		return {"ok": false, "error": "triage ui exercise failed on reopen"}
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
	var before_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_reload, "conflict.reload"):
		return {"ok": false, "error": "reload timeout: %s" % _last_bridge_wait_diagnostic}
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
	var before_stale_save := _session_snapshot(scene)
	save_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_stale_save, "conflict.stale_save"):
		return {"ok": false, "error": "stale save bridge timeout: %s" % _last_bridge_wait_diagnostic}
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
	if not await _wait_bridge_bootstrap(scene, true, "stale.bootstrap"):
		return {"ok": false, "error": "stale bootstrap timeout: %s" % _last_bridge_wait_diagnostic}
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
	if not await _exercise_triage_ui(scene, _triage_expect_rev5(), false):
		return {"ok": false, "error": "triage ui exercise failed when stale"}
	return {"ok": true, "phase": "stale", "snapshot": snap, "session_sha": snap.get("raw_sha256", "")}


func _phase_db_offline(scene: Node3D) -> Dictionary:
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	var before_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_reload, "db_offline.reload"):
		return {"ok": false, "error": "db offline reload timeout: %s" % _last_bridge_wait_diagnostic}
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
	if not await _exercise_triage_ui(scene, _triage_expect_rev5(), false):
		return {"ok": false, "error": "triage ui exercise failed when db offline"}
	return {"ok": true, "phase": "db_offline", "snapshot": snap, "session_sha": sha}


func _phase_recovered(scene: Node3D) -> Dictionary:
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
	if not await _exercise_triage_ui(scene, _triage_expect_rev5(), false):
		return {"ok": false, "error": "triage ui exercise failed after recovery"}
	return {"ok": true, "phase": "recovered", "snapshot": snap, "recovered_sha": sha}


func _phase_terminal(scene: Node3D) -> Dictionary:
	var reload_btn: Button = scene.get_node_or_null("%ReloadSessionButton") as Button
	if reload_btn == null:
		return {"ok": false, "error": "reload missing"}
	var before_terminal_reload := _session_snapshot(scene)
	reload_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_terminal_reload, "terminal.reload"):
		return {"ok": false, "error": "terminal reload timeout: %s" % _last_bridge_wait_diagnostic}
	var before := _session_snapshot(scene)
	if int(before.get("stored_revision", -1)) != 5:
		return {"ok": false, "error": "expected revision 5 before terminal keep"}
	if not before.get("mutations_enabled", false):
		return {"ok": false, "error": "mutations disabled before terminal keep"}
	var sha_before: String = str(before.get("raw_sha256", ""))
	if sha_before.length() != 64:
		return {"ok": false, "error": "terminal pre-mutation sha missing"}
	if not await _select_clip(scene, CLIP_A):
		return {"ok": false, "error": "clip A select failed before terminal keep"}
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null or status_option.selected != 2:
		return {"ok": false, "error": "clip A not revise before terminal keep"}
	var counts_before := _triage_counts_dict(before)
	if int(counts_before.get("revise", -1)) != 1:
		return {"ok": false, "error": "unexpected revise count before terminal keep"}
	var before_keep := _session_snapshot(scene)
	status_option.select(1)
	status_option.item_selected.emit(1)
	var deadline_ms := Time.get_ticks_msec() + BRIDGE_IDLE_TIMEOUT_MS
	while Time.get_ticks_msec() < deadline_ms:
		var mid := _session_snapshot(scene)
		if mid.get("bridge_busy", false):
			var mid_counts := _triage_counts_dict(mid)
			if int(mid_counts.get("revise", -1)) != 1:
				return {"ok": false, "error": "optimistic revise count during bridge"}
		else:
			break
		await process_frame
	if not await _wait_bridge_mutation(scene, before_keep, "terminal.keep"):
		return {"ok": false, "error": "terminal keep bridge timeout: %s" % _last_bridge_wait_diagnostic}
	var after := _session_snapshot(scene)
	if int(after.get("stored_revision", -1)) != 6:
		return {"ok": false, "error": "expected revision 6 after terminal keep"}
	var sha_after: String = str(after.get("raw_sha256", ""))
	if sha_after.length() != 64 or sha_after == sha_before:
		return {"ok": false, "error": "terminal keep did not advance raw sha"}
	if not _triage_matches_terminal(after):
		return {"ok": false, "error": "triage counts mismatch after terminal keep"}
	if not await _exercise_triage_ui(scene, _triage_expect_terminal(), false):
		return {"ok": false, "error": "triage ui exercise failed after terminal keep"}
	if not await _verify_clip_b_keep_and_note(scene):
		return {"ok": false, "error": "clip B changed after terminal keep"}
	if not await _verify_clip_c_unreviewed(scene):
		return {"ok": false, "error": "clip C changed after terminal keep"}
	if not await _ui_matches_clip_a_note_only(scene):
		return {"ok": false, "error": "clip A note changed after terminal keep"}
	return {
		"ok": true,
		"phase": "terminal",
		"snapshot": after,
		"terminal_sha": sha_after,
		"pre_terminal_sha": sha_before,
	}


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
	if int(counts.get("total", -1)) != 3:
		return false
	if int(counts.get("unreviewed", -1)) != 1:
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


func _triage_matches_terminal(snap: Dictionary) -> bool:
	var triage: Variant = snap.get("triage", {})
	if typeof(triage) != TYPE_DICTIONARY:
		return false
	if not triage.get("derived_ok", false):
		return false
	var counts: Variant = triage.get("counts", {})
	if typeof(counts) != TYPE_DICTIONARY:
		return false
	if int(counts.get("reviewed", -1)) != 2:
		return false
	if int(counts.get("total", -1)) != 3:
		return false
	if int(counts.get("unreviewed", -1)) != 1:
		return false
	if int(counts.get("revise", -1)) != 0:
		return false
	if int(counts.get("keep", -1)) != 2:
		return false
	var revise_ids: Variant = triage.get("revise_clip_ids", [])
	if typeof(revise_ids) == TYPE_ARRAY or typeof(revise_ids) == TYPE_PACKED_STRING_ARRAY:
		if revise_ids.size() != 0:
			return false
	return true


func _triage_counts_dict(snap: Dictionary) -> Dictionary:
	var triage: Variant = snap.get("triage", {})
	if typeof(triage) != TYPE_DICTIONARY:
		return {}
	var counts: Variant = triage.get("counts", {})
	if typeof(counts) != TYPE_DICTIONARY:
		return {}
	return counts


func _triage_expect_rev4() -> Dictionary:
	return {
		"reviewed": 2,
		"total": 3,
		"unreviewed": 1,
		"revise": 1,
		"keep": 1,
		"filters": {
			"all": [CLIP_A, CLIP_B, CLIP_C],
			"unreviewed": [CLIP_C],
			"revise": [CLIP_A],
			"keep": [CLIP_B],
		},
	}


func _triage_expect_rev5() -> Dictionary:
	return _triage_expect_rev4()


func _triage_expect_terminal() -> Dictionary:
	return {
		"reviewed": 2,
		"total": 3,
		"unreviewed": 1,
		"revise": 0,
		"keep": 2,
		"filters": {
			"all": [CLIP_A, CLIP_B, CLIP_C],
			"unreviewed": [CLIP_C],
			"revise": [],
			"keep": [CLIP_A, CLIP_B],
		},
	}


func _string_array_equals(left: Variant, right: Array) -> bool:
	if typeof(left) != TYPE_ARRAY and typeof(left) != TYPE_PACKED_STRING_ARRAY:
		return false
	if left.size() != right.size():
		return false
	for index in range(right.size()):
		if str(left[index]) != str(right[index]):
			return false
	return true


func _playback_ui_state(scene: Node3D) -> Dictionary:
	var playback := _playback(scene)
	var snap: Dictionary = {}
	if scene.has_method("review_playback_snapshot"):
		var raw: Variant = scene.call("review_playback_snapshot")
		if typeof(raw) == TYPE_DICTIONARY:
			snap = raw
	var loop_check: CheckButton = playback.get_node_or_null("%LoopCheck") as CheckButton
	return {
		"clip_id": str(playback.call("review_source_clip_id")),
		"position": float(snap.get("position_seconds", -1.0)),
		"speed": float(snap.get("speed_scale", -1.0)),
		"loop": loop_check.button_pressed if loop_check != null else false,
	}


func _playback_ui_state_matches(a: Dictionary, b: Dictionary) -> bool:
	if a.get("clip_id", "") != b.get("clip_id", ""):
		return false
	if absf(float(a.get("position", 0.0)) - float(b.get("position", 0.0))) > EPS:
		return false
	if float(a.get("speed", 0.0)) != float(b.get("speed", 0.0)):
		return false
	return a.get("loop", false) == b.get("loop", false)


func _playback_speed_loop_matches(a: Dictionary, b: Dictionary) -> bool:
	if float(a.get("speed", 0.0)) != float(b.get("speed", 0.0)):
		return false
	return a.get("loop", false) == b.get("loop", false)


func _playback_position_near(state: Dictionary, target: float) -> bool:
	return absf(float(state.get("position", -1.0)) - target) <= EPS


func _filtered_clip_shows_outside(scene: Node3D, expected_clip_id: String) -> bool:
	var outside_label: Label = scene.get_node_or_null("%FilteredClipOutsideLabel") as Label
	var filtered_option: OptionButton = scene.get_node_or_null("%FilteredClipOption") as OptionButton
	if outside_label == null or filtered_option == null:
		return false
	if filtered_option.selected != -1:
		return false
	return outside_label.text.find(expected_clip_id) != -1


func _triage_snapshot(scene: Node3D) -> Dictionary:
	if not scene.has_method("review_triage_snapshot"):
		return {}
	return scene.call("review_triage_snapshot")


func _assert_triage_counts(triage: Dictionary, expected: Dictionary) -> bool:
	var counts: Variant = triage.get("counts", {})
	if typeof(counts) != TYPE_DICTIONARY:
		return false
	for key in ["reviewed", "total", "unreviewed", "revise", "keep"]:
		if int(counts.get(key, -1)) != int(expected.get(key, -2)):
			return false
	return true


func _assert_filter_memberships(scene: Node3D, expected_filters: Dictionary) -> bool:
	var membership_baseline := _playback_ui_state(scene)
	for filter_name in expected_filters.keys():
		if not _playback_ui_state_matches(membership_baseline, _playback_ui_state(scene)):
			return false
		var result: Dictionary = scene.call("request_set_triage_filter", filter_name)
		if result.get("ok", false) != true:
			return false
		await scene.get_tree().process_frame
		if not _playback_ui_state_matches(membership_baseline, _playback_ui_state(scene)):
			return false
		var triage := _triage_snapshot(scene)
		if not _string_array_equals(triage.get("filtered_clip_ids", []), expected_filters[filter_name]):
			return false
	return true


func _exercise_triage_ui(scene: Node3D, expected: Dictionary, include_draft_probe: bool) -> bool:
	var snap := _session_snapshot(scene)
	var triage: Variant = snap.get("triage", {})
	if typeof(triage) != TYPE_DICTIONARY:
		return false
	if not _assert_triage_counts(triage, expected):
		return false
	var expected_filters: Dictionary = expected.get("filters", {})
	if not await _assert_filter_memberships(scene, expected_filters):
		return false
	var filter_before_invalid := str(_triage_snapshot(scene).get("filter", ""))
	var invalid_result: Variant = scene.call("request_set_triage_filter", "not_a_filter")
	if typeof(invalid_result) != TYPE_DICTIONARY:
		return false
	var invalid: Dictionary = invalid_result
	if invalid.get("ok", false):
		return false
	var after_invalid := _triage_snapshot(scene)
	if str(after_invalid.get("filter", "")) != filter_before_invalid:
		return false
	var baseline := _playback_ui_state(scene)
	scene.call("request_set_triage_filter", "keep")
	await scene.get_tree().process_frame
	if not _playback_ui_state_matches(baseline, _playback_ui_state(scene)):
		return false
	if not await _exercise_revise_navigation(scene, expected):
		return false
	if include_draft_probe:
		if not await _exercise_filtered_selection_preservation(scene, expected_filters):
			return false
	scene.call("request_set_triage_filter", "all")
	await scene.get_tree().process_frame
	return true


func _exercise_revise_navigation(scene: Node3D, expected: Dictionary) -> bool:
	var revise_count := int(expected.get("revise", 0))
	if revise_count <= 0:
		if not await _select_clip(scene, CLIP_B):
			return false
		var pose := _playback_ui_state(scene)
		var next_zero: Dictionary = scene.call("request_next_revise")
		if not next_zero.get("ok", false) or not next_zero.get("noop", false):
			return false
		var prev_zero: Dictionary = scene.call("request_previous_revise")
		if not prev_zero.get("ok", false) or not prev_zero.get("noop", false):
			return false
		if not _playback_ui_state_matches(pose, _playback_ui_state(scene)):
			return false
		return true
	if not await _select_clip(scene, CLIP_A):
		return false
	var sole_pose := _playback_ui_state(scene)
	var sole_next: Dictionary = scene.call("request_next_revise")
	if not sole_next.get("ok", false) or not sole_next.get("noop", false):
		return false
	var sole_prev: Dictionary = scene.call("request_previous_revise")
	if not sole_prev.get("ok", false) or not sole_prev.get("noop", false):
		return false
	if not _playback_ui_state_matches(sole_pose, _playback_ui_state(scene)):
		return false
	if not await _select_clip(scene, CLIP_B):
		return false
	var from_b := _playback_ui_state(scene)
	var to_a: Dictionary = scene.call("request_next_revise")
	if not to_a.get("ok", false) or to_a.get("noop", false):
		return false
	if str(_playback(scene).call("review_source_clip_id")) != CLIP_A:
		return false
	if not await _select_clip(scene, CLIP_C):
		return false
	var from_c := _playback_ui_state(scene)
	var c_to_a: Dictionary = scene.call("request_previous_revise")
	if not c_to_a.get("ok", false) or c_to_a.get("noop", false):
		return false
	if str(_playback(scene).call("review_source_clip_id")) != CLIP_A:
		return false
	if not await _select_clip(scene, CLIP_B):
		return false
	var wrap_prev: Dictionary = scene.call("request_previous_revise")
	if not wrap_prev.get("ok", false) or wrap_prev.get("noop", false):
		return false
	if str(_playback(scene).call("review_source_clip_id")) != CLIP_A:
		return false
	return true


func _exercise_filtered_selection_preservation(scene: Node3D, expected_filters: Dictionary) -> bool:
	var playback := _playback(scene)
	if not await _select_clip(scene, CLIP_A):
		return false
	if playback.call("request_set_playback_speed", 0.5).get("ok") != true:
		return false
	if playback.call("request_set_loop", true).get("ok") != true:
		return false
	var seek_slider: HSlider = playback.get_node_or_null("%SeekSlider") as HSlider
	if seek_slider != null:
		seek_slider.set_value_no_signal(SEEK_TARGET)
		seek_slider.value_changed.emit(SEEK_TARGET)
		for _i in range(4):
			await scene.get_tree().process_frame
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	if note_field == null:
		return false
	note_field.text = DRAFT_NOTE_PROBE
	note_field.text_changed.emit(DRAFT_NOTE_PROBE)
	var saved_a := _playback_ui_state(scene)
	scene.call("request_set_triage_filter", "keep")
	await scene.get_tree().process_frame
	var pick_b: Dictionary = scene.call("request_select_filtered_clip_index", 0)
	if pick_b.get("ok") != true:
		return false
	for _i in range(8):
		await scene.get_tree().process_frame
	if str(playback.call("review_source_clip_id")) != CLIP_B:
		return false
	if note_field.text == DRAFT_NOTE_PROBE:
		return false
	scene.call("request_set_triage_filter", "revise")
	await scene.get_tree().process_frame
	if not _filtered_clip_shows_outside(scene, CLIP_B):
		return false
	var pick_a: Dictionary = scene.call("request_select_filtered_clip_index", 0)
	if pick_a.get("ok") != true:
		return false
	for _i in range(8):
		await scene.get_tree().process_frame
	if str(playback.call("review_source_clip_id")) != CLIP_A:
		return false
	var returned_a := _playback_ui_state(scene)
	if not _playback_speed_loop_matches(saved_a, returned_a):
		return false
	if not _playback_position_near(returned_a, 0.0):
		return false
	if note_field.text != DRAFT_NOTE_PROBE:
		return false
	var bookmark_option: OptionButton = scene.get_node_or_null("%BookmarkOption") as OptionButton
	if bookmark_option == null or bookmark_option.item_count < 1:
		return false
	var meta: Variant = bookmark_option.get_item_metadata(0)
	if absf(float(meta) - SEEK_TARGET) > EPS:
		return false
	var seek_btn: Button = scene.get_node_or_null("%SeekBookmarkButton") as Button
	if seek_btn == null or seek_btn.disabled:
		return false
	seek_btn.pressed.emit()
	for _i in range(8):
		await scene.get_tree().process_frame
	if not _playback_ui_state_matches(saved_a, _playback_ui_state(scene)):
		return false
	note_field.text = NOTE_A
	note_field.text_changed.emit(NOTE_A)
	await scene.get_tree().process_frame
	return true


func _verify_clip_c_unreviewed(scene: Node3D) -> bool:
	return await _verify_clip_unreviewed(scene, CLIP_C)


func _verify_clip_b_unreviewed(scene: Node3D) -> bool:
	return await _verify_clip_unreviewed(scene, CLIP_B)


func _verify_clip_unreviewed(scene: Node3D, clip_id: String) -> bool:
	if not await _select_clip(scene, clip_id):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null or status_option.selected != 0:
		return false
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	if note_field == null or not note_field.text.is_empty():
		return false
	var bookmark_option: OptionButton = scene.get_node_or_null("%BookmarkOption") as OptionButton
	if bookmark_option == null:
		return false
	return bookmark_option.item_count == 0


func _verify_clip_a_rev2_intermediate(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_A):
		return false
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	if note_field == null or note_field.text != NOTE_A:
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null or status_option.selected != 2:
		return false
	var bookmark_option: OptionButton = scene.get_node_or_null("%BookmarkOption") as OptionButton
	if bookmark_option == null or bookmark_option.item_count != 0:
		return false
	return true


func _assert_rev2_intermediate_authority(snap: Dictionary) -> bool:
	if int(snap.get("stored_revision", -1)) != 2:
		return false
	if not snap.get("authority_current", false):
		return false
	if not snap.get("mutations_enabled", false):
		return false
	return str(snap.get("raw_sha256", "")).length() == 64


func _ui_matches_clip_a_note_only(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_A):
		return false
	var note_field: LineEdit = scene.get_node_or_null("%ClipNoteField") as LineEdit
	if note_field == null or note_field.text != NOTE_A:
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	return status_option != null and status_option.selected == 1


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


func _log_accepted_revision(stage: String, scene: Node3D) -> void:
	var snap := _session_snapshot(scene)
	print("STAGE: %s.accepted_rev=%d" % [stage, int(snap.get("stored_revision", -1))])


func _apply_clip_a_note_and_revise(scene: Node3D) -> bool:
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
	var before_note := _session_snapshot(scene)
	save_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_note, "write_begin.clip_a.save_note"):
		return false
	_log_accepted_revision("write_begin.clip_a.save_note", scene)
	if not _banner_coherent_for_live_session(scene):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null:
		return false
	var before_revise := _session_snapshot(scene)
	status_option.select(2)
	status_option.item_selected.emit(2)
	if not await _wait_bridge_mutation(scene, before_revise, "write_begin.clip_a.revise"):
		return false
	_log_accepted_revision("write_begin.clip_a.revise", scene)
	return _banner_coherent_for_live_session(scene)


func _apply_clip_a_bookmark(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_A):
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
	var before_bookmark := _session_snapshot(scene)
	add_btn.pressed.emit()
	if not await _wait_bridge_mutation(scene, before_bookmark, "write_finish.clip_a.bookmark"):
		return false
	_log_accepted_revision("write_finish.clip_a.bookmark", scene)
	return _banner_coherent_for_live_session(scene)


func _apply_clip_b_keep(scene: Node3D) -> bool:
	if not await _select_clip(scene, CLIP_B):
		return false
	var status_option: OptionButton = scene.get_node_or_null("%ClipStatusOption") as OptionButton
	if status_option == null:
		return false
	var before_keep := _session_snapshot(scene)
	status_option.select(1)
	status_option.item_selected.emit(1)
	if not await _wait_bridge_mutation(scene, before_keep, "write_finish.clip_b.keep"):
		return false
	_log_accepted_revision("write_finish.clip_b.keep", scene)
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


func _wait_bridge_bootstrap(scene: Node3D, require_session: bool, stage: String) -> bool:
	return await _wait_bridge_idle_inner(scene, stage, true, require_session, {})


func _wait_bridge_mutation(scene: Node3D, baseline: Dictionary, stage: String) -> bool:
	return await _wait_bridge_idle_inner(scene, stage, false, true, baseline)


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
	_last_bridge_wait_diagnostic = _format_bridge_wait_timeout(
		stage, bootstrap, require_session, saw_busy, baseline_rev, baseline_sha, last_snap
	)
	print("FAIL_BRIDGE_WAIT: ", _last_bridge_wait_diagnostic)
	return false


func _format_bridge_wait_timeout(
	stage: String,
	bootstrap: bool,
	require_session: bool,
	saw_busy: bool,
	baseline_rev: int,
	baseline_sha: String,
	snap: Dictionary,
) -> String:
	var triage: Variant = snap.get("triage", {})
	var triage_prov := "?"
	if typeof(triage) == TYPE_DICTIONARY:
		triage_prov = str(triage.get("provisional", "?"))
	return (
		"stage=%s bootstrap=%s require_session=%s saw_busy=%s baseline_rev=%d baseline_sha_len=%d "
		+ "busy=%s configured=%s rev=%s sha_len=%d authority=%s mutations=%s triage_provisional=%s"
	) % [
		stage,
		bootstrap,
		require_session,
		saw_busy,
		baseline_rev,
		baseline_sha.length(),
		str(snap.get("bridge_busy", false)),
		str(snap.get("bridge_configured", false)),
		str(snap.get("stored_revision", "?")),
		str(snap.get("raw_sha256", "")).length(),
		str(snap.get("authority_current", "?")),
		str(snap.get("mutations_enabled", "?")),
		triage_prov,
	]


func _bridge_idle_snapshot_ok(
	snap: Dictionary,
	bootstrap: bool,
	require_session: bool,
	saw_busy: bool,
	baseline_rev: int,
	baseline_sha: String,
) -> bool:
	if not _snapshot_playback_ready(snap):
		return false
	if not snap.get("bridge_configured", false):
		return true
	if bootstrap:
		if require_session:
			return _session_snapshot_loaded(snap)
		return true
	if saw_busy:
		if require_session:
			return _session_snapshot_loaded(snap)
		return true
	if baseline_rev >= 0 and int(snap.get("stored_revision", -1)) > baseline_rev:
		return _session_snapshot_loaded(snap)
	if (
		not baseline_sha.is_empty()
		and str(snap.get("raw_sha256", "")) != baseline_sha
		and str(snap.get("raw_sha256", "")).length() == 64
	):
		return _session_snapshot_loaded(snap)
	return false


func _session_snapshot_loaded(snap: Dictionary) -> bool:
	return (
		int(snap.get("stored_revision", -1)) >= 0
		and str(snap.get("raw_sha256", "")).length() == 64
	)


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
