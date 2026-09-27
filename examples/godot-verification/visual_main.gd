extends Control

## Reference scene for rendered capture. Gameplay state changes only through
## apply_damage and defeat_enemy. The HUD is painted from that state in
## present_for_capture, never from Factory expected values.

const ARENA := Color8(31, 36, 46)
const PLAYER := Color8(64, 140, 242)
const ENEMY := Color8(217, 56, 46)
const BAR_EMPTY := Color8(51, 51, 56)
const BAR_FILL := Color8(51, 191, 71)
const TEXT := Color8(236, 236, 236)

var _player_hp: int = 100
var _enemies_remaining: int = 3
var _score: int = 0
var _snapshot_buffer: Dictionary = {}
var _arena: ColorRect
var _player: ColorRect
var _enemies: Array[ColorRect] = []
var _bar_bg: ColorRect
var _bar_fill: ColorRect
var _hp_label: Label
var _enemy_label: Label
var _score_label: Label


func _ready() -> void:
	set_anchors_preset(Control.PRESET_FULL_RECT)
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	_build()
	present_for_capture()


func apply_damage(amount: int) -> void:
	_player_hp = maxi(0, _player_hp - amount)


func defeat_enemy() -> void:
	if _enemies_remaining > 0:
		_enemies_remaining -= 1
		_score += 100


func verification_snapshot() -> Dictionary:
	_snapshot_buffer["player_hp"] = _player_hp
	_snapshot_buffer["enemies_remaining"] = _enemies_remaining
	_snapshot_buffer["score"] = _score
	return _snapshot_buffer


func present_for_capture() -> void:
	var layout := _layout(get_viewport_rect().size)
	_arena.position = Vector2.ZERO
	_arena.size = layout.viewport
	_player.position = layout.player.position
	_player.size = layout.player.size
	for index in _enemies.size():
		_enemies[index].position = layout.enemies[index].position
		_enemies[index].size = layout.enemies[index].size
		_enemies[index].visible = index < _enemies_remaining
	_bar_bg.position = layout.bar.position
	_bar_bg.size = layout.bar.size
	var filled: float = float(layout.bar.size.x) * float(_player_hp) / 100.0
	_bar_fill.position = layout.bar.position
	_bar_fill.size = Vector2(filled, layout.bar.size.y)
	_hp_label.position = layout.labels.position
	_hp_label.text = "HP %d" % _player_hp
	_enemy_label.position = layout.labels.position + Vector2(0, 36)
	_enemy_label.text = "Enemies %d" % _enemies_remaining
	_score_label.position = layout.labels.position + Vector2(0, 72)
	_score_label.text = "Score %d" % _score


func _build() -> void:
	_arena = _rect("Arena", ARENA)
	_player = _rect("Player", PLAYER)
	for index in 3:
		_enemies.append(_rect("Enemy%d" % (index + 1), ENEMY))
	_bar_bg = _rect("HealthEmpty", BAR_EMPTY)
	_bar_fill = _rect("HealthFill", BAR_FILL)
	_hp_label = _label("HpLabel")
	_enemy_label = _label("EnemyLabel")
	_score_label = _label("ScoreLabel")


func _rect(node_name: String, color: Color) -> ColorRect:
	var rect := ColorRect.new()
	rect.name = node_name
	rect.color = color
	rect.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(rect)
	return rect


func _label(node_name: String) -> Label:
	var label := Label.new()
	label.name = node_name
	label.add_theme_color_override("font_color", TEXT)
	label.add_theme_font_size_override("font_size", 28)
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(label)
	return label


func _layout(viewport_size: Vector2) -> Dictionary:
	var key := "%dx%d" % [int(viewport_size.x), int(viewport_size.y)]
	if key == "720x1280":
		return {
			"viewport": viewport_size,
			"player": {"position": Vector2(80, 980), "size": Vector2(140, 180)},
			"enemies": [
				{"position": Vector2(70, 520), "size": Vector2(90, 90)},
				{"position": Vector2(280, 520), "size": Vector2(90, 90)},
				{"position": Vector2(490, 520), "size": Vector2(90, 90)},
			],
			"bar": {"position": Vector2(36, 48), "size": Vector2(400, 36)},
			"labels": {"position": Vector2(36, 100)},
		}
	return {
		"viewport": viewport_size,
		"player": {"position": Vector2(80, 480), "size": Vector2(120, 160)},
		"enemies": [
			{"position": Vector2(220, 280), "size": Vector2(100, 100)},
			{"position": Vector2(520, 280), "size": Vector2(100, 100)},
			{"position": Vector2(820, 280), "size": Vector2(100, 100)},
		],
		"bar": {"position": Vector2(48, 36), "size": Vector2(480, 32)},
		"labels": {"position": Vector2(48, 80)},
	}
