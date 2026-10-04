# Blender MCP Bridge

Drive Blender from any AI assistant over the **Model Context Protocol**.
An MCP server (this repo) exposes Blender as tools; a Blender add-on executes
them live in your scene.

```
MCP client (Claude Desktop / Cursor / Muse / ...)
   --stdio-->  server/blender_mcp_server.py
   --TCP 127.0.0.1:9876-->  Blender add-on  -->  bpy  -->  your scene
```

## What you can do with it

- `"Create a red metallic sphere at (1, 0, 0)"` → `create_primitive` + `create_material` + `assign_material`
- `"What's in my scene?"` → `get_scene_info`
- `"Show me what it looks like"` → `get_viewport_screenshot` (the AI *sees* the viewport)
- `"Add a subdivision modifier and animate it rotating"` → `execute_blender_code` (raw `bpy`)

## Setup

### 1. Install the Blender add-on

1. Grab `dist/blender_mcp_bridge.zip` (or zip `addon/blender_mcp_bridge.py` yourself).
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk…** → select the zip.
3. Enable **Interface: Blender MCP Bridge**.
4. Open the 3D Viewport sidebar (`N`) → **MCP Bridge** tab → click **Start Server**.

The server listens on `127.0.0.1:9876`. Blender must stay open while you use it.

### 2. Install the MCP server

```bash
cd blender-mcp
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server/blender_mcp_server.py   # sanity check: should start without errors
```

### 3. Point your AI client at it

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "blender": {
      "command": "/absolute/path/to/blender-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/blender-mcp/server/blender_mcp_server.py"]
    }
  }
}
```

**Cursor**: Settings → MCP → Add Server → same command/args.

**Muse CLI**: `Muse mcp add blender -- /absolute/path/to/.venv/bin/python /absolute/path/to/server/blender_mcp_server.py`

Then ask your assistant to `ping_blender` — if it answers with your Blender version, you're live.

## Tools

| Tool | What it does |
|---|---|
| `ping_blender` | Health check; returns Blender version |
| `get_scene_info` | Objects, materials, collections, render settings, frame range |
| `get_object_info` | Transforms, materials, bounds, mesh stats for one object |
| `get_viewport_screenshot` | Viewport capture as an image (visual feedback loop) |
| `execute_blender_code` | Run arbitrary `bpy` Python; assign output to `result` |
| `create_primitive` | cube / spheres / cylinder / cone / plane / torus / monkey / … |
| `transform_object` | Move / rotate / scale by name |
| `delete_object` | Remove an object by name |
| `create_material` | Principled BSDF material from hex color + metallic/roughness |
| `assign_material` | Put a material on an object |

## Protocol (v1)

TCP on `127.0.0.1:9876`. Every message = 4-byte big-endian length + UTF-8 JSON.

- Request: `{"id": str, "command": str, "params": dict}`
- Response: `{"id": str, "ok": bool, "result": any, "error": str | null}`

Commands are queued and executed on Blender's main thread via `bpy.app.timers`
(bpy is not thread-safe), then the response is written back on the same socket.

## Multiple AI clients

Yes — connect as many as you like. Each AI client (Claude Desktop, Cursor,
Muse CLI, …) spawns its **own** copy of the MCP server, and the Blender
add-on accepts simultaneous connections from all of them. Commands are
serialized through Blender's main thread, so concurrent clients interleave
safely instead of corrupting the scene. The sidebar panel shows how many
clients are currently connected.

One tip: two AIs editing the same scene will step on each other's toes
creatively (both creating "Cube", etc.). It works, but give them different
tasks — or different .blend files — for the smoothest experience.

## Troubleshooting

- **"cannot reach Blender on 127.0.0.1:9876"** → Blender isn't open, the add-on
  isn't enabled, or you didn't click **Start Server**.
- **Port already in use** → another Blender (or a zombie process) holds 9876.
  Close it or restart Blender.
- **Screenshot fails** → needs a visible 3D viewport; won't work in pure
  background (`-b`) mode.

## Security

The add-on executes arbitrary Python sent over localhost. That is its job —
but only run it on machines you trust, and never forward port 9876 to a network.

## Project layout

```
addon/blender_mcp_bridge.py   # the Blender add-on (socket server + UI panel)
server/blender_mcp_server.py   # the MCP server (FastMCP tools over stdio)
server/bridge_client.py        # shared TCP client for the wire protocol
tests/                         # protocol + threading tests (no Blender needed)
dist/blender_mcp_bridge.zip    # installable add-on package
```
