"""OSS 上传（W7-1，移植 TS ``inbound/media-enrich/shared.ts`` 172 行）。

## 这一层在 py2 里为什么只剩"上传"半截

TS 的 ``enrichImageMessage`` / ``enrichVideoMessage`` / … 每个都自带**下载 → 上传 → 注入 URL**
三段。py2 的**下载**那半已经有了（``adapter._fetch_media`` 把二进制下到 Hermes 本地缓存，
图片还必须下 —— 心流 judge 看图要的是本地路径），所以本模块只补**上传**这一截：
拿字节 → ``ossutil cp`` → 公网 URL，再由 ``_fetch_media`` 把 URL 写回消息。

⇒ **下载的成败不归这里管**，这里的输入只有"已经拿到的字节"。缺凭据/缺 ossutil/上传失败
一律返 ``None``（降级成"这条消息不带 URL"，与 TS 同款降级），**绝不抛** ——
媒体富化失败不该挡住一条消息（TS 六个 enrich 入口全都写着"失败返 mediaUrl:null、不抛"）。

## INVARIANT: W7-1（三条）

1. **凭据只从 文件/环境变量 读，永不入库、永不进日志**：本模块所有日志只许出现
   bucket / endpoint / key / URL / 异常文本 —— 绝不许出现 ``accessKeyId``/``accessKeySecret``
   的任何前缀（连长度都不打，见 ``_safe_err``）。测试有一条判据专门钉这个。
2. **只有"配置齐全"才进缓存**（TS 同款）：读失败/字段缺 ⇒ 返 ``None`` 且**不缓存**
   ⇒ 老板往机器上补一份凭据文件后**当次即生效**，不必重启。
3. **降级是单条消息级的**：上传失败 = 这条消息不带 URL，不重试到天荒地老
   （3 次指数退避后人就放弃，与 TS 一致），也**不影响**任何别的消息。

## 与 TS 的两处有意偏离（都写在这里，别当成漏移植）

* **缺 ``ossutil`` 二进制时快速失败**，不空转 3 次：TS 的 ``execAsync`` 把 spawn 失败
  当成一次普通的非零退出 ⇒ 会有 1s+2s 的无谓退避。py2 先 ``shutil.which`` 探一次，
  缺了就直接报"这台机器没装 ossutil"（错误信息比"3 次都失败"有用得多）。
  ⚠️ 这条偏离是**为了排障**，不是放宽：真人看日志时能一眼分清"没装"与"传不上去"。
* **``--endpoint`` 传参照抄 TS**（不用 env）。另外 ``ossutil`` 的 ``-f`` 是覆盖确认，
  照抄。

⚠️ **凭据不随代码走**：本模块**只读**凭据文件，**不代拷**（``deploy.sh`` 也不同步凭据 ——
凭据是机器自己的数据，不是代码产物）。缺了就是"不带 URL"的降级，不是故障。
⇒ 换机器要**手工**放，见下一节那三样。⚠️ 历史（别拿旧结论当现状）：
老板 2026-10-02 二次确认「先不放凭据」（``.183`` 当时是"整条降级路"），
2026-10-03 改判「放 OSS 凭据」⇒ ``.183`` 上那三样已**手工**落位（**代码未变**：
本模块至今仍然只读、不代拷）。

## ★ 真机上让上传真能成的**三样东西**（排障先看这里；2026-10-03 在 ``.183`` 落实）

缺**任何一样**，现场都只有一行 ``[OSS] 上传失败（本条消息不带 URL）``：

1. ``/usr/local/bin/ossutil`` —— 由 ``shutil.which`` 在**进程 PATH** 上找
   （``.183`` 的 systemd user 单元 PATH 已含 ``/usr/local/bin``；换机器要自己确认）；
2. ``OSS_ACCESS_KEY_ID`` / ``OSS_ACCESS_KEY_SECRET`` / ``OSS_BUCKET``（profile ``.env``，
   Hermes 规范做法）—— **本模块**只消费 ``bucket`` 与 ``endpoint``（拼公网 URL、
   传 ``--endpoint``）；缺 env 时才回落到 ``~/.hermes/state/credentials/oss-credentials.json``；
3. ``~/.ossutilconfig`` —— **ossutil 自己**读它**鉴权**。

⚠️ **第 3 样最容易漏，漏了最难看出来**：``accessKeyId``/``accessKeySecret`` **从不进命令行**
（与 TS 同款 —— 参数会出现在 ``ps``／``/proc/<pid>/cmdline`` 里，而 ``ossutil`` 本来就有
一条专读配置文件的鉴权路；有一条用例把这件事钉死，防的是"顺手加个 ``-i``")。
只放第 2 样、不放第 3 样时，每次 ``cp`` 都以 ``code=1`` 失败，而日志里**分不出**
是"没鉴权"还是"网络不通" —— 这一条是 2026-10-03 落 OSS 时真踩过的：
本模块把 AK/SK 读进 ``OssConfig`` 却一个字节都不往下传，**能工作的那一半全靠
``~/.ossutilconfig``**。
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
from dataclasses import dataclass
from typing import Any

from ..core.env_compat import secret_env

log = logging.getLogger(__name__)

#: 凭据文件路径（``OSS_CREDENTIALS_PATH`` 指路；缺省为 Hermes 状态目录）。
#: Hermes 规范下凭据优先走 ``.env``（``OSS_ACCESS_KEY_ID`` 等），文件仅作兼容回落。
OSS_CREDENTIALS_ENV = "OSS_CREDENTIALS_PATH"

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

#: 媒体类型目录名（TS ``buildOssKey(ctx.accountId, "images"|"videos"|"voices"|"files", …)``）
KIND_IMAGES = "images"
KIND_VIDEOS = "videos"
KIND_VOICES = "voices"
KIND_FILES = "files"

_HEX_RE = re.compile(r"^[a-f0-9]{8,64}$", re.IGNORECASE)
_EXT_RE = re.compile(r"\.([a-zA-Z0-9]+)$")
#: ⚠️ **必须带 ``re.ASCII``**：JS 的 ``\w`` 是 ASCII 的 ``[A-Za-z0-9_]``，而 **Python 的 ``\w``
#: 是 Unicode 的** —— 不加这个 flag，``我的 文件.pdf`` 会原样进 OSS key（只有空格被换掉），
#: 与 TS 产出的 key **不是同一个字符串**：中文键在 URL 里要百分号编码，而"两套实现对不上"
#: 正是这一整轮要消灭的东西。实测抓到过（测试名里就写着这个）。
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-]", re.ASCII)

#: ``路径 -> (过期时刻, 配置)``。**只放读成功且字段齐全的**（见 INVARIANT 第 2 条）。
_CACHE: dict[str, tuple[float, "OssConfig"]] = {}

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


def oss_credentials_path() -> str:
    """凭据文件路径：``OSS_CREDENTIALS_PATH`` 优先，否则 Hermes 状态目录下的兼容文件。

    ⚠️ 必须走 :func:`..core.env_compat.secret_env`，不能直接 ``os.environ.get``：
    多 profile 网关下进程 env 是 **default profile 的**，本 profile 的 ``.env``
    在 Hermes 的 secret scope 里。直接读 env 的现场表现是"凭据明明配了却报没有"
    （2026-10-18 实测：媒体上传日志 ``没有 OSS 凭据（文件不存在或字段不全）``）。
    """
    explicit = secret_env(OSS_CREDENTIALS_ENV).strip()
    if explicit:
        return explicit
    return os.path.join(os.path.expanduser("~"), ".hermes", "state", "credentials",
                        "oss-credentials.json")


def _oss_config_from_env() -> dict[str, Any] | None:
    """从 profile ``.env`` 读 OSS 配置（Hermes 规范的凭据路径）。三项缺一即返 None。

    四个值都按 profile 作用域读（见 :func:`oss_credentials_path` 的说明）。桶名和
    endpoint 虽不是密钥，但它们同样是**本 profile 的**配置 —— 用进程 env 会拿到
    默认 profile 的值（或拿不到）。
    """
    ak = secret_env("OSS_ACCESS_KEY_ID").strip()
    sk = secret_env("OSS_ACCESS_KEY_SECRET").strip()
    bucket = secret_env("OSS_BUCKET").strip()
    if not (ak and sk and bucket):
        return None
    return {
        "accessKeyId": ak,
        "accessKeySecret": sk,
        "bucket": bucket,
        "endpoint": secret_env("OSS_ENDPOINT").strip(),
    }


def clear_oss_config_cache() -> None:
    """清缓存（测试注入 / 凭据轮换后强制重读）。"""
    _CACHE.clear()


def load_oss_config(*, path: str | None = None, now: float | None = None) -> OssConfig | None:
    """读 OSS 凭据。**任何失败都返 None**（缺文件 / 坏 JSON / 字段缺），只有齐全才进缓存。

    ``endpoint`` 缺省用 :data:`DEFAULT_ENDPOINT`（TS 同款 ``d.endpoint || …``）；
    其余三个字段缺**任意一个**就整份作废 —— 半份凭据传上去只会得到 403，
    不如当场判"没有凭据"。
    """
    raw: Any = None
    if path is None:
        raw = _oss_config_from_env()
        p = "<env>" if raw is not None else oss_credentials_path()
    else:
        p = path
    t = time.monotonic() if now is None else now
    hit = _CACHE.get(p)
    if hit and hit[0] > t:
        return hit[1]
    if raw is None:
        try:
            with open(p, encoding="utf-8") as fh:
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
    if len(_CACHE) >= CRED_CACHE_MAX and p not in _CACHE:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[p] = (t + CRED_TTL_S, cfg)
    return cfg


def build_oss_key(account_id: str, kind: str, filename: str, *, today: str | None = None) -> str:
    """``wpp/{account}/{type}/{YYYY-MM-DD}/{filename}``（老板 2026-08-11 拍的 OSS 结构）。

    ⚠️ 日期用 **UTC**（TS ``new Date().toISOString().slice(0,10)`` 就是 UTC）——
    本机是 +08 ⇒ 北京时间 08:00 前的文件会归到前一天。这是**照抄**不是笔误：
    改成本地日期会让两套实现产生两套目录，检索/清理脚本对不上。
    """
    date = today or time.strftime("%Y-%m-%d", time.gmtime())
    return f"wpp/{account_id or 'default'}/{kind}/{date}/{filename}"


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
    except asyncio.TimeoutError:
        # TS 的 execAsync 超时会 kill 子进程；不 kill 会留一个僵尸 ossutil 一直挂着
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover —— 已经自己退了
            pass
        await proc.wait()
        return -1, f"超时 {timeout_s:.0f}s"
    return proc.returncode or 0, _scrub((err or b"").decode("utf-8", "replace")
                                        + (out or b"").decode("utf-8", "replace"))[:200]


async def upload_to_oss(oss: OssConfig, local_path: str, oss_key: str) -> str:
    """``ossutil cp`` 本地文件 → OSS，返回公网 URL。**3 次尝试后仍失败就抛**（调用方降级）。

    退避是 ``1s * attempt``（TS ``1000 * attempt``）—— OSS 的临时网络抖动能靠它过去。
    """
    binary = shutil.which("ossutil")
    if not binary:
        # 有意偏离 TS（见模块 docstring）：缺二进制别空转 3 次，报一句人能看懂的
        raise RuntimeError(
            "这台机器没有 ossutil（媒体不会被上传；这是降级不是故障）"
            f"：{OSS_CREDENTIALS_ENV}={oss_credentials_path()}")
    last = ""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        code, tail = await _run_ossutil(
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
                            kind: str | None = None) -> str:
    """字节 → 临时文件 → ``ossutil`` → 公网 URL。缺凭据抛错（调用方降级）。**临时文件必删。**

    ``account_id`` 与 ``kind`` **都给**了才用新结构 ``wpp/{account}/{kind}/{date}/…``；
    否则退回老 ``{prefix}/{hash}-{name}``（TS 对 MCP 那条老调用路径就是这么兼容的）。
    """
    oss = load_oss_config()
    if oss is None:
        raise RuntimeError("没有 OSS 凭据（文件不存在或字段不全）")
    m = _EXT_RE.search(filename)
    ext = m.group(1) if m else "bin"
    tmp_path = os.path.join(tempfile.gettempdir(), f"wpp-upload-{secrets.token_hex(6)}.{ext}")
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(buf)
        digest = hashlib.md5(filename.encode("utf-8")).hexdigest()[:12]
        safe_name = _UNSAFE_NAME_RE.sub("_", filename)
        if account_id and kind:
            oss_key = build_oss_key(account_id, kind, f"{digest}-{safe_name}")
        else:
            oss_key = f"{prefix}/{digest}-{safe_name}"
        return await upload_to_oss(oss, tmp_path, oss_key)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def upload_media_bytes(buf: bytes, filename: str, *, account_id: str,
                             kind: str) -> str | None:
    """媒体富化的**唯一出口**：上传成功返 URL，任何失败返 ``None`` 且只 warn（INVARIANT 第 3 条）。

    调用方拿到 ``None`` 就"这条消息不带 URL"，不算错误 —— TS 六个 enrich 入口全这个形状。
    """
    if not buf:
        return None
    try:
        url = await oss_upload_buffer(buf, filename, "wpp", account_id=account_id, kind=kind)
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
    "CRED_TTL_S", "DEFAULT_ENDPOINT", "KIND_FILES", "KIND_IMAGES", "KIND_VIDEOS",
    "KIND_VOICES", "OSS_CREDENTIALS_ENV", "OssConfig", "UPLOAD_ATTEMPTS", "UPLOAD_TIMEOUT_S",
    "build_oss_key", "clear_oss_config_cache", "load_oss_config", "oss_credentials_path",
    "oss_upload_buffer", "public_url", "sanitize_filename_part", "upload_media_bytes",
    "upload_to_oss",
]
