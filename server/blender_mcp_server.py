"""Blender MCP server.

Exposes Blender to any MCP client (Claude Desktop, Cursor, Muse, ...) as a
set of tools. This process speaks MCP over stdio; it forwards every tool call
to the Blender MCP Bridge add-on over a local TCP socket (127.0.0.1:9876),
where the command is executed on Blender's main thread via bpy.

Prerequisites:
    1. Blender 3.0+ with the ``blender_mcp_bridge.py`` add-on installed,
       enabled, and its server started (View3D > Sidebar > MCP Bridge).
    2. ``pip install -r requirements.txt`` (the ``mcp`` Python SDK).

Run:
    python blender_mcp_server.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

try:  # FastMCP image helper (lets screenshots render inline in clients)
    from mcp.server.fastmcp.utilities.types import Image as FastMCPImage
except ImportError:  # pragma: no cover - older SDKs
    FastMCPImage = None

from bridge_client import BridgeError, HOST, PORT, ping as bridge_ping, send_command

mcp = FastMCP("blender-bridge")


# ---------------------------------------------------------------------------
# Connection / introspection
# ---------------------------------------------------------------------------

@mcp.tool()
def ping_blender() -> dict:
    """Check that Blender is reachable. Returns Blender version info.

    Call this first; every other tool needs the add-on server running.
    """
    try:
        return bridge_ping()
    except BridgeError as exc:
        return {"reachable": False, "error": str(exc)}


@mcp.tool()
def get_scene_info() -> dict:
    """Summarize the current Blender scene: objects, materials, collections,
    render settings, and frame range."""
    return send_command("get_scene_info")


@mcp.tool()
def get_object_info(name: str) -> dict:
    """Get detailed info about one object: transforms, materials, bounds,
    and mesh stats (verts/edges/faces)."""
    return send_command("get_object_info", {"name": name})


@mcp.tool()
def get_viewport_screenshot(max_width: int = 1280):
    """Capture the 3D viewport as a PNG image. Use it to *see* the scene and
    verify that modeling operations did what you intended."""
    result = send_command("get_viewport_screenshot", {"max_width": max_width})
    if FastMCPImage is not None:
        import base64

        return FastMCPImage(
            data=base64.b64decode(result["image_base64"]), format="png"
        )
    return result  # fallback: raw base64 dict


# ---------------------------------------------------------------------------
# Raw power tool
# ---------------------------------------------------------------------------

@mcp.tool()
def execute_blender_code(code: str) -> object:
    """Execute arbitrary Python code inside Blender (``bpy`` is available).

    Assign anything you want returned to a variable named ``result``.
    Prefer the dedicated tools for common operations; use this for everything
    else (modifiers, geometry nodes, animation, physics, ...).

    Example:
        import bpy
        bpy.ops.mesh.primitive_cube_add(location=(1, 0, 0))
        result = bpy.context.active_object.name
    """
    return send_command("execute_code", {"code": code})


# ---------------------------------------------------------------------------
# Modeling helpers (generate bpy code and run it via execute_code)
# ---------------------------------------------------------------------------

_PRIMITIVE_OPS = {
    "cube": "bpy.ops.mesh.primitive_cube_add",
    "uv_sphere": "bpy.ops.mesh.primitive_uv_sphere_add",
    "ico_sphere": "bpy.ops.mesh.primitive_ico_sphere_add",
    "cylinder": "bpy.ops.mesh.primitive_cylinder_add",
    "cone": "bpy.ops.mesh.primitive_cone_add",
    "plane": "bpy.ops.mesh.primitive_plane_add",
    "torus": "bpy.ops.mesh.primitive_torus_add",
    "circle": "bpy.ops.mesh.primitive_circle_add",
    "grid": "bpy.ops.mesh.primitive_grid_add",
    "monkey": "bpy.ops.mesh.primitive_monkey_add",
}


@mcp.tool()
def create_primitive(
    primitive_type: str = "cube",
    name: str | None = None,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
) -> str:
    """Create a mesh primitive. Types: cube, uv_sphere, ico_sphere, cylinder,
    cone, plane, torus, circle, grid, monkey. Returns the object's name."""
    op = _PRIMITIVE_OPS.get(primitive_type.lower())
    if op is None:
        raise ValueError(
            f"unknown primitive {primitive_type!r}; choose from {sorted(_PRIMITIVE_OPS)}"
        )
    location = location or [0.0, 0.0, 0.0]
    code = (
        "import bpy\n"
        f"{op}(location={tuple(location)})\n"
        "obj = bpy.context.active_object\n"
    )
    if name:
        code += f"obj.name = {name!r}\n"
    if rotation:
        code += f"obj.rotation_euler = {tuple(rotation)}\n"
    if scale:
        code += f"obj.scale = {tuple(scale)}\n"
    code += "result = obj.name\n"
    return send_command("execute_code", {"code": code})


@mcp.tool()
def transform_object(
    name: str,
    location: list[float] | None = None,
    rotation: list[float] | None = None,
    scale: list[float] | None = None,
) -> dict:
    """Move / rotate (Euler radians XYZ) / scale an object. Only the
    transforms you pass are changed. Returns the new transform."""
    code = (
        "import bpy\n"
        f"obj = bpy.data.objects[{name!r}]\n"
        "if obj is None:\n"
        f"    raise KeyError('object not found: {name}')\n"
    )
    if location is not None:
        code += f"obj.location = {tuple(location)}\n"
    if rotation is not None:
        code += f"obj.rotation_euler = {tuple(rotation)}\n"
    if scale is not None:
        code += f"obj.scale = {tuple(scale)}\n"
    code += (
        "result = {'name': obj.name, 'location': list(obj.location), "
        "'rotation_euler': list(obj.rotation_euler), 'scale': list(obj.scale)}\n"
    )
    return send_command("execute_code", {"code": code})


@mcp.tool()
def delete_object(name: str) -> str:
    """Delete an object from the scene by name. Returns the deleted name."""
    code = (
        "import bpy\n"
        f"obj = bpy.data.objects.get({name!r})\n"
        "if obj is None:\n"
        f"    raise KeyError('object not found: {name}')\n"
        "bpy.data.objects.remove(obj, do_unlink=True)\n"
        f"result = {name!r}\n"
    )
    return send_command("execute_code", {"code": code})


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def _parse_hex_color(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        raise ValueError(f"expected hex color like '#ff8800', got {value!r}")
    r = int(value[0:2], 16) / 255.0
    g = int(value[2:4], 16) / 255.0
    b = int(value[4:6], 16) / 255.0
    return (r, g, b)


@mcp.tool()
def create_material(
    name: str,
    base_color: str = "#808080",
    metallic: float = 0.0,
    roughness: float = 0.5,
) -> str:
    """Create a Principled BSDF material. ``base_color`` is a hex string like
    '#ff8800'. Returns the material name."""
    r, g, b = _parse_hex_color(base_color)
    code = (
        "import bpy\n"
        f"mat = bpy.data.materials.new(name={name!r})\n"
        "mat.use_nodes = True\n"
        "bsdf = mat.node_tree.nodes.get('Principled BSDF')\n"
        f"bsdf.inputs['Base Color'].default_value = {(r, g, b, 1.0)}\n"
        f"bsdf.inputs['Metallic'].default_value = {float(metallic)}\n"
        f"bsdf.inputs['Roughness'].default_value = {float(roughness)}\n"
        "result = mat.name\n"
    )
    return send_command("execute_code", {"code": code})


@mcp.tool()
def assign_material(object_name: str, material_name: str) -> str:
    """Assign a material to an object (replaces its first material slot)."""
    code = (
        "import bpy\n"
        f"obj = bpy.data.objects.get({object_name!r})\n"
        f"mat = bpy.data.materials.get({material_name!r})\n"
        "if obj is None:\n"
        f"    raise KeyError('object not found: {object_name}')\n"
        "if mat is None:\n"
        f"    raise KeyError('material not found: {material_name}')\n"
        "if obj.data.materials:\n"
        "    obj.data.materials[0] = mat\n"
        "else:\n"
        "    obj.data.materials.append(mat)\n"
        f"result = f'{material_name} -> {object_name}'\n"
    )
    return send_command("execute_code", {"code": code})


if __name__ == "__main__":
    mcp.run()
