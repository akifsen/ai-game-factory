extends SceneTree

## Isolated V0.4 asset observer. The only success oracle is this independent
## runtime observation plus fully decoded Godot-rendered PNGs.
const MAX_REQUEST_BYTES := 65536
var request: Dictionary
var failures: Array[String] = []
var visual: MeshInstance3D
var collider_proxy: MeshInstance3D
var world_root: Node3D
var physics_ray_hit := false
var physics_body_present := false
var area_present := false
var collision_shape_present := false
var side_framing := {}
var view_framing := {}
var request_has_profile := false

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 2 or args[0] != "--request":
		_fail("arguments must be --request <json>")
		quit(1)
		return
	var file := FileAccess.open(args[1], FileAccess.READ)
	if file == null or file.get_length() > MAX_REQUEST_BYTES:
		_fail("request is missing or exceeds size limit")
		quit(1)
		return
	request = JSON.parse_string(file.get_as_text())
	file.close()
	if typeof(request) != TYPE_DICTIONARY:
		_fail("request must be a JSON object")
		quit(1)
		return
	for key in ["workflow_id", "revision", "asset_id", "glb", "output_dir"]:
		if not request.has(key):
			_fail("request missing " + key)
			quit(1)
			return
	for key in ["execution_id", "attempt_number", "processed_glb_sha256", "observation_path"]:
		if not request.has(key):
			_fail("request missing " + key)
			quit(1)
			return
	var packed := load(str(request.glb)) as PackedScene
	if packed == null:
		_fail("Godot could not import/load the processed GLB")
		quit(1)
		return
	var imported := packed.instantiate()
	if imported == null:
		_fail("GLB instance failed")
		quit(1)
		return
	world_root = Node3D.new()
	world_root.name = "AssetVerificationWorld"
	root.add_child(world_root)
	var asset_root := Node3D.new()
	asset_root.name = "AssetUnderTest"
	world_root.add_child(asset_root)
	asset_root.add_child(imported)
	visual = _find_mesh(imported, "SM_" + str(request.asset_id) + "_LOD0")
	collider_proxy = _find_mesh(imported, "COL_" + str(request.asset_id))
	if visual == null or visual.mesh == null or visual.mesh.get_surface_count() == 0:
		_fail("LOD0 visible mesh with geometry is missing")
	if collider_proxy == null or collider_proxy.mesh == null or collider_proxy.mesh.get_surface_count() == 0:
		_fail("collider proxy mesh with geometry is missing")
	if not failures.is_empty():
		_write_observation(false, AABB())
		quit(1)
		return
	_hide_non_lod0(imported)
	var profile: Dictionary = {}
	if request.has("profile") and typeof(request.profile) == TYPE_DICTIONARY:
		profile = request.profile
		request_has_profile = true
	var body_kind := str(profile.get("body_kind", "static_body"))
	var require_ray := bool(profile.get("require_ray_hit", true))
	var framing_policy: Dictionary = {}
	if typeof(profile.get("framing", {})) == TYPE_DICTIONARY:
		framing_policy = profile.get("framing", {})
	var min_fraction := float(framing_policy.get("min_screen_fraction", 0.55))
	var max_fraction := float(framing_policy.get("max_screen_fraction", 0.75))
	var target_fraction := float(framing_policy.get("target_screen_fraction", 0.65))
	var margin_fraction := float(framing_policy.get("margin_fraction", 0.04))
	var body: Node3D
	if body_kind == "area":
		var area := Area3D.new()
		area.name = "Area3D"
		area.monitoring = true
		area.monitorable = true
		body = area
		area_present = true
	else:
		var static_body := StaticBody3D.new()
		static_body.name = "StaticBody3D"
		body = static_body
		physics_body_present = true
	var shape := CollisionShape3D.new()
	shape.name = "CollisionShape3D"
	var vertices := PackedVector3Array()
	for surface in range(collider_proxy.mesh.get_surface_count()):
		var arrays := collider_proxy.mesh.surface_get_arrays(surface)
		var points: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
		for point in points:
			vertices.append(asset_root.global_transform.affine_inverse() * collider_proxy.global_transform * point)
	var convex := ConvexPolygonShape3D.new()
	convex.points = vertices
	shape.shape = convex
	body.add_child(shape)
	asset_root.add_child(body)
	collision_shape_present = true
	var bounds := visual.mesh.get_aabb()
	var global_bounds := visual.global_transform * bounds
	if not global_bounds.size.is_finite() or global_bounds.size.x <= 0 or global_bounds.size.y <= 0 or global_bounds.size.z <= 0:
		_fail("runtime mesh bounds are invalid")
	if body.get_child_count() != 1 or shape.shape == null or vertices.size() < 4:
		_fail("runtime physics body/collision shape is invalid")
	if not failures.is_empty():
		_write_observation(false, global_bounds)
		quit(1)
		return
	world_root.add_child(_environment())
	var camera := Camera3D.new()
	camera.current = true
	camera.fov = float(framing_policy.get("fov_degrees", 38.0))
	world_root.add_child(camera)
	var extent: float = max(global_bounds.size.x, max(global_bounds.size.y, global_bounds.size.z))
	var center: Vector3 = global_bounds.get_center()
	var scale_reference := _add_floor(global_bounds, extent)
	var quarter := Vector3(1, 0.65, -1).normalized()
	var angles := {
		"front": Vector3(0, 0, -1),
		"three_quarter": quarter,
		"three_quarter_front": quarter,
		"side": Vector3(1, 0, 0),
		"top": Vector3(0, 1, 0),
	}
	var requested: Array = ["front", "three_quarter", "side"]
	if request.has("angles"):
		if typeof(request.angles) != TYPE_ARRAY or request.angles.is_empty():
			_fail("angles must be a non-empty list")
			_write_observation(false, global_bounds)
			quit(1)
			return
		requested = request.angles
	for angle in requested:
		if not angles.has(angle):
			_fail("unknown capture angle " + str(angle))
			continue
		var direction: Vector3 = angles[angle]
		_park_reference(scale_reference, global_bounds, direction, extent)
		var distance := _view_distance(global_bounds, camera, str(angle), target_fraction)
		var up_vector := Vector3.UP if str(angle) != "top" else Vector3(0, 0, -1)
		camera.position = center + direction * distance
		camera.look_at(center, up_vector)
		await process_frame
		var viewport := root.get_viewport().get_visible_rect().size
		var fitted := _projected_rect(camera, global_bounds)
		if viewport.y > 0.0 and fitted.size.y > 0.0:
			var ratio := fitted.size.y / viewport.y
			if ratio < min_fraction or ratio > max_fraction:
				distance *= ratio / target_fraction
				camera.position = center + direction * distance
				camera.look_at(center, up_vector)
				await process_frame
		await process_frame
		var image: Image = root.get_texture().get_image()
		if image == null or image.get_width() != 1280 or image.get_height() != 720:
			_fail("capture resolution is not 1280x720")
			continue
		var measured := _measure_view_framing(camera, global_bounds, scale_reference, Vector2(image.get_width(), image.get_height()), str(angle), min_fraction, max_fraction, margin_fraction)
		view_framing[str(angle)] = measured
		if str(angle) == "side":
			side_framing = measured
		if not measured.get("ok", false):
			_fail(str(measured.get("reason", "capture framing failed")))
			continue
		var filename := str(request.output_dir).path_join(angle + ".png")
		if image.save_png(filename) != OK:
			_fail("Godot renderer could not save " + angle + " capture")
	await physics_frame
	if require_ray:
		var query := PhysicsRayQueryParameters3D.create(center + Vector3(0, extent * 2.0, 0), center - Vector3(0, extent * 2.0, 0))
		var hit := world_root.get_world_3d().direct_space_state.intersect_ray(query)
		if hit.is_empty() or hit.get("collider") != body:
			_fail("physics ray did not hit the generated collider")
		else:
			physics_ray_hit = true
	if not failures.is_empty():
		_write_observation(false, global_bounds)
		quit(1)
		return
	_write_observation(true, global_bounds)
	quit(0)

func _find_mesh(node: Node, expected_name: String) -> MeshInstance3D:
	if node is MeshInstance3D and node.name == expected_name:
		return node as MeshInstance3D
	for child in node.get_children():
		var found := _find_mesh(child, expected_name)
		if found != null:
			return found
	return null

func _hide_non_lod0(node: Node) -> void:
	if node is MeshInstance3D:
		var mesh_node := node as MeshInstance3D
		mesh_node.visible = mesh_node.name == "SM_" + str(request.asset_id) + "_LOD0"
	for child in node.get_children():
		_hide_non_lod0(child)

func _side_view_distance(bounds: AABB, camera: Camera3D) -> float:
	return _view_distance(bounds, camera, "side", 0.65)

func _view_distance(bounds: AABB, camera: Camera3D, angle: String, target_fraction: float) -> float:
	var half_fov := deg_to_rad(camera.fov * 0.5)
	var vertical := bounds.size.y
	var horizontal := bounds.size.x
	if angle == "side":
		horizontal = bounds.size.z
	elif angle == "top":
		vertical = bounds.size.z
		horizontal = bounds.size.x
	elif angle != "front":
		var dominant: float = maxf(bounds.size.x, maxf(bounds.size.y, bounds.size.z))
		vertical = dominant
		horizontal = dominant
	var viewport: Vector2 = root.get_viewport().get_visible_rect().size
	var aspect: float = viewport.x / maxf(viewport.y, 1.0)
	var tangent: float = tan(half_fov)
	var distance_vertical: float = vertical / (2.0 * tangent * target_fraction)
	var distance_horizontal: float = horizontal / (2.0 * tangent * target_fraction * aspect)
	return maxf(distance_vertical, distance_horizontal)

func _park_reference(reference: MeshInstance3D, bounds: AABB, direction: Vector3, extent: float) -> void:
	var center := bounds.get_center()
	var reference_half := 0.5
	if abs(direction.y) > 0.9:
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
	var cube_half := 0.5
	var reference_between := false
	if angle == "side":
		reference_between = scale_reference.position.x + cube_half >= bounds.position.x - 0.05
	var to_asset := bounds.get_center() - camera.position
	var to_reference := scale_reference.position - camera.position
	var denom := to_asset.length_squared()
	if denom > 0.0001:
		var along := to_reference.dot(to_asset) / denom
		if along > 0.05 and along < 0.95:
			var closest := camera.position + to_asset * along
			if closest.distance_to(scale_reference.position) < 0.55:
				reference_between = true
	var axis := "+X-Z"
	if angle == "side":
		axis = "+X"
	elif angle == "front":
		axis = "-Z"
	elif angle == "top":
		axis = "+Y"
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
	var floor := MeshInstance3D.new()
	floor.name = "NeutralFloor"
	var plane := PlaneMesh.new()
	plane.size = Vector2(extent * 6.0, extent * 6.0)
	floor.mesh = plane
	floor.position = Vector3(bounds.get_center().x, bounds.position.y - 0.03, bounds.get_center().z)
	var material := StandardMaterial3D.new()
	material.albedo_color = Color("59616d")
	floor.material_override = material
	world_root.add_child(floor)
	var scale_reference := MeshInstance3D.new()
	scale_reference.name = "OneMeterScaleReference"
	var reference_mesh := BoxMesh.new()
	reference_mesh.size = Vector3.ONE
	scale_reference.mesh = reference_mesh
	scale_reference.position = Vector3(bounds.end.x + extent, 0.5, bounds.get_center().z)
	var reference_material := StandardMaterial3D.new()
	reference_material.albedo_color = Color("ddb65b")
	scale_reference.material_override = reference_material
	world_root.add_child(scale_reference)
	return scale_reference

func _environment() -> WorldEnvironment:
	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color("30343b")
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.ambient_light_color = Color("c7d6ed")
	environment.ambient_light_energy = 0.8
	var world_environment := WorldEnvironment.new()
	world_environment.environment = environment
	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-35, -35, 0)
	world_environment.add_child(light)
	return world_environment

func _write_observation(passed: bool, bounds: AABB) -> void:
	var observation := {
		"schema_version": "asset-runtime-observation-0.5.0" if request_has_profile else "asset-runtime-observation-0.4.0",
		"workflow_id": request.get("workflow_id", ""),
		"revision": int(request.get("revision", 0)),
		"asset_id": request.get("asset_id", ""),
		"execution_id": request.get("execution_id", ""),
		"attempt_number": int(request.get("attempt_number", 0)),
		"processed_glb_sha256": request.get("processed_glb_sha256", ""),
		"status": "PASS" if passed else "FAIL",
		"mesh_visible": passed and visual != null and visual.visible,
		"mesh_name": visual.name if visual != null else "",
		"mesh_bounds": {"position": [bounds.position.x, bounds.position.y, bounds.position.z], "size": [bounds.size.x, bounds.size.y, bounds.size.z]},
		"physics_body_present": passed and physics_body_present,
		"area_present": passed and area_present,
		"collision_shape_present": passed and collision_shape_present,
		"physics_ray_hit": physics_ray_hit,
		"body_kind": "area" if area_present else "static_body",
		"collider_proxy_name": collider_proxy.name if collider_proxy != null else "",
		"errors": failures,
	}
	if not view_framing.is_empty():
		observation["view_framing"] = view_framing
	if not side_framing.is_empty():
		observation["side_framing"] = {
			"inside_viewport": side_framing.get("inside_viewport", false),
			"margin_ok": side_framing.get("margin_ok", false),
			"margin_fraction": side_framing.get("margin_fraction", 0.04),
			"height_ratio": side_framing.get("height_ratio", 0.0),
			"horizontally_centered": side_framing.get("horizontally_centered", false),
			"center_offset_ratio": side_framing.get("center_offset_ratio", 1.0),
			"reference_between_camera_and_asset": side_framing.get("reference_between_camera_and_asset", true),
			"view_axis": side_framing.get("view_axis", ""),
		}
	var output := FileAccess.open(str(request.get("observation_path", "")), FileAccess.WRITE)
	if output == null:
		push_error("cannot write runtime observation")
		return
	output.store_string(JSON.stringify(observation, "\t") + "\n")
	output.close()

func _fail(message: String) -> void:
	failures.append(message)
	push_error(message)
