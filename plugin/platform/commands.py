"""WPP filehelper 命令处理（迁移自 wpp-openclaw index.ts FILEHELPER_COMMANDS）。

filehelper 消息（发给「文件传输助手」）触发命令，管理功能开关 / 白名单。

## 写配置：**写回账号文件 + 就地重载**（W6，2026-10-02 改）

改动之前，命令写的是 ``core/overrides.py`` 的**内存**覆盖，而 ``Account`` 是 frozen 的、
``adapter.connect()`` 只解析一次并缓存 ⇒ 八条命令（``/user`` ``/group`` ``/blacklist``
``/heartflow`` ``/affection`` ``/jargon`` ``/xiaowei`` …）**全是「回了 ✅、当次不生效、重启就丢」**。
现在统一走 :func:`write_back`：落盘（``core/account_store``）→ 清同键内存覆盖 →
``adapter.reload_account()`` 换掉缓存那份 ``Account`` 并刷新管线快照。

⇒ 命令的回复可以**承诺当次生效**了；写失败时回复「❌ 本次改动未生效」而不是假装成功
（写不进去还回 ✅ 是本项要消灭的失败形态，与"入站落库失败只 warn"刚好相反）。

## 命令面补齐（W7-5，2026-10-02）：``/blacklist`` 与 ``/xiaowei``

TS v1.3.80 把三个白名单域（``/user`` ``/group`` ``/blacklist``）收成一个**批量**处理器
（``src/index.ts:306-369``），v1.3.71 加 ``/xiaowei``（``:249-270``）。py2 此前：
``blacklistGroups`` 这个键**存在且被读**（``core/accounts.py`` + ``inbound/triggers.py``
的群黑名单一票否决）**却没有任何命令能写它** —— 数据路通、入口没有。本轮补齐。

顺带纠正旧实现的两处：① 白名单命令**只认单目标**（``args[1]``），多写的 ID 被**静默丢掉**
而回话照样说「已添加」——"命令说做了、其实没做"那个家族；② list 回话是一个逗号长串，
与 TS 的 ``📋 <label> (n):`` + 每行 ``- x`` 不同形（同一条命令在两边读起来不是一回事）。
⇒ 三个域现在共用**一套**实现，批量语义与回话形状都对齐 TS。

⚠️ ``/xiaowei`` **必须连门禁一起做**：这个键的唯一消费者是 ``tools/policy.py`` 的
``XIAOWEI_GATE``（10 个 ``wpp_xw_*`` 工具**每次调用**读它）。只加开关不加门的写法
造出的正是本计划开头批的那种「开关在、门不在」的谎开关。

## 写回的**底稿**只从盘上读（W7-5）

``_feature_cfg`` / ``_allowlist_items`` 原先经 ``core.accounts.resolve()`` 读底稿，而
``resolve()`` 对"缺 ``agent``"这类瑕疵是**按设计 raise** 的 ⇒ 那时拿到的是**空底稿**，
而 ``write_account_key`` 是**整体替换**那个键 ⇒ 一条 ``/user add x`` 会把现有白名单
**清成只剩 x**（``/heartflow off`` 同理，把 ``replyThreshold`` 一起抹掉），
回话却是「✅ 已加入」。

⇒ 底稿改从**账号文件本身**读（TS 的 ``appendAllowFrom`` 也是直接 readFile，
``config.ts:424``），读不出来就**拒绝写**并说明原因。内存覆盖仍然并入底稿（与
``resolve()`` 的 ③ 层同序）—— 只存在于覆盖里的同级键不该在写回时被丢掉。

（``resolve()`` 的 ② yaml 层在本树**无生产调用方**：生产路径一律 ``resolve(account_id)``，
``extra`` 参数只在测试里出现过 ⇒ 「盘上那份 + 内存覆盖」就是运行期看到的那份配置。）

从旧扁平 ``plugin/commands.py`` 迁入 ``platform/``，装配方式有三处改动：

1. 覆盖存储改从 ``..core.overrides`` 取（旧的是本模块自己定义，导致配置层反向依赖命令层）。
2. 当前状态改读 ``core.accounts.resolve(account_id).raw``（旧的是
   ``config.resolve_account_config``）—— ``raw`` 是**已并入覆盖**的 cfg，
   所以不必再手工叠一层覆盖判断。
3. **删掉 ``/pair`` 分支**（旧的是死码）：``extract_pair_code`` → ``redeem_pairing_code``
   只校验码就回「配对码有效」，**从不真把谁写进白名单**（旧 adapter 也从未调用它）。
   留着一条会骗人的回复，比没有更糟。真正要做「自助加白名单」，得先解决
   「加进哪份白名单」—— 覆盖是内存态、重连就回退到 ``accounts/*.json``，
   拿它当"永久加入"是假的（见 ``platform/pairing.py`` docstring）。
   ``/genpair`` / ``/pairs`` 保留：它们是**管理员自己看码**，本来就是通的。
4. ``/heartflow`` 的**子命令面**搬到 :mod:`.hf_commands`（status 详情 / threshold /
   group / profile / why / layers / veto / report），本模块只留一行分发。理由是那个
   面每一条都要读台账/分层/画像，而本模块是"三功能开关 + 白名单"的通用件 ——
   混在一起会让"改心流展示口径"变成"动一个通用文件"（TS 侧 2026-09-28 同理提取）。
5. （W6）``handle_command`` 多一个**关键字**参数 ``adapter`` —— 写回之后靠它把新配置
   推到已在跑的链路（见 :func:`write_back`）。缺省 ``None`` 是**合法状态**：配置已经
   落盘，只是当次不生效（纯工具模式 / 单测里没有适配器）。回调整条吞异常 ——
   命令链路坏了不该影响别的消息。
"""
from __future__ import annotations

import logging
from typing import Any

from ..core import overrides

log = logging.getLogger(__name__)

HELP_TEXT = """可用命令：
/heartflow on|off|status — 心流主动回复开关（status 含 24h 台账 / 生效阈值 / 预算 / 分层）
/heartflow threshold|group|profile|why|layers|veto|report — 心流运维（见 /heartflow 空参数）
/affection on|off|status — 好感度开关
/jargon on|off|status — 黑话挖掘开关
/style on|off|status — 说话风格学习开关
/style list [群id] — 风格卡 / 版本列表（不带群id 列全部群）
/style show <群id> — 查看当前风格卡内容
/style rollback <群id> <版本> — 回滚到历史版本（版本号只增不减）
/tts on|off|status — 语音合成 / 情绪路由开关
/proactive on|off|status — 私聊主动关心开关
/mute <群id> [分钟] — 临时禁言该群（默认 30 分钟，内存态）
/mute off <群id> — 解除禁言
/mute list — 查看禁言中的群
/xiaowei on|off|status — 小微智能体工具开关（关着时相关工具直接拒绝执行）
/user add|del|list [wxid...] — 私聊白名单（收发共用；改了立刻对出站生效）
/user sync — 从全员群同步员工到私聊白名单（收发同时放行）
/group add|del|list [群id...] — 群白名单（收发共用）
/blacklist add|del|list [群id...] — 黑名单群（一票否决，优先于群白名单）
/whitelist status — 查看统一白名单（私聊/群/管理员/全员群/env 额外项）
/whitelist sync — 同 /user sync（/outbound 是它的别名）
/genpair — 生成配对码
/pairs — 查看配对码
/help — 显示帮助
"""

#: 覆盖里用的**原始 cfg 键**（驼峰）—— ``core.accounts.resolve()`` 就是按这些名字
#: 把覆盖并进 cfg 的，写成别的名字读不到。
#: 键是**命令域名**（TS ``handleWhitelistCommand`` 的三个 ``WhitelistDomain``，
#: ``src/index.ts:304``）—— 域名与命令名同形（``/user`` ``/group`` ``/blacklist``），
#: 所以 ``/`` + 域名就是命令名，用法行不必再单独维护一份名字表。
_ALLOWLIST_KEYS = {"user": "allowFrom", "group": "groupAllowFrom",
                   "blacklist": "blacklistGroups"}

#: 回话里的名字。逐字照抄 TS ``handleWhitelistCommand`` 的 ``label``
#: （``src/index.ts:315``）—— 黑名单那边叫「黑名单群」而不是「黑名单群白名单」。
_ALLOWLIST_LABELS = {"user": "私聊白名单", "group": "群白名单", "blacklist": "黑名单群"}

#: 写回前的**底稿**读不出来时的回话。写回是**整体替换**那个键 ⇒ 底稿为空时不能写：
#: 盲写 = 一条命令把现有白名单/配置段清空，而回话是 ✅（详见模块 docstring 的 W7-5 一节）。
UNREADABLE_CFG_MSG = ("❌ 读不到账号文件（缺失 / 空 / 不是合法 JSON），**本次改动没有生效**："
                      "写回是整体替换那个键，盲写会把现有内容一起抹掉（详见日志）")

#: 状态页读不到配置时的取值（见 :func:`state_label`）。
#: 与 :data:`UNREADABLE_CFG_MSG` 是**两件事**：这条只是"报不出状态"，没有"你的改动没生效"的含义。
UNREADABLE_STATE_MSG = "❓ 读不到账号配置"


def parse_args(args: str) -> list[str]:
    return [a.strip() for a in args.split() if a.strip()]


def handle_command(account_id: str, content: str, *, adapter: Any = None) -> str:
    """处理 filehelper 命令，返回回复文本（空 = 不回复）。

    ``adapter`` 只为**写配置**的命令需要（写回后靠它让新配置当次生效）。
    """
    if not content.startswith("/"):
        return ""
    parts = content.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    arg_list = parse_args(args)

    if cmd == "/help":
        return HELP_TEXT

    # 心流：开关走通用实现，其余子命令（status 详情 / threshold / group / profile /
    # why / layers / veto / report）在 ``hf_commands`` 里，这里**只做分发**。
    # 导入是**函数级**的：hf_commands 在模块级 import 心流七个子模块，放进加载期
    # 就等于给注册链路加一段心流加载代价（F5 的形态，见该模块 docstring）。
    if cmd == "/heartflow":
        from . import hf_commands

        return hf_commands.handle(account_id, arg_list, adapter=adapter)

    # 功能开关: /affection /jargon /tts /proactive（账号 JSON 里同名段）
    if cmd in ("/affection", "/jargon", "/tts", "/proactive"):
        return _handle_feature_toggle(account_id, cmd.lstrip("/"), arg_list, adapter=adapter)

    # 临时禁言（内存态，重启失效 —— 见 adapter._muted_groups）
    if cmd == "/mute":
        return _handle_mute_command(account_id, arg_list, adapter=adapter)

    # 说话风格学习: /style on|off|status 走通用开关；list/show/rollback 走本模块。
    if cmd == "/style":
        return _handle_style_command(account_id, arg_list, adapter=adapter)

    # 统一白名单状态（收发同一份）。/outbound 是历史叫法的别名。
    if cmd in ("/whitelist", "/outbound"):
        return _handle_whitelist(account_id, arg_list, adapter=adapter)

    # 白名单: /user /group /blacklist（三个域共用一套批量实现，见模块 docstring）
    if cmd.lstrip("/") in _ALLOWLIST_KEYS:
        return _handle_allowlist(account_id, cmd.lstrip("/"), arg_list, adapter=adapter)

    # 小微智能体: /xiaowei on|off|status（W7-5）。⚠️ 与 tools/policy.py 的
    #   XIAOWEI_GATE 是**同一个开关的两半** —— 命令写它，门禁读它，缺一半就是谎开关。
    if cmd == "/xiaowei":
        return _handle_xiaowei(account_id, arg_list, adapter=adapter)

    # 配对码: /genpair /pairs（/pair 兑换已删，见模块 docstring）
    if cmd == "/genpair":
        from .pairing import generate_pairing_code
        entry = generate_pairing_code(account_id)
        return f"✅ 新配对码已生成:\n\n配对码: {entry['code']}\n有效期至: {_fmt_ms(entry['expires_at'])}\n\n用法: 发给白名单外用户，用户私聊机器人发 /pair {entry['code']} 自助加入"
    if cmd == "/pairs":
        from .pairing import read_pairing_code
        entry = read_pairing_code(account_id)
        if entry:
            return f"当前配对码:\n\n配对码: {entry['code']}\n有效期至: {_fmt_ms(entry['expires_at'])}"
        return "当前无配对码（未生成或已过期）。用 /genpair 生成。"

    return f"未知命令 {cmd}，/help 查看帮助"


async def handle_command_async(account_id: str, content: str, *, adapter: Any = None) -> str:
    """异步命令入口（网关/filehelper 用）。

    只有「要打厂商接口」的命令需要异步（全员群同步），其余逐字委托
    :func:`handle_command` —— 既有同步命令（含全部单测）行为不变。
    """
    if not content.startswith("/"):
        return ""
    parts = content.split(None, 1)
    cmd = parts[0].lower()
    arg_list = parse_args(parts[1] if len(parts) > 1 else "")
    action = arg_list[0].lower() if arg_list else ""

    if cmd in ("/whitelist", "/outbound") and action == "sync":
        return await _sync_employees(account_id, adapter=adapter)
    if cmd == "/user" and action == "sync":
        return await _sync_employees(account_id, adapter=adapter)
    return handle_command(account_id, content, adapter=adapter)


# --------------------------------------------------------------------------- 内部

def _fmt_ms(ms: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------- 写配置（W6）


def write_back(account_id: str, key: str, value: Any, *, adapter: Any = None) -> str:
    """写回账号配置并让它**当次**生效。成功返回 ``""``，失败返回给用户看的话术。

    三步不可拆：落盘（含备份/原子替换/600）→ 清同键内存覆盖（否则新值被影子掉，
    见 ``core/account_store``）→ 重载 ``Account`` 与管线快照（:func:`reload_now`）。

    ⚠️ 失败**必须出声**：返回「❌ …未生效」而不是照旧回 ✅。
    细节只发**我们自己的** :class:`AccountWriteError`（文案是手写的，不含凭证）；
    其它异常只报**类名** —— 回执要走微信，未知异常的文本里可能带着文件内容/凭据。
    全文一律进日志。
    """
    from ..core import account_store

    try:
        account_store.write_account_key(account_id, key, value)
    except account_store.AccountWriteError as exc:
        log.error("[WPP:%s] 写账号配置失败 key=%s: %s", account_id, key, exc, exc_info=True)
        head = str(exc).splitlines()[0][:200] if str(exc) else "AccountWriteError"
        return f"❌ 写入账号配置失败，**本次改动没有生效**：{head}"
    except Exception as exc:
        log.error("[WPP:%s] 写账号配置失败(未预期) key=%s: %s", account_id, key, exc, exc_info=True)
        return f"❌ 写入账号配置失败，**本次改动没有生效**：{exc.__class__.__name__}（详见日志）"

    reload_now(account_id, adapter)
    return ""


def reload_now(account_id: str, adapter: Any = None) -> bool:
    """让刚写回的配置对**已在跑的链路**生效。返回是否真的重载了。

    ``adapter`` 为 ``None``（纯工具模式 / 单测）**不是错误**：配置已在盘上，
    下次账号解析（重连 / 重启）就会读到 —— 只是当次不生效，所以记一条 warn。
    """
    if adapter is None:
        log.warning("[WPP:%s] 配置已落盘，但没有适配器句柄 ⇒ **当次不生效**"
                    "（重连或重启后生效）", account_id)
        return False
    try:
        adapter.reload_account(account_id)
        return True
    except Exception as exc:
        log.warning("[WPP:%s] 配置已落盘，但重载失败 ⇒ 当次不生效（重连/重启后生效）: %s",
                    account_id, exc, exc_info=True)
        return False


def _safe_cfg(account_id: str) -> dict:
    """取**已并入覆盖**的原始 cfg；解析失败返回空 dict（命令链路不该因配置瑕疵整体哑掉）。

    典型失败：账号没配 ``agent`` ⇒ ``resolve()`` 按设计 raise（串号是安全事故）。
    这时命令只能按内存覆盖判断状态，够用。

    ⚠️ **只用于「读出来给人看」**（状态页/名单展示）。**写回不许用它当底稿**：
    空底稿 + 整体替换 = 一条命令清掉现有内容，而回话是 ✅ —— 见 :func:`_account_file_cfg`。
    """
    try:
        from ..core import accounts as _accounts

        return dict(_accounts.resolve(account_id).raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("[WPP] 命令(account=%s) 读不到账号配置: %s —— 仅按内存覆盖判断", account_id, exc)
        return {}


def _account_file_cfg(account_id: str) -> dict | None:
    """读**盘上那份**账号 cfg —— 写回/读开关时的**底稿**。读不出来返回 ``None``。

    ⚠️ 为什么不用 :func:`_safe_cfg`：那条路经 ``resolve()``，而它对"缺 ``agent``"这类
    瑕疵**按设计 raise** ⇒ 拿到的空 dict 会被当成"这个账号什么都没配"，写回时
    **整体替换**那个键 ⇒ 现有白名单/阈值被清空，而回话是「✅ 已加入」。底稿读不出来
    只该导致**拒绝写**（:data:`UNREADABLE_CFG_MSG`），不该导致静默清段。

    ``load_account_file`` 自己对"文件缺失/坏/根不是对象"只记 error 日志并返回 ``{}``
    ⇒ 这里按"读不出来"处理（一个**真·空账号文件**同样是不可写状态）。
    """
    from ..core import accounts as _accounts

    raw = _accounts.load_account_file(account_id)
    if not raw:
        log.error("[WPP:%s] 读不到账号文件（缺失 / 空 / 不是合法 JSON）—— 拒绝以空底稿写回",
                  account_id)
        return None
    return raw


def _feature_state(account_id: str, feature: str) -> bool | None:
    """当前开关状态。覆盖优先，读不到**盘上那份**配置时返回 ``None``。

    ⚠️ ``None`` 的语义是「**我读不到**」，不是「没开」—— 显示端必须分开说
    （同 :func:`_flag_state` 与 ``tools/policy.py`` 的门禁口径）。混为一谈的后果：
    账号文件坏掉时状态页报「❌ 关闭」，老板照着去 ``/heartflow on`` —— 真故障被话术盖住。

    ⚠️ 底稿读**盘上那份**（:func:`_account_file_cfg`）而不是经 ``resolve()`` 的
    :func:`_safe_cfg`：后者解析失败时**同样**返回空 dict，与"配置里真没这一段"无法区分
    （这正是"读不到"被说成"没开"的机制）。而 ``resolve()`` 的 ② yaml 层在本树没有
    生产调用方 ⇒「文件 ⊕ 覆盖」就是运行期看到的那份，与 :func:`_allowlist_items` 同口径。
    """
    ov = overrides.get_override(account_id).get(feature)
    if isinstance(ov, dict) and "enabled" in ov:
        return bool(ov["enabled"])
    raw = _account_file_cfg(account_id)
    if raw is None:
        return None
    feat = raw.get(feature)
    return bool(isinstance(feat, dict) and feat.get("enabled"))


def state_label(current: bool | None) -> str:
    """状态页把 :func:`_feature_state` 的三种取值印成人话。

    第三种（``None``）是**必须**单列的那一种：把"我读不到"印成「❌ 关闭」是在撒谎，
    而且它同时把老板引向"去把它打开"这个错误动作。
    """
    if current is None:
        return UNREADABLE_STATE_MSG
    return "✅ 开启" if current else "❌ 关闭"


def _state_word(current: bool | None) -> str:
    """同 :func:`state_label`，但印 ``on``/``off``（``/affection`` 那两条的既有口径）。"""
    if current is None:
        return UNREADABLE_STATE_MSG
    return "on" if current else "off"


def _allowlist_items(account_id: str, key: str) -> list[str] | None:
    """当前名单（``allowFrom`` / ``groupAllowFrom`` / ``blacklistGroups``）。

    底稿 = **盘上那份账号文件** + 内存覆盖（覆盖只补缺、不改序），读不出来返回 ``None``
    ⇒ 调用方**必须拒绝写**（空名单 + 整体替换 = 一条命令清空白名单，见模块 docstring）。
    ⚠️ 读出来的这一份**就是运行期看到的那份**：``resolve()`` 的 ② yaml 层在本树没有
    生产调用方（生产路径一律 ``resolve(account_id)``），所以「文件 ⊕ 覆盖」与
    ``resolve()`` 的结果一致。
    """
    raw = _account_file_cfg(account_id)
    if raw is None:
        return None
    val = raw.get(key)
    if val is not None and not isinstance(val, (list, tuple)):
        log.warning("[WPP:%s] %s 不是数组（%s）—— 按空名单处理，本次写回会把它纠正成数组",
                    account_id, key, type(val).__name__)
    items = [str(x) for x in val] if isinstance(val, (list, tuple)) else []
    ov = overrides.get_override(account_id).get(key)
    if isinstance(ov, list):
        items += [str(x) for x in ov if str(x) not in items]
    return items


def _feature_cfg(account_id: str, feature: str) -> dict | None:
    """写开关时的**底稿**：保留阈值等同级键，别让开关动作把整段配置拍扁。

    读不出来返回 ``None`` ⇒ 调用方**必须拒绝写**（空底稿 + 整体替换那个键 =
    一次 ``/heartflow off`` 把 ``replyThreshold`` 一起抹掉，见模块 docstring）。
    内存覆盖**并入**底稿（覆盖优先，与 ``resolve()`` 的 ③ 层同序）—— 只存在于覆盖里的
    同级键不该在写回时被丢掉（旧实现正是靠"返空就退回读覆盖"补的这一条）。
    """
    raw = _account_file_cfg(account_id)
    if raw is None:
        return None
    base = raw.get(feature)
    cfg = dict(base) if isinstance(base, dict) else {}
    ov = overrides.get_override(account_id).get(feature)
    if isinstance(ov, dict):
        cfg = {**cfg, **ov}
    return cfg


def _flag_state(account_id: str, key: str) -> bool | None:
    """读一个**标量**开关键（``xiaoweiEnabled`` 这类）的当前值。

    读不出来返回 ``None`` —— 调用方必须说"读不到"，**不能**说成"没开"
    （同 ``tools/policy.py`` 的门禁口径：把"我读不到"说成"你没开"是撒谎）。
    覆盖优先，同 :func:`_feature_state` 的次序。
    """
    ov = overrides.get_override(account_id).get(key)
    if ov is not None:
        return bool(ov)
    raw = _account_file_cfg(account_id)
    if raw is None:
        return None
    return bool(raw.get(key))


def _handle_feature_toggle(account_id: str, feature: str, args: list[str],
                           *, adapter: Any = None) -> str:
    current = _feature_state(account_id, feature)

    if not args:
        return f"/{feature} 当前状态: {_state_word(current)}"
    action = args[0].lower()
    if action in ("on", "1", "true", "yes", "off", "0", "false", "no"):
        want = action in ("on", "1", "true", "yes")
        base = _feature_cfg(account_id, feature)
        if base is None:
            return UNREADABLE_CFG_MSG
        err = write_back(account_id, feature, {**base, "enabled": want}, adapter=adapter)
        return err or f"/{feature} 已{'开启' if want else '关闭'} ✅（已写入账号配置，当次生效）"
    if action == "status":
        if feature == "affection":
            extra = _affection_data_line(account_id)
        elif feature == "style":
            extra = _style_data_line(account_id)
        elif feature == "tts":
            extra = _tts_data_line(account_id)
        elif feature == "proactive":
            extra = _proactive_data_line(account_id)
        else:
            extra = ""
        return f"/{feature} 状态: {_state_word(current)}{extra}"
    return f"/{feature} 参数: on/off/status"


def _tts_data_line(account_id: str) -> str:
    """``/tts status`` 多报一行：provider / 自动语音概率（读不到就说读不到）。"""
    cfg = _safe_cfg(account_id).get("tts")
    if not isinstance(cfg, dict):
        return "\n配置: (读取失败，见日志)"
    provider = str(cfg.get("provider") or "").strip() or "Hermes 默认"
    try:
        auto = float(cfg.get("autoVoice") or 0)
    except (TypeError, ValueError):
        auto = 0.0
    return f"\n配置: provider={provider} / autoVoice={auto:.0%}"


def _proactive_data_line(account_id: str) -> str:
    """``/proactive status`` 多报一行：名单人数 / 静默阈值 / 每日上限。"""
    cfg = _safe_cfg(account_id).get("proactive")
    if not isinstance(cfg, dict):
        return "\n配置: (读取失败，见日志)"
    allow = cfg.get("allowFrom")
    n = len(allow) if isinstance(allow, (list, tuple)) else 0
    return (f"\n配置: 名单 {n} 人 / 静默 {cfg.get('minIdleHours', 20)}h"
            f" / 每日上限 {cfg.get('maxPerDay', 1)}")


def _handle_mute_command(account_id: str, args: list[str], *,
                         adapter: Any = None) -> str:
    """``/mute`` 的运维面：临时禁言一个群（内存态，重启失效）。

    为什么是**内存态**：这是"这个群现在太吵，先别回"的临时动作；写进账号文件后
    重启仍然生效，过几天没人记得为什么这个群不理人。要长期静音请用 ``/blacklist``。
    """
    import time as _time

    if adapter is None or not hasattr(adapter, "mute_group"):
        return "❌ 当前环境不支持 /mute（缺少 adapter）"
    if not args or args[0].lower() == "list":
        try:
            rows = adapter.muted_groups(account_id)
        except Exception as exc:  # noqa: BLE001 —— 状态命令不许抛
            log.debug("[WPP:%s] 读取禁言列表失败: %s", account_id, exc)
            return "/mute 列表读取失败（见日志）"
        if not rows:
            return "当前没有禁言中的群"
        lines = ["禁言中的群："]
        for chatroom, until in rows:
            lines.append(f"- {chatroom}（剩余 {max(0, int((until - _time.time()) // 60))} 分钟）")
        return "\n".join(lines)

    action = args[0].lower()
    if action in ("off", "unmute"):
        if len(args) < 2:
            return "用法: /mute off <群id>"
        try:
            existed = adapter.unmute_group(account_id, args[1])
        except Exception as exc:  # noqa: BLE001
            return f"❌ 解除禁言失败: {exc}"
        return (f"已解除 {args[1]} 的禁言 ✅" if existed
                else f"{args[1]} 本来就不在禁言列表里")

    chatroom = args[0]
    minutes = 30
    if len(args) > 1:
        try:
            minutes = int(args[1])
        except ValueError:
            return "用法: /mute <群id> [分钟数]"
    try:
        until = adapter.mute_group(account_id, chatroom, minutes)
    except Exception as exc:  # noqa: BLE001
        return f"❌ 禁言失败: {exc}"
    return (f"已禁言 {chatroom} {max(1, min(minutes, 7 * 24 * 60))} 分钟"
            f"（至 {_time.strftime('%m-%d %H:%M', _time.localtime(until))}）✅\n"
            "提示：内存态，网关重启即失效；长期静音请用 /blacklist。")


def _affection_data_line(account_id: str) -> str:
    """``/affection status`` 多报的一行：**真实落库计数**。

    TS **报不出这个数** —— 它的好感度/情绪在进程内的 ``Map`` 里，重启清零、数不出来。
    落库持久化（老板 2026-10-02 拍板）之后这行才有意义：它是"记账到底有没有在写"
    的免登录观测点 —— 少了它，好感度不涨时只能靠翻数据库才发现（而"开关开着、
    实现没接线"正是本轮要消灭的失败形态）。

    ⚠️ 读失败只回一句人话，**绝不让状态命令抛异常**（同 ``hf_commands`` 的约定：
    状态页报不出东西时也要能回一条消息）。
    """
    try:
        from ..data import repo_affection as repo

        groups = repo.count_groups_with_state(account_id)
        rows = repo.count_level_rows(account_id)
    except Exception as exc:  # noqa: BLE001 —— 状态查询失败不该让命令崩
        log.debug("[WPP:%s] 好感度计数读取失败: %s", account_id, exc)
        return "\n数据: (读取失败，见日志)"
    if not groups and not rows:
        return "\n数据: 暂无（好感度只在「过了触发判定④的群消息」上计分）"
    return f"\n数据: 有状态的群 {groups} 个 / 好感度行 {rows} 条"


def _style_data_line(account_id: str) -> str:
    """``/style status`` 多报的一行：库里真实存在的风格卡数（免登录观测点）。"""
    try:
        from ..data import repo_style

        n = repo_style.count_cards(account_id)
    except Exception as exc:  # noqa: BLE001 —— 状态查询失败不该让命令崩
        log.debug("[WPP:%s] 风格卡计数读取失败: %s", account_id, exc)
        return "\n数据: (读取失败，见日志)"
    return f"\n数据: 风格卡 {n} 张"


def _fmt_epoch(ts: Any) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _handle_style_command(account_id: str, args: list[str],
                          *, adapter: Any = None) -> str:
    """``/style`` 的运维面：开关走通用实现，list/show/rollback 读 ``repo_style``。"""
    action = args[0].lower() if args else ""
    if action == "list":
        return _style_list(account_id, args[1] if len(args) > 1 else "")
    if action == "show":
        if len(args) < 2:
            return "用法: /style show <群id>"
        return _style_show(account_id, args[1])
    if action == "rollback":
        if len(args) < 3:
            return "用法: /style rollback <群id> <版本>"
        return _style_rollback(account_id, args[1], args[2])
    return _handle_feature_toggle(account_id, "style", args, adapter=adapter)


def _style_list(account_id: str, group_id: str) -> str:
    from ..data import repo_style

    try:
        if group_id:
            rows = repo_style.list_versions(account_id, group_id, limit=10)
            if not rows:
                return f"📋 风格卡版本 ({group_id}):\n(无)"
            lines = [
                f"- v{r['version']} {r.get('reason') or 'generated'} "
                f"conf={float(r.get('confidence') or 0):.2f} "
                f"samples={r.get('sample_msgs') or 0} "
                f"{_fmt_epoch(r.get('generated_at'))}"
                for r in rows
            ]
            return f"📋 风格卡版本 ({group_id}):\n" + "\n".join(lines)
        rows = repo_style.list_cards(account_id)
    except Exception as exc:
        log.warning("[WPP:%s] 风格卡列表读取失败: %s", account_id, exc, exc_info=True)
        return "❌ 风格卡列表读取失败（详见日志）"
    if not rows:
        return "📋 风格卡 (0):\n(无)"
    lines = [
        f"- {r.get('group_id')}: v{r.get('version')} "
        f"conf={float(r.get('confidence') or 0):.2f} "
        f"samples={r.get('sample_msgs') or 0} "
        f"senders={r.get('sender_count') or 0} "
        f"{_fmt_epoch(r.get('generated_at'))}"
        for r in rows
    ]
    return f"📋 风格卡 ({len(rows)}):\n" + "\n".join(lines)


def _style_show(account_id: str, group_id: str) -> str:
    from ..data import repo_style
    from ..inbound import style_learn

    try:
        row = repo_style.get_card(account_id, group_id)
        if row is None:
            return f"❌ 没有群 {group_id} 的风格卡"
        card = style_learn.parse_style_row(row)
        if card is None:
            return f"❌ 群 {group_id} 的风格卡 JSON 已损坏（v{row.get('version')}）"
        cfg = style_learn.resolve_style_cfg(_safe_cfg(account_id).get("style"))
        body = style_learn.render_style_card(card, cfg.max_prompt_chars)
    except Exception as exc:
        log.warning("[WPP:%s] 风格卡详情读取失败: %s", account_id, exc, exc_info=True)
        return "❌ 风格卡读取失败（详见日志）"
    return (f"📋 群 {group_id} 风格卡 v{row.get('version')} "
            f"(conf={card.confidence:.2f}, samples={row.get('sample_msgs') or 0}, "
            f"{_fmt_epoch(row.get('generated_at'))}):\n{body or '(空)'}")


def _style_rollback(account_id: str, group_id: str, version_raw: str) -> str:
    from ..data import repo_style
    from ..inbound import style_learn

    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        return f"❌ 版本号不是整数: {version_raw}"
    try:
        new_version = repo_style.rollback_card(account_id, group_id, version)
    except Exception as exc:
        log.warning("[WPP:%s] 风格卡回滚失败: %s", account_id, exc, exc_info=True)
        return "❌ 回滚失败（详见日志）"
    if new_version is None:
        return f"❌ 找不到群 {group_id} 的 v{version}（/style list {group_id} 看历史）"
    # 回滚落库后立即刷新本进程缓存；失败不改变"已落库"的事实，下一轮 sweep 会补上。
    try:
        cfg = style_learn.resolve_style_cfg(_safe_cfg(account_id).get("style"))
        style_learn.load_style_cards(account_id, cfg.max_prompt_chars)
    except Exception as exc:  # noqa: BLE001
        log.warning("[WPP:%s] 回滚后缓存刷新失败（下轮 sweep 会补）: %s",
                    account_id, exc)
    return (f"✅ 已将 {group_id} 回滚到 v{version} 的内容，新版本 v{new_version}"
            f"（已落库，当次生效；版本号只增不减）")


def _handle_allowlist(account_id: str, kind: str, args: list[str],
                      *, adapter: Any = None) -> str:
    """``/user`` ``/group`` ``/blacklist`` 的**共用**实现（批量 add/del/list）。

    口径对齐 TS ``handleWhitelistCommand``（``src/index.ts:306-369``）：
    ① 目标**批量**（``args[1:]``）；② 空 action 且无目标 = list；
    ③ 回话只列**本次真的变了**的那些，没变就说「(均已存在)」/「(无)」；
    ④ list 印成 ``📋 <label> (n):`` + 每行 ``- x``；⑤ 三个域一张用法行。

    ⚠️ 两处**有意不齐**（都写在下面注释里）：① 目标在入口**去重**；② ``del`` **全删**
    （TS 的 ``removeAllowFrom`` 用 ``indexOf`` 只删第一个 —— 黑名单那半又全删）。
    ⚠️ 回话在**真的写了**的时候多一句「（已写入账号配置，当次生效）」：与 W6 之后本模块
    其余命令同一个口径（命令必须说清它到底改没改盘上的东西）；没写就一个字都不多。
    """
    key = _ALLOWLIST_KEYS[kind]
    label = _ALLOWLIST_LABELS[kind]
    usage = f"用法: /{kind} add <ID> [ID...] | /{kind} del <ID> [ID...] | /{kind} list"

    action = args[0].lower() if args else ""
    #: 去重保序：TS 的 append/update 各自幂等（``config.ts:436`` / ``:479`` / ``:769``），
    #: 但回话按 ``targets`` 原样拼 ⇒ 同一个 ID 写两遍会印成「x, x」。这里入口去重，
    #: 落盘与回话都只有一个（回话是老板读的，重复项是噪声）。
    targets = list(dict.fromkeys(args[1:]))

    if action == "list" or (not action and not targets):
        items = _allowlist_items(account_id, key)
        if items is None:
            return UNREADABLE_CFG_MSG
        body = "\n".join(f"- {x}" for x in items) if items else "(空)"
        return f"📋 {label} ({len(items)}):\n{body}"

    # 全员群同步要打厂商接口 ⇒ 走 :func:`handle_command_async`（网关路径）。
    if action == "sync" and kind == "user":
        return "全员群同步需要异步执行：请通过网关发送 /user sync（或 /whitelist sync）"

    if action in ("add", "del") and targets:
        items = _allowlist_items(account_id, key)
        if items is None:
            return UNREADABLE_CFG_MSG
        if action == "add":
            changed = [t for t in targets if t not in items]
            new_items = [*items, *changed]
            tail = f"已加入{label}: {', '.join(changed) or '(均已存在)'}"
        else:
            changed = [t for t in targets if t in items]
            gone = {t for t in changed}
            new_items = [x for x in items if x not in gone]
            tail = f"已移出{label}: {', '.join(changed) or '(无)'}"
        if new_items != items:
            err = write_back(account_id, key, new_items, adapter=adapter)
            if err:
                return err
            return f"✅ {tail}（已写入账号配置，当次生效）"
        # 没变就**不写**：TS 那边即使无变化也会逐个调一次 append（各自幂等 no-op），
        # 而 py2 每次写回都要备份 + 原子替换 + 重载 ⇒ 一次没意义的写只多留一份备份。
        return f"✅ {tail}"

    return usage


def _employee_group_ids(account_id: str, adapter: Any = None) -> list[str]:
    """当前账号的全员群 id（适配器在跑时每轮现读；否则读盘上那份）。"""
    if adapter is not None:
        fn = getattr(adapter, "_employee_group_ids", None)
        if callable(fn):
            try:
                return list(fn(account_id) or [])
            except Exception as exc:  # noqa: BLE001 —— 状态命令不许抛
                log.debug("[WPP:%s] 读全员群配置失败: %s", account_id, exc)
    from ..inbound import employee_sync

    raw = _account_file_cfg(account_id) or {}
    return employee_sync.group_ids_from_raw(raw)


def _handle_whitelist(account_id: str, args: list[str], *, adapter: Any = None) -> str:
    """``/whitelist status``（``/outbound`` 同义）：统一白名单总览。

    2026-10-19 老板拍板后**没有独立的出站名单**：``allowFrom`` / ``groupAllowFrom``
    / ``adminUsers`` 同时管收发，``WECHATPRO_SEND_ALLOWLIST`` 只是额外补充。
    这里只印**来源与计数**；明细用 ``/user list`` ``/group list``。
    """
    action = args[0].lower() if args else "status"
    if action not in ("status", "list"):
        if action == "sync":
            return "全员群同步需要异步执行：请通过网关发送 /whitelist sync"
        return ("用法: /whitelist status | /whitelist sync\n"
                "（/outbound 是 /whitelist 的别名；出站已并入统一白名单）")

    allow = _allowlist_items(account_id, "allowFrom")
    groups = _allowlist_items(account_id, "groupAllowFrom")
    if allow is None or groups is None:
        return UNREADABLE_CFG_MSG
    raw = _account_file_cfg(account_id) or {}
    admins = [str(x) for x in (raw.get("adminUsers") or []) if str(x).strip()]
    auto = [str(x) for x in (raw.get("employeeAllowFrom") or []) if str(x).strip()]
    group_ids = _employee_group_ids(account_id, adapter)

    profile_home = getattr(adapter, "_profile_home", None) if adapter is not None else None
    from ..core import netguard

    env_extra = netguard.env_allowlist(profile_home)
    # 用盘上这份算生效集合，避免 `adapter.account()` 解析失败时状态页空掉。
    view = type("_WhitelistView", (), {
        "allow_from": tuple(allow),
        "group_allow_from": tuple(groups),
        "admin_users": tuple(admins),
    })()
    effective = netguard.unified_allowlist(profile_home=profile_home, accounts=[view])

    lines = [
        "📋 统一白名单（收发同一份）",
        f"私聊 allowFrom: {len(allow)} 人（全员群自动同步 {len(auto)} 人）",
        f"群 groupAllowFrom: {len(groups)} 个",
        f"管理员 adminUsers: {len(admins)} 人",
        f"全员群: {', '.join(group_ids) if group_ids else '(未配置 employeeGroupIds)'}",
    ]
    state = None
    if adapter is not None:
        try:
            state = (getattr(adapter, "_employee_sync_state", {}) or {}).get(account_id)
        except Exception:  # noqa: BLE001
            state = None
    if state:
        import time as _time

        when = _time.strftime("%m-%d %H:%M", _time.localtime(int(state.get("at") or 0)))
        ok = "成功" if state.get("ok") else f"失败（{state.get('error') or '见日志'}）"
        lines.append(f"最近同步: {when} {ok}，成员 {state.get('members', 0)} 人")
    lines.extend([
        f".env 额外条目: {len(env_extra)} 条（WECHATPRO_SEND_ALLOWLIST，可留空）",
        f"出站生效目标合计: {len(effective)} 个",
        "明细: /user list · /group list",
    ])
    return "\n".join(lines)


async def _sync_employees(account_id: str, *, adapter: Any = None) -> str:
    """``/user sync`` / ``/whitelist sync``：拉全员群成员 → ``allowFrom``。

    同步失败**整轮不写**（见 ``inbound/employee_sync`` 的失败处理）：宁可这一轮
    不同步，也不能因厂商抖动把员工误删出名单。
    """
    from ..inbound import employee_sync

    if adapter is None:
        return "❌ 当前环境没有适配器，无法拉取厂商群成员"
    client = (getattr(adapter, "_clients", {}) or {}).get(account_id)
    if client is None:
        return "❌ 账号未连接，无法拉取全员群成员（重连后再试）"
    group_ids = _employee_group_ids(account_id, adapter)
    if not group_ids:
        return ("❌ 未配置全员群：请在账号 JSON 里加 "
                "\"employeeGroupIds\": [\"<全员群id>@chatroom\"]，重连后再试")
    try:
        res = await employee_sync.sync_once(
            client, account_id, group_ids, adapter=adapter)
    except Exception as exc:
        log.warning("[WPP:%s] 全员群同步命令失败: %s", account_id, exc, exc_info=True)
        return f"❌ 全员群同步失败（本轮未改动任何名单）：{exc.__class__.__name__}（详见日志）"
    if not res.get("ok"):
        return f"❌ 全员群同步失败（本轮未改动任何名单）：{res.get('error') or '未知原因'}"
    if not res.get("changed"):
        return f"✅ 全员群同步完成：成员 {res.get('members', 0)} 人，名单无变化"
    return (f"✅ 全员群同步完成：成员 {res.get('members', 0)} 人，"
            f"新增 {res.get('added', 0)}，移除 {res.get('removed', 0)}"
            f"（已写入 allowFrom，收发同时生效）")


def _handle_xiaowei(account_id: str, args: list[str], *, adapter: Any = None) -> str:
    """``/xiaowei on|off|status``：小微智能体工具开关（TS ``src/index.ts:249-270``）。

    ⚠️ 这个键的唯一消费者是 ``tools/policy.py`` 的 ``XIAOWEI_GATE`` —— 10 个
    ``wpp_xw_*`` 工具在**每次调用**时读它（``make_tool_gate`` 的 ``pre``），关着就拒。
    两边是**同一个开关的两半**：只加命令不加门禁 = 谎开关；只加门禁不加命令 =
    一个改不动的门。本项（W7-5）把两半一起落。

    回话逐字照抄 TS：status 只有「小微智能体: ✅ 开启 / ❌ 关闭」，成功带 ``account=``，
    其余情况回用法。**读不出来时报'读不到'**而不是"关闭"（见 :func:`_flag_state`
    与 :func:`state_label`）—— 那是与门禁**同一口径**的第三态，不是"没开"。
    """
    action = args[0].lower() if args else ""
    if action == "status":
        #: 与门禁**同一口径**（``policy.raw_account_cfg`` 读不到就说读不到）：状态页说
        #: 「❌ 关闭」而门禁回「读不到账号配置」时，两个面在描述同一个开关却说两套话。
        return f"小微智能体: {state_label(_flag_state(account_id, 'xiaoweiEnabled'))}"
    if action not in ("on", "off"):
        return "用法: /xiaowei on|off|status\n示例: /xiaowei on (开启小微智能体)"
    want = action == "on"
    #: 写**标量**（不是 dict）：门禁读的是 ``raw.get("xiaoweiEnabled")``，
    #: 写成 ``{"enabled": …}`` 会恒真（非空 dict 是真值）⇒ 门永远开着。
    err = write_back(account_id, "xiaoweiEnabled", want, adapter=adapter)
    return err or f"✅ 小微智能体已{'开启' if want else '关闭'} (account={account_id})"
