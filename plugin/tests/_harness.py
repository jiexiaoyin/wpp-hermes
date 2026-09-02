"""Test harness for hermes_plugins.wechatpadpro_platform.* — simulates framework loading.

Uses importlib to load the plugin exactly like the Hermes framework does:
submodule_search_locations + namespace package registration. This avoids
the ``tools`` package collision that happens with naive ``sys.path.insert``.

IMPORTANT: The framework pre-imports ``tools`` (and other hermes internals)
before loading the plugin. Without that pre-warming, ``from gateway.platforms.base``
in adapter.py fails with ``ModuleNotFoundError: tools.registry`` because the
absolute ``from tools...`` imports inside the framework's transitive import
graph can't find a package called ``tools`` (only the empty namespace we set
up). This harness pre-warms the framework modules first.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent  # /root/.hermes/plugins/wechatpadro
NAMESPACE = "hermes_plugins.wechatpadpro_platform"


def _prewarm_framework():
    """Import hermes framework modules so 'tools' package resolves correctly.

    The framework does this during gateway startup, before loading plugins.
    Tests need to do the same or ``from gateway.platforms.base import`` (which
    adapter.py does) will fail when the framework later tries to do
    ``from tools.registry import tool_error``.
    """
    # Pre-import the modules that gatekeeper downstream imports need
    # Use try/except so tests don't crash if individual modules shift names
    for module_name in [
        "agent",
        "agent.turn_context",
        "agent.memory_manager",
        "tools",
        "tools.registry",
        "gateway",
        "gateway.platforms.base",
        "gateway.config",
    ]:
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            # Some modules need a deeper call stack; just try
            pass


def _ensure_tools_package_resolves():
    """Defensively re-bind ``tools`` to the framework's package if needed.

    Test runners may set cwd to the plugin directory or sys.path.insert(0, plugin_dir),
    both of which make Python resolve ``tools`` to this plugin's tools.py
    (a single file) instead of the framework's ``/usr/local/lib/hermes-agent/tools/``
    package. Once the framework's tools package is loaded, downstream ``from
    tools.registry import ...`` succeeds and adapter.py loads cleanly.
    """
    import importlib
    import os
    # If "tools" already exists and isn't a package, blow it away and reimport.
    if "tools" in sys.modules:
        tools_mod = sys.modules["tools"]
        if not hasattr(tools_mod, "__path__"):
            # Single-file module — replace with the framework's real package
            del sys.modules["tools"]
            # Clear any cached submodules (registry, etc.)
            for sub in list(sys.modules.keys()):
                if sub == "tools" or sub.startswith("tools."):
                    del sys.modules[sub]
    # Now do the real import (must resolve to /usr/local/lib/hermes-agent/tools/)
    try:
        importlib.import_module("tools")
        importlib.import_module("tools.registry")
    except Exception:  # noqa: BLE001
        pass


def load_plugin_module(name: str):
    """Load <name>.py from the wechatpadro plugin dir under the hermes_plugins namespace.

    Idempotent: reuses cached sys.modules entries when present.
    """
    full = f"{NAMESPACE}.{name}"
    if full in sys.modules:
        return sys.modules[full]

    # 0. Pre-warm framework so 'tools' package resolves
    _ensure_tools_package_resolves()
    _prewarm_framework()

    # 1. Register namespace package (idempotent)
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    if NAMESPACE not in sys.modules:
        wpp_ns = types.ModuleType(NAMESPACE)
        wpp_ns.__path__ = [str(PLUGIN_DIR)]
        sys.modules[NAMESPACE] = wpp_ns

    # 2. spec_from_file_location + submodule_search_locations（framework 实际用法）
    file_path = PLUGIN_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        full, str(file_path), submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = NAMESPACE
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


def load_all():
    """Load the complete plugin (all .py files) in dependency order."""
    order = [
        "config",
        "db",
        "api_client",
        "ws_client",
        "message_parser",
        "triggers",
        "inbound",
        "heartflow",
        "affection",
        "jargon",
        "silk",
        "stt",
        "media",
        "media_oss",
        "webhook",
        "pairing",
        "commands",
        "tools_data",
        "tools_data_extra",
        "tools",
        "adapter",
        "__init__",
    ]
    loaded = {}
    for name in order:
        try:
            loaded[name] = load_plugin_module(name)
        except Exception as e:  # noqa: BLE001
            loaded[name] = f"FAIL: {type(e).__name__}: {e}"
    return loaded


def reset_plugin_modules():
    """Remove all plugin modules from sys.modules so tests can reimport cleanly."""
    for name in list(sys.modules.keys()):
        if name == NAMESPACE or name.startswith(f"{NAMESPACE}."):
            del sys.modules[name]
