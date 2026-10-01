extends SceneTree

## Internal skin deformation oracle: bake mesh from skeleton pose in real Godot.

const MAX_REQUEST_BYTES := 65536
const MAX_VERTEX_SAMPLES := 4096
const REGION_BOUNDARY_TOLERANCE := 1e-6
const RUNTIME_REQUEST_INT_FIELDS := [
	"vertex_count",
	"affected_vertex_count",
	"unaffected_vertex_count",
]

var _output_path := ""

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 2 or args[0] != "--request":
		_write_result({"status": "FAIL", "reason": "arguments must be --request <json>", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	var file := FileAccess.open(args[1], FileAccess.READ)
	if file == null or file.get_length() > MAX_REQUEST_BYTES:
		_write_result({"status": "FAIL", "reason": "request is missing or exceeds size limit", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	var request: Variant = JSON.parse_string(file.get_as_text())
	file.close()
	if typeof(request) != TYPE_DICTIONARY:
		quit(1)
		return
	if request.has("output_path"):
		_output_path = str(request.output_path)
	for key in [
		"schema_version",
		"glb",
		"output_path",
		"glb_sha256",
		"contract_sha256",
		"harness_sha256",
		"godot_version",
		"pose_bone",
		"rotation_axis",
		"rotation_degrees",
		"affected",
		"unaffected",
		"min_affected_displacement",
		"max_unaffected_displacement",
		"max_affected_displacement",
		"vertex_count",
		"affected_vertex_count",
		"unaffected_vertex_count",
		"region_boundary_tolerance",
	]:
		if not request.has(key):
			_write_result({"status": "FAIL", "reason": "request missing " + key, "method": "bake_mesh_from_current_skeleton_pose"})
			return
	var packed := load(str(request.glb)) as PackedScene
	if packed == null:
		_write_result({"status": "FAIL", "reason": "Godot could not import GLB", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	var imported := packed.instantiate()
	if imported == null:
		_write_result({"status": "FAIL", "reason": "GLB instance failed", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	root.add_child(imported)
	for _i in range(3):
		await process_frame
	var mesh_instance := _find_skinned_mesh(imported)
	if mesh_instance == null:
		_write_result({
			"status": "FAIL",
			"reason": "skinned MeshInstance3D not found",
			"method": "bake_mesh_from_current_skeleton_pose",
			"scene_nodes": _describe_tree(imported),
		})
		return
	var skeleton_path: NodePath = mesh_instance.skeleton
	if skeleton_path.is_empty():
		_write_result({"status": "FAIL", "reason": "skeleton NodePath missing on skinned mesh", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	var skeleton := mesh_instance.get_node(skeleton_path) as Skeleton3D
	if skeleton == null:
		_write_result({"status": "FAIL", "reason": "skeleton node not resolved from NodePath", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	var bone_idx := skeleton.find_bone(str(request.pose_bone))
	if bone_idx < 0:
		_write_result({"status": "FAIL", "reason": "pose bone not found: " + str(request.pose_bone), "method": "bake_mesh_from_current_skeleton_pose"})
		return
	if mesh_instance.skin == null:
		_write_result({
			"status": "FAIL",
			"reason": "MeshInstance3D has no Skin resource after import",
			"method": "bake_mesh_from_current_skeleton_pose",
			"scene_nodes": _describe_tree(imported),
		})
		return
	var rest_mesh := mesh_instance.bake_mesh_from_current_skeleton_pose()
	if rest_mesh == null:
		_write_result({
			"status": "FAIL",
			"reason": "bake_mesh_from_current_skeleton_pose returned null at rest",
			"method": "bake_mesh_from_current_skeleton_pose",
			"scene_nodes": _describe_tree(imported),
			"mesh_surface_count": mesh_instance.mesh.get_surface_count() if mesh_instance.mesh != null else 0,
		})
		return
	var rest := _mesh_surface_vertices(rest_mesh)
	var axis: Vector3 = Vector3(float(request.rotation_axis[0]), float(request.rotation_axis[1]), float(request.rotation_axis[2]))
	if axis.length_squared() < 1e-12:
		_write_result({"status": "FAIL", "reason": "rotation_axis is zero", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	axis = axis.normalized()
	var radians := deg_to_rad(float(request.rotation_degrees))
	var pose_quat := Quaternion(axis, radians)
	skeleton.set_bone_pose_rotation(bone_idx, pose_quat)
	skeleton.force_update_bone_child_transform(bone_idx)
	await process_frame
	var posed_mesh := mesh_instance.bake_mesh_from_current_skeleton_pose()
	if posed_mesh == null:
		_write_result({"status": "FAIL", "reason": "bake_mesh_from_current_skeleton_pose returned null after pose", "method": "bake_mesh_from_current_skeleton_pose"})
		return
	var posed := _mesh_surface_vertices(posed_mesh)
	if rest.size() != posed.size() or rest.size() == 0:
		_write_result({"status": "FAIL", "reason": "baked vertex count mismatch or empty", "method": "bake_mesh_from_current_skeleton_pose", "vertex_count": mini(rest.size(), posed.size())})
		return
	if int(request.vertex_count) != rest.size():
		_write_result({"status": "FAIL", "reason": "request vertex_count does not match baked geometry", "method": "bake_mesh_from_current_skeleton_pose", "vertex_count": rest.size()})
		return
	var region_tol := _strict_region_boundary_tolerance(request.region_boundary_tolerance)
	var affected: Dictionary = request.affected
	var unaffected: Dictionary = request.unaffected
	var max_affected := 0.0
	var max_unaffected := 0.0
	var affected_count := 0
	var unaffected_count := 0
	for i in range(rest.size()):
		var delta := (posed[i] - rest[i]).length()
		var p := rest[i]
		if _inside(p, affected, region_tol):
			affected_count += 1
			max_affected = maxf(max_affected, delta)
		if _inside(p, unaffected, region_tol):
			unaffected_count += 1
			max_unaffected = maxf(max_unaffected, delta)
	if affected_count == 0 or unaffected_count == 0:
		_write_result({
			"status": "FAIL",
			"reason": "fixed regions must contain vertices",
			"method": "bake_mesh_from_current_skeleton_pose",
			"affected_vertex_count": affected_count,
			"unaffected_vertex_count": unaffected_count,
			"vertex_count": rest.size(),
		})
		return
	if int(request.affected_vertex_count) != affected_count or int(request.unaffected_vertex_count) != unaffected_count:
		_write_result({
			"status": "FAIL",
			"reason": "request region populations do not match baked rest geometry",
			"method": "bake_mesh_from_current_skeleton_pose",
			"affected_vertex_count": affected_count,
			"unaffected_vertex_count": unaffected_count,
			"vertex_count": rest.size(),
		})
		return
	var ok := max_affected >= float(request.min_affected_displacement) and max_affected <= float(request.max_affected_displacement) and max_unaffected <= float(request.max_unaffected_displacement)
	var bone_basis := skeleton.get_bone_pose(bone_idx).basis
	var observed_basis := [
		[bone_basis.x.x, bone_basis.y.x, bone_basis.z.x],
		[bone_basis.x.y, bone_basis.y.y, bone_basis.z.y],
		[bone_basis.x.z, bone_basis.y.z, bone_basis.z.z],
	]
	var vertex_samples: Array = []
	var sample_limit := mini(rest.size(), MAX_VERTEX_SAMPLES)
	for i in range(sample_limit):
		vertex_samples.append({
			"vertex_index": i,
			"rest_position": [rest[i].x, rest[i].y, rest[i].z],
			"posed_position": [posed[i].x, posed[i].y, posed[i].z],
		})
	var request_digest := _request_digest(request)
	var payload := {
		"status": "PASS" if ok else "FAIL",
		"reason": "displacement thresholds met" if ok else "displacement thresholds not met",
		"method": "bake_mesh_from_current_skeleton_pose",
		"request_digest": request_digest,
		"glb_sha256": str(request.glb_sha256),
		"contract_sha256": str(request.contract_sha256),
		"harness_sha256": str(request.harness_sha256),
		"godot_version": str(request.godot_version),
		"pose_bone": str(request.pose_bone),
		"observed_bone_transform": {"kind": "basis", "basis": observed_basis},
		"vertex_samples": vertex_samples,
		"max_affected_displacement": max_affected,
		"max_unaffected_displacement": max_unaffected,
		"affected_vertex_count": affected_count,
		"unaffected_vertex_count": unaffected_count,
		"vertex_count": rest.size(),
	}
	if not ok and max_affected < float(request.min_affected_displacement):
		payload["reason"] = "affected region did not move enough for posed bone"
	_write_result(payload)

func _request_digest(request: Dictionary) -> String:
	var copy := request.duplicate(true)
	copy.erase("glb")
	copy.erase("output_path")
	copy.erase("request_digest")
	for field in RUNTIME_REQUEST_INT_FIELDS:
		if copy.has(field):
			copy[field] = _strict_runtime_int(copy[field], field)
	if copy.has("region_boundary_tolerance"):
		_strict_region_boundary_tolerance(copy["region_boundary_tolerance"])
		copy["region_boundary_tolerance"] = 0.000001
	var keys: Array = copy.keys()
	keys.sort()
	var ordered: Dictionary = {}
	for k in keys:
		ordered[k] = copy[k]
	return str(JSON.stringify(ordered).sha256_text())

func _strict_runtime_int(value: Variant, field: String) -> int:
	if typeof(value) == TYPE_BOOL:
		push_error(field)
		return -1
	if typeof(value) != TYPE_INT and typeof(value) != TYPE_FLOAT:
		push_error(field)
		return -1
	var as_float := float(value)
	if not is_finite(as_float) or as_float != floor(as_float):
		push_error(field)
		return -1
	var iv := int(as_float)
	if iv < 0:
		push_error(field)
		return -1
	return iv

func _strict_region_boundary_tolerance(value: Variant) -> float:
	if typeof(value) == TYPE_BOOL or (typeof(value) != TYPE_INT and typeof(value) != TYPE_FLOAT):
		push_error("region_boundary_tolerance")
		return -1.0
	var tol := float(value)
	if not is_finite(tol) or tol != REGION_BOUNDARY_TOLERANCE:
		push_error("region_boundary_tolerance")
		return -1.0
	return REGION_BOUNDARY_TOLERANCE

func _inside(p: Vector3, box: Dictionary, boundary_tolerance: float = REGION_BOUNDARY_TOLERANCE) -> bool:
	var mn: Array = box["min"]
	var mx: Array = box["max"]
	var eps := boundary_tolerance
	return (p.x - float(mx[0])) <= eps and (float(mn[0]) - p.x) <= eps and (p.y - float(mx[1])) <= eps and (float(mn[1]) - p.y) <= eps and (p.z - float(mx[2])) <= eps and (float(mn[2]) - p.z) <= eps

func _describe_tree(node: Node) -> Array:
	var lines: Array = []
	_describe_tree_rec(node, lines, 0)
	return lines

func _describe_tree_rec(node: Node, lines: Array, depth: int) -> void:
	var indent := "  ".repeat(depth)
	var extra := ""
	if node is MeshInstance3D:
		var mi := node as MeshInstance3D
		extra = " skin=" + str(mi.skin != null) + " skeleton=" + str(mi.skeleton)
	lines.append(indent + node.get_class() + " " + node.name + extra)
	for child in node.get_children():
		_describe_tree_rec(child, lines, depth + 1)

func _find_skinned_mesh(node: Node) -> MeshInstance3D:
	if node is MeshInstance3D:
		if node.skin != null or not node.skeleton.is_empty():
			return node
	for child in node.get_children():
		var found := _find_skinned_mesh(child)
		if found != null:
			return found
	return null

func _mesh_surface_vertices(mesh: Mesh) -> PackedVector3Array:
	var arrays := mesh.surface_get_arrays(0)
	return arrays[Mesh.ARRAY_VERTEX]

func _write_result(payload: Dictionary) -> void:
	payload["schema_version"] = "rig-runtime-observation-0.8.0"
	if _output_path == "":
		quit(1)
		return
	var file := FileAccess.open(_output_path, FileAccess.WRITE)
	if file == null:
		quit(1)
		return
	file.store_string(JSON.stringify(payload))
	file.close()
	quit(0 if payload.get("status") == "PASS" else 1)
