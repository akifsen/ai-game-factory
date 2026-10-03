extends Control

## V0.8-12a synchronized side-by-side animation compare controller.

const COMPARE_SCHEMA_VERSION := "animation-review-compare-0.8.0"
const ARG_COMPARE_CONTEXT := "--compare-context-file="
const ALLOWED_SPEEDS: Array[float] = [0.25, 0.5, 1.0, 2.0]
const TIME_EPS := 0.0001
const MAX_CONTEXT_BYTES := 8192
const _CONTEXT_KEYS := ["schema_version", "left_clip_id", "right_clip_id"]

@onready var _left_side: Node3D = %LeftSide
@onready var _right_side: Node3D = %RightSide
@onready var _left_option: OptionButton = %LeftClipOption
@onready var _right_option: OptionButton = %RightClipOption
@onready var _left_time_label: Label = %LeftTimeLabel
@onready var _right_time_label: Label = %RightTimeLabel
@onready var _normalized_label: Label = %NormalizedTimeLabel
@onready var _play_button: Button = %PlayButton
@onready var _pause_button: Button = %PauseButton
@onready var _restart_button: Button = %RestartButton
@onready var _seek_slider: HSlider = %SeekSlider
@onready var _speed_option: OptionButton = %SpeedOption
@onready var _readiness_label: Label = %ReadinessLabel

var _left_clip_id: String = ""
var _right_clip_id: String = ""
var _normalized_progress: float = 0.0
var _shared_speed: float = 1.0
var _playing: bool = false
var _reference_duration: float = 0.0
var _ready_for_compare: bool = false
var _ui_syncing: bool = false
var _slider_dragging: bool = false
var _ordered_clip_ids: PackedStringArray = PackedStringArray()


func _ready() -> void:
	_wire_ui()
	_speed_option.clear()
	for speed in ALLOWED_SPEEDS:
		_speed_option.add_item("%sx" % _format_speed_label(speed), int(speed * 1000.0))
	_speed_option.select(2)
	_shared_speed = 1.0
	_seek_slider.min_value = 0.0
	_seek_slider.max_value = 1.0
	_seek_slider.step = 0.0
	call_deferred("_bootstrap_compare")


func _wire_ui() -> void:
	_play_button.pressed.connect(_on_play_pressed)
	_pause_button.pressed.connect(_on_pause_pressed)
	_restart_button.pressed.connect(_on_restart_pressed)
	_speed_option.item_selected.connect(_on_speed_selected)
	_seek_slider.value_changed.connect(_on_seek_slider_changed)
	_seek_slider.drag_started.connect(func() -> void:
		_slider_dragging = true
	)
	_seek_slider.drag_ended.connect(func(_value_changed: bool) -> void:
		_slider_dragging = false
	)
	_left_option.item_selected.connect(_on_left_option_selected)
	_right_option.item_selected.connect(_on_right_option_selected)


func _bootstrap_compare() -> void:
	_set_controls_enabled(false)
	var context_error := _load_compare_context()
	if not context_error.is_empty():
		_show_readiness_failure(context_error)
		return
	var frames := 0
	while frames < 240:
		frames += 1
		if not _left_side.call("side_is_ready") or not _right_side.call("side_is_ready"):
			await get_tree().process_frame
			continue
		var left_ids := _left_side.call("side_source_clip_ids") as PackedStringArray
		var right_ids := _right_side.call("side_source_clip_ids") as PackedStringArray
		if left_ids.is_empty() or right_ids.is_empty() or left_ids.size() != right_ids.size():
			await get_tree().process_frame
			continue
		for i in range(left_ids.size()):
			if left_ids[i] != right_ids[i]:
				_show_readiness_failure("compare panes disagree on clip order")
				return
		_ordered_clip_ids = left_ids
		if not _ordered_clip_ids.has(_left_clip_id) or not _ordered_clip_ids.has(_right_clip_id):
			_show_readiness_failure("compare context clip ids are not in review set")
			return
		_populate_clip_selectors()
		if not _apply_initial_clips():
			_show_readiness_failure("compare failed to install initial clips")
			return
		_ready_for_compare = true
		_playing = false
		_normalized_progress = 0.0
		_apply_normalized_progress(0.0)
		_readiness_label.text = ""
		_sync_ui()
		_set_controls_enabled(true)
		return
	_show_readiness_failure("compare timed out waiting for panes")


func _load_compare_context() -> String:
	var context_path := ""
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with(ARG_COMPARE_CONTEXT):
			context_path = text.substr(ARG_COMPARE_CONTEXT.length())
			break
	return _load_compare_context_at_path(context_path)


func _load_compare_context_at_path(context_path: String) -> String:
	if context_path.is_empty():
		return "compare context path missing"
	if not FileAccess.file_exists(context_path):
		return "compare context file missing"
	var file := FileAccess.open(context_path, FileAccess.READ)
	if file == null:
		return "compare context file missing"
	var original_length := file.get_length()
	if original_length > MAX_CONTEXT_BYTES:
		file.close()
		return "compare context exceeds allowed size"
	var raw := file.get_buffer(MAX_CONTEXT_BYTES + 1)
	file.close()
	if raw.size() > MAX_CONTEXT_BYTES:
		return "compare context exceeds allowed size"
	if raw.size() != original_length:
		return "compare context exceeds allowed size"
	if raw.is_empty():
		return "compare context is empty"
	var parsed: Variant = JSON.parse_string(raw.get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		return "compare context is not an object"
	var doc: Dictionary = parsed
	if doc.size() != _CONTEXT_KEYS.size():
		return "compare context has unexpected fields"
	for key in _CONTEXT_KEYS:
		if not doc.has(key):
			return "compare context is missing required fields"
	if doc.get("schema_version") != COMPARE_SCHEMA_VERSION:
		return "compare context schema_version mismatch"
	var left: Variant = doc.get("left_clip_id")
	var right: Variant = doc.get("right_clip_id")
	if typeof(left) != TYPE_STRING or typeof(right) != TYPE_STRING:
		return "compare clip ids must be strings"
	if str(left).is_empty() or str(right).is_empty() or str(left) == str(right):
		return "compare clip ids must differ"
	_left_clip_id = str(left)
	_right_clip_id = str(right)
	return ""


func _show_readiness_failure(message: String) -> void:
	_ready_for_compare = false
	_set_controls_enabled(false)
	_readiness_label.text = message
	push_error(message)


func _populate_clip_selectors() -> void:
	_left_option.clear()
	_right_option.clear()
	for clip_id in _ordered_clip_ids:
		_left_option.add_item(clip_id)
		_right_option.add_item(clip_id)
	_set_option_index(_left_option, _ordered_clip_ids.find(_left_clip_id))
	_set_option_index(_right_option, _ordered_clip_ids.find(_right_clip_id))


func _apply_initial_clips() -> bool:
	if not _left_side.call("side_select_clip", _left_clip_id):
		return false
	if not _right_side.call("side_select_clip", _right_clip_id):
		return false
	_recompute_reference_duration()
	return _reference_duration > TIME_EPS


func _recompute_reference_duration() -> void:
	var left_d := float(_left_side.call("side_duration_seconds"))
	var right_d := float(_right_side.call("side_duration_seconds"))
	_reference_duration = maxf(left_d, right_d)


func _process(delta: float) -> void:
	if not _ready_for_compare or not _playing:
		return
	if _reference_duration <= TIME_EPS:
		return
	var dp := delta * _shared_speed / _reference_duration
	var next_p := _normalized_progress + dp
	if next_p >= 1.0:
		next_p = 1.0
		_playing = false
	_apply_normalized_progress(next_p)
	_sync_ui()


func _apply_normalized_progress(progress: float) -> void:
	_normalized_progress = clampf(progress, 0.0, 1.0)
	_left_side.call("side_sample_normalized", _normalized_progress)
	_right_side.call("side_sample_normalized", _normalized_progress)


func _sync_ui() -> void:
	_ui_syncing = true
	var left_pos := float(_left_side.call("side_position_seconds"))
	var right_pos := float(_right_side.call("side_position_seconds"))
	_left_time_label.text = "%.3fs" % left_pos
	_right_time_label.text = "%.3fs" % right_pos
	_normalized_label.text = "Time %.1f%%" % (_normalized_progress * 100.0)
	if not _slider_dragging:
		_seek_slider.set_value_no_signal(_normalized_progress)
	_ui_syncing = false


func _set_controls_enabled(enabled: bool) -> void:
	_play_button.disabled = not enabled
	_pause_button.disabled = not enabled
	_restart_button.disabled = not enabled
	_seek_slider.editable = enabled
	_seek_slider.mouse_filter = Control.MOUSE_FILTER_STOP if enabled else Control.MOUSE_FILTER_IGNORE
	_speed_option.disabled = not enabled
	_left_option.disabled = not enabled
	_right_option.disabled = not enabled


func _format_speed_label(speed: float) -> String:
	if is_equal_approx(speed, 1.0):
		return "1"
	if is_equal_approx(speed, 2.0):
		return "2"
	return str(speed)


func _speed_from_index(index: int) -> float:
	if index < 0 or index >= ALLOWED_SPEEDS.size():
		return -1.0
	return ALLOWED_SPEEDS[index]


func _index_for_speed(speed_f: float) -> int:
	for i in range(ALLOWED_SPEEDS.size()):
		if ALLOWED_SPEEDS[i] == speed_f:
			return i
	return -1


func _is_exact_allowed_speed(speed_f: float) -> bool:
	if not is_finite(speed_f):
		return false
	for candidate in ALLOWED_SPEEDS:
		if speed_f == candidate:
			return true
	return false


func _set_option_index(option: OptionButton, index: int) -> void:
	_ui_syncing = true
	option.set_block_signals(true)
	if index >= 0:
		option.select(index)
	option.set_block_signals(false)
	_ui_syncing = false


func _restore_side_clip_option(is_left: bool) -> void:
	var clip_id := _left_clip_id if is_left else _right_clip_id
	var index := _ordered_clip_ids.find(clip_id)
	_set_option_index(_left_option if is_left else _right_option, index)


func _on_play_pressed() -> void:
	request_play()


func _on_pause_pressed() -> void:
	request_pause()


func _on_restart_pressed() -> void:
	request_restart()


func _on_speed_selected(index: int) -> void:
	request_set_playback_speed(_speed_from_index(index))


func _on_seek_slider_changed(value: float) -> void:
	if _ui_syncing:
		return
	request_scrub_normalized(value)


func _on_left_option_selected(index: int) -> void:
	if _ui_syncing:
		return
	if index < 0 or index >= _ordered_clip_ids.size():
		return
	request_set_left_clip(_ordered_clip_ids[index])


func _on_right_option_selected(index: int) -> void:
	if _ui_syncing:
		return
	if index < 0 or index >= _ordered_clip_ids.size():
		return
	request_set_right_clip(_ordered_clip_ids[index])


func request_play() -> Dictionary:
	if not _ready_for_compare:
		return {"ok": false, "error_code": "not_ready"}
	if _normalized_progress >= 1.0 - TIME_EPS:
		return {"ok": false, "error_code": "at_end"}
	_playing = true
	return {"ok": true}


func request_pause() -> Dictionary:
	if not _ready_for_compare:
		return {"ok": false, "error_code": "not_ready"}
	_playing = false
	return {"ok": true}


func request_restart() -> Dictionary:
	if not _ready_for_compare:
		return {"ok": false, "error_code": "not_ready"}
	_playing = false
	_apply_normalized_progress(0.0)
	_sync_ui()
	return {"ok": true}


func request_scrub_normalized(progress: Variant) -> Dictionary:
	if not _ready_for_compare:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(progress) == TYPE_BOOL:
		return {"ok": false, "error_code": "invalid_progress"}
	if typeof(progress) != TYPE_FLOAT and typeof(progress) != TYPE_INT:
		return {"ok": false, "error_code": "invalid_progress"}
	var p := float(progress)
	if not is_finite(p):
		return {"ok": false, "error_code": "invalid_progress"}
	if p < 0.0 or p > 1.0:
		return {"ok": false, "error_code": "progress_out_of_range"}
	_playing = false
	_apply_normalized_progress(p)
	_sync_ui()
	return {"ok": true}


func request_set_playback_speed(speed: Variant) -> Dictionary:
	if not _ready_for_compare:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(speed) == TYPE_BOOL:
		return {"ok": false, "error_code": "invalid_playback_speed"}
	if typeof(speed) != TYPE_FLOAT and typeof(speed) != TYPE_INT:
		return {"ok": false, "error_code": "invalid_playback_speed"}
	var speed_f := float(speed)
	if not _is_exact_allowed_speed(speed_f):
		_restore_speed_option_from_shared()
		return {"ok": false, "error_code": "invalid_playback_speed"}
	_shared_speed = speed_f
	var speed_index := _index_for_speed(speed_f)
	if speed_index >= 0:
		_ui_syncing = true
		_speed_option.set_block_signals(true)
		_speed_option.select(speed_index)
		_speed_option.set_block_signals(false)
		_ui_syncing = false
	return {"ok": true}


func _restore_speed_option_from_shared() -> void:
	var speed_index := _index_for_speed(_shared_speed)
	if speed_index >= 0:
		_ui_syncing = true
		_speed_option.set_block_signals(true)
		_speed_option.select(speed_index)
		_speed_option.set_block_signals(false)
		_ui_syncing = false


func request_set_left_clip(clip_id: Variant) -> Dictionary:
	return _request_set_side_clip(clip_id, true)


func request_set_right_clip(clip_id: Variant) -> Dictionary:
	return _request_set_side_clip(clip_id, false)


func _request_set_side_clip(clip_id: Variant, is_left: bool) -> Dictionary:
	if not _ready_for_compare:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(clip_id) == TYPE_BOOL:
		_restore_side_clip_option(is_left)
		return {"ok": false, "error_code": "invalid_clip_id"}
	if typeof(clip_id) != TYPE_STRING:
		_restore_side_clip_option(is_left)
		return {"ok": false, "error_code": "invalid_clip_id"}
	var clip_key := str(clip_id)
	if clip_key.is_empty() or not _ordered_clip_ids.has(clip_key):
		_restore_side_clip_option(is_left)
		return {"ok": false, "error_code": "unknown_clip_id"}
	var other := _right_clip_id if is_left else _left_clip_id
	if clip_key == other:
		_restore_side_clip_option(is_left)
		return {"ok": false, "error_code": "duplicate_clip_id"}
	var side := _left_side if is_left else _right_side
	if not side.call("side_select_clip", clip_key):
		_restore_side_clip_option(is_left)
		return {"ok": false, "error_code": "clip_select_failed"}
	if is_left:
		_left_clip_id = clip_key
	else:
		_right_clip_id = clip_key
	_playing = false
	_recompute_reference_duration()
	_apply_normalized_progress(0.0)
	_set_option_index(_left_option if is_left else _right_option, _ordered_clip_ids.find(clip_key))
	_sync_ui()
	return {"ok": true}


func compare_snapshot() -> Dictionary:
	return {
		"ready": _ready_for_compare,
		"normalized_progress": _normalized_progress,
		"playing": _playing,
		"speed": _shared_speed,
		"left_clip_id": _left_clip_id,
		"right_clip_id": _right_clip_id,
		"left_duration": float(_left_side.call("side_duration_seconds")),
		"right_duration": float(_right_side.call("side_duration_seconds")),
		"reference_duration": _reference_duration,
		"left_position": float(_left_side.call("side_position_seconds")),
		"right_position": float(_right_side.call("side_position_seconds")),
	}
