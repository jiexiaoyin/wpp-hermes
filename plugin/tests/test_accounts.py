"""账号层回归（四层解析 / 凭据安全 / 连接前守卫 / 多账号 token）—— 计划第 7 步。

替代旧的 ``test_multi_account.py``（2026-08-31 的旧脚手架，第 7 步删除；它要求
``runner.py`` 先往 ``sys.modules`` 里塞 ``_harness``，且把 ``PLUGIN_DIR`` 写死成
``~/.hermes/plugins/wechatpadpro`` —— 在 dev 树上根本没有那个目录）。

## 覆盖面与**不覆盖**的边界（写在最前面，免得下一个人误判）

本文件测的是 ``core/accounts.py`` + ``core/credentials.py``（**本机可 import**）。
旧脚手架里另外两条意图的归属：

- **多账号路由**（``_parse_chat_id`` 的 ``<account_id>:<peer>`` 首冒号切分）与
  **``WPP_TOOLS_ONLY=1`` 纯工具模式**住在 ``platform/adapter.py``，它模块级
  ``from gateway.config import Platform``（Hermes 独有）⇒ **本机 import 不了**。
  覆盖它们的是 ``tools/probe_adapter_contract.py``（真机，真 Hermes 上 57 项契约）
  与门禁里的 ``adapter-seams`` 变异套件（按调用点源码形状验）。本文件**不再假装**测过。
- 本文件也**不**读 ``plugin/accounts/default.json``（含 authcode，见 .gitignore 红线）：
  每个用例把 ``WECHATPRO_ACCOUNTS_DIR`` 指到 tmp_path。

## 本文件守的五件事

1. **四层解析顺序**（文件 → config.yaml extra → runtime override → env 兜底），
   且 env 是**强制覆盖**而不是 ``setdefault``（``default.json`` 的 ``authcode: ""``
   占位会让 setdefault 永远填不进去 —— 平台起不来的那个根因的下半截）。
2. **凭据永不外泄**：``redacted()`` / 日志 / 指纹里只许出现 ``sha256[:12]``。
3. **连接前守卫四态**：未启用 / 无凭据 / **生产 bot 身份（红线 2）** / 可连。
4. **多账号 token 确定性**：``credential_token`` 取字典序第一个可连账号的
   authcode（旧实现依赖 dict 插入顺序 ⇒ 多账号下不稳定）。
5. **``agent`` 必填且禁 ``main``**（2026-09-01 拍板：防两号共用一个 agent 上下文）。
"""

from __future__ import annotations

import json

import pytest

from plugin.core import accounts as A
from plugin.core import credentials as CR
from plugin.core import overrides as OV


@pytest.fixture(autouse=True)
def _isolated_accounts_dir(tmp_path, monkeypatch):
    """每个用例一个空账号目录 + 清掉 runtime override。

    ⚠️ ``overrides._runtime_overrides`` 是**模块级可变状态**、不在 ``CACHE_REGISTRY``
    里（root conftest 只清心流那九个缓存）⇒ 谁改了谁负责还原。本文件自己兜住。
    """
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_JSON", raising=False)
    # 厂商地址现在是必填项（公开仓库不带默认域名）。测试统一给一个占位地址；
    # 专门测「未配置」的用例自己 delenv 覆盖。
    monkeypatch.setenv("WECHATPRO_API_BASE", "https://vendor.test")
    monkeypatch.delenv("WECHATPRO_WS_URL", raising=False)
    # 受保护身份集合默认空；守卫类用例自己 setenv。
    monkeypatch.delenv(A.PROTECTED_WXIDS_ENV, raising=False)
    monkeypatch.delenv(A.PROTECTED_FINGERPRINTS_ENV, raising=False)
    monkeypatch.delenv(A.ALLOW_PRODUCTION_ENV, raising=False)
    OV._runtime_overrides.clear()
    yield d
    OV._runtime_overrides.clear()


def _write(dirpath, account_id: str, cfg: dict) -> None:
    (dirpath / f"{account_id}.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _cfg(**kw) -> dict:
    base = {"id": "default", "agent": "wpp-wechat"}
    base.update(kw)
    return base


#: 守卫测试用的**编造**受保护 wxid（真实生产 wxid 不进公开仓）。
PROTECTED_WXID = "wxid_protected_test"


# ==================================================== 1. 四层解析

def test_defaults_are_the_documented_ones(_isolated_accounts_dir, monkeypatch):
    """默认值即规格：改这里就是改线上默认行为。"""
    for k in ("WECHATPRO_AUTHCODE", "WECHATPRO_WS_URL",
              "WECHATPRO_WEBHOOK_HOST", "WECHATPRO_WEBHOOK_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    _write(_isolated_accounts_dir, "default", _cfg())

    acct = A.resolve("default")
    assert acct.group_policy == "open"
    # W7-6：500 不是随手改的 —— 它是 TS ``core/constants.ts:62`` 的现值
    # （2026-08-22 由 1500 降下来的那个），而这个键从 W7-6 起**真的进了收信链路**。
    assert acct.debounce_ms == 500
    # 地址没有内置默认值：取 fixture 给的占位；WS 未配时按 API base 推导。
    assert acct.api_base_url == "https://vendor.test"
    assert acct.ws_url == "wss://vendor.test/ws/sync"
    assert acct.webhook_host == "127.0.0.1"
    assert acct.webhook_port == 4398
    assert acct.webhook_path == "/wechatpadpro/default/webhook"
    assert acct.webhook_business_path == "/wechatpadpro/default/webhook/business"
    assert acct.nickname == "微信助手"
    assert acct.enabled is True and acct.auto_set_webhook is True
    assert acct.set_webhook_retries == 3
    assert acct.allow_from == () and acct.group_allow_from == ()
    # 特色功能默认**关**（不在默认路径上替用户做决定）。
    # ⚠️ mcp_enabled 是唯一例外：TS ``mcpEnabledForCurrentAccount`` 是 ``!== false``
    #    （缺省开），2026-10-18 接上 vendor MCP 后按 TS 对齐。
    assert (acct.llm_intent_enabled, acct.mcp_enabled, acct.group_context_enabled,
            acct.dm_pairing_enabled, acct.friend_circle_publish_enabled) == \
        (False, True, False, False, False)
    assert acct.heartflow == {} and acct.jargon == {}


def test_no_default_public_url_is_ever_invented(_isolated_accounts_dir, monkeypatch):
    """``plugin.yaml`` 自称 "No default is applied" —— 旧 config.py 硬编码过域名。

    未配 ``webhookPublicUrl`` 就是空串，由 ``Account.webhook_url`` 显式处理；
    偷偷兜一个域名会让"没配"和"配了"在日志里长得一样。
    """
    monkeypatch.delenv("WECHATPRO_WEBHOOK_PUBLIC_URL", raising=False)
    _write(_isolated_accounts_dir, "default", _cfg())
    acct = A.resolve("default")
    assert acct.webhook_public_url == ""
    assert acct.webhook_url == ""

    _write(_isolated_accounts_dir, "default", _cfg(webhookPublicUrl="https://x.test/"))
    acct2 = A.resolve("default")
    assert acct2.webhook_url == "https://x.test/wechatpadpro/default/webhook", "尾部斜杠不许双写"


def test_layers_override_in_order(_isolated_accounts_dir):
    """文件 → extra → runtime override，后者覆盖前者（顺序即优先级）。"""
    _write(_isolated_accounts_dir, "default", _cfg(nickname="文件", debounceMs=100))
    acct = A.resolve("default", {"nickname": "yaml"})
    assert acct.nickname == "yaml", "config.yaml 覆盖账号文件"
    assert acct.debounce_ms == 100, "没被覆盖的键保持文件里的值"

    OV.set_override("default", "nickname", "override")
    assert A.resolve("default", {"nickname": "yaml"}).nickname == "override", \
        "runtime override 覆盖 extra（filehelper 热改的语义）"


def test_a_dict_valued_override_is_merged_not_replaced(_isolated_accounts_dir):
    """``set_override(id, "heartflow", {...})`` **浅合并** —— 不许抹掉同级键。

    抹掉的后果是静默的：阈值/预算这些同级键消失 ⇒ 心流退回默认档，
    而命令的回执还说"已开启"。
    """
    _write(_isolated_accounts_dir, "default",
           _cfg(heartflow={"enabled": False, "threshold": 0.42, "budget": {"cap": 7}}))
    OV.set_override("default", "heartflow", {"enabled": True})

    hf = A.resolve("default").heartflow
    assert hf["enabled"] is True
    assert hf["threshold"] == 0.42, "同一层 dict 里的其它键必须留下"
    assert hf["budget"] == {"cap": 7}


def test_env_beats_a_placeholder_secret(_isolated_accounts_dir, monkeypatch):
    """env 凭据是**强制覆盖**：``authcode: ""`` 占位必须被换掉，不是 setdefault。

    旧实现用 ``setdefault`` ⇒ 键已存在（空串）就不动 ⇒ 凭据永远填不进去。
    """
    monkeypatch.setenv("WECHATPRO_AUTHCODE", "env-secret-value")
    _write(_isolated_accounts_dir, "default", _cfg(authcode=""))
    assert A.resolve("default").authcode == "env-secret-value"

    # 字面值优先于 env
    _write(_isolated_accounts_dir, "default", _cfg(authcode="file-secret"))
    assert A.resolve("default").authcode == "file-secret"

    # <key>Env 间接（凭据只放 env 名，不落盘）
    monkeypatch.setenv("MY_ACCT_CODE", "indirect-secret")
    _write(_isolated_accounts_dir, "default", _cfg(authcode="", authcodeEnv="MY_ACCT_CODE"))
    assert A.resolve("default").authcode == "indirect-secret"


def test_agent_is_required_and_main_is_forbidden(_isolated_accounts_dir):
    """``agent`` 缺失 / 为 ``main`` ⇒ **raise**（配置错要当场炸，不许静默降级）。

    串号是安全事故：两个微信号共用一个 agent 上下文，会互相"记得"对方的对话。
    """
    _write(_isolated_accounts_dir, "default", {"id": "default"})
    with pytest.raises(A.AccountError, match="缺 agent"):
        A.resolve("default")

    _write(_isolated_accounts_dir, "default", _cfg(agent="main"))
    with pytest.raises(A.AccountError, match="被禁止"):
        A.resolve("default")


def test_a_broken_account_only_skips_itself(_isolated_accounts_dir):
    """一个坏账号不该让整插件起不来（``load_all`` 跳过它并记 error）。"""
    _write(_isolated_accounts_dir, "good", _cfg(id="good", agent="wpp-a"))
    _write(_isolated_accounts_dir, "bad", _cfg(id="bad", agent="main"))
    loaded = A.load_all()
    assert set(loaded) == {"good"}


# ==================================================== 2. 目录扫描

def test_scan_skips_notes_examples_backups(_isolated_accounts_dir):
    """``_notes.json`` / ``*.example.json`` / ``*.bak`` 都不是账号（运维手边的东西）。"""
    for name in ("default.json", "boss2.json"):
        ( _isolated_accounts_dir / name).write_text("{}", encoding="utf-8")
    for junk in ("_notes.json", "default.example.json", "acct_example.json",
                 "default.json.bak", "old.json.tmp", "readme.txt"):
        (_isolated_accounts_dir / junk).write_text("{}", encoding="utf-8")

    assert A.list_account_ids() == ["boss2", "default"], "名单必须确定且有序"
    # extra 给了账号表就以它为准（不扫盘 —— 那是 Hermes config.yaml 的注入路径）
    assert A.list_account_ids({"z": {}, "a": {}}) == ["a", "z"]


def test_a_corrupt_account_file_is_loud_but_not_fatal(_isolated_accounts_dir, caplog):
    ( _isolated_accounts_dir / "default.json").write_text("{ not json", encoding="utf-8")
    assert A.load_account_file("default") == {}
    assert any("JSON 解析失败" in r.getMessage() for r in caplog.records), \
        "坏文件要出声（否则表现成「账号莫名消失」）"
    ( _isolated_accounts_dir / "list.json").write_text("[1,2]", encoding="utf-8")
    assert A.load_account_file("list") == {}, "不是对象的也拒"


# ==================================================== 3. 凭据永不外泄

def test_fingerprint_is_the_only_credential_form_that_may_be_printed(_isolated_accounts_dir):
    secret = "super-secret-authcode"
    acct = A.resolve("default", {"agent": "wpp-a", "authcode": secret})
    fp = acct.credential_fingerprint
    assert len(fp) == 12 and fp != secret
    assert fp == A.fingerprint(secret), "与厂商实例锁同构（sha256[:12]）"
    assert A.fingerprint("") == "-"

    blob = json.dumps(acct.redacted(), ensure_ascii=False)
    assert secret not in blob, "redacted() 是跨账号上下文探查用的，绝不能带凭据"
    assert "tokenKey" not in blob and "webhookSecret" not in blob
    assert secret not in repr(acct), "dataclass repr 也不许带（它在日志里被到处打印）"


def test_token_key_and_webhook_secret_never_reach_the_model_facing_summary(_isolated_accounts_dir):
    acct = A.resolve("default", {"agent": "wpp-a", "authcode": "a1",
                                 "tokenKey": "tk-secret", "webhookSecret": "ws-secret"})
    assert acct.token_key == "tk-secret" and acct.webhook_secret == "ws-secret"
    assert "tk-secret" not in json.dumps(acct.redacted())
    assert "ws-secret" not in json.dumps(acct.redacted())


# ==================================================== 4. 连接前守卫

def _acct(**kw) -> A.Account:
    base = dict(agent="wpp-a", authcode="code", self_wxid="wxid_someone")
    base.update(kw)
    return A.resolve("default", base) if "agent" in base else None


def test_refusal_reasons_are_four_distinct_words(_isolated_accounts_dir, monkeypatch):
    """四种"不能连"各回各的因由 —— 运维面靠它分辨，别合并成一句。"""
    monkeypatch.setenv(A.PROTECTED_WXIDS_ENV, PROTECTED_WXID)

    off = A.resolve("default", {"agent": "wpp-a", "enabled": False, "authcode": "c"})
    assert "enabled=false" in A.refusal_reason(off)

    no_cred = A.resolve("default", {"agent": "wpp-a"})
    assert "无 authcode" in A.refusal_reason(no_cred)

    prod = A.resolve("default", {"agent": "wpp-a", "authcode": "c",
                                 "selfWxid": PROTECTED_WXID})
    reason = A.refusal_reason(prod)
    assert reason and "受保护" in reason and PROTECTED_WXID in reason
    assert A.ALLOW_PRODUCTION_ENV in reason, "拒因里要写清逃生阀叫什么"

    ok = A.resolve("default", {"agent": "wpp-a", "authcode": "c", "selfWxid": "wxid_other"})
    assert A.refusal_reason(ok) is None
    A.assert_connectable(ok)                      # 不抛
    with pytest.raises(A.AccountError):
        A.assert_connectable(prod)


def test_the_protected_wxid_guard_can_only_be_lifted_explicitly(_isolated_accounts_dir, monkeypatch):
    """红线 2：受保护身份**默认拒连**，逃生阀必须显式置 1（且只在受控环境下用）。"""
    monkeypatch.setenv(A.PROTECTED_WXIDS_ENV, PROTECTED_WXID)
    cfg = {"agent": "wpp-a", "authcode": "c", "selfWxid": PROTECTED_WXID}
    assert A.refusal_reason(A.resolve("default", cfg)) is not None

    monkeypatch.setenv(A.ALLOW_PRODUCTION_ENV, "1")
    assert A.refusal_reason(A.resolve("default", cfg)) is None


def test_the_protected_wxid_comes_from_env_not_source(monkeypatch):
    """公开仓不携带生产标识：集合默认空，由部署方在 ``.env`` 里配置。

    （旧实现把生产 wxid 硬编码在源码里。那样虽然"守得住"，但公开仓会泄漏
    生产 bot 标识，而且第三方用户根本不该被这道门影响。）
    """
    monkeypatch.delenv(A.PROTECTED_WXIDS_ENV, raising=False)
    assert A.protected_wxids() == frozenset()

    monkeypatch.setenv(A.PROTECTED_WXIDS_ENV, " wxid_a, wxid_b  wxid_c ")
    assert A.protected_wxids() == frozenset({"wxid_a", "wxid_b", "wxid_c"})


# --------------------------------------------------------- C4：凭据那道判据

def _prod_by_credential(monkeypatch, code: str = "fake-prod-code") -> str:
    """把"受保护凭据指纹"指到一个**编造的**码上。

    真 authcode 不进仓、不进测试（钦定铁律：三条在用生产密钥永不可动），
    所以这里验的是**机制**：指纹命中 ⇒ 拒。生产部署把真实指纹写进
    ``WECHATPRO_PROTECTED_FINGERPRINTS``，由运维面守护。
    """
    monkeypatch.setenv(A.PROTECTED_FINGERPRINTS_ENV, A.fingerprint(code))
    return code


def test_a_production_authcode_is_refused_even_when_selfwxid_looks_innocent(
        _isolated_accounts_dir, monkeypatch):
    """★ C4：``selfWxid`` 只是配置里的一句话 —— 会不会把生产踢下线由 **authcode** 决定。

    审阅原话：守卫只看 ``selfWxid`` ⇒ 配置里不写 / 写成别的 wxid / 被
    ``WECHATPRO_ACCOUNTS_JSON`` 注入覆盖，都能拿**生产那把客户端密钥**去连调试实例。
    厂商按客户端密钥上实例锁（``sha256(key)[:12]``），锁一撞，下线的是生产。
    """
    monkeypatch.delenv(A.ALLOW_PRODUCTION_ENV, raising=False)
    monkeypatch.delenv("WECHATPRO_AUTHCODE", raising=False)
    code = _prod_by_credential(monkeypatch)

    acct = A.resolve("default", {"agent": "wpp-a", "authcode": code,
                                 "selfWxid": "wxid_innocent"})

    assert acct.is_production_identity is True, "凭据命中就是生产身份"
    reason = A.refusal_reason(acct)
    assert reason, "凭据是生产那把锁 ⇒ 不能连"
    assert "红线 2" in reason
    assert acct.credential_fingerprint in reason, "拒因要写**指纹**（不是 authcode 本身）"
    assert code not in reason, "拒因里永不出现明文凭据"
    assert PROTECTED_WXID not in reason, "selfWxid 不是这条路的原因，别写串"

    with pytest.raises(A.AccountError):
        A.assert_no_red_line([acct])


def test_an_empty_authcode_never_matches_a_production_fingerprint(_isolated_accounts_dir, monkeypatch):
    """空 authcode 的指纹是 ``-`` —— 空壳账号不许被误判成生产身份。

    （判据里那句 ``ft != "-"`` 就是为它写的：空串的 sha256 是个**具体值**，
    万一哪天有人把指纹集合写成算出来的空串指纹，全树空壳账号会集体变红线。）
    """
    monkeypatch.delenv(A.ALLOW_PRODUCTION_ENV, raising=False)
    monkeypatch.delenv("WECHATPRO_AUTHCODE", raising=False)
    acct = A.resolve("default", {"agent": "wpp-a"})
    assert acct.credential_fingerprint == "-"
    assert acct.is_production_identity is False
    assert A.fingerprint("") == "-" and "-" not in A.protected_fingerprints()


def test_the_escape_hatch_lifts_the_credential_guard_too(_isolated_accounts_dir, monkeypatch):
    """逃生阀对**两道**判据都有效 —— 否则凭据那条会变成无法解除的死锁。"""
    code = _prod_by_credential(monkeypatch)
    cfg = {"agent": "wpp-a", "authcode": code, "selfWxid": "wxid_innocent"}

    monkeypatch.delenv(A.ALLOW_PRODUCTION_ENV, raising=False)
    assert A.refusal_reason(A.resolve("default", cfg)) is not None

    monkeypatch.setenv(A.ALLOW_PRODUCTION_ENV, "1")
    acct = A.resolve("default", cfg)
    assert A.refusal_reason(acct) is None
    assert A.red_line_reason(acct) is None
    A.assert_no_red_line([acct])                    # 不抛


def test_a_red_line_account_is_a_raise_not_a_skip(_isolated_accounts_dir, monkeypatch):
    """★ ``refusal_reason`` 把"没配好"和"红线"混成同一个出口 ⇒ 后者只被 warn 掉了。

    ``red_line_reason`` 是那条分开的判据：它**只**认红线，
    ``connect()`` 靠它决定"当场炸"而不是"跳过"（C4 的核心）。
    """
    monkeypatch.setenv(A.PROTECTED_WXIDS_ENV, PROTECTED_WXID)
    monkeypatch.delenv("WECHATPRO_AUTHCODE", raising=False)

    off = A.resolve("default", {"agent": "wpp-a", "authcode": "c", "enabled": False})
    nocred = A.resolve("default", {"agent": "wpp-a"})
    prod = A.resolve("default", {"agent": "wpp-a", "authcode": "c",
                                 "selfWxid": PROTECTED_WXID})

    for a in (off, nocred):
        assert A.refusal_reason(a) is not None, "它们确实不能连"
        assert A.red_line_reason(a) is None, "但它们是「没配好」，不是红线"

    assert A.refusal_reason(prod) is not None
    assert A.red_line_reason(prod) is not None, "红线两个判据都认"


def test_an_account_that_cannot_connect_is_not_a_red_line(_isolated_accounts_dir, monkeypatch):
    """★ ``enabled=false`` / 无凭据的生产身份**不算**红线（否则 dev 机起不来）。

    ``~/.hermes`` 上那份 ``default.json`` 就把 ``selfWxid`` 写成生产号、
    同时 ``enabled=false`` + 另一把 authcode —— **摆着不连**是合法形态。
    判据写成"看身份就 raise"的话，那台机器上插件直接起不来（红线的正确后果
    只有一个：**别连**，而不是**别启动**）。
    """
    monkeypatch.setenv(A.PROTECTED_WXIDS_ENV, PROTECTED_WXID)
    monkeypatch.delenv("WECHATPRO_AUTHCODE", raising=False)

    off = A.resolve("default", {"agent": "wpp-a", "authcode": "c", "enabled": False,
                                "selfWxid": PROTECTED_WXID})
    A.assert_no_red_line([off])                      # 不抛
    assert A.red_line_reason(off) is None, "它没在连，谈不上触红线"
    assert "enabled=false" in A.refusal_reason(off), "但仍然不可连 —— 理由是「没启用」"

    nocred = A.resolve("default", {"agent": "wpp-a", "selfWxid": PROTECTED_WXID})
    A.assert_no_red_line([nocred])                   # 不抛
    assert A.red_line_reason(nocred) is None

    on = A.resolve("default", {"agent": "wpp-a", "authcode": "c",
                               "selfWxid": PROTECTED_WXID})
    with pytest.raises(A.AccountError) as ei:
        A.assert_no_red_line([off, nocred, on])
    assert PROTECTED_WXID in str(ei.value)


def test_protected_fingerprints_are_env_driven_and_normalized(monkeypatch):
    """指纹集合从 env 读、大小写归一；默认空集合 = 这道门由部署方启用。"""
    monkeypatch.delenv(A.PROTECTED_FINGERPRINTS_ENV, raising=False)
    assert A.protected_fingerprints() == frozenset()

    monkeypatch.setenv(A.PROTECTED_FINGERPRINTS_ENV, "ABCDEF123456, 0a1b2c3d4e5f")
    assert A.protected_fingerprints() == frozenset({"abcdef123456", "0a1b2c3d4e5f"})


def test_the_adapter_refuses_to_start_on_a_red_line_account():
    """★ 接线（``adapter.connect``）—— 判据再对，不挂上去也是空的。

    ``plugin/platform/adapter.py`` 模块级 ``from gateway.config import Platform``
    （Hermes 独有）⇒ 本机 import 不了，只能按 **AST 源码形状**验；
    变异套件 ``adapter-account-guard`` 保证"拆掉就红"。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "plugin" / "platform" / "adapter.py").read_text(encoding="utf-8"))
    fn = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WppAdapter":
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and sub.name == "connect":
                    fn = sub
    assert fn is not None, "找不到 WppAdapter.connect —— 改名/搬家了？"

    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "assert_no_red_line"]
    assert len(calls) == 1, "connect() 必须（且只需）调一次 assert_no_red_line"
    assert ast.unparse(calls[0]).startswith("accounts_mod.assert_no_red_line("), \
        "必须是 accounts 模块上那个函数，不是同名的别的东西"
    assert ast.unparse(calls[0].args[0]) == "self._accounts.values()", \
        "要判的是**这次加载出来的全部账号** —— 传空集/传别的集合都等于这道门不存在"

    # 排在"可连接账号"那步**之前** —— 反了就等于先把它当"没配好"跳过去，
    # 红线又退回 warn 一句（这正是被修掉的那个 bug）。
    filt = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "connectable" for t in n.targets)]
    assert filt, "找不到 connectable 那步 —— 过滤被挪走了？"
    assert calls[0].lineno < min(n.lineno for n in filt), "红线必须先炸，再谈过滤"


def test_connectable_filters_and_reports(_isolated_accounts_dir, caplog):
    _write(_isolated_accounts_dir, "good", _cfg(id="good", agent="wpp-a", authcode="c"))
    _write(_isolated_accounts_dir, "nocred", _cfg(id="nocred", agent="wpp-a"))
    _write(_isolated_accounts_dir, "off", _cfg(id="off", agent="wpp-a",
                                               authcode="c", enabled=False))
    ok = A.connectable()
    assert [a.id for a in ok] == ["good"]
    assert sum("跳过账号" in r.getMessage() for r in caplog.records) == 2, \
        "每跳过一条都要留痕（否则线上只看到「账号怎么少了」）"


# ==================================================== 5. 多账号 token

def test_credential_token_is_deterministic_not_insertion_ordered(_isolated_accounts_dir):
    """取**字典序第一个可连账号**的 authcode（旧实现取 dict 首个 ⇒ 多账号下不稳定）。

    这个 token 供 Hermes ``_adapter_credential_fingerprint`` 做「同一 authcode
    被两个 adapter 连」的冲突检测。
    """
    a = A.resolve("a", {"agent": "wpp-a", "authcode": "code-a"})
    b = A.resolve("b", {"agent": "wpp-b", "authcode": "code-b"})
    assert A.credential_token([b, a]) == "code-a"
    assert A.credential_token([a, b]) == "code-a", "与传入顺序无关"
    assert A.credential_token([]) == "", "没有可连账号就不参与冲突检测"

    no_cred = A.resolve("z", {"agent": "wpp-z"})
    assert A.credential_token([no_cred, b]) == "code-b", "无凭据的账号不参与"


# ==================================================== 6. credentials：只答"有没有"

def test_credentials_never_raises_and_ignores_empty_shells(monkeypatch):
    """``credentials`` 回答「**有没有**凭据」，永不 raise（Hermes 凭证门用它）。

    随仓库分发的空壳 ``default.json``（``authcode: ""``）**不算配置好** ——
    否则平台会被启用然后连不上厂商、反复重试。
    """
    assert CR.any_configured({"a": {"authcode": ""}}) is False
    assert CR.any_configured({"a": {"authcode": "x"}}) is True
    assert CR.any_configured({}) is False
    assert CR.any_configured(None) is False
    assert CR.any_configured({"a": ["not-a-dict"]}) is False

    monkeypatch.setenv("SOME_CODE_ENV", "v")
    assert CR.any_configured({"a": {"authcodeEnv": "SOME_CODE_ENV"}}) is True
    monkeypatch.delenv("SOME_CODE_ENV")
    assert CR.any_configured({"a": {"authcodeEnv": "SOME_CODE_ENV"}}) is False


def test_env_accounts_json_is_optional_and_forgiving(monkeypatch):
    assert CR.env_accounts() is None
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_JSON", "{ not json")
    assert CR.env_accounts() is None, "坏 JSON 不许抛（凭证门路径上没有从容失败）"
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_JSON", "{}")
    assert CR.env_accounts() is None, "空表等于没配"
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_JSON", '{"a": {"authcode": "x"}}')
    assert CR.env_accounts() == {"a": {"authcode": "x"}}


def test_the_probe_reads_the_same_directory_as_the_loader(_isolated_accounts_dir, monkeypatch):
    """★ 探针（credentials）与账号加载器（accounts）**必须认同一个目录**。

    这条防的是 **F1 同形故障的另一个触发条件**：``credentials.load_local_accounts()``
    原本写死 ``plugin/accounts/``，而 ``accounts.accounts_dir()`` 认
    ``WECHATPRO_ACCOUNTS_DIR`` 覆盖 ⇒ 运维把账号目录挪走后，adapter 从新目录
    读得到账号，探针却只看旧目录 ⇒ seed/判定为假 ⇒ 平台注册了但**永不启用**
    （``gateway_state.platforms = {}``、``No messaging platforms enabled.``）。

    这**不是**测试专用的便利设置：判定分叉就是本模块存在的原因，两处各写一份
    "目录在哪"迟早再分一次。
    """
    _write(_isolated_accounts_dir, "default", {"id": "default", "authcode": "c"})

    assert CR.any_local_account_configured() is True, \
        "探针必须看 WECHATPRO_ACCOUNTS_DIR 指向的目录（与 adapter 同口径）"
    assert set(CR.load_local_accounts()) == {"default"}
    assert A.list_account_ids() == ["default"], "两侧得到的账号名单必须一致"


def test_the_probe_uses_the_same_file_filter(_isolated_accounts_dir):
    """``_notes.json`` 这类运维随手文件，**两侧都不算账号**。

    只有一侧滤掉更糟：探针认它（平台被启用）而加载器不认（一个账号都没有），
    表现就是"启用了但静默不干活"。所以筛选也必须同源。
    """
    ( _isolated_accounts_dir / "_notes.json").write_text(
        json.dumps({"authcode": "oops-should-not-count"}), encoding="utf-8")
    assert CR.any_local_account_configured() is False
    assert A.list_account_ids() == []


def test_the_probe_keys_accounts_by_file_name_not_by_the_id_field(_isolated_accounts_dir):
    """账号的身份 = **文件名**（``resolve(id)`` 就是按文件名读的）。

    若探针按 ``cfg["id"]`` 编号，一个 ``boss2.json``（内含 ``"id": "default"``）
    会让探针喊「已配置」，而 ``resolve("default")`` 读的是**另一个文件**
    —— 又一个"启用了但对不上号"。
    """
    _write(_isolated_accounts_dir, "boss2", {"id": "default", "authcode": "c"})
    assert set(CR.load_local_accounts()) == {"boss2"}, "键必须是文件名"
    assert A.list_account_ids() == ["boss2"]


def test_local_accounts_loader_skips_the_example_file(_isolated_accounts_dir):
    _write(_isolated_accounts_dir, "real", {"id": "real", "authcode": "x"})
    ( _isolated_accounts_dir / "default.example.json").write_text(
        json.dumps({"id": "default", "authcode": "SHOULD-NEVER-LOAD"}), encoding="utf-8")
    ( _isolated_accounts_dir / "broken.json").write_text("{oops", encoding="utf-8")

    got = CR.load_local_accounts()
    assert set(got) == {"real"}, "范例与坏文件都不算账号"
    assert CR.any_local_account_configured() is True
    assert CR.any_configured(got) is True


def test_the_example_shell_alone_does_not_count_as_configured(_isolated_accounts_dir):
    """只有随仓库分发的空壳时，「已配置」必须是假 —— 否则平台空转重试。"""
    _write(_isolated_accounts_dir, "default", {"id": "default", "authcode": ""})
    assert CR.any_local_account_configured() is False


def test_legacy_require_at_mention_key_is_ignored(_isolated_accounts_dir):
    """老板 2026-10-18 拍板：群聊规则 = 被 @ 必回，其余交给心流。

    所以 ``requireAtMention`` 不再建模、不再告警：它不是规则载体（TS 活路径
    ``shouldTrigger`` 也不读它），留着只会让人以为"配了只回 @"。生产账号里
    残留同名键不影响行为，解析时直接忽略。
    """
    _write(_isolated_accounts_dir, "default", _cfg(authcode="c", requireAtMention=True))
    acct = A.resolve("default")
    assert not hasattr(acct, "require_at_mention"), \
        "账号模型里不应再有这个键；规则由 inbound.triggers 的 @ / 心流分流实现"


def test_empty_env_secret_warns_only_once(caplog):
    """``<key>Env`` 指向空变量时**只记录一次，且是 DEBUG 不是 WARNING**。

    两层理由（2026-10-19 审计）：
    1. ``resolve()`` 被 5 分钟一次的 sweep 反复调用，不去重就是一天几百条同款；
    2. 这是**配置写法**问题（键写了 Env 但变量没设），不是运行期异常 —— 多进程各持
       一份进程级 set，WARNING 一天仍有 100+ 条（实测 122），把真异常埋掉。
       所以降 DEBUG，同时保留"每种变量名只一次"的去重契约。

    去重是**进程级**的（``_WARNED_EMPTY_ENVS``），值从"有"变"无"时允许再报。
    """
    import logging

    from plugin.core import accounts as A

    A._WARNED_EMPTY_ENVS.clear()
    cfg = {"webhookSecretEnv": "WPP_TEST_MISSING_SECRET_ENV"}
    with caplog.at_level("DEBUG"):
        assert A._resolve_secret(cfg, "webhookSecret", "WPP_TEST_MISSING_SECRET_ENV") == ""
        assert A._resolve_secret(cfg, "webhookSecret", "WPP_TEST_MISSING_SECRET_ENV") == ""
    hits = [r for r in caplog.records if "WPP_TEST_MISSING_SECRET_ENV" in r.getMessage()]
    assert len(hits) == 1, f"应当只记录一次，实际 {len(hits)} 次"
    assert hits[0].levelno == logging.DEBUG, "配置写法问题不该占 WARNING（会被真异常淹没）"
