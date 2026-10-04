bl_info = {
    "name": "Blender MCP Bridge",
    "author": "Peedee",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > MCP Bridge",
    "description": (
        "Exposes Blender to AI assistants over the Model Context Protocol. "
        "Runs a local TCP server (127.0.0.1:9876) that executes bpy commands "
        "on Blender's main thread and returns JSON results."
    ),
    "category": "Interface",
}

"""Blender MCP Bridge add-on.

Architecture
------------
An MCP server (separate Python process) cannot call into Blender directly,
so this add-on runs a small TCP server *inside* Blender:

    MCP client --stdio--> mcp server --TCP 127.0.0.1:9876--> this add-on --> bpy

Wire protocol (v1): each message is a 4-byte big-endian length header
followed by UTF-8 JSON.
    request:  {"id": str, "command": str, "params": dict}
    response: {"id": str, "ok": bool, "result": any, "error": str | null}

Threading model
---------------
Blender's Python API may only be touched from the main thread. The socket
server therefore runs on background threads; each incoming request is placed
on a queue, and a repeating ``bpy.app.timers`` callback (which always runs on
the main thread) executes it and signals the waiting socket thread.

Security note
-------------
The server binds to 127.0.0.1 only and executes arbitrary Python sent to it.
That is the entire point of the tool, but only run it on a machine you trust
and never expose the port to a network.
"""

import base64
import json
import os
import queue
import socket
import struct
import tempfile
import threading
import traceback

import bpy

HOST = "127.0.0.1"
PORT = 9876
HEADER_SIZE = 4
PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

_request_queue: "queue.Queue[tuple[dict, dict, threading.Event]]" = queue.Queue()
_server_thread: threading.Thread | None = None
_listen_socket: socket.socket | None = None
_stop_event = threading.Event()
_server_running = False

# Active TCP connections (i.e. connected AI clients / MCP server processes).
# Multiple clients are supported: each connection is served by its own thread
# and commands are serialized through the single main-thread queue.
_active_connections = 0
_conn_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes, or return None if the peer closed the connection."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except (ConnectionResetError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _send_frame(conn: socket.socket, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def _json_safe(value):
    """Coerce an arbitrary Python value into something json.dumps can handle."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return repr(value)


# ---------------------------------------------------------------------------
# Command handlers (all run on Blender's main thread)
# ---------------------------------------------------------------------------

def _handle_ping(params: dict) -> dict:
    return {
        "pong": True,
        "protocol": PROTOCOL_VERSION,
        "blender_version": bpy.app.version_string,
        "app_version": ".".join(str(v) for v in bpy.app.version),
    }


def _handle_execute_code(params: dict):
    code = params.get("code", "")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("params.code must be a non-empty string")
    # Convention: the snippet may assign to a variable named `result`;
    # whatever it holds is returned to the caller.
    namespace = {"bpy": bpy, "result": None}
    exec(compile(code, "<mcp-bridge>", "exec"), namespace)
    return _json_safe(namespace.get("result"))


def _object_summary(obj) -> dict:
    mats = []
    try:
        for slot in obj.material_slots:
            if slot.material is not None:
                mats.append(slot.material.name)
    except Exception:
        pass
    return {
        "name": obj.name,
        "type": obj.type,
        "location": [round(float(v), 4) for v in obj.location],
        "rotation_euler": [round(float(v), 4) for v in obj.rotation_euler],
        "scale": [round(float(v), 4) for v in obj.scale],
        "dimensions": [round(float(v), 4) for v in obj.dimensions],
        "visible": bool(obj.visible_get()),
        "selected": bool(obj.select_get()),
        "materials": mats,
    }


def _handle_get_scene_info(params: dict) -> dict:
    scene = bpy.context.scene
    render = scene.render
    return {
        "scene": scene.name,
        "filepath": bpy.data.filepath or None,
        "object_count": len(bpy.data.objects),
        "objects": [_object_summary(o) for o in bpy.data.objects],
        "materials": sorted(m.name for m in bpy.data.materials),
        "collections": sorted(c.name for c in bpy.data.collections),
        "render": {
            "engine": render.engine,
            "resolution": [render.resolution_x, render.resolution_y],
            "resolution_percentage": render.resolution_percentage,
            "filepath": render.filepath,
        },
        "frame": {
            "current": scene.frame_current,
            "start": scene.frame_start,
            "end": scene.frame_end,
        },
    }


def _handle_get_object_info(params: dict) -> dict:
    name = params.get("name")
    if not name:
        raise ValueError("params.name is required")
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise KeyError(f"object not found: {name!r}")
    info = _object_summary(obj)
    info["parent"] = obj.parent.name if obj.parent else None
    info["children"] = [c.name for c in obj.children]
    info["bound_box"] = [
        [round(float(v), 4) for v in corner] for corner in obj.bound_box
    ]
    if obj.type == "MESH" and obj.data is not None:
        mesh = obj.data
        info["mesh"] = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "polygons": len(mesh.polygons),
        }
    return info


def _find_view3d_area():
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                return window, area
    return None, None


def _handle_get_viewport_screenshot(params: dict) -> dict:
    """Capture the 3D viewport with an OpenGL render, returned as base64 PNG."""
    max_width = int(params.get("max_width", 1280))
    window, area = _find_view3d_area()
    if area is None:
        raise RuntimeError("no 3D viewport found in any window")

    scene = bpy.context.scene
    render = scene.render
    old_filepath = render.filepath
    old_pct = render.resolution_percentage

    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        # Keep the capture quick: scale down if the render is huge.
        if render.resolution_x > max_width:
            render.resolution_percentage = max(
                1, int(max_width / render.resolution_x * render.resolution_percentage)
            )
        render.filepath = tmp_path
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        override = {"window": window, "area": area}
        if region is not None:
            override["region"] = region
        with bpy.context.temp_override(**override):
            bpy.ops.render.opengl(write_still=True)
        with open(tmp_path, "rb") as f:
            data = f.read()
    finally:
        render.filepath = old_filepath
        render.resolution_percentage = old_pct
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return {
        "mime": "image/png",
        "width": render.resolution_x,
        "image_base64": base64.b64encode(data).decode("ascii"),
    }


_COMMANDS = {
    "ping": _handle_ping,
    "execute_code": _handle_execute_code,
    "get_scene_info": _handle_get_scene_info,
    "get_object_info": _handle_get_object_info,
    "get_viewport_screenshot": _handle_get_viewport_screenshot,
}


# ---------------------------------------------------------------------------
# Main-thread pump: drains the queue via bpy.app.timers
# ---------------------------------------------------------------------------

def _pump_command_queue() -> float:
    """Execute queued requests on Blender's main thread. Reschedules itself."""
    while True:
        try:
            request, holder, done = _request_queue.get_nowait()
        except queue.Empty:
            break
        cmd = request.get("command")
        params = request.get("params") or {}
        try:
            handler = _COMMANDS.get(cmd)
            if handler is None:
                raise ValueError(
                    f"unknown command {cmd!r}; known: {sorted(_COMMANDS)}"
                )
            result = handler(params)
            holder["response"] = {
                "id": request.get("id"),
                "ok": True,
                "result": _json_safe(result),
                "error": None,
            }
        except Exception as exc:  # never let one bad command kill the pump
            holder["response"] = {
                "id": request.get("id"),
                "ok": False,
                "result": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            done.set()
    return 0.1  # run again in 0.1 s


# ---------------------------------------------------------------------------
# Socket server (background threads; never touches bpy directly)
# ---------------------------------------------------------------------------

def _handle_connection(conn: socket.socket) -> None:
    global _active_connections
    with _conn_lock:
        _active_connections += 1
    try:
        while not _stop_event.is_set():
            header = _recv_exact(conn, HEADER_SIZE)
            if header is None:
                break  # client closed the connection
            (length,) = struct.unpack(">I", header)
            if length > 50 * 1024 * 1024:
                _send_frame(conn, {
                    "id": None, "ok": False, "result": None,
                    "error": f"request too large ({length} bytes)",
                })
                break
            body = _recv_exact(conn, length)
            if body is None:
                break
            try:
                request = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                _send_frame(conn, {
                    "id": None, "ok": False, "result": None,
                    "error": "request is not valid UTF-8 JSON",
                })
                continue

            holder: dict = {}
            done = threading.Event()
            _request_queue.put((request, holder, done))
            if not done.wait(timeout=120):
                response = {
                    "id": request.get("id"), "ok": False, "result": None,
                    "error": "timed out waiting for Blender's main thread",
                }
            else:
                response = holder["response"]
            _send_frame(conn, response)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        with _conn_lock:
            _active_connections -= 1
        try:
            conn.close()
        except OSError:
            pass


def get_connection_count() -> int:
    """Number of currently connected clients (MCP server processes)."""
    with _conn_lock:
        return _active_connections


def _serve_forever() -> None:
    global _listen_socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(8)
    srv.settimeout(0.5)
    _listen_socket = srv
    try:
        while not _stop_event.is_set():
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=_handle_connection, args=(conn,), daemon=True,
                name="mcp-bridge-conn",
            )
            thread.start()
    finally:
        try:
            srv.close()
        except OSError:
            pass
        _listen_socket = None


def start_server() -> None:
    """Start the TCP server and the main-thread pump. Idempotent."""
    global _server_thread, _server_running
    if _server_running:
        return
    _stop_event.clear()
    try:
        bpy.app.timers.register(_pump_command_queue, persistent=True)
    except ValueError:
        pass  # already registered (e.g. after a script reload)
    _server_thread = threading.Thread(
        target=_serve_forever, daemon=True, name="mcp-bridge-server"
    )
    _server_thread.start()
    _server_running = True
    print(f"[MCP Bridge] listening on {HOST}:{PORT}")


def stop_server() -> None:
    """Stop the TCP server and the main-thread pump. Idempotent."""
    global _server_thread, _server_running
    if not _server_running and _server_thread is None:
        return
    _stop_event.set()
    if _listen_socket is not None:
        try:
            _listen_socket.close()
        except OSError:
            pass
    if _server_thread is not None:
        _server_thread.join(timeout=5)
        _server_thread = None
    try:
        bpy.app.timers.unregister(_pump_command_queue)
    except ValueError:
        pass
    # Drain anything left so a restart starts clean.
    while True:
        try:
            _request_queue.get_nowait()
        except queue.Empty:
            break
    _server_running = False
    print("[MCP Bridge] stopped")


def is_server_running() -> bool:
    return _server_running


# ---------------------------------------------------------------------------
# Blender UI: sidebar panel with Start / Stop
# ---------------------------------------------------------------------------

class MCPBRIDGE_OT_start_server(bpy.types.Operator):
    bl_idname = "mcp_bridge.start_server"
    bl_label = "Start MCP Bridge Server"
    bl_description = f"Start the MCP Bridge TCP server on {HOST}:{PORT}"

    def execute(self, context):
        try:
            start_server()
        except OSError as exc:
            self.report({"ERROR"}, f"Could not start server: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"MCP Bridge listening on {HOST}:{PORT}")
        return {"FINISHED"}


class MCPBRIDGE_OT_stop_server(bpy.types.Operator):
    bl_idname = "mcp_bridge.stop_server"
    bl_label = "Stop MCP Bridge Server"
    bl_description = "Stop the MCP Bridge TCP server"

    def execute(self, context):
        stop_server()
        self.report({"INFO"}, "MCP Bridge stopped")
        return {"FINISHED"}


class MCPBRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "MCP Bridge"
    bl_idname = "MCPBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP Bridge"

    def draw(self, context):
        layout = self.layout
        if is_server_running():
            count = get_connection_count()
            noun = "client" if count == 1 else "clients"
            layout.label(
                text=f"Running on {HOST}:{PORT} ({count} {noun})",
                icon="CHECKMARK",
            )
            layout.operator("mcp_bridge.stop_server", icon="X")
        else:
            layout.label(text="Server stopped", icon="X")
            layout.operator("mcp_bridge.start_server", icon="PLAY")
        layout.separator()
        layout.label(text="Connect your AI assistant via")
        layout.label(text="the MCP server to drive Blender.")


_CLASSES = (
    MCPBRIDGE_OT_start_server,
    MCPBRIDGE_OT_stop_server,
    MCPBRIDGE_PT_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    stop_server()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
