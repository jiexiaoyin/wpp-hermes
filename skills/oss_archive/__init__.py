"""OSS Archive skill — 通用阿里云 OSS 上传.

2026-09-01 接总立拍板 (Phase 2.1 wpp-wechat 重构):
  从 wechatpadpro plugin 抽出 media_oss.upload_media_to_oss 到独立 skill.
  任何 channel / agent 都可调用, 不再依赖 wechatpadro plugin.

依赖: ossutil 命令行.
凭证: env 优先 (OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET / OSS_ENDPOINT),
     fallback ~/.openclaw/credentials/oss-credentials.json.

2026-09-01 接老板安全拍板 (Phase 2.4 安全加固):
  加 ALLOWED_ACCOUNTS 白名单机制. plugin 启动时调用 register_accounts() 注册合法账号,
  upload_media_to_oss() 会校验 account_id 是否在白名单内.
  目的: 即使 skill 被别的 agent 误用/越权调用, 未注册的 account_id 会被拒绝上传.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Set

log = logging.getLogger(__name__)

DEFAULT_BUCKET = "openclaw-a"
DEFAULT_ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"
UPLOAD_TIMEOUT_S = 60
MAX_RETRIES = 3

# ---- 2026-09-01 安全: account_id 白名单 (空集合 = 拒绝任何 account_id) ----
ALLOWED_ACCOUNTS: Set[str] = set()


class AccountNotAllowedError(PermissionError):
    """account_id 不在 ALLOWED_ACCOUNTS 白名单内."""
    pass


def register_accounts(accounts) -> None:
    """plugin 启动时调用, 注册允许使用 OSS 的 account_id 集合.

    Args:
        accounts: 可迭代对象, 元素是 account_id 字符串 (e.g. {"default", "boss2"})
    """
    global ALLOWED_ACCOUNTS
    ALLOWED_ACCOUNTS = set(accounts or [])
    log.info("[oss-archive] 注册 account_id 白名单: %s", sorted(ALLOWED_ACCOUNTS))


def _check_account(account_id: str) -> None:
    """校验 account_id 是否在白名单内. 不在则抛 AccountNotAllowedError."""
    if account_id not in ALLOWED_ACCOUNTS:
        raise AccountNotAllowedError(
            f"account_id '{account_id}' 未在 ALLOWED_ACCOUNTS 白名单内 ({sorted(ALLOWED_ACCOUNTS) or '空'}). "
            f"plugin 启动时必须 register_accounts() 注册. 当前调用被拒绝."
        )


def _load_oss_config() -> Optional[dict]:
    """读 OSS 凭据 (env 优先, fallback credentials 文件)."""
    ak = os.environ.get("OSS_ACCESS_KEY_ID") or os.environ.get("WECHATPRO_S3_ACCESS_KEY") or ""
    sk = os.environ.get("OSS_ACCESS_KEY_SECRET") or os.environ.get("WECHATPRO_S3_SECRET_KEY") or ""
    bucket = os.environ.get("OSS_BUCKET", DEFAULT_BUCKET)
    endpoint = os.environ.get("OSS_ENDPOINT", DEFAULT_ENDPOINT)
    if ak and sk:
        return {"accessKeyId": ak, "accessKeySecret": sk, "bucket": bucket, "endpoint": endpoint}

    paths = [
        os.environ.get("OSS_CREDENTIALS_PATH", ""),
        str(Path.home() / ".openclaw" / "credentials" / "oss-credentials.json"),
    ]
    for p in paths:
        if not p:
            continue
        try:
            d = json.loads(Path(p).read_text("utf-8"))
            if d.get("accessKeyId") and d.get("accessKeySecret"):
                return {
                    "accessKeyId": d["accessKeyId"],
                    "accessKeySecret": d["accessKeySecret"],
                    "bucket": d.get("bucket", DEFAULT_BUCKET),
                    "endpoint": d.get("endpoint", DEFAULT_ENDPOINT),
                }
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return None


def build_key(prefix: str, filename: str, media_type: Optional[str] = None,
              date: Optional[str] = None) -> str:
    """生成 OSS key.

    格式: {prefix}/{media_type}/{date}/{filename} (兼容老 wpp/{account}/{type}/{date}/...).
    prefix: 'wpp/{account_id}' 或其他自定义路径.
    media_type: 复数形式 (images/videos/voices/files), 留空则不加.
    """
    if date is None:
        date = time.strftime("%Y-%m-%d")
    if media_type:
        if not media_type.endswith("s"):
            media_type += "s"
        return f"{prefix}/{media_type}/{date}/{filename}"
    return f"{prefix}/{date}/{filename}"


def upload_bytes(data: bytes, filename: str, prefix: str = "wpp/default",
                 media_type: Optional[str] = None) -> Optional[str]:
    """上传 bytes 到 OSS, 返回公网 URL 或 None (失败降级).

    Args:
        data: 原始字节
        filename: 文件名 (含扩展名)
        prefix: 路径前缀 (e.g. 'wpp/{account_id}', '朋友圈', '客户头像')
        media_type: 复数形式 (images/videos/voices/files), 留空用默认 (按 prefix 推)

    Returns:
        HTTPS URL 或 None
    """
    try:
        oss = _load_oss_config()
        if not oss:
            log.warning("[oss-archive] 凭据缺失, 跳过上传")
            return None
        oss_key = build_key(prefix, filename, media_type=media_type)

        tmp_path = os.path.join(tempfile.gettempdir(), f"oss_archive_{int(time.time())}_{hash(data) % 100000}")
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    result = subprocess.run(
                        ["ossutil", "cp", tmp_path, f"oss://{oss['bucket']}/{oss_key}",
                         "--endpoint", oss["endpoint"], "-f"],
                        capture_output=True, text=True, timeout=UPLOAD_TIMEOUT_S,
                    )
                    if result.returncode == 0:
                        url = f"https://{oss['bucket']}.{oss['endpoint']}/{oss_key}"
                        log.info("[oss-archive] 上传 OK: %s (%d bytes)", url, len(data))
                        return url
                    log.warning("[oss-archive] attempt %d/%d failed: %s",
                                attempt, MAX_RETRIES, result.stderr[:120])
                except subprocess.TimeoutExpired:
                    log.warning("[oss-archive] attempt %d/%d timeout", attempt, MAX_RETRIES)
                except Exception as e:  # noqa: BLE001
                    log.warning("[oss-archive] attempt %d/%d error: %s", attempt, MAX_RETRIES, e)
            return None
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("[oss-archive] 上传失败: %s", e)
        return None


def upload_file(local_path: str, prefix: str = "wpp/default",
                media_type: Optional[str] = None) -> Optional[str]:
    """上传本地文件到 OSS. 失败降级返回 None."""
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        filename = os.path.basename(local_path)
        return upload_bytes(data, filename, prefix=prefix, media_type=media_type)
    except OSError as e:  # noqa: BLE001
        log.warning("[oss-archive] 读取文件失败 %s: %s", local_path, e)
        return None


# ---------- 向后兼容: 保留 wechatpadro plugin 的旧 API ----------
def upload_media_to_oss(data: bytes, media_type: str, ext: str,
                        account_id: str = "default") -> Optional[str]:
    """兼容旧 API: 上传微信媒体到 OSS.

    格式: wpp/{account}/{type}s/{date}/{md5}.{ext}

    2026-09-01 安全加固: 校验 account_id 是否在 ALLOWED_ACCOUNTS 白名单内.
    不在则抛 AccountNotAllowedError. 调用方 (plugin) 应 try/except 降级到本地.
    """
    _check_account(account_id)
    md5 = hashlib.md5(data).hexdigest()
    return upload_bytes(
        data=data,
        filename=f"{md5}.{ext}",
        prefix=f"wpp/{account_id or 'default'}",
        media_type=media_type,
    )


def build_oss_key(account_id: str, media_type: str, filename: str) -> str:
    """兼容旧 API."""
    return build_key(f"wpp/{account_id or 'default'}", filename, media_type=media_type)