extends Node

var _player_hp: int = 100
var _enemies_remaining: int = 3
var _score: int = 0
var _snapshot_buffer: Dictionary = {}


func apply_damage(amount: int) -> void:
	_player_hp = maxi(0, _player_hp - amount)


func defeat_enemy() -> void:
	if _enemies_remaining > 0:
		_enemies_remaining -= 1
		_score += 100


func verification_snapshot() -> Dictionary:
	# Reuse the same dictionary to exercise the harness's observation-copy rule.
	_snapshot_buffer["player_hp"] = _player_hp
	_snapshot_buffer["enemies_remaining"] = _enemies_remaining
	_snapshot_buffer["score"] = _score
	return _snapshot_buffer
