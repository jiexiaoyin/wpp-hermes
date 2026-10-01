"""WeChatPadPro 平台适配器 —— Hermes ``BasePlatformAdapter`` 的实现。

旧 ``adapter.py``（1900 行）的功能等价物，但按 Hermes 0.21.5 的**真实契约**重写。
本模块是 ``registration._register_platform`` 的 ``adapter_factory`` 目标。

## 实测契约（每条都在 .183 的 Hermes 源码里核过，不是照着文档猜的）

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
约 2 个 fd 且永远重试（这解释了 .183 上"平台永不启用"里的资源抖动）。

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
import base64
import logging
import os
import time
from typing import Any, Optional

# ── Hermes 侧（插件只在 Hermes 进程内被 import，这些必然可用）
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from ..core import access_policy
from ..core import accounts as accounts_mod
from ..core.api_client import WppApiError, WppClient
from ..inbound import (FILEHELPER, InboundPipeline, WppMessage, is_bot_mentioned_by_text,
                       is_filehelper_session, is_quoting_bot)
from ..inbound.hongbao import is_card_message, process_red_packet
from . import outbound, silk_encoder
from .inbound_webhook import WppWebhookServer
from .inbound_ws import WppWsClient

log = logging.getLogger(__name__)

#: 平台名 —— 必须与目录名 / ``config.yaml plugins.enabled`` 的键 / toolset 名**三者一致**
PLATFORM_NAME = "wechatpadpro"

#: 未指定账号时的兜底
DEFAULT_ACCOUNT = "default"

#: 单条消息上限（**与 registry 的 max_message_length 同值**，见模块 docstring ②）
MAX_MESSAGE_LENGTH = outbound.MAX_MESSAGE_LENGTH

#: 去重窗口：vendor 会重放（WS 断线重连 / webhook 补推）⇒ 5 分钟足够
_DEDUP_TTL_SECONDS = 300.0
_DEDUP_MAX_SIZE = 5000

#: ``should_process`` 里这些 reason 表示「**彻底丢**」，连心流都不进
_DROP_REASONS = frozenset({
    "red_packet", "revoke", "official_account", "self",
    "group_blacklist", "group_disabled", "group_closed", "group_not_allowlisted",
    "dm_not_allowlisted", "dm_fail_closed",
})

#: 当前进程内的适配器实例（工具侧经 :func:`_get_adapter` 取用）
_CURRENT_ADAPTER: Optional["WppAdapter"] = None


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
        return Platform._add_pseudo_member(PLATFORM_NAME)  # noqa: SLF001 —— 无公开等价 API


def _msg_type_to_event_type(msg: WppMessage) -> MessageType:
    """WPP ``msg_type`` → Hermes :class:`MessageType`。

    ``MT_APP``(49) 归 TEXT —— 卡片正文已被厂商译成文字摘要（红包→"微信红包"、
    接龙→列表文本），对 agent 就是文本。
    """
    from ..inbound.parser import (
        MT_CARD, MT_EMOJI, MT_IMAGE, MT_LOCATION, MT_VIDEO, MT_VOICE,
    )

    return {
        MT_IMAGE: MessageType.PHOTO,
        MT_VOICE: MessageType.VOICE,
        MT_VIDEO: MessageType.VIDEO,
        MT_EMOJI: MessageType.STICKER,
        MT_CARD: MessageType.DOCUMENT,
        MT_LOCATION: MessageType.LOCATION,
    }.get(int(msg.msg_type or 0), MessageType.TEXT)


class WppAdapter(BasePlatformAdapter):
    """多账号 WeChatPadPro 适配器（一个实例管多个账号）。"""

    #: 见模块 docstring ②——基类靠 ``getattr`` 读它
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: Any, platform: Optional[Platform] = None) -> None:
        super().__init__(config, platform if platform is not None else _resolve_platform())

        self._accounts: dict[str, accounts_mod.Account] = {}
        self._clients: dict[str, WppClient] = {}
        self._ws: dict[str, WppWsClient] = {}
        self._pipelines: dict[str, InboundPipeline] = {}
        self._webhook: Optional[WppWebhookServer] = None
        #: sweep 句柄 —— ``disconnect()`` **必须**逐个 cancel（否则 reload 累积孤儿任务，R9）
        self._sweep_timers: list[asyncio.Task] = []
        #: 昵称缓存（引用回复用），{wxid: (name, at)}
        self._name_cache: dict[str, tuple[str, float]] = {}

        # ★ 坑③：去重器**必须**是 MessageDeduplicator 且挂命名属性 —— runner 靠
        #   isinstance 找它并在重连时 carry_inbound_dedup 重灌。自己写 set 会在重连后变空。
        self._dedup = MessageDeduplicator(max_size=_DEDUP_MAX_SIZE, ttl_seconds=_DEDUP_TTL_SECONDS)

        #: Hermes 多路复用用 ``token`` 做**冲突检测**（同一凭据被两个 profile 用会互相踢）。
        #: 取排序后第一个账号的凭据指纹 —— 确定性的，不含明文（见 core.accounts.fingerprint）。
        self.token = ""

    # ================================================================== 属性

    @property
    def accounts(self) -> dict[str, accounts_mod.Account]:
        """当前已加载账号（只读视图）。"""
        return dict(self._accounts)

    def account(self, account_id: str = "") -> accounts_mod.Account:
        """取账号；空 ⇒ 默认账号。取不到 raise（**不静默兜底**，否则会串号发到别人）。"""
        aid = account_id or DEFAULT_ACCOUNT
        acct = self._accounts.get(aid)
        if acct is None:
            raise ValueError(
                f"[WPP] 账号 {aid!r} 不存在（已加载：{sorted(self._accounts)}）")
        return acct

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
            self._run_migrations()

            self._accounts = accounts_mod.load_all(self._extra().get("accounts"))
            connectable = [a for a in self._accounts.values()
                           if accounts_mod.refusal_reason(a) is None]

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
                )
                self._pipelines[acct.id] = InboundPipeline(acct.id, self._pipeline_cfg(acct))

            if not self._start_webhook(connectable):
                # webhook 起不来不是致命的（WS 仍是主通道），但要说清楚
                log.warning("[WPP] webhook 未启动（WS 通道不受影响）")

            for acct in connectable:
                self._start_ws(acct)

            self._start_sweeps(connectable)

            self._mark_connected()
            log.info("[WPP] 平台已连接：%d 个账号 %s（is_reconnect=%s）",
                     len(self._clients), sorted(self._clients), is_reconnect)
            return True

        except accounts_mod.AccountError as exc:
            # 配置错误（如 agent 字段误配）—— 重试无用
            log.error("[WPP] 账号配置错误，不再重试: %s", exc, exc_info=True)
            self._set_fatal_error("account_config", str(exc), retryable=False)
            return False
        except Exception as exc:  # noqa: BLE001 —— 未知异常按**可重试**处理
            log.error("[WPP] connect 失败（将交给 runner 重试）: %s", exc, exc_info=True)
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False

    async def disconnect(self) -> None:
        """停 WS / webhook / sweep，关 client。**必须幂等**（runner 可能重入）。"""
        # ① sweep 先停 —— 否则它会在 client 关闭后继续打 DB/网络（R9 孤儿任务）
        for task in self._sweep_timers:
            task.cancel()
        if self._sweep_timers:
            await asyncio.gather(*self._sweep_timers, return_exceptions=True)
        self._sweep_timers.clear()

        # ② WS
        for aid, ws in list(self._ws.items()):
            try:
                await ws.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP:%s] WS 停止时忽略异常: %s", aid, exc)
        self._ws.clear()

        # ③ webhook
        if self._webhook is not None:
            self._webhook.stop()
            self._webhook = None

        # ④ vendor client（旧实现会泄漏，这里显式关）
        for aid, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP:%s] client 关闭时忽略异常: %s", aid, exc)
        self._clients.clear()
        self._pipelines.clear()

        self._mark_disconnected()
        log.info("[WPP] 平台已断开")

    # ---------------------------------------------------------------- 启动子步

    def _run_migrations(self) -> None:
        """跑 DB migration。**失败只 warn，不阻断接入**（DB 坏了不该连微信都上不去）。"""
        try:
            from ..data import db

            if not db.is_available():
                log.warning("[WPP] DB 不可用（%s）—— 收信照常，心流/审计降级",
                            db.describe())
                return
            result = db.migrate()
            log.info("[WPP] DB 就绪: %s", result)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] DB migration 未完成（不影响收信，心流/审计会降级）: %s", exc)

    def _start_ws(self, acct: accounts_mod.Account) -> None:
        """起单个账号的 WS 客户端。WS 起不来不算致命（webhook 仍是通道）。"""
        if not acct.ws_url:
            log.warning("[WPP:%s] 未配 wsUrl，跳过实时通道（只剩 webhook）", acct.id)
            return
        try:
            ws = WppWsClient(
                account_id=acct.id, ws_url=acct.ws_url, authcode=acct.authcode,
                on_raw=lambda payload, a=acct: self._on_raw(a, payload),
            )
            ws.start()
            self._ws[acct.id] = ws
        except Exception as exc:  # noqa: BLE001
            log.error("[WPP:%s] WS 启动失败: %s", acct.id, exc, exc_info=True)

    def _start_webhook(self, connectable: list[accounts_mod.Account]) -> bool:
        """起共享 webhook server 并注册各账号路由。返回是否启动成功。"""
        routed = [a for a in connectable if a.webhook_path]
        if not routed:
            return False
        host = routed[0].webhook_host or "127.0.0.1"
        port = routed[0].webhook_port or 4398
        self._webhook = WppWebhookServer(host=host, port=port,
                                         loop=asyncio.get_running_loop())
        for acct in routed:
            self._webhook.add_path(
                acct.webhook_path,
                lambda payload, a=acct: self._on_raw(a, payload),
                secret=acct.webhook_secret,
            )
            if not acct.webhook_secret:
                log.warning("[WPP:%s] 未配 webhookSecret —— 仅靠路径 token 鉴权；"
                            "nginx 反代后公网可达，建议补上", acct.id)
        return self._webhook.start()

    def _start_sweeps(self, connectable: list[accounts_mod.Account]) -> None:
        """账号启动：**先预热三条缓存，再**起 300s 心流 sweep（每账号一个 task）。

        ⚠️ 心流九模块是计划第 6 步的产物 —— 未就位时**静默跳过**（记 debug），
        绝不让它成为接入的阻塞点。

        预热为什么不是 sweep 的内部事：judge 热路径零 DB IO（V7），读的全是内存缓存；
        而 sweep 是"先睡 300s 再跑"（对齐 TS ``setInterval``）—— 不预热的话，重启后
        头 5 分钟里阈值回落账号级、预算从 0 起算、分层统计全无（TS 在 ``startAccount``
        里同样补这三条）。顺序：**先预热再起任务**，否则首条消息仍可能撞上冷缓存。
        """
        try:
            from ..inbound.heartflow import runtime as hf_runtime
        except Exception:  # noqa: BLE001 —— 第 6 步之前必然 import 失败
            log.debug("[WPP] 心流模块未就位，sweep 未启动")
            return

        for acct in connectable:
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
                log.warning("[WPP:%s] sweep 启动失败: %s", acct.id, exc)

    # ================================================================ 入站

    async def _on_raw(self, acct: accounts_mod.Account, payload: dict) -> None:
        """WS 帧 / webhook payload 的统一入口。

        顺序（**不可调换**）：自消息过滤 → 去重 → 空消息体 → 构造事件 → ``handle_message``。
        """
        pipeline = self._pipelines.get(acct.id)
        if pipeline is None:
            log.warning("[WPP:%s] 收到消息但管线未就绪（未连接？），丢弃", acct.id)
            return

        for msg in pipeline.extract(payload):
            try:
                await self._handle_one(acct, msg, pipeline)
            except Exception as exc:  # noqa: BLE001 —— 单条坏消息不该打断整批
                log.warning("[WPP:%s] 处理消息 %s 失败: %s", acct.id, msg.msg_id, exc, exc_info=True)

    async def _handle_one(self, acct: accounts_mod.Account,
                          msg: WppMessage, pipeline: InboundPipeline) -> None:
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
            log.debug("[WPP:%s] 重复消息，丢弃 %s", acct.id, msg.msg_id)
            return

        # ③ 空消息体保护（既无正文又无媒体 —— vendor 的心跳噪声）
        if not (msg.content or "").strip() and not msg.media:
            log.debug("[WPP:%s] 空消息体，丢弃 %s", acct.id, msg.msg_id)
            return

        # ③b filehelper 管理命令 —— **必须**在 ④ 之前截住。
        #     为什么不能交给 ④ 之后处理：``inbound.triggers`` 的 ④ 硬门会把
        #     filehelper **判成 should_process=True**（它确实是给机器人看的消息），
        #     于是 ``/heartflow on`` 会被构造成事件喂给 LLM，机器人开始"回答"一条命令。
        #     判定与处置必须贴着走 —— 判据在 ``inbound.triggers.is_filehelper_session``。
        if await self._maybe_command(acct, msg):
            return

        # ④ 触发判定（硬门 + 四路 OR，见 inbound.triggers）
        ok, reason = pipeline.decide(msg)
        if is_card_message(msg) and reason == "red_packet":
            process_red_packet(msg)          # 只 log，不拆
        if reason in _DROP_REASONS:
            log.debug("[WPP:%s] 丢弃 %s（reason=%s）", acct.id, msg.msg_id, reason)
            return

        # ④a 心流旁路观测 —— 放行条件对齐 TS 的 ``via !== "blocked"``：**只记未被丢弃的群消息**。
        #     必须在 ④ 之后（否则黑名单/非白名单群的消息也进缓冲，"隐私"那条注释就成空话），
        #     但对**通过与否都记** —— 只在"触发了"时才喂，judge 看到的"最近 N 条上下文"
        #     就只剩 bot 自己那几条，等于给裁判看了一份伪造的现场记录。
        #     ⚠️ 比 TS 略严：红包 / 撤回 / 公众号也不进缓冲（它们不是对话内容）。
        if msg.is_group:
            self._hf_note_group(acct, msg, is_bot=False)

        if not ok:
            # 只剩 heartflow_candidate（其余 deny 已在 _DROP_REASONS 里被丢掉）
            if not await self._try_heartflow(acct, msg, pipeline):
                log.debug("[WPP:%s] 心流未接管 %s（reason=%s）", acct.id, msg.msg_id, reason)
                return
        else:
            log.info("[WPP:%s] 触发（%s）: peer=%s type=%s", acct.id, reason, msg.peer_id, msg.msg_type)

        # ⑤ 构造事件并交给 runner（**必须**走 handle_message，见 docstring ④）
        event = await self._build_event(acct, msg, pipeline)
        if event is None:
            return
        await self.handle_message(event)

    async def _maybe_command(self, acct: accounts_mod.Account, msg: WppMessage) -> bool:
        """filehelper 管理命令 → 就地回复并**终止**这条消息（返回 True = 已接管）。

        判定（:func:`inbound.triggers.is_filehelper_session`）与处置分在两处，是因为
        ④ 那道硬门只回答"该不该处理"，而 filehelper 的"处理方式"是**执行命令**，
        不是喂给 LLM（见 ``_handle_one`` 的 ③b 注释）。

        权限：**仅 ``adminUsers``**（旧实现的安全修复，原样保留）。旧实现对
        「``adminUsers`` 未配」是**放行**的（fail-open）；这里行为不变，但会吵一声 ——
        管理面命令（改白名单 / 发配对码）不该在无人看守时开放，配置漏了要看得见。

        回复目标：filehelper 会话就回 ``filehelper``（vendor 承认的合法收件人），
        不赌 ``msg.from_wxid`` —— 这个字段在 filehelper 会话里常被 vendor 标成
        **机器人自己**（见 :mod:`inbound.triggers` 第 ④ 步的说明）。
        """
        if not is_filehelper_session(msg):
            return False

        sender = msg.from_wxid or ""
        admin = tuple(acct.admin_users)
        if not admin:
            log.warning("[WPP:%s] filehelper 命令：账号未配 adminUsers ⇒ 放行（管理面裸露）", acct.id)
        elif sender and sender not in admin:
            # 也返回 True：拒绝**同样是接管**，漏给 LLM 等于把命令当闲聊回
            log.warning("[WPP:%s] filehelper 命令被拒: 非 admin %s", acct.id, sender)
            return True

        content = msg.content or ""
        try:
            from . import commands

            reply = commands.handle_command(acct.id, content)
        except Exception as exc:  # noqa: BLE001 —— 命令链路坏了不该影响别的消息
            log.warning("[WPP:%s] filehelper 命令处理失败: %s", acct.id, exc, exc_info=True)
            return True

        if reply:
            # 走 self.send：复用出站白名单闸 / 拒绝话术过滤 / 分段（别绕过它们）
            await self.send(self._chat_id(acct.id, FILEHELPER), reply)
            log.info("[WPP:%s] filehelper 命令已回复: %s", acct.id, content.split(None, 1)[0])
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
                           pipeline: InboundPipeline) -> Optional[MessageEvent]:
        """把 :class:`WppMessage` 变成 Hermes :class:`MessageEvent`。

        ⚠️ **顺序有讲究：先取媒体，再算正文**。语音的转写就是在取媒体那一步顺出来的
        （``msg.media["stt_text"]``，见 :meth:`_fill_voice_transcript`），
        ``prepare_text`` 要把它读进正文 —— 反过来写，语音的转写永远赶不上这班车，
        而现场表现只是"语音没转写"（看起来像 STT 坏了，其实只是顺序反了）。
        """
        media_urls, media_types = await self._fetch_media(acct, msg)
        text = pipeline.prepare_text(msg)

        source = self.build_source(
            chat_id=self._chat_id(acct.id, msg.peer_id),
            chat_name=msg.chatroom_id or msg.from_nickname or msg.peer_id,
            chat_type="group" if msg.is_group else "dm",
            user_id=msg.from_wxid,
            user_name=msg.from_nickname or msg.from_wxid,
            message_id=msg.msg_id,
        )
        quoted = msg.reply_to or {}
        return MessageEvent(
            text=text,
            message_type=_msg_type_to_event_type(msg),
            user_id=msg.from_wxid,
            user_name=msg.from_nickname or msg.from_wxid,
            source=source,
            raw_message=msg.raw,
            message_id=msg.msg_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=quoted.get("svrid") or None,
            reply_to_text=quoted.get("quote_content") or None,
            reply_to_author_id=quoted.get("from_wxid") or None,
            reply_to_author_name=quoted.get("display_name") or None,
            reply_to_is_own_message=bool(acct.self_wxid
                                         and quoted.get("from_wxid") == acct.self_wxid),
        )

    async def _fetch_media(self, acct: accounts_mod.Account,
                           msg: WppMessage) -> tuple[list[str], list[str]]:
        """把消息里的媒体下到本地缓存，返回 ``(media_urls, media_types)``。

        **best-effort**：下不下来都不该挡住这条消息（旧实现里下载超时会拖死整批）。
        图片必须下是因为**心流 judge 看图**（v1.11.0 vision）要的是本地路径。
        """
        from gateway.platforms.base import (
            cache_audio_from_bytes, cache_document_from_bytes,
            cache_image_from_bytes, cache_video_from_bytes,
        )
        from ..inbound.parser import MT_APP, MT_IMAGE, MT_VIDEO, MT_VOICE

        client = self._clients.get(acct.id)
        if client is None or not msg.media:
            return [], []

        mtype = int(msg.msg_type or 0)
        try:
            if mtype == MT_IMAGE and msg.media.get("cdn"):
                cdn = msg.media["cdn"]
                data = await client.download_image_cdn(
                    cdn.get("file_aes_key", ""), cdn.get("file_no", ""),
                    variant=cdn.get("variant", "") or "")
                if data:
                    return [cache_image_from_bytes(data, ext=".jpg")], ["image/jpeg"]

            if mtype == MT_VOICE:
                data = await self._download_voice(client, msg)
                if data:
                    # 转写要在返回**之前**写进 msg.media：正文由 prepare_text 稍后读它
                    await self._fill_voice_transcript(acct.id, msg.media, data)
                    # SILK 头是 b"\x02#"；其余当 AMR（vendor 只出这两种）
                    ext = ".silk" if data[:2] == b"\x02#" else ".amr"
                    return [cache_audio_from_bytes(data, ext=ext)], ["audio/silk"]

            if mtype == MT_VIDEO and msg.media.get("video_ctx"):
                data = await self._download_video_chunks(client, msg)
                if data:
                    return [cache_video_from_bytes(data, ext=".mp4")], ["video/mp4"]

            if mtype == MT_APP and msg.media.get("file_ctx"):
                ctx = msg.media["file_ctx"]
                data = await client.download_file_binary(
                    ctx.get("attach_id", ""),
                    user_name=ctx.get("user_name", ""),
                    data_len=int(ctx.get("data_len") or 0),
                    start_pos=int(ctx.get("start_pos") or 0),
                    section_len=int(ctx.get("section_len") or 0),
                    app_id=ctx.get("app_id", ""),
                    file_name=ctx.get("file_name", ""),
                )
                if data:
                    fname = ctx.get("file_name") or msg.media.get("filename") or "file.bin"
                    return [cache_document_from_bytes(data, fname)], ["application/octet-stream"]

        except (WppApiError, NotImplementedError, OSError) as exc:
            log.warning("[WPP:%s] 媒体下载失败（不阻塞消息）: %s", acct.id, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s] 媒体下载异常（不阻塞消息）: %s", acct.id, exc)
        return [], []

    @staticmethod
    async def _fill_voice_transcript(account_id: str, media: dict, silk_bytes: bytes) -> None:
        """把语音的文字写进 ``media["stt_text"]``（vendor 转写优先，缺了才自己转）。

        **没有它，语音在模型眼里只剩 ``[语音]`` 一行** —— 老板 @ 机器人发语音＝没说话，
        而现场日志一切正常（消息进来了、音频文件也落了），是最难查的那种失效。

        两条来源的顺序与取舍见 :func:`plugin.inbound.stt.should_attempt_stt`：
        vendor 已经转过就不再下载/调 API（省一次外呼，也更准）。
        降级（缺解码器 / 缺 key）**只喊一次**，见 :func:`plugin.inbound.stt.note_degradation_once`。
        """
        from ..inbound import stt

        if not stt.should_attempt_stt(media):
            log.debug("[WPP:%s] 语音用 vendor 转写（免 STT）", account_id)
            return

        reason = stt.unavailable_reason()
        if reason:
            stt.note_degradation_once(reason)
            return

        text = await stt.transcribe_silk_buffer(silk_bytes)
        if text:
            media["stt_text"] = text
            log.info("[WPP:%s] 语音转写成功（%d 字）", account_id, len(text))
        else:
            # 走到这说明依赖齐备但对方没给文字（空音频 / 5xx 到底）—— 留痕，别静默
            log.info("[WPP:%s] 语音转写未拿到文字 ⇒ 正文只有 [语音]", account_id)

    async def _download_voice(self, client: WppClient, msg: WppMessage) -> Optional[bytes]:
        """下语音。新 schema（``voice_ctx``）优先，老 schema（``legacy_cdn``）兜底。"""
        ctx = msg.media.get("voice_ctx")
        if ctx:
            return await client.download_voice_binary(
                int(ctx.get("msg_id") or 0),
                new_msg_id=str(ctx.get("new_msg_id") or ""),
                client_msg_id=str(ctx.get("client_msg_id") or ""),
                master_buf_id=str(ctx.get("master_buf_id") or "0"),
                fmt=int(ctx.get("format") or 4),
                length=int(ctx.get("length") or 0),
                chat_room_name=str(ctx.get("chat_room_name") or ""),
                from_user_name=str(ctx.get("from_user_name") or ""),
                to_user_name=str(ctx.get("to_user_name") or ""),
            )
        legacy = msg.media.get("legacy_cdn")
        if legacy:
            return await client.download_voice_legacy(
                legacy.get("aes_key", ""), legacy.get("file_no", ""))
        return None

    async def _download_video_chunks(self, client: WppClient, msg: WppMessage) -> Optional[bytes]:
        """**分片**下完整视频。

        ``/Tools/DownloadVideo`` 一次只给一片（默认 1MB），要 ``start_pos += len(chunk)``
        循环到 ``start_pos >= data_len``（旧 ``api_client`` 的 docstring 明确写了这点，
        但**没有任何调用方真的循环** —— 于是视频永远只拿到第一片）。
        """
        ctx = msg.media["video_ctx"]
        total = int(ctx.get("data_len") or 0)
        chunk_len = int(ctx.get("chunk_len") or 1048576) or 1048576
        if total <= 0:
            return None

        buf = bytearray()
        start = int(ctx.get("start_pos") or 0)
        # 安全上限：最多 4096 片（≈4GB），防 ``total`` 被 vendor 报成天文数字时死循环
        for _ in range(4096):
            piece = await client.download_video(
                to_wxid=ctx.get("to_wxid", ""), msg_id=int(ctx.get("msg_id") or 0),
                data_len=total, start_pos=start, chunk_len=chunk_len,
            )
            if not piece:
                break
            buf += piece
            start += len(piece)
            if start >= total:
                break
        if not buf:
            return None
        if len(buf) < total:
            log.warning("[WPP] 视频分片不完整 (%d/%d 字节)，仍使用已下载部分", len(buf), total)
        return bytes(buf)

    # ================================================================ 出站

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict] = None) -> SendResult:
        """发消息。``chat_id`` 形如 ``accountId:peerId``（见 :meth:`_chat_id`）。"""
        # 拒绝话术**静默丢弃**（agent 偶发输出"无 @提及，按群规则不回复"类话术）
        if outbound.is_rejection_phrase(content):
            log.warning("[WPP] 丢弃拒绝话术（未发出）: %r", (content or "")[:60])
            return SendResult(success=True, message_id=None)

        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
            acct = self.account(account_id)
            client = self._clients.get(acct.id)
            if client is None:
                return SendResult(success=False, error=f"账号 {acct.id} 未连接",
                                  error_kind="transient")

            # 出站白名单闸（未设 env 时是 no-op）
            outbound.assert_outbound_allowed(peer_id, acct.id)

            # 心流交付缝（**前置**）：只有"刚判定通过、正在等这一条回复"的群才有登记，
            # 其余（含所有私聊、所有非心流群消息）是一次内存查表后返回 None。
            plan = self._hf_pre_send(account_id, peer_id, content or "")
            if plan is not None and plan.placeholder:
                # 重复内容闸拦下 ⇒ **静默不发**。账本已按 suppressed 收敛
                # （不占预算 / 不开窗 / 不进重复历史，INVARIANT I5），这里只需把"成功"
                # 还给调用方 —— 报失败会让 runner 重试同一条插话直到天亮。
                log.info("[WPP:%s] 心流插话与近期发言重复，静默不发: %s", acct.id, chat_id)
                return SendResult(success=True, message_id=None,
                                  raw_response={"hfSuppressed": plan.placeholder})

            chunks = outbound.chunk_text(content)
            last_id: Optional[str] = None
            for idx, chunk in enumerate(chunks):
                result = await self._send_chunk(
                    acct, client, peer_id, chunk,
                    reply_to=reply_to if idx == 0 else None,
                    metadata=metadata,
                )
                if not result.success:
                    self._hf_note_sent(account_id, peer_id, content or "", result)
                    return result
                last_id = result.message_id or last_id
            sent = SendResult(success=True, message_id=last_id)
            self._hf_note_bot_reply(account_id, peer_id, content or "")
            self._hf_note_sent(account_id, peer_id, content or "", sent)
            return sent

        except (ValueError, outbound.netguard.GuardError) as exc:
            log.error("[WPP] 发送被拒: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind="forbidden")
        except WppApiError as exc:
            log.error("[WPP] 发送失败: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind="transient")
        except Exception as exc:  # noqa: BLE001
            log.error("[WPP] 发送异常: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), error_kind="unknown")

    async def _send_chunk(self, acct: accounts_mod.Account, client: WppClient, peer_id: str,
                          chunk: str, *, reply_to: Optional[str],
                          metadata: Optional[dict]) -> SendResult:
        """发**单片**。

        引用回复走 **``/Msg/ShareLink`` + ``Type=5`` + ``Xml``**（迁移自旧
        ``_send_quote``）—— **不是** ``send_app_message``（那个是结构化卡片，
        收的是 ``items`` 列表，形态完全不同）。``type=57`` 的 ``refermsg`` XML
        由 :func:`outbound.build_quote_reply_xml` 生成。
        """
        if reply_to:
            # 去掉可能的 ``accountId:`` 前缀 —— svrid 只是消息 id
            svrid = reply_to.split(":", 1)[1] if ":" in reply_to else reply_to
            fromusr = (metadata or {}).get("quote_from_wxid") or ""
            prefix = await self._quote_prefix(acct, client, peer_id, fromusr, metadata)
            xml = outbound.build_quote_reply_xml(
                reply_content=f"{prefix}{chunk}".strip(), svrid=svrid, fromusr=fromusr)
            resp = await client.call("/Msg/ShareLink",
                                     {"ToWxid": peer_id, "Type": 5, "Xml": xml})
        else:
            resp = await client.send_text(to_wxid=peer_id, content=chunk)

        if not outbound.send_ok(resp):
            return SendResult(success=False, error=outbound.send_error_text(resp),
                              error_kind="unknown", raw_response=resp)
        ids = outbound.extract_outbound_msg_ids(resp)
        await self._persist_outbound(acct, peer_id, chunk, ids)
        return SendResult(success=True, message_id=ids.msg_id, raw_response=resp)

    async def _quote_prefix(self, acct: accounts_mod.Account, client: WppClient,
                            peer_id: str, fromusr: str, metadata: Optional[dict]) -> str:
        """群聊引用回复的 ``@被回复人 `` 前缀（**仅群聊**才有）。

        ``display_name`` 四级优先（对齐旧 ``_send_quote`` 注释里的第 1/2/3/4 级）：

        1. ``metadata.quote_display_name`` —— 微信引用消息自带，最准（群友也准）
        2. **DB 联系人昵称**（``repo_messages.get_contact`` 的 ``nickname``/``remark``）
        3. vendor ``/Friend/GetContractDetail``（群友非联系人也能查）
        4. ``wxid`` 兜底

        ⚠️ 旧实现第 2 级查的是**被引用消息行的 ``peer_name``**（要知道被引用消息的
        内容行）；这里改成查**联系人表**（只需要 ``fromusr``），少一次按 msg_id 的
        回查 —— 语义等价且更省。缺 ``fromusr`` 时跳过 2/3 级。
        """
        if not outbound.is_group_peer(peer_id) or not fromusr:
            return ""
        name = (metadata or {}).get("quote_display_name") or ""
        if not name or name == fromusr:
            name = self._contact_name(acct.id, fromusr)
        if not name or name == fromusr:
            name = await self._resolve_display_name(acct, client, fromusr)
        if not name or name == fromusr:
            name = fromusr
        return f"@{name} "

    @staticmethod
    def _contact_name(account_id: str, wxid: str) -> str:
        """第 2 级：本地联系人表查昵称。查不到返空串（**不抛** —— 昵称只是装饰）。"""
        try:
            from ..data import repo_messages

            row = repo_messages.get_contact(account_id, wxid)
        except Exception as exc:  # noqa: BLE001 —— DB 不可用时降级到下一级
            log.debug("[WPP:%s] 联系人查询降级 %s: %s", account_id, wxid, exc)
            return ""
        if not isinstance(row, dict):
            return ""
        return str(row.get("remark") or row.get("nickname") or "").strip()

    async def _resolve_display_name(self, acct: accounts_mod.Account, client: WppClient,
                                    wxid: str) -> str:
        """第 3 级：vendor 查昵称（30 分钟缓存）。取不到返回 ``wxid`` 本身。"""
        cached = self._name_cache.get(f"{acct.id}:{wxid}")
        if cached and time.time() - cached[1] < 1800:
            return cached[0]
        name = wxid
        try:
            detail = await client.get_contract_detail(wxid)
            data = detail.get("Data") if isinstance(detail, dict) else None
            if isinstance(data, dict):
                found = (data.get("NickName") or data.get("nickName")
                         or data.get("Remark") or data.get("nickname"))
                if found:
                    name = str(found)
        except Exception as exc:  # noqa: BLE001 —— 查不到昵称不该挡住发送
            log.debug("[WPP:%s] 查昵称失败 %s: %s", acct.id, wxid, exc)
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
        """
        try:
            from ..data import repo_messages

            repo_messages.save_message(
                acct.id,
                direction="outbound",
                peer_kind="group" if outbound.is_group_peer(peer_id) else "dm",
                peer_id=peer_id,
                chat_id=self._chat_id(acct.id, peer_id),
                msg_id=ids.new_msg_id or ids.msg_id or None,
                new_msg_id=ids.new_msg_id,
                content=content,
                ts=int(time.time()),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s] 出站落库失败: %s", acct.id, exc)

    # ------------------------------------------------------- 媒体覆写（非抽象）

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """微信**没有** typing 指示 —— 显式 no-op。

        不能删这个方法：基类 ``_keep_typing`` 会循环调它（``base.py:3350``），
        默认实现可能打日志刷屏。这里静默即可。
        """
        return None

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[dict] = None) -> SendResult:
        """发图（URL / 本地路径 / base64 均可）。"""
        return await self._send_media(chat_id, image_url, "image", caption, metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None,
                              metadata: Optional[dict] = None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, image_path, "image", caption, metadata)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None,
                         **kwargs) -> SendResult:
        return await self._send_media(chat_id, audio_path, "voice", caption, metadata)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None,
                         **kwargs) -> SendResult:
        return await self._send_media(chat_id, video_path, "video", caption, metadata)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[dict] = None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, file_path, "file", caption, metadata,
                                     file_name=file_name)

    async def _send_media(self, chat_id: str, ref: str, kind: str,
                          caption: Optional[str], metadata: Optional[dict],
                          file_name: Optional[str] = None) -> SendResult:
        """媒体发送的统一实现。

        ``ref`` → base64 的解析走 :func:`outbound.resolve_to_base64`（**内建安全闸**：
        本地路径过白名单、URL 过 SSRF 校验）。

        ⚠️ **视频要缩略图**：``send_video`` 的 ``ImageBase64`` 是 **必填**
        （vendor 侧拒收空值）。这里用 ffmpeg 抽一帧；抽不到就照发并把
        vendor 的报错**如实**返回 —— 不假装成功。
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
            acct = self.account(account_id)
            client = self._clients.get(acct.id)
            if client is None:
                return SendResult(success=False, error=f"账号 {acct.id} 未连接",
                                  error_kind="transient")
            outbound.assert_outbound_allowed(peer_id, acct.id)

            b64 = await outbound.resolve_to_base64(ref)
            if not b64:
                return SendResult(success=False, error=f"媒体内容不可用: {ref[:60]}",
                                  error_kind="bad_format")

            name = file_name or os.path.basename(ref) or f"{kind}.bin"
            if kind == "image":
                resp = await client.send_image(to_wxid=peer_id, image_base64=b64)
            elif kind == "voice":
                resp = await self._send_voice_media(client, peer_id, ref, name, b64)
            elif kind == "video":
                thumb = await self._video_thumb(ref)
                if not thumb:
                    log.warning("[WPP] 视频无缩略图（ffmpeg 不可用或抽帧失败）—— vendor 可能拒收")
                resp = await client.send_video(to_wxid=peer_id, video_base64=b64,
                                               thumb_base64=thumb)
            else:
                resp = await client.send_file_v2(to_wxid=peer_id, file_name=name,
                                                 file_base64=b64)

            if not outbound.send_ok(resp):
                return SendResult(success=False, error=outbound.send_error_text(resp),
                                  raw_response=resp)
            return SendResult(success=True, message_id=outbound.extract_msg_id(resp),
                              raw_response=resp)
        except (ValueError, outbound.netguard.GuardError) as exc:
            log.error("[WPP] 媒体发送被拒: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind="forbidden")
        except Exception as exc:  # noqa: BLE001
            log.error("[WPP] 媒体发送异常: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), error_kind="unknown")

    @staticmethod
    async def _send_voice_media(client: WppClient, peer_id: str, ref: str, name: str,
                                b64: str) -> dict:
        """发语音。**只有 SILK 能进 ``/Msg/SendVoice``**，其余一律降级发文件。

        为什么不能「原样塞 voice_type=4」：厂商对非 SILK / 超限 silk 会**静默拒收**
        并回一个文案为 ``INVALID_CREDENTIAL`` 的 ``Code=-2``（误导性错误码）。
        实证矩阵与根因见 :mod:`plugin.platform.silk_encoder` 的模块 docstring。

        顺序（对齐 TS ``dispatch/outbound.ts`` v1.3.48 的 sendVoice 链路 + 其降级）：

        1. 入参已是 ``.silk`` 且不超 :data:`silk_encoder.MAX_SILK_BYTES` ⇒ 直接发；
        2. 否则试 :func:`silk_encoder.encode_to_silk`（需要 ffmpeg + encoder 二进制，
           ``.183`` 上两者都没有 ⇒ 直接返回 ``None``）；
        3. 还不行，或厂商仍拒收 ⇒ **发文件**（老板 6-12 定的偏好：
           "成功发为语音, 失败降级文件"）。

        返回的是最终那次响应的原样 dict，交给调用方统一判 :func:`outbound.send_ok`。
        """
        ext = os.path.splitext(name)[1].lower()
        silk_b64 = ""
        if ext == ".silk" and len(b64) * 3 // 4 <= silk_encoder.MAX_SILK_BYTES:
            silk_b64 = b64
        elif ext != ".silk":
            encoded = await silk_encoder.encode_to_silk(ref)
            if encoded:
                silk_b64 = encoded

        if silk_b64:
            # Type=4 = SILK（微信默认；0=AMR 是**入站**才见到的格式，厂商出站不收）
            resp = await client.send_voice(to_wxid=peer_id, voice_base64=silk_b64, voice_type=4)
            if outbound.send_ok(resp):
                return resp
            log.warning("[WPP] 语音发送被拒（%s）—— 降级发文件",
                        outbound.send_error_text(resp))
        else:
            log.info("[WPP] 语音非 SILK 且无法转码 —— 降级发文件: %s", name)

        return await client.send_file_v2(to_wxid=peer_id, file_name=name, file_base64=b64)

    @staticmethod
    async def _video_thumb(ref: str) -> str:
        """用 ffmpeg 抽首帧做缩略图，返回 base64（失败返 ``""``）。

        ``ref`` 是本地路径（URL 已经被 :func:`outbound.resolve_to_base64` 校验过）；
        远程 URL 不在这里处理 —— 抽帧只在本地文件上做。
        """
        if not (ref.startswith("/") or ref.startswith("~")):
            return ""
        path = os.path.expanduser(ref)
        if not os.path.isfile(path):
            return ""
        tmp = ""
        try:
            import shutil
            import tempfile

            if not shutil.which("ffmpeg"):
                log.debug("[WPP] 无 ffmpeg，跳过视频抽帧")
                return ""
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
                tmp = fh.name
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", path,
                "-frames:v", "1", "-f", "image2", tmp,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("[WPP] ffmpeg 抽帧超时")
                return ""
            if proc.returncode != 0:
                log.debug("[WPP] ffmpeg 抽帧失败: %s", (stderr or b"")[:200])
                return ""
            with open(tmp, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        except (OSError, asyncio.SubprocessError) as exc:
            log.debug("[WPP] 视频抽帧异常: %s", exc)
            return ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

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
        return {
            "selfWxid": acct.self_wxid,
            "nickname": acct.nickname,
            "allowFrom": list(acct.allow_from),
            "groupPolicy": acct.group_policy,
            "groupAllowFrom": list(acct.group_allow_from),
            "blacklistGroups": list(acct.blacklist_groups),
            "requireAtMention": acct.require_at_mention,
            "adminUsers": list(acct.admin_users),
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


async def _standalone_send(pconfig: Any, chat_id: str, message: str, *,
                           thread_id: Optional[str] = None,
                           media_files: Optional[list] = None,
                           force_document: bool = False) -> dict:
    """进程外发送（cron 无并发 gateway 时用）。

    契约：``dict``，成功 ``{"success": True, "message_id": ...}``，
    失败 ``{"error": str}``。

    ⚠️ 旧实现**泄漏 WppClient**（从不 close）—— 这里用 ``async with`` 保证回收。
    """
    if outbound.is_rejection_phrase(message):
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

        outbound.assert_outbound_allowed(peer_id, acct.id)
        async with WppClient(base_url=acct.api_base_url, authcode=acct.authcode,
                             account_id=acct.id, timeout=30) as client:
            message_id = None
            for chunk in outbound.chunk_text(message):
                resp = await client.send_text(to_wxid=peer_id, content=chunk)
                if not outbound.send_ok(resp):
                    return {"error": outbound.send_error_text(resp)}
                message_id = outbound.extract_msg_id(resp) or message_id
            return {"success": True, "message_id": message_id}
    except Exception as exc:  # noqa: BLE001
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
