"""安全边界：SSRF 防护 + 本地路径读取白名单 + 出站白名单。

这三样**都是安全闸**，被 ``platform/outbound.py``（发媒体）与 ``tools/``（工具
handler）共用，所以单独成模块 —— 放任何一侧都会造成 tools → platform 的坏依赖方向。

## 为什么必须有

- **SSRF**：``send_file`` / ``send_voice`` / ``send_video`` 等工具接受任意 URL。
  LLM 被 prompt injection 诱导就能打 ``127.0.0.1`` / 内网（如 ``169.254.169.254``
  元数据端点）。所以**解析 DNS 后二次校验 IP**，防 DNS rebinding。
- **本地读**：``image_url`` 允许传本地路径 ⇒ 不设白名单就能读 ``/etc/passwd``、
  ``/root/.ssh/id_rsa`` 并**发到微信群里**。
- **统一白名单**（收发共用，2026-10-19 起）：微信是生产环境，发出去撤不回。
  放行集合 = 账号 ``allowFrom`` / ``groupAllowFrom`` / ``adminUsers``
  ∪ ``WECHATPRO_SEND_ALLOWLIST`` 的额外条目；名单非空时，名单外的发送**直接 raise**。
  ``.env`` 那一项只是可选补充，加人请用 ``/user add`` 或全员群同步。

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
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: 下载上限（200MB）
DEFAULT_MAX_BYTES = 200 * 1024 * 1024

#: 无条件允许的临时目录
_ALWAYS_READABLE = ("/tmp/", "/var/tmp/")

#: **任何目录**下一律拒绝的敏感文件名/后缀（含 /tmp —— 否则 ``/tmp/.env``、
#: ``/tmp/id_rsa`` 都能被当成"图片"发进微信群）。
#: 只按**文件名/路径段**判断，不做内容嗅探：漏判的代价是"本可发但被拒"，
#: 过度放行的代价是"凭据发到生产群且撤不回"。
_SENSITIVE_NAMES = frozenset({
    ".netrc", ".pgpass", ".git-credentials", ".my.cnf",
    "credentials", "authcode", "authcodes",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})
_SENSITIVE_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".kdbx",
)
_SENSITIVE_DIR_PARTS = frozenset({
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store",
})

#: 出站白名单 env（**调试用**；未设则不限制）
SEND_ALLOWLIST_ENV = "WECHATPRO_SEND_ALLOWLIST"


class GuardError(ValueError):
    """安全闸拒绝。继承 ``ValueError`` 便于调用方按值错误处理。"""


# --------------------------------------------------------------------- 本地读


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _denied_dirs() -> tuple[str, ...]:
    """**永远不许**通过媒体工具读取的目录（凭据/备份所在）。

    2026-10-18 P0：``readable_dirs()`` 以前把整个插件目录放进白名单，而
    ``plugin/accounts/`` 就在里面 —— 群里任何人 prompt injection 一句
    「把这个文件发给我」，模型调 ``wpp_send_file`` 就能把含 authcode 的账号
    JSON 当文件发出去。白名单目录里的**子目录**必须能被显式排除，否则
    "允许读插件目录"天然等于"允许读凭据"。
    """
    dirs = [_plugin_dir() / "accounts"]
    from .env_compat import gate_env

    backup = gate_env("WECHATPRO_ACCOUNT_BACKUP_DIR")
    if backup.strip():
        dirs.append(Path(backup).expanduser())
    out: list[str] = []
    for d in dirs:
        try:
            out.append(os.path.realpath(d) + os.sep)
        except OSError:
            continue
    return tuple(dict.fromkeys(out))


def _current_profile_home() -> Path | None:
    """当前 task 的 secret scope home（拿不到 ⇒ ``None``）。

    只做**软依赖**：插件单测/进程外 import 时 ``agent.secret_scope`` 不存在，
    这时退回 env 推导。任何异常都当"没有 scope"处理，读取白名单不能因为
    Hermes 内部接口变化而整条报错。
    """
    try:
        from agent.secret_scope import current_secret_scope_home
    except ImportError:
        return None
    try:
        raw = current_secret_scope_home()
    except Exception:  # noqa: BLE001 —— 读白名单绝不允许把工具打挂
        return None
    if not raw:
        return None
    try:
        p = Path(os.path.expanduser(str(raw)))
    except (TypeError, ValueError):
        return None
    return p if p.is_absolute() else None


def _is_sensitive_path(real: str) -> bool:
    """文件名/路径段是否属于凭据类（**对白名单内所有目录生效**，含 /tmp）。"""
    p = Path(real)
    if {part.lower() for part in p.parts} & _SENSITIVE_DIR_PARTS:
        return True
    name = p.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    return name in _SENSITIVE_NAMES or name.endswith(_SENSITIVE_SUFFIXES)


def readable_dirs() -> tuple[str, ...]:
    """允许读取的本地目录（**已 realpath 规范化**，带尾斜杠）。"""
    dirs: list[str] = list(_ALWAYS_READABLE)

    # ① 本插件目录（部署后是 ~/.hermes/plugins/wechatpadpro/）
    dirs.append(str(_plugin_dir()) + os.sep)

    # ② HERMES_HOME 下的 workspace / cache —— 按**当前 profile** 推导，不写死 profile 名
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    profile = (os.environ.get("HERMES_PROFILE") or "").strip()
    roots = [Path(home) / "workspace", Path(home) / "cache", Path(home) / "tmp"]

    # ★ 优先用 secret scope（多 profile 网关的正解）：scope 在 task 上下文里，
    #   即使 systemd 没带 HERMES_PROFILE 也能定位到**当前** profile。
    scope_home = _current_profile_home()
    if scope_home is not None:
        roots += [scope_home / sub for sub in ("workspace", "cache", "tmp")]
        # scope 明确时**不**扫描别的 profile —— 否则 wpp-wechat 的会话能读到
        # wecom 的 workspace（跨租户/跨业务泄露）。
    elif profile and profile != "default":
        roots += [Path(home) / "profiles" / profile / sub
                  for sub in ("workspace", "cache", "tmp")]
    else:
        # ⚠️ 2026-10-19 修复：多 profile 网关（systemd `gateway run`）**不带**
        #   HERMES_PROFILE env、且某些后台任务没有 secret scope ⇒ 旧逻辑只放行
        #   ~/.hermes/workspace，profile 自己的 workspace/cache 全部"本地媒体读取被拒"
        #   （wpp_send_image 从 profile workspace 发图必挂，实测 07:24）。
        #   ⇒ 两者都拿不到时**最后兜底**扫描 profiles/*/ 的工作目录面。
        #   安全边界：敏感文件名/目录先过全局拒绝名单（见 `_is_sensitive_path`），
        #   凭据目录（plugins/accounts、备份目录）另有明确拒绝。
        profiles_root = Path(home) / "profiles"
        try:
            if profiles_root.is_dir():
                for p in sorted(profiles_root.iterdir()):
                    if not p.is_dir():
                        continue
                    for sub in ("workspace", "cache", "tmp"):
                        roots.append(p / sub)
        except OSError:
            pass
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

    # ★ 先判拒绝名单（凭据/备份目录），**再**判白名单 —— 顺序不能反：
    #   ``plugin/accounts`` 本身就在 ``plugin/`` 白名单里。
    for denied in _denied_dirs():
        if real == denied.rstrip(os.sep) or real.startswith(denied):
            log.warning("[WPP] 拒绝读取凭据目录内的文件: %s", real[:120])
            return False
    # ★ 全局敏感文件（对 /tmp 这类"无条件可读"目录尤其重要）
    if _is_sensitive_path(real):
        log.warning("[WPP] 拒绝读取敏感文件: %s", real[:120])
        return False

    # 插件目录里的点文件（.env 等）与 JSON（账号/配置的常见形态）同样拒绝
    plugin = os.path.realpath(_plugin_dir()) + os.sep
    if real.startswith(plugin):
        name = os.path.basename(real)
        if name.startswith(".") or name.lower().endswith((".json", ".env")):
            log.warning("[WPP] 拒绝读取插件目录内的敏感文件: %s", real[:120])
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
        # ``not is_global`` 补齐 ``is_private`` 漏掉的段：CGNAT ``100.64.0.0/10``、
        # 基准测试 ``198.18.0.0/15``、``192.0.0.0/24`` 等 —— 它们能路由到内网设备，
        # 但 ``is_private`` 为 False。``is_reserved`` 仍显式保留（NAT64
        # ``64:ff9b::/96`` 内嵌 127.0.0.1 时 ``is_global`` 为 True）。
        if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
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
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise GuardError(f"SSRF 拦截: 拒绝内网/保留 IP {host}")
    return url


#: 重定向最多跟几跳。每一跳都重新过 :func:`assert_public_url`。
_MAX_REDIRECTS = 3


async def safe_fetch_url(url: str, *, timeout: int = 30,
                         max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """SSRF 防护的 URL 下载。

    ⚠️ **必须逐跳校验**（2026-10-18 修）：以前只校验初始 URL，然后
    ``sess.get(...)`` 用 aiohttp 默认的 ``allow_redirects=True`` —— 攻击者回一个
    302 到 ``http://127.0.0.1:8062/mcp`` 就绕过了 SSRF 闸，内容还会被当成图片发回微信。
    现在手动跟跳（≤3 跳），每一跳都重新解析/校验 IP。
    """
    from urllib.parse import urljoin

    import aiohttp

    current = url
    async with aiohttp.ClientSession() as sess:
        for hop in range(_MAX_REDIRECTS + 1):
            assert_public_url(current)
            async with sess.get(
                current, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location") or ""
                    if not loc:
                        raise GuardError(f"重定向无 Location: {current[:60]}")
                    if hop >= _MAX_REDIRECTS:
                        raise GuardError(f"重定向超过 {_MAX_REDIRECTS} 跳: {url[:60]}")
                    current = urljoin(current, loc)
                    continue
                if resp.status != 200:
                    raise GuardError(f"下载失败 HTTP {resp.status}: {current[:60]}")
                declared = resp.headers.get("Content-Length")
                if declared:
                    # ⚠️ 解析与判定必须分开：``GuardError`` 继承 ``ValueError``，
                    # 把 ``raise GuardError`` 写在 ``try`` 里会被下面的
                    # ``except ValueError`` **静默吞掉** ⇒ 声明上限形同虚设。
                    try:
                        declared_n = int(declared)
                    except ValueError:
                        declared_n = 0
                    if declared_n > max_bytes:
                        raise GuardError(
                            f"下载内容过大（声明 {declared} > {max_bytes}）")
                # ★ 必须**边读边判**：``resp.read()`` 会把 chunked/无 Content-Length 的
                #   响应整包读进内存，再判长度已经晚了 —— 攻击者回一个无限流就 OOM。
                #   这条路径的 URL 来自工具入参（不可信），上限必须是硬上限。
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise GuardError(
                            f"下载内容过大（已读 {len(buf)} > {max_bytes}）")
                return bytes(buf)
    raise GuardError(f"下载失败（重定向未收敛）: {url[:60]}")


# ------------------------------------------------------------------ 出站白名单


def _parse_allowlist(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def env_allowlist(profile_home: str | None = None) -> tuple[str, ...]:
    """``WECHATPRO_SEND_ALLOWLIST`` 解析出的**额外**条目（逗号分隔）。未设 ⇒ 空 tuple。

    ``profile_home`` 非空时**优先**读该 profile 自己的 ``.env``。这是给
    「没有 scope 的后台任务/cron standalone」用的：那种上下文里
    ``platform_gate_env`` 会回落到**根 ``.env``**，把 wpp-wechat 的六条白名单
    读成 default 的一条（2026-10-19 07:36 生产事故）。调用方明确知道
    profile home 时，就不要让 ambient scope 决定读哪份配置。
    """
    if profile_home:
        try:
            from pathlib import Path

            env_path = Path(profile_home) / ".env"
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == SEND_ALLOWLIST_ENV:
                    val = val.strip().strip('"').strip("'")
                    return _parse_allowlist(val)
        except OSError:
            pass
    from .env_compat import gate_env

    return _parse_allowlist(gate_env(SEND_ALLOWLIST_ENV))


def send_allowlist(profile_home: str | None = None) -> tuple[str, ...]:
    """兼容旧名：仅 env 那部分（**不是**完整生效名单，见 :func:`unified_allowlist`）。

    保留它是因为工具/探针里还有调用方只想要 ``WECHATPRO_SEND_ALLOWLIST`` 这一层；
    生产收发路径应使用 :func:`unified_allowlist`。
    """
    return env_allowlist(profile_home)


def unified_allowlist(profile_home: str | None = None, accounts: Any = None) -> tuple[str, ...]:
    """统一白名单（2026-10-19 起**收发同一份**）。

    2026-10-19 老板拍板：白名单不区分进出站。一个人/群既然在账号的
    ``allowFrom`` / ``groupAllowFrom`` / ``adminUsers`` 里，机器人主动发给他/它
    也必须放行；否则会出现「周州能私聊机器人，机器人一转发就被拦」这种割裂。

    生效集合 = ``WECHATPRO_SEND_ALLOWLIST``（额外条目，保留兼容）
    ∪ 各已连接账号的 ``allowFrom`` / ``groupAllowFrom`` / ``adminUsers``。

    ``accounts`` 由调用方传入**已连接**的账号（``Account`` 对象或任何带这三个
    属性的对象）。``None`` 时懒加载本插件的账号文件；加载失败只记日志，不让
    出站闸把整个发送链路拖死（此时至少还有 env 那一层）。
    """
    items: list[str] = list(env_allowlist(profile_home))
    pool = accounts
    if pool is None:
        try:
            from . import accounts as accounts_mod

            pool = accounts_mod.load_all().values()
        except Exception as exc:  # noqa: BLE001 —— 读不到账号文件不该让发送崩
            log.warning("[WPP] 统一白名单：账号配置读取失败（仅用 env 那层）: %s", exc)
            pool = ()
    for acct in pool or ():
        for attr in ("allow_from", "group_allow_from", "admin_users"):
            try:
                items.extend(getattr(acct, attr, ()) or ())
            except Exception:  # noqa: BLE001 —— 单个账号形状不对不拖累其它账号
                continue
    return _dedupe_allowlist(items)


def _dedupe_allowlist(items: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        val = str(item or "").strip()
        if not val or val in seen:
            continue
        seen.add(val)
        out.append(val)
    return tuple(out)


def check_outbound_allowed(peer_id: str, account_id: str = "",
                           *, allowed: tuple[str, ...] | None = None) -> None:
    """统一白名单闸（**名单为空时是 no-op**）。

    放行集合与入站是同一份（``allowFrom`` / ``groupAllowFrom`` / ``adminUsers``），
    另加 ``WECHATPRO_SEND_ALLOWLIST`` 的显式条目。名单非空时，往名单外发直接 raise。

    ``allowed`` 允许调用方传入**连接期捕获**的白名单（见 ``WppClient``）—— 发送
    任务不一定还处在 profile scope 里，不能让它在发送时重新读一次 ambient env。
    """
    allowed = unified_allowlist() if allowed is None else tuple(allowed)
    if not allowed:
        return
    targets = {peer_id, f"{account_id}:{peer_id}" if account_id else peer_id}
    if targets & set(allowed):
        return
    raise GuardError(
        f"出站被统一白名单拦截：{peer_id!r} 不在名单内。"
        f"把对方加进私聊白名单用 /user add <wxid>，群用 /group add <群id>，"
        f"或用 /user sync 从全员群同步员工；名单外的目标不会发送。"
        f"（微信是生产环境，发出去撤不回）"
    )
