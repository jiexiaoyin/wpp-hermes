"""平台层契约探针 —— 在真 Hermes 0.21.5 上校验 ``plugin/platform/*`` 的接口契约。

**只读**：不连网、不连 DB、不发消息、不动 ``~/.hermes/plugins/``。用法::

    P=<环境 venv>/bin/python
    $P /tmp/wpp-py2-verify/tools/probe_adapter_contract.py

⚠️ **本探针不是计划里的 V6**。V6 的定义是「全部 299 个工具都注册上」，
那要等工具层（第 4 步）搬完、且 `hermes tools list | grep -c '^wpp_'` 对得上
才算数 —— 届时在 `probe_registration.py` 里验。本探针只证明**平台这一侧**：
适配器满足 Hermes 契约、注册进真 registry 后 ``Platform(...)`` 成立、
分片/XML/响应判定等出站纯函数正确、红线 2 的生产身份守卫仍在。

退出码 0 = 全绿。任何一条红都表示适配器与 Hermes 0.21.5 的契约不符
（会表现为"平台注册了但起不来"或"起来了但收不到信"）。
"""

from __future__ import annotations

import asyncio
import inspect
import sys

for path in ("/root/.hermes/hermes-agent", "/tmp/wpp-py2-verify"):
    if path not in sys.path:
        sys.path.insert(0, path)

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: object, detail: object = "") -> None:
    RESULTS.append((label, bool(cond), str(detail)))


# ────────────────────────────────────────────────────── ① 基类契约（实测）
from gateway.config import Platform, PlatformConfig                      # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry   # noqa: E402
from gateway.platforms.base import BasePlatformAdapter, SendResult       # noqa: E402
from gateway.platforms.event import MessageEvent, MessageType            # noqa: E402
from gateway.platforms.helpers import MessageDeduplicator                # noqa: E402

abstracts = sorted(BasePlatformAdapter.__abstractmethods__)
check("基类抽象方法恰为那 4 个",
      abstracts == ["connect", "disconnect", "get_chat_info", "send"], abstracts)

# ────────────────────────────────────────── ② 注册 → Platform 成员 → 实例化
from plugin import registration                                          # noqa: E402


class _RealCtx:
    """最小可用的 PluginContext 替身。

    ⚠️ 不做「只收下 kwargs」的假替身 —— 那样验不出两件事：

    1. ``PlatformEntry(**entry_kwargs)`` 对**未知 kwarg 会 raise TypeError**，
       真跑一遍才知道我们的 entry_kwargs 名字对不对；
    2. ``Platform("wechatpadpro")`` **只有注册进真 registry 之后才成立**
       （``Platform._missing_`` 走 ``platform_registry.is_registered``）。
       这就是 py2 里反复强调的「注册 → 才能构造 Platform」顺序约束，
       用假替身会得到一个假的 ValueError，误判成契约不符。
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}

    def register_platform(self, **kw: object) -> object:
        self.entries[str(kw["name"])] = kw
        entry = PlatformEntry(source="plugin", plugin_name="wechatpadpro-platform", **kw)  # type: ignore[arg-type]
        platform_registry.register(entry)
        return entry


ctx = _RealCtx()
registration._register_platform(ctx)
check("register_platform 被调用", "wechatpadpro" in ctx.entries, sorted(ctx.entries))
check("PlatformEntry 构造成功（kwarg 名全对）",
      isinstance(platform_registry.get("wechatpadpro"), PlatformEntry))
check("registry.is_registered 为真", platform_registry.is_registered("wechatpadpro"),
      f"scope={platform_registry.current_scope_key()!r}")

entry = ctx.entries.get("wechatpadpro", {})
check("entry 带 env_enablement_fn（F1 的修复）", callable(entry.get("env_enablement_fn")))
check("entry 的 is_connected 是 enablement 的（非旧实现）",
      getattr(entry.get("is_connected"), "__module__", "") == "plugin.platform.enablement",
      getattr(entry.get("is_connected"), "__module__", ""))
check("max_message_length == 4000", entry.get("max_message_length") == 4000,
      entry.get("max_message_length"))

try:
    plat = Platform("wechatpadpro")
    check('Platform("wechatpadpro") 成立（注册后）', plat.value == "wechatpadpro", plat.value)
except ValueError as exc:
    check('Platform("wechatpadpro") 成立（注册后）', False, f"ValueError: {exc}")

from plugin.platform import adapter as ad                                # noqa: E402

check("WppAdapter 无剩余抽象方法", not ad.WppAdapter.__abstractmethods__,
      sorted(ad.WppAdapter.__abstractmethods__))

inst = ad._build_adapter(PlatformConfig(enabled=True))
check("adapter_factory 单参可构造", inst is not None)
check("平台值正确", inst.platform.value == "wechatpadpro", inst.platform.value)
check("MAX_MESSAGE_LENGTH == 4000", inst.MAX_MESSAGE_LENGTH == 4000, inst.MAX_MESSAGE_LENGTH)
check("max_message_length_for_chat() == 4000",
      inst.max_message_length_for_chat("x") == 4000, inst.max_message_length_for_chat("x"))

# 坑③：runner 靠 isinstance 找去重器并 carry_inbound_dedup
check("去重器是 MessageDeduplicator 实例",
      isinstance(getattr(inst, "_dedup", None), MessageDeduplicator),
      type(getattr(inst, "_dedup", None)).__name__)
check("is_connected 初始为 False", inst.is_connected is False, inst.is_connected)
inst._mark_connected()
check("_mark_connected() 后 is_connected 为 True", inst.is_connected is True)
inst._mark_disconnected()
check("_mark_disconnected() 后回到 False", inst.is_connected is False)
check("_get_adapter() 拿到同一实例", ad._get_adapter() is inst)

# ────────────────────────────────────────────────────── ③ 覆写点齐备
for name in ("send_typing", "send_image", "send_image_file", "send_voice",
             "send_video", "send_document"):
    check(f"覆写 {name}", name in ad.WppAdapter.__dict__)

# ──────────────────────────────── ③b 红线 2：生产身份拒连守卫（R1 的验收点）
import dataclasses                                                        # noqa: E402

from plugin.core import accounts as accounts_mod                          # noqa: E402


def _fake_account(self_wxid: str, authcode: str = "x", enabled: bool = True) -> object:
    """按字段注解造一个最小 Account —— 反射式，未来加字段不会让探针失效。"""
    kw: dict[str, object] = {}
    for f in dataclasses.fields(accounts_mod.Account):
        ann = str(f.type)
        if f.name == "id":
            kw[f.name] = "probe"
        elif f.name == "self_wxid":
            kw[f.name] = self_wxid
        elif f.name == "authcode":
            kw[f.name] = authcode
        elif f.name == "enabled":
            kw[f.name] = enabled
        elif "tuple" in ann:
            kw[f.name] = ()
        elif "int" in ann:
            kw[f.name] = 0
        elif "bool" in ann:
            kw[f.name] = False
        else:
            kw[f.name] = ""
    return accounts_mod.Account(**kw)  # type: ignore[arg-type]


prod_reason = accounts_mod.refusal_reason(_fake_account(accounts_mod.PRODUCTION_WXID))
check("★ 生产 bot 身份被拒连（红线 2）", prod_reason is not None,
      "refusal_reason 竟返回 None —— 生产会被踢下线！")
check("拒绝理由里点名了生产 wxid",
      prod_reason is not None and accounts_mod.PRODUCTION_WXID in prod_reason)
check("普通身份不误伤",
      accounts_mod.refusal_reason(_fake_account("wxid_some_test_account")) is None)
check("无 authcode 被拒",
      accounts_mod.refusal_reason(_fake_account("wxid_t", authcode="")) is not None)
check("enabled=false 被拒",
      accounts_mod.refusal_reason(_fake_account("wxid_t", enabled=False)) is not None)
try:
    accounts_mod.assert_connectable(_fake_account(accounts_mod.PRODUCTION_WXID))
    check("assert_connectable 对生产身份 raise", False, "未 raise")
except accounts_mod.AccountError:
    check("assert_connectable 对生产身份 raise", True)

# 守卫真的挂在连接路径上（不是定义了没人调）
_connect_src = inspect.getsource(ad.WppAdapter.connect)
check("connect() 用 refusal_reason 过滤账号",
      "refusal_reason" in _connect_src, "connect() 里找不到守卫调用")

# ────────────────────────────────────────────────────── ④ 出站纯函数
from plugin.platform import outbound                                     # noqa: E402

check("拒绝话术被识别", outbound.is_rejection_phrase("无 @提及，按群规则不回复"))
check("拒绝话术（变体）被识别", outbound.is_rejection_phrase("按群规则不回复"))
check("正常话术不被误杀", not outbound.is_rejection_phrase("好的，我看一下这个单子"))
check("短消息不分片", outbound.chunk_text("hi") == ["hi"])
check("空串返回单片", outbound.chunk_text("") == [""])

# 分片：对齐 TS chunkMarkdown（含 v1.3.63 的硬切 + 代码块硬 cap）
_long = "x" * 9000
_c1 = outbound.chunk_text(_long)
check("单行超限被硬切（>1 片）", len(_c1) > 1, len(_c1))
check("硬切后拼回原文无损", "".join(_c1) == _long)
check("硬切每片 ≤ MAX_MESSAGE_LENGTH", all(len(p) <= 4000 for p in _c1),
      [len(p) for p in _c1])

_paras = "\n\n".join("y" * 1500 for _ in range(5))
_c2 = outbound.chunk_text(_paras)
check("多段落超限被切分（>1 片）", len(_c2) > 1, len(_c2))
check("段落切分每片 ≤ MAX_MESSAGE_LENGTH", all(len(p) <= 4000 for p in _c2),
      [len(p) for p in _c2])

_cb = "```\n" + "\n".join("z" * 300 for _ in range(30)) + "\n```"
_c3 = outbound.chunk_text(_cb)
_hard_cap = 4000 * outbound.CODE_BLOCK_HARD_CAP
check("巨型代码块在硬 cap 后被强制切",
      len(_c3) > 1 and all(len(p) <= _hard_cap for p in _c3),
      f"片数={len(_c3)} 长度={[len(p) for p in _c3]}")
check("引用 XML 是 type=57", "<type>57</type>" in outbound.build_quote_reply_xml("x", "1", "u"))
check("svrid 空时不带 refermsg",
      "refermsg" not in outbound.build_quote_reply_xml("x", "", "u"))
check("send_ok: ret=0 成功", outbound.send_ok({"Code": 0, "Data": {"BaseResponse": {"ret": 0}}}))
check("send_ok: ret=101 失败", not outbound.send_ok({"Code": 0, "Data": {"BaseResponse": {"ret": 101}}}))
check("send_ok: Code=500 失败", not outbound.send_ok({"Code": 500}))

# v1.5.6 SEND-LIST-RET：逐条结果在 Data.List[].Ret，**List 优先**（09-09 某业务晨报 30362
# 群没收到而一路报成功）。两条都是"真机上看不出来的那种错"，所以钉进真机契约。
check("send_ok: List[0].Ret=-2 判拒收（Code=0 + BaseResponse.ret=0）",
      not outbound.send_ok({"Code": 0,
                            "Data": {"BaseResponse": {"ret": 0}, "List": [{"Ret": -2}]}}))
check("send_ok: List 全 0 成功", outbound.send_ok({"Code": 0, "Data": {"List": [{"Ret": 0}]}}))
check("send_ok: 有 List 时不再看 BaseResponse（逐条即终审）",
      outbound.send_ok({"Code": 0,
                        "Data": {"BaseResponse": {"ret": 101}, "List": [{"Ret": 0}]}}))

check("extract_msg_id 认 NewMsgId",
      outbound.extract_msg_id({"Data": {"NewMsgId": 7}}) == "7")
# v1.3.57 P1-2：真响应里 id 只在 Data.List[0]，顶层 msgId 是 undefined
# （此前 bot_msg_id / wpp_messages.msg_id 恒 NULL 的根因）。
check("extract_msg_id 认 Data.List[0].NewMsgId",
      outbound.extract_msg_id({"Data": {"List": [{"NewMsgId": 7483920192837465}]}})
      == "7483920192837465")
_ids_probe = outbound.extract_outbound_msg_ids({"Data": {"List": [{"NewMsgId": 111, "ClientMsgid": 222}]}})
check("出站 id 三元组口径 msgId = clientMsgId ?? newMsgId",
      (_ids_probe.msg_id, _ids_probe.new_msg_id, _ids_probe.client_msg_id) == ("222", "111", "222"),
      _ids_probe)
check("群标识判定", outbound.is_group_peer("1@chatroom") and not outbound.is_group_peer("wxid_a"))

# ──────────────────────── ④b 出站语音：SILK 闸 + 降级发文件（2026-08-12 生产实证）
# 厂商 /Msg/SendVoice 只收 SILK；非 SILK / 超限 silk 会被**静默拒收**并回一个
# 文案为 INVALID_CREDENTIAL 的 Code=-2（误导）。所以这里断的是「不许硬发」。
from plugin.platform import silk_encoder                                    # noqa: E402

check("silk 尺寸上限 == 60000（老板 curl 实测矩阵）",
      silk_encoder.MAX_SILK_BYTES == 60_000, silk_encoder.MAX_SILK_BYTES)
check("可用性探测自洽（.183 上应为不可用）",
      silk_encoder.is_available() is (silk_encoder.unavailable_reason() is None),
      silk_encoder.unavailable_reason())
check("不可用时 encode_to_silk 返 None 而不是抛错",
      asyncio.run(silk_encoder.encode_to_silk("/tmp/不存在.mp3")) is None)

_calls: list[str] = []


class _VoiceStub:
    async def send_voice(self, **_kw: object) -> dict:
        _calls.append("send_voice")
        return {"Code": 0, "Data": {"BaseResponse": {"ret": 0}}}

    async def send_file_v2(self, **_kw: object) -> dict:
        _calls.append("send_file_v2")
        return {"Code": 0, "Data": {"BaseResponse": {"ret": 0}, "NewMsgId": 1}}


class _VoiceRejectStub(_VoiceStub):
    async def send_voice(self, **_kw: object) -> dict:
        _calls.append("send_voice")
        return {"Code": -2, "Data": {"BaseResponse": {"ret": -2, "errMsg": "INVALID_CREDENTIAL"}}}


async def _async_ret(v: object) -> object:
    return v


async def _voice_cases() -> None:
    send = ad.WppAdapter._send_voice_media
    stub = _VoiceStub()

    # ① 已是 .silk 且不超限 ⇒ 直接发语音（与编码器状态无关）
    await send(stub, "wxid_x", "/tmp/a.silk", "a.silk", "QUJD")
    check("① .silk 不超限直接发语音", _calls[-1] == "send_voice", _calls[-1])

    # ② .silk 超限 ⇒ 不许硬发（会被 Code=-2 拒收）⇒ 降级文件
    _big = "A" * ((60_000 // 3) * 4 + 400)
    await send(stub, "wxid_x", "/tmp/b.silk", "b.silk", _big)
    check("② 超限 .silk 降级发文件", _calls[-1] == "send_file_v2", _calls[-1])

    # ③ mp3 + 无编码器（.183 的真实状态）⇒ 降级文件
    await send(stub, "wxid_x", "/tmp/a.mp3", "a.mp3", "QUJD")
    check("③ mp3 无编码器时降级发文件", _calls[-1] == "send_file_v2", _calls[-1])

    # ④ mp3 + 编码器可用 ⇒ 走 send_voice（把编码器临时"装上"）
    _orig = silk_encoder.encode_to_silk
    silk_encoder.encode_to_silk = lambda _ref: _async_ret("QUJD")  # type: ignore[assignment]
    try:
        await send(stub, "wxid_x", "/tmp/a.mp3", "a.mp3", "QUJD")
        check("④ 可编码时走 send_voice", _calls[-1] == "send_voice", _calls[-1])
    finally:
        silk_encoder.encode_to_silk = _orig  # type: ignore[assignment]

    # ⑤ 厂商拒收语音 ⇒ 仍降级文件，绝不假装成功
    await send(_VoiceRejectStub(), "wxid_x", "/tmp/c.silk", "c.silk", "QUJD")
    check("⑤ 厂商拒收语音后降级发文件", _calls[-1] == "send_file_v2", _calls[-1])


asyncio.run(_voice_cases())

# ────────────────────────────────────────── ⑤ chat_id 编解码（多账号）
check("chat_id 编码", inst._chat_id("default", "1@chatroom") == "default:1@chatroom")
try:
    inst._parse_chat_id("ghost:123")
    check("未知账号必须 raise（不静默兜底）", False, "未 raise")
except ValueError:
    check("未知账号必须 raise（不静默兜底）", True)
try:
    inst._parse_chat_id("")
    check("空 chat_id 必须 raise", False, "未 raise")
except ValueError:
    check("空 chat_id 必须 raise", True)

# ────────────────────────────────────────────── ⑥ 依赖探针（.183 上应为 True）
check("check_wechatpadpro_requirements() 为 True（.183 依赖齐备）",
      ad.check_wechatpadpro_requirements() is True)

# ────────────────────────────────────────────────────── ⑦ V9 回归（真环境复跑）
from plugin.inbound import WppMessage, is_red_packet_message, should_process  # noqa: E402

relay = WppMessage(account_id="t", msg_id="1", from_wxid="a", msg_type=49,
                   chatroom_id="1@chatroom",
                   content="#接龙\n提升业绩, 领取红包🧧\n1. 张三")
check("V9: 接龙含『红包』字样不被当红包", is_red_packet_message(relay) is False)
ok, why = should_process(relay, {"selfWxid": "wxid_bot", "nickname": "小助理"})
check("V9: 接龙仍然被触发（group_solitaire）", ok and why == "group_solitaire", why)
real_hb = WppMessage(account_id="t", msg_id="2", from_wxid="a", msg_type=49,
                     chatroom_id="1@chatroom", content="微信红包")
check("真红包卡片仍被静默", is_red_packet_message(real_hb) is True)
plain = WppMessage(account_id="t", msg_id="3", from_wxid="a", msg_type=1,
                   chatroom_id="1@chatroom", content="@小助理 快去抢红包")
check("纯文本里的『红包』不再被静默",
      is_red_packet_message(plain) is False)

# ─────────────────────────────────────────────────────────────── 输出
ok_n = sum(1 for _, c, _ in RESULTS if c)
for label, cond, detail in RESULTS:
    mark = "✅" if cond else "❌"
    extra = f"  ← {detail}" if (detail and not cond) else ""
    print(f"  {mark} {label}{extra}")
print(f"\n{ok_n}/{len(RESULTS)} 通过")
sys.exit(0 if ok_n == len(RESULTS) else 1)
