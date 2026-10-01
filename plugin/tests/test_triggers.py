"""触发判定回归 —— 现状空白补齐（计划第 7 步：``test_triggers.py``）。

现状树只有 ``test_multi_account.py``（旧脚手架，18 例），**零** triggers 覆盖。
而 triggers 恰恰是"事故现场"最多的一个文件：09-27 的接龙被误判红包、（P0-2）空白名单
放行全部群、（P1-2.3）filehelper 被 self 门拦死 —— 三次都是**静默**失效
（不报错、日志看着像别的问题）。

## 本文件守的七件事

1. **接龙排在最前**（v1.9.1）：含"红包"二字的接龙**不许**被静默（09-27 事故原样）。
2. **关键词只在卡片（49）上采信**（v1.9.2）：纯文本里出现"红包"是人话，吞了它就是吞群友的话。
3. **硬门顺序固定**：filehelper **必须在 self 之前**（P1-2.3），否则命令永远被拦。
4. **群策略 fail-closed**：``allowlist`` 空表拒全部（P0-2）；私聊空白名单拒全部。
5. **管理员不绕过白名单**：这条没有"管理员豁免"分支 —— 群里人人都能 @，真需要就该加进白名单。
6. **可选三路默认关**：msgType / 引用 bot / 关键词（老板 2026-09-01 撤回硬编码关键词的同一理由）。
7. **原因串就是口径**：每条静默都有**不同的** reason，日志与观测靠它分辨关了哪一档。

§1 的**判定细节**（结构性豁免排第一、关键词只认卡片、49 单一真源、两个"接龙"判据
不许合并）在 ``test_hongbao_relay.py`` —— 那是 TS ``hongbao-relay-priority`` 的移植；
本文件只管**触发侧的结果**（该不该回、原因串是什么）。两处都写 = 两份会分叉的规格。

真实日期 / 生产 wxid 一律不进本文件（公开仓红线 + ``conftest`` 生产 bot 红线）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.inbound import hongbao as HB
from plugin.inbound import triggers as T
from plugin.inbound.parser import MT_APP, MT_REVOKE, MT_TEXT, WppMessage

ROOT = Path(__file__).resolve().parents[2]
SELF = "wxid_testself0000000000001"
NICK = "测试小助理"


def _msg(**kw) -> WppMessage:
    base = dict(account_id="acct", msg_id="m1", from_wxid="wxid_friend",
                to_wxid=SELF, content="", msg_type=MT_TEXT)
    base.update(kw)
    return WppMessage(**base)


def _cfg(**kw) -> dict:
    base = {"selfWxid": SELF, "nickname": NICK}
    base.update(kw)
    return base


def _group(**kw) -> WppMessage:
    base = dict(chatroom_id="12345@chatroom", from_wxid="wxid_friend")
    base.update(kw)
    return _msg(**base)


# ==================================================== §1 09-27 事故：接龙 vs 红包

#: 真实群接龙的形状（打码，只保留判定所需特征）：``msgType=49`` + ``#接龙`` 开头 +
#: **文案自带"红包"**。09-27 那晚就是这几句话被静默丢弃的 —— 不许改样本迁就实现。
#: 判定的细节（结构性豁免排第一、关键词只认卡片）在 ``test_hongbao_relay.py``；
#: 这里只钉**触发侧的结果**：它必须走"100% 必回"这一路。
_RELAY_TEXTS = [
    "#接龙 💥抓住新机，全力冲刺 🔥使命必达 今日开售，提升业绩，领取红包🧧 "
    "红包100元 1. 门店＋型号 2. A店 watch6pro 3. B店 watch6pro",
    "#接龙 红包100元\n1.王五",
    "#接龙 周年庆\n1. 门店＋型号\n2. A店 GT7蓝",
    # 这一条走的是"无 #接龙 标记、靠「接龙」+ 编号条目"的变体分支（TS 注释里的兼容路径）。
    # ⚠️ 编号条目的正则要求点号后有空白（``N. xxx``）：``2.赵六`` 不匹配 ⇒
    #    既不是接龙、又是卡片 + "红包" ⇒ 会被判成红包。真实接龙文案是 ``2. 赵六``。
    "[接龙] 抢红包活动\n2. 赵六",
]


@pytest.mark.parametrize("text", _RELAY_TEXTS)
def test_a_relay_with_the_word_hongbao_still_triggers(text):
    """**v1.9.1**：接龙判定**先于**红包判定 —— 文案自带"红包"也必须照常触发。

    事故形态：接龙文案自带"红包"⇒ 关键词启发式命中 ⇒ ``should_process`` 在接龙之前
    ``return (False, "red_packet")`` ⇒ **静默零回复**，日志只留一行
    ``red packet detected: … url=missing``（看着像 vendor 没给 url）。

    注意这里断言的是 ``(True, "group_solitaire")`` 而**不是**"没被判红包就行" ——
    被降级成 ``heartflow_candidate`` 同样是回归（接龙是老板拍板的 100% 必回，
    交给心流就回到了"看心情回不回"）。
    """
    msg = _group(content=text, msg_type=MT_APP)
    assert HB.is_red_packet_message(msg) is False, "接龙不许被判成红包"
    assert T.should_process(msg, _cfg()) == (True, "group_solitaire"), \
        "接龙必须走「100% 必回」这一路（原因串是日志与观测的口径）"


def test_a_plain_text_mentioning_hongbao_goes_to_heartflow_not_red_packet():
    """**v1.9.2**：关键词**只在卡片（msg_type=49）上采信**。

    真红包在本系统里全部是 App 卡片；纯文本里的"红包"是人话（"@某某 群收红包"）。
    拿关键词静默纯文本 = **把群友的话吞了**（而旧码正是这么干的）。

    三条样本都是生产账本里真实出现过的人类聊天形态（打码）—— 直接抄自 TS
    ``hongbao-relay-priority.test.mjs`` 用例 8，别自己编更"干净"的样本。
    """
    for text in ("@某某 群收红包", "50红包店群发放@某某", "抓紧领大红包啦，坐等你们晒单"):
        msg = _group(content=text, msg_type=MT_TEXT)
        assert HB.is_red_packet_message(msg) is False, f"纯文本不该被静默: {text}"
        assert T.should_process(msg, _cfg()) == (False, "heartflow_candidate"), \
            "该交给心流判断，而不是硬静默"


def test_a_card_with_the_keyword_is_still_caught():
    """收窄之后**真红包不许漏**：卡片（49）+ 正文含"红包"仍然判红包。"""
    msg = _group(content="微信红包", msg_type=MT_APP)
    assert HB.is_red_packet_message(msg) is True
    assert T.should_process(msg, _cfg()) == (False, "red_packet")


# ==================================================== §2 硬门顺序（P1-2.3）

def test_filehelper_is_decided_before_self():
    """**filehelper 必须排在 self 之前**（P1-2.3）。

    filehelper 会话的发送者常被 vendor 标成**机器人自己**；排在 self 之后 ⇒
    命令**永远被拦**（症状是"发命令没反应"，而日志里一行 ``self`` 看着完全合理）。
    """
    msg = _msg(to_wxid="filehelper", from_wxid=SELF, content="/help")
    assert T.should_process(msg, _cfg()) == (True, "filehelper")

    # 位置本身也要钉住（顺序就是规格）：filehelper 分支的源码行号 < self 分支
    body = (ROOT / "plugin/inbound/triggers.py").read_text(encoding="utf-8")
    assert body.index('return True, "filehelper"') < body.index('return False, "self"'), \
        "filehelper 判定必须写在 self 之前"


def test_revoke_and_official_account_are_silent():
    """撤回与公众号消息本就不该触发 AI（静默，且原因串要能分辨）。"""
    assert T.should_process(_group(msg_type=MT_REVOKE), _cfg()) == (False, "revoke")
    assert T.should_process(_msg(from_wxid="gh_abcdef"), _cfg()) == (False, "official_account")


def test_self_message_from_the_bot_is_never_answered():
    """vendor 回推自己发的消息 ⇒ 必须静默（否则自问自答死循环）。"""
    assert T.should_process(_group(from_wxid=SELF, content="@测试小助理 在吗"), _cfg()) \
        == (False, "self")


def test_a_dirty_msg_type_does_not_break_the_revoke_gate():
    """脏 ``msg_type``（None / 非数字）不许把撤回门炸成异常 —— 入站链路上没有"从容失败"。"""
    assert T.should_process(_group(msg_type=None), _cfg()) == (False, "heartflow_candidate")
    assert T.should_process(_group(msg_type="abc"), _cfg()) == (False, "heartflow_candidate")


# ==================================================== §3 群策略

def test_group_allowlist_empty_means_reject_everything():
    """**P0-2 fail-closed**：``allowlist`` 模式下**空表 = 拒所有群**。

    旧行为是空表放行全部 ⇒ 管理员"清空白名单暂时禁用"反而**全放行**（静默隐患）。
    """
    cfg = _cfg(groupPolicy="allowlist", groupAllowFrom=[])
    assert T.should_process(_group(content="@测试小助理 在吗"), cfg) == \
        (False, "group_not_allowlisted")

    cfg_ok = _cfg(groupPolicy="allowlist", groupAllowFrom=["12345@chatroom"])
    assert T.should_process(_group(content="随便说说"), cfg_ok) == (False, "heartflow_candidate")


@pytest.mark.parametrize("policy,reason", [("disabled", "group_disabled"),
                                           ("closed", "group_closed")])
def test_group_policy_off_switches(policy, reason):
    """``disabled`` / ``closed`` 两个关闸各回各的原因（运维面要能分辨关了哪一档）。"""
    assert T.should_process(_group(content="@测试小助理 在吗"),
                            _cfg(groupPolicy=policy)) == (False, reason)


def test_group_blacklist_beats_everything_including_mention():
    """群黑名单**一票否决**，@bot 也不放行（黑名单是"这个群别说话"的最终决定）。"""
    cfg = _cfg(blacklistGroups=["12345@chatroom"])
    assert T.should_process(_group(content="@测试小助理 在吗"), cfg) == (False, "group_blacklist")


def test_mention_and_solitaire_are_unconditional_yes():
    """@bot 与接龙是 **100% 必回**（老板 2026-09-01 拍板），不走心流。"""
    assert T.should_process(_group(content="@测试小助理 看看这个"), _cfg()) == \
        (True, "group_mention")
    assert T.should_process(_group(content="#接龙\n1. 我"), _cfg()) == (True, "group_solitaire")
    # 昵称分支**必须带 @**（顺口提到昵称不算 @；旧 Python 的裸子串判定会误触发）
    assert T.should_process(_group(content="测试小助理今天怎么样"), _cfg()) == \
        (False, "heartflow_candidate")


def test_admin_has_no_bypass_of_the_group_allowlist():
    """**没有"管理员豁免"分支**：群里任何人都能 @，能绕过白名单就等于白名单不存在。

    这条用例是"反向断言"：故意传一个看着像管理员的 from_wxid，白名单外照样拒。
    """
    cfg = _cfg(groupPolicy="allowlist", groupAllowFrom=["12345@chatroom"],
               admins=[SELF], adminUsers=["wxid_friend"])
    other = _group(chatroom_id="99999@chatroom", from_wxid="wxid_friend",
                   content="@测试小助理 在吗")
    assert T.should_process(other, cfg) == (False, "group_not_allowlisted")


# ==================================================== §4 私聊

def test_dm_is_fail_closed_when_the_allowlist_is_empty():
    """私聊**空白名单 = 拒所有**（防 P0 联系人 fan-out 重演）。"""
    assert T.should_process(_msg(content="你好"), _cfg()) == (False, "dm_fail_closed")
    assert T.should_process(_msg(content="你好"), _cfg(allowAllUsers=True)) == (True, "dm")


def test_dm_allowlist_admits_and_rejects():
    cfg = _cfg(allowFrom=["wxid_friend"])
    assert T.should_process(_msg(from_wxid="wxid_friend"), cfg) == (True, "dm")
    assert T.should_process(_msg(from_wxid="wxid_other"), cfg) == (False, "dm_not_allowlisted")


def test_allow_all_users_wins_over_a_nonempty_allowlist():
    """``allowAllUsers=True`` 是"全放行"的总开关，与名单是否为空无关。"""
    cfg = _cfg(allowAllUsers=True, allowFrom=["wxid_friend"])
    assert T.should_process(_msg(from_wxid="wxid_stranger"), cfg) == (True, "dm")


# ==================================================== §5 可选三路（默认关）

def test_optional_routes_are_off_by_default():
    """三路（msgType / 引用 bot / 关键词）**默认全关** —— 配了才生效。

    不配也触发的话，"默认配置"就不再是安全的（老板 2026-09-01 撤回硬编码关键词的
    同一个理由：别在默认路径上替用户做决定）。
    """
    weird = _group(content="abc", msg_type=MT_APP, reply_to={"from_wxid": SELF})
    assert T.should_process(weird, _cfg()) == (False, "heartflow_candidate")


def test_msg_type_route_requires_enabled_and_respects_its_own_whitelist():
    cfg = _cfg(msgTypeTrigger={"enabled": True, "msgTypes": [49]})
    assert T.should_process(_group(msg_type=MT_APP), cfg) == (True, "group_msg_type")
    assert T.should_process(_group(msg_type=MT_TEXT), cfg) == (False, "heartflow_candidate")

    scoped = _cfg(msgTypeTrigger={"enabled": True, "msgTypes": [49],
                                  "whitelistGroups": ["12345@chatroom"]})
    assert T.should_process(_group(msg_type=MT_APP), scoped) == (True, "group_msg_type")
    assert T.should_process(_group(chatroom_id="99999@chatroom", msg_type=MT_APP), scoped) \
        == (False, "heartflow_candidate"), "配了白名单就只在这些群触发"


def test_quote_bot_route_is_the_same_definition_as_the_heartflow_marker():
    """引用 bot 的判据**同一个函数**（``is_quoting_bot``）—— 触发用一份、标记用另一份必漂移。"""
    quoted = _group(reply_to={"from_wxid": SELF})
    assert T.is_quoting_bot(quoted, SELF) is True
    assert T.should_process(quoted, _cfg(quoteBotTrigger={"enabled": True})) == \
        (True, "group_quote_bot")
    assert T.should_process(quoted, _cfg()) == (False, "heartflow_candidate")
    assert T.is_quoting_bot(_group(reply_to={"from_wxid": "wxid_other"}), SELF) is False
    assert T.is_quoting_bot(_group(), SELF) is False
    assert T.is_quoting_bot(quoted, "") is False, "没有 selfWxid 就谈不上「引用了我」"


def test_keyword_route_modes_and_the_group_blacklist_veto():
    """关键词路：``substring`` / ``exact`` / ``regex`` 三种模式 + 群黑名单一票否决。"""
    sub = _cfg(keywordTrigger={"enabled": True, "keywords": ["晒单"]})
    assert T.should_process(_group(content="@某人 我晒单啦"), sub) == (True, "group_keyword")

    exact = _cfg(keywordTrigger={"enabled": True, "matchMode": "exact", "keywords": ["晒单"]})
    assert T.should_process(_group(content="我晒单啦"), exact) == (False, "heartflow_candidate")
    assert T.should_process(_group(content=" 晒单 "), exact) == (True, "group_keyword")

    rx = _cfg(keywordTrigger={"enabled": True, "matchMode": "regex", "keywords": [r"出单\d+"]})
    assert T.should_process(_group(content="出单12"), rx) == (True, "group_keyword")

    veto = _cfg(keywordTrigger={"enabled": True, "keywords": ["晒单"],
                                "blacklistGroups": ["12345@chatroom"]})
    assert T.should_process(_group(content="晒单"), veto) == (False, "heartflow_candidate"), \
        "群黑名单优先于关键词命中"


def test_a_broken_regex_does_not_kill_the_other_keywords():
    """坏正则**跳过**（不抛）—— 一个手滑的关键词不许把整条触发链带崩。"""
    cfg = _cfg(keywordTrigger={"enabled": True, "matchMode": "regex",
                               "keywords": ["([", "晒单"]})
    assert T.should_process(_group(content="晒单"), cfg) == (True, "group_keyword")


def test_keyword_route_requires_enabled_flag():
    """``enabled`` 缺席一律不生效（``{keywords: [...]}`` 这种半截配置不许意外开启）。"""
    assert T.should_process(_group(content="晒单"), _cfg(keywordTrigger={"keywords": ["晒单"]})) \
        == (False, "heartflow_candidate")


# ==================================================== §6 日志可分辨性

def test_red_packet_log_distinguishes_url_and_card(caplog):
    """红包日志必须**同时**带 ``url=`` 与 ``card=``（09-27 潜伏的直接原因）。

    事故期间那行 ``url=missing`` 长得像"vendor 没推 url"，实际是接龙被误判 ——
    没有 ``card=`` 字段就分辨不出这两件事。
    """
    import logging

    with caplog.at_level(logging.INFO, logger=HB.__name__):
        assert HB.process_red_packet(_group(content="微信红包", msg_type=MT_APP)) == {}
    rec = [r for r in caplog.records if "红包" in r.message]
    assert len(rec) == 1
    text = rec[0].getMessage()
    assert "url=missing" in text and "card=True" in text and "type=49" in text

    # 有 url 的形态：微信原生红包卡片（appMsg.type=2002）带 hongbaoInfo。
    # ⚠️ 别拿 ``raw={"hongbao": {...}}`` 当"有 url 的输入" —— ``raw.hongbao`` 只是
    #    **提取**路径（extract_red_packet_info），不是**检测**启发式之一；
    #    检测不命中就压根不进日志，这条断言会以"没有日志"的形式假红。
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=HB.__name__):
        HB.process_red_packet(_group(
            msg_type=MT_APP,
            raw={"appMsg": {"type": 2002, "hongbaoInfo": {"url": "https://x/y", "key": "k"}}}))
    assert len(caplog.records) == 1
    assert "url=present" in caplog.records[-1].getMessage()
