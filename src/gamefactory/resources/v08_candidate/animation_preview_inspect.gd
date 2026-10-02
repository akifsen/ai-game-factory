extends SceneTree

const BONES := ["Hips", "Spine", "Chest", "Neck", "Head", "LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand", "LeftUpperLeg"]
const CLIP := "rig_smoke_01"
const EPS := 0.0001
const ANGLE_EPS := 0.002

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var packed := load("res://animation_preview.tscn") as PackedScene
	if packed == null:
		_fail("scene missing")
		return
	var scene := packed.instantiate() as Node3D
	if scene == null:
		_fail("root invalid")
		return
	root.add_child(scene)
	for _i in range(3):
		await process_frame
	var player := scene.get_node_or_null("AnimationPlayer") as AnimationPlayer
	var character := scene.get_node_or_null("Character") as Node3D
	if player == null or character == null:
		_fail("player/character missing")
		return
	if not player.is_playing() or not player.has_animation(CLIP):
		_fail("canonical clip must autoplay")
		return
	var skeleton := _find_skeleton(character)
	var mesh := _find_mesh(character)
	if skeleton == null or mesh == null or mesh.skin == null:
		_fail("skin/skeleton missing")
		return
	if mesh.get_node_or_null(mesh.skeleton) != skeleton or skeleton.get_bone_count() != BONES.size():
		_fail("skin binding/bone count mismatch")
		return
	for bone_name in BONES:
		if skeleton.find_bone(bone_name) < 0:
			_fail("bone identity mismatch")
			return
	var clip := player.get_animation(CLIP)
	if clip.get_track_count() != 1 or clip.track_get_type(0) != Animation.TYPE_ROTATION_3D or abs(clip.length - 1.0) > EPS or clip.loop_mode != Animation.LOOP_LINEAR:
		_fail("clip contract mismatch")
		return
	var track_path := clip.track_get_path(0)
	var mixer_root := player.get_node_or_null(player.root_node)
	if mixer_root == null or mixer_root.get_node_or_null(NodePath(str(track_path).get_slice(":", 0))) != skeleton or track_path.get_subname_count() != 1 or str(track_path.get_subname(0)) != "LeftUpperArm":
		_fail("canonical track targets wrong skeleton/bone")
		return
	if clip.track_get_key_count(0) != 3:
		_fail("wrong key count")
		return
	for i in range(3):
		if abs(clip.track_get_key_time(0, i) - float(i) * 0.5) > EPS:
			_fail("wrong key time")
			return
		var expected := Quaternion(Vector3.UP, deg_to_rad(20.0 if i == 1 else 0.0))
		var observed: Quaternion = clip.track_get_key_value(0, i)
		if observed.angle_to(expected) > ANGLE_EPS:
			_fail("wrong key rotation")
			return
	var body := character.get_node_or_null("PhysicsBody") as StaticBody3D
	var capsule_node := character.get_node_or_null("PhysicsBody/CollisionShape3D") as CollisionShape3D
	if body == null or capsule_node == null or not (capsule_node.shape is CapsuleShape3D):
		_fail("capsule missing")
		return
	var capsule := capsule_node.shape as CapsuleShape3D
	var fixed_nodes: Array[Node3D] = [scene, character, body, capsule_node, skeleton, mesh]
	var transforms: Array[Transform3D] = []
	for node in fixed_nodes:
		transforms.append(node.global_transform)
	var capsule_size := Vector2(capsule.radius, capsule.height)
	var bone := skeleton.find_bone("LeftUpperArm")
	player.pause()
	player.seek(0.0, true)
	for _i in range(3):
		await process_frame
	if skeleton.get_bone_pose_rotation(bone).angle_to(Quaternion.IDENTITY) > ANGLE_EPS:
		_fail("t0 pose mismatch")
		return
	var rest := _vertices(mesh.bake_mesh_from_current_skeleton_pose())
	player.seek(0.5, true)
	for _i in range(3):
		await process_frame
	if skeleton.get_bone_pose_rotation(bone).angle_to(Quaternion(Vector3.UP, deg_to_rad(20.0))) > ANGLE_EPS:
		_fail("t0.5 pose mismatch")
		return
	var posed := _vertices(mesh.bake_mesh_from_current_skeleton_pose())
	if rest.is_empty() or rest.size() != posed.size():
		_fail("baked vertices invalid")
		return
	var affected_count := 0
	var unaffected_count := 0
	var affected_delta := 0.0
	var unaffected_delta := 0.0
	for i in range(rest.size()):
		var delta := rest[i].distance_to(posed[i])
		if _inside(rest[i], Vector3(-0.65, 1.25, -0.12), Vector3(-0.15, 1.55, 0.12)):
			affected_count += 1
			affected_delta = maxf(affected_delta, delta)
		if _inside(rest[i], Vector3(-0.12, 0.85, -0.12), Vector3(0.12, 1.15, 0.12)):
			unaffected_count += 1
			unaffected_delta = maxf(unaffected_delta, delta)
	if affected_count == 0 or unaffected_count == 0 or affected_delta < 0.012 or affected_delta > 0.35 or unaffected_delta > 0.008:
		_fail("fixed weighted/unaffected region deformation mismatch")
		return
	for i in range(fixed_nodes.size()):
		if not fixed_nodes[i].global_transform.is_equal_approx(transforms[i]):
			_fail("root/capsule/model transform changed")
			return
	if Vector2(capsule.radius, capsule.height).distance_to(capsule_size) > EPS:
		_fail("capsule dimensions changed")
		return
	print("PASS: exact_animation_track_and_pose affected=", affected_delta, " unaffected=", unaffected_delta)
	quit(0)

func _inside(p: Vector3, mins: Vector3, maxs: Vector3) -> bool:
	return p.x >= mins.x - EPS and p.x <= maxs.x + EPS and p.y >= mins.y - EPS and p.y <= maxs.y + EPS and p.z >= mins.z - EPS and p.z <= maxs.z + EPS

func _vertices(mesh: Mesh) -> PackedVector3Array:
	if mesh == null or mesh.get_surface_count() != 1:
		return PackedVector3Array()
	return mesh.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]

func _find_skeleton(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton(child)
		if found != null:
			return found
	return null

func _find_mesh(node: Node) -> MeshInstance3D:
	if node is MeshInstance3D and node.name == "SM_HumanoidSkin":
		return node
	for child in node.get_children():
		var found := _find_mesh(child)
		if found != null:
			return found
	return null

func _fail(message: String) -> void:
	print("FAIL: ", message)
	quit(1)
