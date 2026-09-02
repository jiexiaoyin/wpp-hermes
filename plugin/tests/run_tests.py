#!/usr/bin/env python3
"""Multi-account capability verification — uses wechatpadro plugin's existing
tool-registration path to verify all advertised tools register cleanly when
multi-account configuration is in place.

Run after a deploy to ensure adapter.tools dispatch still works:
  cd /root
  /usr/local/lib/hermes-agent/venv/bin/python /root/.hermes/plugins/wechatpadpro/tests/runner.py
"""
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, "/root")
    runner = Path("/root/.hermes/plugins/wechatpadpro/tests/runner.py")
    if not runner.exists():
        print(f"FAIL: runner not found at {runner}")
        sys.exit(2)
    # Execute the runner in-process
    with open(runner, "rb") as f:
        code = compile(f.read(), str(runner), "exec")
        exec(code, {"__name__": "__main__", "__file__": str(runner)})
