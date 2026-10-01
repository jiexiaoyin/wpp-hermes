"""WPP 配对码（迁移自 wpp-openclaw pairing-store.ts）。

白名单外用户私聊机器人发 /pair <8位码> 自助加入白名单。
配对码 1 小时有效，按账号隔离。

从旧扁平 ``plugin/pairing.py`` **原样迁入**，只有 ``PLUGIN_DIR`` 多退一级
（文件从 ``plugin/`` 挪到了 ``plugin/platform/``），以便 ``PAIRING_DIR``
**仍然落在 ``plugin/pairing/``** —— 部署机上的既有配对码文件原地继续有效，
不需要迁移。要换位置请设 ``WPP_PAIRING_DIR``。

## ⚠️ 现状：只有「生成 / 查看」是活的，``/pair`` 兑换是死码

旧树的 ``plugin/adapter.py`` **从未引用过** ``redeem_pairing_code`` ——
整条兑换链路（``extract_pair_code`` → ``redeem_pairing_code``）在真实运行路径上
不可达。所以 ``redeem_pairing_code`` 的注释「实际加入白名单由 commands 处理」
是一句**从未兑现的承诺**：它校验完码就返回，谁也没被写进白名单。

本树**保留这组函数**（配对码文件格式是对外契约，删了会让已发的码失效），
但**没有**把 ``/pair`` 焊进 adapter 的 filehelper 钩子 —— 见
``platform/adapter.py:_maybe_command`` 的说明。真要做「自助加白名单」，
必须先想清楚**改的是哪个账号的哪份白名单**（覆盖是内存态、重连即回退到
``accounts/*.json``，用它加白名单等于假的"永久加入"）。那是独立议题，不在本次搬迁范围。
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


def extract_pair_code(content: str) -> str | None:
    """从 /pair <code> 提取配对码。"""
    c = str(content or "").strip()
    if not c.startswith("/pair"):
        return None
    parts = c.split()
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
    """兑换配对码：**只校验，不落任何白名单**（见模块 docstring 的说明）。"""
    entry = read_pairing_code(account_id)
    if not entry:
        return False, "当前无有效配对码，请联系管理员生成"
    if normalize_pair_code(entry["code"]) != normalize_pair_code(code):
        return False, "配对码错误"
    # 配对码正确 → 提示成功（实际加入白名单由 commands 处理，需知道 wxid）
    return True, "配对码有效"
