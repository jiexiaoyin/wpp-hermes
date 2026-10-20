"""WeChatPadPro 平台适配器 —— Hermes ``BasePlatformAdapter`` 的实现。

旧 ``adapter.py``（1900 行）的功能等价物，但按 Hermes 0.21.5 的**真实契约**重写。
本模块是 ``registration._register_platform`` 的 ``adapter_factory`` 目标。

## 实测契约（每条都在 真机 的 Hermes 源码里核过，不是照着文档猜的）

============================  ====================================================
基类**只有 4 个** ``@abstractmethod``  ``connect`` / ``disconnect`` / ``send`` / ``get_chat_info``
``adapter_factory`` 签名        ``Callable[[PlatformConfig], adapter]``（**单参**）
``Platform("wechatpadpro")``    靠 ``Platform._missing_`` 在 **register_platform 之后**才成立
``SendResult``                  dataclass，``success`` 必填，其余有默认
``MessageEvent``                只有 ``text`` 必填；``MessageType`` 9 个取值
``MessageDeduplicator``         ``is_duplicate/contains/discard/clear/absorb``
============================  ====================================================

## 五个**极易踩**的坑（旧实现全踩了）

### ① ``connect()`` 返回 ``False`` 会让 runner 每 300s 重试一次，**永远**

``GatewayRunner._platform_reconnect_watcher``（``run_adapters.py:699``）退避
30s→300s，**每次重试新建一个 adapter**。所以：

- **永久性失败**（没账号 / 凭据无效 / 生产 bot 被拒）⇒ 必须
  ``self._set_fatal_error(code, msg, retryable=False)``，让 runner 停止重试；
- **瞬时失败**（网络断）⇒ 才返回 ``False`` 交给 runner 重试。

旧实现的 ``_is_connected`` 恒 False + ``connect()`` 返回 False = 每 300 秒泄漏
约 2 个 fd 且永远重试（这解释了 真机 上"平台永不启用"里的资源抖动）。

### ② ``MAX_MESSAGE_LENGTH`` **不在基类上**

``max_message_length_for_chat()`` 用 ``getattr(self, "MAX_MESSAGE_LENGTH", 4096)``。
它和 registry 的 ``PlatformEntry.max_message_length``（只管 standalone/cron 分片）
是**两个东西** —— 本类里同时设，且都取 :data:`outbound.MAX_MESSAGE_LENGTH`。

### ③ 去重器**必须**是 ``MessageDeduplicator`` 实例且挂在**命名属性**上

runner 的 ``inbound_dedup_caches()`` 用 ``isinstance(..., MessageDeduplicator)``
找它，重连时用 ``carry_inbound_dedup`` 重灌。自己写一个 set/list
在重连后**会变空** ⇒ vendor 重放的消息被**二次回答**（微信里撤不回）。

### ④ 入站必须 ``await self.handle_message(event)``

``self._message_handler`` 由 runner 通过 ``set_message_handler`` 注入，
直接调它会**绕过**命令强制转换 / ``_drop_unresolved`` / 会话键控 / 忙碌-中断路由。

### ⑤ 凭据**绝不**用 ``os.getenv``

``gateway.multiplex_profiles`` 下进程 env 是 **default profile 的** ⇒ 多 profile
会串号。走 :mod:`core.accounts`（内部经 ``_shared.get_scoped_secret`` 口径）。

## 与 runner 的分工

runner 管「整个平台掉线」的重连（见 ①）；:mod:`platform.inbound_ws` 只管
「连接活着时 WS 断了」。两者**不要**互相实现。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

# ── Hermes 侧（插件只在 Hermes 进程内被 import，这些必然可用）
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from ..core import access_policy, filehelper_auth, metrics
from ..core import accounts as accounts_mod
from ..core.api_client import WppApiError, WppClient
from ..core.sync_fallback import REASON_OPEN, REASON_PUSH, SyncFallback
from ..inbound import (
    FILEHELPER,
    MT_REVOKE,
    MT_SYSTEM,
    InboundPipeline,
    WppMessage,
    is_bot_mentioned_by_text,
    is_filehelper_session,
    is_quoting_bot,
    jargon_mine,
    quote_ref,
    should_drop,
)
from ..inbound import jargon as jargon_mod
from ..inbound.affection import build_mood_system_prompt, process_affection_message, resolve_affection_cfg
from ..inbound.debounce import InboundDebouncer
from ..inbound.hongbao import is_card_message, process_red_packet
from ..inbound.intent import (
    IntentLlmConfig,
    build_group_context_lines,
    classify_group_intent,
    resolve_embedding_creds,
    resolve_intent_llm_cfg,
    resolve_intent_window,
    select_intent_context,
)
from ..inbound.media_oss import (
    KIND_FILES,
    KIND_IMAGES,
    KIND_VIDEOS,
    KIND_VOICES,
)
from ..inbound.persist import db_chat_id, db_peer_kind, persist_inbound
from ..inbound.rate_limit import TriggerRateLimiter
from ..inbound.relay import apply_relay_render
from . import outbound
from .adapter_media import MediaMixin
from .adapter_webhook import WebhookMixin
from .inbound_webhook import WppWebhookServer
from .inbound_ws import WppWsClient

log = logging.getLogger(__name__)

#: 群成员变动事件的**同群节流窗口（秒）**。vendor 对一次成员变动可能连推多条
#: ``msgType=10000``（进群 + 改群名各一条），不节流会把同一份名单拉两遍。
_MEMBER_EVENT_THROTTLE_SEC = 30.0


def _fmt_members(items) -> str:
    """``[(wxid, 昵称)]`` → ``接居堂(wxid_x)、周州(wxid_y)``；空则 ``无``。"""
    if not items:
        return "无"
    return "、".join(f"{nick or '?'}({wxid})" for wxid, nick in items)

#: 平台名 —— 必须与目录名 / ``config.yaml plugins.enabled`` 的键 / toolset 名**三者一致**
PLATFORM_NAME = "wechatpadpro"

#: 未指定账号时的兜底
DEFAULT_ACCOUNT = "default"

#: 单条消息上限（**与 registry 的 max_message_length 同值**，见模块 docstring ②）
MAX_MESSAGE_LENGTH = outbound.MAX_MESSAGE_LENGTH

#: 去重窗口：vendor 会重放（WS 断线重连 / webhook 补推）⇒ 5 分钟足够
_DEDUP_TTL_SECONDS = 300.0
_DEDUP_MAX_SIZE = 5000

# 丢弃判据**不在这里**：reason 的三分类（allow / heartflow / deny）与判据本体
# 在 ``inbound.triggers``（``should_drop`` / ``reason_class`` / 三张表）。
# 本模块原来自带一个枚举白名单 ``_DROP_REASONS``，失效方向是反的 ——
# 新增 deny reason 忘了登记，那条消息就会被喂给 LLM（审阅 A8）。判据搬过去后
# 默认后果变成「丢」，且**词表与返回 reason 的地方只隔一个文件**，不容易再分家。

#: 当前进程内的适配器实例（工具侧经 :func:`_get_adapter` 取用）
_CURRENT_ADAPTER: WppAdapter | None = None


#: 已告警过「未配 webhookSecret」的账号 —— 进程级去重（重连会反复调 _start_webhook）
# 2026-10-19 已随 _start_webhook 搬到 adapter_webhook.py

def _resolve_platform() -> Platform:
    """取 ``Platform("wechatpadpro")``。

    正常路径：``registration._register_platform`` 已 ``register_platform`` 过 ⇒
    ``Platform._missing_`` 认得它。测试/直构路径下补一个 pseudo member，
    **绝不静默失败**（旧实现的静默吞异常正是 F1 症状之一）。
    """
    try:
        return Platform(PLATFORM_NAME)
    except ValueError:
        log.warning("[WPP] Platform(%r) 未注册 —— 手动补 pseudo member（仅测试/直构路径）", PLATFORM_NAME)
        return Platform._add_pseudo_member(PLATFORM_NAME)


def _msg_type_to_event_type(msg: WppMessage) -> MessageType:
    """WPP ``msg_type`` → Hermes :class:`MessageType`。

    ``MT_APP``(49) 归 TEXT —— 卡片正文已被厂商译成文字摘要（红包→"微信红包"、
    接龙→列表文本），对 agent 就是文本。
    """
    from ..inbound.parser import (
        MT_CARD,
        MT_EMOJI,
        MT_IMAGE,
        MT_LOCATION,
        MT_VIDEO,
        MT_VOICE,
    )

    return {
        MT_IMAGE: MessageType.PHOTO,
        MT_VOICE: MessageType.VOICE,
        MT_VIDEO: MessageType.VIDEO,
        MT_EMOJI: MessageType.STICKER,
        MT_CARD: MessageType.DOCUMENT,
        MT_LOCATION: MessageType.LOCATION,
    }.get(int(msg.msg_type or 0), MessageType.TEXT)


#: W7-1：enrich 行里"OSS 目录 → 中文标记"。**逐字**对齐 TS ``handler.ts``
#: （图片 :232 / 视频 :315 / 语音 :372 / 文件 :392）—— 这四个串是**下游的解析口径**
#: （心流 ``extract_hf_image_urls`` 只认 ``[图片] <http(s) 直链>``、``[转写]`` 同行、
#: 引用、``_media_tag``），改它等于改对外协议。
_OSS_KIND_TAG: dict[str, str] = {
    KIND_IMAGES: "图片",
    KIND_VIDEOS: "视频",
    KIND_VOICES: "语音",
    KIND_FILES: "文件",
}


def _media_enrich_line(kind: str, url: str, *, filename: str = "", size: int = 0) -> str:
    """构造 TS 那条 enrich 行（不含前导 ``\\n``）。

    文件那行带名字与字节数（``[文件] name (1234 bytes) url``），其余三行只有 URL。
    ``size`` 传的是**我们真正传上去的字节数**（``len(data)``），不是厂商声明的
    ``data_len`` —— 下载被截断时声明值会说谎，而这一行是给模型看的"这文件多大"。
    """
    if kind == KIND_FILES:
        return f"[文件] {filename} ({size} bytes) {url}"
    return f"[{_OSS_KIND_TAG[kind]}] {url}"


def _inject_media_enrich_line(msg: WppMessage, kind: str, url: str, *,
                              filename: str = "", size: int = 0) -> None:
    """把 enrich 行追加进 ``msg.content``，并记下 ``oss_url`` / ``oss_kind`` 两个键。

    ⚠️ **两个键只在真拿到 URL 之后才写**：``InboundPipeline._media_tag`` 与
    ``prepare_text`` 都靠 ``oss_url`` 判断"正文里已经有那一行标记了"。写早了会变成
    **两处都没有标记**（前缀那条被抑制、这行又没写）—— 比重复标记更坏，模型会完全
    不知道发来的是张图。所以"写 URL"与"写键"是**同一个动作**，这里不给调用方
    只做一半的入口（与 W6 的 ``write_account_key`` 同款）。

    **幂等**：同一条消息被取两次媒体时不追加第二次 —— 正文里两行一样的
    ``[图片] url`` 会让模型以为发了两张图。生产路径只取一次（``_handle_one`` ④c）。
    """
    msg.media["oss_url"] = url
    msg.media["oss_kind"] = kind
    line = _media_enrich_line(kind, url, filename=filename, size=size)
    if line in msg.content:
        return
    # TS 是**追加**、且带前导 ``\n``：``m.content = \`${m.content}\n[图片] ${url}\``
    # ⇒ 一条纯图片消息的正文在 TS 里就是 ``"\n[图片] url"``。逐字保留这个形状
    # （``_media_tag`` 那套是**前缀**，两条路别混）。
    msg.content = f"{msg.content}\n{line}"


async def _persist_outbound_record(acct: accounts_mod.Account, peer_id: str,
                                   content: str,
                                   ids: outbound.OutboundMsgIds,
                                   msg_type: str | None = None,
                                   raw_payload: Any = None) -> None:
    """落库一条出站记录（live ``send()`` 与 standalone/cron **共用**）。

    ⚠️ live 路径（``_send_chunk``）一直会落库；cron 的 ``_standalone_send``
    在 2026-10-18 之前**漏了这一步** ⇒ 定时投递在 ``wpp_messages`` 里完全
    隐形（投递结果只存在于日志）。两条发送路径必须写同一套映射
    （``db_peer_kind`` / ``db_chat_id``），否则"按群查历史"会在其中一条路上
    看不见消息。

    失败只 warn —— 审计写不进去不该让已发出的消息算发送失败。

    ⚠️ ``repo_messages.save_message`` 是**同步 pymysql**：这条函数在 async 发送
    路径上，直接调用会把整个事件循环卡在一次 DB 往返上（发送高峰时所有账号一起
    停顿）。⇒ 走 ``asyncio.to_thread``（与入站 ``persist_inbound`` 同一做法）。
    """
    try:
        import asyncio

        from ..data import repo_messages

        is_group = outbound.is_group_peer(peer_id)
        await asyncio.to_thread(
            repo_messages.save_message,
            acct.id,
            direction="outbound",
            # 消息层口径写 "dm"，由 db_peer_kind 映射到 ENUM 的 'direct'
            # （与 live 路径同款，见 ``_persist_outbound`` 的注释）。
            peer_kind=db_peer_kind("group" if is_group else "dm"),
            peer_id=peer_id,
            chat_id=db_chat_id(peer_id, is_group=is_group),
            msg_id=ids.new_msg_id or ids.msg_id or None,
            new_msg_id=ids.new_msg_id,
            msg_type=msg_type,
            content=content,
            raw_payload=raw_payload,
            ts=int(time.time()),
        )
        metrics.inc_outbound_persist(True)
    except Exception as exc:  # noqa: BLE001
        metrics.inc_outbound_persist(False)
        log.warning("[WPP:%s] 出站落库失败: %s", acct.id, exc)


class WppAdapter(MediaMixin, WebhookMixin, BasePlatformAdapter):
    """多账号 WeChatPadPro 适配器（一个实例管多个账号）。"""

    #: 见模块 docstring ②——基类靠 ``getattr`` 读它
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: Any, platform: Platform | None = None) -> None:
        super().__init__(config, platform if platform is not None else _resolve_platform())

        self._accounts: dict[str, accounts_mod.Account] = {}
        self._clients: dict[str, WppClient] = {}
        self._ws: dict[str, WppWsClient] = {}
        #: W5 · 每账号的增量兜底（``/Msg/Sync`` + HTTP 心跳），与 ``_ws`` 同生命周期起停。
        #: 它**不替代** WS 帧那条路：帧照旧直接解析，这里只补"断线窗口 + 只发通知的帧"。
        self._sync: dict[str, SyncFallback] = {}
        #: W7-6 · 每账号的**到达层聚合**（同 key 多条合一批）。它**只**决定"什么时候把
        #: 这批消息交出去"—— 交出去之后仍旧逐条走 ``_handle_one``，处理语义一字未动。
        self._debounce: dict[str, InboundDebouncer] = {}
        #: R1 · 硬触发限速闸（2026-10-03 两个机器人互相 @ 死循环的止血闸）。
        #: **进程级一个**（key 里带账号，见 ``inbound/rate_limit`` 的 INVARIANT 4），
        #: 因此不随账号 ``connect()/disconnect()`` 建停 —— 重连一次就清空安全网，
        #: 正好是那个回路想要的效果（重启进程自然会清）。
        self._trigger_limiter = TriggerRateLimiter()
        self._pipelines: dict[str, InboundPipeline] = {}
        self._webhook: WppWebhookServer | None = None
        #: 真正在本地 server 上注册了路由的账号 id。vendor 侧注册**只**对这批账号做 ——
        #: 否则端口/凭据不一致的账号会被推到厂商，vendor 往一个 404 打，两边日志都干净。
        self._webhook_accounts: set[str] = set()
        #: vendor webhook 自动注册的常驻任务（``autoSetWebhook``）。TS 在启动时
        #: 推三段式 webhook，失败则每 5 分钟重试 —— py2 的这两个配置键此前只解析、
        #: **没有任何消费者**（死键）。句柄必须随 ``disconnect()`` 取消。
        self._webhook_tasks: list[asyncio.Task] = []
        #: sweep 句柄 —— ``disconnect()`` **必须**逐个 cancel（否则 reload 累积孤儿任务，R9）
        self._sweep_timers: list[asyncio.Task] = []
        #: 群名同步句柄（``/Group/List`` → ``wpp_chatrooms``）。会话标题/群名显示依赖它；
        #: 与 sweep 一样必须随 disconnect 取消，否则重连累积轮询。
        self._name_tasks: list[asyncio.Task] = []
        #: 群成员变动事件的节流表（``chatroom_id -> 上次拉取时刻``）。
        #: vendor 一次成员变动可能连推多条 ``msgType=10000``，不节流就重复拉同一份名单。
        self._member_event_at: dict[str, float] = {}
        #: 全员群成员同步句柄（→ ``allowFrom``，收发共用）。同样必须随 disconnect 取消。
        self._employee_tasks: list[asyncio.Task] = []
        #: 全员群最近一次同步结果（给 ``/whitelist status`` 看）。
        self._employee_sync_state: dict[str, dict[str, Any]] = {}
        #: 出站白名单在 connect 时定版，热刷新（``/user add`` / 全员群同步）要用它
        #: 回到同一个 profile，而不是重新猜 ambient scope。
        self._profile_home: str | None = None
        #: W2 好感度记账的 fire-and-forget 句柄。**必须**持强引用：``asyncio`` 只对
        #: 任务持弱引用，"create_task 了就不管"的任务可能在跑完之前被 GC 掉 ——
        #: 现场只表现为"好感度有时不涨"（没有异常、没有日志、没有任何痕迹）。
        #: 由 :meth:`_affection_done` 在收尾时丢弃（不用列表，避免长跑进程里无限增长）。
        self._affection_tasks: set[asyncio.Task] = set()
        #: W7-4 黑话挖掘的 fire-and-forget 句柄。理由与 ``_affection_tasks`` **逐条相同**
        #: （强引用防 GC、由 :meth:`_jargon_done` 收尾丢弃）。挖掘一轮最多 16 次 LLM
        #: 调用（1 次批量验证 + 5 个候选 × 3 步推断），``await`` 它等于把一整个 LLM
        #: 往返串进收信链路 —— 对齐 TS 的 ``void mineJargonForGroup(...).catch(warn)``。
        self._jargon_tasks: set[asyncio.Task] = set()
        #: 已从 DB 预热过黑话含义的 ``(account_id, group_id)``。每组只读一次库；
        #: 预热是 fire-and-forget（不阻塞收信），首条消息来不及读到就从下一条开始生效。
        self._jargon_loaded: set[tuple[str, str]] = set()
        #: 昵称缓存（引用回复用），{account:wxid: (name, at)}。
        #: 只有 30 分钟 TTL、没有容量上限时，长跑进程会随联系人/群成员数量无限增长
        #: （2026-10-19 审阅）—— 到上限先清过期，再按插入顺序淘汰最老。
        self._name_cache: dict[str, tuple[str, float]] = {}
        self._name_cache_max = 2048
        #: 临时禁言（SpectreCore 的 ``/mute`` 轻量版）：``(account_id, chatroom_id)``
        #: → 到期时间（epoch 秒）。**内存态**，重启即失效 —— 主动静音是临时运维动作，
        #: 不该因为一次重启变成"这个群永远不理人"。过期条目在读取时顺手清掉。
        self._muted_groups: dict[tuple[str, str], float] = {}

        # ★ 坑③：去重器**必须**是 MessageDeduplicator 且挂命名属性 —— runner 靠
        #   isinstance 找它并在重连时 carry_inbound_dedup 重灌。自己写 set 会在重连后变空。
        self._dedup = MessageDeduplicator(max_size=_DEDUP_MAX_SIZE, ttl_seconds=_DEDUP_TTL_SECONDS)

        #: Hermes 多路复用用 ``token`` 做**冲突检测**（同一凭据被两个 profile 用会互相踢）。
        #: 取排序后第一个账号的凭据指纹 —— 确定性的，不含明文（见 core.accounts.fingerprint）。
        self.token = ""
        #: 出站白名单**在 connect 时捕获**（那时仍在 profile scope 内）。发送任务
        #: （cron / 后台协程）稍后可能已经掉出 scope；若那时重新读 ambient env，
        #: 会回落到根 ``.env`` —— 2026-10-19 07:36 私聊回复被拒两次的根因。
        #: 这是所有账号的**并集**（日志/兼容兜底）；真正注入各 client 的是下面那份。
        self._send_allowlist: tuple[str, ...] = ()
        #: **按账号**捕获的出站白名单：A 账号的 allowFrom 不能替 B 账号开门
        #: （2026-10-19 审阅修复；之前全局并集，多账号下互相放行）。
        self._send_allowlists: dict[str, tuple[str, ...]] = {}

    # ================================================================== 属性

    @property
    def accounts(self) -> dict[str, accounts_mod.Account]:
        """当前已加载账号（只读视图）。"""
        return dict(self._accounts)

    def get_cross_account_context(self) -> list[dict]:
        """跨账号探查（``wpp_accounts_context`` 工具用）。

        **只返回元信息**，绝不含 authcode / tokenKey / webhook token（R1 铁律）。
        字段与 TS/py1 的 ``get_cross_account_context`` 同名同义。
        """
        out: list[dict] = []
        for aid, acct in sorted(self._accounts.items()):
            out.append({
                "account_id": aid,
                "agent": acct.agent,
                "self_wxid": acct.self_wxid,
                "nickname": acct.nickname,
                "enabled": bool(acct.enabled),
                "ws_connected": aid in self._ws,
                "client_ready": aid in self._clients,
                "admin_users": list(acct.admin_users or acct.allow_from),
            })
        return out

    def authorize_account(self, account_id: str,
                          caller_wxid: str | None = None) -> tuple[bool, str]:
        """跨账号权限校验（账号互相隔离的最后一道防线）。

        规则（与 py1 Phase 4.3 一致）：

        1. 目标账号不存在 ⇒ 拒绝；
        2. 无 caller（内部链路：cron / standalone / 工具面探针）⇒ 允许；
        3. caller 在目标账号 ``adminUsers``/``allowFrom`` 白名单里，或就是该账号
           自己的 ``selfWxid`` ⇒ 允许；
        4. 否则拒绝。

        ⚠️ 入站路径上 ``caller_wxid`` 已由 :mod:`core.inbound_context` 覆盖为**真实
        发送者**，模型填的值到不了这里（见 ``toolkit.hermes_contract._payload``）。
        """
        acct = self._accounts.get(account_id)
        if acct is None:
            return False, (f"账号 {account_id!r} 不存在"
                           f"（可用账号: {sorted(self._accounts)}）")
        caller = str(caller_wxid or "").strip()
        if not caller:
            return True, ""
        allow = set(acct.admin_users or ()) | set(acct.allow_from or ())
        if caller in allow or (acct.self_wxid and caller == acct.self_wxid):
            return True, ""
        return False, (f"无权操作账号 {account_id!r}"
                       f"（caller {caller!r} 不在 adminUsers/allowFrom 白名单）")

    def reload_account(self, account_id: str) -> bool:
        """重新解析一个账号并替换缓存（W6：命令写回账号文件后靠它当次生效）。

        做两件事，**只做这两件**：

        1. 重解析 ``Account``（唯一解析点仍是 ``core.accounts.resolve``，与 ``connect()``
           同一份逻辑 + 同一份 ``extra.accounts``）→ 换掉 ``self._accounts[id]``。
           网关授权面 ``_gateway_accounts`` 读的就是它 ⇒ 白名单改动立刻生效。
        2. 刷新该账号管线上的 **cfg 快照**（``InboundPipeline.cfg`` 是 ``connect()`` 时
           拷的一份 dict，``should_process`` 判准入、``prepare_text`` 去 @ 都读它）。
           **就地改字段、不重建对象** —— 重建会把 ``seen`` 去重器一起丢掉（重连期间
           同一条消息会被二次处理）。

        **不**动的：client / WS / webhook（它们吃的是凭据与端点，而本插件的配置写回
        从不改凭据；改端点要重连才谈得上）、也**不连接/断开**账号
        （``enabled`` 的翻转要等下一次 ``connect()``）。

        解析失败（配置改坏了）只 warn 并返回 False —— 命令链路不能因为一次重载失败
        就崩；盘上那份也**照旧保留**（写回前已备份，运维能拿回来）。
        """
        extra_accounts = self._extra().get("accounts")
        extra = (extra_accounts or {}).get(account_id) if isinstance(extra_accounts, dict) else None
        try:
            acct = accounts_mod.resolve(account_id, extra if isinstance(extra, dict) else None)
        except Exception as exc:  # noqa: BLE001 —— 包括 AccountError；命令面不该因此炸
            log.warning("[WPP:%s] 重载账号配置失败（沿用内存里那份）: %s", account_id, exc)
            return False

        self._accounts[account_id] = acct
        pipeline = self._pipelines.get(account_id)
        if pipeline is not None:
            pipeline.cfg = self._pipeline_cfg(acct)
        # W7-6：聚合窗口也吃账号文件（``debounceMs``）—— TS 那边它同样是热加载字段
        # （``account-context.ts:102`` 的"运行时决策字段 …/debounce"）。**就地改属性、
        # 不重建聚合器**：重建会把已经缓冲的那批丢掉（那是真丢消息）。
        db = self._debounce.get(account_id)
        if db is not None:
            db.interval_ms = max(0, int(acct.debounce_ms))
            # 合并开关同样热生效（``merge_allowed`` 本来就读现账号；这里同步标志位）
            db.merge = bool(acct.debounce_merge.get("enabled"))
            db.merge_max_chars = acct.debounce_merge.get("maxChars") or 2000
        # ★ 白名单收发共用：``/user add|del`` 或全员群同步改完 allowFrom 后，
        #   出站那份**连接期捕获**的列表也必须一起刷新，否则「收得到、发不出」
        #   的割裂会一直留到下一次重连。
        self.refresh_outbound_allowlist()
        log.info("[WPP:%s] 账号配置已重载（enabled=%s, 私聊白名单=%d, 群白名单=%d, 黑名单=%d）",
                 account_id, acct.enabled, len(acct.allow_from),
                 len(acct.group_allow_from), len(acct.blacklist_groups))
        return True

    def _allowlist_for(self, acct: accounts_mod.Account) -> tuple[str, ...]:
        """取某账号的出站白名单（未捕获时按**该账号**现算，绝不用全局并集兜底）。"""
        cached = getattr(self, "_send_allowlists", {}).get(acct.id)
        if cached is not None:
            return cached
        return outbound.netguard.unified_allowlist(
            profile_home=getattr(self, "_profile_home", None), accounts=(acct,))

    def refresh_outbound_allowlist(self) -> tuple[str, ...]:
        """重算**每账号**统一白名单并推给对应 client。

        只算**已连接**账号（``_clients`` 里的）：未连接的账号不该替一个不存在的
        连接开门。没有任何已连接账号时退回全部账号（单测/工具态）。

        2026-10-19 审阅修复：以前这里算的是所有账号的并集再注入每个 client ⇒
        A 账号的 allowFrom 能替 B 账号放行（多账号下越权）。现在按账号各算一份；
        返回值仍是并集（日志/兼容调用方）。
        """
        clients = getattr(self, "_clients", {}) or {}
        accounts = getattr(self, "_accounts", {}) or {}
        live = [a for aid, a in accounts.items() if aid in clients]
        targets = live or list(accounts.values())
        per_account = {
            a.id: outbound.netguard.unified_allowlist(
                profile_home=getattr(self, "_profile_home", None), accounts=(a,))
            for a in targets
        }
        self._send_allowlists = per_account
        union: list[str] = []
        for entries in per_account.values():
            for item in entries:
                if item not in union:
                    union.append(item)
        self._send_allowlist = tuple(union)
        for aid, client in clients.items():
            client.send_allowlist = per_account.get(aid, ())
        log.info("[WPP] 统一白名单已刷新: %d 个账号，并集 %d 条%s",
                 len(per_account), len(self._send_allowlist),
                 "（" + ", ".join(f"{aid}={len(v)}" for aid, v in per_account.items())
                 + "）" if per_account else "")
        return self._send_allowlist

    def account(self, account_id: str = "") -> accounts_mod.Account:
        """取账号；空 ⇒ 默认账号。取不到 raise（**不静默兜底**，否则会串号发到别人）。"""
        aid = account_id or DEFAULT_ACCOUNT
        acct = self._accounts.get(aid)
        if acct is None:
            raise ValueError(
                f"[WPP] 账号 {aid!r} 不存在（已加载：{sorted(self._accounts)}）")
        return acct

    # ------------------------------------------------ /mute（临时禁言，内存态）

    def mute_group(self, account_id: str, chatroom_id: str, minutes: int = 30) -> float:
        """禁言一个群 ``minutes`` 分钟（默认 30，上限 7 天）。返回到期 epoch 秒。"""
        minutes = max(1, min(int(minutes), 7 * 24 * 60))
        until = time.time() + minutes * 60
        self._muted_groups[(account_id, str(chatroom_id).strip())] = until
        return until

    def unmute_group(self, account_id: str, chatroom_id: str) -> bool:
        """解除禁言；返回是否本来就在禁言列表里。"""
        return self._muted_groups.pop((account_id, str(chatroom_id).strip()), None) is not None

    def group_muted(self, account_id: str, chatroom_id: str) -> bool:
        """该群现在是否被禁言（过期条目顺手清掉）。"""
        key = (account_id, str(chatroom_id).strip())
        until = self._muted_groups.get(key)
        if until is None:
            return False
        if until <= time.time():
            self._muted_groups.pop(key, None)
            return False
        return True

    def muted_groups(self, account_id: str) -> list[tuple[str, float]]:
        """列出该账号仍在禁言的群（群 id, 到期 epoch），顺带清过期项。"""
        now = time.time()
        out: list[tuple[str, float]] = []
        for (aid, chatroom), until in list(self._muted_groups.items()):
            if aid != account_id:
                continue
            if until <= now:
                self._muted_groups.pop((aid, chatroom), None)
                continue
            out.append((chatroom, until))
        return sorted(out)

    # ------------------------------------------------ 准入策略上报（Hermes 授权面）
    #
    # 网关在插件**之上**还有一道用户授权（``gateway/authz_mixin.py::_principal_authorized``），
    # **没有环境白名单时默认全拒**。这几个属性不上报，就会出现"插件判过了、网关给拒了" ——
    # 现场只有一行 ``WARNING gateway.run: Unauthorized user: <wxid>``，而插件侧
    # ``触发（…）`` 照打、台账照落。2026-10-01 真机实测就是这么被拦掉一条 judge=0.775 的回复。
    #
    # 声明 ``enforces_own_access_policy`` 之后，网关会**反过来问适配器**同名属性
    # （``authz_mixin.py::_adapter_setting``：先 ``getattr(adapter, attr)``，再退 config.extra）。
    # 中心授权只调这四个：``_group_policy`` / ``_dm_policy`` / ``_is_dm_allowed`` / 本开关。
    # （``_is_group_allowed`` 只有 whatsapp/qqbot 等在**自己适配器内部**用，中心不调，故不实现。）
    #
    # ⚠️ 别把 ``open`` 谎报成 ``allowlist`` 去绕开网关：网关对 ``open`` 的 default-deny
    #    是 SECURITY.md §2.6 的反 fail-open 设计。口径与理由见 ``core/access_policy.py``。

    @property
    def _gateway_accounts(self) -> tuple[accounts_mod.Account, ...]:
        """上报用的账号集 = **已连接**的那些（``_clients`` 里）。

        ⚠️ 不能用 ``self._accounts``：``connect()`` 先 ``load_all`` **全量**、再按
        ``refusal_reason`` 过滤出 connectable，所以 ``_accounts`` 里躺着连不上的账号。
        其中最典型的是 ``accounts/default.json`` —— 一份**生产形状**配置，
        ``groupAllowFrom`` 里是 7 个真实生产群。把它的白名单报给网关，
        等于替一个**根本连不上的**账号开门。
        """
        return access_policy.reportable(self._accounts.values(), self._clients.keys())

    @property
    def enforces_own_access_policy(self) -> bool:
        """声明"准入由插件按各账号配置自己把关" —— 网关据此才会回头问下面几个。"""
        return True

    @property
    def _group_policy(self) -> str:
        return access_policy.group_policy(self._gateway_accounts)

    @property
    def _dm_policy(self) -> str:
        return access_policy.dm_policy(self._gateway_accounts)

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """私聊按发件人判 —— 网关只在 ``_dm_policy == "allowlist"`` 时调到这里。"""
        return access_policy.is_dm_allowed(self._gateway_accounts, sender_id)

    # ============================================================ 连接生命周期

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """接入所有可连接账号：建 client → 起 WS → 起 webhook → 起 sweep。

        失败口径见模块 docstring ①：**永久性**失败走 ``_set_fatal_error(retryable=False)``，
        瞬时失败才让 runner 重试。
        """
        try:
            await asyncio.to_thread(self._run_migrations)  # 同步 pymysql（建连+DDL）⇒ 工作线程

            # 启动自述（v1.14 蓝本）：主端点 / 兜底三态 / 旧 env 残留 —— 全是 WARNING 级，
            # 让"换了端点但没生效""兜底配了半套""旧名还在"在第一眼就能分开。
            try:
                from ..llm import judge as _judge_startup
                _judge_startup.log_judge_config_once()
            except Exception:
                log.debug("[WPP] judge 启动自述失败", exc_info=True)

            self._accounts = accounts_mod.load_all(self._extra().get("accounts"))

            # ★ 统一白名单在 connect 时定版（此时仍在 profile runtime scope 内）。
            #   不能等到 send() 再读：后台/cron 发送任务可能没有 scope，届时
            #   platform_gate_env 会回落根 .env，把 wpp-wechat 的六条读成 default 的一条
            #   （2026-10-19 07:36 生产事故）。真正的放行集合要等 accounts 加载完
            #   才能算（它 = 各账号 allowFrom/groupAllowFrom/adminUsers ∪ env），见下。
            profile_home = None
            try:
                from agent.secret_scope import current_secret_scope_home
                profile_home = current_secret_scope_home()
            except Exception:  # noqa: BLE001 —— 插件在 Hermes 进程外时可无此模块
                profile_home = None
            if not profile_home and os.environ.get("HERMES_PROFILE"):
                try:
                    from hermes_constants import get_hermes_home
                    profile_home = get_hermes_home()
                except Exception:  # noqa: BLE001
                    profile_home = None
            self._profile_home = str(profile_home) if profile_home else None
            # 会话标题 hook 的兜底只许读本 profile 的 state.db（跨 profile 读库 = 越权）。
            try:
                from ..inbound import session_title as _session_titles
                _session_titles.set_profile_home(self._profile_home)
            except Exception as exc:  # noqa: BLE001 —— 标题只是展示层
                log.debug("[WPP] 登记 profile home（会话标题）失败: %s", exc)

            # 红线先炸，再谈"跳过"。两者都由 ``refusal_reason`` 返回字符串，但后果
            # 差着量级：未启用 / 无凭据是"这个账号先不连"（正常态，跳过就好），
            # 生产身份是"连上去会把生产踢下线 + 劫持它的 webhook"（**不可逆**）。
            # 旧实现把两者混在同一个列表推导里过滤 ⇒ 红线账号也只是一行 warn，
            # 插件照常起来、照常连别的账号，运维翻日志未必看到那行（审阅 C4）。
            #
            # ⚠️ 判据是 `enabled ∧ 有凭据` 的账号才判（见 ``assert_no_red_line``）：
            #    某台开发机上那份 ``default.json`` 把 ``selfWxid`` 写成生产号
            #    却 ``enabled=false`` —— 它连不上，不该让整个插件起不来。
            accounts_mod.assert_no_red_line(self._accounts.values())

            connectable = [a for a in self._accounts.values()
                           if accounts_mod.refusal_reason(a) is None]

            # ★ 2026-10-19 项目约定：白名单**不区分进出站**。出站放行集合就是
            #   入站那份（allowFrom / groupAllowFrom / adminUsers）∪ env 额外项。
            #   只算**可连接**的账号：被红线/未启用拒掉的账号不该替一个不存在的
            #   连接开门（同 access_policy.reportable 的口径）。
            #   ⚠️ **按账号各算一份**：多账号下并集会互相放行（审阅修复）。
            self._send_allowlists = {
                a.id: outbound.netguard.unified_allowlist(
                    profile_home=self._profile_home, accounts=(a,))
                for a in connectable
            }
            union: list[str] = []
            for entries in self._send_allowlists.values():
                for item in entries:
                    if item not in union:
                        union.append(item)
            self._send_allowlist = tuple(union)
            log.info("[WPP] 统一白名单已绑定: %s 条 (profile=%s)",
                     ", ".join(f"{aid}={len(v)}"
                               for aid, v in self._send_allowlists.items()) or "0",
                     self._profile_home or "-")

            if not connectable:
                refused = {a.id: accounts_mod.refusal_reason(a) for a in self._accounts.values()}
                msg = (f"没有可连接的账号（共加载 {len(self._accounts)} 个：{refused or '空'}）。"
                       f"检查 accounts/*.json 的凭据、enabled，以及生产身份红线的豁免 env。")
                log.error("[WPP] %s", msg)
                self._set_fatal_error("no_connectable_account", msg, retryable=False)
                return False

            # 凭据本身（**是明文 authcode，不是指纹**）—— 对齐 Hermes 的
            # ``PlatformConfig.token`` 约定（Telegram 的 bot token 同样明文放这儿），
            # 供多路复用冲突检测比对。确定性取「字典序第一个可连接账号」，
            # 不依赖 dict 插入顺序。
            #
            # ⚠️ 这里曾写着「凭据指纹 … **不含明文**」，与实现相反 —— 写注释时想当然。
            #    它**只写不读**（py2 与 Hermes 都不消费本属性），所以明文不出进程；
            #    但只要哪天有人把它打进日志，明文 authcode 就落盘了。别那么干。
            self.token = accounts_mod.credential_token(connectable)

            for acct in connectable:
                self._clients[acct.id] = WppClient(
                    base_url=acct.api_base_url, authcode=acct.authcode,
                    account_id=acct.id, timeout=30,
                    send_allowlist=self._allowlist_for(acct),
                )
                self._pipelines[acct.id] = InboundPipeline(acct.id, self._pipeline_cfg(acct))
                # W7-6：每账号一个到达层聚合器。窗口来自账号文件 ``debounceMs``
                # （``core.accounts`` 解析，默认 500ms —— 与 TS v1.3.74 之后的默认值同值）。
                # ``is_control_command`` 接 filehelper 会话：**本树唯一的控制面通道**，
                # 那些消息在 ``_maybe_command`` 里就被接管、从不进 LLM ⇒ 不进缓冲只是
                # 恢复它们的零延迟（TS 声明了这个钩子却没人传，见 ``inbound/debounce`` 的
                # 「与 TS 有意不齐」一节）。
                # ⚠️ 这里**不**做重复 connect 的守卫（不像 ``_start_ws`` 那个兜底）：
                #    本类的契约就是 connect() 前先 disconnect()，而 disconnect 会清 ``_debounce``。
                _merge_cfg = acct.debounce_merge
                self._debounce[acct.id] = InboundDebouncer(
                    acct.id,
                    lambda batch, a=acct.id: self._flush_inbound(a, batch),
                    interval_ms=acct.debounce_ms,
                    is_control_command=is_filehelper_session,
                    merge=bool(_merge_cfg.get("enabled")),
                    merge_allowed=lambda batch, a=acct.id: self._merge_allowed(a, batch),
                    merge_max_chars=_merge_cfg.get("maxChars") or 2000,
                )

            webhook_ok = self._start_webhook(connectable)
            if not webhook_ok:
                # webhook 起不来不是致命的（WS 仍是主通道），但要说清楚
                log.warning("[WPP] webhook 未启动（WS 通道不受影响）")
            else:
                # vendor 侧 webhook 自动注册（``autoSetWebhook``，默认 true）。必须排在
                # 监听起好之后：先有本地口，再告诉厂商往哪推。
                # 2026-10-19 审阅修复：本地监听 bind 失败时旧代码仍然把 URL 推给厂商
                # ⇒ 消息被推到死端口，表现为"webhook 配置成功但收不到消息"。只对
                # **真正注册了路由的账号**推送。
                self._start_vendor_webhooks(
                    [a for a in connectable if a.id in self._webhook_accounts])

            for acct in connectable:
                self._start_ws(acct)

            self._start_sweeps(connectable)
            self._start_name_sync(connectable)
            self._start_employee_sync(connectable)

            self._mark_connected()
            log.info("[WPP] 平台已连接：%d 个账号 %s（is_reconnect=%s）",
                     len(self._clients), sorted(self._clients), is_reconnect)
            return True

        except accounts_mod.AccountError as exc:
            # 配置错误（如 agent 字段误配）—— 重试无用
            log.error("[WPP] 账号配置错误，不再重试: %s", exc, exc_info=True)
            self._set_fatal_error("account_config", str(exc), retryable=False)
            return False
        except Exception as exc:
            log.error("[WPP] connect 失败（将交给 runner 重试）: %s", exc, exc_info=True)
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False

    async def disconnect(self) -> None:
        """停 WS / 兜底 / 聚合器 / webhook / sweep，关 client。**必须幂等**（runner 可能重入）。"""
        # ⓪ W7-6 聚合器**先 flush**：此刻缓冲区里可能躺着刚到的消息（窗口 500ms，
        #    绝大多数停机都撞得上），而它们的派发要用 client / 管线 / 心流 —— 下面
        #    每一步都在拆这些东西。**顺序反了就是停机丢消息**，且现场只表现为
        #    "有些人说话机器人没理"（无法归因）。flush 完再让它拒收（``stop`` 之后
        #    的 ``enqueue`` 返 False ⇒ 交回调用方直通，不静默丢）。
        for aid, db in list(self._debounce.items()):
            try:
                await db.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("[WPP:%s] 入站聚合器停机时出错（继续拆别的）: %s", aid, exc)
        self._debounce.clear()

        # ① sweep 停 —— 否则它会在 client 关闭后继续打 DB/网络（R9 孤儿任务）
        for task in self._sweep_timers:
            task.cancel()
        if self._sweep_timers:
            await asyncio.gather(*self._sweep_timers, return_exceptions=True)
        self._sweep_timers.clear()

        # ② W5 兜底 —— **必须早于 ③ 的 WS 与 ⑤ 的 client**：它的两个定时器（拉取 / HTTP
        #    心跳）都在打厂商，client 关了之后它们只会刷错误日志，还可能拖住关闭。
        for aid, sync in list(self._sync.items()):
            try:
                await sync.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP:%s] 增量兜底停止时忽略异常: %s", aid, exc)
        self._sync.clear()

        # ③ WS
        for aid, ws in list(self._ws.items()):
            try:
                await ws.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP:%s] WS 停止时忽略异常: %s", aid, exc)
        self._ws.clear()

        # ③b 群名同步 —— 早于 client 关闭取消，避免它在关闭后的 client 上打厂商。
        for task in self._name_tasks:
            task.cancel()
        if self._name_tasks:
            await asyncio.gather(*self._name_tasks, return_exceptions=True)
        self._name_tasks.clear()

        # ③c 全员群成员同步 —— 同样早于 client 关闭。
        for task in self._employee_tasks:
            task.cancel()
        if self._employee_tasks:
            await asyncio.gather(*self._employee_tasks, return_exceptions=True)
        self._employee_tasks.clear()

        # ④ webhook：先停自动注册任务，再停监听（否则重试任务会在 client 关闭后继续打厂商）
        for task in self._webhook_tasks:
            task.cancel()
        if self._webhook_tasks:
            await asyncio.gather(*self._webhook_tasks, return_exceptions=True)
        self._webhook_tasks.clear()

        if self._webhook is not None:
            self._webhook.stop()
            self._webhook = None
        self._webhook_accounts.clear()

        # ④b 好感度 / 黑话的 fire-and-forget 任务：它们各自带 DB 读改写与 LLM 外呼
        #     （黑话一轮最多 16 次）。不取消就会在 client/DB 都拆完之后继续跑，且每次
        #     重连再叠一批（句柄与旧 adapter 一起驻留到自然结束）。
        leftover = [*self._affection_tasks, *self._jargon_tasks]
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)
        self._affection_tasks.clear()
        self._jargon_tasks.clear()

        # ⑤ vendor client（旧实现会泄漏，这里显式关）
        for aid, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP:%s] client 关闭时忽略异常: %s", aid, exc)
        self._clients.clear()
        self._pipelines.clear()
        self._debounce.clear()

        self._mark_disconnected()
        log.info("[WPP] 平台已断开")

    # ---------------------------------------------------------------- 启动子步

    def _run_migrations(self) -> None:
        """跑 DB migration。**不阻断接入**（DB 坏了不该连微信都上不去）—— 但**必须吵**。

        ★ D7（2026-10-02 修）：旧写法整条链只有一行 ``warning``，而且**只有启动那一次**。
        之后进程照跑、``db.is_available()``（``SELECT 1``）照通，于是"schema 只建了一半"
        在运维面上与"一切正常"完全一样：心流每条都写不进台账，指标里 0 条发言看起来
        就像"群里没人说话" —— 与 09-13 judge 静默瘫、09-26 停摆 3 天是同一族。

        现在失败会同时落在三处：``error`` 日志（不是 warn）、``wpp_db_ready`` gauge
        （**不是** ``is_available`` 那个"连得上"）、以及 ``db.health()``（``/heartflow
        status`` 会打印）。埋点声明见 ``metrics.GAUGE_DB_READY``。
        """
        try:
            from ..data import db

            if not db.is_available():
                metrics.set_db_gauge(0)
                log.error("[WPP] DB 不可用（%s）—— 收信照常，**心流/审计全部降级**；"
                          "migration 没跑过，schema 状态未知", db.describe())
                return
            result = db.migrate()
            metrics.set_db_gauge(1)
            if result.get("drifted"):
                log.warning("[WPP] DB 就绪（**%d 处定义漂移，只报不改**: %s）: %s",
                            len(result["drifted"]), "; ".join(result["drifted"][:3]), result)
            else:
                log.info("[WPP] DB 就绪: %s", result)
        except Exception as exc:
            metrics.inc_db_migration_failure()
            metrics.set_db_gauge(0)
            log.error("[WPP] DB migration 未完成（不影响收信，**心流/审计降级**；"
                      "schema 可能是**半套**）: %s", exc, exc_info=True)

    def _start_ws(self, acct: accounts_mod.Account) -> None:
        """起单个账号的 WS 客户端 **+ W5 增量兜底**。WS 起不来不算致命（webhook 仍是通道）。"""
        ws: WppWsClient | None = None
        if not acct.ws_url:
            # ★ 不配 wsUrl 只意味着"没有实时通道"，**不等于**不要增量兜底。
            #   旧实现这里 return，把下面的 SyncFallback 一并跳过 —— 账号只配
            #   webhook 时拉取兜底永远不启动（WS 断线期间的消息只能等 webhook 重放）。
            log.warning("[WPP:%s] 未配 wsUrl，跳过实时通道（增量兜底照常启动）", acct.id)
        else:
            try:
                ws = WppWsClient(
                    account_id=acct.id, ws_url=acct.ws_url, authcode=acct.authcode,
                    on_raw=lambda payload, a=acct: self._on_raw(a, payload),
                    # W5：WS 自己发来的两个事件（连上 / 只带通知的帧）—— **同步**回调，
                    #     这里只 create_task（一趟 HTTP 拉取绝不能堵住 WS 读循环）。
                    on_event=lambda reason, a=acct.id: self._on_ws_event(a, reason),
                )
                ws.start()
                self._ws[acct.id] = ws
            except Exception as exc:
                log.error("[WPP:%s] WS 启动失败: %s", acct.id, exc, exc_info=True)

        # 兜底**独立**于 WS 起没起来 —— WS 挂掉的时候正是它最有用的时候
        # （那时只有 60s 定时器在拉，而 ``ws.connected`` 为假 ⇒ HTTP 心跳自动不发）。
        client = self._clients.get(acct.id)
        if client is None:
            log.warning("[WPP:%s] 无 vendor client，增量兜底未启动", acct.id)
            return
        # ⚠️ 同一个账号上挂**两个** SyncFallback = 两个 ``/Msg/Sync`` 轮询、两个游标互相踩
        #    ⇒ **静默丢消息** —— 正是 in-flight 锁要防的那件事，只是发生在实例之间。
        #    本类要求 ``connect()`` 前先 ``disconnect()``，但那条要求不该被当成保证：
        #    这里**宁可少起一个**（少了只是兜底弱一点、WS 帧那条主通道照旧），
        #    也不冒"两条轮询互相踩"的险。真走到这条分支说明调用方漏了 disconnect，要吵。
        if acct.id in self._sync:
            log.warning("[WPP:%s] 已有增量兜底在跑（重复 connect？）—— 沿用旧的，不再起第二个",
                        acct.id)
            return
        try:
            sync = SyncFallback(
                acct.id, client,
                # 拉回来的条目与 WS 帧**同形**（TS 也是把 raw 交给同一个回调：
                # ``ws-client.ts:243`` 的 ``onInboundMessage(raw)``）⇒ 走同一个入口，
                # 幂等由 ``wpp_messages`` 唯一键 + 内存去重兜。
                # ★ 2026-10-19 审阅修复：**不能走 _on_raw** —— 它只把消息塞进聚合器就
                #   返回，而 SyncFallback 拿到返回就推进游标。进程在 500ms 聚合窗口内
                #   退出时这批消息既没落库也没处理，游标却已越过 ⇒ 永久丢失。
                #   兜底通道本来就是"补 WS 漏掉的"，必须等下游真正走完再动游标。
                on_message=lambda item, a=acct: self._on_sync_raw(a, item),
                is_connected=lambda w=ws: bool(w is not None and w.connected),
            )
            sync.start()
            self._sync[acct.id] = sync
        except Exception as exc:
            log.error("[WPP:%s] 增量兜底启动失败（WS 帧那条路不受影响）: %s",
                      acct.id, exc, exc_info=True)

    def _on_ws_event(self, account_id: str, reason: str) -> None:
        """WS 事件钩子（**同步、非阻塞**）—— W5。

        调用点在 WS 读循环里（``inbound_ws._dispatch`` → ``_emit``），所以这里**只许**
        ``spawn``（等价 TS 的 ``void this.triggerSync(...)``）：一趟 ``/Msg/Sync`` 是 HTTP，
        直接 ``await`` 会把**收帧**一起堵住（快群能把读循环压成"一条一拉"）。

        ``reason`` 只有两个来源：``ws-open``（每次连上/重连 —— 断开那段窗口只能这么补）
        与 ``sync-update``（收到**不带消息体**的通知帧）。**未知 reason 一律按 push 转**
        （vendor 加新事件类型时不该被这里静默吃掉），反正两者只差"要不要重开 AutoHeartBeat"。
        """
        sync = self._sync.get(account_id)
        if sync is None:
            return
        sync.spawn(REASON_OPEN if reason == REASON_OPEN else REASON_PUSH)

    def _start_sweeps(self, connectable: list[accounts_mod.Account]) -> None:
        """账号启动：预热内存缓存，再起 300s sweep（心流 + 风格学习各一个 task）。

        ⚠️ 心流九模块是计划第 6 步的产物 —— 未就位时**静默跳过**（记 debug），
        绝不让它成为接入的阻塞点。风格学习是独立开关（2026-10-19），**不依赖心流
        模块是否可用**：即使 ``heartflow`` import 失败，风格 sweep 也要照常起。

        预热为什么不是 sweep 的内部事：judge / 回复侧热路径零 DB IO（V7），读的全是
        内存缓存；而 sweep 是"先睡 300s 再跑"（对齐 TS ``setInterval``）—— 不预热的话，
        重启后头 5 分钟里阈值回落账号级、预算从 0 起算、风格卡缺失。顺序：**先预热
        再起任务**，否则首条消息仍可能撞上冷缓存。
        """
        try:
            from ..inbound.heartflow import runtime as hf_runtime
        except Exception:  # noqa: BLE001 —— 第 6 步之前必然 import 失败
            hf_runtime = None
            log.debug("[WPP] 心流模块未就位，心流 sweep 未启动")

        try:
            from ..inbound import style_learn as style_mod
        except Exception:  # noqa: BLE001 —— 风格模块未就位不该影响接入
            style_mod = None
            log.debug("[WPP] 风格学习模块未就位，风格 sweep 未启动")

        try:
            from ..inbound import proactive as proactive_mod
        except Exception:  # noqa: BLE001 —— 主动关心是独立增强，不该影响接入
            proactive_mod = None
            log.debug("[WPP] 主动关心模块未就位，proactive sweep 未启动")

        for acct in connectable:
            if hf_runtime is not None:
                # 第二道网：``preheat_account`` 自己保证不抛，但这里若被它带崩，下面那条
                # sweep 任务就**一起没了** —— 心流静默停摆，比预热失败严重得多。
                try:
                    hf_runtime.preheat_account(acct.id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[WPP:%s] 心流启动预热失败（不影响接入）: %s", acct.id, exc)
                try:
                    task = asyncio.create_task(
                        hf_runtime.sweep_loop(self, acct.id, interval=300),
                        name=f"wpp-hf-sweep-{acct.id}")
                    self._sweep_timers.append(task)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[WPP:%s] 心流 sweep 启动失败: %s", acct.id, exc)

            if style_mod is not None:
                try:
                    style_mod.preheat_account(acct.id)
                except Exception as exc:  # noqa: BLE001 —— preheat 自己也不抛，双保险
                    log.warning("[WPP:%s] 风格卡启动预热失败（不影响接入）: %s",
                                acct.id, exc)
                try:
                    task = asyncio.create_task(
                        style_mod.sweep_loop(acct.id, interval=300),
                        name=f"wpp-style-sweep-{acct.id}")
                    self._sweep_timers.append(task)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[WPP:%s] 风格 sweep 启动失败: %s", acct.id, exc)

            # 无条件起（每轮 tick 现读账号配置）：这样 ``/proactive on`` 写回后
            # **不需要重启**，下一轮 sweep 就生效。关着时 tick 只是一次 dict 读。
            if proactive_mod is not None:
                try:
                    task = asyncio.create_task(
                        proactive_mod.sweep_loop(self, acct.id),
                        name=f"wpp-proactive-sweep-{acct.id}")
                    self._sweep_timers.append(task)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[WPP:%s] 主动关心 sweep 启动失败: %s", acct.id, exc)

    def _start_name_sync(self, connectable: list[accounts_mod.Account]) -> None:
        """每账号起一个群名同步任务（``/Group/List`` → ``wpp_chatrooms``）。

        只用于**展示**：WebUI 会话标题与 ``SessionSource.chat_name`` 要显示群名，
        而 v1 入站帧只有 ``xxx@chatroom``。同步失败只 warn —— 标题会退回群 ID，
        收发消息完全不受影响。
        """
        from ..inbound import name_sync

        for acct in connectable:
            client = self._clients.get(acct.id)
            if client is None:
                continue
            try:
                task = asyncio.create_task(
                    name_sync.sync_forever(client, acct.id),
                    name=f"wpp-name-sync-{acct.id}")
                self._name_tasks.append(task)
            except Exception as exc:  # noqa: BLE001
                log.warning("[WPP:%s] 群名同步启动失败（不影响收发）: %s", acct.id, exc)

    async def _maybe_member_event(self, acct, msg) -> None:
        """进群/退群系统通知 → 拉该群成员昵称增量（**事件驱动，不做定时轮询**）。

        老板 2026-10-20 拍板：机器人进群时拉一次全量、有新人加入时拉一次该群，
        不要按小时扫全部群。系统通知只带昵称不带 wxid，所以必须回查成员列表
        才能把「张三进群了」变成 ``(wxid, 昵称)`` 落库。

        任何失败都只记日志 —— 记账坏了绝不能让收信链路失败。
        """
        from ..inbound import join_events, name_sync

        try:
            ev = join_events.parse(msg)
        except Exception as exc:  # noqa: BLE001 —— 解析器坏了不该挡住这条消息
            log.debug("[WPP:%s] 成员事件解析失败: %s", acct.id, exc)
            return
        if ev is None:
            return
        if ev.kind == "unknown_system":
            # 厂商换文案时的唯一观测点：先留痕，拿到样本再补正则。
            log.info("[WPP:%s] 未识别的群系统通知（群 %s）：%s",
                     acct.id, ev.chatroom_id, ev.raw)
            return

        client = self._clients.get(acct.id)
        if client is None:
            log.debug("[WPP:%s] 成员事件跳过：client 未就绪（群 %s）",
                      acct.id, ev.chatroom_id)
            return

        # self_join = 机器人自己被拉进群：新群还没 id，只能拉全部群。
        # members_changed = vendor 只给占位符（实测无原文）⇒ 拉该群一次对比即可。
        targets = None if ev.kind == "self_join" else [ev.chatroom_id]

        # 节流：同一群 30s 内只拉一次。vendor 可能对一次成员变动连推多条 10000
        # （实测进群+改群名会各推一条），不节流就是同一份名单拉两遍。
        if ev.kind == "members_changed" and ev.chatroom_id:
            now = time.time()
            last = self._member_event_at.get(ev.chatroom_id, 0.0)
            if now - last < _MEMBER_EVENT_THROTTLE_SEC:
                log.debug("[WPP:%s] 成员变动节流跳过（群 %s，%.0fs 内）",
                          acct.id, ev.chatroom_id, now - last)
                return
            self._member_event_at[ev.chatroom_id] = now

        try:
            res = await name_sync.sync_members(client, acct.id, targets)
            log.info("[WPP:%s] 成员变动 %s（群 %s）⇒ 写入 %d 条｜新增 %s｜移出 %s",
                     acct.id, ev.kind, ev.chatroom_id or "全部", res["written"],
                     _fmt_members(res["added"]), _fmt_members(res["removed"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 昵称刷新失败不影响收发
            log.warning("[WPP:%s] 成员变动后刷新昵称失败（群 %s）: %s",
                        acct.id, ev.chatroom_id, exc)

    def _employee_group_ids(self, account_id: str) -> list[str]:
        """当前账号配置的全员群 id（每轮现读，命令改完不用重连）。"""
        from ..inbound import employee_sync

        acct = self._accounts.get(account_id)
        raw = getattr(acct, "raw", None) if acct is not None else None
        return employee_sync.group_ids_from_raw(raw) if isinstance(raw, dict) else []

    def _start_employee_sync(self, connectable: list[accounts_mod.Account]) -> None:
        """每账号起一个全员群成员同步（成员 → ``allowFrom``，收发共用）。

        失败只 warn：同步失败时沿用上一次的名单，收发完全不受影响。
        """
        from ..inbound import employee_sync

        for acct in connectable:
            if not self._employee_group_ids(acct.id):
                continue
            client = self._clients.get(acct.id)
            if client is None:
                continue
            try:
                task = asyncio.create_task(
                    employee_sync.sync_forever(
                        client, acct.id,
                        lambda aid=acct.id: self._employee_group_ids(aid),
                        adapter=self),
                    name=f"wpp-employee-sync-{acct.id}")
                self._employee_tasks.append(task)
            except Exception as exc:  # noqa: BLE001
                log.warning("[WPP:%s] 全员群同步启动失败（不影响收发）: %s", acct.id, exc)

    # ================================================================ 入站

    async def _on_raw(self, acct: accounts_mod.Account, payload: dict) -> None:
        """WS 帧 / webhook payload 的统一入口。

        顺序（**不可调换**）：自消息过滤 → 去重 → 空消息体 → 构造事件 → ``handle_message``。
        这些都是 ``_handle_one`` 内部的事；**本方法只决定"什么时候交出去"**（W7-6）：
        交给该账号的聚合器（窗口内合并成一批），或直通。
        """
        # ⚠️ 进来的 ``acct`` 可能是**旧快照**：WS 与 webhook 的回调是在 ``connect()`` 时用
        #    默认参数闭包绑定的（``lambda payload, a=acct: self._on_raw(a, payload)``），
        #    而 W6 的命令写回会重载 ``self._accounts``。不在这里取最新那份，命令改过的
        #    白名单/群策略/黑名单就**永远走不到这条唯一活着的入站路径上** ——
        #    「账号文件改了、链路照旧」正是本项要消灭的失败形态。
        acct = self._accounts.get(acct.id, acct)

        pipeline = self._pipelines.get(acct.id)
        if pipeline is None:
            log.warning("[WPP:%s] 收到消息但管线未就绪（未连接？），丢弃", acct.id)
            return

        db = self._debounce.get(acct.id)
        for msg in pipeline.extract(payload):
            # W7-6：``enqueue`` 返 False（没有事件循环 / 聚合器已停机）= **这一条没人接手**
            # ⇒ 必须自己直通。把它当成"已经派发了"是这个模块最安静的坏法：
            # 现场只是"有些人说话机器人永远不理"，日志里连一行 WARN 都不会有。
            if db is None or not db.enqueue(msg):
                await self._dispatch_one(acct, msg, pipeline)

    async def _on_sync_raw(self, acct: accounts_mod.Account, payload: dict) -> None:
        """增量兜底的派发入口：**绕开聚合器，等 ``_handle_one`` 真正返回**。

        与 :meth:`_on_raw` 的差别只有"什么时候算派发完成"：WS/webhook 可以入队就返回，
        因为它们有后续的重放兜底（``/Msg/Sync``）；而这里**就是兜底本身** ——
        ``SyncFallback.trigger`` 在 ``on_message`` 返回后立刻推进游标，若只入队，
        进程在聚合窗口内退出就再也拉不回这批消息。

        代价：兜底回放会逐条串行处理（含 LLM 调用），比 WS 路径慢；但兜底只在 WS
        漏帧/重连窗口触发，正确性优先。去重仍由 ``_handle_one`` 的 ``msg_id`` 闸兜住。
        """
        acct = self._accounts.get(acct.id, acct)
        pipeline = self._pipelines.get(acct.id)
        if pipeline is None:
            log.warning("[WPP:%s SYNC] 兜底消息到达但管线未就绪（未连接？），丢弃", acct.id)
            return
        for msg in pipeline.extract(payload):
            await self._dispatch_one(acct, msg, pipeline)

    async def _dispatch_one(self, acct: accounts_mod.Account, msg: WppMessage,
                            pipeline: InboundPipeline) -> None:
        """派发**一条**入站消息（聚合器 flush 与"直通"两条路共用这一个出口）。

        ⚠️ 单条坏消息不许打断整批 —— 聚合器那边一批 N 条是 ``for`` 着的（``_flush_inbound``），
        这里吞掉异常等于"这几条里只有坏的没了"，正是要的。
        """
        try:
            await self._handle_one(acct, msg, pipeline)
        except Exception as exc:
            metrics.inc_handler_onerror()
            log.warning("[WPP:%s] 处理消息 %s 失败: %s", acct.id, msg.msg_id, exc, exc_info=True)

    def _merge_allowed(self, account_id: str, batch: list[WppMessage]) -> bool:
        """这一批该不该合并（``debounceMerge``；默认关）。

        范围**刻意收窄**：私聊可合；群里只在批内有消息 **@ 或引用机器人**时才合。
        群里其余消息保持逐条 —— 否则会把几个群友互不相干的话拼成一段，心流/好感度
        的"谁说了什么"口径当场被破坏。
        """
        from ..inbound.parser import MT_TEXT

        acct = self._accounts.get(account_id)
        if acct is None or not acct.debounce_merge.get("enabled"):
            return False
        if len(batch) < 2:
            return False
        # 媒体/引用消息各自有富化路径，混进来会丢附件 ⇒ 整批不合并
        if any(m.msg_type != MT_TEXT or m.media or m.reply_to for m in batch):
            return False
        first = batch[0]
        if not first.is_group:
            return True
        self_wxid = acct.self_wxid or ""
        for msg in batch:
            if is_quoting_bot(msg, self_wxid):
                return True
            if is_bot_mentioned_by_text(msg.content or "", self_wxid, acct.nickname):
                return True
        return False

    async def _flush_inbound(self, account_id: str, batch: list[WppMessage]) -> None:
        """W7-6 聚合器的回调：一批同 key 消息，**逐条**走现有 ``_handle_one``。

        ⚠️ **逐条、不合并**：本项只做到达层聚合，处理语义一字未动（见
        ``inbound/debounce`` 的模块 docstring）。把这里改成"只看最后一条"就等于偷换了
        触发语义 —— 那是另一项工作。
        """
        acct = self._accounts.get(account_id)
        pipeline = self._pipelines.get(account_id)
        if acct is None or pipeline is None:
            # 停机窗口（管线已清）或账号被移除：这批无处可去。**要吵** —— 安静地丢掉
            # 一批消息是这条链上最坏的失效形态。
            log.warning("[WPP:%s] 聚合的一批 %d 条无处派发（未连接/已停机），丢弃: %s",
                        account_id, len(batch), [m.msg_id for m in batch][:5])
            return
        for msg in batch:
            await self._dispatch_one(acct, msg, pipeline)

    async def _handle_one(self, acct: accounts_mod.Account,
                          msg: WppMessage, pipeline: InboundPipeline) -> None:
        metrics.inc_messages_in()
        # ① 自消息 / 回声过滤（防 vendor 回推 self → 自问自答）
        #    ⚠️ filehelper **必须豁免**：vendor 常把文件传输助手会话的发送者标成
        #    **机器人自己**，不豁免 ⇒ 管理命令永远被这道门吃掉，后面 ③b 成了死码。
        #    这是历史修复 P1-2.3 的同一条坑 —— ``inbound.triggers`` 里的解法是
        #    "④ filehelper 排在 ⑤ 自己发的之前"，这里豁免是同一个理由。
        if acct.self_wxid and msg.from_wxid == acct.self_wxid and not is_filehelper_session(msg):
            # ⚠️ 自己的回推**不进心流缓冲**（同 TS：自消息在那边的 verdict 是 ``blocked``）。
            #    bot 自己的发言由**出站**那条路径记（见 :meth:`_hf_note_bot_reply`）——
            #    两边都记会在缓冲里留下同一句话的两份，"最近 5 条上下文"就掺水了。
            log.debug("[WPP:%s] 自家消息，丢弃 %s", acct.id, msg.msg_id)
            return

        # ② 去重 —— key 必须**含账号**（多账号可能撞 msg_id）
        if self._dedup.is_duplicate(f"{acct.id}:{msg.msg_id}"):
            metrics.inc_messages_rejected_dedupe()
            log.debug("[WPP:%s] 重复消息，丢弃 %s", acct.id, msg.msg_id)
            return

        # ③ 空消息体保护（既无正文又无媒体 —— vendor 的心跳噪声）
        if not (msg.content).strip() and not msg.media:
            log.debug("[WPP:%s] 空消息体，丢弃 %s", acct.id, msg.msg_id)
            return

        # ③b filehelper 管理命令 —— **必须**在 ④ 之前截住。
        #     为什么不能交给 ④ 之后处理：``inbound.triggers`` 的 ④ 硬门会把
        #     filehelper **判成 should_process=True**（它确实是给机器人看的消息），
        #     于是 ``/heartflow on`` 会被构造成事件喂给 LLM，机器人开始"回答"一条命令。
        #     判定与处置必须贴着走 —— 判据在 ``inbound.triggers.is_filehelper_session``。
        if await self._maybe_command(acct, msg):
            return

        # ③b2 配对码兑换（私聊 ``/pair <code>``）—— 必须在 ④ 触发判定**之前**：
        #       ``dmPolicy=pairing`` 下未配对用户的普通消息会被准入拒掉，但
        #       ``/pair`` 本身是"进门"动作，不能也被拒。
        if not msg.is_group and (acct.dm_policy == "pairing" or acct.dm_pairing_enabled):
            if await self._maybe_pair(acct, msg):
                return

        # ③c 临时禁言（``/mute``，内存态）：到点自动恢复。整条丢弃 —— 包括 @机器人，
        #     否则"禁言期间别人一 @ 就破防"，这个开关就没有意义。filehelper 命令在
        #     ③b 已经走掉，不受影响。
        if msg.is_group and self.group_muted(acct.id, msg.chatroom_id):
            log.debug("[WPP:%s] 群 %s 临时禁言中，丢弃 %s",
                      acct.id, msg.chatroom_id, msg.msg_id)
            return

        # ③d 群成员变动（进群/退群）—— **必须**在 ④ 触发判定之前：
        #     系统通知不满足任何触发路，④ 会 fail-closed 丢掉它，于是"有人进群"
        #     永远进不了 ``wpp_chatroom_members``（2026-10-20 实测：移动群新人
        #     进群零记录，成员表停在 8-11，心流 prompt 里只剩 ``wxid_xxx``）。
        #     这里只做**记账**，不改变 msg 的后续去向 —— 系统消息该丢还是丢。
        await self._maybe_member_event(acct, msg)

        # ④ 触发判定（硬门 + 四路 OR，见 inbound.triggers）
        ok, reason = pipeline.decide(msg)
        if is_card_message(msg) and reason == "red_packet":
            process_red_packet(msg)          # 只 log，不拆
        # 丢不丢交给 ``inbound.triggers.should_drop``（**fail-closed**）：只有
        # ``(False, heartflow_candidate)`` 能漏到下游，其余 ``(False, …)`` ——
        # 包括**没登记过的新 reason** —— 一律丢。本模块只负责留痕与终止。
        if should_drop(ok, reason):
            metrics.inc_messages_rejected_policy()
            log.debug("[WPP:%s] 丢弃 %s（reason=%s）", acct.id, msg.msg_id, reason)
            return

        # ④b1 硬触发限速闸（R1，2026-10-03「两个机器人互相 @ 死循环」的止血）。
        #     只覆盖「100% 必回、绕过心流」的那两条（``group_mention`` / ``group_solitaire``）
        #     —— 心流的预算/冷却全在 ``_try_heartflow`` 里面，而这两条在 ④ 就直通
        #     ``handle_message``，**没有任何上限**。
        #     ⚠️ **位置必须是这里**（④ 之后、④c/④z/④a **之前**），理由有三条：
        #     ① 回路里的那些消息也是"白名单内的群消息" —— 放到 ④z 之后就等于让对刷的
        #        24 条继续进台账、进心流原始缓冲、进好感度与黑话统计，把裁判的现场记录也污染掉；
        #     ② 放到 ④c 之后，每条被掐的消息还会**下载 + 上传一次媒体**（白花钱）；
        #     ③ 闸的判据只需要 ``reason`` 与发送者，早判 = 少做无用工。
        #     ⇒ 被限速的那条**整条丢弃**（不是交给心流）：理由写在
        #     ``TriggerRateLimiter.guard`` 的 docstring 里。
        #     WARNING 级：``真机`` 的 journald 只采 WARNING+，用 debug 就等于"现场看不见"。
        _limit = self._trigger_limiter.guard(
            acct.id, msg.peer_id, msg.from_wxid, reason)
        if not _limit.allowed:
            log.warning("[WPP:%s] 触发限速：%s 在群里触发过密（%s，hits=%d，静默至 %.0f）"
                        "⇒ 丢弃 %s", acct.id, msg.from_wxid, _limit.reason,
                        _limit.hits, _limit.muted_until, msg.msg_id)
            return

        # ④c 媒体富化（W7-1）—— 位置**必须**在这里（④ 之后、④z/④a 之前），理由是一条链：
        #     ``msg.content`` 是这条链路上**唯一**的口径，而三个消费者都在下游读它 ——
        #     ① ``persist_inbound``（④z）：TS 的 ``enrichAndSaveMessage`` 就是**先富化再落库**，
        #        DB 里存的正文自带 OSS URL；
        #     ② ``_hf_note_group``（④a）：心流原始缓冲存的也是富化后的正文；
        #     ③ ``maybe_engage``：``content = getattr(msg, "content", "")`` → ``HfJudgeInput.content``
        #        → ``extract_hf_image_urls`` → judge 的 ``image_url``（v1.11.0 看图）。
        #     ⚠️ **在 ⑤（``_build_event``）里取媒体是不够的** —— 那条路只覆盖"触发了的"消息，
        #     而 ④z/④a 都排在它**之前** ⇒ 提取函数（只认 ``http(s)``/``data:image``）永远
        #     拿不到直链，**v1.11.0 看图在 py2 是一整段空转**（唯一的 ``[图片]`` 标记是
        #     ``_media_tag`` 打的前缀，后面没有 URL）。这就是本项把它提到这里的原因。
        #     代价：白名单内的**每条**媒体消息都要下载 + 上传一次（今天只有触发了的才下）。
        #     与 TS 同款 —— 它的 ``onFlush`` 也是先把**整批** enrich 完再逐条 dispatch
        #     （``handler.ts:222`` 的 ``for (const m of batch)`` 在 ``persistBatch`` 之前）。
        media = await self._fetch_media(acct, msg)

        # ④z 入站落库（W1，对齐 TS ``handler.ts:539`` 的
        #     ``persistBatch = batch.filter(m => persistResults.get(m)?.via !== "blocked")``）。
        #     位置**必须**在 ④ 之后：自消息（①）/重复（②）/空体（③）/红包·撤回·公众号·
        #     黑名单群·非白名单会话（④）都已剔除 ⇒ 落下来的是"白名单内、真被人说出口"的话，
        #     与 TS 的隐私口径一致（TS 注释：黑名单群/非 allowlist/白名单外 DM/自回环不入库）。
        #     又必须**早于** ④a 与 ⑤：心流缓冲、judge、LLM 上下文都从这里取粮。
        #     ⚠️ 仍然**只在白名单内**落库 —— 这是项目约定过的隐私口径，别为了"数据全"放宽。
        #     ``persist_inbound`` 自己吞一切异常（只 warn），不会因为写库失败丢消息。
        # 同步 pymysql（ping + INSERT + commit）⇒ 必须 offload，否则库一慢整条
        # 事件循环停摆（read/write_timeout=30s），WS 心跳与其它账号一起排队。
        if await asyncio.to_thread(persist_inbound, acct, msg):
            metrics.inc_messages_saved_db()

        # ④d 接龙**整条改写**（W7-3）—— 位置 = ④z 与 ④a/④b **之间**。
        #     ⚠️ 标签里的 ``d`` **不表示顺序**（它排在 ④a/④b 之前），别按字母序找它。
        #     判据是 TS ``handler.ts`` 批内三段的先后：``enrichBatch``(539 落库)
        #     → **relay 改写**(541-553) → ``recordRawMessage``(570 心流缓冲)
        #     → JARGON(589) → AFFECTION(620) → dispatch ⇒ 口径是
        #     **DB 里留原始正文，下游（心流缓冲 / 黑话 / 好感度 / judge / LLM）看到改写后的**。
        #     ⚠️ **更正（2026-10-02，做 W7-4 排位时发现）**：W7-3 首次落地把这一行放在
        #     ④a/④b **之后**，理由写的是"TS 的池来自 DB 行，同样是原始形态"—— **那句是错的**。
        #     TS 的 ``recordRawMessage`` 写的是**进程内**缓冲（py2 的对等物是
        #     ``heartflow._raw_buffers``，见 ``heartflow.note_group_message``），
        #     而它拿的正是 relay 改写后的 ``m.content`` ⇒ 排在后面会让 py2 的 judge 与
        #     好感度看到原始那坨，与 TS 分叉，且**两边都不报错**。
        #     ``apply_relay_render`` 自己吞异常（只 warn、正文保持原样，TS 的 try/catch 同款）
        #     —— 接龙解析坏了不该让整条消息消失。
        apply_relay_render(msg)

        # ④a 心流旁路观测 —— 放行条件对齐 TS 的 ``via !== "blocked"``：**只记未被丢弃的群消息**。
        #     必须在 ④ 之后（否则黑名单/非白名单群的消息也进缓冲，"隐私"那条注释就成空话），
        #     但对**通过与否都记** —— 只在"触发了"时才喂，judge 看到的"最近 N 条上下文"
        #     就只剩 bot 自己那几条，等于给裁判看了一份伪造的现场记录。
        #     ⚠️ 比 TS 略严：红包 / 撤回 / 公众号也不进缓冲（它们不是对话内容）。
        if msg.is_group:
            self._hf_note_group(acct, msg, is_bot=False)
            # ④b' 黑话旁路（W7-4，对齐 TS ``handler.ts:589-616``）。
            #     位置**必须**夹在 ④a（心流缓冲）与 ④b（好感度）**之间** —— TS 的批内
            #     三段次序就是 ``recordRawMessage``(570) → **JARGON**(589) → AFFECTION(620)。
            #     顺序本身不改变"谁被采集"（三条旁路互不相干），但它是**排位判据的锚**：
            #     排到 ④d 之前 ⇒ 采到的是原始那坨 ``#接龙 …``（与 TS 分叉），
            #     排到 ④b 之后 ⇒ 与 TS 的批内次序不符、且"黑话只在好感度之后才动"没有任何依据。
            #     纯旁路，与心流互补：**心流 = 何时开口，黑话 = 听懂群文化**（TS 原话）。
            self._jargon_note_group(acct, msg)
            # ④b 好感度旁路（W2，对齐 TS ``handler.ts:623-636``）。与心流**并列**：
            #     都与"这条消息有没有触发 AI"无关 —— 群里的每句人话都算数。
            #     fire-and-forget：见 :meth:`_affection_note_group` 的 docstring。
            self._affection_note_group(acct, msg)

        hf_engaged = False
        if not ok:
            # 只剩 heartflow_candidate（其余 deny 已被 should_drop 拦在上一步）
            if not await self._try_heartflow(acct, msg, pipeline):
                log.debug("[WPP:%s] 心流未接管 %s（reason=%s）", acct.id, msg.msg_id, reason)
                return
            hf_engaged = True
        else:
            log.info("[WPP:%s] 触发（%s）: peer=%s type=%s", acct.id, reason, msg.peer_id, msg.msg_type)

        # ⑤ 构造事件并交给 runner（**必须**走 handle_message，见 docstring ④）
        #
        # 心流回合 = **主动搭话**，不是有人 @ 它：模型若用发送工具把话说完、再按
        # 「不要重复发」输出 ``[SILENT]``，网关不该把这条静默判成故障并替换成
        # ``⚠️ The model returned only a silence marker...``（2026-10-18 生产群事故）。
        # ``reply_expected=False`` 是 Hermes 给"这条消息不是冲着机器人来的"预留的语义，
        # 静默得以成立；@ 提及 / 私聊仍传 ``None``（= 未知 ⇒ 保留可见兜底）。
        event = await self._build_event(
            acct, msg, pipeline, media=media,
            reply_expected=False if hf_engaged else None)
        if event is None:
            return
        # ★ 把**真实**账号/发送者钉在当前异步任务上：工具层据此拒绝
        #   "模型自填 account_id/callerWxid"（见 core/inbound_context.py）。
        #   handle_message 内部的工具 dispatch 在同一个任务里，contextvar 可见。
        from ..core import inbound_context

        with inbound_context.bind(acct.id, msg.from_wxid,
                                  msg.peer_id):
            metrics.inc_dispatch_dispatched()
            await self.handle_message(event)

    async def _maybe_pair(self, acct: accounts_mod.Account, msg: WppMessage) -> bool:
        """私聊 ``/pair <code>`` → 兑换并写入已配对名单（返回 True = 已接管）。

        与 filehelper 管理命令分开：配对是**给陌生人用的**，不能要求 ``adminUsers``；
        它唯一的凭据就是 1 小时有效的配对码。回复走原始 client（不走出站白名单）——
        因为此时用户**尚未配对**，走 ``self.send`` 会被白名单闸拦下，用户永远看不到
        "配对成功/失败"。
        """
        from . import pairing

        content = (msg.content or "").strip()
        if not pairing.is_pair_command(content):
            return False

        code = pairing.extract_pair_code(content)
        sender = msg.from_wxid or ""
        if not code:
            reply = "用法：/pair <8位配对码>（配对码请向管理员索取）"
        else:
            ok, why = pairing.redeem_pairing_code(acct.id, code)
            if ok:
                pairing.add_paired(acct.id, sender)
                reply = "配对成功，现在可以直接和我聊天了。"
            else:
                reply = f"配对失败：{why}"

        client = self._clients.get(acct.id)
        if client is None:
            log.warning("[WPP:%s] 配对回复未发送（账号未连接）: %s", acct.id, sender)
            return True
        try:
            await client.send_text(to_wxid=sender, content=reply)
        except Exception as exc:  # noqa: BLE001 —— 回复失败不影响兑换结果
            log.warning("[WPP:%s] 配对回复发送失败: %s", acct.id, exc)
        return True

    async def _maybe_command(self, acct: accounts_mod.Account, msg: WppMessage) -> bool:
        """filehelper 管理命令 → 就地回复并**终止**这条消息（返回 True = 已接管）。

        判定（:func:`inbound.triggers.is_filehelper_session`）与处置分在两处，是因为
        ④ 那道硬门只回答"该不该处理"，而 filehelper 的"处理方式"是**执行命令**，
        不是喂给 LLM（见 ``_handle_one`` 的 ③b 注释）。

        权限：**仅 ``adminUsers``**，且两条边界都 **fail-closed**（C3，2026-10-02 修）：

        * ``adminUsers`` 未配 ⇒ **不再放行**（旧行为是放行 —— 管理面命令裸奔；真机上
          ``live-test.json`` 就是空表）；
        * 空发件人 ⇒ **不再放行**（旧判据 ``sender and sender not in admin`` 对空串短路，
          直接落到执行；而 vendor 在 filehelper 会话里常把 sender 标成机器人自己或留空）。

        判据在 :mod:`core.filehelper_auth`（纯函数，本机可 import ⇒ 有真实用例与变异门，
        不必靠读源码猜）。

        回复目标：filehelper 会话就回 ``filehelper``（vendor 承认的合法收件人），
        不赌 ``msg.from_wxid`` —— 这个字段在 filehelper 会话里常被 vendor 标成
        **机器人自己**（见 :mod:`inbound.triggers` 第 ④ 步的说明）。
        """
        if not is_filehelper_session(msg):
            return False

        why = filehelper_auth.verdict(acct.admin_users, msg.from_wxid)
        if why != filehelper_auth.ALLOW:
            # 拒绝**同样是接管**（返回 True）：漏给 LLM 等于把管理命令当闲聊回一句。
            if why == filehelper_auth.NO_ADMIN_CONFIGURED:
                log.error("[WPP:%s] filehelper 命令**拒绝**：账号未配 adminUsers ⇒ 一律不执行"
                          "（管理面不裸奔）。要启用：在账号配置里加 "
                          "\"adminUsers\": [\"<你的 wxid>\"]，再重启网关。", acct.id)
            else:
                log.warning("[WPP:%s] filehelper 命令被拒: 非 admin（sender=%r）",
                            acct.id, msg.from_wxid or "")
            return True

        content = msg.content or ""
        try:
            from . import commands

            # adapter=self：写配置的命令（/user /group /heartflow threshold …）写回账号
            # 文件后靠它当次生效（``commands.write_back`` → ``reload_account``）。
            reply = await commands.handle_command_async(acct.id, content, adapter=self)
        except Exception as exc:
            log.warning("[WPP:%s] filehelper 命令处理失败: %s", acct.id, exc, exc_info=True)
            return True

        if reply:
            # 走 self.send：复用出站白名单闸 / 拒绝话术过滤 / 分段（别绕过它们）
            result = await self.send(self._chat_id(acct.id, FILEHELPER), reply)
            # ⚠️ 2026-10-18 真机冒烟抓到的假日志：``send`` 把出站白名单拦截收敛成
            #   ``SendResult(success=False)`` 而**不抛**（见本方法上面 send 的 except），
            #   旧写法不看结果就记"已回复"—— 命令其实一条都没发出去，日志却像成功了。
            if getattr(result, "success", False):
                log.info("[WPP:%s] filehelper 命令已回复: %s", acct.id, content.split(None, 1)[0])
            else:
                log.warning("[WPP:%s] filehelper 命令**未回复**（命令已执行，回复发送失败）: %s — %s",
                            acct.id, content.split(None, 1)[0],
                            getattr(result, "error", "") or "unknown")
        return True

    async def _try_heartflow(self, acct: accounts_mod.Account, msg: WppMessage,
                             pipeline: InboundPipeline) -> bool:
        """心流接管（未 @ 的群消息）。

        ⚠️ 计划第 6 步之前此路**恒 False** —— 心流九模块未就位时不该假装处理了。
        """
        try:
            from ..inbound.heartflow import heartflow as hf
        except Exception:  # noqa: BLE001
            return False
        try:
            return bool(await hf.maybe_engage(self, acct, msg, pipeline))
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s] 心流判定异常（按不接管处理）: %s", acct.id, exc)
            return False

    # ---------------------------------------------------------- 心流三处接缝
    #
    # 三处都是"心流在场就顺手做、不在场就完全不影响主链路"的形状：
    # 懒导入（F5：模块级第三方依赖会让**注册**静默失败）+ 吞异常 + 记一条 warning。
    # 心流坏了不该让一条消息收不进来、也发不出去。

    @staticmethod
    def _hf_mod() -> Any:
        """懒取心流模块（未就位 ⇒ ``None``）。"""
        try:
            from ..inbound.heartflow import heartflow as hf
        except Exception:  # noqa: BLE001 —— 第 6 步之前必然 import 失败
            return None
        return hf

    def _hf_note_group(self, acct: accounts_mod.Account, msg: WppMessage,
                       *, is_bot: bool) -> None:
        """旁路观测：这条群消息进心流缓冲 / 记"有人说话" / 开窗时当接话候选。

        对应 TS handler 的 batch 步骤 1:1（见 ``heartflow.note_group_message``）。
        """
        hf = self._hf_mod()
        if hf is None:
            return
        # ★ 系统通知（"xxx 撤回了一条消息"/"你已加入群聊"）不是人说的话：
        #   进缓冲会污染 judge 的"最近 N 条上下文"，还会刷新"有人说话"的沉默时长
        #   ⇒ 预算以为群里一直有人聊。撤回同理（正常路径已被 should_process 拦，
        #   这里再兜一道，防止未来换入口）。
        try:
            if int(msg.msg_type or 0) in (MT_SYSTEM, MT_REVOKE):
                return
        except (TypeError, ValueError):
            pass
        content = msg.content or ""
        try:
            hf.note_group_message(
                acct.id, msg.chatroom_id,
                sender_name=msg.from_nickname or msg.from_wxid or "",
                content=content,
                ts_sec=float(msg.timestamp or 0) or time.time(),
                is_bot=is_bot,
                quotes_bot=is_quoting_bot(msg, acct.self_wxid or ""),
                # @ 的判据与触发判定第 ⑦ 路**同源**（同一函数），避免"触发了但候选标记为假"
                mentions_bot=is_bot_mentioned_by_text(
                    content, acct.self_wxid or "", acct.nickname or ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("[WPP:%s] 心流旁路观测失败（不影响收信）: %s", acct.id, exc)

    def _jargon_note_group(self, acct: accounts_mod.Account, msg: WppMessage) -> None:
        """旁路采一句黑话 + 到点触发一轮挖掘（**不阻塞收信**，对齐 ``handler.ts:589-616``）。

        调用方已保证是群消息（④a 那个 ``if msg.is_group``）。三处判据逐条对齐 TS：

        * **账号开关** ``jargon.enabled``（默认 `false`，fail-closed）；
        * **采集面** = 非系统通知 + 正文非空（``:598-602`` 的两个 ``continue``）；
        * **触发面** = 单**独**一轮，**不**排除系统通知、**也不**判正文空 —— TS 是**两个
          循环**（采集 :592-604 / 触发 :607-616），第二个循环里只有 ``blocked`` 与
          ``peerKind !== group`` 两道。合并成一个循环会少判几次 ``shouldTriggerMine``，
          后果只是"晚一条消息才挖" —— 极轻，但**不合并才是照抄**，而且合并后
          "系统通知也吃一次挖掘判据"这个 TS 特征就没了（本项不替 TS 改行为）。

        ``shouldTriggerMine`` **有副作用**（命中即记账，见其 docstring）⇒ 判完必须真去挖，
        所以这里判中就直接 ``create_task``。挖掘一轮最多 16 次 LLM 调用（1 次批量验证 +
        至多 5 个候选 × 3 步推断），``await`` 它会把整段往返塞进收信链路。

        ⚠️ ``record_message`` 与 ``update_from_message`` 都在**本进程内存**里、且都是纯
        计算（后者是分词 + 词频表自增）⇒ 不必进任务；只有挖掘那半要 fire-and-forget。
        与 TS 同形：它那两个 ``updateJargonFromMessage``/``recordJargonMessage`` 也是同步调用。
        """
        cfg = jargon_mine.resolve_jargon_cfg(acct.jargon)
        if not cfg.enabled:
            return
        group_id = msg.peer_id
        sender = msg.from_wxid or ""
        content = msg.content or ""

        # 含义缓存预热：每组每个进程只读一次库（重启后 ``_term_meanings`` 是空的，
        # 不预热则要等下一轮挖掘才恢复注入能力）。fire-and-forget，不阻塞收信。
        self._jargon_ensure_meanings(acct.id, group_id)

        # 采集面（TS 第一个循环）
        if msg.msg_type != MT_SYSTEM and content.strip():
            try:
                jargon_mod.update_from_message(content, group_id, sender)
                jargon_mine.record_message(group_id, sender, content)
            except Exception as exc:  # noqa: BLE001 —— 采集坏了不该让消息消失
                log.debug("[WPP:%s] 黑话采集失败（不影响收信）: %s", acct.id, exc)

        # 触发面（TS 第二个循环）—— 判据与副作用都在 ``should_trigger_mine`` 里，
        # ``now_ms`` 与 ``count`` 由本侧给（TS 也是显式传的：``Date.now()`` / 计数器）。
        try:
            if not jargon_mine.should_trigger_mine(
                    group_id, cfg, time.time() * 1000,
                    jargon_mine.group_message_count(group_id)):
                return
            task = asyncio.create_task(
                self._jargon_mine_run(acct.id, group_id, cfg),
                name=f"wpp-jargon-{acct.id}")
        except Exception as exc:  # noqa: BLE001 —— 起不来也绝不许影响收信
            log.warning("[WPP:%s] 黑话挖掘任务创建失败（不影响收信）: %s", acct.id, exc)
            return
        self._jargon_tasks.add(task)                     # 强引用，见 __init__
        task.add_done_callback(self._jargon_done)

    async def _jargon_mine_run(self, account_id: str, group_id: str,
                               cfg: jargon_mine.JargonCfg) -> None:
        """真正挖一轮（独立成方法便于测试直接 ``await``，不必等任务调度）。

        ``creds`` 传 ``None`` ⇒ 由 :func:`jargon_mine.jargon_llm` 自己
        ``resolve_judge_creds()``（house style，同 ``intent.py:388``）。没配 key 时
        ``jargon_llm`` 第一句就返 ``None`` ⇒ 一轮**零外呼**地空转结束（TS 同款）。
        """
        store = jargon_mine.RepoJargonStore(account_id)
        result = await jargon_mine.mine_jargon_for_group(group_id, cfg, None, store)
        if result.confirmed:
            log.info("[WPP:%s] 黑话入库 %d 条（群 %s）", account_id, result.confirmed, group_id)

    def _jargon_ensure_meanings(self, account_id: str, group_id: str) -> None:
        """把 DB 里已确认的黑话含义预热进进程内缓存（每组每进程一次）。

        为什么需要预热：``jargon._term_meanings`` 只活在进程内存里，挖掘成功时才写。
        重启后如果不预热，注入要到该群下一轮挖掘（可能几小时后）才恢复 —— 现象是
        "明明库里有词，回复里就是不解释"。

        该方法是**同步**的：只做一次 set 查重 + ``create_task``，绝不查库；
        真正的 DB 读在 :meth:`_jargon_load_meanings`（线程池）。
        """
        key = (account_id, group_id)
        if key in self._jargon_loaded:
            return
        self._jargon_loaded.add(key)
        try:
            task = asyncio.create_task(
                asyncio.to_thread(self._jargon_load_meanings, account_id, group_id),
                name=f"wpp-jargon-load-{account_id}")
        except Exception as exc:  # noqa: BLE001 —— 预热失败不该影响收信
            log.debug("[WPP:%s] 黑话含义预热任务创建失败: %s", account_id, exc)
            return
        self._jargon_tasks.add(task)
        task.add_done_callback(self._jargon_done)

    @staticmethod
    def _jargon_load_meanings(account_id: str, group_id: str) -> None:
        """从 ``wpp_jargon_terms`` 读该群已确认词条，灌进 ``jargon._term_meanings``。"""
        try:
            from ..data import repo_messages

            rows = repo_messages.list_jargon(account_id, group_id, limit=500)
        except Exception as exc:  # noqa: BLE001 —— 库不可用就保持空缓存（下次进程再试）
            log.debug("[WPP JARGON] 含义预热读库失败（不影响收信）: %s", exc)
            return
        loaded = 0
        for row in rows or []:
            term = str(row.get("term") or "").strip()
            meaning = str(row.get("meaning") or "").strip()
            if term and meaning:
                jargon_mod.remember_meaning(group_id, term, meaning)
                loaded += 1
        if loaded:
            log.info("[WPP JARGON] 含义预热 %d 条（账号 %s 群 %s）", loaded, account_id, group_id)

    def _jargon_done(self, task: asyncio.Task) -> None:
        """收尾：丢句柄 + 把异常记成一行 warn。

        这里**唯一会漏出来**的是 :class:`jargon_mine.JargonConfigError`（``jargon.model``
        没配）—— 它是**故意的**：TS 在 v1.4.0 12:28 项目约定"消除 plugin hardcode"之后
        把那个分支改成了**直接抛**，为的是把"账面上写着在挖、其实没模型"逼到明面上。
        抛出点在任何 try 之外 ⇒ 整轮中止，由这里**每轮一行** warn（不是静默）。
        其余失败（超时 / 坏 JSON / 库不可用）在 ``mine_jargon_for_group`` 内部就吞了，
        根本到不了这里。

        ⚠️ 不挂这个回调，fire-and-forget 的异常**只有 GC 时**那句
        "Task exception was never retrieved" —— 没有群、没有原因，等于把失败埋掉。
        """
        self._jargon_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:  # noqa: BLE001 —— 取异常本身不该再抛
            return
        if exc is not None:
            log.warning("[WPP] 黑话挖掘失败（不影响收信，%s）: %s", task.get_name(), exc)

    def _affection_note_group(self, acct: accounts_mod.Account, msg: WppMessage) -> None:
        """旁路记一笔好感度（**不阻塞收信**，对齐 TS ``void processAffectionMessage``）。

        触发条件逐条对齐 ``handler.ts:623-629``：群消息（调用方已保证）、未被丢弃
        （④a 的入参已经过 ④）、**非系统通知**、**非 bot 自己**、正文非空、账号开了
        ``affection.enabled``。

        ⚠️ **为什么必须是 fire-and-forget**：这条路径上做的是"DB 读改写 +
        （配了 ``model`` 时）一次 5s 超时的 LLM 调用"。``await`` 它 = 把好感度记账的
        耗时直接塞进收信链路 —— 数据库一慢，整个群看着机器人卡住。TS 的 ``void`` 就是这个意思。

        ⚠️ **并发不串行化**：同一群两条消息几乎同时到，会各自读到旧的 level 再各自写回，
        丢一次增量。TS 的进程内 ``Map`` 是同步的、没这个问题；落库版必然要付这个代价 ——
        接受它，因为"好感度少涨 1 分"远轻于"为它加锁/开事务"。

        ⚠️ 打分失败**只 warn**（见 :meth:`_affection_done`）：好感度是旁路，绝不许
        影响收信 —— 与 W6 的"写回失败必须出声"**刚好相反**，两者别混。
        """
        cfg = resolve_affection_cfg(acct.affection)
        if not cfg.enabled:
            return
        if msg.msg_type == MT_SYSTEM:                    # 系统通知不算人说话
            return
        if acct.self_wxid and msg.from_wxid == acct.self_wxid:
            return
        content = msg.content or ""
        if not content.strip():
            return

        try:
            task = asyncio.create_task(
                self._affection_run(acct, msg, cfg, content),
                name=f"wpp-affection-{acct.id}")
        except Exception as exc:  # noqa: BLE001 —— 起不来也绝不许影响收信
            log.warning("[WPP:%s] 好感度任务创建失败（不影响收信）: %s", acct.id, exc)
            return
        self._affection_tasks.add(task)                  # 强引用，见 __init__
        task.add_done_callback(self._affection_done)

    async def _affection_run(self, acct: accounts_mod.Account, msg: WppMessage,
                             cfg: Any, content: str) -> None:
        """真正干活那一段（独立成方法是为了能在测试里直接 await 它，不必等任务调度）。"""
        now_sec = int(msg.timestamp or 0) or int(time.time())
        outcome = await process_affection_message(
            acct.id, msg.chatroom_id, msg.from_wxid or "", content,
            msg.from_nickname or "", cfg, now_sec,
        )
        if outcome is not None:
            log.debug("[WPP:%s] 好感度 %s %+d（%s）", acct.id, outcome.interaction_type,
                      outcome.change, outcome.reason)

    def _affection_done(self, task: asyncio.Task) -> None:
        """收尾：丢句柄 + 把异常记成一行 warn。

        ⚠️ 不挂这个回调，fire-and-forget 的异常**只有 GC 时**那句
        "Task exception was never retrieved" —— 既没有群、也没有原因，
        等于把失败埋掉。这是 ``asyncio.create_task`` 的标准配套，不是可选项。
        """
        self._affection_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception:  # noqa: BLE001 —— 取异常本身不该再抛
            return
        if exc is not None:
            log.warning("[WPP] 好感度记账失败（不影响收信，%s）: %s",
                        task.get_name(), exc)

    def _mood_channel_prompt(self, acct: accounts_mod.Account,
                             msg: WppMessage) -> str | None:
        """当前情绪 → Hermes ``MessageEvent.channel_prompt``（缝见 plan Context 缝 1）。

        ⚠️ **有意与 TS 不齐（而且必须写在注释里）**：TS 把 moodNote 前置进 **Body**
        （``dispatcher.ts:864-866``），本实现走 Hermes 自己的 ``channel_prompt`` 缝 ——
        ``gateway/platforms/event.py:80`` 的原话是 "per-channel ephemeral system prompt;
        applied at API call time, **never persisted to transcript**"，正是这条注入想要
        的语义（情绪说明不该变成"用户说过的话"进历史）。**逐字保留的是内容**
        （含 ``[系统提示-当前情绪]`` 前缀），不是搬运通道。

        只在**群消息 + 账号开了 affection** 时取（TS 同：``isGroupMsg && affection.enabled``）
        ⇒ 没有群、没开开关时**一次 DB 都不碰**。取不到/解析失败一律回 ``None``：
        调用方不传这个关键字参数 ⇒ 请求体与"没这个功能"时**逐字节相同**。
        """
        if not msg.is_group:
            return None
        cfg = resolve_affection_cfg(acct.affection)
        if not cfg.enabled:
            return None
        try:
            from ..data import repo_affection

            # 注意：本方法是**同步**方法（调用它的协程才会阻塞），所以这里不能 await。
            # 单次 get_mood 读放大有限；要彻底 offload 需要把本方法连同调用链改成 async。
            mood = repo_affection.get_mood(acct.id, msg.chatroom_id, int(time.time()))
            text = build_mood_system_prompt("", mood).strip()
            if not text:
                return None
            return f"[系统提示-当前情绪] {text}"
        except Exception as exc:  # noqa: BLE001 —— 情绪注入失败不阻塞（TS 同款 try/catch）
            log.debug("[WPP:%s] 情绪注入失败（照常回复）: %s", acct.id, exc)
            return None

    def _group_profile_channel_prompt(self, acct: accounts_mod.Account,
                                      msg: WppMessage) -> str | None:
        """本群画像 → Hermes ``MessageEvent.channel_prompt``（回复侧）。

        2026-10-18 之前画像只进 judge：它决定"要不要说"，回复正文却看不到画像 ——
        agent 用的是通用人设。这里把同一份画像（同源同缓存，≤400 字）补进回复侧
        的临时 system prompt，且**不进 transcript**（Hermes ``channel_prompt`` 契约）。

        三道闸都在 IO 之前：非群消息 / 心流关着 / 画像缓存未命中 ⇒ ``None`` ——
        此时请求体与"没有这个功能"**逐字节相同**。画像缓存由 sweep 与启动预热灌满，
        本方法**不查 DB**（入站热路径每条消息都会走到这里）。
        """
        if not getattr(msg, "is_group", False):
            return None
        cfg = acct.heartflow if isinstance(acct.heartflow, dict) else {}
        if not cfg.get("enabled"):
            return None
        try:
            from ..inbound.heartflow import profile as hf_profile

            return hf_profile.build_hf_profile_channel_prompt(
                hf_profile.get_hf_profile_prompt_text(acct.id, msg.chatroom_id))
        except Exception as exc:  # noqa: BLE001 —— 画像注入失败不阻塞消息
            log.debug("[WPP:%s] 群画像注入失败（照常回复）: %s", acct.id, exc)
            return None

    def _jargon_channel_prompt(self, acct: accounts_mod.Account,
                               msg: WppMessage) -> str | None:
        """当前消息命中的已知群黑话 → 临时 system prompt（回复侧）。

        只读 ``jargon._term_meanings`` 进程内缓存，**热路径零 DB**。三道闸都在缓存读
        之前：非群消息 / 黑话开关关着 / 缓存里没有该群含义 ⇒ ``None``。
        """
        if not getattr(msg, "is_group", False):
            return None
        try:
            cfg = jargon_mine.resolve_jargon_cfg(acct.jargon)
            if not cfg.enabled:
                return None
            group_id = msg.peer_id
            return jargon_mod.build_jargon_channel_prompt(group_id, msg.content or "")
        except Exception as exc:  # noqa: BLE001 —— 注入失败不阻塞消息
            log.debug("[WPP:%s] 黑话注入失败（照常回复）: %s", acct.id, exc)
            return None

    def _style_channel_prompt(self, acct: accounts_mod.Account,
                              msg: WppMessage) -> str | None:
        """本群已学说话风格 → 临时 system prompt（回复侧）。

        只读 ``style_learn`` 进程内缓存，**热路径零 DB**。三道闸都在缓存读之前：
        非群消息 / ``style.enabled`` 关着 / 缓存里没有该群或置信度不够 ⇒ ``None``。
        """
        if not getattr(msg, "is_group", False):
            return None
        try:
            from ..inbound import style_learn as style_mod

            cfg = style_mod.resolve_style_cfg(acct.style)
            if not cfg.enabled:
                return None
            group_id = msg.peer_id
            return style_mod.build_style_channel_prompt(
                acct.id, group_id, min_confidence=cfg.min_confidence)
        except Exception as exc:  # noqa: BLE001 —— 注入失败不阻塞消息
            log.debug("[WPP:%s] 风格注入失败（照常回复）: %s", acct.id, exc)
            return None

    def _channel_prompt(self, acct: accounts_mod.Account,
                        msg: WppMessage) -> str | None:
        """情绪 + 本群画像 + 已知黑话 + 说话风格 → **一个** ``channel_prompt``。

        Hermes 这条缝只收一个字符串。所有段都为空 ⇒ ``None``：保持"没开这些功能时
        请求体逐字节相同"的既有判据。
        """
        parts: list[str] = []
        mood = self._mood_channel_prompt(acct, msg)
        if mood:
            parts.append(mood)
        profile = self._group_profile_channel_prompt(acct, msg)
        if profile:
            parts.append(profile)
        jargon = self._jargon_channel_prompt(acct, msg)
        if jargon:
            parts.append(jargon)
        style = self._style_channel_prompt(acct, msg)
        if style:
            parts.append(style)
        quote = self._quote_channel_prompt(acct, msg)
        if quote:
            parts.append(quote)
        return "\n\n".join(parts) if parts else None

    def _quote_channel_prompt(self, acct: accounts_mod.Account,
                              msg: WppMessage) -> str | None:
        """引用提示（SpectreCore「模型自主引用」的轻量版，``spectre.quoteHint``，默认关）。

        能力本来就有（``wpp_quote_reply`` 工具 + Hermes 的 ``reply_to_text``），缺的只是
        **让模型知道它可以自己决定引用**。默认关：多一段 prompt 就是多一次行为漂移，
        老板要开才开。引用内容只取前 60 字，且压成一行（防止 prompt 注入式换行）。
        """
        if not bool(acct.spectre.get("quoteHint")):
            return None
        quoted = msg.reply_to or {}
        if not quoted:
            return None
        text = str(quoted.get("quote_content") or "").strip().replace("\n", " ")[:60]
        if not text:
            return None
        who = str(quoted.get("display_name") or "").strip()
        if acct.self_wxid and quoted.get("from_wxid") == acct.self_wxid:
            hint = f"对方引用了你说过的话「{text}」。你可以直接回应，也可以用 wpp_quote_reply 引用回复。"
        else:
            hint = (
                f"本条消息引用了{' ' + who if who else '另一条消息'}的内容「{text}」。"
                "需要时可用 wpp_quote_reply 引用回复，不必每条都引用。"
            )
        return hint

    def _intent_llm_cfg(self, acct: accounts_mod.Account) -> IntentLlmConfig | None:
        """账号 → LLM 意图那一半的配置；``llm_intent_enabled`` 关着 ⇒ ``None``。

        ⚠️ **默认值有意与 TS 相反**：TS ``resolveLlmIntentEnabled``（``dispatcher.ts:162-168``）
        是 ``cfg?.llmIntentEnabled !== false`` —— **缺键算开**；py2 的 ``llm_intent_enabled``
        缺键算 **False**（``accounts.py:420``）。保留 py2 的保守一侧：这条链上多走一次
        外呼（LLM + embedding）都是**花钱**的路径，默认不花。

        解析本体在 :func:`..inbound.intent.resolve_intent_llm_cfg`（TS 那两个 resolver 的家），
        这里只负责"账号开关 → 有没有这半条链"。
        """
        if not acct.llm_intent_enabled:
            return None
        return resolve_intent_llm_cfg(acct.raw, nickname=acct.nickname)

    async def _intent_channel_context(self, acct: accounts_mod.Account,
                                      msg: WppMessage) -> str | None:
        """群聊背景 → Hermes ``MessageEvent.channel_context``（缝见 plan Context 缝 2）。

        ``gateway/platforms/event.py:82`` 的注释写明它 ``kept out of text so run.py's
        sender-prefix logic sees only the trigger message``，消费点
        ``gateway/run_inbound.py:1465-1466`` 把它前置进请求体 ⇒ 与 TS「把群聊上下文前置」
        （``dispatcher.ts:469-477``）**同形**，所以这条**不是** W2 那种有意偏离。

        总闸 = ``group_context_enabled``（TS 默认 **false**，``dispatcher.ts:154-158``），
        且**必须在任何 IO 之前**判：关着时一次 DB、一次外呼都不碰，``channel_context``
        为 ``None`` ⇒ 请求体与"没这个功能"逐字节相同（与 W2 的零副作用判据同款）。

        ⚠️ **有意没移植的一截**：TS 在查库**之前**先短路"触发消息里带引用"这条
        （``dispatcher.ts:331-340``：只注入被引用的那条，不再做 embedding/LLM 选择）。
        py2 的引用走**另一条已有的通道** —— ``_build_event`` 把引用内容填进
        ``reply_to_text``/``reply_to_author_name``，由 Hermes 自己呈现给模型。两条通道
        都在，再叠一层"引用即指定"会让同一句话进两次请求体。**不移植，不是漏了。**
        """
        if not msg.is_group:
            return None
        if not acct.group_context_enabled:
            return None
        try:
            from ..data import repo_messages

            content = msg.content or ""
            kind = classify_group_intent(content)
            if kind == "no-op":
                # TS 同样在查库**之前**判（``dispatcher.ts:338-342``）⇒ 空 @ 不白查一次库。
                return None
            ts = int(msg.timestamp or 0) or int(time.time())
            # ⚠️ pymysql 是**同步**的：直接在收信协程里查库会卡住整个事件循环
            #   （同进程的其他账号/心跳/WS 一起停顿）。DB 调用一律进线程池。
            rows = await asyncio.to_thread(
                repo_messages.list_group_messages,
                acct.id, msg.peer_id,
                limit=resolve_intent_window(acct.raw), before_sec=ts,
            )
            if not rows:
                return None
            kept = await select_intent_context(
                rows, trigger_content=content, intent=kind,
                llm_cfg=self._intent_llm_cfg(acct),
                embed_cfg=resolve_embedding_creds(acct.raw),
            )
            if kept is None:                      # None = "不注入"，不是"注入空"
                return None
            text = build_group_context_lines(kept)
            if text:
                log.debug("[WPP:%s] 群聊上下文注入 %d 条（意图 %s）", acct.id, len(kept), kind)
            return text
        except Exception as exc:  # noqa: BLE001 —— 注入失败不阻塞（TS 同款 try/catch）
            log.warning("[WPP:%s] 群聊上下文注入失败（照常回复）: %s", acct.id, exc)
            return None

    def _hf_note_bot_reply(self, account_id: str, peer_id: str, text: str) -> None:
        """出站记录 bot 自己的群发言（见 ``heartflow.note_bot_reply``）。

        必须走**出站**这条路径，不能等 vendor 回推：回推那条的发送者字段是 bot 自己，
        入站第 ① 步就丢了（TS 那边同样是 ``blocked``）。少了它，judge 眼里的
        "上次机器人回复"永远是空的 —— 而"接不接话"这个判断一半靠它（I6）。
        """
        hf = self._hf_mod()
        if hf is None:
            return
        try:
            hf.note_bot_reply(account_id, peer_id, text)
        except Exception as exc:  # noqa: BLE001
            log.debug("[WPP:%s] 心流 bot 发言记录失败（不影响发送）: %s", account_id, exc)

    def _hf_pre_send(self, account_id: str, peer_id: str, text: str) -> Any:
        """交付前置（见 ``heartflow.pre_send``）。取不到心流 ⇒ ``None`` = 照常发。"""
        hf = self._hf_mod()
        if hf is None:
            return None
        try:
            return hf.pre_send(account_id, peer_id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s] 心流发送前置失败（照常发）: %s", account_id, exc)
            return None

    def _hf_note_sent(self, account_id: str, peer_id: str, text: str,
                      result: SendResult) -> None:
        """交付后置：把 ``platform.SendResult`` **显式翻译**成 ``learn.HfSendResult``。

        ⚠️ 翻译不能省（见 ``heartflow.note_sent`` 的 docstring）：形状不匹配的表现是
        "消息发出去了但账本永远 pending"，既不占预算也不开窗，且日志里一句错都没有。
        """
        hf = self._hf_mod()
        if hf is None:
            return
        try:
            hf.note_sent(account_id, peer_id, text,
                         success=bool(result.success), message_id=result.message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s] 心流发送后置失败（不影响发送结果）: %s", account_id, exc)

    async def _build_event(self, acct: accounts_mod.Account, msg: WppMessage,
                           pipeline: InboundPipeline, *,
                           media: tuple[list[str], list[str]],
                           reply_expected: bool | None = None) -> MessageEvent | None:
        """把 :class:`WppMessage` 变成 Hermes :class:`MessageEvent`。

        ⚠️ **顺序有讲究：先取媒体，再算正文**。语音的转写就是在取媒体那一步顺出来的
        （``msg.media["stt_text"]``，见 :meth:`_fill_voice_transcript`），
        ``prepare_text`` 要把它读进正文 —— 反过来写，语音的转写永远赶不上这班车，
        而现场表现只是"语音没转写"（看起来像 STT 坏了，其实只是顺序反了）。

        ``media`` 由调用方（``_handle_one`` 的 **④c**）传入，**本方法不再自己取**
        —— 全链路只允许有**一个** ``_fetch_media`` 调用点（见 ④c 的长注释：那次调的
        主要产物不是 ``media_urls``，而是被写进 ``msg.content`` 的那行 OSS URL，
        而它的三个消费者全都排在 ⑤ **之前**）。写成必填关键字参数是为了让"忘了传"
        在**调用点**就炸，而不是悄悄多下多传一遍。
        """
        media_urls, media_types = media
        text = pipeline.prepare_text(msg)

        # 群名 / 昵称：v1 入站帧经常只带 wxid，WebUI 里就只剩 ``xxx@chatroom``
        # 和 ``wxid_...``。这里查本地缓存（群名来自 ``/Group/List`` 回填，
        # 群成员昵称来自 ``wpp_chatroom_members``）把可读名字补上；查不到
        # （DB 未起、冷启动）就保持旧行为，绝不影响消息派发。
        chat_name = msg.chatroom_id or msg.from_nickname or msg.peer_id
        user_name = msg.from_nickname or msg.from_wxid
        try:
            from ..inbound import session_title as _titles
            chat_name = _titles.chat_display_name(
                acct.id, msg.peer_kind, msg.peer_id,
                user_id=msg.from_wxid, fallback=msg.from_nickname or "",
            )
            user_name = _titles.sender_display_name(
                acct.id, msg.peer_id, is_group=msg.is_group,
                user_id=msg.from_wxid, fallback=msg.from_nickname or "",
            )
        except Exception as exc:  # noqa: BLE001 —— 名字只影响展示，不能挡住消息
            log.debug("[WPP:%s] 会话显示名解析失败，沿用 wxid: %s", acct.id, exc)

        source = self.build_source(
            chat_id=self._chat_id(acct.id, msg.peer_id),
            chat_name=chat_name,
            chat_type="group" if msg.is_group else "dm",
            user_id=msg.from_wxid,
            user_name=user_name,
            message_id=msg.msg_id,
        )
        quoted = msg.reply_to or {}
        return MessageEvent(
            text=text,
            message_type=_msg_type_to_event_type(msg),
            user_id=msg.from_wxid,
            user_name=user_name,
            source=source,
            raw_message=msg.raw,
            message_id=msg.msg_id,
            media_urls=media_urls,
            media_types=media_types,
            # W2 情绪注入 + 2026-10-18 本群画像注入：Hermes 的"临时 system prompt"缝
            # （**不进 transcript**）。两段都取不到 ⇒ None ⇒ 与旧请求体逐字节相同。
            # 有意的通道偏离见 :meth:`_mood_channel_prompt` 的 docstring；
            # 画像进回复侧的理由见 :meth:`_group_profile_channel_prompt`。
            channel_prompt=self._channel_prompt(acct, msg),
            # W3 群聊背景：Hermes 的"随请求体前置、不进 text"缝（缝见 plan Context 缝 2）。
            # 总闸 group_context_enabled 关着 ⇒ None ⇒ 与旧请求体逐字节相同。
            # ⚠️ 候选池要的是**库里已有的**群消息，而这条消息在 `_handle_one` 的第 ④a 步
            # 就已经落库了（``persist_inbound`` 在 ``_build_event`` **之前**，:611/:635）
            # ⇒ 把"当前这条"排除掉**只能靠** ``before_sec``，不是可有可无的保险。
            channel_context=await self._intent_channel_context(acct, msg),
            reply_to_message_id=quoted.get("svrid") or None,
            reply_to_text=quoted.get("quote_content") or None,
            # 心流主动搭话传 False（静默合法）；@ 提及 / 私聊传 None（保留可见兜底）。
            # 语义见 Hermes ``MessageEvent.reply_expected`` 与 run_turn_persistence
            # 的 ``silence_allowed``：None 和 True 一样会生成兜底文案。
            reply_expected=reply_expected,
            reply_to_author_id=quoted.get("from_wxid") or None,
            reply_to_author_name=quoted.get("display_name") or None,
            reply_to_is_own_message=bool(acct.self_wxid
                                         and quoted.get("from_wxid") == acct.self_wxid),
        )

    # ================================================================ 出站

    async def send(self, chat_id: str, content: str, reply_to: str | None = None,
                   metadata: dict | None = None) -> SendResult:
        """发消息。``chat_id`` 形如 ``accountId:peerId``（见 :meth:`_chat_id`）。

        ★ 微信通道**只发结果，不发过程**（老板 2026-10-18 拍板）：Hermes 会用
        ``_interim_send`` 标记「这一条不是本轮最终答复」——流式中间稿、后台自审摘要、
        「⏳ Working — N min」心跳、卡住告警、网关关停提示……全带这个标记。微信没有
        状态栏、消息发出**撤不回**，这些过程话术一律静默丢弃。

        唯一例外是**审批提示**（``is_approval_prompt``）：它是回合继续下去的必要
        交互，不是进度播报；丢了会让工具一直等到超时。
        """
        # 归一化一次，后面不再重复 ``content or ""``（同一表达式写过 11 遍，
        # 每遍都给 CC 加 1 —— 复杂度指标被重复归一化灌水，掩盖真实分支）。
        content = content or ""
        metadata = metadata or {}
        if metadata.get("_interim_send") and not metadata.get("is_approval_prompt"):
            log.info("[WPP] 丢弃过程消息（_interim_send，只发最终结果）: %r",
                     content[:80])
            return SendResult(success=True, message_id=None)
        # 拒绝话术**静默丢弃**（agent 偶发输出"无 @提及，按群规则不回复"类话术）
        if outbound.is_rejection_phrase(content):
            log.warning("[WPP] 丢弃拒绝话术（未发出）: %r", content[:60])
            return SendResult(success=True, message_id=None)
        # 网关内部机械通知**静默丢弃**（静默兜底英文报错 / 💾 自审摘要，2026-10-18 事故）
        if outbound.is_machinery_notice(content):
            log.warning("[WPP] 丢弃内部机械通知（未发出）: %r", content[:80])
            return SendResult(success=True, message_id=None)

        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
            acct = self.account(account_id)

            # 出站 @self 闸（2026-10-02）：模型会把上下文里的「@本账号昵称」原样抄进
            # 回复 ⇒ 群里显示成"自己 @ 自己"（老板当场指出「@ 的对象也不对」）。
            # 判据在 ``outbound.strip_self_mention``。放在这里 = 分片 / 引用前缀 /
            # 心流记账 **全都看到同一份最终文本**（否则账本记的是没发出的那句）。
            cleaned = outbound.strip_self_mention(content, acct.self_wxid, acct.nickname)
            if cleaned != content:
                log.info("[WPP:%s] 出站摘掉指向自己的 @: %r → %r",
                         acct.id, content[:60], cleaned[:60])
                content = cleaned
                if outbound.is_rejection_phrase(content):
                    # 上面那道早退按**原文**判；摘掉 @ 前缀才现形的拒绝话术在这里补一道。
                    log.warning("[WPP] 丢弃拒绝话术（未发出）: %r", content[:60])
                    return SendResult(success=True, message_id=None)
                if outbound.is_machinery_notice(content):
                    # 同上补一道：@ 前缀摘掉才现形的内部通知（如 ``@张三 ⚠️ The model...``）。
                    log.warning("[WPP] 丢弃内部机械通知（未发出）: %r", content[:80])
                    return SendResult(success=True, message_id=None)

            client = self._clients.get(acct.id)
            if client is None:
                return SendResult(success=False, error=f"账号 {acct.id} 未连接",
                                  error_kind="transient")

            # 出站白名单闸（未设 env 时是 no-op）；用 connect 时捕获的**本账号**那份，
            # 不在发送任务里重新读 ambient env（见 __init__ 的 _send_allowlists）。
            outbound.assert_outbound_allowed(peer_id, acct.id,
                                             allowed=self._allowlist_for(acct))

            # 心流交付缝（**前置**）：只有"刚判定通过、正在等这一条回复"的群才有登记，
            # 其余（含所有私聊、所有非心流群消息）是一次内存查表后返回 None。
            plan = self._hf_pre_send(account_id, peer_id, content)
            if plan is not None and plan.placeholder:
                # 重复内容闸拦下 ⇒ **静默不发**。账本已按 suppressed 收敛
                # （不占预算 / 不开窗 / 不进重复历史，INVARIANT I5），这里只需把"成功"
                # 还给调用方 —— 报失败会让 runner 重试同一条插话直到天亮。
                log.info("[WPP:%s] 心流插话与近期发言重复，静默不发: %s", acct.id, chat_id)
                return SendResult(success=True, message_id=None,
                                  raw_response={"hfSuppressed": plan.placeholder})

            chunks = outbound.chunk_text(content)
            last_id: str | None = None
            for idx, chunk in enumerate(chunks):
                result = await self._send_chunk(
                    acct, client, peer_id, chunk,
                    reply_to=reply_to if idx == 0 else None,
                    metadata=metadata,
                )
                if not result.success:
                    # ★ B5（2026-10-02 修）：**部分发出 ≠ 没发**。
                    #   多片消息第 1 片成功、第 2 片失败时，旧写法把失败结果原样交给
                    #   ``_hf_note_sent`` ⇒ 判成 pending ⇒ 账本不动（等框架重试）。
                    #   可群里**已经有半句了**（红线 3：微信撤不回），于是：不占额度、
                    #   不开观察窗、不进复读历史，而那行台账停在 ``judged`` 等 sweep
                    #   收敛成"没人接话"。重试还会把第 1 片**再发一遍**。
                    #   ``last_id is None`` = 一片都没出去 ⇒ 才是真的"没发"，维持原样。
                    if last_id is None:
                        self._hf_note_sent(account_id, peer_id, content, result)
                        return result
                    log.warning("[WPP:%s] 多片发送中途失败：前 %d 片已发出（不可撤回）"
                                "⇒ 按部分发出记账: %s", acct.id, idx, result.error)
                    # ★ 2026-10-19：把部分投递写进 Hermes 的契约字段。没有它，
                    #   ``_send_with_retry`` 会把整条消息重发一遍（前 idx 片重复）。
                    #   有了 partial_overflow，框架只会尝试续传；本适配器不实现
                    #   ``_resume_partial_send`` ⇒ 直接保留部分失败，不会重发已见头部。
                    raw = dict(result.raw_response) if isinstance(
                        result.raw_response, dict) else {}
                    raw.update({"partial_overflow": True,
                                "sent_chunks": idx,
                                "sent_message_id": last_id})
                    result.raw_response = raw
                    # 账本只认 ``ok``/``msg_id``（见 ``_hf_note_sent``），故这里给一个
                    # "成功发出前几片"的形状；**只记交付缝，不记 bot 上下文** ——
                    # ``note_bot_reply`` 的 docstring 说了整句没出现就不许记，
                    # 半句记成整句会让 judge 以为"我上次已经说过了"。
                    self._hf_note_sent(account_id, peer_id, content,
                                       SendResult(success=True, message_id=last_id))
                    return result
                last_id = result.message_id or last_id
            sent = SendResult(success=True, message_id=last_id)
            self._hf_note_bot_reply(account_id, peer_id, content)
            self._hf_note_sent(account_id, peer_id, content, sent)
            return sent

        except (ValueError, outbound.netguard.GuardError) as exc:
            log.error("[WPP] 发送被拒: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind="forbidden")
        except WppApiError as exc:
            # ★ 超时单独分类：Hermes 的 ``_is_timeout_error`` 只认英文
            #   ``timed out/readtimeout/writetimeout``，而本客户端的超时文案是中文
            #   「超时」⇒ 原先落进 transient 被自动重试。超时语义是「可能已送达」，
            #   重试就是重复发送（微信撤不回）。这里补英文前缀，让框架走
            #   「不重试、不 plain fallback」那条分支。
            error_text = str(exc)
            if getattr(exc, "code", None) == "timeout" or "超时" in error_text:
                log.error("[WPP] 发送超时（结果未知，禁止自动重试）: %s", error_text)
                return SendResult(success=False,
                                  error=f"timed out: {error_text}",
                                  error_kind="unknown")
            log.error("[WPP] 发送失败: %s", error_text)
            return SendResult(success=False, error=error_text, error_kind="transient")
        except Exception as exc:
            log.error("[WPP] 发送异常: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), error_kind="unknown")

    async def send_or_update_status(
        self, chat_id: str, status_key: str, content: str, *,
        metadata: dict | None = None,
    ) -> SendResult:
        """微信**没有状态栏**：生命周期状态（记忆召回 / 压缩 / 模型切换…）一律不落聊天。

        Hermes 的 :func:`gateway.run._send_or_update_status_coro` 会优先调本方法
        （Telegram/Slack 用它编辑同一条状态气泡）。本适配器声明"不支持"：直接吞掉。
        这是一道**兜底**——即便将来网关新增某种状态文案，也不会漏进微信群。
        """
        log.debug("[WPP] 丢弃生命周期状态 %s: %r", status_key, (content or "")[:60])
        return SendResult(success=True, message_id=None)

    async def send_private_notice(
        self, chat_id: str, user_id: str | None, content: str,
        reply_to: str | None = None, metadata: dict | None = None,
    ) -> SendResult:
        """运营/告警类**私密通知**：群里只发结果，通知改投管理员私聊。

        网关的 ``_deliver_platform_notice`` 在 ``notice_delivery: private`` 时先调本方法。
        基类默认就是"照常发到原会话"，在微信群 = 把内部告警刷给客户看。这里改成：
        原会话是群 ⇒ 投给 ``adminUsers`` 里第一个非本账号的 wxid；私聊/无管理员目标
        ⇒ 维持原路（私聊本来就是安全面）。
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
            acct = self.account(account_id)
        except Exception:  # noqa: BLE001 —— 解析不了就按原路走，别把通知彻底吞掉
            return await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)
        if not peer_id.endswith(outbound.GROUP_SUFFIX):
            return await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)
        target = next((u for u in acct.admin_users if u and u != acct.self_wxid), "")
        if not target:
            log.warning("[WPP:%s] 群通知没有管理员私聊目标，改发原群: %s", acct.id, peer_id)
            return await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)
        log.info("[WPP:%s] 群通知改投管理员私聊 %s（原群 %s）", acct.id, target, peer_id)
        return await self.send(self._chat_id(account_id, target), content, metadata=metadata)

    async def _send_chunk(self, acct: accounts_mod.Account, client: WppClient, peer_id: str,
                          chunk: str, *, reply_to: str | None,
                          metadata: dict | None) -> SendResult:
        """发**单片**。

        引用回复走 **``/Msg/ShareLink`` + ``Type=5`` + ``Xml``**（迁移自旧
        ``_send_quote``）—— **不是** ``send_app_message``（那个是结构化卡片，
        收的是 ``items`` 列表，形态完全不同）。``type=57`` 的 ``refermsg`` XML
        由 :func:`outbound.build_quote_reply_xml` 生成。

        ⚠️ **前缀只贴第一片**（``chunk`` 已是分片后的正文）：``_quote_prefix`` 排在这里
        （而不是 :meth:`send`）有两条硬理由 —— ① ``@`` 只能出现一次，贴在分片循环里
        就是"每片都 @ 一次"；② :meth:`send` 里那句 ``strip_self_mention`` 之后的内容
        才是最终文本，而 ``_hf_note_*`` / :meth:`_persist_outbound` 记的是**不带前缀**
        的原文（与 TS 一致：``persistQuoteReply`` / ``recordHeartflowBotReply`` 都收
        原始 ``content``）—— 记账里多一个 ``@张三`` 会让复读判据与 quote 回填都错位。
        """
        if reply_to:
            # 去掉可能的 ``accountId:`` 前缀 —— svrid 只是消息 id
            svrid = reply_to.split(":", 1)[1] if ":" in reply_to else reply_to
            # ★ 被回复人身份**回查 DB**（2026-10-03 修）—— 旧树 ``_send_quote`` 的那一截，
            #   Hermes 移植时丢了 ⇒ ``fromusr`` 恒空 ⇒ 群里的 ``@被回复人`` 从来没出现过。
            #   详见 ``inbound/quote_ref.py`` 的模块 docstring（含"为什么 metadata 那条路
            #   在 wechatpadpro 上永远是空的"）。
            ident = await self._quote_identity(acct, svrid)
            # metadata 那一档**保留在最前**：它是 TS ``params.fromWxid`` 的对应物
            #   （dispatcher 手上现成的触发消息身份），现在才算真的有消费者。
            fromusr = (metadata or {}).get("quote_from_wxid") or ident.fromusr
            prefix = await self._quote_prefix(acct, client, peer_id, fromusr, metadata,
                                              db_name=ident.display_name)
            quote_text = f"{prefix}{chunk}".strip()
            if outbound.quote_reply_fits(quote_text):
                xml = outbound.build_quote_reply_xml(
                    reply_content=quote_text, svrid=svrid, fromusr=fromusr)
                resp = await client.call("/Msg/ShareLink",
                                         {"ToWxid": peer_id, "Type": 5, "Xml": xml})
            else:
                # ★ 2026-10-18 修复：引用气泡只渲染前 500 字符（vendor 同样按 500
                #   slice）⇒ 长回复走 ShareLink 会被**静默砍半**，且发送仍返回成功。
                #   超限时改走普通文本；正文本身已由 chunk_text 按段落/上限切好，
                #   这里只丢掉"引用气泡"，不丢字。@ 前缀保留为普通文本前缀。
                log.info("[WPP:%s] 引用回复正文 %d 字超引用气泡上限 %d ⇒ 改发普通文本",
                         acct.id, len(quote_text), outbound.QUOTE_REPLY_MAX_CHARS)
                # 前缀会额外占几个字：若 chunk 本身已顶到 2000，拼上前缀可能再超
                # 普通文本上限 ⇒ 这里再按平台上限分一次（通常只有一片）。
                resp: dict = {}
                sent_pieces = 0
                for piece in outbound.chunk_text(quote_text):
                    resp = await client.send_text(to_wxid=peer_id, content=piece)
                    if not outbound.send_ok(resp):
                        if sent_pieces:
                            # 前几片已发出（引用气泡降级后的普通文本），不能再让框架
                            # 整条重发 —— 与 send() 的多片失败同一契约。
                            raw = dict(resp) if isinstance(resp, dict) else {"resp": resp}
                            raw.update({"partial_overflow": True,
                                        "sent_chunks": sent_pieces})
                            return SendResult(success=False,
                                              error=outbound.send_error_text(resp),
                                              error_kind="unknown", raw_response=raw)
                        break
                    sent_pieces += 1
        else:
            resp = await client.send_text(to_wxid=peer_id, content=chunk)

        if not outbound.send_ok(resp):
            return SendResult(success=False, error=outbound.send_error_text(resp),
                              error_kind="unknown", raw_response=resp)
        ids = outbound.extract_outbound_msg_ids(resp)
        await self._persist_outbound(acct, peer_id, chunk, ids)
        return SendResult(success=True, message_id=ids.msg_id, raw_response=resp)

    async def _quote_identity(self, acct: accounts_mod.Account,
                              svrid: str) -> quote_ref.QuoteIdentity:
        """按锚点 id 回查被引用消息，拿"是谁被回复了"（``fromusr`` + 该行的 ``peer_name``）。

        **永不抛**：查不到、库读不了都只是少一个 ``@`` —— 昵称是装饰，发送才是正事
        （与 :meth:`_contact_name` / :meth:`_resolve_display_name` 同一条口径）。

        ⚠️ 这里**只**解决"是谁"，不碰 svrid —— 引用 XML 的目标 id 仍用 Hermes 给的锚点
        （``_reply_anchor_for_event`` → ``event.message_id`` = 触发消息的 ``msg_id``）。
        TS 那边 ``svrid = newMsgId || msgId`` 更偏好服务端 id，但旧树同样用行里的
        ``msg_id``，两者在"帧里两个 id 都有"时落在同一个值上 ⇒ **不动**，免得把"引用
        能不能锚上"这件目前正常的事拿去赌一把（本项只修 ``@``）。
        """
        try:
            from ..data import repo_messages

            row = await asyncio.to_thread(
                repo_messages.get_message_by_id_or_new_id,
                acct.id, msg_id=svrid, new_msg_id=svrid)
        except Exception as exc:  # noqa: BLE001 —— DB 不可用只降级，不许挡住发送
            log.debug("[WPP:%s] 引用锚点回查降级 %s: %s", acct.id, svrid, exc)
            return quote_ref.QuoteIdentity()
        ident = quote_ref.resolve_quote_identity(row)
        if ident.fromusr:
            log.info("[WPP:%s] 引用锚点命中: %s → 被回复人 %s（行内昵称 %s）",
                     acct.id, svrid, ident.fromusr, ident.display_name or "无")
        elif ident.matched:
            log.warning("[WPP:%s] 引用锚点 %s 命中但取不到发送者 ⇒ 群聊里不会 @ 任何人",
                        acct.id, svrid)
        else:
            log.debug("[WPP:%s] 引用锚点 %s 未命中 DB（未落库/已过期）⇒ 不 @", acct.id, svrid)
        return ident

    async def _quote_prefix(self, acct: accounts_mod.Account, client: WppClient,
                            peer_id: str, fromusr: str, metadata: dict | None,
                            *, db_name: str = "") -> str:
        """群聊引用回复的 ``@被回复人 `` 前缀（**仅群聊**才有）。

        ``display_name`` 五级优先（第 1/2 级对齐旧 ``_send_quote`` 与 TS
        ``send/quote-reply.ts`` 的 ``displayName``；第 3/4 级是本树的本地化补充）：

        1. ``metadata.quote_display_name`` —— 微信引用消息自带，最准（群友也准）
        2. **被引用消息行的 ``peer_name``**（``db_name``，TS 的
           ``paramsFromNicknameFallback``）
        3. **DB 联系人昵称**（``repo_messages.get_contact`` 的 ``nickname``/``remark``）
        4. vendor ``/Friend/GetContractDetail``（群友非联系人也能查）
        5. ``wxid`` 兜底

        ⚠️ **2026-10-03 更正一句旧注释**：这里原来写着"第 2 级改成查联系人表，语义等价
        且更省"—— 那是**错的**。真机上（``真机``）``wpp_contacts`` / ``wpp_chatrooms`` /
        ``wpp_chatroom_members`` **全是 0 行**（没人同步过通讯录），第 2 级实际上永远是空
        的；而能让 ``fromusr`` 有值的那一档（回查被引用消息）当时根本不存在 ⇒ 整条链
        一次都没跑通过。现在把"行内 ``peer_name``"补回第 2 级（它是**被引用消息自己带的**
        昵称，比通讯录更贴当下），联系人表退到第 3 级。

        形状（``"@张三 "`` / ``""``）由 :func:`inbound.quote_ref.build_mention_prefix`
        产出 —— 那是**唯一**产出点，别在本方法里另写一遍字符串拼接。
        """
        # 成本闸：非群 / 拿不到人时**一次查询都不发**（下面 3/4 级会打通讯录与厂商）。
        # 语义上的"只群聊才有"在 build_mention_prefix 里还有一道，两道都要在：
        # 这一道省外呼，那一道保证格式永远对。
        if not outbound.is_group_peer(peer_id) or not fromusr:
            return ""
        name = (metadata or {}).get("quote_display_name") or ""
        if not name or name == fromusr:
            name = str(db_name or "").strip()
        if not name or name == fromusr:
            name = await asyncio.to_thread(self._contact_name, acct.id, fromusr)
        if not name or name == fromusr:
            name = await self._resolve_display_name(acct, client, fromusr)
        if not name or name == fromusr:
            # 到这儿说明通讯录与厂商都没给出昵称 ⇒ 现场表现就是 ``@wxid_xxx``。
            # 出声一次：这条日志是"@ 出来的是 wxid 而不是名字"的唯一观测点。
            log.info("[WPP:%s] 查不到 %s 的昵称（联系人表与厂商都没给）⇒ @ 用 wxid 兜底",
                     acct.id, fromusr)
            name = fromusr
        return quote_ref.build_mention_prefix(peer_id, name, fromusr)

    @staticmethod
    def _contact_name(account_id: str, wxid: str) -> str:
        """第 3 级：本地联系人表查昵称。查不到返空串（**不抛** —— 昵称只是装饰）。"""
        try:
            from ..data import repo_messages

            row = repo_messages.get_contact(account_id, wxid)
        except Exception as exc:  # noqa: BLE001 —— DB 不可用时降级到下一级
            log.debug("[WPP:%s] 联系人查询降级 %s: %s", account_id, wxid, exc)
            return ""
        if not isinstance(row, dict):
            return ""
        return str(row.get("remark") or row.get("nickname") or "").strip()

    @staticmethod
    def _vendor_nick(data: Any) -> str:
        """从 ``/Friend/GetContractDetail`` 的 ``Data`` 里抠昵称。取不到返空串。

        **真实形状**（旧树 ``plugin/adapter.py::_resolve_display_name`` 与 TS
        ``src/send/quote-reply.ts:82-88`` **两处独立实现给的是同一个**）::

            Data.ContactList[0].NickName.string

        Go 侧把每个字段包成 ``{"string": …}`` —— **必须解包**（``parser._unwrap`` 干的就是
        这件事）。旧写法 ``data.get("NickName")`` 有两个洞，都是静默的：

        * 多包一层 ``ContactList`` ⇒ 顶层**永远没有** ``NickName`` ⇒ 一次都取不到昵称
          （现场表现就是 ``@wxid_xxx``，而不是报错）；
        * 就算命中，``str({"string": "张三"})`` 得到的是 ``"{'string': '张三'}"``
          （Python 的 dict 字面量）—— 一个**不是名字的名字**，会被原样 @ 到群里。

        兜底顺序（都是"取到就用"，取不到就下一档）：``ContactList[0].NickName`` →
        ``ContactList[0].Remark`` → 扁平 ``Data.NickName`` / ``Remark``。
        """
        from ..inbound.parser import _safe_str

        if not isinstance(data, dict):
            return ""
        contact_list = data.get("ContactList")
        if isinstance(contact_list, list) and contact_list:
            first = contact_list[0]
            if isinstance(first, dict):
                for key in ("NickName", "Remark", "nickName"):
                    found = _safe_str(first.get(key))
                    if found:
                        return found
        for key in ("NickName", "nickName", "nickname", "Remark"):
            found = _safe_str(data.get(key))
            if found:
                return found
        return ""

    async def _resolve_display_name(self, acct: accounts_mod.Account, client: WppClient,
                                    wxid: str) -> str:
        """第 4 级：vendor 查昵称（30 分钟缓存）。取不到返回 ``wxid`` 本身。

        ⚠️ 取不到时**也把 wxid 缓存 30 分钟**（负缓存）：这条链在群聊里每条引用回复都会
        走一遍，不缓存就等于每句回复多打一次厂商接口。
        """
        if not wxid or not quote_ref.is_usable_sender(wxid):
            return ""
        cached = self._name_cache.get(f"{acct.id}:{wxid}")
        if cached and time.time() - cached[1] < 1800:
            return cached[0]
        name = wxid
        try:
            detail = await client.get_contract_detail(wxid)
            data = detail.get("Data") if isinstance(detail, dict) else None
            found = self._vendor_nick(data)
            if found:
                name = found
        except Exception as exc:  # noqa: BLE001 —— 查不到昵称不该挡住发送
            log.debug("[WPP:%s] 查昵称失败 %s: %s", acct.id, wxid, exc)
        # getattr: 测试会绕过 __init__ 直接构造适配器；上限不是业务状态，缺失时用默认值。
        cap = getattr(self, "_name_cache_max", 2048)
        if len(self._name_cache) >= cap:
            now = time.time()
            for stale in [k for k, (_v, at) in self._name_cache.items()
                          if now - at >= 1800]:
                self._name_cache.pop(stale, None)
            while len(self._name_cache) >= cap:
                self._name_cache.pop(next(iter(self._name_cache)), None)
        self._name_cache[f"{acct.id}:{wxid}"] = (name, time.time())
        return name

    async def _persist_outbound(self, acct: accounts_mod.Account, peer_id: str,
                                content: str, ids: outbound.OutboundMsgIds) -> None:
        """落库出站记录（群友引用 bot 消息时需要 DB 有这一条）。

        ⚠️ 两个 id 的取法照搬蓝本 ``src/send/msg.ts:121``：
        ``msg_id = newMsgId ?? msgId``、``new_msg_id = newMsgId``
        —— **服务端 id 优先**。群友引用 bot 消息时带的是 ``svrid``（≙ ``newMsgId``），
        落成客户端 id 就永远匹配不上（这也是 2026-10-01 之前 ``wpp_messages.msg_id``
        全是 NULL 的那条线的一截）。

        **失败只 warn** —— 审计写不进去不该让已发出的消息算发送失败。

        ⚠️ 2026-10-02（W1）修掉两个真 bug，**都与上面那半共用一对映射函数**：

        * ``peer_kind`` 原来写 ``"dm"``，而列是 ENUM('direct','group','room')
          ⇒ **私聊出站记录一条都写不进去**（1265，被下面的 except 吞成一条 warn）；
          现在走 :func:`inbound.persist.db_peer_kind`。
        * ``chat_id`` 原来写 ``f"{account_id}:{peer_id}"``，而
          ``repo_messages.list_group_messages`` 查的是 ``chat_id = <裸群 id>``
          ⇒ 出站群消息**对"按群查历史"整个隐形**（两个分支都不命中）。
          现在走 :func:`inbound.persist.db_chat_id`（群聊写裸 id、私聊 NULL）。

        落库本体在模块级 :func:`_persist_outbound_record` —— cron 的
        ``_standalone_send`` 也走它，两条发送路径不再分叉。
        """
        await _persist_outbound_record(acct, peer_id, content, ids)

    async def persist_tool_outbound(self, account_id: str, endpoint: str,
                                    body: dict, resp: Any) -> None:
        """通用端点工具 / ``wpp_send_message`` 的出站落库（非致命）。

        对齐 TS 的 ``dispatch(..., persist=true)``：**所有**发送类端点（13 个）
        成功即入库。``send_ok`` 已经是唯一成败判据（含 ``Data.List[].Ret``），
        失败/被拒收的发送绝不写幽灵记录。
        """
        meta = outbound.outbound_meta_for(endpoint, body)
        if not meta or not outbound.send_ok(resp):
            return
        try:
            acct = self.account(account_id)
        except Exception as exc:  # noqa: BLE001 —— 账号刚被移除/名称不匹配
            log.debug("[WPP] 出站落库跳过（账号 %s 不可用）: %s", account_id, exc)
            return
        await _persist_outbound_record(
            acct, meta["toWxid"], meta["content"],
            outbound.extract_outbound_msg_ids(resp),
            msg_type=meta["msgType"], raw_payload=resp)

    async def persist_chat_outbound(self, account_id: str, peer_id: str,
                                    msg_type: str, content: str, resp: Any) -> None:
        """已按业务语义算出正文的发送路径（``wpp_send_message`` / 媒体）落库。"""
        if not outbound.send_ok(resp):
            return
        try:
            acct = self.account(account_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("[WPP] 出站落库跳过（账号 %s 不可用）: %s", account_id, exc)
            return
        await _persist_outbound_record(
            acct, peer_id, content, outbound.extract_outbound_msg_ids(resp),
            msg_type=msg_type, raw_payload=resp)

    # ------------------------------------------------------- 媒体覆写（非抽象）

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """微信**没有** typing 指示 —— 显式 no-op。

        不能删这个方法：基类 ``_keep_typing`` 会循环调它（``base.py:3350``），
        默认实现可能打日志刷屏。这里静默即可。
        """
        return None

    async def send_image(self, chat_id: str, image_url: str, caption: str | None = None,
                         reply_to: str | None = None,
                         metadata: dict | None = None) -> SendResult:
        """发图（URL / 本地路径 / base64 均可）。"""
        return await self._send_media(chat_id, image_url, "image", caption, metadata,
                                     reply_to=reply_to)

    async def send_image_file(self, chat_id: str, image_path: str, caption: str | None = None,
                              reply_to: str | None = None,
                              metadata: dict | None = None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, image_path, "image", caption, metadata,
                                     reply_to=reply_to)

    async def send_voice(self, chat_id: str, audio_path: str, caption: str | None = None,
                         reply_to: str | None = None, metadata: dict | None = None,
                         **kwargs) -> SendResult:
        return await self._send_media(chat_id, audio_path, "voice", caption, metadata,
                                     reply_to=reply_to)

    async def send_video(self, chat_id: str, video_path: str, caption: str | None = None,
                         reply_to: str | None = None, metadata: dict | None = None,
                         **kwargs) -> SendResult:
        return await self._send_media(chat_id, video_path, "video", caption, metadata,
                                     reply_to=reply_to)

    async def send_document(self, chat_id: str, file_path: str, caption: str | None = None,
                            file_name: str | None = None, reply_to: str | None = None,
                            metadata: dict | None = None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, file_path, "file", caption, metadata,
                                     file_name=file_name, reply_to=reply_to)

    # ============================================================ 会话信息

    async def get_chat_info(self, chat_id: str) -> dict:
        """取会话信息（``name`` + ``type`` 必填）。"""
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
        except ValueError as exc:
            return {"name": chat_id, "type": "unknown", "error": str(exc)}
        return {
            "name": peer_id,
            "type": "group" if outbound.is_group_peer(peer_id) else "dm",
            "account_id": account_id,
            "chat_id": chat_id,
        }

    # ================================================================ 内部

    def _extra(self) -> dict:
        extra = getattr(self.config, "extra", None)
        return extra if isinstance(extra, dict) else {}

    def _pipeline_cfg(self, acct: accounts_mod.Account) -> dict:
        """给 :class:`InboundPipeline` / ``should_process`` 的配置视图。"""
        paired: list[str] = []
        if acct.dm_policy == "pairing" or acct.dm_pairing_enabled:
            try:
                from . import pairing

                paired = pairing.load_paired(acct.id)
            except Exception as exc:  # noqa: BLE001 —— 读配对名单失败不阻塞收信
                log.warning("[WPP:%s] 读取已配对名单失败: %s", acct.id, exc)
        return {
            "selfWxid": acct.self_wxid,
            "nickname": acct.nickname,
            "dmPolicy": acct.dm_policy,
            "allowFrom": list(acct.allow_from),
            "blacklistUsers": list(acct.blacklist_users),
            "pairedUsers": paired,
            "groupPolicy": acct.group_policy,
            "groupAllowFrom": list(acct.group_allow_from),
            "blacklistGroups": list(acct.blacklist_groups),
            "adminUsers": list(acct.admin_users),
            # ★ 之前漏了这一行：``triggers.should_process`` 读 cfg["allowAllUsers"]，
            #   而访问策略层读的是 acct.raw —— 两处口径都在，唯独 pipeline 视图
            #   把键丢了 ⇒ 配了 allowAllUsers=True 的账号在消息触发层仍被名单挡住。
            #   只认布尔 True（字符串 "true"/1 不算，见 access_policy 的判据）。
            "allowAllUsers": acct.raw.get("allowAllUsers") is True,
            "msgTypeTrigger": acct.raw.get("msgTypeTrigger") or {},
            "keywordTrigger": acct.raw.get("keywordTrigger") or {},
            "quoteBotTrigger": acct.raw.get("quoteBotTrigger") or {},
            "heartflow": acct.heartflow,
        }

    @staticmethod
    def _chat_id(account_id: str, peer_id: str) -> str:
        """``chat_id`` 编码：``accountId:peerId``（多账号下必须带账号）。"""
        return f"{account_id}:{peer_id}"

    def _parse_chat_id(self, chat_id: str) -> tuple[str, str]:
        """``chat_id`` 解码。不带 ``:`` ⇒ 落到默认账号；账号未知 ⇒ **raise**（不静默兜底）。"""
        raw = (chat_id or "").strip()
        if not raw:
            raise ValueError("chat_id 为空")
        account_id, sep, peer_id = raw.partition(":")
        if not sep:
            account_id, peer_id = DEFAULT_ACCOUNT, raw
        if not peer_id:
            raise ValueError(f"chat_id 缺 peerId: {chat_id!r}")
        if account_id not in self._accounts:
            raise ValueError(f"未知账号 {account_id!r}（chat_id={chat_id!r}）")
        return account_id, peer_id


# ============================================================ 模块级入口
# registration.py 需要这四个符号。


def _build_adapter(config: Any) -> WppAdapter:
    """``adapter_factory`` 目标（**单参** ``PlatformConfig``）。

    同时记住实例供 :func:`_get_adapter`（工具侧）取用。
    """
    global _CURRENT_ADAPTER
    adapter = WppAdapter(config)
    _CURRENT_ADAPTER = adapter
    return adapter


def _get_adapter() -> WppAdapter:
    """取当前适配器（工具 handler 用）。

    ⚠️ 工具可能在适配器**尚未构造**时被调用（``WPP_TOOLS_ONLY=1`` 的纯工具模式）
    ⇒ 这里优先返回实例；没有实例但有 ``WPP_TOOLS_ONLY`` 时构造一个**不连接**的
    实例（工具自己建 client）。
    """
    if _CURRENT_ADAPTER is not None:
        return _CURRENT_ADAPTER
    if os.environ.get("WPP_TOOLS_ONLY") == "1":
        log.info("[WPP] tools-only 模式：构造未连接的适配器实例")
        return _build_adapter(_NullConfig())
    raise RuntimeError(
        "[WPP] 适配器尚未构造。若只要用工具，请设 WPP_TOOLS_ONLY=1；"
        "否则确认平台已在 config.yaml 的 plugins.enabled 里启用。")


class _NullConfig:
    """tools-only 模式的空配置（``extra`` 为空 dict）。"""

    enabled = True
    token = None
    extra: dict = {}


def check_wechatpadpro_requirements() -> bool:
    """**被动依赖探针**（``PlatformEntry.check_fn``）。

    契约要求**无副作用**（会被 status 显示反复调用）⇒ 只做 ``import`` 检查，
    不装包、不写文件、不打网络。

    只探**传输层**两条（``websockets`` / ``aiohttp``）：

    - ``pymysql`` **故意不探** —— DB 不可用时适配器降级但仍能收信（``_run_migrations``
      只 warn）。把它算进 ``check_fn`` 会让"DB 没装好"变成"平台起不来"，
      正是 F1 那种"看起来正常但已经不工作了"的失败模式。
    """
    for mod in ("websockets", "aiohttp"):
        try:
            __import__(mod)
        except ImportError:
            log.error("[WPP] 缺依赖 %s —— 平台无法接入（install_hint 见 plugin.yaml）", mod)
            return False
    return True


# ------------------------------------------------------------------ standalone 发送
#
# 这一段原来全在 ``_standalone_send`` 里（157 行 / CC 46）：四个 kind 的发送是
# if/elif 叠在一起的，加一种 kind 就要再往那个大函数里塞一条分支。现在拆成
# 「每个 kind 一个函数 + 一张分派表 + 三个阶段函数」，每块都能单独测。


async def _send_image_media(client, peer_id, path, name, b64):
    """图片：UploadImg 上传即发送。"""
    return await client.send_image(to_wxid=peer_id, image_base64=b64)


async def _send_voice_media_standalone(client, peer_id, path, name, b64):
    """语音：silk 编码 + 专用上传（复用 live 的 ``_send_voice_media``）。"""
    return await WppAdapter._send_voice_media(client, peer_id, path, name, b64)


async def _send_video_media(client, peer_id, path, name, b64):
    """视频：抽一帧当缩略图；抽不到也照发（vendor 可能拒收，只告警）。"""
    thumb = await WppAdapter._video_thumb(path)
    if not thumb:
        log.warning("[WPP] standalone 视频无缩略图（vendor 可能拒收）: %s", name)
    return await client.send_video(to_wxid=peer_id, video_base64=b64, thumb_base64=thumb)


async def _send_file_media(client, peer_id, path, name, b64):
    """文件：``send_file_v2``（v1 缺 appID/attachId 那条路已废）。"""
    return await client.send_file_v2(to_wxid=peer_id, file_name=name, file_base64=b64)


#: kind → 发送实现。``outbound.media_kind`` 只产 image/voice/video/file；
#: 未知 kind 按文件发（"发错类型" 好过 "发不出去"）。
_STANDALONE_SENDERS = {
    "image": _send_image_media,
    "voice": _send_voice_media_standalone,
    "video": _send_video_media,
    "file": _send_file_media,
}


def _parse_media_item(item: Any) -> tuple[str, bool] | None:
    """``[(路径, is_voice), …]`` 或裸字符串 → ``(path, is_voice)``；解析不了返 None。"""
    if isinstance(item, str):
        return item, False
    try:
        return str(item[0] or ""), bool(item[1])
    except (TypeError, ValueError, IndexError):
        log.warning("[WPP] standalone 跳过无法解析的媒体项: %r", item)
        return None


def _partial_or_error(err: str, sent: int, message_id: str | None,
                      media_sent: int | None = None) -> dict:
    """统一的失败上报：已经发出过内容时**必须**标 partial（微信撤不回）。

    文本段失败原来不带 ``sent_media``、附件段带 —— 用 ``media_sent is None``
    区分，保证返回的 key 集合与旧行为逐字一致。
    """
    if not (sent or (media_sent or 0)):
        return {"error": err}
    out = {"error": err, "partial": True, "sent_chunks": sent,
           "message_id": message_id}
    if media_sent is not None:
        out["sent_media"] = media_sent
    return out


def _resolve_standalone_profile_home(acct: Any) -> str | None:
    """进程外 cron 没有 adapter 的 connect 期捕获 ⇒ 按 profile 目录直接找。

    否则 ``platform_gate_env`` 会回落根 ``.env``（2026-10-19 07:36 事故）。
    """
    try:
        from agent.secret_scope import current_secret_scope_home
        home = current_secret_scope_home()
    except Exception:  # noqa: BLE001
        home = None
    if home:
        return home
    root_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    candidates = []
    if os.environ.get("HERMES_PROFILE"):
        candidates.append(os.path.join(root_home, "profiles",
                                       os.environ["HERMES_PROFILE"]))
    candidates.append(os.path.join(root_home, "profiles", acct.agent))
    return next((c for c in candidates if os.path.isdir(c)), None)


async def _standalone_send_text(client: Any, acct: Any, peer_id: str,
                                chunks: list[str]) -> tuple[str | None, int, dict | None]:
    """发文本分片，返回 ``(message_id, sent_chunks, error)``。

    多片中途失败时前几片**已经发出**（微信撤不回）⇒ 必须按 partial 上报，
    否则调用方重试会把第 1 片再发一遍（live 的 ``send()`` 早就这么记）。
    """
    message_id: str | None = None
    sent = 0
    for chunk in chunks:
        resp = await client.send_text(to_wxid=peer_id, content=chunk)
        if not outbound.send_ok(resp):
            if sent:
                log.warning("[WPP] standalone 多片发送中途失败：前 %d 片已发出"
                            "（不可撤回）⇒ 按部分发出上报", sent)
            return message_id, sent, _partial_or_error(
                outbound.send_error_text(resp), sent, message_id)
        sent += 1
        ids = outbound.extract_outbound_msg_ids(resp)
        message_id = ids.msg_id or message_id
        # 与 live ``_send_chunk`` 同一套落库 —— 否则 cron 投递在 wpp_messages
        # 里隐形，事后无法核"到底发没发/发给谁"。
        await _persist_outbound_record(acct, peer_id, chunk, ids)
    return message_id, sent, None


async def _standalone_send(pconfig: Any, chat_id: str, message: str, *,
                           thread_id: str | None = None,
                           media_files: list | None = None,
                           force_document: bool = False) -> dict:
    """进程外发送（cron 无并发 gateway 时用）。

    契约：``dict``，成功 ``{"success": True, "message_id": ...}``，
    失败 ``{"error": str}``。

    ⚠️ 旧实现**泄漏 WppClient**（从不 close）—— 这里用 ``async with`` 保证回收。
    """
    # 带附件时**不许**因为文案像拒绝话术就整条丢掉 —— 附件是调用方明确要投递的，
    # 文案被过滤掉只是"别发这句话"，不是"别发这条消息"（见下方二次判定）。
    has_media = bool(media_files)
    if not has_media and outbound.is_rejection_phrase(message):
        return {"success": True, "message_id": None}
    # 与 live ``send()`` 同一道：内部机械通知（静默兜底报错 / 💾 自审摘要）绝不进群。
    if not has_media and outbound.is_machinery_notice(message):
        return {"success": True, "message_id": None}

    account_id, peer_id = DEFAULT_ACCOUNT, chat_id
    if ":" in (chat_id or ""):
        account_id, peer_id = chat_id.split(":", 1)

    try:
        extra = getattr(pconfig, "extra", None) or {}
        all_accounts = accounts_mod.load_all(extra.get("accounts"))
        acct = all_accounts.get(account_id)
        if acct is None:
            return {"error": f"未知账号 {account_id!r}（已加载：{sorted(all_accounts)}）"}
        refusal = accounts_mod.refusal_reason(acct)
        if refusal:
            return {"error": f"账号 {acct.id} 不可连接：{refusal}"}

        # 出站 @self 闸 —— 与 live ``send()`` 同一道（cron 路径不许绕过）
        message = outbound.strip_self_mention(message or "", acct.self_wxid, acct.nickname)
        if outbound.is_rejection_phrase(message):
            if not has_media:
                return {"success": True, "message_id": None}
            message = ""      # 只丢这句文案；下面照常发附件
        if outbound.is_machinery_notice(message):
            if not has_media:
                return {"success": True, "message_id": None}
            message = ""

        profile_home = _resolve_standalone_profile_home(acct)
        # 与 live 路径同一份统一白名单（收发共用）：allowFrom / groupAllowFrom /
        # adminUsers ∪ env 额外项。不能只读 env，否则 standalone 又会退化成
        # 「联系人甲能私聊、cron 却发不出去」。
        send_allowlist = outbound.netguard.unified_allowlist(
            profile_home=profile_home, accounts=(acct,))
        outbound.assert_outbound_allowed(peer_id, acct.id, allowed=send_allowlist)
        async with WppClient(base_url=acct.api_base_url, authcode=acct.authcode,
                             account_id=acct.id, timeout=30,
                             send_allowlist=send_allowlist) as client:
            chunks = outbound.chunk_text(message)
            if not chunks and not has_media:
                # ★ 什么都发不出去时**不许**回 success（``message_id=None``）——
                #   调用方会把它记成"已投递"，实际群里一条都没有。空输入是调用方
                #   的问题，如实报错（带附件时允许文本为空）。
                return {"error": "空消息：没有可发送的文本或附件"}

            message_id, sent, err = await _standalone_send_text(
                client, acct, peer_id, chunks)
            if err:
                return err

            # ── 附件（2026-10-19）─────────────────────────────────────────
            # Hermes cron/send_message 的 ``standalone_sender_fn`` 契约里
            # ``media_files`` 是 ``[(路径, is_voice), …]`` —— 旧实现签名收了却**完全
            # 没消费**：模型生成的图片/文件被静默丢弃，调用方还收到 success
            # （等于"发了"的假账）。这里按扩展名走与 live ``_send_media`` 同一条链路。
            media_sent = 0
            for item in (media_files or []):
                parsed = _parse_media_item(item)
                if parsed is None:
                    continue
                path, is_voice = parsed
                if not path:
                    continue
                kind = outbound.media_kind(path, is_voice=is_voice,
                                           force_document=force_document)
                b64 = await outbound.resolve_to_base64(path)
                if not b64:
                    return _partial_or_error(f"媒体内容不可用: {path[:120]}",
                                             sent, message_id, media_sent)
                name = os.path.basename(path) or f"{kind}.bin"
                resp = await _STANDALONE_SENDERS.get(kind, _send_file_media)(
                    client, peer_id, path, name, b64)
                if not outbound.send_ok(resp):
                    log.warning("[WPP] standalone 附件中途失败：已发 %d 段文本 + %d 个附件"
                                "（不可撤回）⇒ 按部分发出上报", sent, media_sent)
                    return _partial_or_error(outbound.send_error_text(resp),
                                             sent, message_id, media_sent)
                media_sent += 1
                ids = outbound.extract_outbound_msg_ids(resp)
                message_id = ids.msg_id or message_id
                marker = {"image": "[图片]", "voice": "[语音]",
                          "video": "[视频]"}.get(kind, f"[文件] {name}")
                await _persist_outbound_record(acct, peer_id, marker, ids,
                                               msg_type=kind, raw_payload=resp)

            return {"success": True, "message_id": message_id, "media_sent": media_sent}
    except Exception as exc:
        log.error("[WPP] standalone 发送失败: %s", exc, exc_info=True)
        return {"error": str(exc)}


__all__ = [
    "DEFAULT_ACCOUNT",
    "MAX_MESSAGE_LENGTH",
    "PLATFORM_NAME",
    "WppAdapter",
    "_build_adapter",
    "_get_adapter",
    "_standalone_send",
    "check_wechatpadpro_requirements",
]
