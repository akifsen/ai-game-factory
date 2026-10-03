extends SceneTree

## Headless check-only entry for compare-session controller validation probe.

func _initialize() -> void:
	var script := load("res://animation_review_compare_session_controller.gd")
	if script == null:
		push_error("compare session controller missing")
		quit(1)
		return
	var node := Node3D.new()
	node.set_script(script)
	if not node.has_method("_run_bridge_validation_probe"):
		push_error("compare session controller missing validation probe")
		quit(1)
		return
	var ok: bool = node.call("_run_bridge_validation_probe")
	quit(0 if ok else 1)
