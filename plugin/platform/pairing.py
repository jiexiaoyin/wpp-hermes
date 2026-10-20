"""WPP 配对码（迁移自 wpp-openclaw pairing-store.ts）。

白名单外用户私聊机器人发 /pair <8位码> 自助加入白名单。
配对码 1 小时有效，按账号隔离。

从旧扁平 ``plugin/pairing.py`` **原样迁入**，只有 ``PLUGIN_DIR`` 多退一级
（文件从 ``plugin/`` 挪到了 ``plugin/platform/``），以便 ``PAIRING_DIR``
**仍然落在 ``plugin/pairing/``** —— 部署机上的既有配对码文件原地继续有效，
不需要迁移。要换位置请设 ``WPP_PAIRING_DIR``。

## 现状（2026-10-20）：``/pair`` 兑换已接通

旧树里 ``redeem_pairing_code`` 从未被运行路径引用，是一段死码。现在
``adapter._maybe_pair`` 在私聊里接住 ``/pair <code>``：

1. 校验配对码（1 小时有效，按账号隔离）；
2. 校验通过 → :func:`add_paired` 写入 ``<plugin>/pairing/<account>.paired.json``；
3. ``dmPolicy=pairing`` 的准入只放行这份名单里的 wxid。

名单**不写回** ``accounts/*.json``（运行期改账号文件会触发 fs.watch 重载），
所以它是插件目录下的持久文件，重启后仍在。删除该文件即可清空配对。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)

PAIRING_CODE_TTL_MS = 60 * 60 * 1000  # 1 小时
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符
PAIRING_CODE_LENGTH = 8

#: ``plugin/``（本文件在 ``plugin/platform/``，所以多退一级）。
PLUGIN_DIR = Path(__file__).resolve().parent.parent
PAIRING_DIR = Path(os.environ.get("WPP_PAIRING_DIR", str(PLUGIN_DIR / "pairing")))


def get_pairing_path(account_id: str) -> Path:
    return PAIRING_DIR / f"{account_id}.json"


def random_code(length: int = PAIRING_CODE_LENGTH) -> str:
    return "".join(random.choice(CODE_ALPHABET) for _ in range(length))


def normalize_pair_code(code: str) -> str:
    return str(code or "").strip().upper()


def is_pair_command(content: str) -> bool:
    """是否 ``/pair`` 命令（精确匹配命令词，别把 ``/pairs`` 也吃进来）。"""
    parts = str(content or "").strip().split()
    return bool(parts) and parts[0].lower() == "/pair"


def extract_pair_code(content: str) -> str | None:
    """从 /pair <code> 提取配对码。"""
    if not is_pair_command(content):
        return None
    parts = str(content or "").strip().split()
    if len(parts) >= 2:
        return normalize_pair_code(parts[1])
    return None


def generate_pairing_code(account_id: str) -> dict:
    """生成配对码（1 小时有效）。"""
    code = random_code()
    entry = {
        "code": code,
        "account_id": account_id,
        "expires_at": int(time.time() * 1000) + PAIRING_CODE_TTL_MS,
    }
    PAIRING_DIR.mkdir(parents=True, exist_ok=True)
    get_pairing_path(account_id).write_text(json.dumps(entry, ensure_ascii=False, indent=1), "utf-8")
    log.info("[WPP] 配对码已生成: %s (account=%s)", code, account_id)
    return entry


def read_pairing_code(account_id: str) -> dict | None:
    """读取当前配对码（未过期）。"""
    path = get_pairing_path(account_id)
    try:
        entry = json.loads(path.read_text("utf-8"))
        if entry.get("expires_at", 0) > int(time.time() * 1000):
            return entry
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def redeem_pairing_code(account_id: str, code: str) -> tuple[bool, str]:
    """校验配对码；**不改任何名单**。

    兑换后的落盘由调用方完成（``adapter._maybe_pair`` → :func:`add_paired`）——
    这里不接收 wxid，避免"校验"与"写入"混在一个函数里。
    """
    entry = read_pairing_code(account_id)
    if not entry:
        return False, "当前无有效配对码，请联系管理员生成"
    if normalize_pair_code(entry["code"]) != normalize_pair_code(code):
        return False, "配对码错误"
    return True, "配对码有效"


# ---------------------------------------------------------------- 已配对名单
#
# ``dmPolicy=pairing`` 的准入名单。单独一个 JSON（不是 accounts/*.json）：
# 运行期改账号文件会触发 fs.watch 重载，配对是高频动作，不该抖整个账号。
# 文件形如 ``{"users": ["wxid_..."]}``，权限 0600。

def get_paired_path(account_id: str) -> Path:
    return PAIRING_DIR / f"{account_id}.paired.json"


def load_paired(account_id: str) -> list[str]:
    """已配对 wxid 列表（文件不存在/坏 JSON ⇒ 空列表，不抛）。"""
    try:
        raw = json.loads(get_paired_path(account_id).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    users = raw.get("users") if isinstance(raw, dict) else raw
    if not isinstance(users, list):
        return []
    return [str(u) for u in users if isinstance(u, str) and u.strip()]


def add_paired(account_id: str, wxid: str) -> bool:
    """把 wxid 写进已配对名单（幂等）。返回是否新增。"""
    wxid = str(wxid or "").strip()
    if not wxid:
        return False
    users = load_paired(account_id)
    if wxid in users:
        return False
    users.append(wxid)
    PAIRING_DIR.mkdir(parents=True, exist_ok=True)
    path = get_paired_path(account_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=1), "utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)
    log.info("[WPP] 配对成功: account=%s wxid=%s", account_id, wxid)
    return True


def is_paired(account_id: str, wxid: str) -> bool:
    return str(wxid or "") in load_paired(account_id)
