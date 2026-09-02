#!/bin/bash
# Multi-account test runner.
# Runs from /root/ (NOT the plugin dir) so Python resolves `tools` to
# /usr/local/lib/hermes-agent/tools/ (the framework package), not
# /root/.hermes/plugins/wechatpadpro/tools.py (this plugin's single file).

set -e
cd /root
exec /usr/local/lib/hermes-agent/venv/bin/python /root/.hermes/plugins/wechatpadpro/tests/test_multi_account.py "$@"
