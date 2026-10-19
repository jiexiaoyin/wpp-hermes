"""厂商侧 webhook 注册（从 adapter.py 拆出的 WebhookMixin）。

三段式（主 / business / 本地服务）注册、周期刷新、失败退避都在这里。
方法只依赖 ``self`` 上的属性（``_webhook`` / ``_webhook_accounts`` /
``_webhook_tasks`` / ``_clients``），因此用 mixin 搬到本文件是 MRO 等价迁移：
``WppAdapter(WebhookMixin, BasePlatformAdapter)`` 中 mixin 在前，方法解析顺序
与原「类内定义覆盖基类」逐字一致，调用点零改动。
"""

from __future__ import annotations

import asyncio
import logging

from ..core import accounts as accounts_mod
from ..core import metrics, webhook_paths
from .inbound_webhook import WppWebhookServer

log = logging.getLogger(__name__)

# 进程级去重：同一账号只提醒一次「未配 webhookSecret」（每 300 秒重注册会刷屏）
_WARNED_NO_WEBHOOK_SECRET: set[str] = set()


class WebhookMixin:

    def _start_vendor_webhooks(self, connectable: list[accounts_mod.Account]) -> None:
        """按 ``autoSetWebhook`` 把三段式 webhook 推给厂商（TS ``index.ts:713-832``）。

        以前这个键只被解析、**没有任何消费者**，所以配了也不会在 vendor 侧注册
        （表现是"本地口开着、vendor 从不推"，两边日志都干净）。现在：
        * 只有本地监听已起、且该账号确实注册了带凭据路径时才推 —— 否则推过去
          等于让厂商往一个 404 打；
        * ``setWebhookRetries`` 次快速重试（指数退避），仍失败则每 5 分钟重试一次，
          直到成功或 ``disconnect()`` 取消任务。
        """
        if self._webhook is None:
            return
        for acct in connectable:
            if not acct.auto_set_webhook:
                metrics.inc_setwebhook_skipped("disabled")
                continue
            if acct.id not in self._webhook_accounts:
                # 本地 server 上没这条路由 ⇒ 推给厂商只会 404（端口不一致 /
                # 无凭据已被 _start_webhook 拒掉，那里已报 error）。
                log.warning("[WPP:%s] autoSetWebhook=true 但本地没有该账号的路由 —— "
                            "跳过厂商注册", acct.id)
                metrics.inc_setwebhook_skipped("no_authcode")
                continue
            if not acct.webhook_public_url:
                log.warning("[WPP:%s] autoSetWebhook=true 但未配 webhookPublicUrl —— "
                            "跳过厂商 webhook 注册（本地监听仍然工作，需手工设 webhook）", acct.id)
                metrics.inc_setwebhook_skipped("no_public_url")
                continue
            if not acct.webhook_path or not (acct.webhook_path_token or acct.webhook_secret):
                log.warning("[WPP:%s] autoSetWebhook=true 但本地路径没有凭据 —— "
                            "不把该路径推给厂商（否则谁都能伪造入站）", acct.id)
                metrics.inc_setwebhook_skipped("no_authcode")
                continue
            if acct.id not in self._clients:
                metrics.inc_setwebhook_skipped("no_authcode")
                continue
            task = asyncio.create_task(
                self._vendor_webhook_loop(acct), name=f"wpp-vendor-webhook-{acct.id}")
            self._webhook_tasks.append(task)

    async def _vendor_webhook_loop(self, acct: accounts_mod.Account) -> None:
        """一个账号的 vendor webhook 注册循环（**每 5 分钟重注册一次**）。

        对齐 TS ``PERIODIC_RETRY_MS = 5 * 60 * 1000``：不仅失败要重试，**成功之后
        也要周期性重注册** —— vendor 侧可能因实例重启/配置漂移把回调丢掉，而我们
        本地监听一切正常、日志干净，表现就是"消息突然不进来了"（09-26 那类静默
        失效的同型）。首轮用 ``setWebhookRetries`` 次快速退避，之后每 300 秒一次。
        """
        client = self._clients.get(acct.id)
        if client is None:
            return
        attempts = max(1, acct.set_webhook_retries)
        first_cycle = True
        while True:
            registered = False
            for attempt in range(attempts):
                try:
                    result = await client.setup_webhook(
                        acct.webhook_public_url, acct.webhook_path,
                        acct.webhook_path_token, secret=acct.webhook_secret,
                    )
                except Exception as exc:  # noqa: BLE001 —— 绝不让注册任务把适配器带崩
                    result = {"set_error": str(exc), "business_error": str(exc)}
                if "set_error" not in result and "business_error" not in result:
                    # ★ 日志脱敏：sync_url 里带 /Webhook/<token>，原样打日志等于把
                    #   "谁都能伪造入站"的凭据写进 journal（TS v1.16.1 已修过同类）。
                    from ..core.redact import redact_webhook_path

                    metrics.inc_setwebhook_ok(periodic=not first_cycle)
                    log.info("[WPP:%s] 厂商 webhook 已注册（sync=%s）",
                             acct.id, redact_webhook_path(str(result.get("sync_url") or ""),
                                                         acct.webhook_path_token))
                    registered = True
                    break
                metrics.inc_setwebhook_fail(periodic=not first_cycle)
                from ..core.redact import redact_webhook_path_any

                safe = {k: redact_webhook_path_any(str(v or ""), (acct.webhook_path_token,))
                        for k, v in result.items()}
                log.warning("[WPP:%s] 厂商 webhook 注册失败（第 %d/%d 次）：set=%s business=%s",
                            acct.id, attempt + 1, attempts,
                            safe.get("set_error"), safe.get("business_error"))
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(30, 2 ** attempt))
            if registered:
                first_cycle = False
                log.debug("[WPP:%s] 厂商 webhook 将在 300 秒后重新注册（周期刷新）", acct.id)
            else:
                log.warning("[WPP:%s] 厂商 webhook 仍未注册，300 秒后重试", acct.id)
            await asyncio.sleep(300)

    def _start_webhook(self, connectable: list[accounts_mod.Account]) -> bool:
        """起共享 webhook server 并注册各账号路由（**只注册带凭据的路径**）。

        2026-10-02 修（C2 / A6 / A5）：

        * **路径用派生值**（``core.webhook_paths``，与推给 vendor 的 URL 同源）：
          配了 ``webhookPathToken`` 就必须出现在被监听的路径里 —— 旧代码注册的是
          配置原文，token 这个字段谁都没消费，于是回调地址就是可猜的那个；
        * **business 路径也注册**（旧代码只注册主路径 ⇒ vendor 推完整消息的那条
          ``/business`` 一直 404，且日志只有一行 404，看不出是配置问题）；
        * **无凭据不注册**：既没有路径 token 又没有 ``webhookSecret`` 的账号，
          这里直接**跳开并报 error** —— 宁可让运维立刻看见配置缺了，也不开一个
          谁都能伪造入站消息的口子（fail-closed 的第一道网，第二道在
          ``WppWebhookServer._authorized``）。
        """
        self._webhook_accounts = set()
        routed = [a for a in connectable if a.webhook_path]
        if not routed:
            return False
        # 多账号共享一个监听端口：host/port 取首个账号的（vendor 侧设计如此），
        # 但**配置不一致必须吵** —— 旧代码静默按第一个账号的端口起服务，
        # 其余账号的回调就被推到了一个没人听的端口上（表现为"只有第一个账号收得到消息"）。
        host = routed[0].webhook_host or "127.0.0.1"
        port = routed[0].webhook_port or 4398
        strays = [a.id for a in routed
                  if (a.webhook_host or "127.0.0.1") != host or (a.webhook_port or 4398) != port]
        if strays:
            log.error("[WPP] webhook 端口不一致：按 %s:%d 起服务，但这些账号配的是别的"
                      "（它们的回调**收不到**）: %s", host, port, ", ".join(strays))
        self._webhook = WppWebhookServer(host=host, port=port,
                                         loop=asyncio.get_running_loop())
        registered = 0
        for acct in routed:
            if (acct.webhook_host or "127.0.0.1") != host or (acct.webhook_port or 4398) != port:
                continue                      # 上面已报 error，这里不注册到别人的端口上
            paths = webhook_paths.derive_webhook_paths(
                acct.webhook_path, acct.webhook_path_token, acct.webhook_business_path)
            if not acct.webhook_path_token and not acct.webhook_secret:
                log.error("[WPP:%s] webhook **未注册**：既没配 webhookPathToken 也没配 "
                          "webhookSecret ⇒ 这个回调口没有任何凭据（谁来 POST 都算微信消息）。"
                          "要启用：账号配置里加 \"webhookPathToken\": \"<随机串>\"，"
                          "并把 vendor 回调 URL 改成 %s（``autoSetWebhook`` 会自动推）。",
                          acct.id, paths.webhook)
                continue
            self._webhook.add_path(
                paths.webhook,
                lambda payload, a=acct: self._on_raw(a, payload),
                secret=acct.webhook_secret, path_token=acct.webhook_path_token,
            )
            # ⚠️ business 回调**不带任何签名头**（2026-10-19 18:31 实抓：body 是
            # wechatpad.message.v2 全量消息，headers 里没有 X-Webhook-*；vendor
            # /Webhook/Business/Set 也没有 secret 字段）⇒ 这里若沿用账号 secret，
            # 每条都 403、业务消息静默丢失（WS 并不兜底所有 msgType，如 payment_notice）。
            # business 的凭据只有路径 token（21 字符 / ~84 bit，vendor 侧同样按 URL 派发）；
            # 主 webhook 仍强制 v2 HMAC。
            self._webhook.add_path(
                paths.business,
                lambda payload, a=acct: self._on_raw(a, payload),
                secret="" if acct.webhook_path_token else acct.webhook_secret,
                path_token=acct.webhook_path_token,
            )
            registered += 1
            self._webhook_accounts.add(acct.id)
            if not acct.webhook_secret and acct.id not in _WARNED_NO_WEBHOOK_SECRET:
                # 进程级一次：_start_webhook 会被重连/重扫反复调用，每次都打就是噪音
                # （实测 35 条/天）。这是**安全提示**而非错误，一次足够。
                _WARNED_NO_WEBHOOK_SECRET.add(acct.id)
                log.warning("[WPP:%s] 未配 webhookSecret —— 仅靠路径 token 鉴权；"
                            "nginx 反代后公网可达，建议补上（支持 vendor v2 HMAC）",
                            acct.id)
        if registered == 0:
            log.error("[WPP] 没有账号能注册 webhook 路由 ⇒ 不起监听（见上面各账号的原因）")
            self._webhook = None
            return False
        return self._webhook.start()
