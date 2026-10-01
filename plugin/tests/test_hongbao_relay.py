"""接龙 vs 红包 判定优先级 —— ``hongbao-relay-priority.test.mjs`` 的移植（9 例）。

## 为什么这批测试必须存在（抄 TS 原话，别删）

这是一次**静默失效**：接龙被误判成红包后，两处红包拦截（静默入库 / 不触发 AI）
把整条接龙丢掉了 —— **不报错、不写日志标签**，唯一痕迹是一行
``red packet detected: … url=missing``（看着像"来了个没 url 的红包"，
极易误诊成 vendor 侧问题）。实测（只读回放生产账本）：群接龙文案加上"红包"二字
之后的接龙**条条零回复**，而此前文案不含"红包"时的接龙条条有回复，相关性完美对应。

这类"**文案一改就静默失灵**"必须有门钉住，否则下次改文案再中一次。
所以本文件的样本**不是编的**，是那次事故的真实形状（打码后保留判定特征）。

## 与 TS 用例的对应

===============  ==========================================================
TS 用例          本文件
===============  ==========================================================
1  核心回归       :func:`test_a_relay_carrying_the_word_hongbao_is_never_a_red_packet`
2  真红包照旧     :func:`test_real_red_packets_are_still_caught`
3  不误伤         :func:`test_ordinary_messages_are_untouched`
4  53 兼容        :func:`test_legacy_chat_history_rides_the_relay_path`
5  结构优先       :func:`test_only_a_real_relay_is_exempt_from_the_keyword`
5b 编号条目（追加） :func:`test_the_numbered_item_requirement_is_not_dropped`
6  源级门·豁免位置 :func:`test_source_the_exemption_sits_before_the_keyword`
7  源级门·调用点   :func:`test_source_every_call_site_goes_through_the_single_predicate`
8  收窄           :func:`test_plain_text_mentioning_hongbao_is_not_silenced`
9  源级门·卡片绑定 :func:`test_source_the_keyword_is_bound_to_the_card_condition`
===============  ==========================================================

## 本文件与 TS 的两处**有意差异**（都是架构差，不是判定差）

- TS 用例 7 数 ``handler.ts`` 里两处 ``isRedPacketMessage(`` —— py2 没有 handler.ts，
  两处接缝落在 ``inbound/triggers.py``（不触发 AI）与 ``platform/adapter.py``
  （静默入库/只 log）。判据不变：**必须都走同一个判定函数**，不许内联关键词。
- TS 的 ``msgTyp``是数字比较；py2 的 ``is_card_message`` 会 ``int()`` 强制转换
  （vendor 会推字符串型 ``msgType``），所以本文件额外钉住 ``"49"`` 与脏值。

源级门一律**锚在代码行**上（注释里也会出现 "heuristic ①" 等字样，用文字锚会被
自己的注释打败 —— 写 TS 那批时真踩过）。
"""

from __future__ import annotations

from pathlib import Path

from plugin.inbound import hongbao as HB
from plugin.inbound import relay as RL
from plugin.inbound.parser import MT_APP, MT_EMOJI, MT_IMAGE, MT_TEXT, WppMessage

ROOT = Path(__file__).resolve().parents[2]


def _msg(msg_type=MT_TEXT, content="", raw=None) -> WppMessage:
    return WppMessage(account_id="acct", msg_id="m1", from_wxid="wxid_friend",
                      to_wxid="wxid_self0000000000001", content=content,
                      msg_type=msg_type, raw=raw if raw is not None else {})


# ==================================================== TS 1

#: 真实群接龙的形状（打码，只保留判定所需特征）：type=49 + ``#接龙`` 开头 + 文案自带"红包"。
#: **别改这两个样本去迁就实现** —— 它们就是 09-27 那晚被吞掉的那批。
RELAY_WITH_HONGBAO = {
    "msg_type": MT_APP,
    "content": "#接龙 💥抓住新机，全力冲刺 🔥使命必达 今日开售，提升业绩，领取红包🧧 "
               "红包100元 1. 门店＋型号 2. A店 watch6pro 3. B店 watch6pro",
}
RELAY_PLAIN = {"msg_type": MT_APP, "content": "#接龙 周年庆 1. 门店＋型号 2. A店 GT7蓝"}


def test_a_relay_carrying_the_word_hongbao_is_never_a_red_packet():
    """核心回归：文案自带"红包"的接龙**不得**被判为红包（否则整条静默丢弃）。"""
    msg = _msg(**RELAY_WITH_HONGBAO)
    assert RL.is_relay_message(msg) is True, "前提：它确实是接龙"
    assert HB.is_red_packet_message(msg) is False, "接龙优先 ⇒ 不是红包（这就是本次修的 bug）"
    # 不含"红包"的接龙一向正常，一并钉住（防修 A 坏 B）
    assert HB.is_red_packet_message(_msg(**RELAY_PLAIN)) is False


# ==================================================== TS 2

def test_real_red_packets_are_still_caught():
    """真红包照旧被识别（修 bug 不能把红包静默一起放开）。"""
    # heuristic ①: 正文关键词（**卡片形态**，见 TS 8：纯文本不再采信关键词）
    assert HB.is_red_packet_message(_msg(MT_APP, "恭喜发财，红包拿来")) is True
    # 厂商把原生红包卡片译成的精确文案 ⇒ 卡片 + 关键词，必判红包（生产上真红包就是这个形态）
    assert HB.is_red_packet_message(_msg(MT_APP, "微信红包")) is True
    # heuristic ②: 微信原生 red packet appMsg.type=2002（内容里没有"红包"二字也要认）
    assert HB.is_red_packet_message(_msg(MT_APP, "", {"appMsg": {"type": 2002}})) is True
    assert HB.is_red_packet_message(_msg(MT_APP, "", {"appMsg": {"type": "2002"}})) is True
    # heuristic ③: vendor 私有 type 串
    assert HB.is_red_packet_message(_msg(MT_APP, "", {"type": "hongbao"})) is True
    assert HB.is_red_packet_message(_msg(MT_APP, "", {"type": "WX_REDPACKET"})) is True
    # v1.3.72 转账/支付通知：category 命中即静默
    assert HB.is_red_packet_message(_msg(MT_APP, "", {"app": {"category": "payment_notice"}})) is True
    # category 命中 + description 含"转账" ⇒ 兜底分支
    assert HB.is_red_packet_message(
        _msg(MT_APP, "", {"app": {"category": "other", "description": "收到转账10.00元"}})) is True
    # ⚠️ **既有缺口，如实钉住**：description 兜底嵌在 category 判断之内，
    #    所以**没有 category 时它不生效**（TS 同样如此，本次移植不扩大静默面）。
    #    要补就该单独提、单独验 —— 而不是在移植里顺手改掉。
    assert HB.is_red_packet_message(
        _msg(MT_APP, "", {"app": {"description": "收到转账10.00元"}})) is False, \
        "无 category 时 description 兜底不生效（既有缺口，非本次范围）"


# ==================================================== TS 3

def test_ordinary_messages_are_untouched():
    """普通消息不受影响（不误伤）。"""
    assert HB.is_red_packet_message(_msg(MT_TEXT, "今天下午三点开会")) is False
    assert HB.is_red_packet_message(_msg(MT_TEXT, "[图片] https://oss/a.jpg")) is False
    assert HB.is_red_packet_message(_msg(MT_IMAGE, "")) is False


# ==================================================== TS 4

def test_legacy_chat_history_rides_the_relay_path():
    """旧 chat-history（``msgType=53``）走接龙兼容路径 ⇒ 同样不被当红包。"""
    # is_relay_message 对 53 无条件 True（历史兼容）；53 是"聊天记录"而非红包，排除它是正确方向
    assert RL.is_relay_message(_msg(53, "红包")) is True
    assert HB.is_red_packet_message(_msg(53, "聊天记录 红包")) is False


# ==================================================== TS 5

def test_only_a_real_relay_is_exempt_from_the_keyword():
    """结构优先于关键词：**只有真接龙**（49 + ``#接龙``）才豁免。"""
    # 卡片（49）但正文不是接龙 → 不豁免，关键词照常命中
    assert HB.is_red_packet_message(_msg(MT_APP, "红包雨来了")) is True
    # 支付类结构信号也**先过接龙**：真接龙卡片里出现"收款"不算转账
    assert HB.is_red_packet_message(
        _msg(MT_APP, "#接龙 收款方式见群公告 1. 张三",
             {"app": {"category": "payment_notice"}})) is False
    # 纯文本不是接龙也不算红包（v1.9.2）—— 接龙豁免**不是**这里生效，
    # 而是关键词根本不采信纯文本（判据不同：一个管"卡片结构"，一个管"哪种消息才看关键词"）
    assert HB.is_red_packet_message(_msg(MT_TEXT, "接龙报名 1. 张三 红包100元")) is False


# ==================================================== TS 5b（py2 追加：编号条目判据）

def test_the_numbered_item_requirement_is_not_dropped():
    """「接龙」二字**单独**不足以判成接龙卡片 —— 还必须带编号条目（``N. xxx``）。

    只有 ``#接龙`` 是自足标记（微信接龙模板的开头）；光出现"接龙"两字就认，
    会把**讨论接龙**的普通卡片也当成接龙放进来：
    ``_media_tag`` 给正文打上 ``[接龙]``、接龙节流 key 混进非接龙消息、
    触发侧还把它当"100% 必回"。判据与 TS 逐字一致（``N. xxx``）。

    ⚠️ 这条是**变异测出来的盲区**：把 ``and _NUMBERED_ITEM_RE.search(text)``
    删掉后，本文件其余用例**没有一条会红**（放宽方向恰好只影响"含接龙但无编号"
    那类样本，谁都没写）。补上它，那条变异才被杀死。
    """
    assert RL.is_relay_message(_msg(MT_APP, "接龙我不参加了")) is False
    assert RL.is_relay_message(_msg(MT_APP, "接龙 红包雨来了")) is False, "无编号条目 ⇒ 不是接龙卡片"
    assert RL.is_relay_message(_msg(MT_APP, "#接龙 随意一句话")) is True, "``#接龙`` 是自足标记"
    assert RL.is_relay_message(_msg(MT_APP, "接龙\n1. 张三")) is True
    # 编号条目要求「点号后有空白」：``2.赵六`` 不匹配（真实文案是 ``2. 赵六``）。
    # 这条边界值不值得"修"另说，但它**是**现行判据，钉住它免得改法不明。
    assert RL.is_relay_message(_msg(MT_APP, "接龙\n2.赵六")) is False


# ==================================================== TS 6

def _hongbao_src() -> str:
    return (ROOT / "plugin/inbound/hongbao.py").read_text(encoding="utf-8")


def test_source_the_exemption_sits_before_the_keyword():
    """源级门：豁免必须写在 hongbao 内、且在关键词启发式**之前**。"""
    src = _hongbao_src()
    # 锚在**代码行**上（注释里也有 "heuristic ①"，用文字锚会被自己的注释打败）
    guard = "if is_relay_message(msg):"
    heuristic1 = "if _HB_KEYWORD_RE.search(content) and is_card_message(msg):"
    i_guard = src.find(guard)
    i_keyword = src.find(heuristic1)
    assert i_guard > 0, "hongbao 必须有豁免代码行（单一真源：两处调用点自动都受保护）"
    assert i_keyword > 0, "heuristic ① (内容关键词) 必须还在（不能靠删掉关键词判定来「修」 bug）"
    assert i_guard < i_keyword, "接龙豁免必须在 heuristic ① 之前生效"
    assert "from .relay import is_relay_message" in src, \
        "必须 import 结构识别（不得在 hongbao 内联第二套接龙判定）"


# ==================================================== TS 7

def test_source_every_call_site_goes_through_the_single_predicate():
    """源级门：每一处红包拦截都走**同一个判定函数**（不得内联关键词）。

    py2 的接缝与 TS 不同（TS 数 ``handler.ts`` 两处）：这里数的是"入站链路上的
    拦截点" —— ``triggers``（不触发 AI）、``adapter``（静默入库/只 log）、
    ``parser``（正文标签）。
    """
    triggers = (ROOT / "plugin/inbound/triggers.py").read_text(encoding="utf-8")
    adapter = (ROOT / "plugin/platform/adapter.py").read_text(encoding="utf-8")

    assert "from .hongbao import is_red_packet_message" in triggers, \
        "triggers 必须从 hongbao 取判定（不得内联关键词）"
    assert "if is_red_packet_message(msg):" in triggers, "triggers 的通话闸必须调判定函数"
    assert 'return False, "red_packet"' in triggers, "拦截后的原因串是日志与观测的口径，别改名"

    assert "from ..inbound.hongbao import is_card_message, process_red_packet" in adapter, \
        "adapter 必须从 hongbao 取处理入口"
    assert "process_red_packet(msg)" in adapter, "adapter 的静默入库点必须调处理入口"

    # 接龙强制触发路径仍必须在（防"顺手删了接龙分支"）
    assert 'return True, "group_solitaire"' in triggers, "接龙强制触发分支必须还在"


# ==================================================== TS 8

#: 生产账本里真实出现过的**人类聊天**形态（打码）。拿关键词静默它们 = 把群友的话吞了。
_PLAIN_TEXT_WITH_HONGBAO = [
    "@某某 群收红包",
    "50红包店群发放@某某",
    "抓紧领大红包啦，坐等你们晒单",
]


def test_plain_text_mentioning_hongbao_is_not_silenced():
    """v1.9.2 收窄：纯文本提到"红包"不再被静默（卡片照旧）。"""
    for content in _PLAIN_TEXT_WITH_HONGBAO:
        assert HB.is_red_packet_message(_msg(MT_TEXT, content)) is False, \
            f"纯文本不该静默: {content}"
    # 图片/表情同理（只有卡片才采信关键词）
    assert HB.is_red_packet_message(_msg(MT_IMAGE, "红包截图")) is False
    assert HB.is_red_packet_message(_msg(MT_EMOJI, "红包")) is False
    # 反向：卡片形态（真红包的形态）一律照旧静默
    assert HB.is_red_packet_message(_msg(MT_APP, "微信红包")) is True


# ==================================================== TS 9

def test_source_the_keyword_is_bound_to_the_card_condition():
    """源级门：关键词启发式必须与"卡片"条件绑定（不得退回裸关键词）。"""
    src = _hongbao_src()
    assert "is_card_message(msg)" in src, "hongbao 必须有 is_card_message"
    assert "if _HB_KEYWORD_RE.search(content) and is_card_message(msg):" in src, \
        "关键词判定必须 `and is_card_message(msg)` 之后才 return true（否则纯文本又被吞）"
    assert "return int(getattr(msg, \"msg_type\", 0)) == MT_APP" in src, \
        "卡片定义 = MT_APP（数字只在 parser 定义一次）"


# ==================================================== 单一真源（超出 TS，py2 追加）

def test_the_card_number_lives_in_exactly_one_place():
    """``49`` 这个**字面量**只许出现在 ``parser.py`` 的 ``MT_APP`` 定义处。

    TS 侧写死 ``49`` 的地方散着（``isCardMessage`` / ``isRelayMessage`` 各一份），
    py2 收口成一个符号：两处对 49 的理解要漂移，得先改 parser。这是 I4
    （一份算法两处实现，迟早分叉）在"类型常量"上的直接应用。
    """
    assert HB.is_card_message(_msg(MT_APP)) is True
    assert HB.is_card_message(_msg("49")) is True, "vendor 会推字符串型 msgType"
    assert HB.is_card_message(_msg(MT_TEXT)) is False
    assert HB.is_card_message(_msg(None)) is False, "脏值不许抛（入站链路上没有「从容失败」）"
    assert _msg(MT_APP).is_card is True, "parser 侧必须复用同一个判定"
    assert _msg(MT_TEXT).is_card is False

    import ast

    offenders: list[str] = []
    for path in (ROOT / "plugin").rglob("*.py"):
        if "/tests/" in str(path) or path.name == "parser.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Compare) and len(node.comparators) == 1
                    and isinstance(node.comparators[0], ast.Constant)
                    and node.comparators[0].value == 49):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"49 的字面量比较只许在 parser.py 出现（别处用 MT_APP）: {offenders}"


def test_the_two_solitaire_judgements_stay_separate():
    """:func:`triggers._is_solitaire`（正文含"接龙"）与 :func:`relay.is_relay_message`
    （卡片结构）**判的不是一回事**，合并那一刻就是事故复发。

    前者用途是"100% 必回"，后者是"这个 49 是不是接龙卡片"。合并成一个 →
    要么把纯文本接龙漏掉（结构判据认不出它），要么把任何 49 都当接龙
    （华为群实测 49 里含 转账 86 / 应用消息 44 ⇒ 转账会被 100% 触发 AI）。
    """
    from plugin.inbound import triggers as T

    text_relay = _msg(MT_TEXT, "#接龙 1. 我")
    assert T._is_solitaire(text_relay) is True
    assert RL.is_relay_message(text_relay) is False, \
        "纯文本接龙**不是**结构意义上的 relay 卡片（两条判据本就不同）"

    card_without_the_word = _msg(MT_APP, "#接龙 1. 我 2. 他")
    assert RL.is_relay_message(card_without_the_word) is True
    assert T._is_solitaire(card_without_the_word) is True

    card_not_relay = _msg(MT_APP, "这是一条转账")
    assert T._is_solitaire(card_not_relay) is False, "49 里没有「接龙」字样就不该判接龙"
    assert RL.is_relay_message(card_not_relay) is False
