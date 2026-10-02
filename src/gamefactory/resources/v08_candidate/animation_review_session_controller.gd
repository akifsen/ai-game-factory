extends Node3D

## V0.8-9b session overlay: review-set playback plus session annotation UI.
## Bridge I/O is implemented here; Python authority integration is wired by the launcher.

const BRIDGE_SCHEMA_VERSION := "animation-review-session-bridge-0.8.0"
const SESSION_SCHEMA_VERSION := "animation-review-session-0.8.0"
const MAX_RESPONSE_BYTES := 72 * 1024
const MAX_NOTE_UTF8_BYTES := 2048
const MAX_BOOKMARKS_PER_CLIP := 32
const MIN_BINDING_CLIPS := 2
const MAX_BINDING_CLIPS := 8
const MAX_DURATION_SECONDS := 10.0
const BRIDGE_PROCESS_TIMEOUT_MS := 600_000
const PLAYBACK_RESERVE_PX := 260
const SESSION_PANEL_WIDTH_PX := 360
const STATUS_OPTIONS := ["unreviewed", "keep", "revise"]

const ARG_PYTHON := "--session-python-executable="
const ARG_CONTEXT := "--session-context-file="
const ARG_EXCHANGE := "--session-exchange-dir="
const ARG_VALIDATION_PROBE := "--gf-session-bridge-validation-probe"

const _SESSION_KEYS := [
	"schema_version",
	"binding",
	"revision",
	"clip_records",
	"production_eligible",
	"promotion_eligible",
]
const _BINDING_KEYS := [
	"review_root",
	"raw_manifest_sha256",
	"root_payload_sha256",
	"clip_payload_sha256",
	"clips",
]
const _CLIP_BINDING_KEYS := ["clip_id", "raw_clip_sha256", "duration_seconds"]
const _RECORD_KEYS := ["clip_id", "status", "note", "bookmarks"]
const _STORED_KEYS := ["session", "raw_sha256"]
const _RESPONSE_EXACT_KEYS := [
	"schema_version",
	"ok",
	"committed",
	"current",
	"stored",
	"error",
]

@onready var _playback: Node = $Playback
@onready var _session_root: Control = $SessionUI/Root
@onready var _session_margin: MarginContainer = %SessionMargin
@onready var _status_banner: Label = %SessionStatusBanner
@onready var _reload_button: Button = %ReloadSessionButton
@onready var _create_button: Button = %CreateSessionButton
@onready var _status_option: OptionButton = %ClipStatusOption
@onready var _note_field: LineEdit = %ClipNoteField
@onready var _save_note_button: Button = %SaveNoteButton
@onready var _add_bookmark_button: Button = %AddBookmarkButton
@onready var _bookmark_option: OptionButton = %BookmarkOption
@onready var _seek_bookmark_button: Button = %SeekBookmarkButton

var _python_executable: String = ""
var _context_file: String = ""
var _exchange_dir: String = ""
var _bridge_configured: bool = false

var _stored_session: Variant = null
var _raw_sha256: String = ""
var _authority_current: bool = false

var _bridge_busy: bool = false
var _inflight_pid: int = -1
var _inflight_request_path: String = ""
var _inflight_response_path: String = ""
var _inflight_started_ms: int = 0
var _inflight_action: String = ""
var _inflight_owns_exchange_files: bool = false
var _inflight_clip_id: String = ""
var _inflight_submitted_note: String = ""
var _inflight_update_op: String = ""

var _reload_required: bool = false
var _conflict_active: bool = false
var _user_status_message: String = ""

var _tracked_clip_id: String = ""
var _ui_syncing: bool = false
var _note_dirty: bool = false
var _draft_notes: Dictionary = {}
var _request_serial: int = 0
var _bridge_rng: RandomNumberGenerator = RandomNumberGenerator.new()


func _ready() -> void:
	if _wants_validation_probe():
		var ok := _run_bridge_validation_probe()
		get_tree().quit(0 if ok else 1)
		return
	_bridge_rng.randomize()
	_parse_launch_configuration()
	_wire_session_ui()
	_wire_session_layout()
	_refresh_session_controls()
	call_deferred("_bootstrap_session")


func _exit_tree() -> void:
	if _inflight_pid < 0:
		return
	if OS.is_process_running(_inflight_pid):
		OS.kill(_inflight_pid)
	var request_path := _inflight_request_path
	var response_path := _inflight_response_path
	var owns := _inflight_owns_exchange_files
	_clear_inflight_process_state()
	if owns:
		_cleanup_owned_exchange_files(request_path, response_path)


func _bootstrap_session() -> void:
	if not await _wait_playback_ready():
		_set_banner("Playback is not ready; session UI is idle.")
		return
	_tracked_clip_id = _playback_source_clip_id()
	_apply_clip_annotations_for(_tracked_clip_id)
	if _bridge_configured:
		request_reload_session()
	else:
		_set_banner("Session bridge unavailable (missing launch configuration). Playback only.")


func _parse_launch_configuration() -> void:
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(ARG_PYTHON):
			_python_executable = text.substr(ARG_PYTHON.length()).strip_edges()
		elif text.begins_with(ARG_CONTEXT):
			_context_file = text.substr(ARG_CONTEXT.length()).strip_edges()
		elif text.begins_with(ARG_EXCHANGE):
			_exchange_dir = text.substr(ARG_EXCHANGE.length()).strip_edges()
	_bridge_configured = (
		not _python_executable.is_empty()
		and not _context_file.is_empty()
		and not _exchange_dir.is_empty()
		and DirAccess.dir_exists_absolute(_exchange_dir)
	)


func _wire_session_ui() -> void:
	_reload_button.pressed.connect(_on_reload_pressed)
	_create_button.pressed.connect(_on_create_pressed)
	_save_note_button.pressed.connect(_on_save_note_pressed)
	_add_bookmark_button.pressed.connect(_on_add_bookmark_pressed)
	_seek_bookmark_button.pressed.connect(_on_seek_bookmark_pressed)
	_status_option.item_selected.connect(_on_status_option_selected)
	_note_field.text_changed.connect(_on_note_text_changed)
	_status_option.clear()
	for status in STATUS_OPTIONS:
		_status_option.add_item(status.capitalize())


func _wire_session_layout() -> void:
	if not _session_root.resized.is_connected(_on_session_viewport_layout):
		_session_root.resized.connect(_on_session_viewport_layout)
	if not get_viewport().size_changed.is_connected(_on_session_viewport_layout):
		get_viewport().size_changed.connect(_on_session_viewport_layout)
	call_deferred("_on_session_viewport_layout")


func _on_session_viewport_layout() -> void:
	if _session_margin == null or _session_root == null:
		return
	var horizontal_margin := 32
	var panel_width := mini(SESSION_PANEL_WIDTH_PX, maxi(200, _session_root.size.x - horizontal_margin))
	_session_margin.offset_left = -panel_width
	_session_margin.offset_bottom = -PLAYBACK_RESERVE_PX


func _process(_delta: float) -> void:
	_poll_bridge_process()
	if _playback == null:
		return
	if not _playback_ready():
		return
	var clip_id := _playback_source_clip_id()
	if clip_id != _tracked_clip_id:
		_commit_note_draft_for_clip(_tracked_clip_id)
		_tracked_clip_id = clip_id
		_apply_clip_annotations_for(clip_id)


func _poll_bridge_process() -> void:
	if _inflight_pid < 0:
		return
	var now_ms := Time.get_ticks_msec()
	if now_ms - _inflight_started_ms > BRIDGE_PROCESS_TIMEOUT_MS:
		_abort_inflight_bridge(
			"Session bridge timed out. Reload to confirm session state before editing."
		)
		return
	if OS.is_process_running(_inflight_pid):
		return
	_finish_inflight_bridge()


func _wait_playback_ready() -> bool:
	var frames := 0
	while frames < 300:
		frames += 1
		if _playback_ready():
			return true
		await get_tree().process_frame
	return false


func _playback_ready() -> bool:
	return _playback != null and _playback.has_method("review_is_ready") and _playback.review_is_ready()


func _playback_source_clip_id() -> String:
	if _playback == null or not _playback.has_method("review_source_clip_id"):
		return ""
	return str(_playback.review_source_clip_id())


# --- Public API for acceptance / inspector (baseline) ---


func session_state_snapshot() -> Dictionary:
	return {
		"bridge_configured": _bridge_configured,
		"bridge_busy": _bridge_busy,
		"authority_current": _authority_current,
		"mutations_enabled": _mutations_enabled(),
		"reload_required": _reload_required,
		"note_unsaved": _note_dirty,
		"conflict_active": _conflict_active,
		"stored_revision": _stored_revision(),
		"raw_sha256": _raw_sha256,
		"selected_clip_id": _tracked_clip_id,
		"status_message": _status_banner.text,
		"playback": review_playback_snapshot(),
	}


func review_playback_snapshot() -> Dictionary:
	if not _playback_ready():
		return {"ready": false}
	var player: AnimationPlayer = _playback.review_animation_player()
	if player == null:
		return {"ready": false}
	return {
		"ready": true,
		"clip_id": _playback_source_clip_id(),
		"position_seconds": player.current_animation_position,
		"duration_seconds": _playback.review_duration_seconds(),
		"speed_scale": player.speed_scale,
		"is_playing": player.is_playing(),
	}


func request_reload_session() -> Dictionary:
	return _enqueue_bridge_action("read")


func request_create_session() -> Dictionary:
	if not _bridge_configured:
		return {"ok": false, "error_code": "bridge_unavailable"}
	if _reload_required or _conflict_active:
		return {"ok": false, "error_code": "reload_required"}
	if not _authority_current:
		return {"ok": false, "error_code": "authority_not_current"}
	if _stored_session != null:
		return {"ok": false, "error_code": "session_already_exists"}
	return _enqueue_bridge_action("create")


func request_save_note() -> Dictionary:
	var clip_id := _tracked_clip_id
	if clip_id.is_empty():
		return {"ok": false, "error_code": "no_clip_selected"}
	if not _mutations_enabled():
		return {"ok": false, "error_code": "mutations_disabled"}
	var note := _note_field.text
	if not _note_within_limit(note):
		_user_status_message = (
			"Note exceeds the %d-byte limit. Shorten it and try again." % MAX_NOTE_UTF8_BYTES
		)
		_refresh_session_controls()
		return {"ok": false, "error_code": "note_too_long"}
	return _enqueue_bridge_update(
		{"op": "SetNote", "clip_id": clip_id, "note": note},
		clip_id,
		note,
	)


func request_set_clip_status(status: String) -> Dictionary:
	if not STATUS_OPTIONS.has(status):
		return {"ok": false, "error_code": "invalid_status"}
	if _tracked_clip_id.is_empty():
		return {"ok": false, "error_code": "no_clip_selected"}
	if not _mutations_enabled():
		return {"ok": false, "error_code": "mutations_disabled"}
	return _enqueue_bridge_update(
		{"op": "SetStatus", "clip_id": _tracked_clip_id, "status": status},
		_tracked_clip_id,
		"",
	)


func request_add_bookmark_at_playback() -> Dictionary:
	if _tracked_clip_id.is_empty():
		return {"ok": false, "error_code": "no_clip_selected"}
	if not _mutations_enabled():
		return {"ok": false, "error_code": "mutations_disabled"}
	var record := _clip_record_for(_tracked_clip_id)
	var existing: Variant = record.get("bookmarks", [])
	if typeof(existing) == TYPE_ARRAY and existing.size() >= MAX_BOOKMARKS_PER_CLIP:
		return {"ok": false, "error_code": "bookmark_limit"}
	var snap := review_playback_snapshot()
	if not snap.get("ready", false):
		return {"ok": false, "error_code": "playback_not_ready"}
	var timestamp := float(snap.get("position_seconds", -1.0))
	if not _bookmark_timestamp_valid(_tracked_clip_id, timestamp):
		return {"ok": false, "error_code": "invalid_bookmark_time"}
	return _enqueue_bridge_update(
		{"op": "AddBookmark", "clip_id": _tracked_clip_id, "timestamp": timestamp},
		_tracked_clip_id,
		"",
	)


func request_seek_saved_bookmark(index: int) -> Dictionary:
	if index < 0 or index >= _bookmark_option.item_count:
		return {"ok": false, "error_code": "invalid_bookmark_index"}
	var meta: Variant = _bookmark_option.get_item_metadata(index)
	if typeof(meta) != TYPE_FLOAT and typeof(meta) != TYPE_INT:
		return {"ok": false, "error_code": "invalid_bookmark_metadata"}
	if _playback == null or not _playback.has_method("request_seek"):
		return {"ok": false, "error_code": "playback_missing"}
	return _playback.request_seek(float(meta))


func request_refresh_clip_annotations() -> void:
	_apply_clip_annotations_for(_tracked_clip_id)


# --- UI handlers ---


func _on_reload_pressed() -> void:
	request_reload_session()


func _on_create_pressed() -> void:
	request_create_session()


func _on_save_note_pressed() -> void:
	request_save_note()


func _on_add_bookmark_pressed() -> void:
	request_add_bookmark_at_playback()


func _on_seek_bookmark_pressed() -> void:
	var index := _bookmark_option.selected
	request_seek_saved_bookmark(index)


func _on_status_option_selected(index: int) -> void:
	if _ui_syncing:
		return
	if index < 0 or index >= STATUS_OPTIONS.size():
		return
	var status: String = STATUS_OPTIONS[index]
	if not _mutations_enabled():
		return
	request_set_clip_status(status)


func _on_note_text_changed(_text: String) -> void:
	if _ui_syncing:
		return
	_note_dirty = true
	_refresh_session_controls()


# --- Session model / annotations ---


func _stored_revision() -> int:
	if _stored_session == null or typeof(_stored_session) != TYPE_DICTIONARY:
		return -1
	return _json_finite_integral_nonneg(_stored_session.get("revision", null))


func _mutations_enabled() -> bool:
	return (
		_bridge_configured
		and not _bridge_busy
		and _authority_current
		and not _reload_required
		and not _conflict_active
		and _stored_session != null
	)


func _apply_clip_annotations_for(clip_id: String) -> void:
	_ui_syncing = true
	var record := _clip_record_for(clip_id)
	var status := "unreviewed"
	var note := ""
	var bookmarks: Array = []
	if not record.is_empty():
		if typeof(record.get("status", "")) == TYPE_STRING:
			status = record.get("status")
		if typeof(record.get("note", "")) == TYPE_STRING:
			note = record.get("note")
		var raw_bookmarks: Variant = record.get("bookmarks", [])
		if typeof(raw_bookmarks) == TYPE_ARRAY:
			bookmarks = raw_bookmarks.duplicate()
	if _draft_notes.has(clip_id):
		note = str(_draft_notes[clip_id])
		_note_dirty = true
	else:
		_note_dirty = false
	_status_option.select(_status_index(status))
	_note_field.text = note
	_populate_bookmark_selector(bookmarks)
	_ui_syncing = false
	_refresh_session_controls()


func _commit_note_draft_for_clip(clip_id: String) -> void:
	if clip_id.is_empty():
		return
	if _note_dirty:
		_draft_notes[clip_id] = _note_field.text


func _clip_record_for(clip_id: String) -> Dictionary:
	if _stored_session == null or typeof(_stored_session) != TYPE_DICTIONARY:
		return {}
	var records: Variant = _stored_session.get("clip_records", [])
	if typeof(records) != TYPE_ARRAY:
		return {}
	for entry in records:
		if typeof(entry) != TYPE_DICTIONARY:
			continue
		if entry.get("clip_id", "") == clip_id:
			return entry
	return {}


func _clip_duration_seconds(clip_id: String) -> float:
	if _stored_session == null or typeof(_stored_session) != TYPE_DICTIONARY:
		if _playback_ready():
			return _playback.review_duration_seconds()
		return 0.0
	var binding: Variant = _stored_session.get("binding", {})
	if typeof(binding) != TYPE_DICTIONARY:
		return 0.0
	var clips: Variant = binding.get("clips", [])
	if typeof(clips) != TYPE_ARRAY:
		return 0.0
	for clip in clips:
		if typeof(clip) != TYPE_DICTIONARY:
			continue
		if clip.get("clip_id", "") == clip_id:
			var duration: Variant = clip.get("duration_seconds", null)
			if typeof(duration) == TYPE_FLOAT or typeof(duration) == TYPE_INT:
				return float(duration)
	return 0.0


func _populate_bookmark_selector(bookmarks: Array) -> void:
	_bookmark_option.clear()
	var sorted := bookmarks.duplicate()
	sorted.sort()
	for ts in sorted:
		var t := float(ts)
		var idx := _bookmark_option.item_count
		_bookmark_option.add_item("%.3fs" % t)
		_bookmark_option.set_item_metadata(idx, t)
	if _bookmark_option.item_count > 0:
		_bookmark_option.select(0)


func _status_index(status: String) -> int:
	var idx := STATUS_OPTIONS.find(status)
	return idx if idx >= 0 else 0


func _note_within_limit(note: String) -> bool:
	return note.to_utf8_buffer().size() <= MAX_NOTE_UTF8_BYTES


func _bookmark_timestamp_valid(clip_id: String, timestamp: float) -> bool:
	if not is_finite(timestamp):
		return false
	var duration := _clip_duration_seconds(clip_id)
	if duration <= 0.0:
		return false
	return timestamp >= 0.0 and timestamp <= duration


# --- Bridge ---


func _enqueue_bridge_action(action: String) -> Dictionary:
	if not _bridge_configured:
		return {"ok": false, "error_code": "bridge_unavailable"}
	if _bridge_busy:
		return {"ok": false, "error_code": "bridge_busy"}
	_inflight_clip_id = ""
	_inflight_submitted_note = ""
	_inflight_update_op = ""
	var body := {"schema_version": BRIDGE_SCHEMA_VERSION, "action": action}
	return _start_bridge_request(action, body)


func _enqueue_bridge_update(
	operation: Dictionary, clip_id: String, submitted_note: String
) -> Dictionary:
	if not _bridge_configured:
		return {"ok": false, "error_code": "bridge_unavailable"}
	if _bridge_busy:
		return {"ok": false, "error_code": "bridge_busy"}
	if _raw_sha256.is_empty():
		return {"ok": false, "error_code": "missing_raw_sha256"}
	_inflight_clip_id = clip_id
	_inflight_submitted_note = submitted_note
	_inflight_update_op = str(operation.get("op", ""))
	var body := {
		"schema_version": BRIDGE_SCHEMA_VERSION,
		"action": "update",
		"expected_raw_sha256": _raw_sha256,
		"operation": operation,
	}
	return _start_bridge_request("update", body)


func _start_bridge_request(action: String, body: Dictionary) -> Dictionary:
	_request_serial += 1
	var token := "%d-%08x-%d" % [
		OS.get_process_id(),
		_bridge_rng.randi(),
		_request_serial,
	]
	var request_path := _exchange_dir.path_join("request-%s.json" % token)
	var response_path := _exchange_dir.path_join("response-%s.json" % token)
	if FileAccess.file_exists(request_path) or FileAccess.file_exists(response_path):
		return {"ok": false, "error_code": "exchange_collision"}
	var encoded := JSON.stringify(body)
	if encoded.is_empty() and not body.is_empty():
		return {"ok": false, "error_code": "request_encode_failed"}
	var request_file := FileAccess.open(request_path, FileAccess.WRITE)
	if request_file == null:
		return {"ok": false, "error_code": "request_write_failed"}
	request_file.store_string(encoded)
	request_file.close()

	var args := PackedStringArray(
		[
			"-m",
			"gamefactory.cli.animation_review_session_bridge",
			"--context-file",
			_context_file,
			"--request-file",
			request_path,
			"--response-file",
			response_path,
		]
	)
	var pid := OS.create_process(_python_executable, args, false)
	if pid < 0:
		DirAccess.remove_absolute(request_path)
		return {"ok": false, "error_code": "process_start_failed"}

	_inflight_pid = pid
	_inflight_request_path = request_path
	_inflight_response_path = response_path
	_inflight_started_ms = Time.get_ticks_msec()
	_inflight_action = action
	_inflight_owns_exchange_files = true
	_bridge_busy = true
	_refresh_session_controls()
	return {"ok": true, "pending": true}


func _finish_inflight_bridge() -> void:
	var response_path := _inflight_response_path
	var request_path := _inflight_request_path
	var action := _inflight_action
	var owns := _inflight_owns_exchange_files
	var pending_clip := _inflight_clip_id
	var pending_note := _inflight_submitted_note
	var pending_op := _inflight_update_op
	_clear_inflight_process_state()
	var raw := _read_bounded_text(response_path, MAX_RESPONSE_BYTES)
	if owns:
		_cleanup_owned_exchange_files(request_path, response_path)
	if raw.is_empty():
		_handle_bridge_failure(
			"Session bridge returned no response. Reload to confirm session state."
		)
		return
	var parsed: Variant = JSON.parse_string(raw)
	if typeof(parsed) != TYPE_DICTIONARY:
		_handle_bridge_failure("Session bridge response was invalid. Reload required.")
		return
	_apply_bridge_response(action, parsed, pending_clip, pending_note, pending_op)
	_inflight_clip_id = ""
	_inflight_submitted_note = ""
	_inflight_update_op = ""


func _abort_inflight_bridge(message: String) -> void:
	if _inflight_pid >= 0 and OS.is_process_running(_inflight_pid):
		OS.kill(_inflight_pid)
	var request_path := _inflight_request_path
	var response_path := _inflight_response_path
	var owns := _inflight_owns_exchange_files
	_clear_inflight_process_state()
	if owns:
		_cleanup_owned_exchange_files(request_path, response_path)
	_inflight_clip_id = ""
	_inflight_submitted_note = ""
	_inflight_update_op = ""
	_handle_bridge_failure(message)


func _clear_inflight_process_state() -> void:
	_inflight_pid = -1
	_inflight_request_path = ""
	_inflight_response_path = ""
	_inflight_action = ""
	_inflight_owns_exchange_files = false
	_bridge_busy = false


func _cleanup_owned_exchange_files(request_path: String, response_path: String) -> void:
	if not request_path.is_empty() and FileAccess.file_exists(request_path):
		DirAccess.remove_absolute(request_path)
	if not response_path.is_empty() and FileAccess.file_exists(response_path):
		DirAccess.remove_absolute(response_path)


func _read_bounded_text(path: String, max_bytes: int) -> String:
	if not FileAccess.file_exists(path):
		return ""
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return ""
	var data := file.get_buffer(max_bytes + 1)
	if data.size() > max_bytes:
		return ""
	return data.get_string_from_utf8()


func _apply_bridge_response(
	action: String,
	response: Dictionary,
	pending_clip: String,
	pending_note: String,
	pending_op: String,
) -> void:
	var envelope := _parse_bridge_response_envelope(response)
	if not envelope.get("ok", false):
		_invalidate_bridge_authority(envelope.get("message", "Session response invalid. Reload required."))
		_refresh_session_controls()
		return

	var ok_flag: bool = envelope.get("ok_flag")
	var committed: bool = envelope.get("committed")
	var current: bool = envelope.get("current")
	var stored: Variant = envelope.get("stored")
	var error_obj: Variant = envelope.get("error")

	if not _validate_bridge_action_semantics(action, ok_flag, committed, stored):
		_invalidate_bridge_authority("Session response did not match the requested action. Reload required.")
		_refresh_session_controls()
		return

	_authority_current = current

	if not ok_flag:
		_handle_bridge_error_response(action, committed, current, error_obj)
		_refresh_session_controls()
		return

	if committed:
		if stored == null:
			_invalidate_bridge_authority("Committed response missing stored session. Reload required.")
			_refresh_session_controls()
			return
		var parsed_stored := _parse_stored_presentation(stored)
		if not parsed_stored.get("ok", false):
			_invalidate_bridge_authority(
				"Stored session payload was invalid. Reload required before editing."
			)
			_refresh_session_controls()
			return
		_stored_session = parsed_stored.get("session")
		_raw_sha256 = parsed_stored.get("raw_sha256")
		_conflict_active = false
		if committed and not current:
			_reload_required = true
			_user_status_message = (
				"Changes were saved on a historical revision. Reload when current to edit again."
			)
		elif current:
			if not (committed and action == "update"):
				_reload_required = false
			if committed and action == "update" and pending_op == "SetNote":
				_maybe_clear_note_draft_after_save(pending_clip, pending_note)
			if action == "create":
				_user_status_message = "Review session created."
			else:
				_user_status_message = ""
			_apply_clip_annotations_for(_tracked_clip_id)
	elif stored == null:
		if action == "read":
			_stored_session = null
			_raw_sha256 = ""
			_reload_required = false
			_conflict_active = false
			if current:
				_user_status_message = "No review session on disk. Create one when ready."
			else:
				_user_status_message = (
					"Review set changed; stored session is stale (read-only). "
					+ "Reload when the review set matches authority."
				)
			_apply_clip_annotations_for(_tracked_clip_id)
		else:
			_invalidate_bridge_authority("Session response missing stored payload. Reload required.")
			_refresh_session_controls()
			return
	else:
		var parsed_stored := _parse_stored_presentation(stored)
		if not parsed_stored.get("ok", false):
			_invalidate_bridge_authority(
				"Stored session payload was invalid. Reload required before editing."
			)
			_refresh_session_controls()
			return
		_stored_session = parsed_stored.get("session")
		_raw_sha256 = parsed_stored.get("raw_sha256")
		_conflict_active = false
		if action == "read" and current:
			_reload_required = false
			_user_status_message = ""
		elif action == "read" and not current:
			_user_status_message = "Loaded historical session (read-only)."
		elif action == "create" and current:
			_user_status_message = "Review session created."
			_reload_required = false
		_apply_clip_annotations_for(_tracked_clip_id)

	_refresh_session_controls()


func _maybe_clear_note_draft_after_save(pending_clip: String, pending_note: String) -> void:
	var draft_text := _pending_clip_note_draft_text(pending_clip)
	if not _should_clear_note_draft_on_save(
		"SetNote", pending_clip, draft_text, pending_note, true
	):
		return
	_draft_notes.erase(pending_clip)
	if _tracked_clip_id == pending_clip:
		_note_dirty = false


func _pending_clip_note_draft_text(pending_clip: String) -> String:
	if pending_clip.is_empty():
		return ""
	if pending_clip == _tracked_clip_id:
		return _note_field.text
	if _draft_notes.has(pending_clip):
		return str(_draft_notes[pending_clip])
	var record := _clip_record_for(pending_clip)
	if typeof(record.get("note", "")) == TYPE_STRING:
		return record.get("note")
	return ""


func _invalidate_bridge_authority(message: String) -> void:
	_authority_current = false
	_reload_required = true
	_conflict_active = true
	_user_status_message = _sanitize_user_message(message)


func _handle_bridge_error_response(
	action: String, committed: bool, current: bool, error_obj: Variant
) -> void:
	var code := ""
	var message := "Session update failed."
	if typeof(error_obj) == TYPE_DICTIONARY:
		if typeof(error_obj.get("code", "")) == TYPE_STRING:
			code = error_obj.get("code")
		if typeof(error_obj.get("message", "")) == TYPE_STRING:
			message = error_obj.get("message")
	_authority_current = current
	if not committed:
		_reload_required = true
		_conflict_active = true
	var user_msg := _user_facing_bridge_error(code, message)
	if not committed and not user_msg.ends_with("Reload before retrying."):
		user_msg = "%s Reload before retrying." % user_msg
	_user_status_message = user_msg
	if action == "update" and not committed:
		_note_dirty = true


func _user_facing_bridge_error(code: String, message: String) -> String:
	match code:
		"note_too_long", "VALIDATION_NOTE":
			return (
				"Note exceeds the %d-byte limit. Shorten it and reload if needed."
				% MAX_NOTE_UTF8_BYTES
			)
		"ANIMATION_REVIEW_SESSION_STORE_CONFLICT", "session_conflict", "conflict":
			return "Session changed elsewhere. Reload before editing."
		"authority_not_current":
			return "Session is read-only until authority is current. Reload when ready."
		_:
			var sanitized := _sanitize_user_message(message)
			if sanitized.is_empty():
				return "Session update failed. Reload before retrying."
			return sanitized


func _handle_bridge_failure(message: String) -> void:
	_reload_required = true
	_conflict_active = true
	_user_status_message = _sanitize_user_message(message)
	_refresh_session_controls()


func _refresh_session_controls() -> void:
	var mutations := _mutations_enabled()
	_reload_button.disabled = not _bridge_configured or _bridge_busy
	_create_button.disabled = (
		not _bridge_configured
		or _bridge_busy
		or _reload_required
		or _conflict_active
		or not _authority_current
		or _stored_session != null
	)
	_status_option.disabled = not mutations
	_note_field.editable = mutations
	_save_note_button.disabled = not mutations
	_add_bookmark_button.disabled = not mutations
	_seek_bookmark_button.disabled = _bookmark_option.item_count == 0 or not _playback_ready()

	var banner := _user_status_message
	if _bridge_busy:
		banner = "Session bridge busy…"
	elif banner.is_empty():
		if not _bridge_configured:
			banner = "Session bridge unavailable. Playback only."
		elif _stored_session == null and _authority_current:
			banner = "No session loaded."
		elif _mutations_enabled():
			banner = "Session current — annotations editable (Keep is a working note, not approval)."
		elif not _authority_current:
			banner = "Session read-only (authority not current)."
		elif _reload_required:
			banner = "Reload required before editing."
	_set_banner(banner)


func _set_banner(text: String) -> void:
	_status_banner.text = _sanitize_user_message(text)


# --- Bridge response validation (scene-independent helpers) ---


func _wants_validation_probe() -> bool:
	for arg in OS.get_cmdline_user_args():
		if str(arg) == ARG_VALIDATION_PROBE:
			return true
	return false


func _run_bridge_validation_probe() -> bool:
	var failures: Array[String] = []
	if _json_finite_integral_nonneg(0) != 0:
		failures.append("revision int 0")
	if _json_finite_integral_nonneg(4.0) != 4:
		failures.append("revision float 4")
	if _json_finite_integral_nonneg(4.5) != -1:
		failures.append("revision non-integral")
	if _json_finite_integral_nonneg(true) != -1:
		failures.append("revision bool rejected")
	var session := _minimal_valid_session_dict(0)
	var stored := {"session": session, "raw_sha256": "a".repeat(64)}
	if not _parse_stored_presentation(stored).get("ok", false):
		failures.append("stored presentation")
	var bad_sha := {"session": session, "raw_sha256": "UPPER"}
	if _parse_stored_presentation(bad_sha).get("ok", false):
		failures.append("sha case")
	var read_success := {
		"schema_version": BRIDGE_SCHEMA_VERSION,
		"ok": true,
		"committed": false,
		"current": true,
		"stored": stored,
		"error": null,
	}
	if not _parse_bridge_response_envelope(read_success).get("ok", false):
		failures.append("envelope read success")
	var stale_read := {
		"schema_version": BRIDGE_SCHEMA_VERSION,
		"ok": true,
		"committed": false,
		"current": false,
		"stored": null,
		"error": null,
	}
	if not _parse_bridge_response_envelope(stale_read).get("ok", false):
		failures.append("envelope stale read")
	var update_success := {
		"schema_version": BRIDGE_SCHEMA_VERSION,
		"ok": true,
		"committed": true,
		"current": true,
		"stored": stored,
		"error": null,
	}
	if not _parse_bridge_response_envelope(update_success).get("ok", false):
		failures.append("envelope update success")
	if _validate_bridge_action_semantics("update", true, false, null):
		failures.append("update ok uncommitted rejected")
	if not _validate_bridge_action_semantics("update", true, true, stored):
		failures.append("update ok committed requires stored")
	if _validate_bridge_action_semantics("create", true, false, stored):
		failures.append("create ok uncommitted rejected")
	if not _validate_bridge_action_semantics("create", true, true, stored):
		failures.append("create ok committed requires stored")
	if not _validate_bridge_action_semantics("read", true, false, stored):
		failures.append("read ok uncommitted with stored")
	if _validate_bridge_action_semantics("read", true, true, stored):
		failures.append("read ok committed rejected")
	var bridge_failure := {
		"schema_version": BRIDGE_SCHEMA_VERSION,
		"ok": false,
		"committed": false,
		"current": false,
		"stored": null,
		"error": {"code": "session_conflict", "message": "conflict"},
	}
	if not _parse_bridge_response_envelope(bridge_failure).get("ok", false):
		failures.append("envelope failure parse")
	elif _parse_bridge_response_envelope(bridge_failure).get("ok_flag", true):
		failures.append("envelope failure ok_flag")
	var invented_candidate := read_success.duplicate()
	invented_candidate["candidate_state"] = "CLOSED"
	if _parse_bridge_response_envelope(invented_candidate).get("ok", false):
		failures.append("reject candidate_state key")
	var success_with_error_object := read_success.duplicate()
	success_with_error_object["error"] = {"code": "x", "message": "y"}
	if _parse_bridge_response_envelope(success_with_error_object).get("ok", false):
		failures.append("reject success error object")
	var failure_missing_error := bridge_failure.duplicate()
	failure_missing_error.erase("error")
	if _parse_bridge_response_envelope(failure_missing_error).get("ok", false):
		failures.append("reject missing error key")
	if not _should_clear_note_draft_on_save("SetNote", "A", "A", "A", true):
		failures.append("draft clear match")
	if _should_clear_note_draft_on_save("SetNote", "A", "B", "A", true):
		failures.append("draft keep mismatch")
	if _should_clear_note_draft_on_save("SetNote", "A", "newer", "old", true):
		failures.append("draft keep note text mismatch")
	if _should_clear_note_draft_on_save("SetStatus", "A", "A", "A", true):
		failures.append("draft keep status")
	_draft_notes = {"ClipA": "submitted-text"}
	_tracked_clip_id = "ClipB"
	_ui_syncing = true
	_note_field.text = "submitted-text"
	_ui_syncing = false
	if _pending_clip_note_draft_text("ClipA") != "submitted-text":
		failures.append("pending draft uses map not selected field")
	if not _should_clear_note_draft_on_save(
		"SetNote",
		"ClipA",
		_pending_clip_note_draft_text("ClipA"),
		"submitted-text",
		true,
	):
		failures.append("draft clear when pending draft matches submitted")
	_draft_notes = {"ClipA": "edited-after-submit"}
	_note_field.text = "submitted-text"
	if _should_clear_note_draft_on_save(
		"SetNote",
		"ClipA",
		_pending_clip_note_draft_text("ClipA"),
		"submitted-text",
		true,
	):
		failures.append("draft keep when pending draft differs from submitted")
	if failures.is_empty():
		print("PASS: animation_review_session_bridge_validation")
		return true
	for item in failures:
		print("FAIL: %s" % item)
	return false


static func _should_clear_note_draft_on_save(
	op: String,
	pending_clip: String,
	field_text: String,
	submitted_note: String,
	committed: bool,
) -> bool:
	if not committed or op != "SetNote" or pending_clip.is_empty():
		return false
	return field_text == submitted_note


func _parse_bridge_response_envelope(response: Dictionary) -> Dictionary:
	if not _dict_has_exact_keys(response, _RESPONSE_EXACT_KEYS):
		return {"ok": false, "message": "Session response has unexpected fields."}
	if response.get("schema_version", "") != BRIDGE_SCHEMA_VERSION:
		return {"ok": false, "message": "Session bridge schema mismatch."}
	if typeof(response.get("ok")) != TYPE_BOOL:
		return {"ok": false, "message": "Session response ok flag must be a boolean."}
	if typeof(response.get("committed")) != TYPE_BOOL:
		return {"ok": false, "message": "Session response committed flag must be a boolean."}
	if typeof(response.get("current")) != TYPE_BOOL:
		return {"ok": false, "message": "Session response current flag must be a boolean."}
	var ok_flag: bool = response.get("ok")
	var committed: bool = response.get("committed")
	var current: bool = response.get("current")
	var stored: Variant = response.get("stored")
	if stored != null and typeof(stored) != TYPE_DICTIONARY:
		return {"ok": false, "message": "Session stored payload must be an object or null."}
	var error_obj: Variant = response.get("error")
	if ok_flag:
		if error_obj != null:
			return {"ok": false, "message": "Successful response must have error null."}
	else:
		if committed or current:
			return {"ok": false, "message": "Failed response must have committed and current false."}
		if typeof(error_obj) != TYPE_DICTIONARY:
			return {"ok": false, "message": "Session error payload must be an object."}
		if typeof(error_obj.get("code", "")) != TYPE_STRING:
			return {"ok": false, "message": "Session error code must be a string."}
		if typeof(error_obj.get("message", "")) != TYPE_STRING:
			return {"ok": false, "message": "Session error message must be a string."}
		if not _dict_has_exact_keys(error_obj, ["code", "message"]):
			return {"ok": false, "message": "Session error payload has unexpected fields."}
	return {
		"ok": true,
		"ok_flag": ok_flag,
		"committed": committed,
		"current": current,
		"stored": stored,
		"error": error_obj,
	}


func _validate_bridge_action_semantics(
	action: String, ok_flag: bool, committed: bool, stored: Variant
) -> bool:
	if not ok_flag:
		return true
	match action:
		"read":
			return not committed
		"create", "update":
			return committed and stored != null
	return false


func _parse_stored_presentation(stored: Variant) -> Dictionary:
	if typeof(stored) != TYPE_DICTIONARY:
		return {"ok": false}
	if not _dict_has_exact_keys(stored, _STORED_KEYS):
		return {"ok": false}
	var digest: Variant = stored.get("raw_sha256")
	if not _is_lowercase_sha256(digest):
		return {"ok": false}
	var session: Variant = stored.get("session")
	if typeof(session) != TYPE_DICTIONARY:
		return {"ok": false}
	if not _validate_session_document(session):
		return {"ok": false}
	return {"ok": true, "session": session, "raw_sha256": digest}


func _validate_session_document(session: Dictionary) -> bool:
	if not _dict_has_exact_keys(session, _SESSION_KEYS):
		return false
	if session.get("schema_version", "") != SESSION_SCHEMA_VERSION:
		return false
	if _json_finite_integral_nonneg(session.get("revision", null)) < 0:
		return false
	if typeof(session.get("production_eligible")) != TYPE_BOOL:
		return false
	if session.get("production_eligible") != false:
		return false
	if typeof(session.get("promotion_eligible")) != TYPE_BOOL:
		return false
	if session.get("promotion_eligible") != false:
		return false
	var binding: Variant = session.get("binding")
	if not _validate_binding(binding):
		return false
	return _validate_clip_records(session.get("clip_records"), binding)


func _validate_binding(binding: Variant) -> bool:
	if typeof(binding) != TYPE_DICTIONARY:
		return false
	if not _dict_has_exact_keys(binding, _BINDING_KEYS):
		return false
	if not _is_non_empty_string(binding.get("review_root")):
		return false
	for key in ["raw_manifest_sha256", "root_payload_sha256", "clip_payload_sha256"]:
		if not _is_lowercase_sha256(binding.get(key)):
			return false
	var clips: Variant = binding.get("clips")
	if typeof(clips) != TYPE_ARRAY:
		return false
	if clips.size() < MIN_BINDING_CLIPS or clips.size() > MAX_BINDING_CLIPS:
		return false
	var seen: Dictionary = {}
	for clip in clips:
		if typeof(clip) != TYPE_DICTIONARY:
			return false
		if not _dict_has_exact_keys(clip, _CLIP_BINDING_KEYS):
			return false
		var clip_id: Variant = clip.get("clip_id")
		if not _is_clip_id(clip_id):
			return false
		if seen.has(clip_id):
			return false
		seen[clip_id] = true
		if not _is_lowercase_sha256(clip.get("raw_clip_sha256")):
			return false
		if not _is_valid_duration(clip.get("duration_seconds")):
			return false
	return true


func _validate_clip_records(records: Variant, binding: Variant) -> bool:
	if typeof(records) != TYPE_ARRAY or typeof(binding) != TYPE_DICTIONARY:
		return false
	var clips: Array = binding.get("clips")
	if records.size() != clips.size():
		return false
	for index in range(records.size()):
		var record: Variant = records[index]
		if typeof(record) != TYPE_DICTIONARY:
			return false
		if not _dict_has_exact_keys(record, _RECORD_KEYS):
			return false
		var expected_id: Variant = clips[index].get("clip_id")
		if record.get("clip_id") != expected_id:
			return false
		var status: Variant = record.get("status")
		if typeof(status) != TYPE_STRING or not STATUS_OPTIONS.has(status):
			return false
		var note: Variant = record.get("note")
		if typeof(note) != TYPE_STRING:
			return false
		if note.to_utf8_buffer().size() > MAX_NOTE_UTF8_BYTES:
			return false
		var duration := _duration_from_binding_clip(clips[index])
		if not _validate_bookmarks_array(record.get("bookmarks"), duration):
			return false
	return true


func _duration_from_binding_clip(clip: Dictionary) -> float:
	var duration: Variant = clip.get("duration_seconds")
	if typeof(duration) == TYPE_INT or typeof(duration) == TYPE_FLOAT:
		return float(duration)
	return -1.0


func _validate_bookmarks_array(bookmarks: Variant, duration: float) -> bool:
	if typeof(bookmarks) != TYPE_ARRAY:
		return false
	if bookmarks.size() > MAX_BOOKMARKS_PER_CLIP:
		return false
	var seen: Dictionary = {}
	for entry in bookmarks:
		if typeof(entry) != TYPE_FLOAT and typeof(entry) != TYPE_INT:
			return false
		var ts := float(entry)
		if not is_finite(ts) or ts < 0.0 or ts > duration:
			return false
		if seen.has(ts):
			return false
		seen[ts] = true
	return true


func _dict_has_exact_keys(data: Dictionary, allowed: Array) -> bool:
	if data.size() != allowed.size():
		return false
	for key in allowed:
		if not data.has(key):
			return false
	for key in data.keys():
		if not allowed.has(key):
			return false
	return true


func _dict_has_allowed_keys(data: Dictionary, required: Array, optional: Array) -> bool:
	for key in required:
		if not data.has(key):
			return false
	for key in data.keys():
		if not required.has(key) and not optional.has(key):
			return false
	return true


func _json_finite_integral_nonneg(value: Variant) -> int:
	if typeof(value) == TYPE_BOOL:
		return -1
	if typeof(value) == TYPE_INT:
		if value < 0:
			return -1
		return value
	if typeof(value) == TYPE_FLOAT:
		if not is_finite(value):
			return -1
		if value != floor(value):
			return -1
		if value < 0.0:
			return -1
		return int(value)
	return -1


func _is_lowercase_sha256(value: Variant) -> bool:
	if typeof(value) != TYPE_STRING:
		return false
	if value.length() != 64:
		return false
	for i in range(64):
		var code: int = value.unicode_at(i)
		var is_digit: bool = code >= 48 and code <= 57
		var is_hex: bool = code >= 97 and code <= 102
		if not is_digit and not is_hex:
			return false
	return true


func _is_non_empty_string(value: Variant) -> bool:
	return typeof(value) == TYPE_STRING and not value.is_empty()


func _is_clip_id(value: Variant) -> bool:
	if typeof(value) != TYPE_STRING or value.is_empty():
		return false
	var first: int = value.unicode_at(0)
	if not ((first >= 65 and first <= 90) or (first >= 97 and first <= 122)):
		return false
	for i in range(1, value.length()):
		var code: int = value.unicode_at(i)
		var ok: bool = (
			(code >= 48 and code <= 57)
			or (code >= 65 and code <= 90)
			or (code >= 97 and code <= 122)
			or code == 95
		)
		if not ok:
			return false
	return value.length() <= 64


func _is_valid_duration(value: Variant) -> bool:
	if typeof(value) == TYPE_BOOL:
		return false
	if typeof(value) != TYPE_INT and typeof(value) != TYPE_FLOAT:
		return false
	var duration := float(value)
	return is_finite(duration) and duration > 0.0 and duration <= MAX_DURATION_SECONDS


func _sanitize_user_message(message: String) -> String:
	if message.is_empty():
		return message
	var parts := message.split(" ")
	for part in parts:
		if part.contains(":\\") or part.begins_with("/") or part.contains("res://"):
			return "Session operation failed. Reload required."
		if part.ends_with(".py") or part.ends_with(".json") or part.ends_with(".gd"):
			return "Session operation failed. Reload required."
	return message


func _minimal_valid_session_dict(revision: int) -> Dictionary:
	var clip_a := {
		"clip_id": "IdleA",
		"raw_clip_sha256": "b".repeat(64),
		"duration_seconds": 1.0,
	}
	var clip_b := {
		"clip_id": "IdleB",
		"raw_clip_sha256": "c".repeat(64),
		"duration_seconds": 1.0,
	}
	return {
		"schema_version": SESSION_SCHEMA_VERSION,
		"binding": {
			"review_root": "/review/root",
			"raw_manifest_sha256": "d".repeat(64),
			"root_payload_sha256": "e".repeat(64),
			"clip_payload_sha256": "f".repeat(64),
			"clips": [clip_a, clip_b],
		},
		"revision": revision,
		"clip_records": [
			{"clip_id": "IdleA", "status": "unreviewed", "note": "", "bookmarks": []},
			{"clip_id": "IdleB", "status": "unreviewed", "note": "", "bookmarks": []},
		],
		"production_eligible": false,
		"promotion_eligible": false,
	}
