extends SceneTree

## Standalone runtime proof for a static, unrigged V0.7 character.
const MAX_REQUEST_BYTES := 131072

var request: Dictionary = {}
var failures: Array[String] = []
var import_tree: Dictionary = {}
var lod_observations: Dictionary = {}
var collider_observation: Dictionary = {}
var view_framing: Dictionary = {}
var captures: Dictionary = {}
var world_root: Node3D
var imported_root: Node

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var request_path := ""
	var index := 0
	while index < args.size():
		if args[index] == "--request" and index + 1 < args.size():
			request_path = args[index + 1]
			index += 2
		else:
			index += 1
	if request_path.is_empty():
		_fail("arguments must include --request <json>")
		quit(1)
		return
	if not _is_safe_path(request_path) or not request_path.ends_with(".json"):
		_fail("runtime request path is invalid or unsafe: " + request_path)
		quit(1)
		return
	var request_file := FileAccess.open(request_path, FileAccess.READ)
	if request_file == null or request_file.get_length() > MAX_REQUEST_BYTES:
		_fail("runtime request is missing or exceeds its size limit")
		quit(1)
		return
	var raw_request := request_file.get_as_text()
	request_file.close()
	var parsed: Variant = JSON.parse_string(raw_request)
	if typeof(parsed) != TYPE_DICTIONARY:
		_fail("runtime request must be a JSON object")
		quit(1)
		return

	var obs_val: Variant = parsed.get("observation_path")
	if not _is_safe_path(obs_val) or not str(obs_val).ends_with(".json"):
		_fail("observation_path is invalid, unsafe, or does not end in .json: " + str(obs_val))
		quit(1)
		return
	var out_dir_val: Variant = parsed.get("output_dir")
	if not _is_safe_path(out_dir_val) or str(out_dir_val).ends_with(".json") or str(out_dir_val).ends_with(".glb") or str(out_dir_val).ends_with(".png"):
		_fail("output_dir is invalid, unsafe, or has unsafe extension: " + str(out_dir_val))
		quit(1)
		return
	var glb_val: Variant = parsed.get("glb")
	if not _is_safe_path(glb_val) or not str(glb_val).ends_with(".glb"):
		_fail("glb must be a safe res:// .glb path: " + str(glb_val))
		quit(1)
		return

	request = parsed
	for key in ["workflow_id", "revision", "asset_id", "execution_id", "attempt_number", "raw_glb_sha256", "processed_glb_sha256", "harness_sha256", "profile_sha256", "specification_sha256", "request_digest", "glb", "output_dir", "observation_path", "review_views", "spec", "profile"]:
		if not request.has(key):
			_fail("runtime request is missing " + key)
			_write_observation(false)
			quit(1)
			return
	if not _positive_integer(request.revision) or not _positive_integer(request.attempt_number):
		_fail("revision and attempt_number must be positive integers")
		_write_observation(false)
		quit(1)
		return
	var requested_views: Array[String] = []
	var seen: Dictionary = {}
	if typeof(request.review_views) != TYPE_ARRAY or request.review_views.is_empty():
		_fail("review_views must be a non-empty list")
		_write_observation(false)
		quit(1)
		return
	for view in request.review_views:
		if typeof(view) != TYPE_STRING or view.is_empty() or view_placement(view).is_empty() or seen.has(view):
			_fail("review view is invalid, duplicated, or unsupported: " + str(view))
			_write_observation(false)
			quit(1)
			return
		seen[view] = true
		requested_views.append(view)
	if typeof(request.spec) != TYPE_DICTIONARY or typeof(request.profile) != TYPE_DICTIONARY:
		_fail("spec and profile must be JSON objects")
		_write_observation(false)
		quit(1)
		return

	var packed := load(str(request.glb)) as PackedScene
	if packed == null:
		_fail("Godot failed to import or load the processed character GLB")
		_write_observation(false)
		quit(1)
		return
	imported_root = packed.instantiate()
	if imported_root == null:
		_fail("Godot failed to instantiate the processed character scene")
		_write_observation(false)
		quit(1)
		return
	import_tree = _record_tree(imported_root)
	world_root = Node3D.new()
	world_root.name = "CharacterVerificationWorld"
	root.add_child(world_root)
	world_root.add_child(imported_root)

	var asset_id := str(request.asset_id)
	var expected_names := ["SM_" + asset_id + "_LOD0", "SM_" + asset_id + "_LOD1"]
	var all_meshes: Array[MeshInstance3D] = []
	_collect_meshes(imported_root, all_meshes)
	var mesh_names: Array[String] = []
	for mesh_node in all_meshes:
		mesh_names.append(mesh_node.name)
	mesh_names.sort()
	var sorted_expected := expected_names.duplicate()
	sorted_expected.sort()
	if mesh_names != sorted_expected:
		_fail("imported mesh inventory differs from declared LOD0/LOD1: " + str(mesh_names))
	for name in expected_names:
		var matches := _find_all(imported_root, name)
		if matches.size() != 1 or not (matches[0] is MeshInstance3D):
			_fail("imported LOD node missing or duplicated: " + name)
			continue
		var lod := matches[0] as MeshInstance3D
		if lod.mesh == null or lod.mesh.get_surface_count() < 1:
			_fail(name + " has no imported mesh surfaces")
		if not _identity(lod.transform):
			_fail(name + " imported local transform is not identity")
		if name.ends_with("LOD0"):
			lod.visible = true
		else:
			lod.visible = false
		lod_observations[name] = {
			"mesh_present": lod.mesh != null,
			"surface_count": lod.mesh.get_surface_count() if lod.mesh != null else 0,
			"visible": lod.visible,
			"local_position": _vec_array(lod.transform.origin),
			"local_basis": _basis_rows(lod.transform.basis),
			"local_scale": _vec_array(lod.transform.basis.get_scale()),
		}
	if _contains_semantic_node(imported_root):
		_fail("single_mesh character import unexpectedly contains PART_ or SOCKET_ nodes")
	if not failures.is_empty():
		_write_observation(false)
		quit(1)
		return

	var spec: Dictionary = request.spec
	var profile: Dictionary = request.profile
	var collider_spec: Dictionary = spec.get("collider", {})
	var capsule: Dictionary = collider_spec.get("capsule", {})
	var radius := float(capsule.get("radius_m", 0.0))
	var height := float(capsule.get("height_m", 0.0))
	var origin_policy := str(spec.get("origin_policy", ""))
	if radius <= 0.0 or height <= 2.0 * radius or not (origin_policy in ["bottom_center", "center"]):
		_fail("typed capsule dimensions or origin policy are invalid")
		_write_observation(false)
		quit(1)
		return
	var expected_center_y := height * 0.5 if origin_policy == "bottom_center" else 0.0
	var body := StaticBody3D.new()
	body.name = "CharacterStaticBody"
	body.position = Vector3(0.0, expected_center_y, 0.0)
	world_root.add_child(body)
	var collision := CollisionShape3D.new()
	collision.name = "CharacterCapsuleCollision"
	var shape := CapsuleShape3D.new()
	shape.radius = radius
	shape.height = height
	collision.shape = shape
	body.add_child(collision)
	await physics_frame
	await physics_frame
	var ray_from := Vector3(0.0, expected_center_y + height * 0.5 + 0.3, 0.0)
	var ray_to := Vector3(0.0, expected_center_y - height * 0.5 - 0.3, 0.0)
	var query := PhysicsRayQueryParameters3D.create(ray_from, ray_to)
	query.collide_with_areas = false
	query.collide_with_bodies = true
	var hit := world_root.get_world_3d().direct_space_state.intersect_ray(query)
	var ray_hit: bool = not hit.is_empty() and hit.get("collider") == body
	if not ray_hit:
		_fail("physics ray did not hit the generated character capsule")
	collider_observation = {
		"body_kind": body.get_class(),
		"shape_class": shape.get_class(),
		"radius_m": shape.radius,
		"height_m": shape.height,
		"center_y_m": body.position.y,
		"physics_ray_hit": ray_hit,
	}

	var bounds := _character_bounds(imported_root)
	if bounds.size.x <= 0.0 or bounds.size.y <= 0.0 or bounds.size.z <= 0.0:
		_fail("imported character LOD0 bounds are empty")
		_write_observation(false)
		quit(1)
		return
	world_root.add_child(_environment())
	var framing: Dictionary = profile.get("framing", {})
	var min_fraction := float(framing.get("min_screen_fraction", 0.55))
	var max_fraction := float(framing.get("max_screen_fraction", 0.75))
	var target_fraction := float(framing.get("target_screen_fraction", 0.65))
	var margin_fraction := float(framing.get("margin_fraction", 0.04))
	var camera := Camera3D.new()
	camera.name = "CharacterReviewCamera"
	camera.current = true
	camera.fov = float(framing.get("fov_degrees", 38.0))
	world_root.add_child(camera)
	var center := bounds.get_center()
	var extent := maxf(bounds.size.x, maxf(bounds.size.y, bounds.size.z))
	var scale_reference := _add_floor(bounds, extent)
	var out_dir := str(request.output_dir)
	var dir_err := DirAccess.make_dir_recursive_absolute(out_dir)
	if dir_err != OK and dir_err != ERR_ALREADY_EXISTS:
		_fail("could not create output directory: " + out_dir)
		_write_observation(false)
		quit(1)
		return
	for view in requested_views:
		var direction := _view_direction_vector(view)
		_park_reference(scale_reference, bounds, direction, extent)
		var geom := _framing_geometry(bounds, camera, view, target_fraction)
		var distance := float(geom.distance)
		camera.position = center + direction * distance
		camera.look_at(center, _view_up_vector(view))
		await process_frame
		await process_frame
		var image := root.get_texture().get_image()
		if image == null or image.get_width() != 1280 or image.get_height() != 720:
			_fail("rendered capture resolution is not 1280x720 for " + view)
			continue
		var measured := _measure_view_framing(camera, bounds, scale_reference, Vector2(1280, 720), view, min_fraction, max_fraction, margin_fraction)
		measured["view_axis"] = view_placement(view).axis_label
		measured["camera_direction"] = _vec_array(-camera.global_basis.z.normalized())
		measured["camera_up"] = _vec_array(camera.global_basis.y.normalized())
		view_framing[view] = measured
		if not measured.get("ok", false):
			_fail(view + " camera framing failed: " + str(measured.get("reason", "")))
			continue
		var capture_path := out_dir.path_join(view + ".png")
		if not _is_safe_path(capture_path) or not capture_path.ends_with(".png"):
			_fail("capture path is unsafe for " + view)
			continue
		if image.save_png(capture_path) != OK:
			_fail("could not save rendered review capture for " + view)
		else:
			captures[view] = {"sha256": FileAccess.get_sha256(capture_path)}
	if not failures.is_empty():
		_write_observation(false)
		quit(1)
		return
	_write_observation(true)
	quit(0)

func _fail(message: String) -> void:
	failures.append(message)
	push_error(message)

func _identity(value: Transform3D) -> bool:
	return value.origin.length() <= 1e-5 and value.basis.is_equal_approx(Basis.IDENTITY)

func _positive_integer(value: Variant) -> bool:
	if typeof(value) != TYPE_INT and typeof(value) != TYPE_FLOAT:
		return false
	var numeric := float(value)
	return is_finite(numeric) and numeric >= 1.0 and floor(numeric) == numeric

func _find_all(node: Node, target: String) -> Array[Node]:
	var found: Array[Node] = []
	if node.name == target:
		found.append(node)
	for child in node.get_children():
		found.append_array(_find_all(child, target))
	return found

func _collect_meshes(node: Node, output: Array[MeshInstance3D]) -> void:
	if node is MeshInstance3D:
		output.append(node as MeshInstance3D)
	for child in node.get_children():
		_collect_meshes(child, output)

func _contains_semantic_node(node: Node) -> bool:
	if node.name.begins_with("PART_") or node.name.begins_with("SOCKET_"):
		return true
	for child in node.get_children():
		if _contains_semantic_node(child):
			return true
	return false

func _record_tree(node: Node) -> Dictionary:
	var value := {"name": node.name, "class": node.get_class(), "children": []}
	if node is Node3D:
		value["position"] = _vec_array((node as Node3D).transform.origin)
	for child in node.get_children():
		value.children.append(_record_tree(child))
	return value

func _character_bounds(node: Node) -> AABB:
	var meshes: Array[MeshInstance3D] = []
	_collect_meshes(node, meshes)
	var found := false
	var result := AABB()
	for mesh in meshes:
		if not mesh.name.ends_with("_LOD0") or mesh.mesh == null:
			continue
		var transformed := mesh.global_transform * mesh.mesh.get_aabb()
		result = transformed if not found else result.merge(transformed)
		found = true
	return result if found else AABB()

func _write_observation(passed: bool) -> void:
	var path := str(request.get("observation_path", ""))
	if path.is_empty() or not _is_safe_path(path) or not path.ends_with(".json"):
		return
	var base_dir := path.get_base_dir()
	if not base_dir.is_empty() and base_dir != "res://":
		if not _is_safe_path(base_dir):
			return
		DirAccess.make_dir_recursive_absolute(base_dir)
	var value := {
		"schema_version": "character-runtime-observation-0.7.0",
		"workflow_id": request.get("workflow_id", ""),
		"revision": int(request.get("revision", 0)),
		"asset_id": request.get("asset_id", ""),
		"execution_id": request.get("execution_id", ""),
		"attempt_number": int(request.get("attempt_number", 0)),
		"raw_glb_sha256": request.get("raw_glb_sha256", ""),
		"processed_glb_sha256": request.get("processed_glb_sha256", ""),
		"harness_sha256": request.get("harness_sha256", ""),
		"profile_sha256": request.get("profile_sha256", ""),
		"specification_sha256": request.get("specification_sha256", ""),
		"request_digest": request.get("request_digest", ""),
		"status": "PASS" if passed else "FAIL",
		"import_tree": import_tree,
		"lods": lod_observations,
		"collider": collider_observation,
		"view_framing": view_framing,
		"captures": captures,
		"errors": failures,
	}
	var output := FileAccess.open(path, FileAccess.WRITE)
	if output == null:
		push_error("could not write runtime observation")
		return
	output.store_string(JSON.stringify(value, "\t") + "\n")
	output.close()

func _basis_rows(value: Basis) -> Array:
	return [
		[value.x.x, value.y.x, value.z.x],
		[value.x.y, value.y.y, value.z.y],
		[value.x.z, value.y.z, value.z.z],
	]

func _vec_array(value: Vector3) -> Array:
	return [value.x, value.y, value.z]

static func view_placement(angle: String) -> Dictionary:
	match angle:
		"front": return {"direction": Vector3(0, 0, -1), "up": Vector3.UP, "axis_label": "-Z"}
		"rear": return {"direction": Vector3(0, 0, 1), "up": Vector3.UP, "axis_label": "+Z"}
		"left": return {"direction": Vector3(-1, 0, 0), "up": Vector3.UP, "axis_label": "-X"}
		"right", "side": return {"direction": Vector3(1, 0, 0), "up": Vector3.UP, "axis_label": "+X"}
		"three_quarter", "three_quarter_front":
			return {"direction": Vector3(1, 0.65, -1).normalized(), "up": Vector3.UP, "axis_label": "+X-Z"}
		"three_quarter_rear":
			return {"direction": Vector3(1, 0.65, 1).normalized(), "up": Vector3.UP, "axis_label": "+X+Z"}
		"top": return {"direction": Vector3(0, 1, 0), "up": Vector3(0, 0, -1), "axis_label": "+Y"}
	return {}

func _view_direction_vector(angle: String) -> Vector3:
	var placement := view_placement(angle)
	return placement.get("direction", Vector3.ZERO)

func _view_up_vector(angle: String) -> Vector3:
	var placement := view_placement(angle)
	return placement.get("up", Vector3.UP)

func _framing_geometry(bounds: AABB, camera: Camera3D, angle: String, target_fraction: float) -> Dictionary:
	var d := _view_direction_vector(angle)
	var up := _view_up_vector(angle)
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
	var aspect: float = (viewport.x / maxf(viewport.y, 1.0)) if viewport.y > 0.0 else (1280.0 / 720.0)
	var d_c := e_f + maxf(e_u / (t * target_fraction), e_r / (t * target_fraction * aspect))

	var corners: Array[Vector3] = []
	for sx in [-1.0, 1.0]:
		for sy in [-1.0, 1.0]:
			for sz in [-1.0, 1.0]:
				corners.append(Vector3(sx * h.x, sy * h.y, sz * h.z))

	var low := e_f + minf(0.0001, (d_c - e_f) * 0.5)
	var high := d_c
	for _iter in 60:
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

	return {
		"distance": high,
		"conservative_distance": d_c,
		"projected_right_half": e_r,
		"projected_up_half": e_u,
		"view_depth_half": e_f,
	}

func _park_reference(reference: MeshInstance3D, bounds: AABB, direction: Vector3, extent: float) -> void:
	var center := bounds.get_center()
	var reference_half := 0.5
	if absf(direction.y) > 0.9:
		reference.position = Vector3(bounds.end.x + reference_half + extent + 0.25, bounds.position.y + reference_half, center.z)
		return
	var away := Vector3(-direction.x, 0.0, -direction.z)
	if away.length() < 0.001:
		away = Vector3(1, 0, 0)
	away = away.normalized()
	reference.position = Vector3(center.x, bounds.position.y + reference_half, center.z) + away * (extent + reference_half + 0.5)

func _projected_rect(camera: Camera3D, bounds: AABB) -> Rect2:
	var min_x := INF
	var min_y := INF
	var max_x := -INF
	var max_y := -INF
	var origin := bounds.position
	var size := bounds.size
	for k in 8:
		var corner := origin + Vector3(
			size.x if (k & 1) != 0 else 0.0,
			size.y if (k & 2) != 0 else 0.0,
			size.z if (k & 4) != 0 else 0.0
		)
		var screen := camera.unproject_position(corner)
		min_x = minf(min_x, screen.x)
		min_y = minf(min_y, screen.y)
		max_x = maxf(max_x, screen.x)
		max_y = maxf(max_y, screen.y)
	return Rect2(Vector2(min_x, min_y), Vector2(max_x - min_x, max_y - min_y))

func _measure_view_framing(camera: Camera3D, bounds: AABB, scale_reference: MeshInstance3D, viewport: Vector2, angle: String, min_fraction: float, max_fraction: float, margin_fraction: float) -> Dictionary:
	var rect := _projected_rect(camera, bounds)
	var margin_x := viewport.x * margin_fraction
	var margin_y := viewport.y * margin_fraction
	var max_x := rect.position.x + rect.size.x
	var max_y := rect.position.y + rect.size.y
	var inside := rect.position.x >= margin_x and rect.position.y >= margin_y and max_x <= viewport.x - margin_x and max_y <= viewport.y - margin_y
	var height_ratio := rect.size.y / viewport.y
	var center_x := rect.position.x + rect.size.x * 0.5
	var center_offset := absf(center_x - viewport.x * 0.5) / viewport.x
	var centered := center_offset <= 0.08

	var reference_between := false
	var to_asset := bounds.get_center() - camera.position
	var to_reference := scale_reference.position - camera.position
	var denom := to_asset.length_squared()
	if denom > 0.0001:
		var along := to_reference.dot(to_asset) / denom
		if along > 0.05 and along < 0.95:
			var closest := camera.position + to_asset * along
			if closest.distance_to(scale_reference.position) < 0.55:
				reference_between = true

	var placement := view_placement(angle)
	var axis: String = placement.get("axis_label", "")
	var reason := ""
	if bounds.has_point(camera.position):
		reason = "camera is inside asset geometry"
	elif not inside:
		reason = "asset bounds are outside the viewport safety margin"
	elif height_ratio < min_fraction or height_ratio > max_fraction:
		reason = "asset height fraction is outside the profile framing range"
	elif not centered:
		reason = "asset is not horizontally centered"
	elif reference_between:
		reason = "reference object sits between the camera and the asset"

	return {
		"ok": reason == "",
		"reason": reason,
		"inside_viewport": inside and not bounds.has_point(camera.position),
		"margin_ok": inside,
		"margin_fraction": margin_fraction,
		"height_ratio": height_ratio,
		"horizontally_centered": centered,
		"center_offset_ratio": center_offset,
		"reference_between_camera_and_asset": reference_between,
		"view_axis": axis,
	}

func _add_floor(bounds: AABB, extent: float) -> MeshInstance3D:
	var floor_node := MeshInstance3D.new()
	floor_node.name = "NeutralFloor"
	var plane := PlaneMesh.new()
	plane.size = Vector2(extent * 6.0, extent * 6.0)
	floor_node.mesh = plane
	floor_node.position = Vector3(bounds.get_center().x, bounds.position.y - 0.03, bounds.get_center().z)
	var material := StandardMaterial3D.new()
	material.albedo_color = Color("59616d")
	floor_node.material_override = material
	world_root.add_child(floor_node)

	var scale_reference := MeshInstance3D.new()
	scale_reference.name = "OneMeterScaleReference"
	var reference_mesh := BoxMesh.new()
	reference_mesh.size = Vector3.ONE
	scale_reference.mesh = reference_mesh
	scale_reference.position = Vector3(bounds.end.x + extent, bounds.position.y + 0.5, bounds.get_center().z)
	var reference_material := StandardMaterial3D.new()
	reference_material.albedo_color = Color("ddb65b")
	scale_reference.material_override = reference_material
	world_root.add_child(scale_reference)
	return scale_reference

func _environment() -> WorldEnvironment:
	var env := Environment.new()
	env.background_mode = Environment.BG_COLOR
	env.background_color = Color("30343b")
	env.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	env.ambient_light_color = Color("c7d6ed")
	env.ambient_light_energy = 0.8
	var world_environment := WorldEnvironment.new()
	world_environment.environment = env
	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-35, -35, 0)
	world_environment.add_child(light)
	return world_environment

static func _has_unsafe_extension(s: String) -> bool:
	var lower := s.to_lower()
	var unsafe_exts := [
		".gd", ".godot", ".uid", ".tscn", ".scn", ".res", ".tres", ".import",
		".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".cmd", ".ps1",
		".py", ".vbs", ".bin", ".com", ".scr"
	]
	for ext in unsafe_exts:
		if lower.ends_with(ext) or lower.contains(ext + "/") or lower.contains(ext + "."):
			return true
	return false

static func _is_safe_path(p: Variant) -> bool:
	if typeof(p) != TYPE_STRING:
		return false
	var s := str(p)
	if s.is_empty() or s.to_utf8_buffer().has(0) or s.contains("\\") or s.contains("\r") or s.contains("\n"):
		return false
	if not s.begins_with("res://"):
		return false
	var sub := s.substr(6)
	if sub.is_empty() or sub.begins_with("/") or sub.ends_with("/") or sub.contains(":") or sub.contains("//"):
		return false
	var parts := sub.split("/")
	for part in parts:
		if part == ".." or part == "." or part.is_empty():
			return false
	if _has_unsafe_extension(s):
		return false
	return true


