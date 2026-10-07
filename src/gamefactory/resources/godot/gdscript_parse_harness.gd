extends SceneTree

const MAX_MANIFEST_BYTES := 1_000_000
const MAX_SCRIPTS := 256
const MAX_PATH_LEN := 512

func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var manifest_path := _option(args, "--manifest")
	var output_path := _option(args, "--output")
	if manifest_path.is_empty() or output_path.is_empty():
		_fail("missing_manifest_or_output")
		return
	var manifest_file := FileAccess.open(manifest_path, FileAccess.READ)
	if manifest_file == null:
		_fail("manifest_unavailable")
		return
	if manifest_file.get_length() <= 0 or manifest_file.get_length() > MAX_MANIFEST_BYTES:
		_fail("manifest_oversized")
		return
	var parsed: Variant = JSON.parse_string(manifest_file.get_as_text())
	manifest_file.close()
	if not parsed is Dictionary:
		_fail("manifest_invalid")
		return
	var scripts: Variant = parsed.get("scripts")
	if typeof(scripts) != TYPE_ARRAY or scripts.size() > MAX_SCRIPTS:
		_fail("scripts_invalid")
		return
	var results: Array[Dictionary] = []
	var failed := false
	for entry in scripts:
		if typeof(entry) != TYPE_STRING:
			failed = true
			results.append({"path": "?", "status": "FAIL", "message": "invalid_manifest_entry"})
			continue
		var relative := String(entry)
		var message := _check_relative_script(relative)
		if not message.is_empty():
			failed = true
			results.append({"path": relative, "status": "FAIL", "message": message})
		else:
			results.append({"path": relative, "status": "PASS", "message": ""})
	if not _write_output(output_path, results):
		quit(1)
		return
	quit(1 if failed else 0)


func _option(args: PackedStringArray, flag: String) -> String:
	var index := args.find(flag)
	if index < 0 or index + 1 >= args.size():
		return ""
	return args[index + 1]


func _check_relative_script(relative: String) -> String:
	if relative.is_empty() or relative.length() > MAX_PATH_LEN:
		return "invalid_path"
	if relative.begins_with("/") or relative.contains("\\") or relative.contains(":"):
		return "invalid_path"
	if relative.begins_with(".factory-"):
		return "internal_harness_path"
	for part in relative.split("/"):
		if part.is_empty() or part == "." or part == "..":
			return "invalid_path"
	if not relative.to_lower().ends_with(".gd"):
		return "not_gdscript"
	var resource_path := "res://" + relative
	var script: Variant = ResourceLoader.load(resource_path, "", ResourceLoader.CACHE_MODE_REUSE)
	if script == null:
		return "load_failed"
	if not script is GDScript:
		return "not_gdscript_resource"
	return ""


func _write_output(output_path: String, results: Array[Dictionary]) -> bool:
	if FileAccess.file_exists(output_path) or FileAccess.file_exists(output_path + ".tmp"):
		push_error("output_already_exists")
		return false
	var temp_path := output_path + ".tmp"
	var output_file := FileAccess.open(temp_path, FileAccess.WRITE)
	if output_file == null:
		push_error("output_unavailable")
		return false
	output_file.store_string(JSON.stringify({"scripts": results}))
	output_file.flush()
	if output_file.get_error() != OK:
		output_file.close()
		push_error("output_flush_failed")
		return false
	output_file.close()
	if DirAccess.rename_absolute(temp_path, output_path) != OK:
		push_error("output_publish_failed")
		return false
	return true


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
