extends SceneTree

const REQUEST_SCHEMA := "gameplay-harness-request-1.0.0"
const OUTPUT_SCHEMA := "gameplay-harness-output-1.0.0"
const MAX_REQUEST_BYTES := 2_000_000
const MAX_OUTPUT_BYTES := 8_000_000

func _engine_version() -> String:
	var version_info: Dictionary = Engine.get_version_info()
	var version: String = version_info.get("string", "unknown")
	return version.replace("-", ".").replace(" (official)", ".official")

var request: Dictionary
var output_path := ""
var state: Dictionary = {}
var state_observed_at := ""
var screenshots: Array[String] = []
var metric_samples: Dictionary = {}
var metric_sample_times: Array[int] = []
var metric_started_at := ""
var metric_ended_at := ""
var metric_interval_ms := 0.0
var tick := 0
var exit_status := 0
var _scene_by_id: Dictionary = {}
var _entity_by_id: Dictionary = {}
var _parent_by_id: Dictionary = {}
var _input_by_id: Dictionary = {}

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var request_path := _option(args, "--request")
	output_path = _option(args, "--output")
	if request_path.is_empty() or output_path.is_empty():
		_fail("missing_request_or_output")
		return
	var request_file := FileAccess.open(request_path, FileAccess.READ)
	if request_file == null or request_file.get_length() <= 0 or request_file.get_length() > MAX_REQUEST_BYTES:
		_fail("request_unavailable_or_oversized")
		return
	var parsed: Variant = JSON.parse_string(request_file.get_as_text())
	if not parsed is Dictionary or parsed.get("schema_version") != REQUEST_SCHEMA:
		_fail("invalid_request_schema")
		return
	request = parsed
	_scene_by_id = request.get("scene_allowlist", {})
	_entity_by_id = request.get("entity_allowlist", {})
	_parent_by_id = request.get("parent_allowlist", {})
	_input_by_id = request.get("input_allowlist", {})
	for metric in request.get("metrics", []):
		metric_samples[metric] = []
	seed(request.get("seed", 0))
	Engine.physics_ticks_per_second = request.get("fixed_tick_hz", 60)
	var started := _utc_now()
	var actions: Array = request.get("actions", [])
	var action_index := 0
	while action_index < actions.size():
		var action: Dictionary = actions[action_index]
		var target_tick: int = action.get("tick", -1)
		if target_tick < tick or target_tick > request.get("max_ticks", 0):
			_fail("action_tick_out_of_bounds")
			return
		while tick < target_tick:
			await physics_frame
			tick += 1
		while action_index < actions.size() and actions[action_index].get("tick", -1) == target_tick:
			action = actions[action_index]
			if not await _execute(action):
				return
			action_index += 1
			if action.get("action") == "quit":
				break
			if action.get("action") == "collect_metrics":
				var metric_args: Dictionary = action.get("args", {})
				metric_started_at = _utc_now()
				var end_tick := tick + int(metric_args.get("ticks", 0))
				if end_tick > request.get("max_ticks", 0):
					_fail("metric_collection_exceeds_tick_budget")
					return
				var previous_usec := Time.get_ticks_usec()
				while tick < end_tick:
					await physics_frame
					tick += 1
					var now_usec := Time.get_ticks_usec()
					var delta_seconds := float(now_usec - previous_usec) / 1000000.0
					previous_usec = now_usec
					if (tick - target_tick) % int(metric_args.get("sample_interval_ticks", 1)) == 0:
						_sample_metrics(delta_seconds)
				metric_ended_at = _utc_now()
				if metric_sample_times.size() >= 2:
					metric_interval_ms = float(metric_sample_times[-1] - metric_sample_times[0]) / float(metric_sample_times.size() - 1) / 1000.0
				break
		if action.get("action") == "quit":
			break
	var ended := _utc_now()
	var samples: Array = []
	for metric in request.get("metrics", []):
		var values: Array = metric_samples.get(metric, [])
		if not values.is_empty():
			samples.append({"metric": metric, "unit": _metric_unit(metric), "samples": values})
	var result := {
		"schema_version": OUTPUT_SCHEMA,
		"execution_id": request.get("execution_id"),
		"seed": request.get("seed"),
		"fixed_tick_hz": request.get("fixed_tick_hz"),
		"final_tick": tick,
		"scenario_sha256": request.get("scenario", {}).get("scenario_sha256"),
		"contract_sha256": request.get("contract_sha256"),
		"source_sha256": request.get("source_sha256"),
		"harness_sha256": request.get("harness_sha256"),
		"executable_sha256": request.get("executable_sha256"),
		"engine_identity_sha256": request.get("engine_identity_sha256"),
		"state": state,
		"state_tick": request.get("state_tick", -1),
		"engine_version": _engine_version(),
		"started_at": metric_started_at if not metric_started_at.is_empty() else started,
		"ended_at": metric_ended_at if not metric_ended_at.is_empty() else ended,
		"sample_interval_ms": metric_interval_ms,
		"samples": samples,
		"screenshots": screenshots,
		"exit_status": exit_status,
		"state_observed_at": state_observed_at,
		"execution_mode": "wall_clock_render_fixed_physics" if request.has("performance_budget") else "fixed_render_and_physics",
	}
	var encoded := JSON.stringify(result)
	if encoded.to_utf8_buffer().size() > MAX_OUTPUT_BYTES:
		_fail("output_exceeds_size_limit")
		return
	var output := FileAccess.open(output_path, FileAccess.WRITE)
	if output == null:
		_fail("output_unavailable")
		return
	output.store_string(encoded)
	output.flush()
	output.close()
	quit(exit_status)

func _option(args: PackedStringArray, name: String) -> String:
	var index := args.find(name)
	return args[index + 1] if index >= 0 and index + 1 < args.size() else ""

func _execute(item: Dictionary) -> bool:
	var name: String = item.get("action", "")
	var args: Dictionary = item.get("args", {})
	match name:
		"start_game", "load_scene", "start_level":
			var scene_id: String = args.get("scene_id", "")
			if not _scene_by_id.has(scene_id):
				return _fail("scene_not_allowlisted")
			var packed: Resource = load(_scene_by_id[scene_id])
			if not packed is PackedScene:
				return _fail("allowlisted_scene_failed_to_load")
			var new_root: Node = packed.instantiate()
			if new_root == null:
				return _fail("allowlisted_scene_failed_to_instantiate")
			if current_scene != null:
				root.remove_child(current_scene)
				current_scene.queue_free()
			root.add_child(new_root)
			current_scene = new_root
		"spawn_entity":
			var entity_id: String = args.get("entity_id", "")
			var parent_id: String = args.get("parent_id", "")
			if not _entity_by_id.has(entity_id) or not _parent_by_id.has(parent_id):
				return _fail("spawn_binding_not_allowlisted")
			var packed: Resource = load(_entity_by_id[entity_id])
			var parent := root.get_node_or_null(NodePath(_parent_by_id[parent_id]))
			if not packed is PackedScene or parent == null:
				return _fail("spawn_target_unavailable")
			parent.add_child(packed.instantiate())
		"simulate_input":
			var input_id: String = args.get("input_id", "")
			if not _input_by_id.has(input_id) or not InputMap.has_action(_input_by_id[input_id]):
				return _fail("input_not_allowlisted_or_unmapped")
			if args.get("pressed", false):
				_send_input_action(_input_by_id[input_id], true)
				for _frame in range(args.get("duration_ticks", 1)):
					await physics_frame
					tick += 1
				_send_input_action(_input_by_id[input_id], false)
			else:
				_send_input_action(_input_by_id[input_id], false)
		"wait":
			for _frame in range(args.get("ticks", 1)):
				await physics_frame
				tick += 1
		"capture_screenshot":
			if DisplayServer.get_name() == "headless" or root.get_viewport() == null:
				return _fail("rendered_capture_requires_display_renderer")
			await process_frame
			var image := root.get_viewport().get_texture().get_image()
			var capture_path := _screenshot_path(screenshots.size())
			if image.is_empty() or image.save_png(capture_path) != OK:
				return _fail("viewport_capture_failed")
			screenshots.append(capture_path.get_file())
		"dump_state":
			var captured: Dictionary = {}
			for binding in request.get("property_bindings", []):
				var node := root.get_node_or_null(NodePath(binding.get("node_path", "")))
				if node == null:
					return _fail("bound_node_missing")
				var property_name: String = binding.get("property", "")
				if not _has_property(node, property_name):
					return _fail("bound_property_missing")
				var value: Variant = node.get(property_name)
				if not _is_scalar(value):
					return _fail("bound_property_is_not_scalar")
				captured[binding.get("field", "")] = value
			state = captured
			request["state_tick"] = tick
			state_observed_at = _utc_now()
		"collect_metrics":
			pass
		"quit":
			pass
		_:
			return _fail("unsupported_action")
	return true

func _sample_metrics(delta_seconds: float) -> void:
	metric_sample_times.append(Time.get_ticks_usec())
	for metric in request.get("metrics", []):
		var value := -1.0
		match metric:
			"fps": value = float(Performance.get_monitor(Performance.TIME_FPS))
			"frame_time_ms": value = float(Performance.get_monitor(Performance.TIME_PROCESS)) * 1000.0
			"memory_mb":
				if OS.is_debug_build():
					value = float(Performance.get_monitor(Performance.MEMORY_STATIC)) / 1048576.0
			"entity_count": value = float(Performance.get_monitor(Performance.OBJECT_NODE_COUNT))
			"draw_calls":
				var version_info := Engine.get_version_info()
				if int(version_info.get("major", 0)) > 4 or (int(version_info.get("major", 0)) == 4 and int(version_info.get("minor", 0)) >= 3):
					# Godot 4.3 documents RENDER_TOTAL_DRAW_CALLS_IN_FRAME as monitor enum value 13.
					value = float(Performance.get_monitor(13))
		if value >= 0.0 and is_finite(value):
			metric_samples[metric].append(value)

func _metric_unit(metric: String) -> String:
	match metric:
		"fps": return "frames_per_second"
		"frame_time_ms", "cpu_time_ms": return "milliseconds"
		"memory_mb": return "mebibytes"
		"draw_calls", "entity_count", "particle_count": return "count"
		"texture_size_px": return "pixels"
	return "unknown"

func _is_scalar(value: Variant) -> bool:
	if value == null or typeof(value) in [TYPE_BOOL, TYPE_INT, TYPE_STRING]:
		return true
	return typeof(value) == TYPE_FLOAT and is_finite(value)

func _has_property(node: Object, property_name: String) -> bool:
	for descriptor in node.get_property_list():
		if descriptor.get("name") == property_name:
			return true
	return false

func _send_input_action(action_name: String, is_pressed: bool) -> void:
	var event := InputEventAction.new()
	event.action = action_name
	event.pressed = is_pressed
	event.strength = 1.0 if is_pressed else 0.0
	Input.parse_input_event(event)
	if is_pressed:
		Input.action_press(action_name)
	else:
		Input.action_release(action_name)

func _utc_now() -> String:
	var unix_time := Time.get_unix_time_from_system()
	var date := Time.get_datetime_dict_from_unix_time(int(unix_time))
	var fraction := int(floor((unix_time - floor(unix_time)) * 1000000.0))
	return "%04d-%02d-%02dT%02d:%02d:%02d.%06dZ" % [date.year, date.month, date.day, date.hour, date.minute, date.second, fraction]

func _screenshot_path(index: int) -> String:
	return output_path.get_base_dir().path_join("screenshot-%03d.png" % index)

func _fail(code: String) -> bool:
	exit_status = 1
	var output := FileAccess.open(output_path, FileAccess.WRITE)
	if output != null:
		output.store_string(JSON.stringify({"schema_version": OUTPUT_SCHEMA, "failure": code}))
		output.flush()
	quit(1)
	return false
