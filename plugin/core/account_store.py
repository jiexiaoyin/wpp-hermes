"""账号文件写回 —— filehelper 命令的**唯一落盘口**（W6，2026-10-02）。

## 为什么需要它

旧实现把命令写进 ``core/overrides.py`` 的**内存**覆盖，回复「已开启 ✅」，而
``Account`` 是 frozen 的、``adapter.connect()`` 只解析一次并缓存 ⇒ 当次**不生效**，
进程一重启就回退到 ``accounts/*.json``。于是 ``/user`` ``/group`` ``/heartflow``
``/affection`` ``/jargon`` 六条命令全是「会说谎的开关」—— 比没有这个命令更坏，
因为它让运维以为改完了。

本模块把那条路径换成**写回账号文件 + 就地重载**（见 ``adapter.reload_account``）。

## 四条硬约束（每一条都对应一种真实的失效）

1. **绝不从 ``Account`` dataclass 反序列化回去**：``Account`` 只建模了它认识的键，
   同级还有 ``groupContextEnabled`` / ``llmIntentModel`` / 各种阈值等**本树不建模**的
   键 —— 从 dataclass 拼一份写回去 = 静默删配置。做法永远是
   「读**原始 dict** → 改**一个**键 → 写回」。
2. **凭证字段一个都不许写**（:data:`FORBIDDEN_KEYS`）：``authcode`` / ``tokenKey`` /
   ``webhookSecret`` / ``webhookPathToken`` 只从 env 或厂商面板来。命令面能改它们
   等于给"把生产凭据改坏"开了一条路，且改坏之后**收信直接停**。
3. **读不出来就不写**（:func:`_read_strict`）：文件缺失/JSON 损坏/根不是对象 ⇒
   **raise**。半份配置写回去 = 平台再也起不来，而"命令回了个 ✅"会让人以为没事。
4. **写前必须备份、写时原子替换、写后 600**：
   - 备份（:func:`backup_dir`，沿用既有 ``<name>.pre-<tag>-<epoch>`` 命名）——
     写错了能拿回来；
   - ``临时文件 + os.replace`` + fsync —— 断电/被杀不会留一个截断的 JSON；
   - ``0o600`` —— 这个文件里有 authcode。实测 ``.183`` 上那份是 ``644``（同机任何
     用户可读），本模块顺带把它收紧。

## 落盘之后**还要**清掉同键的内存覆盖

``core.accounts.resolve()`` 的优先级是 ①账号文件 → ②yaml → ③**内存覆盖**。
所以"只写文件"是不够的：**同一把键上残留的覆盖会把刚写的新值影子掉**，
链路读到的还是旧值 —— 这正是本项要消灭的那类失败。⇒ 写回与清覆盖是**同一个动作**
（:func:`write_account_key` 内完成），调用方拿不到"只做一半"的入口。

⚠️ 落盘失败**必须让命令看得见**（抛 :class:`AccountWriteError`，由命令层回一句
「❌ 写入失败」）—— 与入站落库那条"失败只 warn"**刚好相反**：那条的失败不能影响收信，
这条的失败若不吭声，运维就会以为开关生效了。

INVARIANT: W6 —— 命令面改账号配置只有本模块一条路：读原始 dict、只改一个键、写前备份、
原子替换、600，并且**同键内存覆盖一起清**（否则新值被影子掉）。凭证键在本模块
**结构性不可写**（:data:`FORBIDDEN_KEYS`），不靠调用方自觉。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 凭证键 —— 写回路径**一律拒绝**（见模块 docstring 约束 2）。
#: 含 ``*Env`` 间接键：那种键写进去等于让运维以为"凭据配好了"，而值只是个变量名。
FORBIDDEN_KEYS = frozenset({
    "authcode", "authcodeEnv", "tokenKey", "tokenKeyEnv",
    "webhookSecret", "webhookSecretEnv", "webhookPathToken",
})

#: 命令写回是**低频**动作（人手工敲一条命令），但可能撞上 sweep / 工具线程 ⇒ 加锁。
#: 进程内锁足够：同一份账号文件只有一个网关进程在写。
_LOCK = threading.Lock()

#: 备份目录的 env 覆盖（测试用；运维也可以指到别处）。
BACKUP_DIR_ENV = "WECHATPRO_ACCOUNT_BACKUP_DIR"

#: 写回动作的标签 —— 进备份文件名（``<name>.pre-writeback-<epoch>``）
_BACKUP_TAG = "writeback"


class AccountWriteError(RuntimeError):
    """写回失败 —— **必须**让调用方看见（与"落库失败只 warn"是两个方向的取舍）。"""


# ==================================================================== 路径

def account_path(account_id: str) -> Path:
    """``accounts/<id>.json`` 的绝对路径。

    ⚠️ 账号 id 直接进文件名 ⇒ 挡住路径穿越（``../`` / ``/``）。命令面拿得到
    ``account_id`` 的地方都是配置里来的，但这是写文件的一条路，不加白不加。
    """
    aid = str(account_id or "").strip()
    if not aid or "/" in aid or "\\" in aid or aid in (".", ".."):
        raise AccountWriteError(f"账号 id {account_id!r} 不能用作文件名")
    from . import accounts as _accounts

    return _accounts.accounts_dir() / f"{aid}.json"


def backup_dir() -> Path:
    """备份目录：``WECHATPRO_ACCOUNT_BACKUP_DIR`` → ``/data``（老板定的备份根）→
    ``~/.hermes/backups``（宿主上没有 ``/data`` 时）。

    为什么要有兜底：``/data`` 是**宿主机**的约定（生产机上就是挂在那儿），
    而本插件的 dev 树跑在任何装得下 Hermes 的机器上。兜底路径与
    ``.183`` 上既有的 ``~/.hermes/backups/``（``.env.pre-*`` / ``default.json.pre-*``
    都躺在那儿）是同一处，运维不用记第二个地方。
    """
    override = str(os.environ.get(BACKUP_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    data = Path("/data")
    if data.is_dir():
        return data / "wpp-account-backups"
    return Path.home() / ".hermes" / "backups"


# ================================================================ 读（严格）

def read_account_json(account_id: str) -> dict[str, Any]:
    """读**原始**账号 dict。任何读不出来的情形都 **raise**（见 docstring 约束 3）。"""
    path = account_path(account_id)
    if not path.is_file():
        raise AccountWriteError(f"账号文件不存在：{path}（写回会凭空造一份残缺配置）")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AccountWriteError(f"账号文件读不了：{path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # ⚠️ 只报**位置**，不报原文 —— 这份文件里有 authcode，异常文本会进日志。
        raise AccountWriteError(f"账号文件不是合法 JSON：{path}（行 {exc.lineno} 列 {exc.colno}）") from exc
    if not isinstance(data, dict):
        raise AccountWriteError(f"账号文件根不是对象：{path}（{type(data).__name__}）")
    return data


# ================================================================ 写（原子）

def write_account_key(account_id: str, key: str, value: Any) -> dict[str, Any]:
    """把 ``key`` 写进账号文件并**立刻清掉同键的内存覆盖**。返回写入后的 dict。

    这是命令面唯一该用的入口 —— 文件与覆盖必须一起动（见模块 docstring）。
    失败抛 :class:`AccountWriteError`（**不吞**）。
    """
    k = str(key or "").strip()
    if not k:
        raise AccountWriteError("要写的键名是空的")
    if k in FORBIDDEN_KEYS:
        raise AccountWriteError(
            f"拒绝写凭证键 {k!r}：它只从 env（<键>Env / WECHATPRO_*）或厂商面板来，"
            f"命令面改它 = 改坏就收不到信")

    with _LOCK:
        path = account_path(account_id)
        data = read_account_json(account_id)          # 先读（读不到就不会写）
        backup = _backup(path)                        # 再备份（备不了就不写）
        data[k] = value
        _atomic_write(path, data)
        _clear_shadow_override(account_id, k)
        log.info("[WPP:%s] 账号配置已写回：%s（备份 %s）", account_id, k, backup)
        return dict(data)


def _clear_shadow_override(account_id: str, key: str) -> None:
    """清掉同键的内存覆盖 —— 否则 ``resolve()`` 的 ③ 层会把刚写的值盖掉。"""
    from . import overrides  # 同层模块，无环

    if overrides.get_override(account_id).pop(key, None) is not None:
        log.info("[WPP:%s] 清掉键 %s 的内存覆盖（它会把刚写回的值影子掉）", account_id, key)


def _backup(path: Path) -> Path:
    """把原文件拷一份到 :func:`backup_dir`。**备不了就 raise**（没备份不写）。"""
    d = backup_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)          # 备份里也有 authcode，目录不对谁都能翻
    except OSError as exc:
        raise AccountWriteError(f"备份目录建不了：{d}: {exc}") from exc

    stamp = int(time.time())
    dst = d / f"{path.name}.pre-{_BACKUP_TAG}-{stamp}"
    seq = 1
    while dst.exists():             # 同一秒内连写两次别互相覆盖
        dst = d / f"{path.name}.pre-{_BACKUP_TAG}-{stamp}-{seq}"
        seq += 1
    try:
        shutil.copyfile(path, dst)
        os.chmod(dst, 0o600)
    except OSError as exc:
        raise AccountWriteError(f"备份失败（未做任何改动）：{dst}: {exc}") from exc
    return dst


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """原子替换：同目录临时文件（600）→ fsync → ``os.replace`` → fsync 目录。

    ⚠️ 临时文件**必须同目录**：``os.replace`` 跨文件系统不是原子的。
    ⚠️ 临时文件名带 pid：同机多进程（不该有，但）不互相踩。
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            # fdopen 成功时 fd 归文件对象管；失败路径里它可能还没被接管 ⇒ 忽略双关
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise AccountWriteError(f"写账号文件失败：{path}: {exc}") from exc

    # 目录 fsync：``os.replace`` 的元数据落盘，断电后不会"文件没了也没备份"
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:  # noqa: PERF203 —— 拿不到目录 fd（某些文件系统）不该让写回算失败
        log.debug("[WPP] 账号目录 fsync 跳过（%s）: %s", path.parent, exc)
