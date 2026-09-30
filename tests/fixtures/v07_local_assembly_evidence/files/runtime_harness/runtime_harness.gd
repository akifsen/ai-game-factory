extends SceneTree

## Standalone V0.7 Godot Assembly Runtime Verification Harness.
## Proves runtime representation, hierarchy, pivots, sockets, colliders,
## articulation, exact restoration, and review captures.
const MAX_REQUEST_BYTES := 131072

var request: Dictionary = {}
var failures: Array[String] = []
var actual_import_tree: Dictionary = {}
var parts_verified: Array = []
var sockets_verified: Array = []
var articulation_results: Array = []
var view_framing: Dictionary = {}
var captures: Dictionary = {}
var collider_info: Dictionary = {}
var semantic_root: Node3D = null
var world_root: Node3D = null
var asset_root: Node3D = null
var physics_body: Node3D = null
var physics_shape: CollisionShape3D = null
var physics_ray_hit := false
var restoration_verified := true

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var args := OS.get_cmdline_user_args()
	var request_path := ""
	var i := 0
	while i < args.size():
		if args[i] == "--request" and i + 1 < args.size():
			request_path = args[i + 1]
			i += 2
		else:
			i += 1

	if request_path.is_empty():
		_fail("arguments must include --request <json>")
		quit(1)
		return

	var file := FileAccess.open(request_path, FileAccess.READ)
	if file == null or file.get_length() > MAX_REQUEST_BYTES:
		_fail("request file missing or exceeds size limit")
		quit(1)
		return
	var raw_text := file.get_as_text()
	file.close()

	request = JSON.parse_string(raw_text)
	if typeof(request) != TYPE_DICTIONARY:
		_fail("request must be a JSON object")
		quit(1)
		return

	for key in [
		"workflow_id", "execution_id", "attempt_number", "asset_id",
		"glb", "output_dir", "observation_path", "processed_glb_sha256",
		"review_views", "spec", "profile"
	]:
		if not request.has(key):
			_fail("request missing required key: " + key)
			_write_observation(false)
			quit(1)
			return

	# Pre-load review views check: unknown/duplicate/nonstring/empty views fail before load, no fallback.
	var raw_views = request.get("review_views")
	if typeof(raw_views) != TYPE_ARRAY or raw_views.is_empty():
		_fail("review_views must be a non-empty list of strings")
		_write_observation(false)
		quit(1)
		return

	var requested_views: Array[String] = []
	var seen_views: Dictionary = {}
	for v in raw_views:
		if typeof(v) != TYPE_STRING or str(v).is_empty():
			_fail("review view must be a non-empty string")
			_write_observation(false)
			quit(1)
			return
		var v_str := str(v)
		if view_placement(v_str).is_empty():
			_fail("unknown capture angle " + v_str)
			_write_observation(false)
			quit(1)
			return
		if seen_views.has(v_str):
			_fail("duplicate capture angle " + v_str)
			_write_observation(false)
			quit(1)
			return
		seen_views[v_str] = true
		requested_views.append(v_str)

	# Load processed GLB
	var glb_path := str(request.glb)
	var packed := load(glb_path) as PackedScene
	if packed == null:
		_fail("Godot could not import/load the processed GLB: " + glb_path)
		_write_observation(false)
		quit(1)
		return

	var imported := packed.instantiate()
	if imported == null:
		_fail("GLB instance failed: " + glb_path)
		_write_observation(false)
		quit(1)
		return

	# Examine glTF extras via GLTFDocument / GLTFState
	var gltf_extras_map := _read_gltf_extras(glb_path)

	# Record actual import tree before modification
	actual_import_tree = _record_node_tree(imported)

	# Verify exact semantic identity ROOT
	if imported.name == "ROOT":
		semantic_root = imported as Node3D
	else:
		if not (imported is Node3D):
			_fail("PackedScene container is not a Node3D: " + imported.get_class())
		elif imported.get_child_count() != 1 or imported.get_child(0).name != "ROOT":
			_fail("unexpected semantic wrapper outside ROOT: " + imported.name + " (children count: " + str(imported.get_child_count()) + ")")
		else:
			if not _is_identity_transform(imported.transform):
				_fail("PackedScene container has non-identity transform: " + str(imported.transform))
			semantic_root = imported.get_child(0) as Node3D

	if semantic_root == null:
		_fail("semantic ROOT node not found")
		_write_observation(false)
		quit(1)
		return

	if not _is_identity_transform(semantic_root.transform):
		_fail("semantic ROOT has non-identity local transform: " + str(semantic_root.transform))

	# Setup verification world
	world_root = Node3D.new()
	world_root.name = "AssetVerificationWorld"
	root.add_child(world_root)

	asset_root = Node3D.new()
	asset_root.name = "AssetUnderTest"
	world_root.add_child(asset_root)
	asset_root.add_child(imported)

	# Extract spec and profile configurations
	var spec: Dictionary = request.spec
	var profile: Dictionary = request.profile
	var asset_id := str(request.asset_id)
	var declared_parts: Array = spec.get("parts", [])
	var declared_sockets: Array = spec.get("sockets", [])
	var pivot_tolerance := float(profile.get("pivot_tolerance_m", 0.01))
	var basis_tolerance := float(profile.get("basis_tolerance_deg", 1.0))
	var socket_pos_tolerance := float(profile.get("socket_position_tolerance_m", 0.01))
	var socket_rot_tolerance := float(profile.get("socket_angle_tolerance_deg", 1.0))

	# Verify PART parent tree, pivots, extras, and named LOD meshes
	var part_nodes: Dictionary = {}
	var part_specs: Dictionary = {}
	var created_markers: Dictionary = {}
	var expected_part_names: Dictionary = {}
	var expected_socket_names: Dictionary = {}
	var expected_mesh_names: Dictionary = {}

	for p_item in declared_parts:
		var p: Dictionary = p_item
		var part_id := str(p.part_id)
		part_specs[part_id] = p
		var expected_name := "PART_" + part_id
		expected_part_names[expected_name] = true
		var p_node := _find_node_by_name(semantic_root, expected_name) as Node3D
		if p_node == null:
			_fail("part.missing." + part_id)
			continue
		part_nodes[part_id] = p_node

		# Verify parent topology
		var expected_parent_name := "ROOT" if str(p.parent) == "root" else ("PART_" + str(p.parent))
		var actual_parent := p_node.get_parent()
		if actual_parent == null or actual_parent.name != expected_parent_name:
			_fail("part.parent." + part_id + ": expected parent " + expected_parent_name + ", got " + (actual_parent.name if actual_parent != null else "null"))

		# Verify scale: uniform, positive
		var scale_vec := p_node.transform.basis.get_scale()
		if scale_vec.x <= 0 or scale_vec.y <= 0 or scale_vec.z <= 0 or absf(scale_vec.x - scale_vec.y) > 1e-4 or absf(scale_vec.x - scale_vec.z) > 1e-4:
			_fail("part.scale." + part_id + ": scale must be positive uniform scale, got " + str(scale_vec))

		# Verify pivot position
		var pivot_spec: Dictionary = p.get("pivot", {})
		var declared_pos_arr: Array = pivot_spec.get("position_m", [0.0, 0.0, 0.0])
		var declared_pos := Vector3(declared_pos_arr[0], declared_pos_arr[1], declared_pos_arr[2])
		var actual_pos := p_node.transform.origin
		var pos_dist := actual_pos.distance_to(declared_pos)
		if declared_pos.length() > 0.001 and actual_pos.length() < 0.0001:
			_fail("pivot.collapsed." + part_id + ": declared nonzero position " + str(declared_pos) + " collapsed to origin")
		elif pos_dist > pivot_tolerance:
			_fail("pivot.position." + part_id + ": position mismatch " + str(actual_pos) + " vs declared " + str(declared_pos) + " (delta: " + str(pos_dist) + ")")

		# Verify pivot basis / orientation
		var declared_basis := Basis.IDENTITY
		var basis_val = pivot_spec.get("basis", "identity")
		if typeof(basis_val) == TYPE_ARRAY and basis_val.size() == 4:
			var q := Quaternion(basis_val[0], basis_val[1], basis_val[2], basis_val[3]).normalized()
			declared_basis = Basis(q)
		var actual_q := p_node.transform.basis.orthonormalized().get_rotation_quaternion().normalized()
		var decl_q := declared_basis.orthonormalized().get_rotation_quaternion().normalized()
		var dot_val := clampf(absf(actual_q.dot(decl_q)), -1.0, 1.0)
		var angle_deg := rad_to_deg(2.0 * acos(dot_val))
		if angle_deg > basis_tolerance:
			_fail("pivot.orientation." + part_id + ": rotation angle delta " + str(angle_deg) + " deg exceeds tolerance " + str(basis_tolerance))

		# Verify motion extras (gf_motion, gf_axis)
		var motion_spec: Dictionary = pivot_spec.get("motion", {})
		var decl_motion_kind := str(motion_spec.get("kind", "fixed"))
		var node_extras := _extract_node_extras(p_node, gltf_extras_map.get(expected_name, {}))
		var actual_motion_kind := str(node_extras.get("gf_motion", ""))
		if actual_motion_kind != decl_motion_kind:
			_fail("pivot.axis." + part_id + ": motion kind mismatch: expected " + decl_motion_kind + ", got " + actual_motion_kind)
		elif decl_motion_kind != "fixed":
			var decl_axis_arr: Array = motion_spec.get("axis", [0.0, 1.0, 0.0])
			var decl_axis := Vector3(decl_axis_arr[0], decl_axis_arr[1], decl_axis_arr[2]).normalized()
			var actual_axis_arr = node_extras.get("gf_axis")
			if actual_axis_arr == null or typeof(actual_axis_arr) != TYPE_ARRAY or actual_axis_arr.size() != 3:
				_fail("pivot.axis." + part_id + ": missing or invalid gf_axis in extras: " + str(node_extras))
			else:
				var actual_axis := Vector3(actual_axis_arr[0], actual_axis_arr[1], actual_axis_arr[2]).normalized()
				if actual_axis.distance_to(decl_axis) > 1e-4:
					_fail("pivot.axis." + part_id + ": motion axis mismatch: declared " + str(decl_axis) + ", actual " + str(actual_axis))

		# Verify named LOD0 mesh: direct child, non-empty, identity local transform
		var lod0_name := "SM_" + asset_id + "_" + part_id + "_LOD0"
		expected_mesh_names[lod0_name] = true
		var lod0_node := p_node.get_node_or_null(lod0_name) as MeshInstance3D
		if lod0_node == null:
			_fail("part.lod0." + part_id + ": missing direct named child " + lod0_name)
		elif lod0_node.mesh == null or lod0_node.mesh.get_surface_count() == 0:
			_fail("part.lod0." + part_id + ": " + lod0_name + " has empty mesh or zero surfaces")
		elif not _is_identity_transform(lod0_node.transform):
			_fail("part.lod0." + part_id + ": " + lod0_name + " has non-identity local transform: " + str(lod0_node.transform))
		else:
			lod0_node.visible = true

		# Verify named LOD1 mesh (if present or required)
		var lod1_name := "SM_" + asset_id + "_" + part_id + "_LOD1"
		var lod1_node := p_node.get_node_or_null(lod1_name) as MeshInstance3D
		if lod1_node != null:
			expected_mesh_names[lod1_name] = true
			if lod1_node.mesh == null or lod1_node.mesh.get_surface_count() == 0:
				_fail("part.lod1." + part_id + ": " + lod1_name + " has empty mesh or zero surfaces")
			elif not _is_identity_transform(lod1_node.transform):
				_fail("part.lod1." + part_id + ": " + lod1_name + " has non-identity local transform: " + str(lod1_node.transform))
			# LOD1 must be hidden initially
			lod1_node.visible = false
		elif bool(profile.get("lod1_required", true)):
			_fail("part.lod1." + part_id + ": required LOD1 child is missing")

		# Reject undeclared geometry children and semantic PART nodes hidden elsewhere.
		for child in p_node.get_children():
			if child is MeshInstance3D and child != lod0_node and child != lod1_node and not child.name.begins_with("SOCKET_"):
				_fail("part.geometry." + part_id + ": unexpected mesh child " + child.name)

		parts_verified.append({
			"part_id": part_id,
			"node_name": expected_name,
			"parent": str(p.parent),
			"local_position": [actual_pos.x, actual_pos.y, actual_pos.z],
			"local_basis": _basis_rows(p_node.transform.basis),
			"local_scale": [scale_vec.x, scale_vec.y, scale_vec.z],
			"motion_kind": actual_motion_kind,
			"gf_axis": node_extras.get("gf_axis", []),
			"lod0_present": lod0_node != null,
			"lod1_present": lod1_node != null,
		})

	# Verify Sockets: imported empty Node3D, assert transform, create Marker3D representation
	for s_item in declared_sockets:
		var s: Dictionary = s_item
		var socket_id := str(s.socket_id)
		var parent_part_id := str(s.parent_part)
		var socket_name := "SOCKET_" + socket_id
		expected_socket_names[socket_name] = true
		var parent_part_node: Node3D = part_nodes.get(parent_part_id)

		if parent_part_node == null:
			_fail("socket.parent." + socket_id + ": declared parent part " + parent_part_id + " not found")
			continue

		var socket_node := parent_part_node.get_node_or_null(socket_name) as Node3D
		if socket_node == null:
			_fail("socket.missing." + socket_id + ": direct child " + socket_name + " under PART_" + parent_part_id + " not found")
			continue

		# Must be empty Node3D (not a MeshInstance3D, no children, scale 1)
		if socket_node is MeshInstance3D or socket_node.get_child_count() > 0:
			_fail("socket.structure." + socket_id + ": socket must have no mesh and no children")
		if not socket_node.transform.basis.get_scale().is_equal_approx(Vector3.ONE):
			_fail("socket.structure." + socket_id + ": socket scale must be identity")

		# Check local translation
		var decl_s_trans: Array = s.get("translation_m", [0.0, 0.0, 0.0])
		var expected_s_pos := Vector3(decl_s_trans[0], decl_s_trans[1], decl_s_trans[2])
		var actual_s_pos := socket_node.transform.origin
		var s_pos_dist := actual_s_pos.distance_to(expected_s_pos)
		if s_pos_dist > socket_pos_tolerance:
			_fail("socket.position." + socket_id + ": socket translation mismatch: actual " + str(actual_s_pos) + " vs declared " + str(expected_s_pos) + " (delta: " + str(s_pos_dist) + ")")

		# Check local rotation
		var decl_s_rot = s.get("rotation", "identity")
		var expected_s_basis := Basis.IDENTITY
		if typeof(decl_s_rot) == TYPE_ARRAY and decl_s_rot.size() == 4:
			var sq := Quaternion(decl_s_rot[0], decl_s_rot[1], decl_s_rot[2], decl_s_rot[3]).normalized()
			expected_s_basis = Basis(sq)
		var actual_sq := socket_node.transform.basis.orthonormalized().get_rotation_quaternion().normalized()
		var decl_sq := expected_s_basis.orthonormalized().get_rotation_quaternion().normalized()
		var s_dot := clampf(absf(actual_sq.dot(decl_sq)), -1.0, 1.0)
		var s_rot_deg := rad_to_deg(2.0 * acos(s_dot))
		if s_rot_deg > socket_rot_tolerance:
			_fail("socket.orientation." + socket_id + ": socket rotation mismatch: " + str(s_rot_deg) + " deg exceeds tolerance")

		# Create Marker3D representation under parent_part without concealing errors
		var marker := Marker3D.new()
		marker.name = "MARKER_" + socket_id
		marker.transform = socket_node.transform
		parent_part_node.add_child(marker)
		created_markers[socket_id] = marker

		# Verify full ancestor world transform parity
		socket_node.force_update_transform()
		marker.force_update_transform()
		if not marker.global_transform.is_equal_approx(socket_node.global_transform):
			_fail("socket.marker." + socket_id + ": created Marker3D global transform does not match imported socket node")

		sockets_verified.append({
			"socket_id": socket_id,
			"parent_part": parent_part_id,
			"local_position": [actual_s_pos.x, actual_s_pos.y, actual_s_pos.z],
			"local_basis": _basis_rows(socket_node.transform.basis),
			"world_position": _vector_array(socket_node.global_position),
			"world_basis": _basis_rows(socket_node.global_basis),
			"marker_created": true,
		})

	# Compare complete semantic inventories before runtime probes.
	var actual_part_names: Dictionary = {}
	var actual_socket_names: Dictionary = {}
	_collect_semantic_names(semantic_root, actual_part_names, actual_socket_names)
	if actual_part_names != expected_part_names:
		_fail("part.inventory: imported PART nodes differ from the specification: " + str(actual_part_names.keys()))
	if actual_socket_names != expected_socket_names:
		_fail("socket.inventory: imported SOCKET nodes differ from the specification: " + str(actual_socket_names.keys()))

	# Find COL_<asset_id> proxy mesh under ROOT
	var col_name := "COL_" + asset_id
	var col_node := semantic_root.get_node_or_null(col_name) as MeshInstance3D
	if col_node == null:
		# Also check direct children of semantic_root with prefix COL_
		for child in semantic_root.get_children():
			if child.name.begins_with("COL_") and child is MeshInstance3D:
				col_node = child as MeshInstance3D
				break
	if col_node == null or col_node.mesh == null or col_node.mesh.get_surface_count() == 0:
		_fail("collider.missing: assembly root COL_" + asset_id + " mesh proxy missing or empty")
	elif not _is_identity_transform(col_node.transform):
		_fail("collider.transform: COL proxy must be identity-local under ROOT")
	expected_mesh_names[col_name] = true

	# Hide COL proxy mesh
	if col_node != null:
		col_node.visible = false

	# Calculate assembly COL rest bounds
	var col_aabb := AABB()
	if col_node != null and col_node.mesh != null:
		col_aabb = col_node.mesh.get_aabb()
	if col_aabb.size.x <= 0.0 or col_aabb.size.y <= 0.0 or col_aabb.size.z <= 0.0:
		_fail("collider.bounds: COL mesh has empty rest bounds")
	var actual_mesh_names: Dictionary = {}
	_collect_mesh_names(imported, actual_mesh_names)
	if actual_mesh_names != expected_mesh_names:
		_fail("mesh.inventory: imported mesh nodes differ from required LOD0/LOD1/COL inventory: " + str(actual_mesh_names.keys()))

	# Build runtime physics body & CollisionShape3D matching COL rest bounds
	var body_kind := str(profile.get("body_kind", "static_body"))
	if body_kind == "area":
		var area := Area3D.new()
		area.name = "Area3D"
		area.monitoring = true
		area.monitorable = true
		physics_body = area
	else:
		var sb := StaticBody3D.new()
		sb.name = "StaticBody3D"
		physics_body = sb

	physics_shape = CollisionShape3D.new()
	physics_shape.name = "CollisionShape3D"
	var box_shape := BoxShape3D.new()
	box_shape.size = col_aabb.size
	physics_shape.shape = box_shape
	physics_shape.position = col_aabb.get_center()
	physics_body.add_child(physics_shape)

	# Root collider parented to asset_root (does NOT follow moving PART)
	asset_root.add_child(physics_body)

	collider_info = {
		"body_kind": body_kind,
		"shape": "box",
		"bounds": {
			"position": [col_aabb.position.x, col_aabb.position.y, col_aabb.position.z],
			"size": [col_aabb.size.x, col_aabb.size.y, col_aabb.size.z],
		},
		"physics_ray_hit": false,
	}

	# Run physics ray check if required
	var require_ray := bool(profile.get("require_ray_hit", true))
	if require_ray:
		await physics_frame
		await physics_frame
		var center := col_aabb.get_center()
		var extent := maxf(col_aabb.size.x, maxf(col_aabb.size.y, col_aabb.size.z))
		var from_pt := center + Vector3(0, extent * 2.0, 0)
		var to_pt := center - Vector3(0, extent * 2.0, 0)
		var query := PhysicsRayQueryParameters3D.create(from_pt, to_pt)
		var hit := world_root.get_world_3d().direct_space_state.intersect_ray(query)
		if hit.is_empty() or hit.get("collider") != physics_body:
			_fail("physics.ray_miss: ray did not intersect assembly root collider body")
		else:
			physics_ray_hit = true
			collider_info["physics_ray_hit"] = true

	# Articulation verification: for each moving part, apply bounded test displacement,
	# independently verify pivot world position/axis, descendants/sockets rigid motion,
	# unchanged ancestors/siblings/root/collider, and restore exact rest locals.
	for p_item in declared_parts:
		var p: Dictionary = p_item
		var part_id := str(p.part_id)
		var pivot_spec: Dictionary = p.get("pivot", {})
		var motion_spec: Dictionary = pivot_spec.get("motion", {})
		var kind := str(motion_spec.get("kind", "fixed"))
		if kind == "fixed":
			continue

		var p_node: Node3D = part_nodes.get(part_id)
		if p_node == null:
			continue

		var axis_arr: Array = motion_spec.get("axis", [0.0, 1.0, 0.0])
		var local_axis := Vector3(axis_arr[0], axis_arr[1], axis_arr[2]).normalized()

		# Store initial rest states
		p_node.force_update_transform()
		var rest_local := p_node.transform
		var rest_world_pivot := p_node.global_position
		var rest_world_trans := p_node.global_transform
		var rest_collider_world := physics_body.global_transform

		# Collect descendants and non-descendants
		var descendants := _collect_descendants(p_node)
		var non_descendants := _collect_non_descendants(semantic_root, p_node)
		non_descendants.append(physics_body)

		var rest_rel_descendants: Dictionary = {}
		for d in descendants:
			(d as Node3D).force_update_transform()
			rest_rel_descendants[d] = p_node.global_transform.affine_inverse() * (d as Node3D).global_transform

		var rest_non_descendants_world: Dictionary = {}
		for nd in non_descendants:
			(nd as Node3D).force_update_transform()
			rest_non_descendants_world[nd] = (nd as Node3D).global_transform

		# Apply test displacement
		var disp_angle := 0.5  # ~28.6 degrees
		var disp_dist := 0.2   # 0.2 meters
		var expected_axis_world := (rest_world_trans.basis * local_axis).normalized()
		if kind == "revolute":
			p_node.rotate_object_local(local_axis, disp_angle)
		elif kind == "prismatic":
			p_node.transform.origin = rest_local.origin + local_axis * disp_dist
		else:
			_fail("articulation.kind." + part_id + ": unsupported motion kind " + kind)

		p_node.force_update_transform()

		# 1. Check pivot world position
		var pivot_world_ok := true
		var axis_world_ok := true
		var motion_applied := false
		if kind == "revolute":
			var pivot_drift := p_node.global_position.distance_to(rest_world_pivot)
			if pivot_drift > 1e-4:
				_fail("articulation.pivot_moved." + part_id + ": revolute pivot drifted in world space by " + str(pivot_drift))
				pivot_world_ok = false
			var expected_world := rest_world_trans * Transform3D(Basis(local_axis, disp_angle), Vector3.ZERO)
			motion_applied = not p_node.global_transform.is_equal_approx(rest_world_trans)
			if not p_node.global_transform.is_equal_approx(expected_world):
				_fail("articulation.axis_world." + part_id + ": rotation did not follow the declared local axis " + str(expected_axis_world))
				axis_world_ok = false
		elif kind == "prismatic":
			var parent_trans: Transform3D = Transform3D.IDENTITY
			if p_node.get_parent() is Node3D:
				parent_trans = (p_node.get_parent() as Node3D).global_transform
			var expected_delta: Vector3 = parent_trans.basis * (local_axis * disp_dist)
			var actual_delta := p_node.global_position - rest_world_pivot
			if actual_delta.distance_to(expected_delta) > 1e-4:
				_fail("articulation.pivot_moved." + part_id + ": prismatic translation error")
				pivot_world_ok = false
			motion_applied = not p_node.global_transform.is_equal_approx(rest_world_trans)

		# 2. Check descendants & sockets rigid motion
		var descendants_rigid_ok := true
		var descendant_moved := false
		for d in descendants:
			var d_node := d as Node3D
			d_node.force_update_transform()
			var curr_rel := p_node.global_transform.affine_inverse() * d_node.global_transform
			if not curr_rel.is_equal_approx(rest_rel_descendants[d]):
				_fail("articulation.descendant_not_rigid." + part_id + ": descendant " + d_node.name + " did not move rigidly")
				descendants_rigid_ok = false
			# Verify descendant actually moved in world space if offset from pivot
			var rest_d_world = rest_rel_descendants[d]
			if not d_node.global_transform.is_equal_approx(rest_world_trans * rest_d_world):
				descendant_moved = true

		# 3. Check unchanged ancestors, siblings, root, collider
		var non_descendants_ok := true
		for nd in non_descendants:
			var nd_node := nd as Node3D
			nd_node.force_update_transform()
			if not nd_node.global_transform.is_equal_approx(rest_non_descendants_world[nd]):
				_fail("articulation.ancestor_moved." + part_id + ": non-descendant node " + nd_node.name + " moved during articulation")
				non_descendants_ok = false

		if not physics_body.global_transform.is_equal_approx(rest_collider_world):
			_fail("articulation.collider_moved." + part_id + ": root collider moved during part articulation")
			non_descendants_ok = false
		if not motion_applied:
			_fail("articulation.motion_not_applied." + part_id + ": test displacement did not change the PART transform")
		if not descendant_moved:
			_fail("articulation.descendant_not_moved." + part_id + ": articulation did not move any PART descendants")

		# 4. Restore exact rest local transform and verify
		p_node.transform = rest_local
		p_node.force_update_transform()
		var restored_part_ok := p_node.transform == rest_local and p_node.global_transform.is_equal_approx(rest_world_trans)
		var restored_desc_ok := true
		for d in descendants:
			var d_node := d as Node3D
			d_node.force_update_transform()
			var d_rest_world = rest_world_trans * rest_rel_descendants[d]
			if not d_node.global_transform.is_equal_approx(d_rest_world):
				restored_desc_ok = false
		var restored_non_desc_ok := true
		for nd in non_descendants:
			var nd_node := nd as Node3D
			nd_node.force_update_transform()
			if not nd_node.global_transform.is_equal_approx(rest_non_descendants_world[nd]):
				restored_non_desc_ok = false

		if not restored_part_ok or not restored_desc_ok or not restored_non_desc_ok:
			_fail("articulation.restore_failed." + part_id + ": rest local transform was not restored")
			restoration_verified = false

		articulation_results.append({
			"part_id": part_id,
			"motion_kind": kind,
			"motion_applied": motion_applied,
			"pivot_world_ok": pivot_world_ok,
			"axis_world_ok": axis_world_ok,
			"descendants_rigid_ok": descendants_rigid_ok,
			"descendant_moved": descendant_moved,
			"ancestors_siblings_unchanged": non_descendants_ok,
			"restored_ok": restored_part_ok and restored_desc_ok and restored_non_desc_ok,
		})

	# If any errors so far, write observation and stop
	if not failures.is_empty():
		_write_observation(false)
		quit(1)
		return

	# Capture after restore all profile.review_views
	# Calculate total assembly rest bounds from all visible LOD0 meshes
	var total_assembly_bounds := _calculate_assembly_bounds(asset_root)
	if not total_assembly_bounds.size.is_finite() or total_assembly_bounds.size.x <= 0 or total_assembly_bounds.size.y <= 0 or total_assembly_bounds.size.z <= 0:
		_fail("assembly rest bounds are invalid: " + str(total_assembly_bounds))
		_write_observation(false)
		quit(1)
		return

	# Setup lighting and environment
	world_root.add_child(_environment())

	var framing_policy: Dictionary = profile.get("framing", {})
	var min_fraction := float(framing_policy.get("min_screen_fraction", 0.55))
	var max_fraction := float(framing_policy.get("max_screen_fraction", 0.75))
	var target_fraction := float(framing_policy.get("target_screen_fraction", 0.65))
	var margin_fraction := float(framing_policy.get("margin_fraction", 0.04))
	var fov := float(framing_policy.get("fov_degrees", 38.0))

	var camera := Camera3D.new()
	camera.current = true
	camera.fov = fov
	world_root.add_child(camera)

	var extent: float = max(total_assembly_bounds.size.x, max(total_assembly_bounds.size.y, total_assembly_bounds.size.z))
	var center: Vector3 = total_assembly_bounds.get_center()
	var scale_reference := _add_floor(total_assembly_bounds, extent)

	var out_dir := str(request.output_dir)

	for angle in requested_views:
		var direction := _view_direction_vector(angle)
		var up_vector := _view_up_vector(angle)
		_park_reference(scale_reference, total_assembly_bounds, direction, extent)

		var geom := _framing_geometry(total_assembly_bounds, camera, angle, target_fraction)
		var distance: float = geom["distance"]
		var e_f: float = geom["view_depth_half"]

		camera.position = center + direction * distance
		camera.look_at(center, up_vector)
		await process_frame
		await process_frame

		var viewport_size := root.get_viewport().get_visible_rect().size
		var fitted := _projected_rect(camera, total_assembly_bounds)
		var initial_height_ratio := 0.0
		var correction_applied := false
		if viewport_size.y > 0.0 and fitted.size.y > 0.0:
			initial_height_ratio = fitted.size.y / viewport_size.y
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
			_fail("capture resolution is not 1280x720 for view " + angle)
			continue

		var measured := _measure_view_framing(camera, total_assembly_bounds, scale_reference, Vector2(image.get_width(), image.get_height()), angle, min_fraction, max_fraction, margin_fraction)
		measured["camera_direction"] = _vector_array(-camera.global_basis.z.normalized())
		measured["camera_up"] = _vector_array(camera.global_basis.y.normalized())
		measured["camera_distance"] = distance
		measured["initial_height_ratio"] = initial_height_ratio
		measured["correction_applied"] = correction_applied
		view_framing[angle] = measured

		if not measured.get("ok", false):
			_fail("view framing failed for " + angle + ": " + str(measured.get("reason", "")))
			continue

		var filename := out_dir.path_join(angle + ".png")
		var save_err := image.save_png(filename)
		if save_err != OK:
			_fail("Godot renderer could not save " + angle + " capture: error " + str(save_err))
		else:
			captures[angle] = {"sha256": FileAccess.get_sha256(filename)}

	if not failures.is_empty():
		_write_observation(false)
		quit(1)
		return

	_write_observation(true)
	quit(0)

func _fail(msg: String) -> void:
	failures.append(msg)
	push_error(msg)

func _is_identity_transform(t: Transform3D) -> bool:
	return t.origin.length() < 1e-4 and t.basis.is_equal_approx(Basis.IDENTITY)

func _find_node_by_name(parent: Node, target_name: String) -> Node:
	if parent.name == target_name:
		return parent
	for child in parent.get_children():
		var found := _find_node_by_name(child, target_name)
		if found != null:
			return found
	return null

func _record_node_tree(node: Node) -> Dictionary:
	var result := {
		"name": node.name,
		"class": node.get_class(),
	}
	if node is Node3D:
		var n3d := node as Node3D
		result["position"] = [n3d.transform.origin.x, n3d.transform.origin.y, n3d.transform.origin.z]
	if node.has_meta("extras"):
		result["extras"] = node.get_meta("extras")
	var children_arr: Array = []
	for c in node.get_children():
		children_arr.append(_record_node_tree(c))
	result["children"] = children_arr
	return result

func _read_gltf_extras(glb_path: String) -> Dictionary:
	var extras_map := {}
	var doc := GLTFDocument.new()
	var state := GLTFState.new()
	if doc.append_from_file(glb_path, state) == OK:
		var json = state.get_json()
		if typeof(json) == TYPE_DICTIONARY and json.has("nodes"):
			for n in json.nodes:
				if typeof(n) == TYPE_DICTIONARY and n.has("name") and n.has("extras"):
					extras_map[str(n.name)] = n.extras
	return extras_map

func _extract_node_extras(node: Node, gltf_extras: Dictionary) -> Dictionary:
	if node.has_meta("extras") and typeof(node.get_meta("extras")) == TYPE_DICTIONARY:
		return node.get_meta("extras")
	return gltf_extras

func _collect_descendants(node: Node) -> Array[Node]:
	var result: Array[Node] = []
	for child in node.get_children():
		result.append(child)
		result.append_array(_collect_descendants(child))
	return result

func _collect_non_descendants(root_node: Node, target_subtree: Node) -> Array[Node]:
	var result: Array[Node] = []
	if root_node == target_subtree:
		return result
	result.append(root_node)
	for child in root_node.get_children():
		if child != target_subtree:
			result.append_array(_collect_non_descendants(child, target_subtree))
	return result

func _calculate_assembly_bounds(root_n: Node3D) -> AABB:
	var combined := AABB()
	var first := true
	var meshes: Array[MeshInstance3D] = []
	_gather_visible_lod0_meshes(root_n, meshes)
	for mi in meshes:
		if mi.mesh != null:
			var mi_aabb := mi.mesh.get_aabb()
			var world_aabb := root_n.global_transform.affine_inverse() * mi.global_transform * mi_aabb
			if first:
				combined = world_aabb
				first = false
			else:
				combined = combined.merge(world_aabb)
	return combined

func _gather_visible_lod0_meshes(node: Node, out_list: Array[MeshInstance3D]) -> void:
	if node is MeshInstance3D and (node as MeshInstance3D).visible and node.name.ends_with("_LOD0"):
		out_list.append(node as MeshInstance3D)
	for child in node.get_children():
		_gather_visible_lod0_meshes(child, out_list)

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

func _write_observation(passed: bool) -> void:
	var obs_path := str(request.get("observation_path", ""))
	if obs_path.is_empty():
		return
	var observation := {
		"schema_version": "assembly-runtime-observation-0.7.0",
		"workflow_id": request.get("workflow_id", ""),
		"revision": int(request.get("revision", 0)),
		"asset_id": request.get("asset_id", ""),
		"execution_id": request.get("execution_id", ""),
		"attempt_number": int(request.get("attempt_number", 0)),
		"processed_glb_sha256": request.get("processed_glb_sha256", ""),
		"harness_sha256": request.get("harness_sha256", ""),
		"profile_sha256": request.get("profile_sha256", ""),
		"specification_sha256": request.get("specification_sha256", ""),
		"request_digest": request.get("request_digest", ""),
		"status": "PASS" if passed else "FAIL",
		"semantic_root": {
			"name": semantic_root.name if semantic_root != null else "",
			"transform": {
				"origin": [semantic_root.transform.origin.x, semantic_root.transform.origin.y, semantic_root.transform.origin.z] if semantic_root != null else [],
				"basis": _basis_rows(semantic_root.transform.basis) if semantic_root != null else [],
			},
		},
		"profile_id": request.get("profile", {}).get("profile_id", ""),
		"profile_version": int(request.get("profile", {}).get("version", 0)),
		"import_tree": actual_import_tree,
		"parts_verified": parts_verified,
		"sockets_verified": sockets_verified,
		"collider": collider_info,
		"articulation_results": articulation_results,
		"restoration_verified": restoration_verified,
		"view_framing": view_framing,
		"captures": captures,
		"errors": failures,
	}
	var out_file := FileAccess.open(obs_path, FileAccess.WRITE)
	if out_file != null:
		out_file.store_string(JSON.stringify(observation, "\t") + "\n")
		out_file.close()
	else:
		push_error("could not open observation output path: " + obs_path)

func _collect_semantic_names(node: Node, part_names: Dictionary, socket_names: Dictionary) -> void:
	if node.name.begins_with("PART_"):
		part_names[node.name] = true
	if node.name.begins_with("SOCKET_"):
		socket_names[node.name] = true
	for child in node.get_children():
		_collect_semantic_names(child, part_names, socket_names)

func _collect_mesh_names(node: Node, mesh_names: Dictionary) -> void:
	if node is MeshInstance3D:
		mesh_names[node.name] = true
	for child in node.get_children():
		_collect_mesh_names(child, mesh_names)

func _basis_rows(basis: Basis) -> Array:
	return [
		[basis.x.x, basis.y.x, basis.z.x],
		[basis.x.y, basis.y.y, basis.z.y],
		[basis.x.z, basis.y.z, basis.z.z],
	]

func _vector_array(value: Vector3) -> Array:
	return [value.x, value.y, value.z]
