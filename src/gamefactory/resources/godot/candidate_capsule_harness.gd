extends SceneTree

## V0.8-3B CLOSED candidate capsule + nine-view runtime (ADR 0021). No legacy LOD0
## fallback, no bottom_center capsule policy. Behavior derives from bound request digest.

const MAX_REQUEST_BYTES := 65536
const JSON_SAFE_INTEGER_MAX := 9007199254740991
const VIEW_TABLE := {
	"front": [Vector3(0, 0, -1), "-Z"],
	"rear": [Vector3(0, 0, 1), "+Z"],
	"left": [Vector3(-1, 0, 0), "-X"],
	"right": [Vector3(1, 0, 0), "+X"],
	"side": [Vector3(1, 0, 0), "+X"],
	"three_quarter": [Vector3(1, 0.65, -1), "+X-Z"],
	"three_quarter_front": [Vector3(1, 0.65, -1), "+X-Z"],
	"three_quarter_rear": [Vector3(1, 0.65, 1), "+X+Z"],
	"top": [Vector3(0, 1, 0), "+Y"],
}
var request: Dictionary = {}
var failures: Array[String] = []
var world_root: Node3D
var visual: MeshInstance3D
var physics_body: StaticBody3D
var collision_shape: CollisionShape3D
var physics_ray_hit := false
var view_framing := {}
var harness_sha256_raw := ""
var observed_glb_sha256 := ""
var bound_revision := 0
var bound_strict_attempt := 0

func _sha256_hex(data: PackedByteArray) -> String:
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(data)
	var digest = ctx.finish()
	var out := ""
	for i in range(digest.size()):
		out += "%02x" % digest[i]
	return out

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 2 or args[0] != "--request":
		_fail("arguments must be --request <json>")
		_write_observation(false)
		quit(1)
		return
	var file := FileAccess.open(args[1], FileAccess.READ)
	if file == null or file.get_length() > MAX_REQUEST_BYTES:
		_fail("request is missing or exceeds size limit")
		_write_observation(false)
		quit(1)
		return
	request = JSON.parse_string(file.get_as_text())
	file.close()
	if typeof(request) != TYPE_DICTIONARY:
		_fail("request must be a JSON object")
		_write_observation(false)
		quit(1)
		return
	if str(request.get("schema_version", "")) != "candidate-runtime-request-0.8.0":
		_fail("schema_version must be candidate-runtime-request-0.8.0")
		_write_observation(false)
		quit(1)
		return
	if not request.has("bound_payload_canonical"):
		_fail("bound_payload_canonical is required")
		_write_observation(false)
		quit(1)
		return
	var canonical := str(request.bound_payload_canonical)
	var digest := canonical.sha256_text()
	if str(request.get("request_digest", "")) != digest:
		_fail("request_digest does not match bound_payload_canonical")
		_write_observation(false)
		quit(1)
		return
	var bound_core: Variant = JSON.parse_string(canonical)
	if typeof(bound_core) != TYPE_DICTIONARY:
		_fail("bound_payload_canonical is not a JSON object")
		_write_observation(false)
		quit(1)
		return
	for key in bound_core.keys():
		request[key] = bound_core[key]
	request["bound_payload_canonical"] = canonical
	request["request_digest"] = digest
	bound_revision = _strict_runtime_int(request.get("revision", null), "revision")
	bound_strict_attempt = _strict_runtime_int(
		request.get("strict_attempt_number", null),
		"strict_attempt_number"
	)
	request["revision"] = bound_revision
	request["strict_attempt_number"] = bound_strict_attempt
	var glb_bytes: PackedByteArray = FileAccess.get_file_as_bytes("res://asset.glb")
	if glb_bytes.is_empty():
		_fail("asset.glb bytes are missing or empty")
		_write_observation(false)
		quit(1)
		return
	observed_glb_sha256 = _sha256_hex(glb_bytes).to_lower()
	var expected_glb_sha := str(request.get("processed_glb_sha256", "")).to_lower()
	if observed_glb_sha256 != expected_glb_sha:
		_fail(
			"observed GLB hash does not match request: got "
			+ observed_glb_sha256
			+ " expected "
			+ expected_glb_sha
		)
		_write_observation(false)
		quit(1)
		return
	var harness_path := ProjectSettings.globalize_path("res://candidate_capsule_harness.gd")
	var harness_bytes := FileAccess.get_file_as_bytes(harness_path)
	harness_sha256_raw = _sha256_hex(harness_bytes)
	var packed := load(str(request.glb)) as PackedScene
	if packed == null:
		_fail("Godot could not import/load the processed GLB")
		_write_observation(false)
		quit(1)
		return
	var imported := packed.instantiate()
	if imported == null:
		_fail("GLB instance failed")
		_write_observation(false)
		quit(1)
		return
	world_root = Node3D.new()
	world_root.name = "CandidateVerificationWorld"
	root.add_child(world_root)
	var asset_root := Node3D.new()
	asset_root.name = "AssetUnderTest"
	world_root.add_child(asset_root)
	asset_root.add_child(imported)
	visual = _find_mesh(imported, str(request.get("visual_mesh_name", "SM_HumanoidSkin")))
	if _find_node(imported, "HumanoidRoot") == null:
		_fail("HumanoidRoot is missing from imported graph")
	if visual == null or visual.mesh == null or visual.mesh.get_surface_count() == 0:
		_fail("SM_HumanoidSkin visible mesh with geometry is missing")
		_write_observation(false)
		quit(1)
		return
	_hide_other_meshes(imported, str(visual.name))
	var bounds := visual.global_transform * visual.mesh.get_aabb()
	if not bounds.size.is_finite() or bounds.size.x <= 0 or bounds.size.y <= 0 or bounds.size.z <= 0:
		_fail("runtime mesh bounds are invalid")
	physics_body = StaticBody3D.new()
	physics_body.name = "CandidateStaticBody"
	var shape_node := CollisionShape3D.new()
	shape_node.name = "CandidateCapsuleShape"
	var capsule := CapsuleShape3D.new()
	var cap_decl: Dictionary = request.get("capsule", {})
	capsule.radius = float(cap_decl.get("radius_m", 0.0))
	capsule.height = float(cap_decl.get("height_m", 0.0))
	shape_node.shape = capsule
	collision_shape = shape_node
	var center_arr: Array = request.get("capsule_center_m", [])
	if center_arr.size() != 3:
		_fail("capsule_center_m must have three components")
	shape_node.position = Vector3(float(center_arr[0]), float(center_arr[1]), float(center_arr[2]))
	physics_body.add_child(shape_node)
	world_root.add_child(physics_body)
	if not failures.is_empty():
		_write_observation(false, bounds)
		quit(1)
		return
	world_root.add_child(_environment())
	var camera := Camera3D.new()
	camera.current = true
	var framing: Dictionary = request.get("framing", {})
	camera.fov = float(framing.get("fov_degrees", 38.0))
	world_root.add_child(camera)
	var extent: float = max(bounds.size.x, max(bounds.size.y, bounds.size.z))
	var center: Vector3 = bounds.get_center()
	var scale_reference := _add_floor(bounds, extent)
	var views: Array = request.get("nine_view_set", [])
	if typeof(views) != TYPE_ARRAY or views.is_empty():
		_fail("nine_view_set must be a non-empty array")
	var min_fraction := float(framing.get("min_screen_fraction", 0.55))
	var max_fraction := float(framing.get("max_screen_fraction", 0.75))
	var target_fraction := float(framing.get("target_screen_fraction", 0.65))
	var margin_fraction := float(framing.get("margin_fraction", 0.04))
	var captures: Array = []
	for angle in views:
		var view_name := str(angle)
		if not VIEW_TABLE.has(view_name):
			_fail("unknown capture view " + view_name)
			continue
		var direction: Vector3 = _view_direction_vector(view_name)
		_park_reference(scale_reference, bounds, direction, extent)
		var geom := _framing_geometry(bounds, camera, view_name, target_fraction)
		var distance: float = geom["distance"]
		var up_vector := Vector3.UP if view_name != "top" else Vector3(0, 0, -1)
		camera.position = center + direction * distance
		camera.look_at(center, up_vector)
		await process_frame
		await process_frame
		var image: Image = root.get_texture().get_image()
		var vp: Dictionary = request.get("viewport", {})
		var w := int(vp.get("width", 1280))
		var h := int(vp.get("height", 720))
		if image == null or image.get_width() != w or image.get_height() != h:
			_fail("capture resolution does not match viewport contract")
			continue
		var measured := _measure_view_framing(camera, bounds, scale_reference, Vector2(w, h), view_name, min_fraction, max_fraction, margin_fraction)
		measured["camera_distance"] = distance
		view_framing[view_name] = measured
		if not measured.get("ok", false):
			_fail(str(measured.get("reason", "capture framing failed")))
			continue
		var filename := str(request.capture_dir).path_join(view_name + ".png")
		if image.save_png(filename) != OK:
			_fail("Godot renderer could not save " + view_name + " capture")
			continue
		var png_bytes := FileAccess.get_file_as_bytes(filename)
		var viewport_size := root.get_viewport().get_visible_rect().size if root != null else Vector2(1280, 720)
		captures.append({
			"view": view_name,
			"png_sha256": _sha256_hex(png_bytes),
			"png_width": int(viewport_size.x),
			"png_height": int(viewport_size.y),
			"execution_id": str(request.execution_id),
			"revision": bound_revision,
			"strict_attempt_number": bound_strict_attempt,
			"request_digest": digest,
		})
	await physics_frame
	var query := PhysicsRayQueryParameters3D.create(center + Vector3(0, extent * 2.0, 0), center - Vector3(0, extent * 2.0, 0))
	var hit := world_root.get_world_3d().direct_space_state.intersect_ray(query)
	if hit.is_empty() or hit.get("collider") != physics_body:
		_fail("physics ray did not hit the runtime capsule body")
	else:
		physics_ray_hit = true
	request["_captures"] = captures
	if not failures.is_empty():
		_write_observation(false, bounds)
		quit(1)
		return
	_write_observation(true, bounds)
	quit(0)

func _find_mesh(node: Node, expected_name: String) -> MeshInstance3D:
	if node is MeshInstance3D and node.name == expected_name:
		return node as MeshInstance3D
	for child in node.get_children():
		var found := _find_mesh(child, expected_name)
		if found != null:
			return found
	return null

func _find_node(node: Node, expected_name: String) -> Node:
	if str(node.name) == expected_name:
		return node
	for child in node.get_children():
		var found := _find_node(child, expected_name)
		if found != null:
			return found
	return null

func _hide_other_meshes(node: Node, keep_name: String) -> void:
	if node is MeshInstance3D:
		node.visible = str(node.name) == keep_name
	for child in node.get_children():
		_hide_other_meshes(child, keep_name)

func _fail(message: String) -> void:
	failures.append(message)

func _strict_runtime_int(value: Variant, field: String) -> int:
	if typeof(value) == TYPE_BOOL:
		_fail(field + " must not be a boolean")
		return 0
	if typeof(value) == TYPE_STRING:
		_fail(field + " must be an integer")
		return 0
	var out := 0
	if typeof(value) == TYPE_INT:
		out = value
	elif typeof(value) == TYPE_FLOAT:
		if not is_finite(value):
			_fail(field + " must be a finite integer")
			return 0
		if value != floor(value):
			_fail(field + " must be an integer")
			return 0
		if value > JSON_SAFE_INTEGER_MAX:
			_fail(field + " exceeds the JSON-safe integer maximum")
			return 0
		out = int(value)
	else:
		_fail(field + " must be an integer")
		return 0
	if out < 1:
		_fail(field + " must be >= 1")
	if out > JSON_SAFE_INTEGER_MAX:
		_fail(field + " exceeds the JSON-safe integer maximum")
	return out

func _view_direction_vector(angle: String) -> Vector3:
	var entry: Array = VIEW_TABLE[angle]
	return (entry[0] as Vector3).normalized()

func _view_axis_label(angle: String) -> String:
	return str(VIEW_TABLE[angle][1])

func _framing_geometry(bounds: AABB, camera: Camera3D, angle: String, target_fraction: float) -> Dictionary:
	var d := _view_direction_vector(angle)
	var up := Vector3.UP if angle != "top" else Vector3(0, 0, -1)
	var f := -d
	var r := f.cross(up).normalized()
	var u := r.cross(f)
	var h := bounds.size * 0.5
	var e_r := absf(r.x) * h.x + absf(r.y) * h.y + absf(r.z) * h.z
	var e_u := absf(u.x) * h.x + absf(u.y) * h.y + absf(u.z) * h.z
	var e_f := absf(f.x) * h.x + absf(f.y) * h.y + absf(f.z) * h.z
	var half_fov := deg_to_rad(camera.fov * 0.5)
	var t := tan(half_fov)
	var viewport: Vector2 = root.get_viewport().get_visible_rect().size if root != null else Vector2(1280, 720)
	var aspect: float = viewport.x / maxf(viewport.y, 1.0)
	var d_c := e_f + maxf(e_u / (t * target_fraction), e_r / (t * target_fraction * aspect))
	var corners: Array[Vector3] = []
	for sx in [-1.0, 1.0]:
		for sy in [-1.0, 1.0]:
			for sz in [-1.0, 1.0]:
				corners.append(Vector3(sx * h.x, sy * h.y, sz * h.z))
	var low := e_f + minf(0.0001, (d_c - e_f) * 0.5)
	var high := d_c
	for _i in 60:
		var mid := (low + high) * 0.5
		var max_v := -INF
		var min_v := INF
		var max_h := -INF
		var min_h := INF
		for p in corners:
			var z := mid + f.dot(p)
			var py := u.dot(p) / (z * t)
			var px := r.dot(p) / (z * t * aspect)
			if py > max_v: max_v = py
			if py < min_v: min_v = py
			if px > max_h: max_h = px
			if px < min_h: min_h = px
		var vf := (max_v - min_v) * 0.5
		var hf := (max_h - min_h) * 0.5
		if maxf(vf, hf) > target_fraction:
			low = mid
		else:
			high = mid
	return {"distance": high, "conservative_distance": d_c}

func _measure_view_framing(camera: Camera3D, bounds: AABB, scale_reference: MeshInstance3D, viewport: Vector2, angle: String, min_fraction: float, max_fraction: float, margin_fraction: float) -> Dictionary:
	var rect := _projected_rect(camera, bounds)
	var margin_x := viewport.x * margin_fraction
	var margin_y := viewport.y * margin_fraction
	var inside := rect.position.x >= margin_x and rect.position.y >= margin_y and rect.position.x + rect.size.x <= viewport.x - margin_x and rect.position.y + rect.size.y <= viewport.y - margin_y
	var height_ratio := rect.size.y / viewport.y if viewport.y > 0 else 0.0
	var fill_ratio := maxf(height_ratio, rect.size.x / viewport.x if viewport.x > 0 else 0.0)
	var center_x := rect.position.x + rect.size.x * 0.5
	var center_offset := absf(center_x - viewport.x * 0.5) / viewport.x if viewport.x > 0 else 1.0
	var reason := ""
	if not inside:
		reason = "asset bounds are outside the viewport safety margin"
	elif fill_ratio < min_fraction or fill_ratio > max_fraction:
		reason = "asset screen fill is outside the profile framing range"
	elif center_offset > 0.08:
		reason = "asset is not horizontally centered"
	return {
		"ok": reason == "",
		"reason": reason,
		"height_ratio": height_ratio,
		"fill_ratio": fill_ratio,
		"horizontally_centered": center_offset <= 0.08,
		"view_axis": _view_axis_label(angle),
		"projected_rect_pixels": {
			"x": rect.position.x,
			"y": rect.position.y,
			"width": rect.size.x,
			"height": rect.size.y,
		},
		"center_offset": center_offset,
	}

func _projected_rect(camera: Camera3D, bounds: AABB) -> Rect2:
	var min_x := INF
	var min_y := INF
	var max_x := -INF
	var max_y := -INF
	var origin := bounds.position
	var size := bounds.size
	for i in 8:
		var corner := origin + Vector3(
			size.x if (i & 1) != 0 else 0.0,
			size.y if (i & 2) != 0 else 0.0,
			size.z if (i & 4) != 0 else 0.0
		)
		var screen := camera.unproject_position(corner)
		min_x = minf(min_x, screen.x)
		min_y = minf(min_y, screen.y)
		max_x = maxf(max_x, screen.x)
		max_y = maxf(max_y, screen.y)
	return Rect2(Vector2(min_x, min_y), Vector2(max_x - min_x, max_y - min_y))

func _add_floor(bounds: AABB, extent: float) -> MeshInstance3D:
	var scale_reference := MeshInstance3D.new()
	scale_reference.name = "OneMeterScaleReference"
	var reference_mesh := BoxMesh.new()
	reference_mesh.size = Vector3.ONE
	scale_reference.mesh = reference_mesh
	scale_reference.position = Vector3(bounds.end.x + extent, 0.5, bounds.get_center().z)
	world_root.add_child(scale_reference)
	return scale_reference

func _park_reference(reference: MeshInstance3D, bounds: AABB, direction: Vector3, extent: float) -> void:
	var center := bounds.get_center()
	var reference_half := 0.5
	if abs(direction.y) > 0.9:
		reference.position = Vector3(bounds.end.x + reference_half + extent + 0.25, bounds.position.y + reference_half, center.z)
		return
	var away := Vector3(-direction.x, 0.0, -direction.z).normalized()
	reference.position = Vector3(center.x, bounds.position.y + reference_half, center.z) + away * (extent + reference_half + 0.5)

func _environment() -> WorldEnvironment:
	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color("30343b")
	var world_environment := WorldEnvironment.new()
	world_environment.environment = environment
	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-35, -35, 0)
	world_root.add_child(light)
	return world_environment

func _rest_aabb_canonical_sha256(min_arr: Array, max_arr: Array) -> String:
	var payload := '{"max":' + JSON.stringify(max_arr) + ',"min":' + JSON.stringify(min_arr) + '}'
	return payload.sha256_text()

func _write_observation(passed: bool, bounds: AABB = AABB()) -> void:
	var digest := str(request.get("request_digest", ""))
	var cap_decl: Dictionary = request.get("capsule", {})
	var center_arr: Array = request.get("capsule_center_m", [])
	var local_aabb := visual.mesh.get_aabb() if visual != null and visual.mesh != null else AABB()
	var rest_min := [
		local_aabb.position.x,
		local_aabb.position.y,
		local_aabb.position.z,
	]
	var rest_max := [
		local_aabb.position.x + local_aabb.size.x,
		local_aabb.position.y + local_aabb.size.y,
		local_aabb.position.z + local_aabb.size.z,
	]
	var observation := {
		"schema_version": "candidate-runtime-observation-0.8.0",
		"workflow_id": str(request.get("workflow_id", "")),
		"revision": bound_revision,
		"execution_id": str(request.get("execution_id", "")),
		"strict_attempt_number": bound_strict_attempt,
		"asset_id": str(request.get("asset_id", "")),
		"processed_glb_sha256": str(request.get("processed_glb_sha256", "")),
		"observed_glb_sha256": observed_glb_sha256,
		"request_digest": digest,
		"rest_aabb_canonical_sha256": str(request.get("rest_aabb_canonical_sha256", "")),
		"godot_version": str(request.get("godot_version", "")),
		"harness_sha256": str(request.get("harness_sha256", "")),
		"harness_sha256_raw": harness_sha256_raw,
		"status": "PASS" if passed else "FAIL",
		"candidate_state": str(request.get("candidate_state", "CLOSED")),
		"public_status": str(request.get("public_status", "UNSUPPORTED")),
		"production_eligible": false,
		"visual_mesh_name": str(request.get("visual_mesh_name", "")),
		"runtime_body_kind": "static_body",
		"collision_shape_class": "CapsuleShape3D",
		"physics_ray_hit": physics_ray_hit,
		"rest_mesh_bounds": {
			"min": rest_min,
			"max": rest_max,
		},
		"capsule": {
			"shape_class": "CapsuleShape3D",
			"observed_radius_m": collision_shape.shape.radius if collision_shape != null and collision_shape.shape is CapsuleShape3D else 0.0,
			"observed_height_m": collision_shape.shape.height if collision_shape != null and collision_shape.shape is CapsuleShape3D else 0.0,
			"center_m": [
				collision_shape.position.x if collision_shape != null else 0.0,
				collision_shape.position.y if collision_shape != null else 0.0,
				collision_shape.position.z if collision_shape != null else 0.0,
			],
		},
		"view_framing": view_framing,
		"captures": request.get("_captures", []),
		"errors": failures.duplicate(),
	}
	var out_path := str(request.get("observation_path", ""))
	if out_path != "":
		var out := FileAccess.open(out_path, FileAccess.WRITE)
		if out != null:
			out.store_string(JSON.stringify(observation))
			out.close()
