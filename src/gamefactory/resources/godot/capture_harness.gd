extends SceneTree

const HARNESS_VERSION := "0.3.0"
const MAX_REQUEST_BYTES := 262144
const MAX_ACTIONS := 256
const MAX_SNAPSHOTS := 8
const MAX_TICKS := 10000
const WARMUP_FRAMES := 3
const CAPTURE_FRAMES := 2
const FRAME_WAIT_MSEC := 2000

var _request_path := ""
var _output_path := ""
var _capture_directory := ""
var _request: Dictionary = {}
var _scene_root: Node
var _snapshots: Array[Dictionary] = []
var _captures: Array[Dictionary] = []
var _failure_message := ""


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if not _parse_args(args):
		_fail("arguments must be exactly --request <path> --output <path> --captures <path>")
		return
	var request_file := FileAccess.open(_request_path, FileAccess.READ)
	if request_file == null:
		_fail("cannot open request file")
		return
	if request_file.get_length() > MAX_REQUEST_BYTES:
		_fail("request exceeds size limit")
		return
	var parsed: Variant = JSON.parse_string(request_file.get_as_text())
	request_file.close()
	if typeof(parsed) != TYPE_DICTIONARY:
		_fail("request root must be an object")
		return
	_request = parsed
	if not _validate_request():
		return
	if not _configure_viewport():
		return
	var packed: Resource = ResourceLoader.load(_request.scene)
	if packed == null or not packed is PackedScene:
		_fail("scene could not be loaded as a PackedScene")
		return
	_scene_root = (packed as PackedScene).instantiate()
	if _scene_root == null:
		_fail("scene could not be instantiated")
		return
	root.add_child(_scene_root)
	if not _has_scene_contract():
		_fail("scene must implement verification_snapshot, present_for_capture, apply_damage and defeat_enemy")
		return
	if not await _wait_frames(WARMUP_FRAMES):
		return
	if not await _sample(0):
		return
	var action_index := 0
	var actions: Array = _request.actions
	var requested_snapshots: Dictionary = {}
	for requested_tick in _request.snapshots:
		requested_snapshots[int(requested_tick)] = true
	for tick in range(1, int(_request.max_tick) + 1):
		await create_timer(0.0).timeout
		while action_index < actions.size() and int(actions[action_index].tick) == tick:
			var action: Dictionary = actions[action_index]
			if action.action == "apply_damage":
				_scene_root.call("apply_damage", int(action.amount))
			else:
				_scene_root.call("defeat_enemy")
			action_index += 1
		if requested_snapshots.has(tick):
			if not await _sample(tick):
				return
	if not _write_report():
		quit(1)
		return
	quit(0)


func _parse_args(args: PackedStringArray) -> bool:
	if args.size() != 6 or args[0] != "--request" or args[2] != "--output" or args[4] != "--captures":
		return false
	_request_path = args[1]
	_output_path = args[3]
	_capture_directory = args[5]
	return _is_absolute(_request_path) and _is_absolute(_output_path) and _is_absolute(_capture_directory)


func _is_absolute(path: String) -> bool:
	return path.is_absolute_path()


func _validate_request() -> bool:
	var keys := [
		"schema_version", "execution_id", "scenario_id", "scenario_sha256", "scene",
		"max_tick", "actions", "snapshots", "captures", "viewport_width",
		"viewport_height", "renderer_profile", "capture_directory",
	]
	if _request.size() != keys.size():
		_fail("request has missing or unknown fields")
		return false
	for key in keys:
		if not _request.has(key):
			_fail("request is missing field: " + key)
			return false
	if _request.schema_version != "0.3.0" or not _safe_id(_request.execution_id) or not _safe_id(_request.scenario_id):
		_fail("unsupported schema version or invalid identifier")
		return false
	if typeof(_request.scenario_sha256) != TYPE_STRING or _request.scenario_sha256.length() != 64 or not _request.scenario_sha256.is_valid_hex_number(false):
		_fail("invalid scenario fingerprint")
		return false
	if typeof(_request.scene) != TYPE_STRING or not _request.scene.begins_with("res://") or not _request.scene.ends_with(".tscn"):
		_fail("scene must be a res:// .tscn path")
		return false
	if _request.renderer_profile != "gl_compatibility":
		_fail("unsupported renderer profile")
		return false
	if _request.capture_directory != _capture_directory:
		_fail("capture directory does not match the invocation path")
		return false
	if not _is_integer_number(_request.viewport_width) or not _is_integer_number(_request.viewport_height):
		_fail("viewport size must be an integer pair")
		return false
	var width := int(_request.viewport_width)
	var height := int(_request.viewport_height)
	var allowed := (width == 1280 and height == 720) or (width == 720 and height == 1280)
	if not allowed:
		_fail("viewport profile is not supported")
		return false
	if not _is_integer_number(_request.max_tick) or _request.max_tick < 1 or _request.max_tick > MAX_TICKS:
		_fail("max_tick is outside the supported range")
		return false
	if typeof(_request.actions) != TYPE_ARRAY or _request.actions.size() > MAX_ACTIONS:
		_fail("actions must be a bounded array")
		return false
	var last_tick := 0
	for action in _request.actions:
		if typeof(action) != TYPE_DICTIONARY or action.size() < 2 or action.size() > 3:
			_fail("action must be a supported object")
			return false
		if not action.has("tick") or not action.has("action") or not _is_integer_number(action.tick) or action.tick < 1 or action.tick > _request.max_tick or action.tick < last_tick:
			_fail("action tick is invalid or unordered")
			return false
		last_tick = int(action.tick)
		if action.action == "apply_damage":
			if action.size() != 3 or not action.has("amount") or not _is_integer_number(action.amount) or action.amount < 1 or action.amount > 1000000:
				_fail("apply_damage requires a bounded positive integer amount")
				return false
		elif action.action == "defeat_enemy":
			if action.size() != 2 or action.has("amount"):
				_fail("defeat_enemy does not accept amount")
				return false
		else:
			_fail("unsupported action")
			return false
	if typeof(_request.snapshots) != TYPE_ARRAY or _request.snapshots.is_empty() or _request.snapshots.size() > MAX_SNAPSHOTS:
		_fail("snapshots must be a bounded nonempty array")
		return false
	var previous := -1
	for tick in _request.snapshots:
		if not _is_integer_number(tick) or tick < 0 or tick > _request.max_tick or tick <= previous:
			_fail("snapshot ticks must be strictly increasing and in range")
			return false
		previous = int(tick)
	if int(_request.snapshots[0]) != 0 or int(_request.snapshots[-1]) != int(_request.max_tick):
		_fail("snapshots must include tick zero and max_tick")
		return false
	if typeof(_request.captures) != TYPE_ARRAY or _request.captures.size() != _request.snapshots.size():
		_fail("captures must match the snapshot list")
		return false
	var seen := {}
	for index in _request.captures.size():
		var capture: Variant = _request.captures[index]
		if typeof(capture) != TYPE_DICTIONARY or capture.size() != 2 or not capture.has("id") or not capture.has("tick"):
			_fail("capture entry must contain only id and tick")
			return false
		if not _safe_id(capture.id) or seen.has(capture.id):
			_fail("capture id is invalid or duplicated")
			return false
		if not _is_integer_number(capture.tick) or int(capture.tick) != int(_request.snapshots[index]):
			_fail("capture tick does not match the snapshot order")
			return false
		seen[capture.id] = true
	return true


func _configure_viewport() -> bool:
	var size := Vector2i(int(_request.viewport_width), int(_request.viewport_height))
	root.size = size
	root.content_scale_mode = Window.CONTENT_SCALE_MODE_DISABLED
	root.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_IGNORE
	root.content_scale_size = size
	DisplayServer.window_set_mode(DisplayServer.WINDOW_MODE_WINDOWED)
	DisplayServer.window_set_size(size)
	var method := RenderingServer.get_current_rendering_method()
	var driver := RenderingServer.get_current_rendering_driver_name()
	var display_name := DisplayServer.get_name()
	if method != "gl_compatibility" or driver == "dummy" or display_name == "headless":
		_fail("rendered capture requires the gl_compatibility method on a real display, got method=%s driver=%s display=%s" % [method, driver, display_name])
		return false
	return true


func _safe_id(value: Variant) -> bool:
	if typeof(value) != TYPE_STRING or value.is_empty() or value.length() > 64:
		return false
	var regex := RegEx.new()
	regex.compile("^[A-Za-z0-9][A-Za-z0-9_.-]*$")
	return regex.search(value) != null


func _is_integer_number(value: Variant) -> bool:
	if typeof(value) == TYPE_INT:
		return true
	if typeof(value) == TYPE_FLOAT:
		return is_finite(value) and floor(value) == value
	return false


func _has_scene_contract() -> bool:
	return (
		_scene_root.has_method("verification_snapshot")
		and _scene_root.has_method("present_for_capture")
		and _scene_root.has_method("apply_damage")
		and _scene_root.has_method("defeat_enemy")
	)


func _sample(tick: int) -> bool:
	var state: Variant = _scene_root.call("verification_snapshot")
	if typeof(state) != TYPE_DICTIONARY or state.size() != 3 or not state.has("player_hp") or not state.has("enemies_remaining") or not state.has("score"):
		_fail("verification_snapshot returned an invalid state")
		return false
	for key in ["player_hp", "enemies_remaining", "score"]:
		if typeof(state[key]) != TYPE_INT:
			_fail("verification_snapshot fields must be integers")
			return false
	var observed_state := {
		"player_hp": state.player_hp,
		"enemies_remaining": state.enemies_remaining,
		"score": state.score,
	}
	_scene_root.call("present_for_capture")
	if not await _wait_frames(CAPTURE_FRAMES):
		return false
	var capture: Variant = null
	for item in _request.captures:
		if int(item.tick) == tick:
			capture = item
			break
	if capture == null:
		_fail("no capture is bound to tick %d" % tick)
		return false
	if not _save_viewport(String(capture.id), tick, observed_state):
		_fail(_failure_message)
		return false
	_snapshots.append({"tick": tick, "state": observed_state})
	return true


func _wait_frames(count: int) -> bool:
	var started := Time.get_ticks_msec()
	for _index in count:
		if Time.get_ticks_msec() - started > FRAME_WAIT_MSEC:
			_fail("render frame wait exceeded %d ms" % FRAME_WAIT_MSEC)
			return false
		await RenderingServer.frame_post_draw
	return true


func _save_viewport(capture_id: String, tick: int, state: Dictionary) -> bool:
	var texture := root.get_texture()
	if texture == null:
		_failure_message = "root viewport texture is missing"
		return false
	var image := texture.get_image()
	if image == null or image.is_empty():
		_failure_message = "viewport readback returned an empty image"
		return false
	var expected_width := int(_request.viewport_width)
	var expected_height := int(_request.viewport_height)
	if image.get_width() != expected_width or image.get_height() != expected_height:
		_failure_message = "viewport image size %dx%d does not match %dx%d" % [image.get_width(), image.get_height(), expected_width, expected_height]
		return false
	image.convert(Image.FORMAT_RGBA8)
	var final_path := _capture_directory.path_join(capture_id + ".png")
	var temp_path := _capture_directory.path_join(capture_id + ".partial.png")
	if FileAccess.file_exists(final_path) or FileAccess.file_exists(temp_path):
		_failure_message = "capture destination already exists"
		return false
	var err := image.save_png(temp_path)
	if err != OK:
		_failure_message = "could not write capture image"
		return false
	err = DirAccess.rename_absolute(temp_path, final_path)
	if err != OK:
		_failure_message = "could not publish capture image"
		return false
	_captures.append({
		"id": capture_id,
		"tick": tick,
		"file": capture_id + ".png",
		"width": image.get_width(),
		"height": image.get_height(),
		"format": "rgba8",
		"state": state,
	})
	return true


func _renderer_report() -> Dictionary:
	return {
		"rendering_method": RenderingServer.get_current_rendering_method(),
		"rendering_driver": RenderingServer.get_current_rendering_driver_name(),
		"adapter_name": RenderingServer.get_video_adapter_name(),
		"adapter_vendor": RenderingServer.get_video_adapter_vendor(),
		"display_server": DisplayServer.get_name(),
		"os_name": OS.get_name(),
		"viewport_width": root.size.x,
		"viewport_height": root.size.y,
		"source": "engine_api",
	}


func _write_report() -> bool:
	if FileAccess.file_exists(_output_path) or FileAccess.file_exists(_output_path + ".tmp"):
		_fail("report destination already exists")
		return false
	var report := {
		"schema_version": "0.3.0",
		"harness_version": HARNESS_VERSION,
		"execution_id": _request.execution_id,
		"scenario_id": _request.scenario_id,
		"scenario_sha256": _request.scenario_sha256,
		"completed_tick": int(_request.max_tick),
		"snapshots": _snapshots,
		"captures": _captures,
		"renderer": _renderer_report(),
		"completion": "COMPLETED",
		"error": null,
	}
	var temp_file := FileAccess.open(_output_path + ".tmp", FileAccess.WRITE)
	if temp_file == null:
		push_error("cannot create temporary report")
		return false
	temp_file.store_string(JSON.stringify(report))
	temp_file.flush()
	if temp_file.get_error() != OK:
		temp_file.close()
		push_error("could not flush temporary report")
		return false
	temp_file.close()
	var err := DirAccess.rename_absolute(_output_path + ".tmp", _output_path)
	if err != OK:
		push_error("could not publish completed report")
		return false
	return true


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
