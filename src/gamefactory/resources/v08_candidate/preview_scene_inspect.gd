extends SceneTree

const TOLERANCE := 1e-4
const EXPECTED_VISUAL_MESH_NAME := "SM_HumanoidSkin"
const EXPECTED_BONE_COUNT := 12
const EXPECTED_BONES := ["Hips", "Spine", "Chest", "Neck", "Head", "LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand", "LeftUpperLeg"]

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var collider_path := "res://collider.json"
	for arg in OS.get_cmdline_user_args():
		var text := str(arg)
		if text.begins_with("--collider="):
			collider_path = text.trim_prefix("--collider=")
	var collider_file := FileAccess.open(collider_path, FileAccess.READ)
	if collider_file == null:
		_fail("collider.json is missing")
		return
	var collider: Variant = JSON.parse_string(collider_file.get_as_text())
	if typeof(collider) != TYPE_DICTIONARY:
		_fail("collider.json must be an object")
		return
	var packed_scene := load("res://character.tscn") as PackedScene
	if packed_scene == null:
		_fail("character.tscn failed to load")
		return
	var char_root := packed_scene.instantiate()
	if char_root == null or not (char_root is Node3D):
		_fail("character.tscn root must be Node3D")
		return
	root.add_child(char_root)
	var skeleton := _find_skeleton(char_root)
	if skeleton == null:
		_fail("Skeleton3D is missing from imported character")
		return
	if skeleton.get_bone_count() != EXPECTED_BONE_COUNT:
		_fail("skeleton bone count does not match humanoid_12bone_v1 contract")
		return
	for bone_name in EXPECTED_BONES:
		if skeleton.find_bone(bone_name) < 0:
			_fail("expected skeleton bone is missing: " + bone_name)
			return
	var mesh := _find_named_skinned_mesh(char_root, EXPECTED_VISUAL_MESH_NAME)
	if mesh == null or mesh.skin == null:
		_fail("skinned visual mesh is missing")
		return
	var skin_skeleton: NodePath = mesh.skeleton
	if skin_skeleton.is_empty():
		_fail("skinned mesh is not bound to a skeleton")
		return
	var bound_skeleton := mesh.get_node_or_null(skin_skeleton)
	if bound_skeleton != skeleton:
		_fail("skinned mesh skeleton relationship is invalid")
		return
	var body := char_root.get_node_or_null("PhysicsBody")
	if body == null or not (body is StaticBody3D):
		_fail("PhysicsBody StaticBody3D is missing")
		return
	var shape_node := body.get_node_or_null("CollisionShape3D")
	if shape_node == null or not (shape_node is CollisionShape3D):
		_fail("CollisionShape3D is missing under PhysicsBody")
		return
	var capsule_shape: Shape3D = shape_node.shape
	if capsule_shape == null or not (capsule_shape is CapsuleShape3D):
		_fail("CollisionShape3D must use CapsuleShape3D")
		return
	var capsule := capsule_shape as CapsuleShape3D
	var expected_radius := float(collider.get("radius_m", 0.0))
	var expected_height := float(collider.get("height_m", 0.0))
	var center_arr: Array = collider.get("center_m", [])
	if center_arr.size() != 3:
		_fail("collider center_m must have three components")
		return
	if abs(capsule.radius - expected_radius) > TOLERANCE:
		_fail("capsule radius mismatch")
		return
	if abs(capsule.height - expected_height) > TOLERANCE:
		_fail("capsule height mismatch")
		return
	var pos: Vector3 = shape_node.position
	if abs(pos.x - float(center_arr[0])) > TOLERANCE:
		_fail("capsule center x mismatch")
		return
	if abs(pos.y - float(center_arr[1])) > TOLERANCE:
		_fail("capsule center y mismatch")
		return
	if abs(pos.z - float(center_arr[2])) > TOLERANCE:
		_fail("capsule center z mismatch")
		return
	print("PASS")
	quit(0)

func _fail(message: String) -> void:
	print("FAIL:", message)
	quit(1)

func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node as Skeleton3D
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null

func _find_named_skinned_mesh(node: Node, mesh_name: String) -> MeshInstance3D:
	if node is MeshInstance3D:
		var mesh_node := node as MeshInstance3D
		if mesh_node.name == mesh_name and mesh_node.skin != null:
			return mesh_node
	for child in node.get_children():
		var found := _find_named_skinned_mesh(child, mesh_name)
		if found != null:
			return found
	return null
