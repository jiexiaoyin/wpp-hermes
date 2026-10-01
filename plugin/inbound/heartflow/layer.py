"""心流分层学习（**群 × 时段**）—— ``heartflow-layer.ts`` (v1.8.0) 的移植。

## 老板 2026-09-26 拍板的 P2 定义

「群×时段×话题，n≥k 才生效，不够回落画像先验（**不是地板**），shadow 先只记录」。
本批**只做群 × 时段** —— 话题层的样本在"每群每天 ~4 条 bot 发言"的量级下长期 <k，
而且会让 ``why`` 的归因链变成三层不可解释。``layer_kind`` 字段与 ``'topic'`` 槽位已预留。

## 三条不可动摇的设计（都是实测踩出来的，不是偏好）

1. **每轮全量重算，绝不做增量累加**（I3）。sweep 每 300s 一次；若把"新收敛的行"累加进桶，
   同一批行会被重复计入 ⇒ n 一小时虚涨 12 倍 ⇒ ``min_samples`` 门槛形同虚设，
   而且**永远无法自愈**。重算是幂等的：同样的输入永远得到同样的输出，改口径/改分段后
   几个 sweep 周期内自动收敛，不需要数据迁移。
2. **窗口上限 ``window_days``（默认 60）**。没有上限时桶内速率会被 60 天前的旧样本永久锁死，
   "晚段更受欢迎"这类结论再也不会随近况变化 —— 这是与"累加"并列的第二个不可逆点。
3. **不新增冷却**。分层每轮从同一批重算 ⇒ 天然不累积、跑不飞；判定直接复用调阈侧的
   **死区**当防抖，**不要写第二套参数**。

judge 热路径**零 DB 读**：段统计常驻内存（:data:`_layer_stats`），由 sweep 每轮重算后填充；
判定只看内存。

本模块同时托管「每群 × 每小时入站人类消息数」的**唯一取数入口**
:func:`load_hf_group_hour_counts`：分层的本底过滤与调阈侧的反事实基线用的是**同一条**聚合查询。
放在这里是因为 ``learn`` 反向依赖本模块（拿分层决策），若放 learn 里再被本模块 import 就成环。
缓存 120s ⇒ 同一轮 sweep 的两个 pass 共用一次 IO。

## 与 TS 的一处**故意**签名分歧

TS 的 ``maybeRecomputeHfLayerStats(accountId, cfg, nowSec)`` 在体内调 ``resolveHfLearning(cfg)``。
Py 侧把这两个学习参数（``label_window_sec`` / ``ambient_max``）改成**必填的关键字参数**：

* TS 的 ``heartflow-layer.ts`` 在**模块级** import 了 ``heartflow.js``（后者只以 ``import type``
  回指 layer）—— 这条"运行时单向"的边靠 TS 的类型擦除成立。Py 没有类型擦除这回事，而
  :mod:`.heartflow`（gate）要 import 本模块 ⇒ 照抄会成环。
* 更要紧的是**口径**：TS 注释写明「两个 pass 必须用**同一个** lookback 与同一个
  ``labelWindowSec``，否则同一条样本会在调阈侧'可采信'、在分层侧'不可采信'，
  ``n`` 就永远对不上」。做成必填参数，正是让"同一份 LRN"在调用点**显式**传进来 ——
  漏传会 ``TypeError``，而不是各解析一份、静默漂移（那正是 TS 注释警告的失效形态）。

调用点（``learn.run_sweep``）负责解析一次 ``LRN`` 并传给两侧。
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, NamedTuple, Sequence

from .budget import hf_local_hour
from .label import as_hf_engage_signal, hf_ambient_p, is_hf_sample_informative
from .runtime import CACHE_REGISTRY

log = logging.getLogger(__name__)

#: 本底基线回看天数（与调阈侧的反事实基线**同源**，全插件一个数）。
#:
#: 导出给 ``learn`` 用：两个 pass 必须用**同一个** lookback 与同一个 ``label_window_sec``，
#: 否则同一条样本会在调阈侧"可采信"、在分层侧"不可采信"（或被反过来），
#: ``/heartflow report`` 的 ``n`` 与 ``why`` 的 ``n`` 就永远对不上。
HF_AMBIENT_LOOKBACK_DAYS = 14

#: 段统计只分"日时段"一种（话题层预留未启用）。
HF_LAYER_KIND = "daypart"

#: 分段数上限（超过一律回落到默认四段，防止有人配出 24 段让表爆掉）。
HF_LAYER_MAX_BUCKETS = 6

#: 每群 × 每小时入站量查询的缓存时长（秒）：同一轮 sweep 的调阈 pass 与分层 pass 共用一次 IO。
HF_HOUR_COUNTS_TTL_SEC = 120

#: 落库行数上限（一次 sweep 最多写这么多行；与适配器的 LIMIT 钳制同档）。
#:
#: v1.9.0：导出给 ``/heartflow report`` 复用 —— 两处取同一批已收敛样本，上限必须一致，
#: 否则报告里的"接话率可用 N 条"与 sweep 实际拿去调阈的样本数会对不上。
HF_LAYER_DB_ROW_CAP = 5000

#: 默认档。``apply=False`` 是**刻意的**：分层要先当一段时间的影子，让老板从
#: ``/heartflow report`` 看清"若生效会怎么变"之后再手动开；``allowLoosen=False`` 同理
#: （放开"更主动"的权利只需要他一句话）。
HF_LAYERED_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "apply": False,
    "allowLoosen": False,
    "minSamples": 30,
    "windowDays": 60,
    "buckets": ((0, 7), (7, 12), (12, 18), (18, 24)),
}


@dataclass(frozen=True)
class HfLayeredConfig:
    """分层配置（已解析、已钳制）。"""

    #: 总开关（默认 true：照算照记；关掉则连统计都不跑）
    enabled: bool
    #: **是否让分层值真正生效**（默认 false = 影子：照算照记，只在 why/report 里显示"若生效会是多少"）
    apply: bool
    #: 是否允许分层**放宽**阈值（默认 false：只许收紧 = 更克制；历史教训见 heartflow v1.6.6）
    allow_loosen: bool
    #: 段内样本门槛（默认 30：低于此值一律回落群级 learned）
    min_samples: int
    #: 统计窗口天数（默认 60：防止旧样本永久锁死速率）
    window_days: int
    #: 分段定义（默认四段 ``(0,7) (7,12) (12,18) (18,24)``；非法输入整体回落默认）
    buckets: tuple[tuple[int, int], ...]


# ------------------------------------------------------------------ 分段（纯函数）


def _default_buckets() -> tuple[tuple[int, int], ...]:
    return tuple(HF_LAYERED_DEFAULTS["buckets"])


def _as_int_like(v: Any) -> int | None:
    """``Number.isInteger(Number(v))`` 的等价；非整数 / 非数 / ``None`` / bool ⇒ ``None``。

    与 TS 有三处**故意**分歧，三处都朝"更严"的方向（宁可回落默认，也不放行半套分段）：

    * ``Number(None)`` 在 JS 里是 ``0``（整数）、``Number(True)`` 是 ``1``、``Number("")`` 是 ``0``；
      Py 侧一律判非法。
    * 数字字符串（``"7"``）在 JS 里算整数 —— Py 侧**同样接受**（配置可能来自 YAML 的引号值）。

    配置源头是 JSON/YAML，上面那些形态只可能来自手写脏值；"猜他想说什么"不如"回落默认"安全。
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if math.isfinite(v) and v.is_integer() else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return int(f) if math.isfinite(f) and f.is_integer() else None
    return None


def _floor(v: Any, fallback: int) -> int:
    """``Math.floor``（非数 / NaN / ∞ / bool ⇒ ``fallback``）。

    TS 在脏值上会算出 ``NaN`` 并一路传染到 ``window_start``（``now - NaN * 86400``）⇒
    整轮聚合的窗口边界变成 NaN。Py 侧显式收敛成缺省 —— 这是"脏配置"与"静默 NaN"之间
    必须选一个的地方，选的是前者。
    """
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return fallback
    if not math.isfinite(v):
        return fallback
    return int(math.floor(v))


def normalize_hf_buckets(raw: Any) -> tuple[tuple[int, int], ...]:
    """分段定义校验：越界 / 倒置 / 重叠 / 非整数 / 超上限 ⇒ **整体**回落默认。

    整体回落而不是"逐段丢弃"：半套分段会让 ``layer_key`` 的语义变得不可预测
    （``"3-9"`` 是老板配的还是残留的？）。``None`` 与 ``[]`` 都回落默认 —— 与"没配"同义。
    """
    D = _default_buckets()
    if not isinstance(raw, (list, tuple)) or len(raw) == 0 or len(raw) > HF_LAYER_MAX_BUCKETS:
        return D
    out: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return D
        s, e = _as_int_like(item[0]), _as_int_like(item[1])
        if s is None or e is None:
            return D
        if s < 0 or e > 24 or s >= e:
            return D
        out.append((s, e))
    out.sort(key=lambda p: p[0])
    for i in range(1, len(out)):
        if out[i][0] < out[i - 1][1]:
            return D            # 已排序 ⇒ 重叠只可能与**前一段**发生
    return tuple(out)


def resolve_hf_layered_cfg(cfg: Mapping[str, Any] | None) -> HfLayeredConfig:
    """合并 ``cfg["layered"]`` 与 :data:`HF_LAYERED_DEFAULTS`。"""
    p = (cfg or {}).get("layered") or {}
    D = HF_LAYERED_DEFAULTS
    return HfLayeredConfig(
        enabled=bool(p.get("enabled", D["enabled"])),
        apply=bool(p.get("apply", D["apply"])),
        allow_loosen=bool(p.get("allowLoosen", D["allowLoosen"])),
        min_samples=max(1, _floor(p.get("minSamples"), D["minSamples"])),
        window_days=max(1, _floor(p.get("windowDays"), D["windowDays"])),
        buckets=normalize_hf_buckets(p.get("buckets", D["buckets"])),
    )


def hf_layer_key_for(hour: Any, buckets: Sequence[Sequence[int]]) -> str | None:
    """本地小时 → 段名（如 ``"18-24"``）；未被任何段覆盖 ⇒ ``None``（该样本不进任何桶，不报错）。"""
    if not _is_number(hour):
        return None
    h = int(math.floor(hour))
    for pair in buckets:
        s, e = int(pair[0]), int(pair[1])
        if s <= h < e:
            return f"{s}-{e}"
    return None


# ------------------------------------------------------------------ 聚合（纯函数）


@dataclass(frozen=True)
class HfLayerSample:
    """一条已收敛的台账行（分层统计的输入；只取算得着的列）。"""

    group_id: str
    engaged: int
    engage_signal: str | None = None
    #: 发出时刻（unix 秒）—— 归段的依据是"什么时候说的"，不是"什么时候收敛的"
    sent_at: int | None = None

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> "HfLayerSample":
        """数据层行 → 样本（只做形状转换，不判断合法性 —— 合法性归 :func:`aggregate_hf_layer_stats`）。"""
        raw_at = row.get("sent_at")
        return cls(
            group_id=str(row.get("group_id") or ""),
            engaged=int(row.get("engaged") or 0),
            engage_signal=row.get("engage_signal"),
            sent_at=int(raw_at) if _is_number(raw_at) else None,
        )


@dataclass(frozen=True)
class HfLayerStatRow:
    """一行段统计（落库 + 内存缓存同构）。"""

    group_id: str
    layer_kind: str
    layer_key: str
    n: int
    engaged: int
    #: 段内本底（该段各小时 ``ambient_p`` 的最大值；只用于展示/解释，**不参与判定**）
    ambient_p: float | None
    window_start: int


@dataclass(frozen=True)
class HfLayerAggregateOpts:
    """:func:`aggregate_hf_layer_stats` 的选项。"""

    buckets: Sequence[Sequence[int]]
    #: ``"{group_id}|{hour}"`` → ``ambient_p``（见 :func:`load_hf_group_hour_counts`）
    ambient_p_by_group_hour: Mapping[str, float]
    #: 弱信号本底上限（与调阈侧同一个 ``ambient_max``）
    ambient_max: float
    #: 统计窗口起点（unix 秒）
    window_start: int
    #: 白名单外的群直接不参与（与调阈侧同一个群白名单判据）
    group_filter: Callable[[str], bool] | None = None
    #: 除了有样本的群，还**强制输出**这些群的空桶（让 ``/heartflow layers`` 能显示进展）
    also_groups: Sequence[str] = ()


def _round4(v: float) -> float:
    """四舍五入到 4 位小数 —— 用 ``floor(v*1e4 + 0.5)`` 而不是 :func:`round`。

    Python 的 ``round`` 是**银行家舍入**（``round(0.5) == 0``），JS 的 ``Math.round`` 是
    "半值向上"。``ambient_p`` 恒非负，所以这条式子在 Py 侧与 TS 逐位一致。
    """
    return math.floor(v * 10000 + 0.5) / 10000


def aggregate_hf_layer_stats(
    samples: Sequence[HfLayerSample],
    opts: HfLayerAggregateOpts,
) -> list[HfLayerStatRow]:
    """**纯函数**：一批已收敛样本 → 每群每段的 ``(n, engaged)``。

    ⚠️ **INVARIANT I3**：这里必须**纯** —— 同输入同输出、不改输入、不写模块级缓存。
    它是"从零重算"那一步（sweep 另有增量刷新路线）：一旦在里面顺手写缓存或就地改样本，
    两条路就会给出不同的段统计，而产物长得一模一样（都是 :class:`HfLayerStatRow`）。
    守卫：``tests/invariants/test_invariants.py`` 的 ``test_i3_*``。

    取舍逐条写死，免得日后有人"顺手放宽"：

    * ``engage_signal`` 为 ``None`` 的行**一律丢弃**（v1.6.8 之前落的行没有信号列，
      那些正是**旧错误标签**）；
    * ``sent_at`` 缺失/非法的行丢弃（无法归段）；
    * **逐样本**用它**自己那一小时**的 ``ambient_p`` 过滤（不是段平均值 —— 段内 23 点冷清、
      18 点热闹，用平均会把 23 点的弱样本放进统计）；
    * 强信号（quote/mention/negative/veto）不受本底过滤（:func:`is_hf_sample_informative` 内已实现）；
    * 空段也输出 ``n=0`` 的行（前端要显示进度条，不能"没数据就不显示"）。
    """
    acc: dict[str, dict[str, list[int]]] = {}
    for s in samples:
        gid = s.group_id
        if not gid:
            continue
        if opts.group_filter is not None and not opts.group_filter(gid):
            continue
        signal = as_hf_engage_signal(s.engage_signal)
        if signal is None:
            continue
        at = s.sent_at
        if not _is_number(at) or at <= 0:
            continue
        hour = hf_local_hour(int(at))
        key = hf_layer_key_for(hour, opts.buckets)
        if key is None:
            continue
        ap = opts.ambient_p_by_group_hour.get(f"{gid}|{hour}") or 0
        if not is_hf_sample_informative(signal, ap, opts.ambient_max):
            continue
        cell = acc.setdefault(gid, {}).setdefault(key, [0, 0])
        cell[0] += 1
        if s.engaged == 1:
            cell[1] += 1

    groups = set(acc)
    for g in opts.also_groups:
        if not g:
            continue
        if opts.group_filter is not None and not opts.group_filter(g):
            continue
        groups.add(g)

    rows: list[HfLayerStatRow] = []
    for gid in sorted(groups):
        by_key = acc.get(gid) or {}
        for pair in opts.buckets:
            s0, e0 = int(pair[0]), int(pair[1])
            key = f"{s0}-{e0}"
            cell = by_key.get(key)
            amb = 0.0
            for h in range(s0, e0):
                v = opts.ambient_p_by_group_hour.get(f"{gid}|{h}")
                if v is not None and _is_number(v) and v > amb:
                    amb = float(v)
            rows.append(HfLayerStatRow(
                group_id=gid,
                layer_kind=HF_LAYER_KIND,
                layer_key=key,
                n=0 if cell is None else cell[0],
                engaged=0 if cell is None else cell[1],
                ambient_p=_round4(amb),
                window_start=int(opts.window_start),
            ))
    return rows


# ---------------------------------------------------------------- 内存缓存（判定的唯一依赖）


class HfLayerLookup(NamedTuple):
    """当前时段命中的段统计（``None`` = 无数据 / 当前时刻未被任何段覆盖）。"""

    key: str
    stat: HfLayerStatRow


#: ``"{account_id}:{group_id}"`` → (``layer_key`` → 行)
_layer_stats: dict[str, dict[str, HfLayerStatRow]] = {}
#: ``"{account_id}:{group}:{kind}:{key}"`` → 上次**成功写库**的内容签名（diff 用；只在真写成功后更新）
_written: dict[str, str] = {}
#: 每群 × 每小时入站量（见 :func:`load_hf_group_hour_counts`）
_hour_counts: dict[str, tuple[int, dict[str, int]]] = {}
_lock = threading.RLock()


def _ck(account_id: str, group_id: str) -> str:
    return f"{account_id}:{group_id}"


def _wk(account_id: str, r: HfLayerStatRow) -> str:
    return f"{account_id}:{r.group_id}:{r.layer_kind}:{r.layer_key}"


def _fmt_ambient(v: float | None) -> str:
    """签名里的本底分量：``None`` → 空串（对齐 TS ``v ?? ""``），数值用 ``%g``（对齐 JS ``String()``）。"""
    return "" if v is None else f"{v:g}"


def _sig_of(r: HfLayerStatRow) -> str:
    return f"{r.n}|{r.engaged}|{_fmt_ambient(r.ambient_p)}|{r.window_start}"


def _put_row(account_id: str, r: HfLayerStatRow) -> None:
    with _lock:
        _layer_stats.setdefault(_ck(account_id, r.group_id), {})[r.layer_key] = r


def get_hf_layer_stat(
    account_id: str,
    group_id: str,
    now_sec: int,
    buckets: Sequence[Sequence[int]],
) -> HfLayerLookup | None:
    """当前时刻落在哪一段（返回该段的行；无数据/未覆盖 ⇒ ``None``）。

    ⚠️ judge 热路径**只调这一个读口**，体内不得出现**导入语句 / SQL / 任何 IO**
    （见 ``test_layer.py`` 的源级守卫）。
    """
    with _lock:
        m = _layer_stats.get(_ck(account_id, group_id))
        if not m:
            return None
        key = hf_layer_key_for(hf_local_hour(now_sec), buckets)
        if key is None:
            return None
        stat = m.get(key)
    return None if stat is None else HfLayerLookup(key, stat)


def hf_layer_keys_cached(account_id: str, group_id: str) -> list[str]:
    """该群有缓存的段名清单（只给诊断/测试用）。"""
    with _lock:
        m = _layer_stats.get(_ck(account_id, group_id))
        return sorted(m) if m else []


def hf_layer_cache_size() -> int:
    with _lock:
        return sum(len(m) for m in _layer_stats.values())


def reset_hf_layer_cache() -> None:
    """测试用：清掉全部内存态（统计 + 写库签名 + 入站量缓存）。"""
    with _lock:
        _layer_stats.clear()
        _written.clear()
        _hour_counts.clear()


def load_hf_layer_stats(account_id: str) -> int:
    """启动预热：把库里已有的段统计读进内存（judge 热路径零 DB 读的前提）。

    失败降级：空缓存启动，首个 sweep 会重算补全 —— 预热失败**不该**挡住账号启动。
    """
    from ...data import repo_hf          # 延迟导入：本模块不许在加载期拉进第三方依赖（F5）
    try:
        rows = repo_hf.list_layer_stats(account_id)
    except Exception as exc:  # noqa: BLE001 —— 预热失败降级，见 docstring
        log.warning("[WPP HF] 分层统计预热失败 ⇒ 账号以空缓存启动（首个 sweep 重算补全）: %s", exc)
        return 0

    prefix = f"{account_id}:"
    with _lock:
        for k in [k for k in _layer_stats if k.startswith(prefix)]:
            del _layer_stats[k]
        for k in [k for k in _written if k.startswith(prefix)]:
            del _written[k]
    for r in rows:
        row = HfLayerStatRow(
            group_id=str(r["group_id"]),
            layer_kind=str(r["layer_kind"]),
            layer_key=str(r["layer_key"]),
            n=int(r.get("n") or 0),
            engaged=int(r.get("engaged") or 0),
            ambient_p=None if r.get("ambient_p") is None else float(r["ambient_p"]),
            window_start=int(r.get("window_start") or 0),
        )
        _put_row(account_id, row)
        with _lock:
            _written[_wk(account_id, row)] = _sig_of(row)
    return len(rows)


# ------------------------------------------------- 每群 × 每小时入站量（唯一取数入口）


def _local_offset_sec(now_sec: int) -> int:
    """本地 UTC 偏移（秒）—— 与 JS ``-new Date(nowSec*1000).getTimezoneOffset() * 60`` 同义。

    走 ``astimezone()`` 让运行时的时区规则（含夏令时）决定**该时刻**的偏移，
    不自己写死 8 小时。
    """
    off = datetime.fromtimestamp(now_sec).astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


def load_hf_group_hour_counts(account_id: str, now_sec: int) -> dict[str, int]:
    """每群 × 每小时**入站人类**消息数 → ``{"group|hour": n}``。

    调阈侧与分层侧共用同一条聚合查询，结果缓存 :data:`HF_HOUR_COUNTS_TTL_SEC` 秒
    ⇒ 一轮 sweep 只查一次。失败**向上抛**（让调用方各自 warn + 决定降级策略），
    成功才写缓存（失败不留脏缓存）。
    """
    from ...data import repo_messages   # 延迟导入，同上

    with _lock:
        hit = _hour_counts.get(account_id)
    if hit is not None and now_sec - hit[0] < HF_HOUR_COUNTS_TTL_SEC:
        return hit[1]
    rows = repo_messages.list_group_msg_hour_buckets(
        account_id,
        now_sec - HF_AMBIENT_LOOKBACK_DAYS * 86400,
        _local_offset_sec(now_sec),
    )
    counts: dict[str, int] = {}
    for r in rows:
        counts[f"{r['group_id']}|{int(r['h'])}"] = int(r["n"])
    with _lock:
        _hour_counts[account_id] = (now_sec, counts)
    return counts


def ambient_p_from_hour_counts(
    counts: Mapping[str, int],
    label_window_sec: int,
) -> dict[str, float]:
    """``{"group|hour": n}`` → ``{"group|hour": ambient_p}``（公式与调阈侧同一处，不复制第二份）。"""
    return {
        k: hf_ambient_p(n, HF_AMBIENT_LOOKBACK_DAYS * 3600, label_window_sec)
        for k, n in counts.items()
    }


# ------------------------------------------------------------------ IO 入口（sweep 每轮一次）


def maybe_recompute_hf_layer_stats(
    account_id: str,
    cfg: Mapping[str, Any] | None,
    now_sec: int,
    *,
    label_window_sec: int,
    ambient_max: float,
    group_filter: Callable[[str], bool] | None = None,
) -> int:
    """重算并落库分层统计。返回**实际写库的行数**（0 = 全部无变化，稳态下的正常结果）。

    调用位置：``learn.run_sweep`` 里画像 pass 之后、``if not L.enabled: return`` **之前**
    —— 分层统计与画像同级，不依赖"自动调阈"开关（老板可能关掉自动调阈但仍要看分层观测）。

    ``label_window_sec`` / ``ambient_max`` 必须由调用方从**它与调阈侧共用的那一份**
    ``LRN`` 传入（见模块 docstring 的"故意签名分歧"）。
    """
    from ...data import repo_hf          # 延迟导入，同上

    LAY = resolve_hf_layered_cfg(cfg)
    if not LAY.enabled:
        return 0                          # 连查询都不发
    window_start = now_sec - LAY.window_days * 86400

    rows_in = repo_hf.list_closed_since(account_id, window_start, HF_LAYER_DB_ROW_CAP)
    if len(rows_in) >= HF_LAYER_DB_ROW_CAP:
        log.warning("[WPP HF] 分层样本触顶（%d 行，窗口 %d 天）⇒ 统计可能偏高；考虑缩小 windowDays",
                    HF_LAYER_DB_ROW_CAP, LAY.window_days)

    # 本底取数失败不该拖垮统计本身：退化成"不过滤本底"（= v1.6.8 之前的旧行为），如实 warn 出来。
    ambient_by_group_hour: dict[str, float] = {}
    try:
        ambient_by_group_hour = ambient_p_from_hour_counts(
            load_hf_group_hour_counts(account_id, now_sec), label_window_sec)
    except Exception as exc:  # noqa: BLE001 —— 降级路径见上
        log.warning("[WPP HF] 分层本底取数失败 ⇒ 本轮不做本底过滤: %s", exc)

    prefix = f"{account_id}:"
    with _lock:
        also = [k[len(prefix):] for k in _layer_stats if k.startswith(prefix)]

    rows = aggregate_hf_layer_stats(
        [HfLayerSample.from_db_row(r) for r in rows_in],
        HfLayerAggregateOpts(
            buckets=LAY.buckets,
            ambient_p_by_group_hour=ambient_by_group_hour,
            ambient_max=ambient_max,
            window_start=window_start,
            group_filter=group_filter,
            also_groups=also,
        ),
    )

    for r in rows:
        _put_row(account_id, r)

    wrote = 0
    for r in rows:
        k = _wk(account_id, r)
        sig = _sig_of(r)
        with _lock:
            if _written.get(k) == sig:
                continue                  # 内容没变 ⇒ 不写（稳态下每轮 0 写，这是 I3 的落点）
        repo_hf.upsert_layer_stat(account_id, r.group_id, r.layer_kind, r.layer_key,
                                  r.n, r.engaged, r.window_start, ambient_p=r.ambient_p)
        with _lock:
            _written[k] = sig
        wrote += 1
    if wrote > 0:
        log.debug("[WPP HF] 分层统计写入 %d/%d 行 (account=%s)", wrote, len(rows), account_id)
    return wrote


def _is_number(v: Any) -> bool:
    """``Number.isFinite`` 的等价：bool 不算数（``True`` 会被当 1 混进来）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


CACHE_REGISTRY.register("layer", reset_hf_layer_cache)
