"""Batch command tests: execute_batch over the real socket + fake main-thread pump.

Covers: ordered execution in one round trip, per-item failure capture,
nested-batch rejection, and empty-batch rejection.

Run:  python3 tests/test_addon_batch.py
"""

import sys
import threading
import time

sys.path.insert(0, "/home/hatch/workspace/blender-mcp/tests")

from harness import load_addon  # noqa: E402


def pump_until(mod, done, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        mod._pump_command_queue()  # what bpy.app.timers would call
        if done.is_set():
            return True
        time.sleep(0.02)
    return False


def run_batch(mod, payload):
    from bridge_client import send_command

    holder, done = {}, threading.Event()

    def client():
        try:
            holder["r"] = send_command("execute_batch", payload, timeout=10)
        except Exception as exc:  # noqa: BLE001 - asserted by callers
            holder["e"] = exc
        finally:
            done.set()

    threading.Thread(target=client, daemon=True).start()
    assert pump_until(mod, done), "main-thread pump never answered batch"
    return holder


def run_tests():
    mod, _fake_bpy = load_addon()
    from bridge_client import BridgeError  # noqa: E402  (needs load_addon's sys.path)

    mod.register()
    mod.start_server()

    # 1. mixed batch, one round trip, results in order
    h = run_batch(mod, {"commands": [
        {"command": "ping", "params": {}},
        {"command": "execute_code", "params": {"code": "result = 6 * 7"}},
        {"command": "ping", "params": {}},
    ]})
    assert "e" not in h, h.get("e")
    r = h["r"]
    assert r["count"] == 3, r
    assert [i["command"] for i in r["results"]] == ["ping", "execute_code", "ping"]
    assert all(i["ok"] for i in r["results"]), r
    assert r["results"][1]["result"] == 42, r
    print("PASS: mixed batch executes in order, single round trip")

    # 2. one bad item does not abort the batch
    h = run_batch(mod, {"commands": [
        {"command": "execute_code", "params": {"code": "result = 'first'"}},
        {"command": "nope", "params": {}},
        {"command": "execute_code", "params": {"code": "result = 'third'"}},
    ]})
    r = h["r"]
    assert r["results"][0]["ok"] and r["results"][0]["result"] == "first", r
    assert not r["results"][1]["ok"], r
    assert "unknown command" in r["results"][1]["error"], r
    assert r["results"][2]["ok"] and r["results"][2]["result"] == "third", r
    print("PASS: item failure captured per-item, batch continues")

    # 3. nested batch rejected
    h = run_batch(mod, {"commands": [
        {"command": "execute_batch", "params": {"commands": []}},
    ]})
    r = h["r"]
    assert not r["results"][0]["ok"], r
    assert "nested" in r["results"][0]["error"], r
    print("PASS: nested batch rejected")

    # 4. empty batch -> top-level BridgeError
    h = run_batch(mod, {"commands": []})
    assert "e" in h and isinstance(h["e"], BridgeError), h
    assert "non-empty" in str(h["e"]), h["e"]
    print("PASS: empty batch rejected with BridgeError")

    mod.stop_server()
    mod.unregister()
    print("\nAll batch tests passed.")


if __name__ == "__main__":
    run_tests()
