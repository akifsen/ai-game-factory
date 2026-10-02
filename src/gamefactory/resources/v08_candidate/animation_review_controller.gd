extends Node3D

## Interactive review controller for V0.8-6 animation clip preview packages.

const REVIEW_LIBRARY := "gf_review"
const REVIEW_ANIMATION := "gf_review_playback"
const READY_FRAME_BUDGET := 240
const ALLOWED_SPEEDS: Array[float] = [0.25, 0.5, 1.0, 2.0]
const TIME_EPS := 0.0001

@onready var _play_button: Button = %PlayButton
@onready var _pause_button: Button = %PauseButton
@onready var _restart_button: Button = %RestartButton
@onready var _seek_slider: HSlider = %SeekSlider
@onready var _loop_check: CheckButton = %LoopCheck
@onready var _speed_option: OptionButton = %SpeedOption
@onready var _time_label: Label = %TimeLabel
@onready var _duration_label: Label = %DurationLabel
@onready var _review_camera: Camera3D = $ReviewCamera
@onready var _review_light: DirectionalLight3D = $ReviewDirectionalLight
@onready var _bottom_panel: Control = $UI/Root/Margin/Panel

const CAMERA_FIT_MIN_DISTANCE := 0.45
const CAMERA_FIT_MAX_DISTANCE := 14.0
const CAMERA_FIT_ITERATIONS := 28
const VIEWPORT_EDGE_MARGIN_PX := 14.0
const PANEL_CLEARANCE_PX := 10.0

var _player: AnimationPlayer
var _source_clip_id: String = ""
var _review_animation_key: String = ""
var _duration: float = 0.0
var _ready_for_review: bool = false
var _ui_syncing: bool = false
var _slider_dragging: bool = false
var _camera_fit_rest_world_vertices: PackedVector3Array = PackedVector3Array()
var _refit_camera_serial: int = 0


func _ready() -> void:
	_wire_ui()
	_speed_option.clear()
	for speed in ALLOWED_SPEEDS:
		_speed_option.add_item("%sx" % _format_speed_label(speed), int(speed * 1000.0))
	_speed_option.select(2)
	if not get_viewport().size_changed.is_connected(_on_review_viewport_resized):
		get_viewport().size_changed.connect(_on_review_viewport_resized)
	call_deferred("_bootstrap_review")


func _bootstrap_review() -> void:
	_set_controls_enabled(false)
	var preview := get_node_or_null("AnimationPreview")
	if preview == null:
		push_error("AnimationPreview instance missing")
		return
	_player = preview.get_node_or_null("AnimationPlayer") as AnimationPlayer
	if _player == null:
		push_error("AnimationPlayer missing under AnimationPreview")
		return
	var frames := 0
	while frames < READY_FRAME_BUDGET:
		frames += 1
		if _try_finalize_review_setup():
			return
		await get_tree().process_frame
	push_error("animation review timed out waiting for authored clip")


func _try_finalize_review_setup() -> bool:
	if _player == null:
		return false
	var assigned := str(_player.assigned_animation)
	if assigned.is_empty():
		assigned = str(_player.current_animation)
	if assigned.is_empty() or not _player.has_animation(assigned):
		return false
	_player.pause()
	_review_animation_key = "%s/%s" % [REVIEW_LIBRARY, REVIEW_ANIMATION]
	if not _install_review_clone(assigned):
		return false
	_source_clip_id = assigned
	_duration = _player.get_animation(_source_clip_id).length
	_player.pause()
	_player.seek(0.0, true)
	_player.speed_scale = 1.0
	_seek_slider.min_value = 0.0
	_seek_slider.max_value = maxf(_duration, TIME_EPS)
	_seek_slider.step = 0.0
	var source_anim := _player.get_animation(_source_clip_id)
	var loop_from_source := source_anim.loop_mode == Animation.LOOP_LINEAR
	_ui_syncing = true
	_loop_check.set_pressed_no_signal(loop_from_source)
	_apply_loop_mode_to_review_clone(loop_from_source)
	_speed_option.set_block_signals(true)
	_speed_option.select(2)
	_speed_option.set_block_signals(false)
	_ui_syncing = false
	_sync_ui_from_player()
	if not _cache_camera_fit_rest_world_vertices():
		push_error("failed to cache rest-pose mesh vertices for camera fit")
		return false
	_ready_for_review = true
	_set_controls_enabled(true)
	call_deferred("review_refit_camera")
	_play_button.grab_focus()
	return true


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
	clone.loop_mode = source_anim.loop_mode
	library.add_animation(REVIEW_ANIMATION, clone)
	if not library.has_animation(REVIEW_ANIMATION):
		return false
	_player.add_animation_library(REVIEW_LIBRARY, library)
	var review_key := "%s/%s" % [REVIEW_LIBRARY, REVIEW_ANIMATION]
	_player.assigned_animation = review_key
	return _player.has_animation(review_key)


func _wire_ui() -> void:
	_play_button.pressed.connect(_on_play_pressed)
	_pause_button.pressed.connect(_on_pause_pressed)
	_restart_button.pressed.connect(_on_restart_pressed)
	_loop_check.toggled.connect(_on_loop_toggled)
	_speed_option.item_selected.connect(_on_speed_selected)
	_seek_slider.value_changed.connect(_on_seek_slider_changed)
	_seek_slider.drag_started.connect(func() -> void:
		_slider_dragging = true
	)
	_seek_slider.drag_ended.connect(func(_value_changed: bool) -> void:
		_slider_dragging = false
	)


func _process(_delta: float) -> void:
	if not _ready_for_review or _ui_syncing:
		return
	_sync_ui_from_player()


func _sync_ui_from_player() -> void:
	if _player == null:
		return
	_ui_syncing = true
	var position := _player.current_animation_position
	_time_label.text = "%.3fs" % position
	_duration_label.text = "/ %.3fs" % _duration
	if not _slider_dragging:
		_seek_slider.set_value_no_signal(position)
	_ui_syncing = false


func _set_controls_enabled(enabled: bool) -> void:
	_play_button.disabled = not enabled
	_pause_button.disabled = not enabled
	_restart_button.disabled = not enabled
	_seek_slider.editable = enabled
	_seek_slider.mouse_filter = Control.MOUSE_FILTER_STOP if enabled else Control.MOUSE_FILTER_IGNORE
	_loop_check.disabled = not enabled
	_speed_option.disabled = not enabled


func _format_speed_label(speed: float) -> String:
	if is_equal_approx(speed, 1.0):
		return "1"
	if is_equal_approx(speed, 2.0):
		return "2"
	return str(speed)


func _speed_from_index(index: int) -> float:
	if index < 0 or index >= ALLOWED_SPEEDS.size():
		return -1.0
	return ALLOWED_SPEEDS[index]


func _index_for_speed(speed_f: float) -> int:
	for i in range(ALLOWED_SPEEDS.size()):
		if ALLOWED_SPEEDS[i] == speed_f:
			return i
	return -1


func _apply_loop_mode_to_review_clone(enabled: bool) -> void:
	if _review_animation_key.is_empty():
		return
	var review_anim := _player.get_animation(_review_animation_key)
	if review_anim == null:
		return
	review_anim.loop_mode = Animation.LOOP_LINEAR if enabled else Animation.LOOP_NONE


func _on_play_pressed() -> void:
	request_play()


func _on_pause_pressed() -> void:
	request_pause()


func _on_restart_pressed() -> void:
	request_restart()


func _on_loop_toggled(pressed: bool) -> void:
	request_set_loop(pressed)


func _on_speed_selected(index: int) -> void:
	request_set_playback_speed(_speed_from_index(index))


func _on_seek_slider_changed(value: float) -> void:
	if _ui_syncing:
		return
	request_seek(value)


func request_play() -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	_player.play(_review_animation_key)
	return {"ok": true}


func request_pause() -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	_player.pause()
	return {"ok": true}


func request_restart() -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	_player.pause()
	_player.seek(0.0, true)
	_player.play(_review_animation_key)
	return {"ok": true}


func request_seek(time: Variant) -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(time) == TYPE_BOOL:
		return {"ok": false, "error_code": "invalid_seek_time"}
	if typeof(time) != TYPE_FLOAT and typeof(time) != TYPE_INT:
		return {"ok": false, "error_code": "invalid_seek_time"}
	var seek_s := float(time)
	if not is_finite(seek_s):
		return {"ok": false, "error_code": "invalid_seek_time"}
	if seek_s < 0.0 or seek_s > _duration:
		return {"ok": false, "error_code": "seek_out_of_range"}
	_player.pause()
	_player.seek(seek_s, true)
	_sync_ui_from_player()
	return {"ok": true}


func _is_exact_allowed_speed(speed_f: float) -> bool:
	if not is_finite(speed_f):
		return false
	for candidate in ALLOWED_SPEEDS:
		if speed_f == candidate:
			return true
	return false


func request_set_playback_speed(speed: Variant) -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(speed) == TYPE_BOOL:
		return {"ok": false, "error_code": "invalid_playback_speed"}
	if typeof(speed) != TYPE_FLOAT and typeof(speed) != TYPE_INT:
		return {"ok": false, "error_code": "invalid_playback_speed"}
	var speed_f := float(speed)
	if not _is_exact_allowed_speed(speed_f):
		return {"ok": false, "error_code": "invalid_playback_speed"}
	var previous := _player.speed_scale
	_player.speed_scale = speed_f
	if _player.speed_scale != speed_f:
		_player.speed_scale = previous
		return {"ok": false, "error_code": "invalid_playback_speed"}
	var speed_index := _index_for_speed(speed_f)
	if speed_index >= 0:
		_ui_syncing = true
		_speed_option.set_block_signals(true)
		_speed_option.select(speed_index)
		_speed_option.set_block_signals(false)
		_ui_syncing = false
	return {"ok": true}


func request_set_loop(loop_flag: Variant) -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(loop_flag) != TYPE_BOOL:
		return {"ok": false, "error_code": "invalid_loop"}
	_apply_loop_mode_to_review_clone(loop_flag)
	_ui_syncing = true
	_loop_check.set_pressed_no_signal(loop_flag)
	_ui_syncing = false
	return {"ok": true}


func review_is_ready() -> bool:
	return _ready_for_review


func review_source_clip_id() -> String:
	return _source_clip_id


func review_duration_seconds() -> float:
	return _duration


func review_animation_player() -> AnimationPlayer:
	return _player


func review_source_animation() -> Animation:
	if _source_clip_id.is_empty():
		return null
	return _player.get_animation(_source_clip_id)


func review_clone_animation() -> Animation:
	if _review_animation_key.is_empty():
		return null
	return _player.get_animation(_review_animation_key)


func _on_review_viewport_resized() -> void:
	if _ready_for_review:
		review_refit_camera()


func review_refit_camera() -> void:
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
	print(
		"GF_REVIEW_CAMERA_FIT bounds_min=%s bounds_max=%s distance=%.4f safe_rect=%s camera_pos=%s fov=%.2f"
		% [
			str(bounds.position),
			str(bounds.position + bounds.size),
			best_distance,
			str(safe),
			str(_review_camera.global_position),
			_review_camera.fov,
		]
	)


func _cache_camera_fit_rest_world_vertices() -> bool:
	var mesh := _find_review_mesh()
	if mesh == null:
		return false
	_camera_fit_rest_world_vertices = _baked_mesh_world_vertices(mesh)
	return not _camera_fit_rest_world_vertices.is_empty()


func _viewport_safe_rect() -> Rect2:
	var vp_size := get_viewport().get_visible_rect().size
	var panel_top := vp_size.y
	if _bottom_panel != null:
		panel_top = _bottom_panel.get_global_rect().position.y
	var top := VIEWPORT_EDGE_MARGIN_PX
	var bottom := maxf(top + 8.0, panel_top - PANEL_CLEARANCE_PX)
	return Rect2(
		VIEWPORT_EDGE_MARGIN_PX,
		top,
		maxf(8.0, vp_size.x - VIEWPORT_EDGE_MARGIN_PX * 2.0),
		maxf(8.0, bottom - top)
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
