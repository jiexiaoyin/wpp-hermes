"""厂商 HTTP API 客户端 —— 旧 ``api_client.py``(735 行) 的重写。

## 相对旧实现的三处实质改动

1. **★ 补上 API 审计**。旧 ``api_client.py`` **一次审计都不做** ——
   ``wpp_api_calls`` 的 INSERT 只存在于新数据层，从未被调用过。本实现把审计
   收敛到 ``call()`` **唯一入口**，任何走 ``call`` 的请求都留痕。
2. **★ 消除 9 份重复的响应解析**。旧实现有 9 个 ``download_*`` 方法，每个都
   重复「读 text → json.loads → 在 ``Data.*`` 里用 8 种候选键名捞 base64」，
   且各版本候选键名还不一致（有的漏 ``Voice``、有的漏 ``data.buffer``）。
   现收敛为 ``_extract_b64()`` 一处，**取并集**，不再漏。
3. **★ 审计写库不阻塞事件循环**。旧实现的同步 pymysql 调用直接在 async 里跑
   （已知缺陷）；这里审计走 ``asyncio.to_thread``。

## 必须原样保留的厂商契约

- 鉴权**双保险**：``X-Access-Token`` header **和** ``?authcode=`` query。
- URL 形状 ``{base}/api{endpoint}``（``call`` 会自动补 ``/api``）。
- 成功判定 ``Code == 0`` 且 ``Success != false``；``/Msg/Quote`` 例外（永久 ret=-2）。
- 仅 ``{502,503,504}`` 可重试（**4xx 是永久错误，重试无意义**）。
- 下载类端点**不走** ``_url()`` 的 ``/api`` 拼接，且额外带 ``TokenKey`` header。

## ⚠️ ``aiohttp`` 必须在**函数体内**导入（不许搬到模块级）

``platform/adapter.py`` 在**模块级** ``from ..core.api_client import WppApiError, WppClient``，
而 ``registration._register_platform`` 会导入那个 adapter ⇒ **本模块的加载期依赖就是注册期
依赖**。``plugin_validate`` 的 probe 在 scratch ``HERMES_HOME`` 子进程里跑 ``register()``，
那个解释器未必装了 aiohttp ⇒ 模块级 ``import aiohttp`` 会让 probe 里的 ``register()`` 抛
``ModuleNotFoundError``，而失败方式是"平台/工具静默没注册"，极难倒查。

本模块的 ``import aiohttp`` 分布在 4 个真正发请求的方法里（``_acquire`` / ``_request`` /
``_post_bytes`` / ``_post_json_b64``）；下面 138/142 行的 ``aiohttp.ClientSession`` 只出现在
**注解**里，有 ``from __future__ import annotations`` 兜着，**不求值**。

（形状守卫 ``tools/verify_tree.py`` 的 R2 会 BFS 注册执行链路并把模块级第三方依赖标出来。）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover —— 只给类型检查器/ruff 看，运行期**不** import aiohttp
    # （probe 解释器可能没装 aiohttp，模块级 import 会让 register() 静默失败，见 docstring）
    import aiohttp
from urllib.parse import quote, unquote, urlencode

from . import outbound_guard
from .webhook_paths import derive_webhook_paths

log = logging.getLogger(__name__)

#: 文件下载上限（200MB）—— 防 vendor 返回超大数据把进程打 OOM
MAX_FILE_BYTES = 200 * 1024 * 1024

#: 已知语义特殊的端点：``Code != 0`` **不代表调用失败**，豁免判定
_IGNORE_CODE_ENDPOINTS = frozenset({"/Msg/Quote"})

#: 可重试的瞬时 HTTP 状态码（vendor 网关/上游抖动）。**4xx 不在此列**。
_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})

#: 响应里可能承载 base64 载荷的键名 —— **取并集**，旧实现各方法用的子集不同
_B64_KEYS = ("buffer", "Buffer", "Image", "image", "Voice", "voice",
             "File", "file", "Video", "video", "base64", "Base64")


class WppApiError(Exception):
    """厂商调用失败。``code`` 可能是 HTTP 码（``http_502``）或厂商业务码。"""

    def __init__(self, code: Any, message: str, endpoint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.endpoint = endpoint


def _extract_b64(payload: Any) -> str:
    """从厂商 ``Data`` 载荷里捞出 base64 串。

    厂商各端点返回结构不统一（``Data.Image`` / ``Data.data.buffer`` /
    ``Data.Buffer`` / ``Data.Voice`` / ``Data.File`` …）。这里按**并集**逐层找，
    比旧实现每个方法各写一份更容易维护，也不会漏键。
    """
    if not isinstance(payload, dict):
        return ""
    # ① 嵌套一层 data.buffer（GetContractProfile 系与二进制系的共同形状）
    for nested_key in ("data", "Data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in _B64_KEYS:
                val = nested.get(key)
                if isinstance(val, str) and val:
                    return val
    # ② 平铺
    for key in _B64_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _decode_b64(text: str) -> bytes | None:
    """base64 → bytes（坏数据返 None，不抛 —— 单个媒体坏掉不该中断整批）。"""
    if not text:
        return None
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError) as exc:
        log.warning("[WPP] base64 解码失败: %s", exc)
        return None


def _bind_endpoint(endpoint: str, body: dict, method: str) -> tuple[str, dict]:
    """把路径占位符与 GET query 收进 URL，返回 ``(endpoint, 剩余 body)``。

    路径占位符只认 ``{name}`` 形式（XiaoWei 会话端点）。名单里的键是**请求参数**，
    不是请求体字段 —— 取走后不能再出现在 body 里。
    """
    body = dict(body)
    if "{" in endpoint:
        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            value = body.pop(key, "")
            return quote("" if value is None else str(value), safe="")

        endpoint = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, endpoint)

    if method == "GET" and body:
        pairs: list[tuple[str, Any]] = []
        for key, value in body.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                pairs.extend((key, item) for item in value)
            else:
                pairs.append((key, value))
        query = urlencode(pairs)
        if query:
            endpoint = f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"
        body = {}
    return endpoint, body


def _content_disposition_name(header: str) -> str:
    """从 ``Content-Disposition`` 取文件名（RFC 5987 优先）。"""
    if not header:
        return ""
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", header, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"?([^";]+)"?', header, re.IGNORECASE)
    return unquote(match.group(1).strip()) if match else ""


def derive_business_url(public_url: str, webhook_path: str, token: str) -> tuple[str, str]:
    """推导 vendor business webhook 的 ``(syncMessageUrl, logoutUrl)``。

    路径部分**一律**走 :func:`core.webhook_paths.derive_webhook_paths`（唯一来源）——
    这里只负责拼对外可达的 host。同源是刻意的：这条 URL 会被推给 vendor，
    而"我们监听哪条路径"在适配器那一侧派生，两处各写一遍必然漂，
    漂了的表现是**入站静默全丢**（404）而两边日志都干净。

    ⚠️ 2026-08-31 踩过：Hermes 迁移时**漏了 business 段**，只设 ``/Webhook/Set``
    会让 vendor 只推空 ``Data`` 的 ``sync_message``（完整消息要 business 才推）。
    """
    public = (public_url or "").split("/wechatpadpro/")[0].rstrip("/")
    business = derive_webhook_paths(webhook_path, token).business
    if business and not business.endswith("/business"):
        business = f"{business}/business"
    sync_url = f"{public}{business}"
    return sync_url, f"{sync_url}/logout"


class WppClient:
    """单个账号的厂商 API 客户端。

    ``account_id`` 只用于审计落库，不参与鉴权（鉴权只有 ``authcode``）。
    """

    def __init__(
        self,
        base_url: str,
        authcode: str,
        *,
        account_id: str = "",
        timeout: int = 30,
        audit: bool = True,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.authcode = authcode or ""
        self.account_id = account_id
        self.timeout = timeout
        self.audit = audit
        #: ``loop -> ClientSession``。会话是 **loop-bound** 的（aiohttp 的硬约束）：
        #: 同一个 WppClient 会被 adapter 的 loop 与工具调用的 loop 同时用，共用一个
        #: 会话必炸（"Timeout context manager should be used inside a task" /
        #: "got Future attached to a different loop"），而**关掉**另一个活 loop 的
        #: 会话又会掐断它上面在途的请求。所以按 loop 各持一个，互不干扰。
        self._sessions: dict[Any, aiohttp.ClientSession] = {}
        #: 最近一次用到的会话/loop（诊断与兼容用，不参与判定）。
        self._session: aiohttp.ClientSession | None = None
        self._session_loop: Any = None

    # ------------------------------------------------------------------ 会话

    async def _acquire(self) -> aiohttp.ClientSession:
        import aiohttp  # 延迟导入，见模块 docstring

        # ⚠️ 审阅曾建议这里加 ``asyncio.Lock`` 防"并发首调各建一个会话" —— 复核后
        #   **不加**：本函数从判空到赋值之间没有 await，单事件循环里协程不会在这里
        #   交错，那个竞态不存在；而 asyncio.Lock 绑定首次使用的 loop，跨线程/多 loop
        #   复用同一个 client 时会抛 "bound to a different event loop"，把一个原本
        #   能跑（只是泄漏）的形态变成硬失败。会话本身已经绑定 loop，多 loop 复用
        #   在 aiohttp 层就不被支持。
        #
        # ★ 2026-10-18：既然"多 loop 复用不被支持"，就必须**按 loop 隔离**。
        #   Hermes 的 TUI 里工具调用跑在与平台 adapter 不同的 loop 上，复用旧会话会让
        #   **所有**厂商工具报 "Timeout context manager should be used inside a task"。
        #   反过来也有一条红线：**绝不能去关别的活 loop 上的会话** —— 那个 loop 上可能
        #   正有媒体下载/发送在途，关连接池等于把它掐断（实测 Connector is closed）。
        #   所以这里是 ``loop -> session`` 的表：只取/建当前 loop 的那一个，只清"已经
        #   死掉的 loop"的条目，从不触碰活着的另一个 loop。
        loop = asyncio.get_running_loop()
        session = self._sessions.get(loop)
        if session is not None and not session.closed:
            return session
        self._forget_dead_loops(keep=loop)
        session = aiohttp.ClientSession()
        self._sessions[loop] = session
        self._session, self._session_loop = session, loop
        return session

    def _forget_dead_loops(self, keep: Any = None) -> None:
        """丢掉**已结束的 loop** 上的会话条目。活着的 loop（含另一个线程里的
        adapter loop）一律不动 —— 那是别人的连接池，关它就是掐断在途请求。

        loop 已经关了就没法 ``await session.close()``，退而用同步的
        ``connector.close()`` 收掉 fd（否则等到 GC 才收，且 aiohttp 会刷
        "Unclosed connector"）。
        """
        for lp in [l for l in self._sessions if l is not keep and l.is_closed()]:
            session = self._sessions.pop(lp, None)
            if session is None or session.closed:
                continue
            connector = getattr(session, "_connector", None)
            try:
                if connector is not None:
                    # ``close()`` 只会返回一个待 await 的对象（loop 已死就再也等不到了）；
                    # 内部的 ``_close()`` 是同步的：它会清掉连接表、放掉 protocol 引用，
                    # fd 随后由 GC 收掉。死 loop 上能做到的只有这些。
                    connector._close()
            except Exception:
                log.debug("[WPP] 死 loop 的会话清理失败（已丢弃）", exc_info=True)
            if self._session is session:
                self._session, self._session_loop = None, None

    async def close(self) -> None:
        """关连接池。**必须被调用** —— 旧 ``_standalone_send`` 每次新建 client
        却从不 close，每次 cron 投递泄漏一个 aiohttp session（已知缺陷）。

        同 loop 或**已死** loop 的会话直接 ``await close()``（关闭不再依赖事件循环
        调度，实测会正常置 ``session.closed``）；另一个**活** loop 的会话把 close
        投递过去（不 await，避免跨 loop 死等）。
        """
        running = asyncio.get_running_loop()
        sessions, self._sessions = self._sessions, {}
        self._session, self._session_loop = None, None
        for lp, session in sessions.items():
            if session.closed:
                continue
            if lp is running or lp.is_closed():
                try:
                    await session.close()
                except Exception:
                    log.debug("[WPP] 关闭会话失败（忽略）", exc_info=True)
                continue
            try:
                asyncio.run_coroutine_threadsafe(session.close(), lp)
            except Exception:
                log.debug("[WPP] 跨 loop 关闭会话失败（已丢弃）", exc_info=True)

    async def __aenter__(self) -> WppClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -------------------------------------------------------------- URL/头

    def _raw_url(self, endpoint: str, body: Any = None) -> str:
        """``{base}/api{endpoint}?authcode=<authcode>`` —— 鉴权的 query 那一半。

        所有请求（业务 + 下载）都走这里，**没有例外** —— 旧实现有 9 个下载方法
        自行拼 URL，拼法与 ``_url()`` 不一致，是漏带 authcode 的隐患来源。

        ★ **出站闸的唯一收口**（C1/C5，2026-10-02）：判据在这里调一次，于是"某个工具
        忘了过闸"在结构上不可能发生 —— 299 个工具（含通用端点表）、适配器、standalone
        全都经过本方法。判据本身见 :mod:`plugin.core.outbound_guard`（未设白名单时是
        no-op，故生产行为不变）。
        """
        outbound_guard.guard(endpoint, body, self.account_id)
        ep = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url}/api{ep}"
        if self.authcode:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}authcode={self.authcode}"
        return url

    def _headers(self, *, token_key: bool = False) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.authcode:
            # 双保险之一：header。之二在 query（见 _raw_url）
            h["X-Access-Token"] = self.authcode
        if token_key:
            # 下载类端点额外要求 TokenKey header（老 schema 遗留，仍被 vendor 认）
            h["TokenKey"] = self.authcode
        return h

    # ------------------------------------------------------------------ 审计

    async def _audit(
        self, endpoint: str, method: str, *,
        status_code: int | None = None, vendor_code: int | None = None,
        latency_ms: int | None = None,
        request_body: Any = None, response_body: Any = None,
    ) -> None:
        """留痕到 ``wpp_api_calls``。**任何异常都必须咽掉** —— 审计不能反过来搞挂主流程。

        同步 pymysql 走 ``to_thread``，不阻塞事件循环（旧实现是在 async 里直连）。
        """
        if not self.audit or not self.account_id:
            return
        # ★ 凭据**一律**打码，与端点无关：``/Webhook/Set``、``/Webhook/Test`` 的请求体里
        #   有明文 ``authcode``（set_webhook 与工具层都会 setdefault 一份），而审计表
        #   ``wpp_api_calls`` 是长期留存 + 会进备份 ⇒ 原样落盘等于把账号唯一凭据复制进
        #   DB（拿到即可接管这个微信账号）。按键名打码，覆盖所有端点。
        from .redact import redact_deep, redact_secret_keys

        request_body = redact_secret_keys(request_body)
        response_body = redact_secret_keys(response_body)
        # ★ webhook 注册三端点的请求/响应体里还带 ``/wechatpadpro/<token>/...``：
        #   结构式打码只对这些端点有意义（全局跑会误伤普通业务字符串）。
        if any(mark in endpoint for mark in ("/Webhook/", "/Msg/StartAutoSync")):
            request_body = redact_deep(request_body)
            response_body = redact_deep(response_body)
        try:
            from ..data import repo_messages  # 延迟导入：注册期不拉起 DB 依赖

            await asyncio.to_thread(
                repo_messages.record_api_call,
                self.account_id, endpoint,
                method=method, status_code=status_code, vendor_code=vendor_code,
                latency_ms=latency_ms, request_body=request_body,
                response_body=response_body,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("[WPP] API 审计写入失败 %s: %s", endpoint, exc)

    # ------------------------------------------------------------- 核心调用

    async def call(
        self, endpoint: str, body: dict | None = None,
        method: str = "POST", max_retries: int = 2, *, audit: bool = True,
    ) -> dict:
        """通用厂商调用 —— **所有业务端点的唯一入口**。

        ``endpoint`` 形如 ``/Group/GetChatroomInfo``（不带 ``/api``，这里补）。

        两条路径参数约定（2026-10-18 补，对齐 TS 里 GET + 路径参数的端点）：

        * ``/XiaoWei/Chat/Sessions/{session_id}/Messages`` —— 花括号占位符从
          ``body`` 里取同名键（取完即从请求体删除），URL 编码后替换；
        * ``method="GET"`` 时剩余 ``body`` 全部转成 query string —— 旧实现
          ``_request`` 对 GET 直接丢掉 body，导致带参 GET 端点永远只查默认值。
        """
        method = method.upper()
        endpoint, body = _bind_endpoint(endpoint, body or {}, method)
        # ★ 写端点**一律不重试**（2026-10-18 审阅修）。超时/连接断时无法区分
        #   "请求没到"和"请求到了、响应丢了"；重试后者的代价是第二条真实微信消息
        #   （不可撤回）。分类复用出站闸的写词表 —— 通用端点表里的 216 个端点
        #   也因此自动覆盖，不靠每个调用点记得传 ``max_retries=0``。
        #   读端点保持原有重试（幂等，抖动能自愈）。
        if not outbound_guard.is_read_only(endpoint):
            max_retries = 0
        return await self._request(
            endpoint, body,
            method=method, max_retries=max_retries, audit=audit,
        )

    async def _request(
        self, endpoint: str, body: dict, *,
        method: str, max_retries: int, audit: bool = True,
    ) -> dict:
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        url = self._raw_url(endpoint, body)
        headers = self._headers()
        is_get = method == "GET"
        # GET 退避比 POST 更保守（0.3*attempt vs 0.5*2^n）
        backoff = (lambda a: 0.3 * a) if is_get else (lambda a: 0.5 * (2 ** (a - 1)))

        last_err: Exception | None = None
        started = time.monotonic()
        for attempt in range(1, max_retries + 2):
            try:
                async with session.request(
                    method, url, json=None if is_get else body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    text = await resp.text()
                    status = resp.status
                    if status != 200:
                        err = WppApiError(f"http_{status}",
                                          f"{endpoint} -> HTTP {status}: {text[:200]}", endpoint)
                        if status in _RETRYABLE_HTTP_STATUS and attempt <= max_retries:
                            last_err = err
                            await asyncio.sleep(backoff(attempt))
                            continue
                        raise err

                    if is_get:
                        # 旧 _get 不解析 Code，直接返回（保持行为不变）
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise WppApiError("bad_json",
                                              f"{endpoint} -> 非 JSON: {text[:200]}", endpoint) from exc
                        if audit:
                            await self._audit(endpoint, method, status_code=status,
                                              latency_ms=int((time.monotonic() - started) * 1000),
                                              request_body=None, response_body=data)
                        return data

                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise WppApiError("bad_json",
                                          f"{endpoint} -> 非 JSON: {text[:200]}", endpoint) from exc

                    code = data.get("Code")
                    failed = data.get("Success") is False or (
                        code is not None and code != 0 and endpoint not in _IGNORE_CODE_ENDPOINTS
                    )
                    if audit:
                        await self._audit(endpoint, method, status_code=status,
                                          vendor_code=code if isinstance(code, int) else None,
                                          latency_ms=int((time.monotonic() - started) * 1000),
                                          request_body=body, response_body=data)
                    if failed:
                        raise WppApiError(code if code is not None else data.get("CodeValue", "?"),
                                          f"{endpoint} -> {text[:300]}", endpoint)
                    return data
            except aiohttp.ClientError as exc:
                last_err = exc
                if attempt <= max_retries:
                    await asyncio.sleep(backoff(attempt))
                    continue
            except TimeoutError:
                # ★ 总超时抛的是 ``asyncio.TimeoutError``（3.11+ 即内置 TimeoutError），
                #   它**不是** ``aiohttp.ClientError`` 子类 —— 旧实现让它直接穿透：
                #   读端点一次都不重试、也不包成 WppApiError，下游 ``except WppApiError``
                #   收不到，落到 ``except Exception`` 变成 error_kind="unknown"，
                #   工具层只看到 ``调用失败: ``（消息为空）。这里按"网络类"统一处理。
                last_err = WppApiError("timeout", f"{endpoint} -> 超时 {self.timeout}s", endpoint)
                if attempt <= max_retries:
                    await asyncio.sleep(backoff(attempt))
                    continue
        if isinstance(last_err, WppApiError):
            raise last_err  # 已经是分类好的（如 timeout），别套第二层 "network"
        raise WppApiError("network", f"{endpoint} -> {last_err}", endpoint) from last_err

    # ------------------------------------------------------- 原始字节下载

    async def _post_bytes(
        self, endpoint: str, body: dict, *, timeout: int = 60,
        cap: int = MAX_FILE_BYTES, accept_json_b64: bool = True,
    ) -> bytes | None:
        """下载类端点的统一实现。

        vendor 这些端点**有时返原始字节、有时返 JSON 裹 base64**（取决于
        Content-Type），所以两条路都要走。所有失败都返回 ``None`` 并 warning ——
        单个媒体取不到不该让调用方崩（旧实现同此语义）。
        """
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        try:
            async with session.post(
                self._raw_url(endpoint, body), json=body,
                headers=self._headers(token_key=True),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)
                    return None
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        if int(cl) > cap:
                            log.warning("[WPP] %s Content-Length=%s 超上限 %s，拒绝", endpoint, cl, cap)
                            return None
                    except ValueError:
                        pass
                content_type = (resp.headers.get("Content-Type") or "").lower()
                raw = await resp.read()
                if not raw:
                    return None
                if accept_json_b64 and "json" in content_type:
                    try:
                        data = json.loads(raw.decode("utf-8", errors="ignore"))
                    except json.JSONDecodeError:
                        return raw if len(raw) > 10 else None
                    got = _decode_b64(_extract_b64(data.get("Data") or data))
                    return got
                return raw if len(raw) > 10 else None
        except Exception as exc:  # noqa: BLE001 —— 下载失败不该上抛
            log.warning("[WPP] %s 失败: %s", endpoint, exc)
            return None

    async def _post_json_b64(self, endpoint: str, body: dict, *, timeout: int = 60) -> bytes | None:
        """一定返 JSON 的下载端点（从 ``Data.*`` 里捞 base64）。"""
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        try:
            async with session.post(
                self._raw_url(endpoint, body), json=body,
                headers=self._headers(token_key=True),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)
                    return None
                try:
                    data = json.loads(await resp.text())
                except json.JSONDecodeError:
                    return None
                return _decode_b64(_extract_b64(data.get("Data") or data))
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] %s 失败: %s", endpoint, exc)
            return None

    async def get_bytes(
        self, endpoint: str, *, timeout: int = 60, cap: int = MAX_FILE_BYTES,
    ) -> dict | None:
        """GET 原始文件流（``/FriendCircle/BatchDownloadFile`` 这类端点）。

        与 ``_post_bytes`` 的分工：那条走 POST、这条走 GET。两者都**不走**
        ``_request`` —— JSON 解析会把二进制毁掉。

        返回 ``{content, content_type, file_name, size_bytes}``；失败 ``None``。
        """
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        try:
            async with session.get(
                self._raw_url(endpoint), headers=self._headers(token_key=True),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)
                    return None
                length = resp.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > cap:
                            log.warning("[WPP] %s Content-Length=%s 超上限 %s，拒绝",
                                        endpoint, length, cap)
                            return None
                    except ValueError:
                        pass
                raw = await resp.read()
                if not raw or len(raw) > cap:
                    return None
                return {
                    "content": raw,
                    "content_type": resp.headers.get("Content-Type") or "",
                    "file_name": _content_disposition_name(
                        resp.headers.get("Content-Disposition") or ""),
                    "size_bytes": len(raw),
                }
        except Exception as exc:  # noqa: BLE001 —— 下载失败不该上抛
            log.warning("[WPP] %s 失败: %s", endpoint, exc)
            return None

    async def read_sse(
        self, endpoint: str, *, timeout: int = 10, max_events: int = 50,
    ) -> dict:
        """读取 SSE 事件流的前若干条（``/XiaoWei/Chat/Sessions/{id}/Events``）。

        ⚠️ 这是**长连接**：调用方（AI 工具）等不到 EOF，所以这里强制一个总超时，
        到点把已收到的事件返回。TS 的同名工具直接用普通 JSON 客户端，实际上会挂住；
        这里改成"最多等 ``timeout`` 秒"的可返回实现（工具面保持一致，行为更安全）。
        """
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        events: list[str] = []
        truncated = False
        try:
            async with session.get(
                self._raw_url(endpoint), headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return {"events": [], "error": f"HTTP {resp.status}"}
                async for raw in resp.content:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        events.append(line)
                    if len(events) >= max_events:
                        truncated = True
                        break
        except TimeoutError:
            truncated = True
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] SSE %s 失败: %s", endpoint, exc)
            return {"events": events, "error": str(exc)[:200]}
        return {"events": events, "truncated": truncated}

    # ============================================================== 状态查询

    async def get_online_info(self) -> dict:
        return await self.call("/User/GetOnlineInfo", {}, method="GET", max_retries=1)

    async def get_long_link_status(self) -> dict:
        return await self.call("/Login/LongLinkStatus", {}, method="GET", max_retries=1)

    async def get_contract_profile(self) -> dict:
        return await self.call("/User/GetContractProfile")

    async def heartbeat(self) -> dict:
        return await self.call("/Login/HeartBeat")

    async def heartbeat_long(self) -> dict:
        """``/Login/HeartBeatLong`` —— WS 保活的 HTTP 双保险（W5，TS ``ws-client.ts:273-278``）。

        主力是 WS 层的**原生 ping**（``inbound_ws`` 的 ``ping_interval=20``：TS v1.9.3-fix
        的根因结论是"vendor 的 idle timeout 挂在 /ws/sync 长连接本身，HTTP 调用不产生
        ws 流量"）；这一路只是双保险，失败由调用方降级为一行 warn。
        """
        return await self.call("/Login/HeartBeatLong")

    async def auto_heartbeat(self) -> dict:
        """``/Login/AutoHeartBeat`` —— 让**厂商**自己维持长连接（W5，TS ``:138-142``）。

        ⚠️ **每次重连都要重开一遍**：新 ws 会话需要重新告知厂商，只开一次会在
        第一次断连之后失效（那种失效形态是"跑一阵就掉线"，看不出跟这里有关系）。
        """
        return await self.call("/Login/AutoHeartBeat")

    async def get_contract_detail(self, user_name: str) -> dict:
        """查联系人详情（引用回复里取被回复人昵称用）。"""
        return await self.call("/Friend/GetContractDetail", {"userName": user_name}, max_retries=0)

    # ============================================================== 发送

    async def send_text(self, to_wxid: str, content: str, ats: list[str] | None = None) -> dict:
        """``/Msg/SendTxt`` —— ``At`` 是逗号串、``Type`` 固定 1（vendor swagger 定义）。"""
        body: dict[str, Any] = {"ToWxid": to_wxid, "Content": content, "Type": 1}
        if ats:
            body["At"] = ",".join(str(a) for a in ats)
        # ★ 非幂等：发送类端点一律 ``max_retries=0``。超时/连接断这类失败**无法区分**
        #   "没发出去"和"发出去了但响应丢了"；重试的代价是给真实联系人发第二条
        #   （微信不可撤回）。宁可把错误抛给调用方，也不重复投递。
        return await self.call("/Msg/SendTxt", body, max_retries=0)

    async def revoke_msg(self, msg_id: str, new_msg_id: str = "", create_time: int = 0,
                         to_wxid: str = "") -> dict:
        """``/Msg/Revoke`` —— swagger 字段是 ``ClientMsgId/NewMsgId/CreateTime/ToUserName``。

        ⚠️ 三处都必须对齐 TS（``send/msg.ts:601-614``）：
        * 键名是 ``ClientMsgId``（不是 ``MsgId``）、``ToUserName``（不是 ``ToWxid``）；
        * 值是 **int64**（传字符串 Go 反序列化 ``Code=-8``）；
        * ``CreateTime`` 必须是**发送时返回的那个** server time，用 ``now()`` 会
          ``ret=0`` 但不真撤。拿不到原值才退回 now。
        """
        def _int(v: Any) -> int:
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        body: dict[str, Any] = {
            "ClientMsgId": _int(msg_id),
            "NewMsgId": _int(new_msg_id or msg_id),
            "CreateTime": _int(create_time) or int(time.time()),
        }
        if to_wxid:
            body["ToUserName"] = to_wxid
        return await self.call("/Msg/Revoke", body, max_retries=0)

    async def send_image(self, to_wxid: str, image_base64: str) -> dict:
        """``/Msg/UploadImg`` —— 上传即发送（``ret=0`` 为成功）。"""
        return await self.call("/Msg/UploadImg",
                               {"ToWxid": to_wxid, "Base64": image_base64}, max_retries=0)

    async def send_cdn_image(self, to_wxid: str, img_url: str) -> dict:
        return await self.call("/Msg/SendCDNImg",
                               {"ToWxid": to_wxid, "Content": img_url}, max_retries=0)

    async def send_cdn_video(self, to_wxid: str, video_url: str) -> dict:
        return await self.call("/Msg/SendCDNVideo",
                               {"ToWxid": to_wxid, "Content": video_url}, max_retries=0)

    async def share_location(self, to_wxid: str, latitude: float, longitude: float,
                            label: str = "", poi_name: str = "") -> dict:
        """``/Msg/ShareLocation``（Type 48 定位卡片）。

        ⚠️ ``X=纬度``、``Y=经度`` —— 反了定位缩略图会错位。
        """
        return await self.call("/Msg/ShareLocation", {
            "ToWxid": to_wxid, "X": float(latitude), "Y": float(longitude),
            "Label": label or "", "Poiname": poi_name or "", "Scale": 16, "Infourl": "",
        }, max_retries=0)

    async def send_emoji(self, to_wxid: str, emoji_md5: str, emoji_size: int = 0) -> dict:
        """``/Msg/SendEmoji`` —— swagger ``{ToWxid, Md5, TotalLen}``（**不是**驼峰长名）。"""
        return await self.call("/Msg/SendEmoji",
                               {"ToWxid": to_wxid, "Md5": emoji_md5,
                                "TotalLen": int(emoji_size or 0)}, max_retries=0)

    async def share_card(self, to_wxid: str, card_wxid: str, card_nickname: str,
                        card_alias: str = "") -> dict:
        return await self.call("/Msg/ShareCard", {
            "ToWxid": to_wxid, "CardWxId": card_wxid,
            "CardNickName": card_nickname, "CardAlias": card_alias or "",
        }, max_retries=0)

    async def send_xcx(self, to_wxid: str, xcx_title: str, xcx_desc: str, xcx_url: str,
                      xcx_app_id: str, thumb_url: str = "",
                      page_path: str = "", username: str = "") -> dict:
        """``/Msg/SendXCX`` —— ``{ToWxid, Content}``，Content 是 appmsg XML。

        对齐 TS ``send/msg.ts:733-755`` 的两种形状：
        * 传 ``page_path`` ⇒ 现代 ``type=33`` 卡片（可落到小程序内部页面）；
        * 不传 ⇒ legacy ``type=2001`` + ``<mmapp>``（老行为）。
        旧实现发的是 ``xcxTitle/xcxUrl/...`` 等**厂商根本不认的键**，Content 为空。
        """
        def esc(s: Any) -> str:
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))

        if page_path:
            content = (
                "<appmsg>"
                f"<title>{esc(xcx_title)}</title>"
                "<weappinfo>"
                f"<pagepath>{esc(page_path)}</pagepath>"
                + (f"<weappiconurl>{esc(thumb_url)}</weappiconurl>" if thumb_url else "")
                + f"<appid>{esc(xcx_app_id)}</appid><type>2</type>"
                + (f"<username>{esc(username)}</username>" if username else "")
                + "</weappinfo>"
                f"<sourceusername>{esc(xcx_app_id)}</sourceusername>"
                "<type>33</type>"
                f"<sourcedisplayname>{esc(xcx_desc or xcx_title)}</sourcedisplayname>"
                f"<des>{esc(xcx_desc or xcx_title)}</des>"
                "</appmsg>"
            )
        else:
            mm = (f"<appid>{esc(xcx_app_id)}</appid>"
                  f"<sourcedisplayname>{esc(xcx_title)}</sourcedisplayname>"
                  f"<url>{esc(xcx_url)}</url>"
                  + (f"<weappiconurl>{esc(thumb_url)}</weappiconurl>" if thumb_url else ""))
            content = (
                '<appmsg appid="" sdkver="0">'
                f"<title>{esc(xcx_title)}</title><des>{esc(xcx_desc)}</des>"
                "<action>webview</action><type>2001</type><showtype>0</showtype>"
                "<content/><url/><lowurl/><dataurl/><lowdataurl/><appattach/>"
                f"<mmapp>{mm}</mmapp><fromusername>{esc(to_wxid)}</fromusername></appmsg>"
            )
        return await self.call("/Msg/SendXCX",
                               {"ToWxid": to_wxid, "Content": content}, max_retries=0)

    async def share_link(self, to_wxid: str, title: str, desc: str, link_url: str,
                        thumb_url: str = "") -> dict:
        """``/Msg/ShareLink`` —— 自建 ``Type=5`` 的 appmsg XML。"""
        return await self.call("/Msg/ShareLink", {
            "ToWxid": to_wxid, "Type": 5,
            "Xml": build_appmsg_xml(title, desc, link_url, thumb_url),
        }, max_retries=0)

    async def send_group_mass_text(self, to_ids: list[str], content: str) -> dict:
        return await self.call("/Msg/SendGroupMassMsgText",
                               {"ToIds": to_ids, "Content": content}, max_retries=0)

    async def send_file_v2(self, to_wxid: str, file_name: str, file_base64: str) -> dict:
        """``/Msg/SendFile`` —— 自动上传 + 发送。"""
        return await self.call("/Msg/SendFile",
                               {"ToWxid": to_wxid, "FileName": file_name, "Base64": file_base64},
                               max_retries=0)

    async def send_file_via_app(self, to_wxid: str, file_name: str,
                                file_base64: str, file_size: int = 0) -> dict:
        """发文件：``/Tools/UploadFile`` → ``/Msg/ShareLink`` ``Type=6``。

        对齐 TS ``api-client.ts:156-186``（``sendFileViaApp``）。旧的
        ``/Msg/UploadFile`` 端点**不存在**（swagger 只有 ``/Tools/UploadFile``），
        调用必然 404/``Code=-1``。
        """
        import base64 as _b64

        up = await self.call("/Tools/UploadFile", {"base64": file_base64}, audit=False)
        media_id = str((up.get("Data") or {}).get("mediaId") or "")
        if not media_id:
            return {"Success": False, "Code": -2,
                    "CodeValue": "UPLOAD_NO_MEDIA_ID", "Data": up.get("Data")}

        def esc(s: Any) -> str:
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))

        size = int(file_size or 0)
        if size <= 0:
            try:
                size = len(_b64.b64decode(file_base64, validate=False))
            except Exception:  # noqa: BLE001 —— 尺寸只是展示字段，算不出就给 0
                size = 0
        ext = (file_name.rsplit(".", 1)[-1] if "." in file_name else "dat").lower()
        xml = (
            '<appmsg appid="wxfile" sdkver="0">'
            f"<title>{esc(file_name)}</title><des></des><action>view</action><type>6</type>"
            f"<content>dataType=1|filename={esc(file_name)}|fileext={ext}"
            f"|totallen={size}|attachid={esc(media_id)}|</content>"
            f"<appattach><totallen>{size}</totallen><attachid>{esc(media_id)}</attachid>"
            f"<fileext>{ext}</fileext></appattach></appmsg>"
        )
        return await self.call("/Msg/ShareLink",
                               {"ToWxid": to_wxid, "Type": 6, "Xml": xml}, max_retries=0)

    async def send_voice(self, to_wxid: str, voice_base64: str,
                         voice_type: int = 4, voice_time: int = 0) -> dict:
        """``/Msg/SendVoice`` —— ``{ToWxid, Base64, Type, VoiceTime}``。

        ⚠️ 厂商**只收 SILK**（Type=4）；mp3 直传会失败/无声。输入是 mp3 时调用方
        必须转码，转不了就降级成文件（TS v1.3.53 VOICE-DEGRADE 同口径）。
        """
        return await self.call("/Msg/SendVoice", {
            "ToWxid": to_wxid, "Base64": voice_base64,
            "Type": int(voice_type or 4), "VoiceTime": int(voice_time or 0),
        }, max_retries=0)

    async def send_video(self, to_wxid: str, video_base64: str,
                        thumb_base64: str = "", play_length: int = 10) -> dict:
        """``/Msg/SendVideo`` —— ``ImageBase64`` 缩略图**必填**，否则 vendor 拒收。"""
        return await self.call("/Msg/SendVideo", {
            "ToWxid": to_wxid, "Base64": video_base64,
            "ImageBase64": thumb_base64, "PlayLength": play_length,
        }, max_retries=0)

    async def send_app_message(self, items: list[dict]) -> dict:
        """``/Msg/SendAppMessage`` —— 结构化应用卡片，单次最多 20 项。"""
        return await self.call("/Msg/SendAppMessage", {"items": items}, max_retries=0)

    # ============================================================== 朋友圈

    async def publish_circle_text(self, title: str) -> dict:
        return await self.call("/FriendCircle/Messages",
                               {"title": title, "private": 0}, max_retries=0)

    async def publish_circle_images(self, title: str, image_base64_list: list[str]) -> dict:
        """图片朋友圈（1–9 张）：逐张 ``UploadImage`` 拿 ``publishItem`` 再发布。"""
        if not image_base64_list or len(image_base64_list) > 9:
            return {"Code": -1, "Success": False,
                    "CodeValue": f"图片数量必须 1-9 张（实际 {len(image_base64_list)}）"}
        items = []
        for b64 in image_base64_list:
            up = await self.call("/FriendCircle/UploadImage", {"imageData": b64})
            item = (up.get("Data") or {}).get("publishItem")
            if up.get("Code") != 0 or not item:
                return {"Code": up.get("Code", -1), "Success": False,
                        "CodeValue": f"UploadImage 失败 {up.get('CodeValue', '')}"}
            items.append(item)
        return await self.call("/FriendCircle/Messages",
                               {"title": title, "private": 0, "images": items}, max_retries=0)

    async def publish_circle_video(self, title: str, video_base64: str, thumb_base64: str) -> dict:
        up = await self.call("/FriendCircle/UploadVideo",
                             {"videoData": video_base64, "thumbData": thumb_base64})
        item = (up.get("Data") or {}).get("publishItem")
        if up.get("Code") != 0 or not item:
            return {"Code": up.get("Code", -1), "Success": False,
                    "CodeValue": f"UploadVideo 失败 {up.get('CodeValue', '')}"}
        return await self.call("/FriendCircle/Messages",
                               {"title": title, "private": 0, "video": item}, max_retries=0)

    # ============================================================== 消息同步

    async def sync_message(self, prev_synckey: str = "") -> dict:
        """``/Msg/Sync`` —— 拉增量消息（服务端用 authcode 定位账号 + synckey 游标）。

        ``{"Scene": 0, "Synckey": ...}`` 是 vendor 的 ``Msg.SyncParamDoc`` 形状
        （TS ``send/msg.ts:814`` / ``ws-client.ts:223``）。⚠️ **空串 = 首次全量** ——
        vendor 会把上千条历史一次性倒回来（TS 注释里 19:56 那次的 1458 条重放风暴就是
        这么来的），所以只有"库里真没游标"那一次才允许空串（见 ``core.sync_fallback``）。
        """
        return await self.call("/Msg/Sync", {"Scene": 0, "Synckey": prev_synckey or ""})

    # ============================================================== webhook

    async def set_webhook(self, url: str) -> dict:
        """``/Webhook/Set`` —— 只设这一条**不够**（vendor 只推空 Data 的 sync_message）。

        ⚠️ 字段必须与 TS ``webhook.ts::set`` 一致：漏 ``enabled:true`` 时 Go bool
        零值 = false，webhook **设了等于没设**（v1.1.17 P0-E 事故）。
        """
        return await self.call("/Webhook/Set", {
            "url": url,
            "authcode": self.authcode or "",
            "enabled": True,
            "retryCount": 3,
            "timeout": 10,
            "messageTypes": ["all"],
        })

    async def set_business_webhook(self, sync_url: str, logout_url: str) -> dict:
        return await self.call("/Webhook/Business/Set",
                               {"syncMessageUrl": sync_url, "logoutUrl": logout_url})

    async def start_auto_sync(self, target_url: str) -> dict:
        """``/Msg/StartAutoSync`` —— 启动完整消息推送到 business webhook。"""
        return await self.call("/Msg/StartAutoSync", {"TargetURL": target_url})

    async def setup_webhook(self, public_url: str, webhook_path: str, token: str) -> dict:
        """三段式 webhook 设置（**必须三段都做**，见 ``derive_business_url`` 注释）。

        ⚠️ 2026-10-02（C2）：``/Webhook/Set`` 这里过去推的是**不带 token** 的
        ``webhook_path``，而 business 那条走的是带 token 的路径 —— 同一个账号两条
        回调地址形状不一致，且前者正是"可猜的那个"。现在两条都走
        :func:`core.webhook_paths.derive_webhook_paths`（适配器监听侧同源）。
        """
        paths = derive_webhook_paths(webhook_path, token)
        sync_url, logout_url = derive_business_url(public_url, webhook_path, token)
        out = {"sync_url": sync_url, "logout_url": logout_url, "webhook_path": paths.webhook}
        try:
            out["set"] = await self.set_webhook(f"{public_url.rstrip('/')}{paths.webhook}")
        except Exception as exc:  # noqa: BLE001
            from .redact import redact_webhook_path_any

            safe = redact_webhook_path_any(str(exc), (token,))
            log.warning("[WPP] vendor /Webhook/Set 失败: %s", safe)
            out["set_error"] = safe
        try:
            out["business"] = await self.set_business_webhook(sync_url, logout_url)
            out["auto_sync"] = await self.start_auto_sync(sync_url)
        except Exception as exc:  # noqa: BLE001
            from .redact import redact_webhook_path_any

            safe = redact_webhook_path_any(str(exc), (token,))
            log.warning("[WPP] vendor business 设置失败: %s", safe)
            out["business_error"] = safe
        return out

    # ============================================================== 媒体下载

    async def download_image(self, msg_id: str, to_wxid: str, data_len: int = 0) -> bytes | None:
        """``/Tools/DownloadImg`` —— **字段名是 snake_case**（旧 ``MsgId`` 会报
        ``INVALID_ARGUMENT``）。返回 ``Data.data.buffer``（vendor 有 64KB 上限）。"""
        body: dict[str, Any] = {"msg_id": msg_id, "to_wxid": to_wxid,
                                "data_len": data_len or 0, "compress_type": 0}
        if data_len:
            body["section"] = {"start_pos": 0, "data_len": data_len}
        return await self._post_json_b64("/Tools/DownloadImg", body)

    async def download_image_cdn(self, file_aes_key: str, file_no: str,
                                variant: str = "") -> bytes | None:
        """``/Tools/CdnDownloadImage`` —— CDN 完整大图。"""
        body: dict[str, Any] = {"file_aes_key": file_aes_key, "file_no": file_no}
        if variant:
            body["variant"] = variant
        return await self._post_json_b64("/Tools/CdnDownloadImage", body)

    async def download_video_thumb(self, file_aes_key: str, file_no: str) -> bytes | None:
        """视频缩略图（与视频同 ``aes_key``/``file_no``，走图片接口）。"""
        return await self._post_json_b64("/Tools/CdnDownloadImage", {
            "FileAESKey": file_aes_key, "FileNo": file_no,
            "Section": {"StartPos": 0, "DataLen": 100000},
        })

    async def download_video(self, to_wxid: str, msg_id: int, data_len: int,
                            start_pos: int = 0, chunk_len: int = 1048576,
                            compress_type: int = 0) -> bytes | None:
        """``/Tools/DownloadVideo`` —— **分片**下载。

        完整视频要循环：``start_pos += len(chunk)`` 直到 ``start_pos >= totalLen``。
        """
        return await self._post_json_b64("/Tools/DownloadVideo", {
            "to_wxid": to_wxid, "msg_id": msg_id, "data_len": data_len,
            "section": {"start_pos": start_pos, "data_len": chunk_len},
            "compress_type": compress_type,
        })

    async def download_voice(self, msg_id: int, from_user_name: str, length: int,
                            bufid: str = "") -> bytes | None:
        """``/Tools/DownloadVoice`` —— 新 schema（``{msgId, fromUserName, length}``）。"""
        body: dict[str, Any] = {"msgId": msg_id, "fromUserName": from_user_name, "length": length}
        if bufid:
            body["bufid"] = bufid
        return await self._post_json_b64("/Tools/DownloadVoice", body)

    async def download_voice_legacy(self, aes_key: str, file_no: str) -> bytes | None:
        """``/Tools/DownloadVoice`` —— **老 schema**（``{aesKey, fileId}``）。"""
        return await self._post_json_b64("/Tools/DownloadVoice",
                                         {"aesKey": aes_key, "fileId": file_no})

    async def download_voice_binary(self, msg_id: int, *, new_msg_id: str = "",
                                   client_msg_id: str = "", master_buf_id: str = "0",
                                   fmt: int = 4, length: int = 0, chat_room_name: str = "",
                                   from_user_name: str = "", to_user_name: str = "",
                                   file_name: str = "voice.silk") -> bytes | None:
        """``/Tools/DownloadVoiceBinary`` —— 原始字节（SILK/AMR/MP3/WAV，非 JSON）。"""
        return await self._post_bytes("/Tools/DownloadVoiceBinary", {
            "msg_id": msg_id, "new_msg_id": new_msg_id, "client_msg_id": client_msg_id,
            "master_buf_id": master_buf_id, "format": fmt, "length": length,
            "chat_room_name": chat_room_name, "from_user_name": from_user_name,
            "to_user_name": to_user_name, "file_name": file_name or "voice.silk",
        })

    async def download_file_binary(self, attach_id: str, *, user_name: str = "",
                                   data_len: int = 0, start_pos: int = 0,
                                   section_len: int = 0, app_id: str = "",
                                   file_name: str = "") -> bytes | None:
        """``/Tools/DownloadFileBinary`` —— 文件原始字节流。

        ``data_len`` 超 200MB 直接拒（防 OOM），且带 ``Content-Length`` 二次防护。
        """
        if data_len > MAX_FILE_BYTES:
            log.warning("[WPP] 文件过大 %d > %d，拒绝下载", data_len, MAX_FILE_BYTES)
            return None
        body: dict[str, Any] = {
            "attach_id": attach_id, "user_name": user_name, "data_len": data_len,
            "section": {"start_pos": start_pos, "data_len": section_len or data_len},
        }
        if app_id:
            body["app_id"] = app_id
        if file_name:
            body["file_name"] = file_name
        return await self._post_bytes("/Tools/DownloadFileBinary", body, timeout=180)


def build_appmsg_xml(title: str, desc: str, url: str, thumb_url: str = "") -> str:
    """构造 ``type=5`` 的 appmsg XML（分享链接 / 引用回复都用它）。"""
    def esc(s: Any) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    return (
        f'<appmsg appid="" sdkver="0"><title>{esc(title)}</title>'
        f'<des>{esc(desc)}</des><action>view</action><type>5</type>'
        f'<url>{esc(url)}</url><thumburl>{esc(thumb_url)}</thumburl></appmsg>'
    )
