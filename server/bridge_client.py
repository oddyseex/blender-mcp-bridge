"""TCP client for the Blender MCP Bridge protocol.

Wire protocol (v1):
    - TCP connection to 127.0.0.1:9876 (Blender add-on side)
    - Every message is a 4-byte big-endian length header followed by UTF-8 JSON.
    - Request:  {"id": str, "command": str, "params": dict}
    - Response: {"id": str, "ok": bool, "result": any, "error": str | null}

A connection may carry multiple request/response pairs in sequence.
"""

from __future__ import annotations

import json
import socket
import struct
import uuid

HOST = "127.0.0.1"
PORT = 9876
HEADER_SIZE = 4
DEFAULT_TIMEOUT = 60.0


class BridgeError(Exception):
    """Raised when the Blender side reports an error or the link fails."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise BridgeError("connection closed by Blender while reading response")
        buf.extend(chunk)
    return bytes(buf)


def send_command(
    command: str,
    params: dict | None = None,
    host: str = HOST,
    port: int = PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> object:
    """Send one command to the Blender add-on and return its ``result``.

    Raises:
        BridgeError: if Blender reports an error, the connection fails,
            or the response times out.
    """
    request = {
        "id": uuid.uuid4().hex,
        "command": command,
        "params": params or {},
    }
    payload = json.dumps(request).encode("utf-8")
    frame = struct.pack(">I", len(payload)) + payload

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise BridgeError(
            f"cannot reach Blender on {host}:{port} "
            f"(is Blender running with the MCP Bridge add-on enabled and the server started?): {exc}"
        ) from exc

    try:
        sock.settimeout(timeout)
        sock.sendall(frame)
        header = _recv_exact(sock, HEADER_SIZE)
        (length,) = struct.unpack(">I", header)
        if length > 50 * 1024 * 1024:  # 50 MB sanity cap (screenshots are ~1-5 MB)
            raise BridgeError(f"response too large ({length} bytes), refusing to read")
        body = _recv_exact(sock, length)
    except socket.timeout as exc:
        raise BridgeError(f"timed out waiting for Blender response ({timeout}s)") from exc
    except OSError as exc:
        raise BridgeError(f"socket error talking to Blender: {exc}") from exc
    finally:
        sock.close()

    try:
        response = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BridgeError(f"invalid JSON response from Blender: {exc}") from exc

    if not response.get("ok", False):
        raise BridgeError(response.get("error") or "Blender reported an unknown error")
    return response.get("result")


def ping(host: str = HOST, port: int = PORT, timeout: float = 5.0) -> dict:
    """Quick health check. Returns Blender version info on success."""
    result = send_command("ping", host=host, port=port, timeout=timeout)
    if not isinstance(result, dict):
        raise BridgeError(f"unexpected ping response: {result!r}")
    return result
