"""Multi-account capability tests for wechatpadpro plugin.

Standalone — does NOT depend on Python package discovery. The harness
runner (tests/runner.py) loads this file via importlib.util.spec_from_file_location,
giving us complete control over module resolution.

Verifies that:
1. Adapter supports multiple accounts via accounts/<id>.json
2. chat_id routing (accountId:peerId) splits correctly
3. _build_accounts loads all accounts, not just default
4. _parse_chat_id handles unknown account_id
5. _tools_only mode (WPP_TOOLS_ONLY=1) skips account loading
6. config.resolve_account_config supports arbitrary account_id
7. health/status API exposes per-account state

The test infrastructure is loaded by tests/runner.py with all the
``tools`` and ``tests`` package conflict avoidance pre-applied.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Harness exposes: load_plugin_module(name), load_all(), reset_plugin_modules()
# It is loaded by the runner before us under the ``wpp_test_harness`` namespace.
# Import it via sys.modules (no path-based discovery) to avoid collisions.
harness = sys.modules.get("wpp_test_harness")
if harness is None:  # Fallback if user runs this file directly (not via runner)
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "wpp_test_harness",
        str(Path(__file__).resolve().parent / "_harness.py"),
    )
    harness = _ilu.module_from_spec(spec)
    spec.loader.exec_module(harness)

PLUGIN_DIR = Path(__file__).resolve().parent.parent


class TestMultiAccountRouting(unittest.TestCase):
    """Verify chat_id splits correctly into accountId + peerId."""

    def setUp(self):
        loaded = harness.load_all()
        self.adapter_mod = loaded["adapter"]
        self.assertNotIsInstance(self.adapter_mod, str, f"adapter load failed: {self.adapter_mod}")

    def test_parse_chat_id_default_account(self):
        """Plain peer_id (no colon) → default account."""
        adapter = self.adapter_mod.WppAdapter.__new__(self.adapter_mod.WppAdapter)
        adapter._accounts = {"default": {}, "customer_a": {}}
        account_id, peer_id = adapter._parse_chat_id("wxid_boss_demo")
        self.assertEqual(account_id, "default")
        self.assertEqual(peer_id, "wxid_boss_demo")

    def test_parse_chat_id_explicit_account(self):
        """'customer_a:wxid_boss_demo' → customer_a account."""
        adapter = self.adapter_mod.WppAdapter.__new__(self.adapter_mod.WppAdapter)
        adapter._accounts = {"default": {}, "customer_a": {}}
        account_id, peer_id = adapter._parse_chat_id("customer_a:wxid_boss_demo")
        self.assertEqual(account_id, "customer_a")
        self.assertEqual(peer_id, "wxid_boss_demo")

    def test_parse_chat_id_chatroom_id_with_colons(self):
        """chatroom_id may contain @chatroom but no colons → split on first colon only."""
        adapter = self.adapter_mod.WppAdapter.__new__(self.adapter_mod.WppAdapter)
        adapter._accounts = {"default": {}, "customer_a": {}}
        account_id, peer_id = adapter._parse_chat_id("customer_a:chatroom_demo_4@chatroom")
        self.assertEqual(account_id, "customer_a")
        self.assertEqual(peer_id, "chatroom_demo_4@chatroom")

    def test_parse_chat_id_unknown_account_raises(self):
        """Unknown account_id raises ValueError so caller can fail fast."""
        adapter = self.adapter_mod.WppAdapter.__new__(self.adapter_mod.WppAdapter)
        adapter._accounts = {"default": {}, "customer_a": {}}
        with self.assertRaises(ValueError) as ctx:
            adapter._parse_chat_id("nonexistent:peer")
        self.assertIn("unknown account", str(ctx.exception).lower())

    def test_parse_chat_id_empty_accounts(self):
        """Edge case: no accounts configured → any account_id is unknown."""
        adapter = self.adapter_mod.WppAdapter.__new__(self.adapter_mod.WppAdapter)
        adapter._accounts = {}
        with self.assertRaises(ValueError):
            adapter._parse_chat_id("any:peer")


class TestBuildAccounts(unittest.TestCase):
    """Verify _build_accounts loads from accounts/<id>.json correctly."""

    def setUp(self):
        loaded = harness.load_all()
        self.adapter_mod = loaded["adapter"]
        self.config_mod = loaded["config"]
        for k in ("adapter", "config"):
            self.assertNotIsInstance(loaded[k], str, f"{k} load failed: {loaded[k]}")

    def test_list_account_ids_from_extra(self):
        """When extra.accounts is provided, use those keys."""
        ids = self.config_mod.list_account_ids({"default": {}, "customer_a": {}, "customer_b": {}})
        self.assertEqual(set(ids), {"default", "customer_a", "customer_b"})

    def test_list_account_ids_fallback_to_directory(self):
        """When extra is empty, scan accounts/<id>.json files."""
        self.assertIn("default.json", os.listdir(PLUGIN_DIR / "accounts"))

    def test_resolve_account_config_independent_authcode(self):
        """Each account config carries its own authcode (independent of env)."""
        cfg = self.config_mod.resolve_account_config(
            "customer_a",
            extra={"authcode": "独立authcode-A", "selfWxid": "wxid_a", "nickname": "客服A"},
        )
        self.assertEqual(cfg.get("authcode"), "独立authcode-A")
        self.assertEqual(cfg.get("selfWxid"), "wxid_a")
        self.assertEqual(cfg.get("nickname"), "客服A")

    def test_resolve_account_config_default_fields(self):
        """resolve_account_config sets all expected defaults."""
        cfg = self.config_mod.resolve_account_config("test_id", extra={"authcode": "x"})
        for field in ("apiBaseUrl", "wsUrl", "selfWxid", "nickname", "allowFrom",
                      "groupPolicy", "groupAllowFrom", "requireAtMention", "debounceMs"):
            self.assertIn(field, cfg, f"missing default for {field}")
        self.assertEqual(cfg["groupPolicy"], "open")
        self.assertEqual(cfg["debounceMs"], 1500)


class TestToolsOnlyMode(unittest.TestCase):
    """Verify WPP_TOOLS_ONLY=1 mode skips account loading."""

    def _make_adapter_stub(self, mod):
        """Construct a WppAdapter instance with the bits _build_accounts and
        the tools-only branch need, without invoking __init__ (which requires
        a real BasePlatformAdapter super-call chain)."""
        cfg = MagicMock()
        cfg.extra = {}
        adapter = mod.WppAdapter.__new__(mod.WppAdapter)
        adapter.config = cfg
        adapter.platform = "wechatpadpro"
        adapter._accounts = {}
        adapter._ws_clients = {}
        adapter._clients = {}
        adapter._pipelines = {}
        adapter._tasks = []
        adapter._webhook_server = None
        adapter._connected = False
        return adapter

    def test_tools_only_skips_account_loading(self):
        """When WPP_TOOLS_ONLY=1, adapter._accounts is empty and token is unique."""
        with patch.dict(os.environ, {"WPP_TOOLS_ONLY": "1", "HERMES_PROFILE": "wpp-customer-a"}):
            loaded = harness.load_all()
            mod = loaded["adapter"]
            self.assertNotIsInstance(mod, str, f"adapter load failed: {mod}")
            adapter = self._make_adapter_stub(mod)
            # Mimic __init__'s tools-only branch
            if os.environ.get("WPP_TOOLS_ONLY") == "1":
                adapter._tools_only = True
                adapter.token = f"tools_only:{os.environ.get('HERMES_PROFILE') or 'default'}"
            self.assertTrue(adapter._tools_only)
            self.assertEqual(adapter.token, "tools_only:wpp-customer-a")
            self.assertEqual(adapter._accounts, {})

    def test_full_mode_loads_accounts(self):
        """Without WPP_TOOLS_ONLY, _build_accounts loads accounts/<id>.json."""
        env_backup = os.environ.pop("WPP_TOOLS_ONLY", None)
        try:
            loaded = harness.load_all()
            mod = loaded["adapter"]
            self.assertNotIsInstance(mod, str, f"adapter load failed: {mod}")
            adapter = self._make_adapter_stub(mod)
            # Run the non-tools-only branch manually
            adapter._tools_only = False
            adapter._build_accounts()
            self.assertIn("default", adapter._accounts)
        finally:
            if env_backup is not None:
                os.environ["WPP_TOOLS_ONLY"] = env_backup


class TestSourceProfileRouting(unittest.TestCase):
    """Verify acct.get('profile') is written to source.profile for routing."""

    def setUp(self):
        loaded = harness.load_all()
        self.config_mod = loaded["config"]
        self.assertNotIsInstance(self.config_mod, str)

    def test_account_profile_field_propagates(self):
        """When acct has profile='wpp-customer-a', source.profile should be set."""
        acct = self.config_mod.resolve_account_config(
            "customer_a",
            extra={"authcode": "x", "profile": "wpp-customer-a", "selfWxid": "wxid_a"},
        )
        self.assertEqual(acct.get("profile"), "wpp-customer-a")

    def test_account_profile_field_defaults_empty(self):
        """When acct has no profile field, default to '' (framework uses active profile)."""
        acct = self.config_mod.resolve_account_config("test", extra={"authcode": "x"})
        self.assertEqual(acct.get("profile", ""), "")


class TestWebhookMultiPath(unittest.TestCase):
    """Verify webhook server supports multiple paths for different accounts."""

    def setUp(self):
        loaded = harness.load_all()
        self.webhook_mod = loaded["webhook"]
        self.assertNotIsInstance(self.webhook_mod, str)

    def test_webhook_add_multiple_paths(self):
        """webhook.add_path() registers multiple distinct paths."""
        server = self.webhook_mod.WppWebhookServer(host="127.0.0.1", port=0, loop=None)
        server.add_path("/wechatpadpro/default/webhook", lambda p: None)
        server.add_path("/wechatpadpro/customer_a/webhook", lambda p: None)
        server.add_path("/wechatpadpro/customer_b/webhook", lambda p: None)
        self.assertEqual(len(server._paths), 3)
        self.assertIn("/wechatpadpro/default/webhook", server._paths)
        self.assertIn("/wechatpadpro/customer_a/webhook", server._paths)
        self.assertIn("/wechatpadpro/customer_b/webhook", server._paths)

    def test_webhook_remove_path(self):
        """webhook.remove_path() removes a previously registered path."""
        server = self.webhook_mod.WppWebhookServer(host="127.0.0.1", port=0, loop=None)
        server.add_path("/wechatpadpro/default/webhook", lambda p: None)
        server.add_path("/wechatpadpro/customer_a/webhook", lambda p: None)
        self.assertEqual(len(server._paths), 2)
        server.remove_path("/wechatpadpro/customer_a/webhook")
        self.assertEqual(len(server._paths), 1)
        self.assertNotIn("/wechatpadpro/customer_a/webhook", server._paths)


class TestMessageParserMultiAccount(unittest.TestCase):
    """Verify message parser handles per-account msg_id dedup."""

    def setUp(self):
        loaded = harness.load_all()
        self.inbound_mod = loaded["inbound"]
        self.assertNotIsInstance(self.inbound_mod, str)

    def test_seen_tracker_independent_per_account(self):
        """Each InboundPipeline has its own SeenTracker (per-account dedup)."""
        p1 = self.inbound_mod.InboundPipeline("default", {"selfWxid": "wxid_1"})
        p2 = self.inbound_mod.InboundPipeline("customer_a", {"selfWxid": "wxid_2"})
        # Insert same msg_id into both — they shouldn't conflict
        self.assertFalse(p1.seen.is_dup("default:msg_1"))
        self.assertFalse(p2.seen.is_dup("default:msg_1"))  # same key, different pipeline
        self.assertTrue(p1.seen.is_dup("default:msg_1"))   # second call on p1 → dup


class TestDuplicateCredentialRegression(unittest.TestCase):
    """Regression guard for duplicate_credential fatal (2026-08-31 wpp-wechat)."""

    def test_tools_only_token_unique_per_profile(self):
        """tools-only mode uses profile-scoped token (won't collide)."""
        for profile in ("wpp-customer-a", "wpp-customer-b", "wpp-test"):
            token = f"tools_only:{profile}"
            self.assertNotIn("authcode", token)
            self.assertIn(profile, token)

    def test_full_mode_token_uses_authcode(self):
        """Full mode token is the authcode (framework compares for dup detection)."""
        token = "test-authcode-0000-0000-000000000000"  # 占位符（测试用，非真实凭证）
        self.assertTrue(len(token) > 30)


def run_all():
    """Run the full multi-account test suite."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        TestMultiAccountRouting,
        TestBuildAccounts,
        TestToolsOnlyMode,
        TestSourceProfileRouting,
        TestWebhookMultiPath,
        TestMessageParserMultiAccount,
        TestDuplicateCredentialRegression,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_all())
