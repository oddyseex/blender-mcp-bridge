"""Concurrency test: multiple AI clients connected at once, no cross-talk.

Spawns N client threads that each hold a connection open and interleave
commands (ping + execute_code). The main thread drives the (fake) Blender
main-thread pump. Verifies:
  1. all N connections are live simultaneously (connection counter),
  2. every response carries the id of its own request with the right result,
  3. the counter drains back to 0 afterwards.

Run:  python3 tests/test_addon_concurrency.py
"""

import json
import socket
import struct
import sys
import threading
import time
import uuid

sys.path.insert(0, "/home/hatch/workspace/blender-mcp/tests")

from harness import load_addon  # noqa: E402

N_CLIENTS = 4
CMDS_PER_CLIENT = 10


def send_frame(sock, payload):
    data = json.dumps(payload).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_frame(sock):
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise AssertionError("connection closed waiting for response")
        header += chunk
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise AssertionError("connection closed mid-frame")
        body += chunk
    return json.loads(body.decode())


def client_worker(idx, barrier, results, errors):
    """Hold one connection open; interleave ping and execute_code commands."""
    try:
        sock = socket.create_connection(("127.0.0.1", 9876), timeout=20)
    except OSError as exc:
        errors.append(f"client {idx}: connect failed: {exc}")
        barrier.wait(timeout=10)  # don't deadlock the others
        return
    try:
        barrier.wait(timeout=10)  # line up: all clients connected at once
        for i in range(CMDS_PER_CLIENT):
            if i % 2 == 0:
                req = {"id": uuid.uuid4().hex, "command": "ping", "params": {}}
                expected = ("pong", True)
            else:
                req = {"id": uuid.uuid4().hex, "command": "execute_code",
                       "params": {"code": f"result = {idx} * 1000 + {i}"}}
                expected = ("result", idx * 1000 + i)
            send_frame(sock, req)
            resp = recv_frame(sock)
            results.append((idx, i, req["id"], resp, expected))
    except Exception as exc:  # noqa: BLE001 - surfaced via errors list
        errors.append(f"client {idx}: {type(exc).__name__}: {exc}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def run_tests():
    mod, _fake_bpy = load_addon()
    mod.register()
    mod.start_server()

    barrier = threading.Barrier(N_CLIENTS + 1)
    results, errors = [], []
    threads = [
        threading.Thread(target=client_worker, args=(i, barrier, results, errors),
                         daemon=True)
        for i in range(N_CLIENTS)
    ]
    for t in threads:
        t.start()
    barrier.wait(timeout=15)

    # 1. all clients connected simultaneously?
    peak = 0
    deadline = time.time() + 5
    while time.time() < deadline:
        peak = max(peak, mod.get_connection_count())
        if peak >= N_CLIENTS:
            break
        time.sleep(0.01)
    assert peak >= N_CLIENTS, f"expected {N_CLIENTS} concurrent, saw {peak}"
    print(f"PASS: {N_CLIENTS} simultaneous client connections (counter peaked at {peak})")

    # 2. pump the main-thread queue until every client finishes
    deadline = time.time() + 30
    while time.time() < deadline:
        mod._pump_command_queue()
        if all(not t.is_alive() for t in threads):
            break
        time.sleep(0.01)
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"client errors: {errors}"
    assert all(not t.is_alive() for t in threads), "a client thread hung"
    assert len(results) == N_CLIENTS * CMDS_PER_CLIENT, (
        f"got {len(results)} responses, expected {N_CLIENTS * CMDS_PER_CLIENT}")

    for idx, i, req_id, resp, (kind, expected) in results:
        assert resp["id"] == req_id, (
            f"CROSS-TALK: client {idx} cmd {i} got response for {resp['id']}")
        assert resp["ok"], f"client {idx} cmd {i} failed: {resp.get('error')}"
        if kind == "pong":
            assert resp["result"]["pong"] is True, resp["result"]
        else:
            assert resp["result"] == expected, (
                f"client {idx}: got {resp['result']}, expected {expected}")
    print(f"PASS: {len(results)} interleaved commands, every response matched "
          f"its own request (no cross-talk)")

    # 3. counter drains once clients disconnect
    deadline = time.time() + 5
    while mod.get_connection_count() != 0 and time.time() < deadline:
        time.sleep(0.05)
    assert mod.get_connection_count() == 0, mod.get_connection_count()
    print("PASS: connection counter drains to 0 after disconnect")

    mod.stop_server()
    mod.unregister()
    print("\nAll concurrency tests passed.")


if __name__ == "__main__":
    run_tests()
