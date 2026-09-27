extends SceneTree

const HARNESS_VERSION := "0.2.0"
const MAX_REQUEST_BYTES := 262144
const MAX_ACTIONS := 256
const MAX_SNAPSHOTS := 256
const MAX_TICKS := 10000

var _request_path := ""
var _output_path := ""
var _request: Dictionary = {}
var _scene_root: Node
var _snapshots: Array[Dictionary] = []
var _failure_message := ""


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if not _parse_args(args):
		_fail("arguments must be exactly --request <absolute path> --output <absolute path>")
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
	var packed: Resource = ResourceLoader.load(_request.scene)
	if packed == null or not packed is PackedScene:
		_fail("scene could not be loaded as a PackedScene")
		return
	_scene_root = (packed as PackedScene).instantiate()
	if _scene_root == null:
		_fail("scene could not be instantiated")
		return
	root.add_child(_scene_root)
	await process_frame
	if not _has_scene_contract():
		_fail("scene must implement verification_snapshot, apply_damage and defeat_enemy")
		return
	_capture(0)
	if not _failure_message.is_empty():
		_fail(_failure_message)
		return
	var action_index := 0
	var actions: Array = _request.actions
	var requested_snapshots: Dictionary = {}
	for requested_tick in _request.snapshots:
		requested_snapshots[int(requested_tick)] = true
	for tick in range(1, int(_request.max_tick) + 1):
		# A zero-duration SceneTreeTimer fires at the end of the process frame,
		# after ordinary node processing. Actions then precede that tick's sample.
		await create_timer(0.0).timeout
		while action_index < actions.size() and int(actions[action_index].tick) == tick:
			var action: Dictionary = actions[action_index]
			if action.action == "apply_damage":
				_scene_root.call("apply_damage", int(action.amount))
			else:
				_scene_root.call("defeat_enemy")
			action_index += 1
		if requested_snapshots.has(tick):
			_capture(tick)
			if not _failure_message.is_empty():
				_fail(_failure_message)
				return
	if not _write_report():
		quit(1)
		return
	quit(0)


func _parse_args(args: PackedStringArray) -> bool:
	if args.size() != 4 or args[0] != "--request" or args[2] != "--output":
		return false
	_request_path = args[1]
	_output_path = args[3]
	return _is_absolute(_request_path) and _is_absolute(_output_path)


func _is_absolute(path: String) -> bool:
	return path.is_absolute_path()


func _validate_request() -> bool:
	var keys := ["schema_version", "execution_id", "scenario_id", "scenario_sha256", "scene", "max_tick", "actions", "snapshots"]
	if _request.size() != keys.size():
		_fail("request has missing or unknown fields")
		return false
	for key in keys:
		if not _request.has(key):
			_fail("request is missing field: " + key)
			return false
	if _request.schema_version != "0.2.0" or not _safe_id(_request.execution_id) or not _safe_id(_request.scenario_id):
		_fail("unsupported schema version or invalid identifier")
		return false
	if typeof(_request.scenario_sha256) != TYPE_STRING or _request.scenario_sha256.length() != 64 or not _request.scenario_sha256.is_valid_hex_number(false):
		_fail("invalid scenario fingerprint")
		return false
	if typeof(_request.scene) != TYPE_STRING or not _request.scene.begins_with("res://") or not _request.scene.ends_with(".tscn"):
		_fail("scene must be a res:// .tscn path")
		return false
	var relative_scene := String(_request.scene).substr(6)
	if relative_scene.begins_with("/") or relative_scene.contains("\\") or relative_scene.contains(":"):
		_fail("scene path must be normalized and project-relative")
		return false
	for character in relative_scene:
		var codepoint := character.unicode_at(0)
		if codepoint < 32 or (codepoint >= 127 and codepoint <= 159):
			_fail("scene path contains a control character")
			return false
	var scene_parts: PackedStringArray = relative_scene.split("/")
	for part in scene_parts:
		if part.is_empty() or part == "." or part == "..":
			_fail("scene path is not normalized")
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
		last_tick = action.tick
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
		previous = tick
	if _request.snapshots[0] != 0 or _request.snapshots[-1] != _request.max_tick:
		_fail("snapshots must include tick zero and max_tick")
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
	return _scene_root.has_method("verification_snapshot") and _scene_root.has_method("apply_damage") and _scene_root.has_method("defeat_enemy")


func _capture(tick: int) -> void:
	var state: Variant = _scene_root.call("verification_snapshot")
	if typeof(state) != TYPE_DICTIONARY or state.size() != 3 or not state.has("player_hp") or not state.has("enemies_remaining") or not state.has("score"):
		_failure_message = "verification_snapshot returned an invalid state"
		return
	for key in ["player_hp", "enemies_remaining", "score"]:
		if typeof(state[key]) != TYPE_INT:
			_failure_message = "verification_snapshot fields must be integers"
			return
	# Godot Dictionaries are reference types. Copy only the allowlisted values
	# so later game mutations cannot rewrite an earlier observation.
	var observed_state := {
		"player_hp": state.player_hp,
		"enemies_remaining": state.enemies_remaining,
		"score": state.score,
	}
	_snapshots.append({"tick": tick, "state": observed_state})


func _write_report() -> bool:
	if FileAccess.file_exists(_output_path) or FileAccess.file_exists(_output_path + ".tmp"):
		_fail("report destination already exists")
		return false
	var report := {
		"schema_version": "0.2.0",
		"execution_id": _request.execution_id,
		"scenario_id": _request.scenario_id,
		"scenario_sha256": _request.scenario_sha256,
		"completed_tick": int(_request.max_tick),
		"snapshots": _snapshots,
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
