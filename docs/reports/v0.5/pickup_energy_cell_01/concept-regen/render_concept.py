"""Concept render for pickup_energy_cell_01 / r001 (local Blender, no network).

Builds a stylized sci-fi energy cell at spec scale (0.22 x 0.22 x 0.48 m,
Z-up, front = -Y) and renders a 3/4 hero view with Cycles.
"""
import math
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else "//concept.png"
SAMPLES = int(argv[1]) if len(argv) > 1 else 192

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


# ---------------------------------------------------------------- materials
def principled(name, color, metallic=0.0, rough=0.5, coat=0.0,
               emission=None, strength=0.0, transmission=0.0, ior=1.45, alpha=1.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")

    def setin(key, val):
        if key in bsdf.inputs:
            bsdf.inputs[key].default_value = val

    setin("Base Color", (*color, 1.0))
    setin("Metallic", metallic)
    setin("Roughness", rough)
    setin("Coat Weight", coat)
    setin("Coat Roughness", 0.15)
    setin("Transmission Weight", transmission)
    setin("IOR", ior)
    setin("Alpha", alpha)
    if emission:
        setin("Emission Color", (*emission, 1.0))
        setin("Emission Strength", strength)
    return mat


M_SHELL = principled("gunmetal_shell", (0.05, 0.055, 0.064), metallic=0.8, rough=0.36, coat=0.3)
M_FRAME = principled("graphite_frame", (0.02, 0.022, 0.026), metallic=0.5, rough=0.5)
M_STEEL = principled("edge_steel", (0.3, 0.32, 0.36), metallic=1.0, rough=0.25)
M_GLASS = principled("chamber_glass", (0.92, 0.98, 1.0), rough=0.0, transmission=1.0, ior=1.45)
M_CORE = principled("energy_core", (0.1, 0.8, 1.0), emission=(0.0, 0.62, 1.0), strength=4.5)
M_CORE_HOT = principled("energy_core_hot", (0.6, 0.97, 1.0), emission=(0.45, 0.95, 1.0), strength=12.0)
M_LED = principled("indicator", (0.1, 0.8, 1.0), emission=(0.0, 0.7, 1.0), strength=3.5)


# ---------------------------------------------------------------- geometry
def finish(obj, mat, bevel=0.0, segments=3):
    obj.data.materials.append(mat)
    if bevel > 0:
        mod = obj.modifiers.new("bevel", "BEVEL")
        mod.width = bevel
        mod.segments = segments
        mod.limit_method = "ANGLE"
        mod.harden_normals = False
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    try:
        bpy.ops.object.shade_auto_smooth(angle=math.radians(40))
    except Exception:
        try:
            bpy.ops.object.shade_smooth_by_angle(angle=math.radians(40))
        except Exception:
            bpy.ops.object.shade_flat()
    obj.select_set(False)
    return obj


def box(name, size, loc, mat, bevel=0.004):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = size
    bpy.ops.object.transform_apply(scale=True)
    return finish(obj, mat, bevel)


def cyl(name, r, depth, loc, mat, verts=64, bevel=0.0, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=r, depth=depth, location=loc, rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    return finish(obj, mat, bevel)


def torus(name, major, minor, z, mat):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor,
                                     major_segments=64, minor_segments=16, location=(0, 0, z))
    obj = bpy.context.active_object
    obj.name = name
    return finish(obj, mat)


def cut(target, cutter):
    mod = target.modifiers.new("cut", "BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.object = cutter
    cutter.hide_render = True
    cutter.hide_viewport = True


# Bottom cap assembly (z 0.00 - 0.09)
bottom = box("bottom_cap", (0.22, 0.22, 0.072), (0, 0, 0.036), M_SHELL, bevel=0.014)
box("bottom_collar", (0.196, 0.196, 0.018), (0, 0, 0.081), M_FRAME, bevel=0.004)
box("bottom_plinth_band", (0.224, 0.224, 0.012), (0, 0, 0.022), M_FRAME, bevel=0.004)

# Top cap assembly (z 0.375 - 0.48)
top = box("top_cap", (0.22, 0.22, 0.068), (0, 0, 0.424), M_SHELL, bevel=0.014)
box("top_collar", (0.196, 0.196, 0.018), (0, 0, 0.384), M_FRAME, bevel=0.004)
cyl("top_terminal", 0.062, 0.016, (0, 0, 0.466), M_FRAME, bevel=0.004)
cyl("top_terminal_ring", 0.044, 0.01, (0, 0, 0.477), M_STEEL, bevel=0.002)
cyl("top_terminal_core", 0.026, 0.006, (0, 0, 0.4805), M_LED)

# Chamber frame (z 0.09 - 0.375)
CH_Z0, CH_Z1 = 0.09, 0.375
CH_H = CH_Z1 - CH_Z0
CH_C = (CH_Z0 + CH_Z1) / 2
for sx in (-1, 1):
    for sy in (-1, 1):
        box(f"pillar_{sx}_{sy}", (0.04, 0.04, CH_H), (sx * 0.086, sy * 0.086, CH_C), M_SHELL, bevel=0.009)
        box(f"pillar_trim_{sx}_{sy}", (0.012, 0.012, CH_H * 0.8),
            (sx * 0.1035, sy * 0.1035, CH_C), M_STEEL, bevel=0.003)

# Side armor plates with a vertical glow slot
for sx in (-1, 1):
    plate = box(f"side_plate_{sx}", (0.018, 0.134, CH_H), (sx * 0.093, 0.0, CH_C), M_FRAME, bevel=0.003)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(sx * 0.093, -0.012, CH_C))
    slot = bpy.context.active_object
    slot.scale = (0.05, 0.016, CH_H * 0.62)
    cut(plate, slot)
    for i, dz in enumerate((-0.1, -0.085, -0.07, 0.07, 0.085, 0.1)):
        box(f"vent_{sx}_{i}", (0.004, 0.05, 0.005), (sx * 0.1025, 0.032, CH_C + dz), M_SHELL, bevel=0.001)
box("back_plate", (0.134, 0.018, CH_H), (0, 0.093, CH_C), M_FRAME, bevel=0.003)

# Energy chamber: glass tube, mounts, containment rings, hex core
cyl("chamber_glass", 0.071, CH_H - 0.004, (0, 0, CH_C), M_GLASS, verts=96)
cyl("core_mount_low", 0.058, 0.022, (0, 0, CH_Z0 + 0.012), M_STEEL, bevel=0.003)
cyl("core_mount_high", 0.058, 0.022, (0, 0, CH_Z1 - 0.012), M_STEEL, bevel=0.003)
for i, z in enumerate((0.16, CH_C, 0.305)):
    torus(f"containment_ring_{i}", 0.073, 0.0055, z, M_STEEL)
core = cyl("energy_core", 0.036, CH_H - 0.05, (0, 0, CH_C), M_CORE, verts=6, bevel=0.004)
core.rotation_euler.z = math.radians(30)
cyl("energy_core_inner", 0.016, CH_H - 0.03, (0, 0, CH_C), M_CORE_HOT, verts=24)
for i, z in enumerate((CH_Z0 + 0.03, CH_Z1 - 0.03)):
    bpy.ops.mesh.primitive_cone_add(vertices=6, radius1=0.036, radius2=0.012, depth=0.02,
                                    location=(0, 0, z), rotation=(0 if i else math.pi, 0, math.radians(30)))
    finish(bpy.context.active_object, M_CORE)

# Front panel details on caps: recessed panel lines and indicators
FRONT_Y = -0.111
box("top_front_inset", (0.15, 0.004, 0.03), (0, FRONT_Y, 0.426), M_FRAME, bevel=0.0015)
box("top_front_strip", (0.1, 0.005, 0.006), (0, FRONT_Y - 0.001, 0.426), M_LED, bevel=0.0)
box("bottom_front_inset", (0.15, 0.004, 0.034), (0, FRONT_Y, 0.042), M_FRAME, bevel=0.0015)
for i, x in enumerate((-0.03, 0.0, 0.03)):
    box(f"bottom_led_{i}", (0.014, 0.005, 0.008), (x, FRONT_Y - 0.001, 0.042), M_LED if i < 2 else M_STEEL, bevel=0.0)
for sx in (-1, 1):
    box(f"top_side_inset_{sx}", (0.004, 0.15, 0.03), (sx * 0.111, 0, 0.426), M_FRAME, bevel=0.0015)
    box(f"bottom_side_inset_{sx}", (0.004, 0.15, 0.034), (sx * 0.111, 0, 0.042), M_FRAME, bevel=0.0015)
# Top plane seams around the terminal
for sy in (-1, 1):
    box(f"top_seam_{sy}", (0.16, 0.012, 0.003), (0, sy * 0.082, 0.4585), M_FRAME, bevel=0.001)

# ---------------------------------------------------------------- world
world = bpy.data.worlds.new("backdrop")
scene.world = world
world.use_nodes = True
nt = world.node_tree
nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputWorld")
coord = nt.nodes.new("ShaderNodeTexCoord")
sep = nt.nodes.new("ShaderNodeSeparateXYZ")
ramp = nt.nodes.new("ShaderNodeValToRGB")
ramp.color_ramp.elements[0].color = (0.012, 0.013, 0.016, 1)
ramp.color_ramp.elements[1].color = (0.085, 0.09, 0.1, 1)
cam_bg = nt.nodes.new("ShaderNodeBackground")
light_bg = nt.nodes.new("ShaderNodeBackground")
light_bg.inputs["Color"].default_value = (0.05, 0.055, 0.065, 1)
light_bg.inputs["Strength"].default_value = 1.0
lp = nt.nodes.new("ShaderNodeLightPath")
mix = nt.nodes.new("ShaderNodeMixShader")
nt.links.new(coord.outputs["Window"], sep.inputs["Vector"])
nt.links.new(sep.outputs["Y"], ramp.inputs["Fac"])
nt.links.new(ramp.outputs["Color"], cam_bg.inputs["Color"])
nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"])
nt.links.new(light_bg.outputs["Background"], mix.inputs[1])
nt.links.new(cam_bg.outputs["Background"], mix.inputs[2])
nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])

# ---------------------------------------------------------------- lights
TARGET = Vector((0, 0, 0.235))


def area(name, loc, power, size, color=(1, 1, 1)):
    data = bpy.data.lights.new(name, "AREA")
    data.energy = power
    data.size = size
    data.color = color
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.location = loc
    obj.rotation_euler = (TARGET - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return obj


area("key", (-0.9, -1.1, 1.1), 80, 1.0, (1.0, 0.97, 0.93))
area("fill", (1.3, -0.7, 0.5), 35, 1.2, (0.9, 0.95, 1.0))
area("rim_right", (0.9, 1.0, 0.8), 140, 0.6, (0.75, 0.9, 1.0))
area("rim_left", (-1.0, 0.9, 0.6), 110, 0.6, (0.75, 0.9, 1.0))
area("top", (0.0, -0.2, 1.4), 45, 0.8)

# ---------------------------------------------------------------- camera
cam_data = bpy.data.cameras.new("cam")
cam_data.lens = 70
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
direction = Vector((0.62, -1.0, 0.58)).normalized()
cam.location = TARGET + direction * 1.28
cam.rotation_euler = (TARGET - cam.location).to_track_quat("-Z", "Y").to_euler()
scene.camera = cam

# ---------------------------------------------------------------- render
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.cycles.max_bounces = 12
scene.cycles.transmission_bounces = 12
scene.render.resolution_x = 1200
scene.render.resolution_y = 1600
scene.render.resolution_percentage = 100
scene.render.film_transparent = False
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGB"
try:
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Punchy"
except Exception:
    pass
scene.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print("RENDERED", OUT)
