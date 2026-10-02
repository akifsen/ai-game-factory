extends "res://animation_review_controller.gd"

## Interactive review controller for V0.8-8 multi-clip animation review sets.

@onready var _clip_option: OptionButton = %ClipOption

var _ordered_clip_ids: PackedStringArray = PackedStringArray()


func _wire_ui() -> void:
	super._wire_ui()
	_clip_option.item_selected.connect(_on_clip_option_item_selected)


func _set_controls_enabled(enabled: bool) -> void:
	super._set_controls_enabled(enabled)
	_clip_option.disabled = not enabled


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
		if not _player.review_set_player_ready():
			await get_tree().process_frame
			continue
		if _try_finalize_review_setup():
			_populate_clip_selector()
			return
		await get_tree().process_frame
	push_error("animation review set timed out waiting for authored clips")


func _populate_clip_selector() -> void:
	_ordered_clip_ids = _player.review_set_source_clip_ids()
	_clip_option.clear()
	for clip_id in _ordered_clip_ids:
		_clip_option.add_item(clip_id)
	_set_clip_option_index(0, false)


func review_set_clip_ids() -> PackedStringArray:
	return _ordered_clip_ids.duplicate()


func request_select_clip(clip_id: Variant) -> Dictionary:
	if not _ready_for_review:
		return {"ok": false, "error_code": "not_ready"}
	if typeof(clip_id) == TYPE_BOOL:
		return {"ok": false, "error_code": "invalid_clip_id"}
	if typeof(clip_id) != TYPE_STRING:
		return {"ok": false, "error_code": "invalid_clip_id"}
	var clip_key := str(clip_id)
	if clip_key.is_empty():
		return {"ok": false, "error_code": "invalid_clip_id"}
	var index := _ordered_clip_ids.find(clip_key)
	if index < 0:
		return {"ok": false, "error_code": "unknown_clip_id"}

	var session_speed := _player.speed_scale
	var session_loop := _loop_check.button_pressed

	_player.pause()
	_reset_all_skeleton_bone_poses()
	if not _install_review_clone(clip_key):
		return {"ok": false, "error_code": "clip_select_failed"}

	_source_clip_id = clip_key
	_duration = _player.get_animation(_source_clip_id).length
	_player.pause()
	_player.seek(0.0, true)
	_player.speed_scale = session_speed
	_seek_slider.min_value = 0.0
	_seek_slider.max_value = maxf(_duration, TIME_EPS)
	_apply_loop_mode_to_review_clone(session_loop)
	_sync_ui_from_player()
	_set_clip_option_index(index, false)
	return {"ok": true}


func _on_clip_option_item_selected(index: int) -> void:
	if _ui_syncing:
		return
	if index < 0 or index >= _ordered_clip_ids.size():
		return
	request_select_clip(_ordered_clip_ids[index])


func _set_clip_option_index(index: int, _from_user: bool) -> void:
	_ui_syncing = true
	_clip_option.set_block_signals(true)
	_clip_option.select(index)
	_clip_option.set_block_signals(false)
	_ui_syncing = false


func _reset_all_skeleton_bone_poses() -> void:
	var preview := get_node_or_null("AnimationPreview")
	if preview == null:
		return
	var skeleton := _find_skeleton_recursive(preview)
	if skeleton == null:
		return
	for bone_idx in range(skeleton.get_bone_count()):
		skeleton.reset_bone_pose(bone_idx)


func _find_skeleton_recursive(node: Node) -> Skeleton3D:
	if node is Skeleton3D:
		return node
	for child in node.get_children():
		var found := _find_skeleton_recursive(child)
		if found != null:
			return found
	return null
