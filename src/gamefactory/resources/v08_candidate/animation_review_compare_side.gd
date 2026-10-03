extends Node3D

## One compare pane: preview rig, review clone, and canonical rest-bounds camera fit.

const REVIEW_LIBRARY := "gf_review"
const REVIEW_ANIMATION := "gf_review_playback"
const READY_FRAME_BUDGET := 240
const TIME_EPS := 0.0001

const CAMERA_FIT_MIN_DISTANCE := 0.45
const CAMERA_FIT_MAX_DISTANCE := 14.0
const CAMERA_FIT_ITERATIONS := 28
const VIEWPORT_EDGE_MARGIN_PX := 14.0

@onready var _review_camera: Camera3D = $ReviewCamera
@onready var _review_light: DirectionalLight3D = $ReviewDirectionalLight

var _player: AnimationPlayer
var _source_clip_id: String = ""
var _review_animation_key: String = ""
var _duration: float = 0.0
var _side_ready: bool = false
var _camera_fit_rest_world_vertices: PackedVector3Array = PackedVector3Array()
var _refit_camera_serial: int = 0
var _source_clip_ids: PackedStringArray = PackedStringArray()


func _ready() -> void:
	if not get_viewport().size_changed.is_connected(_on_viewport_resized):
		get_viewport().size_changed.connect(_on_viewport_resized)
	call_deferred("_bootstrap_side")


func _bootstrap_side() -> void:
	var preview := get_node_or_null("AnimationPreview")
	if preview == null:
		push_error("compare side AnimationPreview missing")
		return
	_player = preview.get_node_or_null("AnimationPlayer") as AnimationPlayer
	if _player == null:
		push_error("compare side AnimationPlayer missing")
		return
	_configure_manual_animation_mixer()
	var frames := 0
	while frames < READY_FRAME_BUDGET:
		frames += 1
		if not _player.review_set_player_ready():
			await get_tree().process_frame
			continue
		_source_clip_ids = _player.review_set_source_clip_ids()
		_player.pause()
		_player.speed_scale = 0.0
		_reset_all_skeleton_bone_poses()
		_force_skin_mesh_update()
		if not _cache_camera_fit_rest_world_vertices():
			push_error("compare side failed to cache rest vertices")
			return
		_side_ready = true
		call_deferred("side_refit_camera")
		return
	push_error("compare side timed out waiting for review set player")


func side_is_ready() -> bool:
	return _side_ready


func side_source_clip_ids() -> PackedStringArray:
	return _source_clip_ids.duplicate()


func side_select_clip(clip_id: String) -> bool:
	if not _side_ready:
		return false
	if not _source_clip_ids.has(clip_id):
		return false
	_player.pause()
	_player.speed_scale = 0.0
	_reset_all_skeleton_bone_poses()
	if not _install_review_clone(clip_id):
		return false
	_source_clip_id = clip_id
	_duration = _player.get_animation(_source_clip_id).length
	_player.pause()
	_player.speed_scale = 0.0
	_player.seek(0.0, true)
	_player.advance(0.0)
	return true


func side_duration_seconds() -> float:
	return _duration


func side_source_clip_id() -> String:
	return _source_clip_id


func side_sample_normalized(progress: float) -> void:
	if not _side_ready or _review_animation_key.is_empty():
		return
	var clamped := clampf(progress, 0.0, 1.0)
	var time_s := clamped * _duration
	_player.pause()
	_player.speed_scale = 0.0
	_player.seek(time_s, true)
	_player.advance(0.0)


func side_position_seconds() -> float:
	if _player == null or _review_animation_key.is_empty():
		return 0.0
	return _player.current_animation_position


func _configure_manual_animation_mixer() -> void:
	if _player == null:
		return
	_player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL


func _install_review_clone(source_clip_id: String) -> bool:
	var source_anim := _player.get_animation(source_clip_id)
	if source_anim == null:
		return false
	if _player.has_animation_library(REVIEW_LIBRARY):
		_player.remove_animation_library(REVIEW_LIBRARY)
	var library := AnimationLibrary.new()
	var clone := source_anim.duplicate(true) as Animation
	if clone == null:
		return false
	clone.loop_mode = Animation.LOOP_NONE
	library.add_animation(REVIEW_ANIMATION, clone)
	_player.add_animation_library(REVIEW_LIBRARY, library)
	_review_animation_key = "%s/%s" % [REVIEW_LIBRARY, REVIEW_ANIMATION]
	_player.assigned_animation = _review_animation_key
	_player.pause()
	_player.speed_scale = 0.0
	return _player.has_animation(_review_animation_key)


func _on_viewport_resized() -> void:
	if _side_ready:
		side_refit_camera()


func side_refit_camera() -> void:
	if _player == null or _review_camera == null:
		return
	if _camera_fit_rest_world_vertices.is_empty():
		return
	_refit_camera_serial += 1
	var serial := _refit_camera_serial
	await get_tree().process_frame
	if serial != _refit_camera_serial:
		return
	_apply_camera_fit_from_world_points(_camera_fit_rest_world_vertices)


func _apply_camera_fit_from_world_points(world_points: PackedVector3Array) -> void:
	if world_points.is_empty() or _review_camera == null:
		return
	var bounds := _points_aabb(world_points)
	var safe := _viewport_safe_rect()
	if safe.size.y <= 1.0 or safe.size.x <= 1.0:
		return
	var center := bounds.get_center()
	var view_basis := _review_camera.global_transform.basis
	var lo := CAMERA_FIT_MIN_DISTANCE
	var hi := CAMERA_FIT_MAX_DISTANCE
	if not _camera_distance_fits(center, hi, world_points, safe, view_basis):
		while hi < 40.0 and not _camera_distance_fits(center, hi, world_points, safe, view_basis):
			hi += 2.0
	var best_distance := hi
	for _i in range(CAMERA_FIT_ITERATIONS):
		var mid := (lo + hi) * 0.5
		if _camera_distance_fits(center, mid, world_points, safe, view_basis):
			best_distance = mid
			hi = mid
		else:
			lo = mid
	_place_review_camera(center, best_distance, view_basis)
	_align_review_light(center)


func _cache_camera_fit_rest_world_vertices() -> bool:
	var mesh := _find_review_mesh()
	if mesh == null:
		return false
	_camera_fit_rest_world_vertices = _baked_mesh_world_vertices(mesh)
	return not _camera_fit_rest_world_vertices.is_empty()


func _viewport_safe_rect() -> Rect2:
	var visible := get_viewport().get_visible_rect()
	var margin := VIEWPORT_EDGE_MARGIN_PX
	return Rect2(
		visible.position.x + margin,
		visible.position.y + margin,
		maxf(8.0, visible.size.x - margin * 2.0),
		maxf(8.0, visible.size.y - margin * 2.0),
	)


func _place_review_camera(target: Vector3, distance: float, view_basis: Basis) -> void:
	_review_camera.global_transform = Transform3D(view_basis, target + view_basis.z * distance)


func _camera_distance_fits(
	target: Vector3,
	distance: float,
	world_points: PackedVector3Array,
	safe: Rect2,
	view_basis: Basis,
) -> bool:
	_place_review_camera(target, distance, view_basis)
	return _world_points_fit_screen(_review_camera, world_points, safe)


func _world_points_fit_screen(
	camera: Camera3D, world_points: PackedVector3Array, safe: Rect2
) -> bool:
	for point in world_points:
		if camera.is_position_behind(point):
			return false
		var screen := camera.unproject_position(point)
		if screen.x < safe.position.x or screen.y < safe.position.y:
			return false
		if screen.x > safe.position.x + safe.size.x or screen.y > safe.position.y + safe.size.y:
			return false
	return true


func _align_review_light(target: Vector3) -> void:
	if _review_light == null or _review_camera == null:
		return
	_review_light.global_transform = _review_camera.global_transform
	_review_light.rotate_object_local(Vector3.RIGHT, deg_to_rad(-18.0))
	_review_light.light_energy = 1.25


func _reset_all_skeleton_bone_poses() -> void:
	var preview := get_node_or_null("AnimationPreview")
	if preview == null:
		return
	var skeleton := _find_skeleton_recursive(preview)
	if skeleton == null:
		return
	for bone_idx in range(skeleton.get_bone_count()):
		skeleton.reset_bone_pose(bone_idx)
		skeleton.force_update_bone_child_transform(bone_idx)


func _force_skin_mesh_update() -> void:
	var preview := get_node_or_null("AnimationPreview")
	if preview == null:
		return
	var skeleton := _find_skeleton_recursive(preview)
	if skeleton == null:
		return
	for bone_idx in range(skeleton.get_bone_count()):
		skeleton.force_update_bone_child_transform(bone_idx)


func _find_skeleton_recursive(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton_recursive(child)
		if found != null:
			return found
	return null


func _find_review_mesh() -> MeshInstance3D:
	var preview := get_node_or_null("AnimationPreview")
	if preview == null:
		return null
	return _find_review_mesh_recursive(preview)


func _find_review_mesh_recursive(node: Node) -> MeshInstance3D:
	if node is MeshInstance3D and node.name == "SM_HumanoidSkin":
		return node
	for child in node.get_children():
		var found := _find_review_mesh_recursive(child)
		if found != null:
			return found
	return null


func _baked_mesh_world_vertices(mesh: MeshInstance3D) -> PackedVector3Array:
	var baked := mesh.bake_mesh_from_current_skeleton_pose()
	if baked == null or baked.get_surface_count() != 1:
		return PackedVector3Array()
	var local: PackedVector3Array = baked.surface_get_arrays(0)[Mesh.ARRAY_VERTEX]
	var world := PackedVector3Array()
	world.resize(local.size())
	var xf := mesh.global_transform
	for i in range(local.size()):
		world[i] = xf * local[i]
	return world


func _points_aabb(points: PackedVector3Array) -> AABB:
	if points.is_empty():
		return AABB()
	var min_v := points[0]
	var max_v := points[0]
	for i in range(1, points.size()):
		min_v = min_v.min(points[i])
		max_v = max_v.max(points[i])
	return AABB(min_v, max_v - min_v)
