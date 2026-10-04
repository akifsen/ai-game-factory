"""Create a small, ordinary Godot project in a new directory."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_staging import _is_reparse
from gamefactory.core.domain.errors import ValidationError


def create_godot_project(
    destination: Path | str, name: str, dimension: str = "2d", *, overwrite: bool = False
) -> dict[str, Any]:
    if overwrite:
        raise ValidationError("Overwriting a project is unsupported")
    if dimension not in {"2d", "3d"}:
        raise ValidationError("dimension must be 2d or 3d")
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 80
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ValidationError("name must contain 1 to 80 characters")
    target = Path(destination).absolute()
    current = target
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Destination path cannot contain a symbolic link or junction")
        current = current.parent
    if target.exists() or not target.parent.is_dir():
        raise ValidationError("destination must be a new directory with an existing parent")
    display_name = name.strip().replace('"', "'")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", display_name).strip("_") or "Game"
    if dimension == "2d":
        scene = '[gd_scene load_steps=2 format=3]\n\n[ext_resource type="Script" path="res://main.gd" id="1"]\n\n[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n'
        script = 'extends Node2D\n\nfunc _ready() -> void:\n    queue_redraw()\n\nfunc _draw() -> void:\n    draw_circle(get_viewport_rect().size / 2.0, 48.0, Color("66c2a5"))\n'
    else:
        scene = '[gd_scene load_steps=4 format=3]\n\n[ext_resource type="Script" path="res://main.gd" id="1"]\n[sub_resource type="StandardMaterial3D" id="Material_main"]\nalbedo_color = Color(0.2, 0.65, 0.52, 1)\n[sub_resource type="BoxMesh" id="BoxMesh_main"]\nmaterial = SubResource("Material_main")\n\n[node name="Main" type="Node3D"]\nscript = ExtResource("1")\n\n[node name="MeshInstance3D" type="MeshInstance3D" parent="."]\nmesh = SubResource("BoxMesh_main")\n\n[node name="Camera3D" type="Camera3D" parent="."]\nposition = Vector3(0, 2, 4)\nrotation_degrees = Vector3(-20, 0, 0)\ncurrent = true\n\n[node name="DirectionalLight3D" type="DirectionalLight3D" parent="."]\nrotation_degrees = Vector3(-35, -25, 0)\n'
        script = (
            "extends Node3D\n\nfunc _process(delta: float) -> void:\n    rotate_y(delta * 0.6)\n"
        )
    quoted_name = json_quote(display_name)
    cfg = f'config_version=5\n\n[application]\nconfig/name={quoted_name}\nrun/main_scene="res://main.tscn"\nconfig/features=PackedStringArray("4.0", "GL Compatibility")\n\n[rendering]\nrenderer/rendering_method="gl_compatibility"\nrenderer/rendering_method.mobile="gl_compatibility"\n'
    staged_files = (("project.godot", cfg), ("main.tscn", scene), ("main.gd", script))
    staging = Path(tempfile.mkdtemp(prefix=".gamefactory-project-", dir=target.parent))
    created: list[tuple[Path, tuple[int, int], str, int]] = []
    published = False
    owns_target = False
    recovery_paths: list[Path] = []
    recovery_required = False
    publication_failure: Exception | None = None
    try:
        for filename, content in staged_files:
            staged = staging / filename
            with staged.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # Reserve the target exclusively. Each final name is also created without
        # replacement; on a partial failure only our own known files are removed.
        target.mkdir(exist_ok=False)
        owns_target = True
        for filename, content in staged_files:
            source, final = staging / filename, target / filename
            os.link(source, final)
            info = final.stat()
            encoded = content.encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            created.append((final, (info.st_dev, info.st_ino), digest, len(encoded)))
        published = True
    except Exception as exc:
        publication_failure = exc
    finally:
        if not published:
            for final, identity, digest, expected_size in created:
                try:
                    if not final.exists():
                        continue
                    captured = final.with_name(
                        f".{final.name}.gamefactory-recovery-{os.urandom(8).hex()}"
                    )
                    os.rename(final, captured)
                    info = captured.lstat()
                    owned = (
                        stat.S_ISREG(info.st_mode)
                        and info.st_size == expected_size
                        and (info.st_dev, info.st_ino) == identity
                        and hashlib.sha256(captured.read_bytes()).hexdigest() == digest
                    )
                    if owned:
                        captured.unlink()
                    else:
                        recovery_required = True
                        recovery_paths.append(captured)
                        try:
                            os.link(captured, final)
                        except FileExistsError:
                            pass
                except FileNotFoundError:
                    pass
            if owns_target:
                try:
                    target.rmdir()
                except OSError:
                    pass
        for filename, content in staged_files:
            staged = staging / filename
            try:
                encoded = content.encode("utf-8")
                if (
                    staged.is_file()
                    and staged.stat().st_size == len(encoded)
                    and hashlib.sha256(staged.read_bytes()).hexdigest()
                    == hashlib.sha256(encoded).hexdigest()
                ):
                    staged.unlink()
            except FileNotFoundError:
                pass
        try:
            staging.rmdir()
        except OSError:
            pass
    if recovery_required:
        raise ValidationError(
            f"Concurrent project file changes were preserved for recovery: {', '.join(str(path) for path in recovery_paths)}"
        ) from publication_failure
    if publication_failure is not None:
        raise publication_failure
    return {
        "schema_version": 1,
        "created": True,
        "root": str(target.resolve()),
        "name": slug,
        "dimension": dimension,
        "files": ["project.godot", "main.tscn", "main.gd"],
    }


def json_quote(value: str) -> str:
    """Godot config quoted strings use JSON-compatible escaping."""
    import json

    return json.dumps(value, ensure_ascii=False)
