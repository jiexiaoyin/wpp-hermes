"""OSS 上传的**通道中立机制层**（扩展边界 P0-7，2026-10-19）。

从 ``inbound/media_oss.py`` 抽出与 WPP 无关的部分：凭据解析与缓存、文件名净化、
公网 URL 拼接、``ossutil cp`` 重试、临时文件生命周期、日志脱敏。

本模块不 import 任何平台模块，也**不假设** OSS key 的结构 —— 三个依赖由调用方注入：

* ``load_config``：零参可调用，返回 :class:`OssConfig` 或 ``None``。凭据来自哪个
  profile / 哪个文件由通道侧决定（Hermes 的 profile secret scope 不是通用概念）。
* ``key_builder(account_id, kind, filename)``：对象键结构（WPP 是
  ``wpp/{account}/{kind}/{date}/…``）。
* ``uploader(oss, local_path, oss_key)``：真正执行上传的可调用（默认本模块
  :func:`upload_to_oss`；注入是为了测试与替换实现）。

WPP 侧的 ``plugin/inbound/media_oss.py`` 保留原有公开 API，只是把这些依赖绑进来。
安全不变量（凭据不进日志/不进 argv、失败只降级、临时文件必删）在本层实现，
因此任何通道接入都自动继承。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: TS 同款默认端点（凭据里没写 ``endpoint`` 时用它）
DEFAULT_ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"

#: 凭据缓存 TTL（TS ``ttlMs: 30_000``）。低频变动 + 省媒体热路径的 disk IO。
CRED_TTL_S = 30.0
#: 缓存条数上限（TS ``maxSize: 2``）
CRED_CACHE_MAX = 2

#: ``ossutil cp`` 单次超时（TS ``timeoutMs: 60_000``）
UPLOAD_TIMEOUT_S = 60.0
#: 上传尝试次数（TS ``attempt <= 3``）
UPLOAD_ATTEMPTS = 3

_HEX_RE = re.compile(r"^[a-f0-9]{8,64}$", re.IGNORECASE)
_EXT_RE = re.compile(r"\.([a-zA-Z0-9]+)$")
#: ⚠️ **必须带 ``re.ASCII``**：JS 的 ``\w`` 是 ASCII 的 ``[A-Za-z0-9_]``，而 **Python 的 ``\w``
#: 是 Unicode 的** —— 不加这个 flag，``我的 文件.pdf`` 会原样进 OSS key（只有空格被换掉），
#: 与 TS 产出的 key **不是同一个字符串**：中文键在 URL 里要百分号编码，而"两套实现对不上"
#: 正是这一整轮要消灭的东西。实测抓到过（测试名里就写着这个）。
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-]", re.ASCII)

#: ``路径 -> (过期时刻, 配置)``。**只放读成功且字段齐全的**（见 INVARIANT 第 2 条）。
_CACHE: dict[str, tuple[float, OssConfig]] = {}

#: 本进程**见过**的密钥串 —— 只为日志脱敏（``_scrub``）而留，不参与任何判断。
#: 用集合而不是遍历 ``_CACHE``：缓存会过期、会被清，而"日志里永远不出现密钥"不能有窗口期。
_SECRETS: set[str] = set()


def _scrub(text: str) -> str:
    """把文本里所有"见过的密钥串"换掉。**所有要进日志的外部文本都必须过这里。**"""
    for secret in _SECRETS:
        if secret and secret in text:
            text = text.replace(secret, "<隐去>")
    return text


@dataclass(frozen=True)
class OssConfig:
    """OSS 四要素。``__repr__`` 被覆盖成不含密钥 —— 免得有人 ``log.info("%s", cfg)`` 就把密钥打出去。"""

    access_key_id: str
    access_key_secret: str
    bucket: str
    endpoint: str

    def __repr__(self) -> str:  # pragma: no cover —— 只为"手滑打印"兜底
        return (f"OssConfig(bucket={self.bucket!r}, endpoint={self.endpoint!r}, "
                f"access_key_id=<{len(self.access_key_id)} 字符, 已隐去>, "
                f"access_key_secret=<{len(self.access_key_secret)} 字符, 已隐去>)")


def clear_oss_config_cache() -> None:
    """清缓存（测试注入 / 凭据轮换后强制重读）。"""
    _CACHE.clear()


def load_oss_config(path: str = "", *, now: float | None = None,
                    raw: Any = None) -> OssConfig | None:
    """读 OSS 凭据。**任何失败都返 None**（缺文件 / 坏 JSON / 字段缺），只有齐全才进缓存。

    ``path`` 是缓存键，由调用方决定（文件路径 / ``"<env>"`` 等）。``raw`` 可选地传入
    调用方已经读好的 dict（例如 profile secret scope 里的值），跳过文件读取；``raw``
    为 ``None`` 时从 ``path`` 读 JSON。

    ``endpoint`` 缺省用 :data:`DEFAULT_ENDPOINT`（TS 同款 ``d.endpoint || …``）；
    其余三个字段缺**任意一个**就整份作废 —— 半份凭据传上去只会得到 403，
    不如当场判"没有凭据"。
    """
    t = time.monotonic() if now is None else now
    hit = _CACHE.get(path)
    if hit and hit[0] > t:
        return hit[1]
    if raw is None:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:  # noqa: BLE001 —— 文件不在/坏 JSON 都是"没有凭据"
            return None
    if not isinstance(raw, dict):
        return None
    ak = raw.get("accessKeyId")
    sk = raw.get("accessKeySecret")
    bucket = raw.get("bucket")
    if not isinstance(ak, str) or not ak or not isinstance(sk, str) or not sk \
            or not isinstance(bucket, str) or not bucket:
        return None
    endpoint = raw.get("endpoint")
    cfg = OssConfig(ak, sk, bucket,
                    endpoint if isinstance(endpoint, str) and endpoint else DEFAULT_ENDPOINT)
    _SECRETS.update((ak, sk))
    # 条数上限：满了先丢最早过期的那个（TS LruCache maxSize:2；这里按过期时刻近似 LRU）
    if len(_CACHE) >= CRED_CACHE_MAX and path not in _CACHE:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[path] = (t + CRED_TTL_S, cfg)
    return cfg


def sanitize_filename_part(value: Any, fallback_len: int = 16) -> str:
    """把"md5"这类自由字段净化成文件名片段。

    v0 的 md5 是 hex digest，但 v1 的 md5 是**自由字符串** ⇒ 直接拼进 OSS key 能污染路径
    （``../../`` 之类）。所以只认 ``^[a-f0-9]{8,64}$``（大小写都收，转小写）；
    其余一律换成**随机** hex —— 不信自由字符串，也不给它一个可预测的替身。
    """
    s = value if isinstance(value, str) else ""
    if _HEX_RE.match(s):
        return s.lower()
    n = max(1, (fallback_len + 1) // 2)
    return secrets.token_hex(n)[:fallback_len]


def public_url(oss: OssConfig, oss_key: str) -> str:
    """公网 URL：``https://{bucket}.{endpoint}/{key}``（TS 同款拼接）。"""
    return f"https://{oss.bucket}.{oss.endpoint}/{oss_key}"


async def _run_ossutil(args: list[str], timeout_s: float) -> tuple[int, str]:
    """跑一次 ``ossutil``，返回 ``(退出码, stderr+stdout 截断)``。**认不出二进制就直接报错。**"""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        # TS 的 execAsync 超时会 kill 子进程；不 kill 会留一个僵尸 ossutil 一直挂着
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover —— 已经自己退了
            pass
        await proc.wait()
        return -1, f"超时 {timeout_s:.0f}s"
    return proc.returncode or 0, _scrub((err or b"").decode("utf-8", "replace")
                                        + (out or b"").decode("utf-8", "replace"))[:200]


async def upload_to_oss(oss: OssConfig, local_path: str, oss_key: str, *,
                        run_ossutil: Callable[[list[str], float], Any] | None = None) -> str:
    """``ossutil cp`` 本地文件 → OSS，返回公网 URL。**3 次尝试后仍失败就抛**（调用方降级）。

    退避是 ``1s * attempt``（TS ``1000 * attempt``）—— OSS 的临时网络抖动能靠它过去。
    ``run_ossutil`` 可注入（测试 / 换执行器）。
    """
    run = run_ossutil if run_ossutil is not None else _run_ossutil
    binary = shutil.which("ossutil")
    if not binary:
        # 有意偏离 TS（见 :mod:`plugin.inbound.media_oss` 的说明）：缺二进制别空转 3 次，
        # 报一句人能看懂的。
        raise RuntimeError("这台机器没有 ossutil（媒体不会被上传；这是降级不是故障）")
    last = ""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        code, tail = await run(
            [binary, "cp", local_path, f"oss://{oss.bucket}/{oss_key}",
             "--endpoint", oss.endpoint, "-f"],
            UPLOAD_TIMEOUT_S)
        if code == 0:
            return public_url(oss, oss_key)
        last = tail
        # ⚠️ 这里再 ``_scrub`` 一次，**不依赖** ``_run_ossutil`` 那层已经脱敏过：
        #    脱敏下在"读外部文本的函数"上，那道闸就只覆盖它自己产出的文本；
        #    而"日志里不许出现密钥"要对**所有**进日志的外部文本成立（未来的 helper / 换实现）。
        log.warning("[OSS] ossutil cp 第 %d/%d 次失败（code=%s）: %s",
                    attempt, UPLOAD_ATTEMPTS, code, _scrub(tail))
        if attempt < UPLOAD_ATTEMPTS:
            await asyncio.sleep(1.0 * attempt)
    raise RuntimeError(f"ossutil cp {UPLOAD_ATTEMPTS} 次都失败（最后一次 code={code}）: {last}")


async def oss_upload_buffer(buf: bytes, filename: str, prefix: str, *,
                            account_id: str | None = None,
                            kind: str | None = None,
                            key_builder: Callable[[str, str, str], str] | None = None,
                            load_config: Callable[[], OssConfig | None] | None = None,
                            uploader: Callable[[OssConfig, str, str], Any] | None = None) -> str:
    """字节 → 临时文件 → ``ossutil`` → 公网 URL。缺凭据抛错（调用方降级）。**临时文件必删。**

    ``account_id`` 与 ``kind`` **都给**且注入了 ``key_builder`` 时才用通道自己的目录结构；
    否则退回老 ``{prefix}/{hash}-{name}``。
    """
    loader = load_config if load_config is not None else load_oss_config
    up = uploader if uploader is not None else upload_to_oss
    oss = loader()
    if oss is None:
        raise RuntimeError("没有 OSS 凭据（文件不存在或字段不全）")
    m = _EXT_RE.search(filename)
    ext = m.group(1) if m else "bin"
    tmp_path = os.path.join(tempfile.gettempdir(), f"oss-upload-{secrets.token_hex(6)}.{ext}")
    try:
        # 写临时文件可能到 MB 级，放线程池（ASYNC230）
        def _write_tmp() -> None:
            with open(tmp_path, "wb") as fh:
                fh.write(buf)

        await asyncio.to_thread(_write_tmp)
        digest = hashlib.md5(filename.encode("utf-8")).hexdigest()[:12]
        safe_name = _UNSAFE_NAME_RE.sub("_", filename)
        if account_id and kind and key_builder is not None:
            oss_key = key_builder(account_id, kind, f"{digest}-{safe_name}")
        else:
            oss_key = f"{prefix}/{digest}-{safe_name}"
        return await up(oss, tmp_path, oss_key)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def upload_media_bytes(buf: bytes, filename: str, prefix: str = "wpp", *,
                             account_id: str, kind: str,
                             key_builder: Callable[[str, str, str], str] | None = None,
                             load_config: Callable[[], OssConfig | None] | None = None,
                             uploader: Callable[[OssConfig, str, str], Any] | None = None) -> str | None:
    """媒体富化的**唯一出口**：上传成功返 URL，任何失败返 ``None`` 且只 warn（INVARIANT 第 3 条）。

    调用方拿到 ``None`` 就"这条消息不带 URL"，不算错误 —— 参考实现六个 enrich 入口全这个形状。
    """
    if not buf:
        return None
    try:
        url = await oss_upload_buffer(
            buf, filename, prefix, account_id=account_id, kind=kind,
            key_builder=key_builder, load_config=load_config, uploader=uploader,
        )
    except Exception as exc:  # noqa: BLE001 —— 媒体富化失败绝不能冒泡成"消息处理失败"
        log.warning("[OSS] 上传失败（本条消息不带 URL）: %s", _safe_err(exc))
        return None
    log.info("[OSS] 上传成功 %s（%d 字节）", url, len(buf))
    return url


def _safe_err(exc: BaseException) -> str:
    """异常文本**过一遍**再进日志：凭据绝不许出现在日志里（INVARIANT 第 1 条）。

    ``upload_to_oss`` 失败时会把 ``ossutil`` 的 stderr 带出来，而 osutil 在某些错误下
    会回显 ``--access-key-id``（我们不传它，但**万一日后有人加了参数**，这里就是那道闸）。
    做法是：任何出现密钥子串的文本都换掉，并截断长度。
    """
    return _scrub(str(exc))[:300]


__all__ = [
    "CRED_CACHE_MAX",
    "CRED_TTL_S",
    "DEFAULT_ENDPOINT",
    "UPLOAD_ATTEMPTS",
    "UPLOAD_TIMEOUT_S",
    "OssConfig",
    "clear_oss_config_cache",
    "load_oss_config",
    "oss_upload_buffer",
    "public_url",
    "sanitize_filename_part",
    "upload_media_bytes",
    "upload_to_oss",
]
