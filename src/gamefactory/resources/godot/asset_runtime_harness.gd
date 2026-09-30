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
## asset-runtime-observation-0.7.0 state (present only when the request carries
## contract_v07; historical requests keep the 0.4.0/0.5.0 behavior unchanged).
var contract_v07: Dictionary = {}
var geometry_mode := "single_mesh"
var collider_policy := "box"
var visuals: Array[MeshInstance3D] = []
var hierarchy_report := {}
var articulation_report: Array = []
var capsule_report := {}
var collision_shape_class := ""
## ADR 0014: every named review view has one placement. Unknown views fail;
## there is no fallback. The asset faces -Z with +Y up, so its right is +X.
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
	if request.has("contract_v07") and typeof(request.contract_v07) == TYPE_DICTIONARY:
		contract_v07 = request.contract_v07
		geometry_mode = str(contract_v07.get("geometry_mode", "single_mesh"))
		collider_policy = str(contract_v07.get("collider_policy", "box"))
	if contract_v07.is_empty():
		visual = _find_mesh(imported, "SM_" + str(request.asset_id) + "_LOD0")
		collider_proxy = _find_mesh(imported, "COL_" + str(request.asset_id))
		if visual == null or visual.mesh == null or visual.mesh.get_surface_count() == 0:
			_fail("LOD0 visible mesh with geometry is missing")
		if collider_proxy == null or collider_proxy.mesh == null or collider_proxy.mesh.get_surface_count() == 0:
			_fail("collider proxy mesh with geometry is missing")
		if visual != null:
			visuals.append(visual)
	else:
		for mesh_name in _lod0_names():
			var found := _find_mesh(imported, mesh_name)
			if found == null or found.mesh == null or found.mesh.get_surface_count() == 0:
				_fail("LOD0 visible mesh with geometry is missing: " + mesh_name)
			else:
				visuals.append(found)
		if not visuals.is_empty():
			visual = visuals[0]
		collider_proxy = _find_mesh(imported, "COL_" + str(request.asset_id))
		if collider_policy == "box":
			if collider_proxy == null or collider_proxy.mesh == null or collider_proxy.mesh.get_surface_count() == 0:
				_fail("collider proxy mesh with geometry is missing")
		elif collider_policy == "capsule":
			if collider_proxy != null:
				_fail("capsule assets must not carry a collider mesh")
		else:
			_fail("unsupported collider policy " + collider_policy)
		if geometry_mode == "assembly":
			_verify_hierarchy(imported)
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
	if collider_policy == "capsule":
		var declared: Dictionary = contract_v07.get("capsule", {}) if typeof(contract_v07.get("capsule", {})) == TYPE_DICTIONARY else {}
		var capsule := CapsuleShape3D.new()
		capsule.radius = float(declared.get("radius_m", 0.0))
		capsule.height = float(declared.get("height_m", 0.0))
		shape.shape = capsule
		var origin_policy := str(contract_v07.get("origin_policy", "bottom_center"))
		shape.position = Vector3(0, capsule.height * 0.5, 0) if origin_policy == "bottom_center" else Vector3.ZERO
		collision_shape_class = "CapsuleShape3D"
	else:
		for surface in range(collider_proxy.mesh.get_surface_count()):
			var arrays := collider_proxy.mesh.surface_get_arrays(surface)
			var points: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
			for point in points:
				vertices.append(asset_root.global_transform.affine_inverse() * collider_proxy.global_transform * point)
		var convex := ConvexPolygonShape3D.new()
		convex.points = vertices
		shape.shape = convex
		collision_shape_class = "BoxShape3D"
	body.add_child(shape)
	asset_root.add_child(body)
	collision_shape_present = true
	var bounds := visual.mesh.get_aabb()
	var global_bounds := visual.global_transform * bounds
	for index in range(1, visuals.size()):
		global_bounds = global_bounds.merge(visuals[index].global_transform * visuals[index].mesh.get_aabb())
	if not global_bounds.size.is_finite() or global_bounds.size.x <= 0 or global_bounds.size.y <= 0 or global_bounds.size.z <= 0:
		_fail("runtime mesh bounds are invalid")
	if collider_policy == "capsule":
		_check_capsule(shape, global_bounds)
		if body.get_child_count() != 1 or shape.shape == null:
			_fail("runtime physics body/collision shape is invalid")
	elif body.get_child_count() != 1 or shape.shape == null or vertices.size() < 4:
		_fail("runtime physics body/collision shape is invalid")
	if geometry_mode == "assembly" and failures.is_empty():
		_articulate(imported)
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
	var requested: Array = ["front", "three_quarter", "side"]
	if request.has("angles"):
		if typeof(request.angles) != TYPE_ARRAY or request.angles.is_empty():
			_fail("angles must be a non-empty list")
			_write_observation(false, global_bounds)
			quit(1)
			return
		requested = request.angles
	for angle in requested:
		if not VIEW_TABLE.has(str(angle)):
			_fail("unknown capture angle " + str(angle))
			continue
		var direction: Vector3 = _view_direction_vector(str(angle))
		_park_reference(scale_reference, global_bounds, direction, extent)
		var geom := _framing_geometry(global_bounds, camera, str(angle), target_fraction)
		var distance: float = geom["distance"]
		var conservative_dist: float = geom["conservative_distance"]
		var e_r: float = geom["projected_right_half"]
		var e_u: float = geom["projected_up_half"]
		var e_f: float = geom["view_depth_half"]
		var up_vector := Vector3.UP if str(angle) != "top" else Vector3(0, 0, -1)
		camera.position = center + direction * distance
		camera.look_at(center, up_vector)
		await process_frame
		var viewport := root.get_viewport().get_visible_rect().size
		var fitted := _projected_rect(camera, global_bounds)
		var initial_height_ratio := 0.0
		var correction_applied := false
		if viewport.y > 0.0 and fitted.size.y > 0.0:
			initial_height_ratio = fitted.size.y / viewport.y
			if not contract_v07.is_empty() and viewport.x > 0.0:
				# asset-runtime-observation-0.7.0 frames the dominant screen extent,
				# as the distance solver does, so elongated assets are not overfilled.
				initial_height_ratio = maxf(initial_height_ratio, fitted.size.x / viewport.x)
			if initial_height_ratio < min_fraction or initial_height_ratio > max_fraction:
				var near := maxf(distance - e_f, 0.001)
				distance = maxf(e_f + 0.01, e_f + near * initial_height_ratio / target_fraction)
				correction_applied = true
				camera.position = center + direction * distance
				camera.look_at(center, up_vector)
				await process_frame
		await process_frame
		var image: Image = root.get_texture().get_image()
		if image == null or image.get_width() != 1280 or image.get_height() != 720:
			_fail("capture resolution is not 1280x720")
			continue
		var measured := _measure_view_framing(camera, global_bounds, scale_reference, Vector2(image.get_width(), image.get_height()), str(angle), min_fraction, max_fraction, margin_fraction)
		measured["camera_distance"] = distance
		measured["conservative_distance"] = conservative_dist
		measured["projected_right_half"] = e_r
		measured["projected_up_half"] = e_u
		measured["view_depth_half"] = e_f
		measured["initial_height_ratio"] = initial_height_ratio
		measured["correction_applied"] = correction_applied
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

func _lod0_names() -> Array[String]:
	var names: Array[String] = []
	if geometry_mode == "assembly":
		for entry in contract_v07.get("hierarchy", []):
			names.append("SM_" + str(request.asset_id) + "_" + str(entry.get("part_id", "")) + "_LOD0")
	else:
		names.append("SM_" + str(request.asset_id) + "_LOD0")
	return names

func _hide_non_lod0(node: Node) -> void:
	if node is MeshInstance3D:
		var mesh_node := node as MeshInstance3D
		if contract_v07.is_empty():
			mesh_node.visible = mesh_node.name == "SM_" + str(request.asset_id) + "_LOD0"
		else:
			mesh_node.visible = _lod0_names().has(str(mesh_node.name))
	for child in node.get_children():
		_hide_non_lod0(child)

func _find_node(node: Node, expected_name: String) -> Node:
	if str(node.name) == expected_name:
		return node
	for child in node.get_children():
		var found := _find_node(child, expected_name)
		if found != null:
			return found
	return null

func _vec3(value: Variant) -> Vector3:
	if typeof(value) != TYPE_ARRAY or (value as Array).size() != 3:
		return Vector3.ZERO
	var items: Array = value
	return Vector3(float(items[0]), float(items[1]), float(items[2]))

func _quat(value: Variant) -> Quaternion:
	if typeof(value) != TYPE_ARRAY or (value as Array).size() != 4:
		return Quaternion.IDENTITY
	var items: Array = value
	return Quaternion(float(items[0]), float(items[1]), float(items[2]), float(items[3])).normalized()

func _descendants(node: Node) -> Array[Node3D]:
	var result: Array[Node3D] = []
	for child in node.get_children():
		if child is Node3D:
			result.append(child as Node3D)
		result.append_array(_descendants(child))
	return result

## ADR 0013: the imported PART_ tree must equal the declared hierarchy, pivots
## and sockets. Sockets are replaced by Marker3D nodes with the same transform.
func _verify_hierarchy(imported: Node) -> void:
	var pivot_tol := float(contract_v07.get("pivot_tolerance_m", 0.005))
	var basis_tol := float(contract_v07.get("basis_tolerance_deg", 0.5))
	var socket_tol := float(contract_v07.get("socket_tolerance_m", 0.01))
	var socket_deg := float(contract_v07.get("socket_angle_tolerance_deg", 2.0))
	var root_node := _find_node(imported, "ROOT") as Node3D
	var ok := root_node != null
	if root_node == null:
		_fail("assembly ROOT node is missing after import")
	var parts: Array = []
	for entry in contract_v07.get("hierarchy", []):
		var node_name := str(entry.get("node", ""))
		var node := _find_node(imported, node_name) as Node3D
		var pivot: Dictionary = entry.get("pivot", {})
		var row := {"part_id": str(entry.get("part_id", "")), "node": node_name, "present": node != null}
		if node == null:
			ok = false
			_fail("assembly part node is missing after import: " + node_name)
			parts.append(row)
			continue
		var parent_name := str(node.get_parent().name) if node.get_parent() != null else ""
		var declared_position := _vec3(pivot.get("position_m", []))
		var declared_basis := _quat(pivot.get("basis_quaternion_xyzw", []))
		var angle := rad_to_deg(node.basis.orthonormalized().get_rotation_quaternion().angle_to(declared_basis))
		row["parent"] = parent_name
		row["parent_ok"] = parent_name == str(entry.get("parent_node", ""))
		row["position"] = [node.position.x, node.position.y, node.position.z]
		row["position_ok"] = node.position.distance_to(declared_position) <= pivot_tol
		row["basis_angle_deg"] = angle
		row["basis_ok"] = angle <= basis_tol
		var meshes_ok := true
		for mesh_name in entry.get("meshes", []):
			var mesh_child := node.get_node_or_null(NodePath(str(mesh_name)))
			if mesh_child == null or not (mesh_child is MeshInstance3D):
				meshes_ok = false
		row["meshes_ok"] = meshes_ok
		var part_ok: bool = row["parent_ok"] and row["position_ok"] and row["basis_ok"] and meshes_ok
		row["ok"] = part_ok
		if not part_ok:
			ok = false
			_fail("assembly part does not match its declared pivot/tree: " + node_name)
		parts.append(row)
	var sockets: Array = []
	for entry in contract_v07.get("sockets", []):
		var node_name := str(entry.get("node", ""))
		var node := _find_node(imported, node_name) as Node3D
		var row := {"socket_id": str(entry.get("socket_id", "")), "node": node_name, "present": node != null}
		if node == null:
			ok = false
			_fail("socket node is missing after import: " + node_name)
			sockets.append(row)
			continue
		var parent := node.get_parent()
		var parent_name := str(parent.name) if parent != null else ""
		var declared := _vec3(entry.get("translation_m", []))
		var forward := -node.global_transform.basis.z.normalized()
		var rest_forward := _vec3(entry.get("rest_forward", [])) if entry.get("rest_forward") != null else forward
		var forward_angle := rad_to_deg(forward.angle_to(rest_forward.normalized())) if rest_forward.length() > 0.0 else 0.0
		row["parent"] = parent_name
		row["parent_ok"] = parent_name == str(entry.get("parent_node", ""))
		row["position_ok"] = node.position.distance_to(declared) <= socket_tol
		row["children"] = node.get_child_count()
		row["forward_world"] = [forward.x, forward.y, forward.z]
		row["forward_angle_deg"] = forward_angle
		row["forward_ok"] = forward_angle <= socket_deg
		var socket_ok: bool = row["parent_ok"] and row["position_ok"] and row["forward_ok"] and node.get_child_count() == 0 and not (node is MeshInstance3D)
		row["ok"] = socket_ok
		if not socket_ok:
			ok = false
			_fail("socket does not match its declared frame: " + node_name)
		var marker := Marker3D.new()
		marker.name = node_name
		marker.transform = node.transform
		parent.remove_child(node)
		node.free()
		parent.add_child(marker)
		row["node_class"] = marker.get_class()
		sockets.append(row)
	hierarchy_report = {"ok": ok and failures.is_empty(), "root_present": root_node != null, "parts": parts, "sockets": sockets}

## Move every non-fixed part about its declared pivot/axis, assert the pivot
## stays put (revolute) or moves along the axis (prismatic), that descendants and
## sockets follow rigidly and that everything else is untouched, then restore.
func _articulate(imported: Node) -> void:
	var root_node := _find_node(imported, "ROOT") as Node3D
	if root_node == null:
		return
	var everything: Array[Node3D] = [root_node]
	everything.append_array(_descendants(root_node))
	for entry in contract_v07.get("hierarchy", []):
		var pivot: Dictionary = entry.get("pivot", {})
		var motion := str(pivot.get("motion", "fixed"))
		if motion == "fixed":
			continue
		var node := _find_node(imported, str(entry.get("node", ""))) as Node3D
		if node == null:
			continue
		var axis := _vec3(pivot.get("axis", [])).normalized()
		var before := {}
		for item in everything:
			before[item] = item.global_transform
		var moving: Array[Node3D] = [node]
		moving.append_array(_descendants(node))
		var relative := {}
		for item in moving:
			relative[item] = node.global_transform.affine_inverse() * item.global_transform
		var rest := node.transform
		var pivot_before := node.global_position
		var expected_pivot := pivot_before
		if motion == "revolute":
			node.basis = node.basis * Basis(axis, deg_to_rad(15.0))
		else:
			var parent_basis := (node.get_parent() as Node3D).global_transform.basis
			expected_pivot = pivot_before + parent_basis * (rest.basis * (axis * 0.05))
			node.position += rest.basis * (axis * 0.05)
		var moved := not node.global_transform.is_equal_approx(before[node])
		var pivot_ok := node.global_position.distance_to(expected_pivot) <= 0.0001
		var rigid := true
		for item in moving:
			if not (node.global_transform.affine_inverse() * item.global_transform).is_equal_approx(relative[item]):
				rigid = false
		var others := true
		for item in everything:
			if moving.has(item):
				continue
			if not item.global_transform.is_equal_approx(before[item]):
				others = false
		node.transform = rest
		var restored := node.global_transform.is_equal_approx(before[node])
		var row_ok := moved and pivot_ok and rigid and others and restored
		articulation_report.append({
			"part_id": str(entry.get("part_id", "")),
			"motion": motion,
			"axis": [axis.x, axis.y, axis.z],
			"moved": moved,
			"pivot_ok": pivot_ok,
			"descendants_rigid": rigid,
			"others_unchanged": others,
			"restored": restored,
			"ok": row_ok,
		})
		if not row_ok:
			_fail("articulation check failed for " + str(entry.get("node", "")))

func _check_capsule(shape: CollisionShape3D, global_bounds: AABB) -> void:
	var capsule := shape.shape as CapsuleShape3D
	var declared: Dictionary = contract_v07.get("capsule", {}) if typeof(contract_v07.get("capsule", {})) == TYPE_DICTIONARY else {}
	var tol := float(contract_v07.get("dimension_tolerance_m", 0.02))
	var radius := float(declared.get("radius_m", 0.0))
	var height := float(declared.get("height_m", 0.0))
	var matches := capsule != null and absf(capsule.radius - radius) <= 1e-6 and absf(capsule.height - height) <= 1e-6
	var fits := height <= global_bounds.size.y + tol and 2.0 * radius <= maxf(global_bounds.size.x, global_bounds.size.z) + tol
	capsule_report = {
		"declared": {"radius_m": radius, "height_m": height},
		"observed": {"radius_m": capsule.radius if capsule != null else 0.0, "height_m": capsule.height if capsule != null else 0.0},
		"axis": "+Y",
		"center": [shape.position.x, shape.position.y, shape.position.z],
		"shape_ok": matches,
		"fit_ok": fits,
		"ok": matches and fits,
	}
	if not matches:
		_fail("capsule shape differs from the declared contract")
	if not fits:
		_fail("capsule does not fit the runtime mesh bounds")

func _view_direction_vector(angle: String) -> Vector3:
	if not VIEW_TABLE.has(angle):
		_fail("view has no camera placement: " + angle)
		return Vector3.ZERO
	var entry: Array = VIEW_TABLE[angle]
	return (entry[0] as Vector3).normalized()

func _view_axis_label(angle: String) -> String:
	if not VIEW_TABLE.has(angle):
		_fail("view has no camera placement: " + angle)
		return ""
	var entry: Array = VIEW_TABLE[angle]
	return str(entry[1])

func _view_up_vector(angle: String) -> Vector3:
	if angle == "top":
		return Vector3(0, 0, -1)
	return Vector3.UP

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

	return {
		"distance": high,
		"conservative_distance": d_c,
		"projected_right_half": e_r,
		"projected_up_half": e_u,
		"view_depth_half": e_f,
	}

func _side_view_distance(bounds: AABB, camera: Camera3D) -> float:
	return _view_distance(bounds, camera, "side", 0.65)

func _view_distance(bounds: AABB, camera: Camera3D, angle: String, target_fraction: float) -> float:
	var geom := _framing_geometry(bounds, camera, angle, target_fraction)
	return float(geom.get("distance", 0.0))

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
	var fill_ratio := maxf(height_ratio, rect.size.x / viewport.x)
	var framed_ratio := height_ratio if contract_v07.is_empty() else fill_ratio
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
	var axis := _view_axis_label(angle)
	var reason := ""
	if bounds.has_point(camera.position):
		reason = "camera is inside asset geometry"
	elif not inside:
		reason = "asset bounds are outside the viewport safety margin"
	elif framed_ratio < min_fraction or framed_ratio > max_fraction:
		reason = "asset height fraction is outside the profile framing range" if contract_v07.is_empty() else "asset screen fill is outside the profile framing range"
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
		"fill_ratio": fill_ratio,
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
	var schema := "asset-runtime-observation-0.5.0" if request_has_profile else "asset-runtime-observation-0.4.0"
	if not contract_v07.is_empty():
		schema = "asset-runtime-observation-0.7.0"
	var observation := {
		"schema_version": schema,
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
	if not contract_v07.is_empty():
		var names: Array = []
		for item in visuals:
			names.append(str(item.name))
		observation["geometry_mode"] = geometry_mode
		observation["collider_policy"] = collider_policy
		observation["collision_shape_class"] = collision_shape_class
		observation["mesh_names"] = names
		if geometry_mode == "assembly":
			observation["hierarchy"] = hierarchy_report
			observation["articulation"] = articulation_report
		if collider_policy == "capsule":
			observation["capsule"] = capsule_report
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
