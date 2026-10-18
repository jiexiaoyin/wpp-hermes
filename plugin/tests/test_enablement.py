"""平台启用判定 —— **F1 死锁的直接回归门**（计划第 7 步点名要的那一套）。

## F1 是什么（本文件存在的唯一理由）

历史故障（2026-10-01 在 172.20.109.183 实测）：插件加载成功、日志有
``[WPP] 平台已注册``，但 ``gateway_state.platforms = {}``、
``No messaging platforms enabled.`` —— **平台永远不会被启动**，而工具全都
注册好了、doctor 也全绿。看起来"什么都没坏"，实际整条入站链路是死的。

根因是**判定口径分裂**：Hermes 的凭证门按 ``is_connected`` 判，探针按
``env_enablement_fn`` 给 seed；旧实现这两处口径不同（探针 seed 填了
``accounts``，判定却只认 env/extra），于是"探针说配好了、判定说不认识"，
平台被门挡下。

⇒ 本文件的核心不是"is_connected 返回对不对"，而是

    **探针给的 seed，拿去喂判定，必须判真。**
    **探针说没配（None），判定也必须说没配。**

两者任何一侧单独看都是"合理"的，只有并排才暴露死锁 —— 所以每条路径都要
**成对**断言（见 :func:`test_seed_and_judgement_agree_on_every_path`）。

## Hermes 侧的三条必要条件（0.21.5 实读，见 enablement.py docstring）

seed 非空 + ``is_connected`` 真 + ``check_fn`` 真。``check_fn`` 是硬编码
``return True``（不算变量），所以本地要钉的就是前两条的**一致性**。
"""

from __future__ import annotations

import json

import pytest

from plugin.core import credentials as CR
from plugin.platform import enablement as EN


class _Cfg:
    """Hermes ``GatewayConfig`` 的最小替身 —— 判定只读 ``.extra``。"""

    def __init__(self, extra=None):
        self.extra = dict(extra) if extra else {}


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """把四条输入路径全部清空，再让每条用例只打开自己要的那一条。

    不这么做，"路径 ④ 返回真"可能是因为跑测试的机器上恰好有真凭据 ——
    那样的绿是假的（本机 import 时 ``plugin/accounts/`` 就在旁边）。
    """
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    for k in ("WECHATPRO_ACCOUNTS_JSON", "WECHATPRO_AUTHCODE",
              "WECHATPRO_HOME_CHANNEL", "WECHATPRO_HOME_CHANNEL_NAME"):
        monkeypatch.delenv(k, raising=False)
    yield d


def _write(dirpath, name: str, cfg: dict) -> None:
    (dirpath / f"{name}.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


# ==================================================== 四条路径（F1 的直接回归）

def test_no_path_configured_means_disabled(_clean_env):
    """基线：一条都没有 ⇒ 判定假、seed 为空。

    这条必须先绿 —— 否则下面三条"某路径为真"的断言可能是被别的路径蒙对的。
    """
    assert EN.is_connected(_Cfg()) is False
    assert EN.env_enablement_fn() is None


def test_path_1_extra_accounts(_clean_env):
    """① YAML / 探针 seed 注入的 ``extra["accounts"]``。"""
    cfg = _Cfg({"accounts": {"default": {"authcode": "c", "agent": "wpp-a"}}})
    assert EN.is_connected(cfg) is True


def test_path_2_accounts_json_env(_clean_env, monkeypatch):
    """② ``WECHATPRO_ACCOUNTS_JSON`` 显式注入（不想把凭据落盘时走这条）。"""
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_JSON",
                       json.dumps({"default": {"authcode": "c", "agent": "wpp-a"}}))
    assert EN.is_connected(_Cfg()) is True
    seed = EN.env_enablement_fn()
    assert seed and "default" in seed["accounts"], "② 也要能 seed（否则判定真但没 seed，仍启用不了）"


def test_path_3_authcode_env(_clean_env, monkeypatch):
    """③ 单条 env 凭据（``WECHATPRO_AUTHCODE``）必须**合成 default 账号**再 seed。

    旧实现这里 return None：``is_connected`` 判真、探针却不 seed ⇒ 平台永远启用不了。
    这条路径是单机部署最常见的样子（``.env`` 里只填一个 authcode），必须能起来。
    """
    monkeypatch.setenv("WECHATPRO_AUTHCODE", "single-env-code")
    assert EN.is_connected(_Cfg()) is True
    seed = EN.env_enablement_fn()
    assert seed and "default" in seed["accounts"], "③ 必须合成 default 账号，否则平台不启用"
    acct = seed["accounts"]["default"]
    assert acct["authcodeEnv"] == "WECHATPRO_AUTHCODE"
    assert acct["agent"] == "wpp-wechat"


def test_path_4_local_account_files_are_seen(_clean_env):
    """④ 插件自管 ``accounts/*.json`` —— ★ 旧实现漏掉的就是这一路。

    这是**最常用**的一路（运维就是往 ``accounts/default.json`` 填 authcode），
    漏掉它 = 老老实实按文档配好也起不来。
    """
    _write(_clean_env, "default", {"id": "default", "agent": "wpp-a", "authcode": "c"})
    assert EN.is_connected(_Cfg()) is True
    assert CR.any_local_account_configured() is True


def test_seed_and_judgement_agree_on_every_path(_clean_env, monkeypatch):
    """★★ **防 F1 死锁的核心断言**：探针给的 seed 喂回判定，必须判真。

    死锁的形状是"两处各自都说得通、并排才矛盾"，所以只有在同一条用例里
    把 seed 与判定接起来才测得到。逐路径检查（②④是能 seed 的两条）。
    """
    scenarios = [
        {"WECHATPRO_ACCOUNTS_JSON": json.dumps({"default": {"authcode": "c", "agent": "wpp-a"}})},
        {},  # ④：靠下面写进 accounts 目录的文件
    ]
    for env in scenarios:
        for k in ("WECHATPRO_ACCOUNTS_JSON",):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        _write(_clean_env, "default", {"id": "default", "agent": "wpp-a", "authcode": "c"})

        seed = EN.env_enablement_fn()
        assert seed, f"探针必须给 seed（env={env}）"
        # Hermes ``_plugin_is_configured`` 就是把 seed 里除 home_channel 的键
        # setdefault 进 extra，再调 is_connected。这里照做一遍。
        extra = {k: v for k, v in seed.items() if k != "home_channel"}
        assert EN.is_connected(_Cfg(extra)) is True, \
            f"★ 死锁回归：探针 seed 了 {sorted(seed)} 但判定说不认识（env={env}）"
        assert EN.validate_config(_Cfg(extra)) is True, "validate_config 与门同口径"


def test_seed_and_judgement_agree_on_the_negative_side(_clean_env):
    """反向一致：探针说没配（None）⇒ 判定也必须说没配。

    另一半死锁：探针返回 None（平台不启用）而判定返回真 ——
    表现成 ``hermes config`` 说"配好了"、实际平台不启动，运维会去查错方向。
    """
    _write(_clean_env, "default", {"id": "default", "agent": "wpp-a", "authcode": ""})
    assert EN.env_enablement_fn() is None
    assert EN.is_connected(_Cfg()) is False, \
        "探针说没配（随仓库分发的空壳），判定不许说配好了"


def test_an_empty_shell_account_is_never_enough_to_enable(_clean_env):
    """只有随仓库分发的空壳 ``default.json``（``authcode: ""``）⇒ **不启用**。

    启用了但没凭据 = 连不上厂商的重试循环，比"没启用"更难查。
    判据是**凭据齐不齐**，不是"文件在不在"。
    """
    _write(_clean_env, "default", {"id": "default", "agent": "wpp-a", "authcode": ""})
    assert EN.env_enablement_fn() is None
    assert EN.is_connected(_Cfg()) is False
    assert CR.any_local_account_configured() is False

    # authcodeEnv 指向的空变量同样不算（"看起来很配了"的第二种形态）
    _write(_clean_env, "default",
           {"id": "default", "agent": "wpp-a", "authcode": "", "authcodeEnv": "NOPE_NOT_SET"})
    assert EN.is_connected(_Cfg()) is False


# ==================================================== seed 的形状

def test_seed_carries_the_accounts_table(_clean_env):
    """seed 的 ``accounts`` 结构与 ``config.extra["accounts"]`` 同构且键=文件名。"""
    _write(_clean_env, "default", {"id": "default", "agent": "wpp-a", "authcode": "c"})
    _write(_clean_env, "boss2", {"id": "boss2", "agent": "wpp-b", "authcode": "c2"})
    seed = EN.env_enablement_fn()
    assert sorted(seed["accounts"]) == ["boss2", "default"]
    assert seed["accounts"]["default"]["authcode"] == "c", \
        "adapter 要拿这份 cfg 去 resolve()，凭据必须在里面（否则 seed 了也连不上）"


def test_home_channel_is_lifted_only_when_configured(_clean_env, monkeypatch):
    """``home_channel`` 是特殊 key（核心 lift 成 HomeChannel，不做 setdefault）。"""
    _write(_clean_env, "default", {"id": "default", "agent": "wpp-a", "authcode": "c"})

    assert "home_channel" not in EN.env_enablement_fn(), "没配就别塞"

    monkeypatch.setenv("WECHATPRO_HOME_CHANNEL", "wxid_group_abc")
    hc = EN.env_enablement_fn()["home_channel"]
    assert hc["chat_id"] == "wxid_group_abc"
    assert hc["name"] == "Home", "默认名"

    monkeypatch.setenv("WECHATPRO_HOME_CHANNEL_NAME", "门店群")
    assert EN.env_enablement_fn()["home_channel"]["name"] == "门店群", \
        "``_NAME`` 后缀是命名约定（旧实现读的是另一个变量名）"


def test_validate_config_is_the_same_function(_clean_env):
    """``validate_config`` 不许另写一套判据（Hermes 的配置页会显示它）。"""
    assert EN.validate_config is EN.is_connected or \
        EN.validate_config(_Cfg({"accounts": {"a": {"authcode": "c"}}})) is True
    assert EN.validate_config(_Cfg()) is False


# ==================================================== 与工具注册的口径

def test_the_platform_name_is_the_one_tools_register_under():
    """平台名 / toolset 名 / 目录名**三处必须同字面量**，且不一致时是**静默**失效。

    Hermes 的 ``_platform_default_toolset`` 派生 ``hermes-<platform>``，插件自己的
    工具又按 ``toolset="wechatpadpro"`` 注册 —— 名字对不上 ⇒ 注册计数全对、
    平台侧却一个工具都没有（doctor 也看不出问题）。

    这里只有 ``enablement`` / ``adapter`` 两份常量能本地拿到；``adapter.py``
    模块级 import Hermes（本机 import 不了），所以对着**源码行**核。
    """
    from pathlib import Path

    from plugin.toolkit import registry

    root = Path(__file__).resolve().parents[2]
    assert EN.PLATFORM_NAME == "wechatpadpro"
    assert registry.TOOLSET == EN.PLATFORM_NAME, \
        "工具注册用的 toolset 必须与平台同名（否则工具挂不上去）"

    adapter_src = (root / "plugin/platform/adapter.py").read_text(encoding="utf-8")
    assert f'PLATFORM_NAME = "{EN.PLATFORM_NAME}"' in adapter_src, \
        "adapter 的平台名漂了（它决定 config.yaml plugins.enabled 的键与目录名）"

    reg_src = (root / "plugin/registration.py").read_text(encoding="utf-8")
    assert "name=enablement.PLATFORM_NAME" in reg_src, \
        "注册必须用符号（写第二遍字面量就多一处会漂的地方）"
    # toolset 侧**故意**保持字面量（旧实现如此，见 tools/registry.py docstring），
    # 所以这里钉的是"注册点确实带着这个字面量"，不是"它已被常量替换"。
    tools_src = (root / "plugin/toolkit/registry.py").read_text(encoding="utf-8")
    assert f'toolset="{EN.PLATFORM_NAME}"' in tools_src, \
        "工具注册点的 toolset 必须等于平台名（改一个就得改另一个）"
