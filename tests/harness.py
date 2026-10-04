"""Shared test harness: a minimal fake ``bpy`` plus the add-on loader.

Lets us exercise the add-on's real socket server, threading, queue, and
main-thread pump without Blender installed.
"""

import importlib.util
import sys
import types

ADDON_PATH = "/home/hatch/workspace/blender-mcp/addon/blender_mcp_bridge.py"
SERVER_DIR = "/home/hatch/workspace/blender-mcp/server"


def make_fake_bpy():
    """Just enough ``bpy`` for the add-on to import, register, and serve.

    Command handlers that need real Blender state (scene info, screenshots)
    are not exercised; ``ping`` and ``execute_code`` (pure-Python snippets)
    run for real.
    """
    bpy = types.ModuleType("bpy")

    class _Timers:
        def __init__(self):
            self.registered = []

        def register(self, fn, persistent=False):
            if fn in self.registered:
                raise ValueError("already registered")
            self.registered.append(fn)

        def unregister(self, fn):
            self.registered.remove(fn)  # raises ValueError if missing

        def is_registered(self, fn):
            return fn in self.registered

    class _App:
        version_string = "4.5.0 (fake)"
        version = (4, 5, 0)
        timers = _Timers()

    class _Types:
        class Operator:
            pass

        class Panel:
            pass

    class _Utils:
        def __init__(self):
            self.classes = []

        def register_class(self, cls):
            self.classes.append(cls)

        def unregister_class(self, cls):
            self.classes.remove(cls)

    bpy.app = _App()
    bpy.types = _Types()
    bpy.utils = _Utils()
    bpy.props = types.SimpleNamespace()
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace()
    return bpy


def load_addon():
    """Import the add-on fresh with the fake ``bpy``. Returns (module, bpy)."""
    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)
    fake_bpy = make_fake_bpy()
    sys.modules["bpy"] = fake_bpy
    sys.modules.pop("blender_mcp_bridge", None)
    spec = importlib.util.spec_from_file_location(
        "blender_mcp_bridge", ADDON_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["blender_mcp_bridge"] = mod
    spec.loader.exec_module(mod)
    return mod, fake_bpy
