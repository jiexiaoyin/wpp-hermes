"""安全边界：SSRF 防护 + 本地路径读取白名单 + 出站白名单。

这三样**都是安全闸**，被 ``platform/outbound.py``（发媒体）与 ``tools/``（工具
handler）共用，所以单独成模块 —— 放任何一侧都会造成 tools → platform 的坏依赖方向。

## 为什么必须有

- **SSRF**：``send_file`` / ``send_voice`` / ``send_video`` 等工具接受任意 URL。
  LLM 被 prompt injection 诱导就能打 ``127.0.0.1`` / 内网（如 ``169.254.169.254``
  元数据端点）。所以**解析 DNS 后二次校验 IP**，防 DNS rebinding。
- **本地读**：``image_url`` 允许传本地路径 ⇒ 不设白名单就能读 ``/etc/passwd``、
  ``/root/.ssh/id_rsa`` 并**发到微信群里**。
- **出站白名单**（调试用）：微信是生产环境，发出去撤不回。调试期把
  ``WECHATPRO_SEND_ALLOWLIST`` 设成测试群 id，任何白名单外的发送**直接 raise**。

## 相对旧实现的两处修正

1. 旧 ``_LOCAL_READ_DIRS`` 写死了 ``/root/.hermes/profiles/wpp-wechat/workspace/``
   —— 单 profile 路径，多 profile / 多账号下会误放行或误拒。现在从
   ``HERMES_HOME`` 与**当前 profile** 推导。
2. 白名单目录改为**规范化后比对**（``os.path.realpath``）—— 旧实现用 ``abspath``，
   ``/tmp/../etc/passwd`` 这类路径能绕过（``abspath`` 会折叠 ``..``，所以那例其实
   挡住了；但**符号链接**挡不住：``/tmp/link → /etc/passwd`` 会被放行）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: 下载上限（200MB）
DEFAULT_MAX_BYTES = 200 * 1024 * 1024

#: 无条件允许的临时目录
_ALWAYS_READABLE = ("/tmp/", "/var/tmp/")

#: 出站白名单 env（**调试用**；未设则不限制）
SEND_ALLOWLIST_ENV = "WECHATPRO_SEND_ALLOWLIST"


class GuardError(ValueError):
    """安全闸拒绝。继承 ``ValueError`` 便于调用方按值错误处理。"""


# --------------------------------------------------------------------- 本地读


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def readable_dirs() -> tuple[str, ...]:
    """允许读取的本地目录（**已 realpath 规范化**，带尾斜杠）。"""
    dirs: list[str] = list(_ALWAYS_READABLE)

    # ① 本插件目录（部署后是 ~/.hermes/plugins/wechatpadpro/）
    dirs.append(str(_plugin_dir()) + os.sep)

    # ② HERMES_HOME 下的 workspace / cache —— 按当前 profile 推导，不写死 profile 名
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    roots = [Path(home) / "workspace", Path(home) / "cache", Path(home) / "tmp"]
    if profile and profile != "default":
        roots += [Path(home) / "profiles" / profile / "workspace"]
    dirs += [str(r) + os.sep for r in roots]

    out: list[str] = []
    for d in dirs:
        try:
            out.append(os.path.realpath(d) + os.sep if not d.endswith(os.sep)
                       else os.path.realpath(d.rstrip(os.sep)) + os.sep)
        except OSError:
            continue
    return tuple(dict.fromkeys(out))


def is_local_read_allowed(path: str) -> bool:
    """本地路径是否可读。

    ⚠️ 用 ``realpath`` 而非 ``abspath``：后者解析 ``..`` 但**不解析符号链接**，
    ``/tmp/link_to_passwd`` 会被放行。
    """
    if not path:
        return False
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except OSError:
        return False
    return any(real == d.rstrip(os.sep) or real.startswith(d) for d in readable_dirs())


# ------------------------------------------------------------------------ SSRF


def assert_public_url(url: str) -> str:
    """校验 URL 不指向内网/保留地址。返回原 URL 或 raise ``GuardError``。

    **解析 DNS 后逐 IP 校验** —— 只看域名字符串挡不住 DNS rebinding。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise GuardError(f"只允许 http/https URL: {url[:60]}")
    host = parsed.hostname or ""
    if not host:
        raise GuardError(f"URL 无主机名: {url[:60]}")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise GuardError(f"URL 域名无法解析: {host}") from exc

    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip.split("%")[0])   # 去掉 IPv6 zone id
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise GuardError(f"SSRF 拦截: 拒绝内网/保留 IP {raw_ip}（host={host}）")
    return url


def assert_endpoint_public(url: str) -> str:
    """**不做 DNS** 的端点校验：拒绝 loopback / 私网 / link-local **字面量 IP** 与 localhost。

    为什么另立一个而不是复用 :func:`assert_public_url`：后者要 ``getaddrinfo``，于是
    **解析器抖一下就把一次 judge 调用打成硬失败**。而对 judge 来说，
    ``base_url`` 是**运维配置**（``cfg.base_url``）或写死的默认端点，**不是用户输入** ——
    没有"被诱导改 URL"这条路径（用户唯一能影响的是图片 URL，那只作为**数据**发给厂商，
    不由我们抓取）。所以这里只要挡住"配置里写了个内网字面量地址"这一种，够用且不误伤。

    真正需要防 DNS rebinding 的是**抓取**路径（``safe_fetch_url`` / ``assert_public_url``），
    那里的 URL 来自工具入参，可以用不可信输入形容。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise GuardError(f"只允许 http/https 端点: {url[:60]}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise GuardError(f"端点无主机名: {url[:60]}")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise GuardError(f"SSRF 拦截: 拒绝 localhost 端点（{host}）")
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return url                      # 域名（非字面量 IP）⇒ 放行，见 docstring
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        raise GuardError(f"SSRF 拦截: 拒绝内网/保留 IP {host}")
    return url


async def safe_fetch_url(url: str, *, timeout: int = 30,
                         max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """SSRF 防护的 URL 下载。"""
    import aiohttp

    assert_public_url(url)
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise GuardError(f"下载失败 HTTP {resp.status}: {url[:60]}")
            declared = resp.headers.get("Content-Length")
            if declared:
                try:
                    if int(declared) > max_bytes:
                        raise GuardError(f"下载内容过大（声明 {declared} > {max_bytes}）")
                except ValueError:
                    pass
            data = await resp.read()
            if len(data) > max_bytes:
                raise GuardError(f"下载内容过大（{len(data)} > {max_bytes}）")
            return data


# ------------------------------------------------------------------ 出站白名单


def send_allowlist() -> tuple[str, ...]:
    """``WECHATPRO_SEND_ALLOWLIST`` 解析出的白名单（逗号分隔）。未设 ⇒ 空 tuple。"""
    raw = (os.environ.get(SEND_ALLOWLIST_ENV) or "").strip()
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def check_outbound_allowed(peer_id: str, account_id: str = "") -> None:
    """出站白名单闸（**未设白名单时是 no-op**）。

    调试期的保险：微信是生产环境，发出去撤不回。设了白名单后，往别处发直接 raise。
    """
    allowed = send_allowlist()
    if not allowed:
        return
    targets = {peer_id, f"{account_id}:{peer_id}" if account_id else peer_id}
    if targets & set(allowed):
        return
    raise GuardError(
        f"出站被 {SEND_ALLOWLIST_ENV} 拦截：{peer_id!r} 不在白名单 {list(allowed)} 内。"
        f"（微信是生产环境，该闸用于调试期防止误发）"
    )
