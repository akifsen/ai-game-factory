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
	var body := StaticBody3D.new()
	body.name = "VerifiedPhysicsBody"
	var shape := CollisionShape3D.new()
	shape.name = "VerifiedCollisionShape"
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
	world_root.add_child(camera)
	var extent: float = max(global_bounds.size.x, max(global_bounds.size.y, global_bounds.size.z))
	var center: Vector3 = global_bounds.get_center()
	_add_floor(global_bounds, extent)
	var angles := {"front": Vector3(0, 0, -1), "three_quarter": Vector3(1, 0.65, -1).normalized(), "side": Vector3(1, 0, 0)}
	for angle in ["front", "three_quarter", "side"]:
		var direction: Vector3 = angles[angle]
		camera.position = center + direction * extent * 2.8
		camera.look_at(center, Vector3.UP)
		camera.fov = 38
		await process_frame
		await process_frame
		var image: Image = root.get_texture().get_image()
		var filename := str(request.output_dir).path_join(angle + ".png")
		if image.save_png(filename) != OK:
			_fail("Godot renderer could not save " + angle + " capture")
	await physics_frame
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

func _add_floor(bounds: AABB, extent: float) -> void:
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
		"schema_version": "asset-runtime-observation-0.4.0",
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
		"physics_body_present": passed and root.find_child("VerifiedPhysicsBody", true, false) != null,
		"collision_shape_present": passed and root.find_child("VerifiedCollisionShape", true, false) != null,
		"physics_ray_hit": physics_ray_hit,
		"collider_proxy_name": collider_proxy.name if collider_proxy != null else "",
		"errors": failures,
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
