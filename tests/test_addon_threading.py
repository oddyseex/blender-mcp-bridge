"""End-to-end test of the add-on's threading + protocol machinery.

Blender isn't installed here, so we inject a minimal fake ``bpy`` module that
provides just enough for the add-on to import, register, and run its socket
server. The ``ping`` and unknown-command handlers don't touch real Blender
state, so the full path is exercised for real:

    bridge_client --TCP--> addon socket thread --queue--> fake main-thread pump

Run:  python3 tests/test_addon_threading.py
"""

import sys
import threading
import time

sys.path.insert(0, "/home/hatch/workspace/blender-mcp/tests")

from harness import load_addon  # noqa: E402


def pump_until(mod, done_event, timeout=10):
    """Drive the fake main-thread pump until the socket thread is answered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        mod._pump_command_queue()  # what bpy.app.timers would call
        if done_event.is_set():
            return True
        time.sleep(0.02)
    return False


def run_tests():
    mod, fake_bpy = load_addon()
    from bridge_client import BridgeError, send_command  # noqa: E402

    # register() should not explode with the fake bpy
    mod.register()
    assert len(fake_bpy.utils.classes) == 3, fake_bpy.utils.classes
    print("PASS: register() with fake bpy")

    # start the real socket server machinery
    mod.start_server()
    assert mod.is_server_running()
    assert fake_bpy.app.timers.is_registered(mod._pump_command_queue)
    mod.start_server()  # idempotent: second call must be a no-op
    print("PASS: start_server() (idempotent)")

    # --- ping over a real TCP connection ---
    holder = {}
    done = threading.Event()

    def client():
        try:
            holder["result"] = send_command("ping", timeout=10)
        except Exception as exc:  # noqa: BLE001 - capture for assertion
            holder["error"] = exc
        finally:
            done.set()

    threading.Thread(target=client, daemon=True).start()
    assert pump_until(mod, done), "main-thread pump never answered ping"
    assert "error" not in holder, holder.get("error")
    assert holder["result"]["pong"] is True, holder["result"]
    assert holder["result"]["blender_version"] == "4.5.0 (fake)"
    print(f"PASS: ping round-trip -> {holder['result']}")

    # --- unknown command -> structured error, server keeps working ---
    holder2, done2 = {}, threading.Event()

    def client2():
        try:
            send_command("nope", timeout=10)
        except BridgeError as exc:
            holder2["error"] = str(exc)
        finally:
            done2.set()

    threading.Thread(target=client2, daemon=True).start()
    assert pump_until(mod, done2), "pump never answered bad command"
    assert "unknown command" in holder2["error"], holder2
    print(f"PASS: unknown command error -> {holder2['error'][:60]}...")

    # --- still alive afterwards: second ping on a new connection ---
    holder3, done3 = {}, threading.Event()

    def client3():
        try:
            holder3["result"] = send_command("ping", timeout=10)
        except Exception as exc:  # noqa: BLE001
            holder3["error"] = exc
        finally:
            done3.set()

    threading.Thread(target=client3, daemon=True).start()
    assert pump_until(mod, done3), "server died after bad command"
    assert holder3["result"]["pong"] is True
    print("PASS: server survives bad commands")

    # --- stop ---
    mod.stop_server()
    assert not mod.is_server_running()
    assert not fake_bpy.app.timers.is_registered(mod._pump_command_queue)
    mod.stop_server()  # idempotent
    try:
        send_command("ping", timeout=3)
    except BridgeError as exc:
        assert "cannot reach Blender" in str(exc), exc
        print("PASS: stop_server() (port closed, idempotent)")
    else:
        raise AssertionError("port should be closed after stop_server()")

    mod.unregister()
    assert fake_bpy.utils.classes == []
    print("PASS: unregister()")

    print("\nAll add-on threading tests passed.")


if __name__ == "__main__":
    run_tests()
