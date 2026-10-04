"""Protocol tests for bridge_client against a mock add-on socket server.

These verify the wire framing (4-byte length prefix + JSON) and the client's
error handling without needing Blender.
"""

import json
import socket
import struct
import sys
import threading

sys.path.insert(0, "/home/hatch/workspace/blender-mcp/server")

from bridge_client import BridgeError, send_command  # noqa: E402

MOCK_PORT = 19876


def _send_frame(conn, payload):
    data = json.dumps(payload).encode()
    conn.sendall(struct.pack(">I", len(data)) + data)


def _recv_frame(conn):
    header = conn.recv(4)
    if not header:
        return None
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            raise AssertionError("mock server: connection dropped mid-frame")
        body += chunk
    return json.loads(body.decode())


def mock_addon_server(stop_event, behaviors):
    """Minimal stand-in for the Blender add-on side of the protocol."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MOCK_PORT))
    srv.listen(5)
    srv.settimeout(0.2)
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        with conn:
            while not stop_event.is_set():
                req = _recv_frame(conn)
                if req is None:
                    break
                cmd = req["command"]
                if cmd == "slow":
                    # never respond -> client timeout
                    import time

                    time.sleep(30)
                    break
                behavior = behaviors.get(cmd, ("ok", {"echo": req["params"]}))
                kind, payload = behavior
                if kind == "ok":
                    _send_frame(conn, {"id": req["id"], "ok": True,
                                       "result": payload, "error": None})
                elif kind == "err":
                    _send_frame(conn, {"id": req["id"], "ok": False,
                                       "result": None, "error": payload})
                elif kind == "garbage":
                    conn.sendall(b"\xff\xfe not json {{{")
                    break
    srv.close()


def run_tests():
    stop_event = threading.Event()
    behaviors = {
        "ping": ("ok", {"pong": True, "blender_version": "4.5.0 (mock)"}),
        "boom": ("err", "RuntimeError: kaboom"),
        "garbage": ("garbage", None),
    }
    t = threading.Thread(target=mock_addon_server, args=(stop_event, behaviors),
                         daemon=True)
    t.start()

    try:
        # 1. happy path
        r = send_command("ping", port=MOCK_PORT, timeout=5)
        assert r == {"pong": True, "blender_version": "4.5.0 (mock)"}, r
        print("PASS: ping round-trip")

        # 2. params echo
        r = send_command("anything", {"a": [1, 2]}, port=MOCK_PORT, timeout=5)
        assert r == {"echo": {"a": [1, 2]}}, r
        print("PASS: params echo")

        # 3. Blender-side error propagates as BridgeError
        try:
            send_command("boom", port=MOCK_PORT, timeout=5)
        except BridgeError as e:
            assert "kaboom" in str(e), e
            print("PASS: error propagation")
        else:
            raise AssertionError("expected BridgeError")

        # 4. garbage response -> BridgeError, not a crash
        try:
            send_command("garbage", port=MOCK_PORT, timeout=5)
        except BridgeError as e:
            print(f"PASS: garbage response handled ({e})")
        else:
            raise AssertionError("expected BridgeError for garbage")

        # 5. timeout on unresponsive server
        try:
            send_command("slow", port=MOCK_PORT, timeout=1)
        except BridgeError as e:
            assert "timed out" in str(e), e
            print("PASS: timeout handling")
        else:
            raise AssertionError("expected BridgeError on timeout")

        # 6. connection refused -> helpful BridgeError
        try:
            send_command("ping", port=19999, timeout=2)
        except BridgeError as e:
            assert "127.0.0.1:19999" in str(e), e
            print("PASS: connection-refused message")
        else:
            raise AssertionError("expected BridgeError on refused connection")

        print("\nAll bridge_client protocol tests passed.")
    finally:
        stop_event.set()
        t.join(timeout=5)


if __name__ == "__main__":
    run_tests()
