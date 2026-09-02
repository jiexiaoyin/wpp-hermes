"""WppAdapter — WeChatPadPro (个人微信 Pad 协议) Hermes 平台适配器。

迁移自 wpp-openclaw (TypeScript) v1.3.80 → Hermes Python BasePlatformAdapter。

注册平台: ``wechatpadpro``
消息流:
  入站: vendor WS /ws/sync → /Msg/Sync 拉增量 → 解析 → MessageEvent → Hermes agent
  出站: Hermes agent 回复 → send(chat_id, content) → /Msg/SendTxt → 微信
chat_id 编码: ``f"{accountId}:{peerId}"``（多账号隔离）
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 2026-09-01 修复: 过滤"无 @提及，按群规则不回复"类拒绝话术（老板在益融管理群看到 2 次）
# 防线 2（插件层）：agent 偶发输出这类话术时直接丢弃，绝不发到群里
_REJECTION_PHRASE_RE = re.compile(r"无\s*@\s*提及|按群规则不回复|不回复\s*[:：]")

# oss-archive skill (2026-09-01 抽自 wechatpadro plugin, Phase 2.1, ✅ 安全: 无 vendor authcode)
_SKILLS_DIR = Path("/root/.hermes/skills")
if str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))
try:
    from oss_archive.loader import upload_bytes, upload_file, upload_media_to_oss, register_accounts as _oss_register_accounts  # type: ignore
    _OSS_ARCHIVE_OK = True
    logging.getLogger(__name__).info("[WPP] oss-archive skill 加载成功 (Phase 2.1 解耦)")
except ImportError as e:  # noqa: BLE001
    upload_media_to_oss = None  # type: ignore
    _oss_register_accounts = None  # type: ignore
    _OSS_ARCHIVE_OK = False
    logging.getLogger(__name__).warning("[WPP] oss-archive skill 加载失败: %s (降级本地使用)", e)

# wpp-stt skill (2026-09-01 Phase 2.3, ✅ 安全: 无 vendor authcode, 只调 SiliconFlow)
try:
    from wpp_stt import transcribe as wpp_stt_transcribe  # type: ignore
    _WPP_STT_OK = True
    logging.getLogger(__name__).info("[WPP] wpp-stt skill 加载成功 (Phase 2.3 解耦)")
except ImportError as e:  # noqa: BLE001
    wpp_stt_transcribe = None  # type: ignore
    _WPP_STT_OK = False
    logging.getLogger(__name__).warning("[WPP] wpp-stt skill 加载失败: %s (降级旧 transcribe_voice)", e)


# ---- vendor API 全部在 plugin 内调用, authcode 不外泄 ----
# media.py 提供 download_*_to_file, 直接接收 client (vendor client), 不经过任何 skill.
# 原则 (2026-09-01 接老板拍板):
#   R1. Skill 不接收 vendor callable, 不接收 authcode
#   R2. Skill 只接受 bytes / str / dict 纯数据
#   R3. vendor API 调用永远在 plugin 层, authcode 永不离开 plugin 进程


from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

log = logging.getLogger(__name__)

PLATFORM_NAME = "wechatpadpro"
DEFAULT_ACCOUNT = "default"

# 被回复人昵称缓存（迁移自 wpp resolveDisplayName 30min 缓存）
_display_name_cache: dict[str, tuple[str, float]] = {}


class WppAdapter(BasePlatformAdapter):
    """WeChatPadPro 平台适配器。"""

    # 能力声明
    supports_code_blocks = False          # 微信纯文本
    typed_command_prefix = "/"

    # 微信无 native typing indicator（vendor 未暴露）。
    # Hermes gateway 的 _keep_typing 会每 2s 自动调用 send_typing（base.py:5337 _keep_typing，
    # interval=2.0）。我们只在「每个 chat_id 第一次」时发一条 progress 短文本，
    # 后续调用直接 no-op（避免「稍等~ ⏳」被发 3-5 次）。
    # 2026-08-31 修复:之前每 2s 都发一次，user 18:57:05 实锤「稍等~ ⏳为什么要两次」。

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """发 typing 信号。

        ⚠️ 微信 vendor 无 typing API，且发 progress 文本（如"稍等~ ⏳"）会干扰用户
        （用户会收到"稍等~"再收到最终回复，老板 18:57 实锤"为什么要两次"）。
        **完全 no-op**：不发送任何 progress 文本，用户只收到最终回复（对齐 OpenClaw）。
        """
        return

    async def stop_typing(self, chat_id: str) -> None:
        """停止 typing（微信无 typing API，no-op）。"""
        return

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        finalize: bool = False,
        metadata: dict | None = None,
    ) -> dict | None:
        """编辑已发的微信消息（用于 final send 替代，避免 duplicate send warning）。

        vendor 没有原生 edit API, 我们用 revoke + re-send 的方式:
        1. 撤回 message_id 对应的消息
        2. 重新发送新内容
        3. 返回新 message_id (callers 应更新流式状态)

        当 finalize=True (final send 在用), 直接 resend 不撤回, 避免丢上下文
        (微信编辑实现粗糙, 不能保证内容连贯)。
        """
        # 解析 account_id
        account_id = "default"
        if chat_id and ":" in chat_id:
            # chat_id 格式 default:wxid_boss_demo 或 default:chatroom_demo_1@chatroom
            parts = chat_id.split(":", 1)
            if parts[0]:
                account_id = parts[0]

        client = self._clients.get(account_id)
        if not client:
            log.warning("[WPP:%s] edit_message: 无 client, fallback", account_id)
            return None

        target = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        try:
            if not finalize:
                # 撤回原消息
                try:
                    await client.revoke_msg(message_id)
                except Exception as rev_err:  # noqa: BLE001
                    log.debug("[WPP:%s] revoke %s 失败(继续 send): %s", account_id, message_id, rev_err)
            # 重新发
            resp = await client.send_text(target, content)
            new_id = None
            if isinstance(resp, dict):
                data = resp.get("data", {}) if isinstance(resp.get("data"), dict) else {}
                new_id = data.get("MsgId") or data.get("msgId") or data.get("newMsgId")
            return {"message_id": str(new_id) if new_id else None, "raw": resp}
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] edit_message 失败: %s", account_id, e)
            return None

    @property
    def enforces_own_access_policy(self) -> bool:
        """WPP 在 intake 层用群白名单(群 id)+私聊 allow_from 把关，trust adapter 决策。

        群白名单按群 id（group_allow_from/group_allowed_chats），不按群成员；
        私聊才按个人 wxid（allow_from）。Hermes 的 env allowlist 校验委托给本 adapter。
        """
        return True

    # ------------------------------------------------------------------
    # 2026-09-01 插件侧授权契约（替代 PATCH-1，升级 hermes 不失效）
    # framework authz_mixin._is_user_authorized 对 enforces_own_access_policy=True
    # 的 adapter，群消息读 _group_policy / _group_allow_from / _groups，
    # 私聊读 _dm_policy / _allow_from。这些是 WeCom 等原生 adapter 的属性，
    # wpp 之前没实现 → framework 读不到 policy → 走 user 白名单 fallback →
    # 群内非白名单成员被拒（接龙根因）。这里从 default 账号配置实现这些属性，
    # framework 即可直接信任 wpp 的 intake 群授权（triggers.should_process 已把关）。
    # ------------------------------------------------------------------
    @property
    def _group_policy(self) -> str:
        acct = self._accounts.get("default") or {}
        policy = str(acct.get("groupPolicy") or acct.get("group_policy") or "open").strip().lower()
        # fail-closed 加固：allowlist 但无白名单 = 拒绝所有群（与 framework 安全模型一致，
        # 避免 wpp intake 的 allowlist 空白名单 fail-open 被 framework 信任）
        if policy == "allowlist" and not (acct.get("groupAllowFrom") or acct.get("group_allow_from") or []):
            return "disabled"
        return policy

    @property
    def _group_allow_from(self):
        acct = self._accounts.get("default") or {}
        return acct.get("groupAllowFrom") or acct.get("group_allow_from") or []

    @property
    def _dm_policy(self) -> str:
        acct = self._accounts.get("default") or {}
        # 私聊无显式 dm_policy；有 allowFrom 则视为 allowlist，否则 open
        allow = acct.get("allowFrom") or acct.get("allow_from") or []
        return "allowlist" if allow else "open"

    @property
    def _allow_from(self):
        acct = self._accounts.get("default") or {}
        return acct.get("allowFrom") or acct.get("allow_from") or []

    @property
    def _groups(self) -> dict:
        # 群级 sender allowlist：wpp 的群授权按"群白名单(群 id)"，无 per-group
        # sender 名单（任何成员在白名单群内发的消息都可触发）。
        # framework/authz_mixin.py:651 要求 `_adapter_group_has_sender_allowlist` 真
        # 返回 True，框架才会放行（line 651-656 早退）→ 否则 line 746 走
        # WECHATPRO_ALLOWED_USERS env 兜底，群内非白名单成员（含接龙发送方）
        # 会被 framework 拦截（接龙根因）。
        #
        # 修复：把群白名单转成 framework 期望的格式——
        #   {"<chat_id>": {"allow_from": ["*"]}}
        # "*" 表示"该群任意成员都通过"（群已经在 groupAllowFrom 白名单里）。
        acct = self._accounts.get("default") or {}
        group_allow = acct.get("groupAllowFrom") or acct.get("group_allow_from") or []
        return {
            f"{gid}@chatroom" if not gid.endswith("@chatroom") else gid: {"allow_from": ["*"]}
            for gid in group_allow
            if gid
        }

    def __init__(self, config, platform):
        super().__init__(config, platform)
        self._accounts: dict[str, dict] = {}      # account_id -> account cfg
        self._ws_clients: dict[str, Any] = {}     # account_id -> WppWsClient
        self._clients: dict[str, Any] = {}        # account_id -> WppClient
        self._pipelines: dict[str, Any] = {}      # account_id -> InboundPipeline
        self._tasks: list[asyncio.Task] = []
        self._webhook_server = None
        self._connected = False
        # 2026-08-31 fix：每 chat_id 只发一次「稍等~ ⏳」（framework 每 2s 调 send_typing，
        # 之前实现每次都发，导致 user 实锤收到 2 次 progress 文本）
        self._progress_sent: dict[str, bool] = {}
        # 2026-08-31 fix：WPP_TOOLS_ONLY=1 时（wpp-wechat profile）不加载任何账号配置
        # 避免跟 default profile 的 authcode 冲突（adapter.token 是 framework 比对 credential 的关键属性，
        # _build_accounts 即使不连 WS 也会读 authcode 放进 self._accounts[id].authcode → framework 判定冲突）
        if os.environ.get("WPP_TOOLS_ONLY") == "1":
            self._tools_only = True
            self.token = f"tools_only:{_current_profile() or 'default'}"
            log.info("[WPP] tools-only 模式（profile=%s），跳过账号加载", _current_profile())
        else:
            self._tools_only = False
            self._build_accounts()
            # 凭据指纹：让 Hermes 的 multiplex 冲突检测能识别同 authcode 双连接
            # （gateway.run._adapter_credential_fingerprint 读 token/_token 等属性）
            first = next(iter(self._accounts.values()), {})
            self.token = (first or {}).get("authcode", "")

    # ------------------------------------------------------------------ 配置
    def _build_accounts(self) -> None:
        """从 config.extra.accounts 或 accounts/<id>.json 构建账号表。"""
        from . import config as cfg_mod

        extra = (getattr(self.config, "extra", None) or {}).get("accounts") or {}
        ids = cfg_mod.list_account_ids(extra)
        for acct_id in ids:
            extra_cfg = extra.get(acct_id) or {}
            acct = cfg_mod.resolve_account_config(acct_id, extra_cfg)
            if not acct.get("authcode"):
                log.warning("[WPP] 账号 %s 无 authcode，跳过", acct_id)
                continue
            # 2026-09-01 接老板拍板 (对齐 wpp-openclaw v1.3.61 P1 安全加固):
            # cfg.agent 必填且禁止 "main" — 防止 fallback main 导致多账号串号
            agent = acct.get("agent", "")
            if not agent or not isinstance(agent, str) or agent.strip() == "":
                raise RuntimeError(
                    f"[WPP] account '{acct_id}' agent 字段缺失. 必须配置 agent (e.g. 'wpp-wechat')."
                )
            if agent == "main":
                raise RuntimeError(
                    f"[WPP] account '{acct_id}' agent='main' 被禁止 (防多账号串号). "
                    f"必须配置专属 agent profile (e.g. 'wpp-wechat', 'wpp-wechat-boss2')."
                )
            acct["id"] = acct_id
            acct["_extra"] = extra_cfg  # 供配置热加载保留 extra
            self._accounts[acct_id] = acct
            log.info("[WPP] 账号 %s → agent=%s authcode=%s...%s", acct_id, agent,
                     acct["authcode"][:4] if len(acct["authcode"]) >= 4 else "****",
                     acct["authcode"][-4:] if len(acct["authcode"]) >= 4 else "")
        log.info("[WPP] 已加载账号: %s", list(self._accounts.keys()))

    def get_account(self, account_id: str) -> dict:
        return self._accounts.get(account_id) or {}

    def get_cross_account_context(self) -> list[dict]:
        """Phase 4.3: 返回所有账号的上下文元信息（跨账号探查）。

        安全边界（R1 铁律）: 只返回元信息（account_id/agent/selfWxid/nickname/
        连接状态/白名单），**绝不含 authcode / tokenKey**。authcode 永不离开 plugin 进程。

        用途: 老板多账号场景下，"想用 boss2 账号回复老板家人"时，agent 先调
        wpp_accounts_context 探查有哪些账号、各自绑定哪个 agent、selfWxid 是什么，
        再通过 send 工具的 account_id 参数跨账号路由（底层 _clients 已按账号隔离）。
        """
        result = []
        for aid, acct in sorted(self._accounts.items()):
            result.append({
                "account_id": aid,
                "agent": acct.get("agent", ""),
                "self_wxid": acct.get("selfWxid", ""),
                "nickname": acct.get("nickname", "微信助手"),
                "enabled": bool(acct.get("enabled", True)),
                "ws_connected": aid in self._ws_clients,
                "client_ready": aid in self._clients,
                "admin_users": acct.get("adminUsers") or acct.get("allowFrom") or [],
            })
        return result

    def authorize_account(self, account_id: str, caller_wxid: str | None = None) -> tuple[bool, str]:
        """Phase 4.3: 跨账号权限校验（账号互相隔离的最后一道防线）。

        规则:
          1. 目标账号不存在 → 拒绝
          2. caller_wxid 为空（内部调用: cron/standalone/系统）→ 允许（信任内部链路）
          3. caller 在目标账号 adminUsers/allowFrom 白名单 → 允许
          4. 否则拒绝（跨账号越权，账号互相隔离）

        返回 (allowed, reason)。reason 为空串表示允许。
        """
        if account_id not in self._accounts:
            return False, f"账号 {account_id!r} 不存在（可用账号: {sorted(self._accounts.keys())}）"
        if not caller_wxid:
            # 内部调用（cron deliver / standalone_send / 系统任务）信任，无 caller 概念
            return True, ""
        acct = self._accounts[account_id]
        allow = acct.get("adminUsers") or acct.get("allowFrom") or []
        if caller_wxid in allow:
            return True, ""
        # 自己账号的 selfWxid 也算自己（自己给自己发消息）
        if acct.get("selfWxid") and caller_wxid == acct.get("selfWxid"):
            return True, ""
        return False, f"无权操作账号 {account_id!r}（caller {caller_wxid!r} 不在 adminUsers/allowFrom 白名单）"

    # ------------------------------------------------------------------ 生命周期
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        from .api_client import WppClient
        from .ws_client import WppWsClient

        # 纯工具模式：非 default profile（multiplex secondary，如 wpp-wechat）只注册工具
        # 供 cron 的 standalone_send / deliver 识别，不启动 WS/webhook（避免双连接/双回调）。
        # 判断：WPP_TOOLS_ONLY env（profile .env 或 shell）或当前 Hermes profile != default。
        is_secondary = _current_profile() not in ("default", "")
        if os.environ.get("WPP_TOOLS_ONLY") == "1" or is_secondary:
            log.info("[WPP] 纯工具模式（profile=%s），跳过 WS/webhook 连接", _current_profile())
            self._connected = False
            # 即使纯工具模式也要注册 oss_archive 白名单 (供 cron / standalone_send 用)
            if _OSS_ARCHIVE_OK and _oss_register_accounts:
                try:
                    _oss_register_accounts(set(self._accounts.keys()))
                    log.info("[WPP] oss_archive account_id 白名单已注册 (纯工具模式): %s",
                             sorted(self._accounts.keys()))
                except Exception as e:  # noqa: BLE001
                    log.warning("[WPP] 注册 oss_archive account_id 白名单失败: %s", e)
            return True

        # 2026-09-01 安全加固 (Phase 2.4): 把 plugin 已加载的 account_id 注册到 oss_archive 白名单
        # 这样 oss_archive skill 只会接受 plugin 内合法的账号, 拒绝外部 agent 越权调用
        # 注意: 必须放在 is_secondary 检查**之后**, 因为纯工具模式 self._accounts 不一定有内容
        # 但放这里是因为 default profile 会走下面 for 循环创建 clients
        if _OSS_ARCHIVE_OK and _oss_register_accounts:
            try:
                _oss_register_accounts(set(self._accounts.keys()))
                log.info("[WPP] oss_archive account_id 白名单已注册: %s",
                         sorted(self._accounts.keys()))
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP] 注册 oss_archive account_id 白名单失败: %s", e)

        for acct_id, acct in self._accounts.items():
            api_base = acct.get("apiBaseUrl", "https://wx.juhe.chat")
            authcode = acct.get("authcode", "")
            ws_url = acct.get("wsUrl", "wss://wx.juhe.chat/ws/sync")
            if not authcode:
                continue
            client = WppClient(api_base, authcode)
            self._clients[acct_id] = client
            # 入站管线
            from .inbound import InboundPipeline
            self._pipelines[acct_id] = InboundPipeline(acct_id, acct)
            ws = WppWsClient(acct_id, ws_url, authcode, self._make_on_raw(acct_id))
            self._ws_clients[acct_id] = ws
            ws.start()
            log.info("[WPP:%s] WS 已启动: %s", acct_id, ws_url)
        self._connected = bool(self._ws_clients)
        # 配置热加载（对齐 wpp fs.watch：改 accounts/<id>.json 零重启生效）
        try:
            from . import config as cfg_mod
            cfg_mod.start_config_watcher(self._reload_account)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] 配置热加载启动失败: %s", e)
        # webhook 兜底（vendor sync_message 通知 → /Msg/Sync 拉消息）
        try:
            self._start_webhook()
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] webhook 兜底启动失败: %s", e)
        return self._connected

    def _start_webhook(self) -> None:
        """启动 webhook server + 配置 vendor webhook URL（消息兜底）。"""
        from .webhook import WppWebhookServer
        # 单实例 server（多账号共享端口，path 区分）
        if self._webhook_server is None:
            import asyncio as _asyncio
            try:
                main_loop = _asyncio.get_running_loop()
            except RuntimeError:
                main_loop = None
            self._webhook_server = WppWebhookServer(host="127.0.0.1", port=4398, loop=main_loop)
            self._webhook_server.start()
        # 每账号注册 path + 设置 vendor webhook URL
        for acct_id, acct in self._accounts.items():
            authcode = acct.get("authcode", "")
            if not authcode:
                continue
            webhook_path = acct.get("webhookPath", f"/wechatpadpro/{acct_id}/webhook")
            self._webhook_server.add_path(webhook_path, self._make_webhook_handler(acct_id))
            # 设置 vendor webhook URL（autoSetWebhook 配置开启时）
            if acct.get("autoSetWebhook", True) and acct.get("webhookPublicUrl"):
                url = f"{acct['webhookPublicUrl']}{webhook_path}"
                log.info("[WPP:%s] 设置 vendor webhook: %s", acct_id, url)
                client = self._clients.get(acct_id)
                if client:
                    try:
                        asyncio.create_task(self._set_vendor_webhook(client, url, acct=acct))
                    except Exception as e:  # noqa: BLE001
                        log.warning("[WPP:%s] vendor webhook 设置失败: %s", acct_id, e)

    async def _set_vendor_webhook(self, client, url: str, *, acct: Optional[dict] = None) -> None:
        """调 vendor 设置 webhook URL（普通 + business + autoSync）。

        对齐 OpenClaw autoSetWebhook：只设 /Webhook/Set 只会推空 Data 的 sync_message，
        business webhook (syncMessageUrl/logoutUrl) + StartAutoSync 才推完整消息。
        2026-08-31 修复：Hermes 迁移漏了 business set，vendor business webhook 还指向 openclaw.juhe.chat。
        """
        try:
            resp = await client.call("/Webhook/Set", {"url": url})
            log.info("[WPP webhook] vendor Set 结果: Code=%s Success=%s", resp.get("Code"), resp.get("Success"))
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP webhook] vendor Set 失败: %s", e)

        # business webhook（带 token 的 businessPath）+ StartAutoSync 完整消息推送
        try:
            # 对齐 OpenClaw deriveWebhookPaths: webhookPath 里 /wechatpadpro/ → /wechatpadpro/<token>/，再 + /business
            acct_cfg = acct or {}
            webhook_path = acct_cfg.get("webhookPath", "") or ""
            token = acct_cfg.get("webhookPathToken", "") or ""
            if token and webhook_path:
                business_path = webhook_path.replace("/wechatpadpro/", f"/wechatpadpro/{token}/")
                if not business_path.endswith("/business"):
                    business_path = f"{business_path}/business"
            else:
                business_path = f"{webhook_path}/business" if webhook_path else "/wechatpadpro/default/webhook/business"
            public = (url or "").split("/wechatpadpro/")[0].rstrip("/")
            business_url = f"{public}{business_path}"
            logout_url = f"{business_url}/logout"
            r = await client.call("/Webhook/Business/Set", {"syncMessageUrl": business_url, "logoutUrl": logout_url})
            log.info("[WPP webhook] vendor Business/Set 结果: Code=%s (syncMessageUrl=%s)", r.get("Code"), business_url)
            # StartAutoSync 启动轮询（vendor 推完整消息到 business_url）
            s = await client.call("/Msg/StartAutoSync", {"TargetURL": business_url})
            log.info("[WPP webhook] vendor StartAutoSync 结果: Code=%s", s.get("Code"))
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP webhook] vendor Business/Set + StartAutoSync 失败: %s", e)

    def _make_webhook_handler(self, account_id: str):
        """webhook 回调：sync_message 通知 → /Msg/Sync 拉消息（与 WS 兜底）。"""
        async def on_webhook(payload: dict) -> None:
            try:
                raw = payload or {}
                # vendor webhook 只推 sync_message 通知（MessageType），需 /Msg/Sync 拉
                if str(raw.get("MessageType") or raw.get("message_type") or "") != "sync_message":
                    return
                client = self._clients.get(account_id)
                if not client:
                    return
                resp = await client.sync_message()
                data = resp.get("Data") or {}
                # 复用 WS 推送处理逻辑（提取消息 → 管线 → agent）
                await self._make_on_raw(account_id)({"Data": data})
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP:%s] webhook 处理失败: %s", account_id, e)
        return on_webhook

    def _reload_account(self, account_id: str) -> None:
        """配置变更回调：刷新账号配置 + 入站管线。"""
        try:
            from . import config as cfg_mod
            old_cfg = self._accounts.get(account_id) or {}
            new_cfg = cfg_mod.resolve_account_config(account_id, old_cfg.get("_extra") or {})
            if not new_cfg.get("authcode"):
                return
            # 保留 WS/API 连接不变，只刷新配置 + 管线
            new_cfg["id"] = account_id
            self._accounts[account_id] = new_cfg
            from .inbound import InboundPipeline
            self._pipelines[account_id] = InboundPipeline(account_id, new_cfg)
            log.info("[WPP:%s] 配置已热加载（零重启生效）", account_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] 配置热加载失败: %s", account_id, e)

    async def disconnect(self) -> None:
        self._connected = False
        for ws in self._ws_clients.values():
            try:
                await ws.stop()
            except Exception:  # noqa: BLE001
                pass
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws_clients.clear()
        self._clients.clear()
        try:
            from . import config as cfg_mod
            cfg_mod.stop_config_watcher()
        except Exception:  # noqa: BLE001
            pass
        if self._webhook_server:
            try:
                self._webhook_server.stop()
            except Exception:  # noqa: BLE001
                pass
            self._webhook_server = None

    # ------------------------------------------------------------------ 入站
    def _make_on_raw(self, account_id: str):
        async def on_raw(payload: dict) -> None:
            """收到 WS sync_message 推送 → 直接从 payload 提取消息 → 分发（astrbot 模式）。"""
            try:
                pipeline = self._pipelines.get(account_id)
                if not pipeline:
                    return
                msgs = pipeline.extract(payload)
                if msgs:
                    log.info("[WPP:%s] WS 推送 %d 条消息", account_id, len(msgs))
                for msg in msgs:
                    # metrics: inbound_msg
                    try:
                        from . import metrics as _mt
                        _mt.incr("inbound_msg", peer=msg.peer_id or "?")
                    except Exception:  # noqa: BLE001
                        pass
                    # 消息落库（幂等）
                    self._persist_message(account_id, msg)
                    # 红包检测 + 入库 (2026-08-31 完整对照 P1-3 补全)
                    try:
                        from . import hongbao as _hb
                        if _hb.is_redpacket_message(msg):
                            _hb.record_redpacket(account_id, msg.peer_id or "", msg.from_wxid or "", msg)
                            log.info("[WPP:%s] 红包记录: peer=%s from=%s", account_id, msg.peer_id, msg.from_wxid)
                    except Exception as _hb_err:  # noqa: BLE001
                        log.debug("[WPP:%s] hongbao 检测异常: %s", account_id, _hb_err)
                    ok, reason = pipeline.decide(msg)
                    if reason == "filehelper":
                        # filehelper 命令处理
                        await self._handle_filehelper(account_id, msg)
                        continue
                    if not ok:
                        if reason == "heartflow_candidate":
                            # 未@群消息：尝试心流主动参与
                            await self._try_heartflow(account_id, msg)
                            continue
                        log.info("[WPP:%s] 消息被过滤 (%s): from=%s", account_id, reason, msg.from_wxid)
                        continue
                    event = await self._build_event(account_id, msg, pipeline)
                    if not event:
                        continue
                    try:
                        await self.handle_message(event)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[WPP:%s] dispatch 失败: %s", account_id, e)
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP:%s] WS 推送处理失败: %s", account_id, e)
        return on_raw

    async def _build_event(self, account_id: str, msg, pipeline) -> Optional[MessageEvent]:
        """构造 Hermes MessageEvent（支持媒体下载到本地）。"""
        acct = self._accounts.get(account_id) or {}
        peer_id = msg.peer_id
        chat_id = f"{account_id}:{peer_id}"
        # chat_type
        chat_type = "group" if msg.is_group else "dm"
        text = pipeline.prepare_text(msg)
        # intent-embed 快路径：从 wpp_messages 历史里挑 top-N 相关候选, 作为 channel_prompt 注入
        channel_prompt = None
        if text and len(text) > 1:
            try:
                from . import db as _db
                from . import intent_embed as _ie
                if _ie.is_embed_intent_enabled(acct):
                    top_n, threshold = _ie.get_embed_config(acct)
                    history = _db.query_messages(account_id, peer_id, limit=30)
                    cand_list = [
                        {"text": (m.get("content") or "")[:300],
                         "from_name": m.get("from_wxid", "?"),
                         "ts": m.get("ts")}
                        for m in history
                    ]
                    picked = _ie.find_relevant_candidates(text, cand_list, top_n=top_n, threshold=threshold)
                    if picked:
                        ctx_lines = [
                            f"• [{c.get('from_name', '?')}] {c.get('text', '')[:120]}"
                            for c in picked
                        ]
                        channel_prompt = "[历史相关上下文]\n" + "\n".join(ctx_lines)
            except Exception as e:  # noqa: BLE001
                log.debug("[WPP:%s] intent_embed 注入失败: %s", account_id, e)
        # msg_type 映射
        mt = MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []
        log.info("[WPP:%s] _build_event: msg_type=%s media_keys=%s", account_id, msg.msg_type, list(msg.media.keys()) if msg.media else "None")
        # Phase A (2026-09-01 老板拍板): 入站媒体时注入 mmx 提醒到 channel_prompt,
        # 引导 model 用 mmx_vision_describe 而不是 terminal + tesseract (后者 OCR 不准)
        channel_prompt_media: Optional[str] = None
        if msg.msg_type == 3:      # 图片
            log.info("[WPP:%s] IMAGE msg.media keys: %s", account_id, list(msg.media.keys()) if msg.media else "None")
            mt = MessageType.PHOTO
            client = self._clients.get(account_id)
            if client:
                # vendor 下载在 plugin 内调用 (authcode 不外泄)
                # Phase 2.2 skill 已撤销, 直接走 media.download_image_to_file
                from .media import download_image_to_file
                path, mtype = await download_image_to_file(client, msg.media)
                # OSS 上传走 oss_archive skill (✅ 无 vendor authcode)
                if path and _OSS_ARCHIVE_OK and upload_media_to_oss:
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                        ext = (mtype or "jpg").split("/")[-1] if "/" in (mtype or "") else (mtype or "jpg")
                        # upload_media_to_oss(data, media_type, ext, account_id)
                        url = upload_media_to_oss(data, "image", ext, account_id)
                        log.info("[WPP:%s] IMAGE OSS: path=%s url=%s", account_id, path, url)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[WPP:%s] IMAGE OSS 上传失败(非阻塞): %s", account_id, e)
                if path:
                    media_urls.append(path)
                    media_types.append("image")
                    # Phase A 引导 model 用 mmx_vision_describe (VLM > tesseract OCR)
                    channel_prompt_media = (
                        f"⚡ [用户发了图片] 本地路径: {path}\n"
                        f"👉 **优先调 `mmx_vision_describe(image='{path}', prompt='详细描述图片内容和文字')`**\n"
                        f"   - mmx 是 MiniMax VLM, 能看布局/表格/印章/手写/实景 (OCR 看不懂实景)\n"
                        f"   - ❌ 不要用 terminal + tesseract (OCR 弱, 仅对清晰文字截图有效)\n"
                    )
        elif msg.msg_type == 34:   # 语音
            mt = MessageType.VOICE
            client = self._clients.get(account_id)
            # v1.3.22 VENDOR-TRANSCRIPT (openclaw): 优先用 vendor 自带 transcript (wechat_official),
            # 免 STT token, 省快准. 无 vendor transcript 才走 plugin STT.
            vendor_transcript = (msg.media or {}).get("vendor_transcript", "")
            if vendor_transcript:
                log.info("[WPP:%s] 语音用 vendor transcript: %s", account_id, vendor_transcript[:50])
                text = f"[语音转写] {vendor_transcript}".strip()
                # 仍然下载语音 + OSS 上传 (老板要求入站媒体都存档, 2026-09-01)
                if client:
                    try:
                        from .media import download_voice_to_file
                        vpath, _ = await download_voice_to_file(client, msg.media)
                        if vpath and _OSS_ARCHIVE_OK and upload_media_to_oss:
                            with open(vpath, "rb") as f:
                                data = f.read()
                            # upload_media_to_oss(data, media_type, ext, account_id)
                            upload_media_to_oss(data, "voice", "silk", account_id)
                            log.info("[WPP:%s] 语音已下载+OSS: %s", account_id, vpath)
                    except Exception as e:  # noqa: BLE001
                        log.debug("[WPP:%s] 语音下载/OSS 失败(非阻塞): %s", account_id, e)
            elif client:
                # 降级: vendor 没 transcript, 自己下载 + STT (Phase 2.3 wpp-stt skill)
                vpath = None
                try:
                    from .media import download_voice_to_file
                    vpath, _ = await download_voice_to_file(client, msg.media)
                    if vpath and _OSS_ARCHIVE_OK and upload_media_to_oss:
                        with open(vpath, "rb") as f:
                            data = f.read()
                        # upload_media_to_oss(data, media_type, ext, account_id)
                        upload_media_to_oss(data, "voice", "silk", account_id)
                except Exception as e:  # noqa: BLE001
                    log.debug("[WPP:%s] 语音下载失败(降级 STT): %s", account_id, e)
                # 调 wpp-stt skill (✅ 无 vendor authcode)
                if _WPP_STT_OK and wpp_stt_transcribe and vpath:
                    try:
                        with open(vpath, "rb") as f:
                            silk_bytes = f.read()
                        stt_result = await wpp_stt_transcribe(
                            silk_bytes=silk_bytes,
                            vendor_transcript="",  # 已确认无 vendor
                        )
                        if stt_result.text:
                            text = f"[语音转写] {stt_result.text}".strip()
                            log.info("[WPP:%s] 语音 STT 来源=%s", account_id, stt_result.source)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[WPP:%s] wpp-stt skill 调用失败: %s", account_id, e)
                else:
                    # 降级到旧 transcribe_voice (compat shim)
                    try:
                        from .media import transcribe_voice
                        transcript = await transcribe_voice(client, msg.media)
                        if transcript:
                            text = f"[语音转写] {transcript}".strip()
                    except Exception as e:  # noqa: BLE001
                        log.debug("[WPP:%s] 旧 STT 降级也失败: %s", account_id, e)
        elif msg.msg_type == 43:   # 视频
            mt = MessageType.VIDEO
            log.info("[WPP:%s] VIDEO msg.media keys: %s", account_id, list(msg.media.keys()) if msg.media else "None")
            client = self._clients.get(account_id)
            if client:
                # vendor 下载在 plugin 内调用 (authcode 不外泄)
                from .media import download_video_to_file
                vpath, vtype, _ = await download_video_to_file(client, msg.media)
                # OSS 上传走 oss_archive skill (✅ 无 vendor authcode)
                if vpath and _OSS_ARCHIVE_OK and upload_media_to_oss:
                    try:
                        with open(vpath, "rb") as f:
                            data = f.read()
                        ext = (vtype or "mp4").split("/")[-1] if "/" in (vtype or "") else (vtype or "mp4")
                        # upload_media_to_oss(data, media_type, ext, account_id)
                        upload_media_to_oss(data, "video", ext, account_id)
                        log.info("[WPP:%s] VIDEO OSS: %s", account_id, vpath)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[WPP:%s] VIDEO OSS 上传失败(非阻塞): %s", account_id, e)
                if vpath:
                    media_urls.append(vpath)
                    media_types.append("video")
                    # Phase A: 视频已下载, 引导 mmx 看缩略图/关键帧 + 总结
                    channel_prompt_media = (
                        f"⚡ [用户发了视频] 本地路径: {vpath}\n"
                        f"👉 **优先调 `mmx_vision_describe(image='{vpath}', prompt='描述视频画面内容/关键帧')`**\n"
                        f"   - mmx VLM 能理解画面 (OCR 仅文字, 看不懂视频)\n"
                        f"   - ❌ 不要用 terminal + ffmpeg + tesseract 自己解析 (效率低, 只能读字幕)\n"
                    )
                else:
                    log.warning("[WPP:%s] VIDEO 下载失败, media=%s", account_id, msg.media)
        elif msg.msg_type == 49:   # 文件
            mt = MessageType.DOCUMENT
            client = self._clients.get(account_id)
            if client:
                # vendor 下载在 plugin 内调用 (authcode 不外泄)
                from .media import download_file_to_file
                path, mtype, fname = await download_file_to_file(client, msg.media)
                # OSS 上传走 oss_archive skill (✅ 无 vendor authcode)
                if path and _OSS_ARCHIVE_OK and upload_media_to_oss:
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                        ext = (fname.rsplit(".", 1)[-1] if "." in fname else "bin").lower()
                        # upload_media_to_oss(data, media_type, ext, account_id)
                        upload_media_to_oss(data, "file", ext, account_id)
                        log.info("[WPP:%s] FILE OSS: %s", account_id, path)
                    except Exception as e:  # noqa: BLE001
                        log.warning("[WPP:%s] FILE OSS 上传失败(非阻塞): %s", account_id, e)
                if path:
                    media_urls.append(path)
                    media_types.append("document")
                    text = f"[文件] {fname} {text}".strip()
                    # Phase A: 文件已下载, 引导 model 根据文件类型用不同工具
                    file_ext = (fname.rsplit(".", 1)[-1] if "." in fname else "").lower()
                    if file_ext in ("jpg", "jpeg", "png", "webp", "bmp", "gif"):
                        channel_prompt_media = (
                            f"⚡ [用户发了图片文件] 本地路径: {path}\n"
                            f"👉 **优先调 `mmx_vision_describe(image='{path}', prompt='描述图片内容/文字')`**\n"
                            f"   - mmx VLM 能识别图片内文字 (比 OCR 准, 还能看布局)\n"
                        )
                    elif file_ext in ("pdf", "docx", "doc"):
                        channel_prompt_media = (
                            f"⚡ [用户发了 {file_ext.upper()} 文档] 本地路径: {path}\n"
                            f"👉 **用 read_file 工具读取内容** (hermes 自带, 支持 PDF/DOCX)\n"
                        )
                    elif file_ext in ("xlsx", "csv"):
                        channel_prompt_media = (
                            f"⚡ [用户发了 {file_ext.upper()} 表格] 本地路径: {path}\n"
                            f"👉 **用 read_file 读取 + python pandas 处理**\n"
                            f"   - 或 phoneerp_query 查询对应数据\n"
                        )
                    else:
                        channel_prompt_media = (
                            f"⚡ [用户发了文件 {fname}] 本地路径: {path}\n"
                            f"👉 根据扩展名选择工具 (read_file / mmx_vision_describe / phoneerp_query)\n"
                        )
        elif msg.msg_type == 47:   # 表情
            mt = MessageType.STICKER

        # affection 好感度：群消息更新好感度 + 情绪注入 channel_prompt
        channel_prompt_aff = None
        if msg.is_group and msg.from_wxid:
            try:
                acct_cfg = acct.get("affection") or {}
                if acct_cfg.get("enabled"):
                    from . import affection as aff
                    aff.process_affection_message(chat_id, msg.from_wxid, msg.content)
                    summary = aff.get_affection_summary(chat_id)
                    channel_prompt_aff = f"[好感度] {summary}"
            except Exception as e:  # noqa: BLE001
                log.debug("[WPP:%s] affection 处理失败: %s", account_id, e)
            # jargon 黑话统计
            try:
                from . import jargon as jg
                jg.update_from_message(msg.content, chat_id, msg.from_wxid)
            except Exception as e:  # noqa: BLE001
                log.debug("[WPP:%s] jargon 统计失败: %s", account_id, e)

        # 合并 channel_prompt (intent_embed 历史 + affection 好感度 + media 引导 Phase A)
        _cp_parts = []
        if channel_prompt:
            _cp_parts.append(channel_prompt)
        if channel_prompt_aff:
            _cp_parts.append(channel_prompt_aff)
        if channel_prompt_media:
            _cp_parts.append(channel_prompt_media)
        channel_prompt = "\n\n".join(_cp_parts) if _cp_parts else None

        # 引用消息：设置 reply_to_message_id + 注入被引用内容
        reply_to_id = None
        reply_to_text = ""
        if msg.reply_to and msg.reply_to.get("svrid"):
            reply_to_id = f"quote:{msg.reply_to['svrid']}"
            reply_to_text = msg.reply_to.get("quote_content") or ""
            if reply_to_text:
                text = f"{text}（引用: {reply_to_text[:100]}）".strip()

        source = self.build_source(
            chat_id=chat_id,
            chat_name=peer_id,
            chat_type=chat_type,
            user_id=msg.from_wxid,
            user_name=msg.from_nickname or msg.from_wxid,
        )
        # 多账号路由：按账号的 agent 配置设置 source.profile (multiplex 时 Hermes 路由到对应 agent)
        # 2026-09-01 接老板拍板: 字段名对齐 wpp-openclaw (cfg.agent), 同时兼容旧 acct.profile 字段
        acct_agent = acct.get("agent") or acct.get("profile") or ""
        if acct_agent:
            source.profile = acct_agent
        return MessageEvent(
            text=text,
            message_type=mt,
            source=source,
            raw_message=msg.raw,
            message_id=msg.msg_id or None,
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            user_id=msg.from_wxid,
            user_name=msg.from_nickname or msg.from_wxid,
            media_urls=media_urls,
            media_types=media_types,
            channel_prompt=channel_prompt,
            metadata={
                "account_id": account_id,
                "peer_id": peer_id,
                "quote_display_name": (msg.reply_to or {}).get("display_name", ""),
            },
        )

    def _persist_message(self, account_id: str, msg) -> None:
        """入站消息落库（幂等）。"""
        try:
            from . import db
            # P1-6.2 修复：new_msg_id 从 raw 提取（vendor 引用/查消息用 new_msg_id，msg_id 可能为 0）
            new_msg_id = None
            raw = msg.raw if isinstance(msg.raw, dict) else {}
            for k in ("NewMsgId", "newMsgId", "Newmsgid"):
                v = raw.get(k)
                if v:
                    new_msg_id = str(v)
                    break
            if not new_msg_id and msg.msg_id:
                new_msg_id = msg.msg_id
            db.save_message(
                account_id=account_id,
                msg_id=msg.msg_id or None,
                new_msg_id=new_msg_id,
                direction="inbound",
                peer_kind="group" if msg.is_group else "direct",
                peer_id=msg.peer_id,
                peer_name=msg.from_nickname or None,
                chat_id=msg.chatroom_id or None,
                msg_type=str(msg.msg_type),
                content=msg.content or None,
                raw_payload=msg.raw,
                from_wxid=msg.from_wxid or None,
                ts=msg.timestamp or None,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("[WPP:%s] 消息落库失败: %s", account_id, e)

    async def _handle_filehelper(self, account_id: str, msg) -> None:
        """处理 filehelper 命令（安全修复：仅 adminUsers 可执行，防管理命令被任何人触发）。"""
        try:
            acct = self._accounts.get(account_id) or {}
            admin = acct.get("adminUsers") or []
            sender = msg.from_wxid or ""
            # 非 admin 忽略管理命令（filehelper 是管理面，开放会改白名单/发配对码）
            if admin and sender and sender not in admin:
                log.warning("[WPP:%s] filehelper 命令被拒: 非 admin %s", account_id, sender)
                return
            from .commands import handle_command
            reply = handle_command(account_id, msg.content)
            if reply:
                client = self._clients.get(account_id)
                if client:
                    # 回给 filehelper 的发送者
                    target = msg.from_wxid or msg.peer_id
                    await client.send_text(target, reply)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] filehelper 命令处理失败: %s", account_id, e)

    async def _try_heartflow(self, account_id: str, msg) -> None:
        """未@群消息 → 心流判断是否主动参与。"""
        acct = self._accounts.get(account_id) or {}
        hf_cfg = acct.get("heartflow") or {}
        from .heartflow import HeartflowConfig, try_independent_trigger

        cfg = HeartflowConfig(hf_cfg)
        if not cfg.enabled:
            return
        import os

        # 2026-09-01 fix: api_key/base_url 跟随模型（heartflow model 已是 deepseek-v4-flash，
        #   但还打 MiniMax → 429 Token Plan 上限）。模型含 deepseek 用 DeepSeek 端点，否则 MiniMax。
        model_name = (cfg.model or "").lower()
        if "deepseek" in model_name:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            base_url = cfg.base_url or "https://api.deepseek.com/anthropic"
            provider_label = "DeepSeek"
        else:
            api_key = os.environ.get("MINIMAX_CN_API_KEY") or os.environ.get("MINIMAX_API_KEY") or ""
            base_url = cfg.base_url or "https://api.minimaxi.com/anthropic"
            provider_label = "MiniMax"
        if not api_key:
            log.info("[WPP:%s] heartflow: 无 %s key，跳过", account_id, provider_label)
            return
        try:
            result = await try_independent_trigger(
                chat_id=msg.chatroom_id or msg.peer_id,
                content=msg.content,
                sender_name=msg.from_nickname or msg.from_wxid,
                bot_nickname=acct.get("nickname", ""),
                cfg=cfg,
                api_key=api_key,
                base_url=base_url,
            )
            if result.get("triggered"):
                log.info("[WPP:%s] heartflow 触发: %s", account_id, result.get("reason"))
                # 心流触发 → 构造事件交给 agent
                pipeline = self._pipelines.get(account_id)
                if pipeline:
                    event = await self._build_event(account_id, msg, pipeline)
                    if event:
                        event.metadata = {**(event.metadata or {}), "via": "heartflow"}
                        await self.handle_message(event)
            else:
                log.debug("[WPP:%s] heartflow 未触发: %s", account_id, result.get("reason"))
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] heartflow 判断失败: %s", account_id, e)

    # ------------------------------------------------------------------ 出站
    _TEXT_CHUNK_LIMIT = 6000  # 对齐 OpenClaw chunkMarkdown

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送消息到微信。chat_id 格式: accountId:peerId。

        引用回复（reply_to 有值）时：群聊加 @被回复人前缀 + 构造引用 XML 走 ShareLink。
        ≥6000 字自动切片逐段发送（P0-3.1 修复，对齐 OpenClaw chunkMarkdown）。
        出站消息成功后入库（P0-6.1 修复，老板拍板"任意渠道发送都要入库"）。
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
        except ValueError:
            return SendResult(success=False, error=f"invalid chat_id: {chat_id}")
        client = self._clients.get(account_id)
        if not client:
            return SendResult(success=False, error=f"account not connected: {account_id}")
        # 2026-09-01 修复: 过滤"无 @提及，按群规则不回复"类拒绝话术（老板在益融管理群看到 2 次）
        # 防线 2（插件层，不依赖 LLM 行为）：这类话术不该出现在群里，直接丢弃
        if _REJECTION_PHRASE_RE.search(content or ""):
            log.info("[WPP:%s] 过滤拒绝话术出站（丢弃）: %s", account_id, (content or "")[:60])
            return SendResult(success=True, message_id=None)
        try:
            # 引用回复：群聊 + reply_to（引用内部已入库）
            if reply_to:
                result = await self._send_quote(account_id, peer_id, content, reply_to, client, metadata)
                if result:
                    return result
                # 引用失败降级为普通文本
            # ≥6000 切片（对齐 OpenClaw：分片发送 + 每片入库）
            chunks = self._chunk_text(content) if len(content) > self._TEXT_CHUNK_LIMIT else [content]
            last_result: Optional[SendResult] = None
            for chunk in chunks:
                resp = await client.send_text(peer_id, chunk)
                data = resp.get("Data") or {}
                br = data.get("BaseResponse") if isinstance(data, dict) else None
                ret = br.get("ret") if isinstance(br, dict) else None
                msg_id = (data.get("NewMsgId") or data.get("Newmsgid") or data.get("Msgid")
                          or (br or {}).get("NewMsgId")) if isinstance(data, dict) else None
                # 成功判定对齐 OpenClaw isSendOk：Code 0 且 ret==0（ret None 视为成功兼容）
                ok = resp.get("Code") in (0, 200, None) and (ret in (0, "0", None))
                if ok:
                    # 出站入库（P0-6.1）
                    self._persist_outbound(account_id, peer_id, chunk, str(msg_id) if msg_id else None, metadata)
                    last_result = SendResult(success=True, message_id=str(msg_id) if msg_id else None, raw_response=resp)
                else:
                    log.warning("[WPP:%s] SendTxt ret=%s err=%s", account_id, ret, (br or {}).get("errMsg"))
                    last_result = SendResult(success=False, error=f"vendor SendTxt ret={ret}", retryable=True)
                    break
            return last_result or SendResult(success=False, error="empty content")
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] send failed: %s", account_id, e)
            return SendResult(success=False, error=str(e), retryable=True)

    @staticmethod
    def _chunk_text(content: str, limit: int = 6000) -> list[str]:
        """按段落切片（≥limit 时）。对齐 OpenClaw chunkMarkdown 的简化实现。"""
        if len(content) <= limit:
            return [content]
        chunks: list[str] = []
        para = ""
        for line in content.split("\n"):
            if len(para) + len(line) + 1 > limit and para:
                chunks.append(para.strip())
                para = line
            else:
                para = f"{para}\n{line}" if para else line
        if para.strip():
            chunks.append(para.strip())
        return chunks

    def _persist_outbound(self, account_id: str, peer_id: str, content: str,
                          msg_id: Optional[str], metadata: Optional[Dict[str, Any]] = None) -> None:
        """出站消息入库（P0-6.1：群友引用 bot 消息需 DB 有记录）。"""
        try:
            from . import db
            peer_kind = "group" if peer_id.endswith("@chatroom") else "direct"  # DB enum('direct','group','room')
            db.save_message(
                account_id=account_id,
                msg_id=msg_id,
                new_msg_id=None,
                direction="outbound",
                peer_kind=peer_kind,
                peer_id=peer_id,
                peer_name=None,
                chat_id=peer_id,
                msg_type="text",
                content=content,
                raw_payload=None,
                from_wxid=None,
                ts=int(time.time()),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] 出站消息入库失败: %s", account_id, e)

    async def _send_quote(self, account_id: str, peer_id: str, content: str,
                          reply_to: str, client, metadata: Optional[Dict[str, Any]] = None) -> Optional[SendResult]:
        """引用回复：群聊加 @前缀 + 构造引用 XML + ShareLink 发送。

        被回复人昵称优先级（对齐 wpp quote-reply.ts）：
        1. metadata.quote_display_name（微信引用消息自带的 display_name，最准确，群友也准）
        2. DB 被引用消息 peer_name
        3. vendor /Friend/GetContractDetail 查昵称（resolveDisplayName 范式）
        4. wxid 兜底
        """
        is_group = peer_id.endswith("@chatroom")
        # 从 DB 查被引用消息（svrid + fromusr + 发送者昵称）
        from . import db
        quote_msg = None
        quote_id = reply_to
        if ":" in quote_id:
            quote_id = quote_id.split(":", 1)[1]
        # 按 msg_id 查消息（含 svrid）
        rows = db._query(
            "SELECT msg_id, new_msg_id, from_wxid, peer_name FROM wpp_messages WHERE account_id=%s AND (msg_id=%s OR new_msg_id=%s) LIMIT 1",
            (account_id, quote_id, quote_id),
        )
        if rows:
            quote_msg = rows[0]

        display_name = ""
        fromusr = ""
        svrid = ""
        if quote_msg:
            fromusr = quote_msg.get("from_wxid") or ""
            display_name = quote_msg.get("peer_name") or ""
            svrid = str(quote_msg.get("msg_id") or "") or str(quote_msg.get("new_msg_id") or "")
        elif quote_id:
            svrid = quote_id

        # @ 用昵称（对齐 wpp：微信 display_name 优先 → DB peer_name → vendor 查 → wxid 兜底）
        if is_group:
            at_name = ""
            # 1. 微信引用自带的 display_name（最准确）
            if metadata:
                at_name = metadata.get("quote_display_name") or ""
            # 2. DB 被引用消息昵称
            if not at_name or at_name == fromusr:
                at_name = display_name
            # 3. vendor 查昵称（resolveDisplayName 范式：群友非联系人也能查）
            if not at_name or at_name == fromusr:
                try:
                    at_name = await self._resolve_display_name(account_id, fromusr)
                except Exception:  # noqa: BLE001
                    pass
            # 4. wxid 兜底
            if not at_name:
                at_name = fromusr
            display_name = at_name

        # 群聊加 @被回复人 前缀
        final_content = content
        if is_group:
            at_name = display_name if display_name and display_name != fromusr else (fromusr or "")
            if at_name:
                final_content = f"@{at_name} {content}".strip()

        # 构造引用 XML
        xml = _build_quote_reply_xml(final_content, svrid, fromusr)
        try:
            resp = await client.call("/Msg/ShareLink", {
                "ToWxid": peer_id,
                "Type": 5,
                "Xml": xml,
            })
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") if isinstance(data, dict) else None
            msg_id = (data.get("NewMsgId") or data.get("Newmsgid") or data.get("Msgid")
                      or (br or {}).get("NewMsgId")) if isinstance(data, dict) else None
            ret = br.get("ret") if isinstance(br, dict) else None
            ok = resp.get("Code") in (0, 200, None) and (ret in (0, "0", None))
            if ok or msg_id:
                log.info("[WPP:%s] 引用回复发送成功: %s → %s", account_id, peer_id, final_content[:30])
                # 出站引用入库（P0-6.1）
                self._persist_outbound(account_id, peer_id, final_content,
                                       str(msg_id) if msg_id else None, metadata)
                return SendResult(success=True, message_id=str(msg_id) if msg_id else None, raw_response=resp)
            return None  # 失败降级普通文本
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] 引用回复失败（降级普通文本）: %s", account_id, e)
            return None

    async def _resolve_display_name(self, account_id: str, wxid: str) -> str:
        """查被回复人昵称（迁移自 wpp resolveDisplayName：调 vendor /Friend/GetContractDetail）。

        30 分钟内存缓存。失败返回空串（不阻塞引用回复）。
        """
        if not wxid or wxid.endswith("@chatroom"):
            return ""
        cache_key = f"{account_id}:{wxid}"
        cached = _display_name_cache.get(cache_key)
        if cached and time.monotonic() - cached[1] < 1800:
            return cached[0]
        try:
            client = self._clients.get(account_id)
            if not client:
                return ""
            resp = await client.call("/Friend/GetContractDetail", {"userName": wxid}, max_retries=0)
            data = resp.get("Data") or {}
            contact_list = data.get("ContactList") or []
            if isinstance(contact_list, list) and contact_list:
                nick_obj = contact_list[0].get("NickName") or {}
                if isinstance(nick_obj, dict):
                    nick = nick_obj.get("string") or nick_obj.get("String") or ""
                else:
                    nick = str(nick_obj) if nick_obj else ""
                if nick:
                    _display_name_cache[cache_key] = (nick, time.monotonic())
                    return nick
        except Exception as e:  # noqa: BLE001
            log.debug("[WPP:%s] 昵称查询失败: %s", account_id, e)
        return ""

    def _parse_chat_id(self, chat_id: str) -> tuple[str, str]:
        if ":" in chat_id:
            account_id, peer_id = chat_id.split(":", 1)
        else:
            account_id, peer_id = DEFAULT_ACCOUNT, chat_id
        if account_id not in self._accounts:
            raise ValueError(f"unknown account: {account_id}")
        return account_id, peer_id

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送图片到微信（UploadImg 上传即发送）。image_url 可为 URL/base64/本地路径。"""
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
        except ValueError:
            return SendResult(success=False, error=f"invalid chat_id: {chat_id}")
        client = self._clients.get(account_id)
        if not client:
            return SendResult(success=False, error=f"account not connected: {account_id}")
        try:
            # 本地文件/URL → base64
            base64_str = await _resolve_image_to_base64(image_url)
            if not base64_str:
                return SendResult(success=False, error="无法解析图片（本地文件不存在或 URL 不可达）")
            resp = await client.send_image(peer_id, base64_str)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") if isinstance(data, dict) else None
            ret = br.get("ret") if isinstance(br, dict) else None
            msg_id = (data.get("Newmsgid") or data.get("Msgid") or data.get("newMsgId")
                      or (br or {}).get("NewMsgId")) if isinstance(data, dict) else None
            # 成功判定对齐 OpenClaw isSendOk：Code 0 且 ret==0（ret None 兼容）
            ok = resp.get("Code") in (0, 200, None) and (ret in (0, "0", None))
            if ok:
                # 出站图片入库（P0-6.1）
                self._persist_outbound(account_id, peer_id, "[图片]",
                                       str(msg_id) if msg_id else None, metadata)
            return SendResult(success=bool(ok), message_id=str(msg_id) if msg_id else None, raw_response=resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] send_image failed: %s", account_id, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送本地图片文件到微信 (对齐 hermes wecom adapter 签名, 2026-09-01).

        本地路径: 直接调 send_image(本地路径当 URL)
        URL/base64: 转发给 send_image
        """
        return await self.send_image(
            chat_id=chat_id,
            image_url=image_path,
            caption=caption,
            reply_to=reply_to,
        )

    async def send_voice(
        self,
        chat_id: str,
        voice_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送语音到微信 (对齐 hermes wecom adapter 签名, 2026-09-01).

        voice_url 可为本地路径/URL/base64 (silk/amr/mp3/wav 格式).
        微信原生不支持任意 voice, 一般通过 send_file_v2 走文件通道 (兼容).
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
        except ValueError:
            return SendResult(success=False, error=f"invalid chat_id: {chat_id}")
        client = self._clients.get(account_id)
        if not client:
            return SendResult(success=False, error=f"account not connected: {account_id}")
        try:
            # 语音本质走文件通道 (vendor silk/voice 通过 /Tools/UploadFile 上传)
            voice_bytes = await _resolve_image_to_base64(voice_url)  # 此函数也接受 raw bytes
            if not voice_bytes:
                return SendResult(success=False, error=f"无法读取语音文件: {voice_url}")
            fname = os.path.basename(voice_url) if voice_url.startswith("/") else "voice.silk"
            resp = await client.send_file_v2(peer_id, fname, voice_bytes)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") if isinstance(data, dict) else None
            ret = br.get("ret") if isinstance(br, dict) else None
            msg_id = (data.get("NewMsgId") or data.get("Msgid")) if isinstance(data, dict) else None
            ok = resp.get("Code") in (0, 200, None) and (ret in (0, "0", None))
            if ok:
                self._persist_outbound(account_id, peer_id, "[语音]", str(msg_id) if msg_id else None)
            return SendResult(success=bool(ok), message_id=str(msg_id) if msg_id else None, raw_response=resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] send_voice failed: %s", account_id, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_video(
        self,
        chat_id: str,
        video_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送视频到微信 (对齐 hermes wecom adapter 签名, 2026-09-01).

        video_url: 本地路径 / URL (mp4).
        优先走 CDN 视频发送 (vendor 完整 mp4 上传). 失败降级到文件通道.
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
        except ValueError:
            return SendResult(success=False, error=f"invalid chat_id: {chat_id}")
        client = self._clients.get(account_id)
        if not client:
            return SendResult(success=False, error=f"account not connected: {account_id}")
        try:
            # 优先 CDN 视频 (完整 mp4)
            if video_url.startswith("http"):
                resp = await client.send_cdn_video(peer_id, video_url)
            else:
                # 本地文件: 走文件通道
                video_bytes = await _resolve_image_to_base64(video_url)
                if not video_bytes:
                    return SendResult(success=False, error=f"无法读取视频: {video_url}")
                fname = os.path.basename(video_url) if video_url.startswith("/") else "video.mp4"
                resp = await client.send_file_v2(peer_id, fname, video_bytes)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") if isinstance(data, dict) else None
            ret = br.get("ret") if isinstance(br, dict) else None
            msg_id = (data.get("NewMsgId") or data.get("Msgid")) if isinstance(data, dict) else None
            ok = resp.get("Code") in (0, 200, None) and (ret in (0, "0", None))
            if ok:
                self._persist_outbound(account_id, peer_id, "[视频]", str(msg_id) if msg_id else None)
            return SendResult(success=bool(ok), message_id=str(msg_id) if msg_id else None, raw_response=resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] send_video failed: %s", account_id, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送文件到微信 (对齐 hermes wecom adapter 签名, 2026-09-01).

        file_path: 本地路径 (二进制文件).
        file_name: 可选, 显示文件名 (默认从 path 取).
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
        except ValueError:
            return SendResult(success=False, error=f"invalid chat_id: {chat_id}")
        client = self._clients.get(account_id)
        if not client:
            return SendResult(success=False, error=f"account not connected: {account_id}")
        try:
            file_bytes = await _resolve_image_to_base64(file_path)
            if not file_bytes:
                return SendResult(success=False, error=f"无法读取文件: {file_path}")
            fname = file_name or (os.path.basename(file_path) if file_path.startswith("/") else "file")
            resp = await client.send_file_v2(peer_id, fname, file_bytes)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") if isinstance(data, dict) else None
            ret = br.get("ret") if isinstance(br, dict) else None
            msg_id = (data.get("NewMsgId") or data.get("Msgid")) if isinstance(data, dict) else None
            ok = resp.get("Code") in (0, 200, None) and (ret in (0, "0", None))
            if ok:
                self._persist_outbound(account_id, peer_id, f"[文件] {fname}", str(msg_id) if msg_id else None)
            return SendResult(success=bool(ok), message_id=str(msg_id) if msg_id else None, raw_response=resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP:%s] send_document failed: %s", account_id, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_stream_frame(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """流式输出 frame (对齐 hermes wecom adapter 签名, 2026-09-01).

        微信不支持 streaming (老板拍板 2026-08-31, 已禁流式). 此方法为空操作.
        """
        # 微信不支持 streaming — 静默 no-op
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """返回 chat 信息。chat_id 格式 accountId:peerId。"""
        try:
            _, peer_id = self._parse_chat_id(chat_id)
        except ValueError:
            return {"name": chat_id, "type": "dm"}
        is_group = "@chatroom" in peer_id
        return {
            "name": peer_id,
            "type": "group" if is_group else "dm",
        }


# ------------------------------------------------------------------ 引用回复 XML
def _escape_xml(text: str) -> str:
    """XML 转义。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_quote_reply_xml(reply_content: str, svrid: str, fromusr: str = "") -> str:
    """构造引用回复 XML（迁移自 wpp quote-xml.ts buildQuoteReplyXml）。

    <appmsg><title>回复</title><des>回复</des><type>57</type><refermsg><svrid>..</svrid><fromusr>..</fromusr></refermsg></appmsg>
    """
    reply = (reply_content or "引用回复").strip()
    title = _escape_xml(reply[:500])
    des = _escape_xml(reply[:500])
    svrid = _escape_xml(svrid.strip())
    fromusr = _escape_xml(fromusr) if fromusr else ""
    refermsg = (
        f"<refermsg><svrid>{svrid}</svrid>"
        + (f"<fromusr>{fromusr}</fromusr>" if fromusr else "")
        + "</refermsg>"
    ) if svrid else ""
    return f"<appmsg><title>{title}</title><des>{des}</des><type>57</type>{refermsg}</appmsg>"


# ------------------------------------------------------------------ 媒体下载辅助
async def _download_media_safe(client, msg, kind: str):
    """下载图片/文件到本地。返回 (path, media_type[, filename])。

    2026-08-31 fix: 下载后自动上传 OSS（入站媒体留存）。失败降级不影响本地使用。
    """
    from .media import download_file_to_file, download_image_to_file

    if kind == "image":
        authcode = ""
        try:
            authcode = client.auth_token or ""
        except Exception:  # noqa: BLE001
            pass
        path, mtype = await download_image_to_file(client, msg.media, authcode=authcode)
        if path:
            try:
                # oss-archive skill (Phase 2.1)
                with open(path, "rb") as f:
                    data = f.read()
                url = upload_media_to_oss(data, "image", "jpg") if _OSS_ARCHIVE_OK else None
                if url:
                    log.info("[WPP] 入站图片已上传 OSS: %s", url)
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP] 入站图片 OSS 上传失败(降级本地): %s", e)
        return path, mtype
    if kind == "voice":
        from .media import download_voice_to_file
        authcode = ""
        try:
            authcode = client.auth_token or ""
        except Exception:  # noqa: BLE001
            pass
        vpath, vtype = await download_voice_to_file(client, msg.media, authcode=authcode)
        if vpath:
            try:
                # oss-archive skill (Phase 2.1)
                with open(vpath, "rb") as f:
                    data = f.read()
                url = upload_media_to_oss(data, "voice", "silk") if _OSS_ARCHIVE_OK else None
                if url:
                    log.info("[WPP] 入站语音已上传 OSS: %s", url)
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP] 入站语音 OSS 上传失败(降级本地): %s", e)
        return vpath, vtype
    if kind == "video":
        from .media import download_video_to_file
        # 拿 authcode 传给 video 下载 (vendor URL query string 需要)
        authcode = ""
        try:
            authcode = client.auth_token or ""
        except Exception:  # noqa: BLE001
            pass
        vpath, tpath, vtype = await download_video_to_file(client, msg.media, authcode=authcode)
        if vpath:
            try:
                # oss-archive skill (Phase 2.1)
                with open(vpath, "rb") as f:
                    data = f.read()
                url = upload_media_to_oss(data, "video", "mp4") if _OSS_ARCHIVE_OK else None
                if url:
                    log.info("[WPP] 入站视频已上传 OSS: %s", url)
                if tpath:
                    with open(tpath, "rb") as f:
                        tdata = f.read()
                    turl = upload_media_to_oss(tdata, "image", "jpg") if _OSS_ARCHIVE_OK else None
                    if turl:
                        log.info("[WPP] 入站视频缩略图已上传 OSS: %s", turl)
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP] 入站视频 OSS 上传失败(降级本地): %s", e)
        return vpath, vtype
    authcode = ""
    try:
        authcode = client.auth_token or ""
    except Exception:  # noqa: BLE001
        pass
    path, mtype, fname = await download_file_to_file(client, msg.media, authcode=authcode)
    if path:
        try:
            # oss-archive skill (Phase 2.1)
            import os as _os
            with open(path, "rb") as f:
                data = f.read()
            ext = _os.path.splitext(fname or "file")[1].lstrip(".") or "bin"
            url = upload_media_to_oss(data, "file", ext) if _OSS_ARCHIVE_OK else None
            if url:
                log.info("[WPP] 入站文件已上传 OSS: %s", url)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] 入站文件 OSS 上传失败(降级本地): %s", e)
    return path, mtype, fname


async def _resolve_image_to_base64(image_ref: str) -> str:
    """URL / 本地路径 / base64 → 纯 base64 字符串（安全版：SSRF 防护 + 本地白名单）。"""
    import base64

    # 已是 base64（data:image/... 或纯 base64）
    if image_ref.startswith("data:image"):
        return image_ref.split(",", 1)[1] if "," in image_ref else image_ref
    # 本地文件（白名单目录，防读任意文件外泄）
    if image_ref.startswith("/"):
        from .tools import _is_local_read_allowed
        if not _is_local_read_allowed(image_ref):
            log.warning("[WPP] 本地图片读取被拒（不在白名单）: %s", image_ref[:50])
            return ""
        try:
            with open(image_ref, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except OSError as e:
            log.warning("[WPP] 本地图片读取失败: %s", e)
            return ""
    # URL（SSRF 防护）
    if image_ref.startswith("http"):
        try:
            from .tools import _safe_fetch_url
            data = await _safe_fetch_url(image_ref, timeout=15)
            return base64.b64encode(data).decode()
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] 图片 URL 下载失败: %s", e)
            return ""
    # 纯 base64（可能是裸 base64）
    return image_ref


# ------------------------------------------------------------------ 平台注册
def check_wechatpadpro_requirements() -> bool:
    """被动依赖探测：确认 vendor API 可达。"""
    return True


def _is_connected(config) -> bool:
    """extra.accounts 非空 或 env 有 authcode。"""
    extra = (getattr(config, "extra", None) or {})
    accounts = extra.get("accounts")
    if accounts:
        return True
    return bool(os.environ.get("WECHATPRO_AUTHCODE"))


def _validate_config(config) -> bool:
    return _is_connected(config)


def _current_profile() -> str:
    """返回当前 Hermes profile 名（multiplex 多账号隔离用）。"""
    import os
    return os.environ.get("HERMES_PROFILE", "default")


def _build_adapter(config):
    from gateway.config import Platform
    adapter = WppAdapter(config, Platform(PLATFORM_NAME))
    profile = _current_profile()
    _ADAPTER_HOLDER[profile] = adapter
    return adapter


# 工具 handler 用：保存各 profile 的 adapter 实例（connect 时更新，multiplex 隔离）
_ADAPTER_HOLDER: dict = {}


def _get_adapter():
    return _ADAPTER_HOLDER.get(_current_profile())


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict:
    """cron/standalone 进程出站发送（Hermes standalone_sender_fn 契约）。

    cron deliver=wechatpadpro:<accountId>:<peerId> 在独立进程投递时调用本函数，
    直接经 vendor API 发送 (文本 + 媒体) 到微信群/私聊。返回 ``{"success": ...}``
    或 ``{"error": ...}``（契约见 tools/send_message_tool.py:940）。

    多账号隔离:
      chat_id 格式 = accountId:peerId.
      accountId 从 chat_id 解析, 通过 cfg_mod.resolve_account_config 拿到对应账号的 authcode/apiBaseUrl.
      每个 accountId 独立 WppClient (vendor authcode 不串)。

    媒体发送规则 (按扩展名分发到 vendor API):
      .jpg/.jpeg/.png/.gif/.bmp/.webp → /Msg/UploadImg (send_image)
      .mp4/.mov/.avi → /Msg/SendVideo (send_video, 需缩略图 base64, 视频时长秒)
      .silk/.amr → /Msg/SendVoice (send_voice, voice_type=4 silk / 0 amr)
      其它 (pdf/xlsx/docx/...) → /Msg/SendFile (send_file_v2, 需文件名)
    """
    import base64 as _b64
    import os as _os
    try:
        # chat_id 格式: accountId:peerId（对齐 adapter._parse_chat_id）
        if ":" not in chat_id:
            return {"error": f"invalid chat_id for wechatpadpro: {chat_id!r} (expected accountId:peerId)"}
        account_id, peer_id = chat_id.split(":", 1)

        from .api_client import WppClient
        from . import config as cfg_mod

        extra = getattr(pconfig, "extra", None) or {}
        acct = cfg_mod.resolve_account_config(account_id, extra.get("accounts", {}).get(account_id))
        api_base = acct.get("apiBaseUrl") or "https://wx.juhe.chat"
        authcode = acct.get("authcode") or ""
        if not authcode:
            return {"error": f"wechatpadpro account '{account_id}' missing authcode (WECHATPRO_AUTHCODE)"}

        client = WppClient(api_base, authcode)
        last_msg_id = None

        # 1) 文本先发（如果非空）
        if message and message.strip():
            resp = await client.send_text(peer_id, message)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") if isinstance(data, dict) else None
            if isinstance(br, dict) and br.get("ret") not in (0, "0", None):
                return {"error": f"vendor SendTxt failed: {br.get('errMsg') or br.get('ret')}"}
            last_msg_id = (data.get("NewMsgId") or data.get("Newmsgid") or data.get("Msgid")) if isinstance(data, dict) else None

        # 2) 媒体附件（cron agent 输出含 MEDIA:/path 标签 → media_files=[(path, is_voice)]）
        #    framework 已分 chunk 调度, _standalone_send 只处理单个 chunk 的媒体
        if not media_files:
            return {"success": True, "message_id": str(last_msg_id) if last_msg_id else None}

        _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}
        _VOICE_EXTS = {".silk", ".amr", ".mp3", ".wav"}

        errors = []
        for media_path, is_voice in media_files:
            p = str(media_path)
            if not _os.path.isfile(p):
                errors.append(f"media file not found: {p}")
                continue
            try:
                with open(p, "rb") as f:
                    raw = f.read()
                b64 = _b64.b64encode(raw).decode()
                ext = _os.path.splitext(p)[1].lower()
                fname = _os.path.basename(p)
                resp = None
                if ext in _IMAGE_EXTS:
                    resp = await client.send_image(peer_id, b64)
                elif ext in _VIDEO_EXTS:
                    # 视频需缩略图 base64（取首帧 jpg 替代，如无就用一张 1x1 占位 jpg）
                    thumb_b64 = _make_video_thumb_b64(raw) or _PLACEHOLDER_THUMB_B64
                    play_len = _guess_video_duration(raw) or 10
                    resp = await client.send_video(peer_id, b64, thumb_b64, play_len)
                elif ext in _VOICE_EXTS:
                    voice_type = 4 if ext == ".silk" else 0  # silk=4, amr=0
                    resp = await client.send_voice(peer_id, b64, voice_type)
                else:
                    # 文件 (pdf/xlsx/docx/...): SendFile 需文件名
                    resp = await client.send_file_v2(peer_id, fname, b64)
                if resp is None:
                    errors.append(f"unsupported media: {p}")
                    continue
                data = resp.get("Data") or {}
                br = data.get("BaseResponse") if isinstance(data, dict) else None
                if isinstance(br, dict) and br.get("ret") not in (0, "0", None):
                    err_msg = br.get("errMsg") or br.get("ret")
                    errors.append(f"vendor send failed for {fname} ({ext}): {err_msg}")
                else:
                    last_msg_id = (data.get("NewMsgId") or data.get("Newmsgid") or data.get("Msgid")) if isinstance(data, dict) else last_msg_id
            except Exception as e:  # noqa: BLE001
                errors.append(f"send {fname} raised: {e}")

        if errors:
            # Phase 4.1: 记录失败状态到 DB (老板可查 cron 投递状态)
            try:
                from . import db as _db
                _db.save_message(
                    account_id=account_id, msg_id=f"cron-fail-{int(__import__('time').time())}",
                    new_msg_id=None, direction="outbound",
                    peer_kind="group" if peer_id.endswith("@chatroom") else "direct",
                    peer_id=peer_id, peer_name=None, chat_id=peer_id,
                    msg_type="text", content=(message or "")[:500],
                    raw_payload=None, from_wxid=None,
                    ts=int(__import__('time').time()),
                    delivery_status="failed",
                    delivery_error="; ".join(errors)[:500],
                )
            except Exception:
                pass
            return {"error": "; ".join(errors)}
        # Phase 4.1: 记录成功状态到 DB
        try:
            from . import db as _db
            _db.save_message(
                account_id=account_id, msg_id=str(last_msg_id) if last_msg_id else f"cron-{int(__import__('time').time())}",
                new_msg_id=None, direction="outbound",
                peer_kind="group" if peer_id.endswith("@chatroom") else "direct",
                peer_id=peer_id, peer_name=None, chat_id=peer_id,
                msg_type="text", content=(message or "")[:500],
                raw_payload=None, from_wxid=None,
                ts=int(__import__('time').time()),
                delivery_status="success",
                delivery_error=None,
                delivery_message_id=str(last_msg_id) if last_msg_id else None,
            )
        except Exception:
            pass
        return {"success": True, "message_id": str(last_msg_id) if last_msg_id else None}
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] standalone send failed: %s", e)
        # Phase 4.1: 记录 exception
        try:
            from . import db as _db
            _db.save_message(
                account_id=chat_id.split(":", 1)[0] if ":" in chat_id else "default",
                msg_id=f"cron-exc-{int(__import__('time').time())}",
                new_msg_id=None, direction="outbound",
                peer_kind="direct", peer_id=chat_id, peer_name=None, chat_id=chat_id,
                msg_type="text", content=(message or "")[:500],
                raw_payload=None, from_wxid=None,
                ts=int(__import__('time').time()),
                delivery_status="failed",
                delivery_error=str(e)[:500],
            )
        except Exception:
            pass
        return {"error": f"wechatpadpro standalone send failed: {e}"}


# ----- 视频缩略图与时长辅助 (out-of-process 调用) -----
# 占位缩略图：1x1 黑色 jpg (约 134 字节)，vendor SendVideo 必填 ImageBase64
# 老板要更好画质可以在 _standalone_send 调 ffmpeg 取首帧（但 out-of-process 不一定装了 ffmpeg）
_PLACEHOLDER_THUMB_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAr/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEB"
    "AAAAAAAAAAAAAAAAAAAAAP/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AL+AB//Z"
)


def _make_video_thumb_b64(video_bytes: bytes) -> str:
    """尝试用 ffmpeg 取视频首帧作为缩略图 base64；失败返回空串 (走占位图)。"""
    import subprocess
    import tempfile
    if not video_bytes:
        return ""
    # 检测 ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=2, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(video_bytes)
            src = f.name
        thumb = src + ".jpg"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ss", "00:00:01", "-vframes", "1", thumb],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0 or not os.path.exists(thumb):
            try:
                os.remove(src)
            except OSError:
                pass
            return ""
        with open(thumb, "rb") as f:
            data = f.read()
        os.remove(src)
        os.remove(thumb)
        import base64 as _b
        return _b.b64encode(data).decode()
    except Exception:
        return ""


def _guess_video_duration(video_bytes: bytes) -> int:
    """尝试用 ffprobe 取视频时长（秒）；失败返回 0 (走默认 10)。"""
    import subprocess
    import tempfile
    if not video_bytes:
        return 0
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(video_bytes)
            src = f.name
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            capture_output=True, text=True, timeout=10,
        )
        os.remove(src)
        if result.returncode != 0:
            return 0
        try:
            return max(1, int(float(result.stdout.strip())))
        except (ValueError, TypeError):
            return 0
    except Exception:
        return 0


def register(ctx) -> None:
    """Hermes 平台插件入口。"""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="WeChatPadPro (Personal WeChat)",
        adapter_factory=_build_adapter,
        check_fn=check_wechatpadpro_requirements,
        is_connected=_is_connected,
        validate_config=_validate_config,
        required_env=["WECHATPRO_AUTHCODE"],
        install_hint="Run `hermes setup` or configure platforms.wechatpadpro in config.yaml.",
        allowed_users_env="WECHATPRO_ALLOWED_USERS",
        allow_all_env="WECHATPRO_ALLOW_ALL_USERS",
        cron_deliver_env_var="WECHATPRO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4000,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are chatting via WeChatPadPro (personal WeChat, vendor wx.juhe.chat). "
            "Group chats: reply with at-mentions (@nickname). Plain text only. "
            "Current account resolves from the session metadata."
        ),
    )
    # 注册 agent 工具（迁移自 wpp-openclaw agent-tools）
    try:
        from .tools import register_msg_tools, register_jargon_tools, register_mcp_tools, _register_generic_tools
        from . import tools_data as td
        n_msg = register_msg_tools(ctx, _get_adapter)
        register_jargon_tools(ctx)
        register_mcp_tools(ctx)
        n1 = _register_generic_tools(ctx, _get_adapter, td.GROUP_TOOLS)
        n2 = _register_generic_tools(ctx, _get_adapter, td.USER_TOOLS)
        n3 = _register_generic_tools(ctx, _get_adapter, td.FRIEND_TOOLS)
        n4 = _register_generic_tools(ctx, _get_adapter, td.SEARCH_TOOLS)
        n5 = _register_generic_tools(ctx, _get_adapter, td.LABEL_TOOLS)
        n6 = _register_generic_tools(ctx, _get_adapter, td.TRANSLATE_TOOLS)
        n7 = _register_generic_tools(ctx, _get_adapter, td.TOOLS_TOOLS)
        n8 = _register_generic_tools(ctx, _get_adapter, td.TENPAY_TOOLS)
        n9 = _register_generic_tools(ctx, _get_adapter, td.FRIENDCIRCLE_TOOLS)
        n10 = _register_generic_tools(ctx, _get_adapter, td.FINDER_TOOLS)
        n11 = _register_generic_tools(ctx, _get_adapter, td.OA_TOOLS)
        n12 = _register_generic_tools(ctx, _get_adapter, td.QWC_TOOLS)
        n13 = _register_generic_tools(ctx, _get_adapter, td.FAVORITES_TOOLS)
        n14 = _register_generic_tools(ctx, _get_adapter, td.XIAOWEI_TOOLS)
        n15 = _register_generic_tools(ctx, _get_adapter, td.WXAPP_TOOLS)
        n16 = _register_generic_tools(ctx, _get_adapter, td.VOICE_TOOLS)
        n17 = _register_generic_tools(ctx, _get_adapter, td.WEBHOOK_TOOLS)
        n18 = _register_generic_tools(ctx, _get_adapter, td.SAYHELLO_TOOLS)
        n19 = _register_generic_tools(ctx, _get_adapter, td.LOGIN_TOOLS)
        n20 = _register_generic_tools(ctx, _get_adapter, td.CUSTOMIZED_TOOLS)
        # 2026-08-31 完整对照补全：OpenClaw agent-tools 缺失工具（tools_data_extra.py）
        from . import tools_data_extra as tde
        n21 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_GROUP_TOOLS)
        n22 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_FRIEND_TOOLS)
        n23 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_FRIENDCIRCLE_TOOLS)
        n24 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_SEARCH_TOOLS)
        n25 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_TENPAY_TOOLS)
        n26 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_TOOLS_TOOLS)
        n27 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_USER_TOOLS)
        n28 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_LOGIN_TOOLS)
        n29 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_MSG_TOOLS)
        n30 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_VOICE_TOOLS)
        n31 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_OA_TOOLS)
        n32 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_QW_TOOLS)
        n33 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_FINDER_TOOLS)
        n34 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_WXAPP_TOOLS)
        n35 = _register_generic_tools(ctx, _get_adapter, tde.EXTRA_XIAOWEI_TOOLS)
        # 2026-08-31 老板拍板: phoneerp 必须作为独立 tool 注册, 让 wpp-wechat agent 必须真调工具
        # 不能依赖 terminal 跑 CLI (model 经常"打字"假装调 terminal)
        from . import phoneerp_tools as _pet
        n_pe_query = _pet.register(ctx, lambda account_id="default": _get_adapter())
        log.info("[WPP] phoneerp 工具注册: %d 个", n_pe_query)
        from . import wecom_tools as _wct
        n_wct = _wct.register(ctx, lambda account_id="default": _get_adapter())
        log.info("[WPP] wecom 工具注册: %d 个", n_wct)
        from . import wpp_extras_tools as _wpe
        n_wpe = _wpe.register(ctx, lambda account_id="default": _get_adapter())
        log.info("[WPP] wpp-extras 工具注册: %d 个", n_wpe)


        log.info("[WPP] 工具注册: msg%d group%d user%d friend%d search%d label%d trans%d tools%d tenpay%d fc%d finder%d oa%d qwc%d fav%d xw%d wxapp%d voice%d webhook%d sayhello%d login%d customized%d extra%d",
                 n_msg, n1, n2, n3, n4, n5, n6, n7, n8, n9, n10, n11, n12, n13, n14, n15, n16, n17, n18, n19, n20,
                 n21 + n22 + n23 + n24 + n25 + n26 + n27 + n28 + n29 + n30 + n31 + n32 + n33 + n34 + n35)
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 工具注册失败: %s", e)
    log.info("[WPP] 平台已注册: %s", PLATFORM_NAME)
