"""OSS 上传（W7-1，移植 TS ``inbound/media-enrich/shared.ts`` 172 行）。

## 分层（扩展边界 P0-7，2026-10-19）

与 WPP 无关的机制（凭据解析与缓存、文件名净化、``ossutil`` 重试、临时文件、
日志脱敏）在 :mod:`plugin.neutral.media_oss`；本模块是 **WPP 绑定层**：

* 凭据来源：Hermes profile secret scope（``secret_env``）+ ``OSS_CREDENTIALS_PATH``；
* OSS key 结构：``build_oss_key``（``wpp/{account}/{kind}/{date}/…``）；
* 公开 API 与拆分前一致（旧 import 路径继续可用）。

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
老板 2026-10-02 二次确认「先不放凭据」（``真机`` 当时是"整条降级路"），
2026-10-03 改判「放 OSS 凭据」⇒ ``真机`` 上那三样已**手工**落位（**代码未变**：
本模块至今仍然只读、不代拷）。

## ★ 真机上让上传真能成的**三样东西**（排障先看这里；2026-10-03 在 ``真机`` 落实）

缺**任何一样**，现场都只有一行 ``[OSS] 上传失败（本条消息不带 URL）``：

1. ``/usr/local/bin/ossutil`` —— 由 ``shutil.which`` 在**进程 PATH** 上找
   （``真机`` 的 systemd user 单元 PATH 已含 ``/usr/local/bin``；换机器要自己确认）；
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

import asyncio  # noqa: F401 —— 测试会 monkeypatch ``media_oss.asyncio.sleep``
import os
import shutil  # noqa: F401 —— 测试会 monkeypatch ``media_oss.shutil.which``
import time
from typing import Any

from ..core.env_compat import gate_env, secret_env
from ..neutral import media_oss as _neutral
from ..neutral.media_oss import (  # noqa: F401 —— 兼容旧 import
    _UNSAFE_NAME_RE,
    CRED_CACHE_MAX,
    CRED_TTL_S,
    DEFAULT_ENDPOINT,
    UPLOAD_ATTEMPTS,
    UPLOAD_TIMEOUT_S,
    OssConfig,
    _run_ossutil,
    public_url,
    sanitize_filename_part,
)

#: 凭据文件路径（``OSS_CREDENTIALS_PATH`` 指路；缺省为 Hermes 状态目录）。
#: Hermes 规范下凭据优先走 ``.env``（``OSS_ACCESS_KEY_ID`` 等），文件仅作兼容回落。
OSS_CREDENTIALS_ENV = "OSS_CREDENTIALS_PATH"

#: 媒体类型目录名（TS ``buildOssKey(ctx.accountId, "images"|"videos"|"voices"|"files", …)``）
KIND_IMAGES = "images"
KIND_VIDEOS = "videos"
KIND_VOICES = "voices"
KIND_FILES = "files"

#: 中立层的缓存与密钥集合（同一个对象；测试按旧路径 ``M._CACHE`` 检查条数上限）。
_CACHE = _neutral._CACHE
_SECRETS = _neutral._SECRETS

_safe_err = _neutral._safe_err


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
    _neutral.clear_oss_config_cache()


def oss_enabled() -> bool:
    """OSS 媒体转存开关（**默认关闭**，2026-10-20 老板要求）。

    ``WECHATPRO_OSS_ENABLED``：

    * 未设置 / ``0/false/off/no`` —— 关闭（即使凭据齐全也不上传）；
    * ``1/true/on/yes`` —— 开启（仍需凭据齐全）。

    旧行为是"有凭据就自动开启"，那让 OSS 变成事实上的默认能力。现在必须显式开启：
    既有部署升级时要在 ``.env`` 补 ``WECHATPRO_OSS_ENABLED=1``（向导会写）。
    """
    raw = str(gate_env("WECHATPRO_OSS_ENABLED", "") or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


def load_oss_config(*, path: str | None = None, now: float | None = None) -> OssConfig | None:
    """读 OSS 凭据。**任何失败都返 None**（缺文件 / 坏 JSON / 字段缺），只有齐全才进缓存。

    凭据来源的**解析**在本模块（profile secret scope → 兼容文件），缓存与校验在
    :func:`plugin.neutral.media_oss.load_oss_config`。
    """
    if not oss_enabled():
        return None
    if path is None:
        raw = _oss_config_from_env()
        if raw is not None:
            return _neutral.load_oss_config("<env>", now=now, raw=raw)
        path = oss_credentials_path()
    return _neutral.load_oss_config(path, now=now)


def build_oss_key(account_id: str, kind: str, filename: str, *, today: str | None = None) -> str:
    """``wpp/{account}/{type}/{YYYY-MM-DD}/{filename}``（老板 2026-08-11 拍的 OSS 结构）。

    ⚠️ 日期用 **UTC**（TS ``new Date().toISOString().slice(0,10)`` 就是 UTC）——
    本机是 +08 ⇒ 北京时间 08:00 前的文件会归到前一天。这是**照抄**不是笔误：
    改成本地日期会让两套实现产生两套目录，检索/清理脚本对不上。
    """
    date = today or time.strftime("%Y-%m-%d", time.gmtime())
    return f"wpp/{account_id or 'default'}/{kind}/{date}/{filename}"


async def upload_to_oss(oss: OssConfig, local_path: str, oss_key: str) -> str:
    """``ossutil cp`` 本地文件 → OSS，返回公网 URL。**3 次尝试后仍失败就抛**（调用方降级）。

    机制（重试/脱敏/超时）在 :mod:`plugin.neutral.media_oss`；这里注入
    ``_run_ossutil`` —— 测试按旧路径 monkeypatch 它时必须生效。
    """
    return await _neutral.upload_to_oss(oss, local_path, oss_key,
                                        run_ossutil=_run_ossutil)


async def oss_upload_buffer(buf: bytes, filename: str, prefix: str, *,
                            account_id: str | None = None,
                            kind: str | None = None) -> str:
    """字节 → 临时文件 → ``ossutil`` → 公网 URL。缺凭据抛错（调用方降级）。**临时文件必删。**

    ``account_id`` 与 ``kind`` **都给**了才用新结构 ``wpp/{account}/{kind}/{date}/…``；
    否则退回老 ``{prefix}/{hash}-{name}``（TS 对 MCP 那条老调用路径就是这么兼容的）。
    """
    return await _neutral.oss_upload_buffer(
        buf, filename, prefix, account_id=account_id, kind=kind,
        key_builder=build_oss_key, load_config=load_oss_config,
        uploader=upload_to_oss,
    )


async def upload_media_bytes(buf: bytes, filename: str, *, account_id: str,
                             kind: str) -> str | None:
    """媒体富化的**唯一出口**：上传成功返 URL，任何失败返 ``None`` 且只 warn（INVARIANT 第 3 条）。

    调用方拿到 ``None`` 就"这条消息不带 URL"，不算错误 —— TS 六个 enrich 入口全这个形状。
    """
    return await _neutral.upload_media_bytes(
        buf, filename, "wpp", account_id=account_id, kind=kind,
        key_builder=build_oss_key, load_config=load_oss_config,
        uploader=upload_to_oss,
    )


__all__ = [
    "CRED_TTL_S",
    "DEFAULT_ENDPOINT",
    "KIND_FILES",
    "KIND_IMAGES",
    "KIND_VIDEOS",
    "KIND_VOICES",
    "OSS_CREDENTIALS_ENV",
    "UPLOAD_ATTEMPTS",
    "UPLOAD_TIMEOUT_S",
    "OssConfig",
    "build_oss_key",
    "clear_oss_config_cache",
    "load_oss_config",
    "oss_credentials_path",
    "oss_upload_buffer",
    "public_url",
    "sanitize_filename_part",
    "upload_media_bytes",
    "upload_to_oss",
]
