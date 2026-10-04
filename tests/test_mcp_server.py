"""Verify the MCP server: tool registration + generated bpy code validity.

No Blender needed: we monkeypatch send_command to capture the generated
code instead of sending it, then prove each snippet is syntactically valid
Python.

Run with the project venv:
    .venv/bin/python tests/test_mcp_server.py
"""

import asyncio
import sys

sys.path.insert(0, "/home/hatch/workspace/blender-mcp/server")

import blender_mcp_server as srv  # noqa: E402


async def list_tool_names():
    try:
        tools = await srv.mcp.list_tools()
        return sorted(t.name for t in tools)
    except AttributeError:
        return sorted(srv.mcp._tool_manager._tools.keys())


EXPECTED_TOOLS = sorted([
    "ping_blender",
    "get_scene_info",
    "get_object_info",
    "get_viewport_screenshot",
    "execute_blender_code",
    "create_primitive",
    "transform_object",
    "delete_object",
    "create_material",
    "assign_material",
])


def capture_calls():
    """Replace send_command with a recorder; return (calls, restore)."""
    calls = []
    original = srv.send_command

    def fake(command, params=None, **kwargs):
        calls.append((command, params))
        return f"<captured:{command}>"

    srv.send_command = fake
    return calls, original


def call_tool(fn, *args, **kwargs):
    """Call a FastMCP-registered tool function regardless of wrapper type."""
    target = getattr(fn, "fn", fn)  # FunctionTool wraps .fn in some versions
    if not callable(target):
        target = fn
    return target(*args, **kwargs)


def check_code_compiles(label, code):
    compile(code, f"<{label}>", "exec")
    assert "result" in code, f"{label}: generated code never sets `result`"
    print(f"PASS: {label} code compiles ({len(code)} chars)")


def run_tests():
    names = asyncio.run(list_tool_names())
    assert names == EXPECTED_TOOLS, f"tool mismatch:\n got: {names}\n want: {EXPECTED_TOOLS}"
    print(f"PASS: {len(names)} tools registered: {', '.join(names)}")

    calls, original = capture_calls()
    try:
        # create_primitive: every primitive type must generate valid code
        for ptype in ["cube", "uv_sphere", "ico_sphere", "cylinder", "cone",
                      "plane", "torus", "circle", "grid", "monkey"]:
            calls.clear()
            call_tool(srv.create_primitive, primitive_type=ptype, name="TestObj",
                      location=[1, 2, 3], rotation=[0, 0, 1.57], scale=[2, 2, 2])
            cmd, params = calls[0]
            assert cmd == "execute_code", cmd
            check_code_compiles(f"create_primitive[{ptype}]", params["code"])

        # unknown primitive -> ValueError before any socket use
        try:
            call_tool(srv.create_primitive, primitive_type="teapot")
        except ValueError as e:
            assert "unknown primitive" in str(e)
            print("PASS: create_primitive rejects unknown type")
        else:
            raise AssertionError("expected ValueError for teapot")

        calls.clear()
        call_tool(srv.transform_object, name="Cube", location=[0, 0, 5],
                  rotation=[0, 0, 0], scale=[1, 1, 2])
        check_code_compiles("transform_object", calls[0][1]["code"])

        calls.clear()
        call_tool(srv.transform_object, name="Cube")  # no transforms: still valid
        check_code_compiles("transform_object[no-op]", calls[0][1]["code"])

        calls.clear()
        call_tool(srv.delete_object, name="Cube")
        check_code_compiles("delete_object", calls[0][1]["code"])

        calls.clear()
        call_tool(srv.create_material, name="RedMetal", base_color="#ff2200",
                  metallic=1.0, roughness=0.25)
        check_code_compiles("create_material", calls[0][1]["code"])

        calls.clear()
        call_tool(srv.assign_material, object_name="Cube", material_name="RedMetal")
        check_code_compiles("assign_material", calls[0][1]["code"])

        # color parsing edge cases
        assert srv._parse_hex_color("#fff") == (1.0, 1.0, 1.0)
        assert srv._parse_hex_color("ff8800")[0] == 1.0
        try:
            srv._parse_hex_color("notacolor")
        except ValueError:
            print("PASS: _parse_hex_color rejects bad input")
        else:
            raise AssertionError("expected ValueError for bad color")
    finally:
        srv.send_command = original

    print("\nAll MCP server tests passed.")
    print("NOTE: ping_blender/get_scene_info/etc. need a live Blender; not tested here.")


if __name__ == "__main__":
    run_tests()
