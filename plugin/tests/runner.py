#!/usr/bin/env python3
"""Standalone runner for the multi-account tests.

Loads ``tests/_harness.py`` and ``tests/test_multi_account.py`` via
importlib.util.spec_from_file_location under private namespaces, then
runs the test suite. Avoids both:
  * the ``tools`` package conflict (plugin/tools.py vs hermes/tools/)
  * the ``tests`` package conflict (plugin/tests/ vs hermes/tests/)
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path("/root/.hermes/plugins/wechatpadpro")
HARNESS_PATH = PLUGIN_DIR / "tests" / "_harness.py"
TEST_PATH = PLUGIN_DIR / "tests" / "test_multi_account.py"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Pre-warm: import the framework's ``tools`` package FIRST (before anything
# touchesable). This must come BEFORE any test that uses spec_from_file_location
# because the framework pre-imports tools during startup.
import tools  # noqa: F401  -- must come before harness load

# Load harness under private namespace
harness_mod = _load_module("wpp_test_harness", HARNESS_PATH)

# Load the test file (it pulls harness from sys.modules under wpp_test_harness)
test_mod = _load_module("wpp_test_multi_account", TEST_PATH)

# Run the test suite
exit_code = test_mod.run_all()
sys.exit(exit_code)
