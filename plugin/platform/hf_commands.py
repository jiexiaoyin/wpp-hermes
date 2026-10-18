"""心流运维命令面（``/heartflow ...`` 的**子命令**）—— 移植自 TS ``inbound/filehelper-features.ts``。

## 为什么单独成模块

TS 侧这段有 370 行，且是 ``index.ts`` 里最大的一块（2026-09-28 因此被提取出来）。
搬到 Py 侧时**没有**放进 :mod:`.commands`：那个模块是"三个功能开关 + 白名单"的通用处理器，
而这里每一条子命令都只服务心流、都要读台账/分层/画像。混在一起的结果是
``commands.py`` 涨到 500 行、且**改心流展示口径时要动一个"通用"文件**。

分工：

    commands.py     通用（on/off/status 的骨架、/user /group /genpair /pairs） + **只做分发**
    hf_commands.py  /heartflow 的 status 详情与 threshold/group/profile/why/layers/veto/report

``commands.handle_command`` 里对 ``/heartflow`` 的导入是**函数级**的（同 ``/genpair``）：
本模块在模块级 import 心流七个包内子模块，放进加载期就等于给"注册链路"加了一段
心流加载代价 —— 那正是 F5 的形态（见 ``plugin/inbound/heartflow/__init__.py`` 的说明）。

## 与 TS 的三处**设计差异**（不是漏移植）

1. **没有"运行时单例"这一层**。TS 的 ``runtimeHeartflow.get(accountId) ?? defaultHeartflowConfig()``
   是为了绕开"配置单例可能被谁写脏"。Py 侧 2026-10-01 把那个单例**删掉**了，
   配置的唯一来源就是 ``core.accounts.resolve(account_id).heartflow``（已并入
   ``core/overrides`` 的覆盖），所以这里取一次 cfg 就同时是"账号档"与"生效档"。
2. ~~**写配置是内存覆盖，不是落盘**~~ —— **2026-10-02（W6）已改**：本侧现在和 TS 一样
   写 ``accounts/*.json``（``core/account_store``，带备份 + 原子替换 + 600），并靠
   ``adapter.reload_account`` 让新值**当次**生效。所以写命令的回复改成承诺
   「已写入账号配置，当次生效、重启不回退」—— 承诺变了，是因为实现变了
   （旧文案写「重启回退」是当时的实话：命令只改内存，重启就没了）。
3. **``/pair`` 兑换不在本模块**（Py 侧已删，见 ``commands.py`` docstring）；本模块与它无关。

## 一个必须照抄的行为：分段兜底

TS 的 ``status`` 由 8 个独立 ``try/catch`` 拼成，某一段读库失败只让**那一段**降级成
「(读取失败)」。照抄的原因不是风格：这条命令是**停摆时唯一能用的诊断入口**，
而"库里某张表正好有问题"恰恰是最需要它的时刻 —— 一个 try 包全部 ⇒ 整页变成
"读取失败"，运维反而失去了所有其它段的信息。每段的降级文案保持与 TS 同字。
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ..inbound.heartflow import budget as hf_budget
from ..inbound.heartflow import dedupe as hf_dedupe
from ..inbound.heartflow import heartflow as hf
from ..inbound.heartflow import layer as hf_layer
from ..inbound.heartflow import learn as hf_learn
from ..inbound.heartflow import observe as hf_observe
from ..inbound.heartflow import profile as hf_profile

log = logging.getLogger(__name__)

#: ``status`` / ``report`` 里群列表的显示上限（超出折叠成「…其余 N 群」）。
#: TS v1.8.0 加的：群多起来之后这条消息会长到看不清（20 群 ≈ 20 行）。
HF_STATUS_MAX_GROUPS = 5

#: 台账摘要 / 信号分布的回看窗口（秒）—— TS 里是字面量 ``86400``。
DAY_SEC = 86400

#: ``veto`` 推断"最近一条"的活跃窗口（秒）—— TS 里是字面量 ``900``。
VETO_INFER_WINDOW_SEC = 900

#: ``report`` 的天数区间（TS: ``Math.min(Math.max(floor(raw), 1), 30)``）。
REPORT_DAYS_MIN = 1
REPORT_DAYS_MAX = 30
REPORT_DAYS_DEFAULT = 7

#: 停摆金丝雀的告警线（天）—— 超过它就在「上次真发言」那行挂 ⚠️。
STALL_WARN_DAYS = 3

#: 开关别名 —— 与 ``commands._handle_feature_toggle`` 认的写法**必须**是同一组
#: （旧实现认 ``1/true/yes``，删掉别名就是悄悄改行为）。
_ON_OFF = frozenset({"on", "off", "1", "true", "yes", "0", "false", "no"})


def handle(account_id: str, args: Sequence[str], *, adapter: Any = None) -> str:
    """处理 ``/heartflow <子命令> ...``。**任何输入都返回一句可回复的话**（不抛、不返回空）。

    返回空串会让 ``adapter._maybe_command`` 静默不回 —— 命令打进 filehelper 却毫无反应，
    是最难排查的一种失败（老板只能猜"是不是又停摆了"）。

    ``adapter``：写配置的三个子命令（on|off / threshold / group）写回账号文件后，
    靠它让新值当次生效（见 ``commands.write_back``）。
    """
    arg_list = [a for a in args if a]
    sub = arg_list[0].lower() if arg_list else ""

    # on|off（含 /heartflow 1 这类别名）：与 /affection /jargon 共用同一份实现
    # —— 别在这里再写一遍开关语义（旧实现支持 1/true/yes，删掉别名就是悄悄改行为）
    if sub in _ON_OFF:
        return _commands()._handle_feature_toggle(account_id, "heartflow", arg_list,
                                                  adapter=adapter)
    if sub == "status":
        return _status(account_id)
    if sub == "threshold":
        return _threshold(account_id, arg_list, adapter=adapter)
    if sub == "group":
        return _group(account_id, arg_list, adapter=adapter)
    if sub == "profile":
        return _profile(account_id, arg_list)
    if sub == "why":
        return _why(account_id, arg_list)
    if sub == "layers":
        return _layers(account_id, arg_list)
    if sub == "veto":
        return _veto(account_id, arg_list)
    if sub == "report":
        return _report(account_id, arg_list)
    return _usage(account_id)


# --------------------------------------------------------------------- 通用小工具


def _commands():
    """延迟取命令模块（模块级 import 会成环：commands 要通过本模块分发 ``/heartflow``）。"""
    from . import commands

    return commands


def _cfg(account_id: str) -> dict[str, Any]:
    """账号档心流配置（账号文件 + 内存覆盖，都已并入）；取不到 / 形状脏 ⇒ ``{}``。

    直接复用 :func:`commands._safe_cfg` 的读法而不是自己 ``accounts.resolve`` 一次：
    "解析失败怎么办"只有一处判断（那里返空 dict 并 warn），再写一份迟早在那条分支上分叉。
    """
    raw = _commands()._safe_cfg(account_id).get("heartflow")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _hf_cfg(account_id: str) -> dict[str, Any]:
    """:func:`_cfg` + "空则回落缺省" —— 对应 TS 的 ``?? defaultHeartflowConfig()``。

    回落缺省只影响**展示**（白名单数、阈值行显示 0.6 而不是 "-"），不改变任何行为。
    """
    return _cfg(account_id) or hf.default_hf_config()


def _str_list(v: Any) -> list[str]:
    """``Array.isArray(x) ? x.map(String) : []`` —— 形状脏（字符串/None）⇒ 空列表。"""
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v]


def _now() -> int:
    return int(time.time())


def _fixed(v: Any, digits: int) -> str:
    """``Number.prototype.toFixed`` 的等价（**不是** ``f"{v:.2f}"``，平局取大）。

    第三份实现，``learn._fixed`` / ``observe._fixed`` 之外——本仓的既有约定是
    **格式化助手各留一份**（纯排版，不构成"两个真源"）；真正必须共用的**口径**
    （``ambient_p`` 公式）早已在 :mod:`.layer` 收口。
    """
    try:
        quant = Decimal(1).scaleb(-digits)
        return str(Decimal(v).quantize(quant, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return f"{v:.{digits}f}"


def _half_up(x: float) -> int:
    """``Math.round`` 的等价（``floor(x + 0.5)``；Python 的 ``round`` 是银行家舍入）。"""
    return int(math.floor(x + 0.5))


def _num_of(v: Any) -> float | None:
    """``Number(v)`` 的等价，但**拒 bool、拒空、拒非有限数**（拿不到数 ⇒ ``None``）。

    - ``bool`` 必须显式拒：``Number(true)`` 在 JS 里是 1，"阈值设成 true"要么是手滑、
      要么是配置写坏 —— 回一句用法，比默默设成 1.0 安全。
    - 空串**不用单独判**：``float("")`` / ``float("   ")`` 自己就抛（JS 的 ``Number("")``
      是 0 —— 两侧唯一必须记住的差异，而它对我们的意义只是"不能沿用 JS 的宽松"）。
      ⚠️ 所以这里的 ``except`` **必须**catch ``ValueError``：漏掉它，``/heartflow threshold abc``
      会抛穿到适配器，而适配器的兜底是"记一行 warn、不回话"，老板看到的是**毫无反应**。
    - 非有限数也拒：``nan`` 会**穿过** ``v < 0 or v > 1`` 的两侧比较（都是 False），
      写进覆盖后判定恒 False ⇒ 心流永不触发，且不报任何错。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _fmt_ts(sec: Any) -> str:
    try:
        return datetime.fromtimestamp(int(sec)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _safe(fallback: str, fn: Callable[[], str]) -> str:
    """跑一段展示逻辑；炸了只让**这一段**降级（见模块 docstring「分段兜底」）。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 —— 诊断入口不许因为一个坏查询整体哑掉
        log.warning("[WPP HF] 运维命令的某一段读取失败（已降级为占位）: %s", exc)
        return fallback


def _usage(account_id: str) -> str:
    """未知子命令 / 空参数：用法 + 当前开关状态（TS 末尾的兜底分支）。"""
    state = _commands().state_label(_commands()._feature_state(account_id, "heartflow"))
    return (
        "用法: /heartflow on|off|status"
        "\n  或 /heartflow threshold <0-1>"
        "\n  或 /heartflow group add|del|list <群ID>"
        "\n  或 /heartflow profile <群ID>"
        "\n  或 /heartflow why <群ID>"
        "\n  或 /heartflow layers [群ID]"
        "\n  或 /heartflow veto [群ID]"
        "\n  或 /heartflow report [天数]"
        f"\n状态: {state}"
    )


# ------------------------------------------------------------------------ status


def _status(account_id: str) -> str:
    """``/heartflow status``：开关 + 8 段详情（TS v1.10.0 的全量运维面）。

    段的**顺序是刻意的**（照抄 TS）：判决依据（生效阈值 / 上次真发言）在最前，
    "历史 → 现状 → 外环"在后。停摆时人从上往下读，因此最不该被折叠的两条必须最先出现。
    """
    hf_cfg = _hf_cfg(account_id)
    enabled = _commands()._feature_state(account_id, "heartflow")
    threshold = hf_learn.resolve_reply_threshold(hf_cfg)
    groups = _str_list(hf_cfg.get("whitelistGroups"))
    now = _now()

    learn_head = "\n自适应调阈: " + ("✅ 开启" if hf_learn.resolve_hf_learning(hf_cfg)["enabled"]
                                   else "❌ 关闭")
    extra = f"\n心流群白名单: {len(groups)} 个"
    extra += _safe("\nDB: (读体检状态失败)", lambda: _block_db_health())
    extra += _safe("\n生效阈值: (读取失败)",
                   lambda: _block_effective(account_id, hf_cfg, threshold, groups, now))
    extra += _safe("", lambda: _block_last_send(account_id, now))
    extra += _safe(f"{learn_head} (读学习状态失败)",
                   lambda: _block_learn(account_id, hf_cfg, learn_head))
    extra += _safe("\n近24h台账: (读台账失败)",
                   lambda: _block_ledger(account_id, now))
    extra += _safe("\n信号分布(24h): (读取失败)",
                   lambda: _block_signals(account_id, now))
    extra += _safe("\n发言预算: (读取失败)",
                   lambda: _block_budget(account_id, hf_cfg))
    extra += _safe("\n分层(群×时段): (读取失败)",
                   lambda: _block_layer(account_id, hf_cfg))
    extra += _safe("\n重复闸/占比外环: (读取失败)",
                   lambda: _block_share_and_dedupe(account_id, hf_cfg, now))

    return (f"心流主动回复 (account={account_id}):"
            f"\n状态: {_commands().state_label(enabled)}{extra}"
            f"\n用法: /heartflow on|off|status")


def _block_db_health() -> str:
    """**DB 体检**一行（D7，2026-10-02 修）。

    为什么状态页必须有这一行：``migration 未完成`` 的旧形态是**启动时一行 warn**。
    进程照跑、``SELECT 1`` 照通，于是"schema 只建了一半"在运维面上与"一切正常"
    完全一样（心流每条都写不进台账，而 0 条发言看起来就像群里没人说话）。
    这里把它变成一条**每次查状态都看得到**的行。
    """
    from ..data import db

    h = db.health()
    state = str(h.get("state") or "unknown")
    mark = {"ok": "✅", "drift": "⚠️", "failed": "❌", "unavailable": "❌"}.get(state, "❔")
    detail = str(h.get("detail") or "")
    line = f"\nDB: {mark} {state}"
    if state == "drift":
        line += "（**只报不改**，R8）"
    if detail:
        line += f" — {detail}"
    return line


def _block_effective(account_id: str, hf_cfg: Mapping[str, Any], threshold: float,
                     groups: Sequence[str], now: int) -> str:
    """**逐群生效阈值** + 生效预算（v1.10.0 的直接产物）。

    为什么必须有这一段：09-26 停摆 3 天期间旧状态页打印的是账号级 ``replyThreshold``（0.6）
    —— 看起来一切正常。真正决定"回不回"的是逐群生效值（学习/分层 + 画像抬升 + 可达性天花板），
    不摊开就永远看不见"阈值被抬到够不着"（见 :func:`learn.resolve_hf_effective_threshold`）。
    """
    acct_budget = hf_budget.resolve_budget(hf_cfg)
    allowed = [g for g in groups if hf.is_hf_group_allowed(g, hf_cfg)]
    shown = allowed[:HF_STATUS_MAX_GROUPS]

    effs = [hf_learn.resolve_hf_effective_threshold(account_id, g, hf_cfg, now) for g in shown]
    budgets = [hf.resolve_hf_effective_budget(account_id, g, hf_cfg, now) for g in shown]

    rows: list[str] = []
    for g, E, B in zip(shown, effs, budgets):
        src = (f"群级/分层 {_fixed(E.override, 2)}" if E.override is not None
               else f"账号级 {_fixed(E.base, 2)}")
        band = (f", 画像建议 {_fixed(E.band, 2)}" + (" 已截断" if E.band_capped else "")
                if E.band is not None else "")
        cap = (f", 天花板 {_fixed(E.reach_cap, 2)}"
               f" (最高只 {_fixed(E.reach_info.max_score, 2)}/{E.reach_info.n}条)"
               if E.reach_info is not None else "")
        # 生效预算与阈值同理：展示的必须是**实际生效**的三层收紧结果（账号档 → 画像 → 占比外环）
        budget = ""
        if (B.min_gap_sec, B.max_per_hour, B.max_per_day) != (
                acct_budget.min_gap_sec, acct_budget.max_per_hour, acct_budget.max_per_day):
            budget = (f"; 预算 {B.min_gap_sec}s/{B.max_per_hour}条·时/{B.max_per_day}条·天"
                      f" (账号档 {acct_budget.min_gap_sec}s/{acct_budget.max_per_hour}"
                      f"/{acct_budget.max_per_day}, 已被收紧)")
        rows.append(f"- {g}: 阈值 {_fixed(E.threshold, 2)} ({src}{band}{cap}){budget}")

    max_eff = max([*(e.threshold for e in effs), threshold])
    out = (f"\n生效阈值(=判据, 与 judge 同源): 账号级 {_fixed(threshold, 2)}"
           f" / 最高生效 {_fixed(max_eff, 2)}")
    if rows:
        out += "\n" + "\n".join(rows)
        if len(allowed) > len(rows):
            out += f"\n…其余 {len(allowed) - len(rows)} 群"
    else:
        out += "\n(白名单无群 ⇒ 心流不会在任何群发言)"
    return out


def _block_last_send(account_id: str, now: int) -> str:
    """停摆金丝雀：距上次**真**发言多久（``MAX(sent_at)``，不含被拦下的占位符）。"""
    from ..data import repo_hf

    last = repo_hf.get_last_sent_at(account_id)
    if last is None:
        return "\n上次真发言: 从未 (检查群白名单 / 阈值 / 预算)"
    age = now - int(last)
    days = age / 86400
    when = (f"{_fixed(days, 1)} 天前" if days >= 1 else f"{_half_up(age / 60)} 分钟前")
    line = f"\n上次真发言: {when} ({_fmt_ts(last)})"
    if days >= STALL_WARN_DAYS:
        line += f" ⚠️ 已超 {STALL_WARN_DAYS} 天 —— 典型停摆, 查上面的生效阈值/天花板/预算"
    return line


def _block_learn(account_id: str, hf_cfg: Mapping[str, Any], head: str) -> str:
    """已学阈值群列表（上限 :data:`HF_STATUS_MAX_GROUPS`）。

    提示里的样本门槛取**配置的真值**（``learning.minSample``）而不是抄 TS 的字面量 10 ——
    写成字面量的话，把门槛调到 20 之后这条运维提示会继续骗人（"满 10 条才自动调"）。
    """
    from ..data import repo_hf

    rows = repo_hf.list_group_states(account_id)
    if not rows:
        min_sample = hf_learn.resolve_hf_learning(hf_cfg)["minSample"]
        return head + f" (暂无已学阈值 — 样本收集中, 满 {min_sample} 条才自动调)"
    shown = rows[:HF_STATUS_MAX_GROUPS]
    lines = []
    for s in shown:
        cur = ("回落账号级" if s.get("learned_threshold") is None
               else _fixed(s["learned_threshold"], 2))
        last = ""
        if s.get("last_change_new") is not None:
            old = ("账号级" if s.get("last_change_old") is None
                   else _fixed(s["last_change_old"], 2))
            last = f" (上次 {old}→{_fixed(s['last_change_new'], 2)})"
        lines.append(f"- {s.get('group_id')} → {cur}{last}")
    out = head + f"\n已学阈值群 ({len(rows)}):\n" + "\n".join(lines)
    if len(rows) > len(shown):
        out += f"\n…其余 {len(rows) - len(shown)} 群"
    return out


def _block_ledger(account_id: str, now: int) -> str:
    """近 24h 台账摘要（v1.6.1：**"judge 跑了但没回"曾是盲区**，09-11 静默瘫教训）。"""
    from ..data import repo_hf

    c = repo_hf.count_by_status(account_id, now - DAY_SEC)
    if int(c.get("total") or 0) == 0:
        return "\n近24h台账: 0 行 (群里无消息 / judge 未跑 / judge 全失败 — 看日志 [WPP HEARTFLOW])"
    by_status = c.get("by_status") or {}
    reasons = c.get("by_suppressed_reason") or {}
    replied = int(by_status.get("sent", 0)) + int(by_status.get("closed", 0))
    silent = int(by_status.get("suppressed", 0))
    pending = int(by_status.get("judged", 0))
    out = (f"\n近24h台账: judge {int(c['total'])} 次 → 回复 {replied} / 沉默 {silent}"
           + (f" / 待发送 {pending}" if pending else ""))
    if reasons:
        out += "\n  沉默原因: " + ", ".join(f"{k} {v}" for k, v in reasons.items())
    return out


def _block_signals(account_id: str, now: int) -> str:
    """近 24h 已收敛样本的**命中信号分布**（老板要看的"标签是不是锚在我那条上"）。"""
    from ..data import repo_hf

    sig = repo_hf.count_engage_signals(account_id, now - DAY_SEC)
    if not sig:
        return "\n信号分布(24h): 0 (还没攒到已收敛样本)"
    body = " / ".join(f"{k} {v}" for k, v in sig.items())
    return (f"\n信号分布(24h): {body}"
            "\n  (quote/@=强正, negative=强负, short-window=窄窗有人说话, silence=无人接;"
            " 强信号恒采信, 弱信号按本底过滤)")


def _block_budget(account_id: str, hf_cfg: Mapping[str, Any]) -> str:
    """发言预算档位 + 进程内拦截计数。

    拦截计数是**进程内**的（``hf_budget_blocked_snapshot``），重启归零 —— 所以文案写
    「本次运行拦截」，不写「今日拦截」。
    """
    b = hf_budget.resolve_budget(hf_cfg)
    blocked = hf_budget.hf_budget_blocked_snapshot()
    quiet = (" / 静默段 " + ",".join(f"{s}-{e}时" for s, e in b.quiet_hours)
             if b.quiet_hours else " / 静默段 关")
    blocked_str = (" / ".join(f"{k} {v}" for k, v in blocked.items()) if blocked else "无")
    return (f"\n发言预算: {'开' if b.enabled else '关'}"
            f" (每群 ≥{b.min_gap_sec}s / ≤{b.max_per_hour}条·小时 / ≤{b.max_per_day}条·天{quiet})"
            f"\n本次运行拦截: {blocked_str}")


def _block_layer(account_id: str, hf_cfg: Mapping[str, Any]) -> str:
    """分层模式 + 已达门槛的段（老板要看"到底有没有在分层学"，否则影子态像"什么都没发生"）。"""
    from ..data import repo_hf

    LAY = hf_layer.resolve_hf_layered_cfg(hf_cfg)
    rows = repo_hf.list_layer_stats(account_id)
    qualified = [r for r in rows if int(r.get("n") or 0) >= LAY.min_samples]
    detail = " / ".join(
        f"{r['group_id']}[{r['layer_key']}] n={int(r['n'])}"
        f" rate={_fixed(int(r['engaged']) / int(r['n']), 3) if int(r['n']) > 0 else '-'}"
        for r in qualified[:HF_STATUS_MAX_GROUPS])
    mode = ("❌ 关闭" if not LAY.enabled else ("✅ 生效" if LAY.apply else "影子 (只记录不生效)"))
    out = (f"\n分层(群×时段): {mode}"
           f" (门槛 n≥{LAY.min_samples} / 窗口 {LAY.window_days} 天"
           f" / {'允许放宽' if LAY.allow_loosen else '只许收紧'})"
           f"\n  已达标段 {len(qualified)} 个" + (f": {detail}" if detail else " (样本仍在攒)"))
    if len(qualified) > HF_STATUS_MAX_GROUPS:
        out += f" …其余 {len(qualified) - HF_STATUS_MAX_GROUPS} 个"
    return out


def _block_share_and_dedupe(account_id: str, hf_cfg: Mapping[str, Any], now: int) -> str:
    """重复闸 + 占比外环（v1.9.0：老板要一眼看到"重复闸拦了多少"与"外环收紧了谁"）。"""
    from ..data import repo_hf

    DED = hf_dedupe.resolve_dedupe_cfg(hf_cfg)
    SG = hf_observe.resolve_hf_share_guard_cfg(hf_cfg)
    hits = hf_observe.hf_share_guard_snapshot(
        account_id, hf_budget.resolve_budget(hf_cfg), hf_cfg, now)
    suppressed = repo_hf.count_by_status(account_id, now - DAY_SEC).get("by_suppressed_reason") or {}
    out = (f"\n重复闸: {'✅ 开' if DED.enabled else '❌ 关'}"
           f" (仅心流主动插话 / 同群 {_half_up(DED.window_sec / 3600)}h"
           f" / 相似 ≥{DED.sim_threshold} / 短句豁免 <{DED.min_chars} 字)"
           f"\n  近24h拦下: {int(suppressed.get('repeat', 0))} 条"
           f" (异常增多=误杀, 可 heartflow.dedupe.simThreshold 调高)"
           f"\n占比外环: {'✅ 开' if SG.enabled else '❌ 关'}"
           f" (目标 ≤{_half_up(SG.target_share * 100)}%"
           f" / 样本 ≥{SG.min_msgs} 条·≥{SG.min_bot_sends} bot 条)")
    if hits:
        out += "\n  今日已收紧 %d 群: " % len(hits) + " / ".join(
            f"{h.group_id} {_fixed(h.share * 100, 1)}%" for h in hits[:HF_STATUS_MAX_GROUPS])
    else:
        out += "\n  今日未收紧 (无群超目标占比)"
    return out


# ------------------------------------------------------------------- threshold


def _threshold(account_id: str, args: Sequence[str], *, adapter: Any = None) -> str:
    """``/heartflow threshold <0-1>``：改账号级阈值（写回账号文件，W6 起持久）。"""
    v = _num_of(args[1] if len(args) > 1 else None)
    if v is None or v < 0 or v > 1:
        return ("用法: /heartflow threshold <0-1>"
                "\n示例: /heartflow threshold 0.5 (0.6=默认, 越低越活跃)")
    base = _commands()._feature_cfg(account_id, "heartflow")
    if base is None:
        # 底稿读不出来 ⇒ 拒绝写（否则会把整个 heartflow 段替换成只剩 replyThreshold）
        return _commands().UNREADABLE_CFG_MSG
    cfg = dict(base)
    cfg["replyThreshold"] = v
    err = _commands().write_back(account_id, "heartflow", cfg, adapter=adapter)
    if err:
        return err
    log.info("[WPP] /heartflow threshold → %s (account=%s, 已写回账号配置)", v, account_id)
    return (f"✅ 心流阈值已设为 {v:g} (account={account_id})\n"
            "已写入账号配置（accounts/<账号>.json），当次生效、重启不回退")


# ----------------------------------------------------------------------- group


def _group(account_id: str, args: Sequence[str], *, adapter: Any = None) -> str:
    """``/heartflow group add|del|list [群ID...]``：心流群白名单（写回账号文件）。

    ``add`` 会**顺带**把群补进群聊白名单（否则心流收不到那个群的消息），``del`` 不动它
    —— 与 TS ``updateHeartflowGroups`` 同一条规矩（照抄：只增不减）。

    ⚠️ 两个键分两次写（``heartflow`` / ``groupAllowFrom``）⇒ 第二次失败时第一次**已经生效**。
    这时必须让运维看见"改了一半"（回复里点名哪个键没写进去），而不是笼统报个 ❌。
    """
    sub = args[1].lower() if len(args) > 1 else ""
    targets = [a for a in args[2:] if a]
    wl = _str_list(_hf_cfg(account_id).get("whitelistGroups"))

    if sub == "list" or (not sub and not targets):
        body = "\n".join(f"- {g}" for g in wl) if wl else "(空)"
        return f"心流群白名单 ({len(wl)}):\n{body}"

    if sub in ("add", "del") and targets:
        cmd = _commands()
        base = cmd._feature_cfg(account_id, "heartflow")
        if base is None:
            # 底稿读不出来 ⇒ **拒绝写**：整体替换那个键会把阈值等同级键一起抹掉，
            # 而回话还是 ✅（见 commands.py 的 W7-5 一节）。
            return cmd.UNREADABLE_CFG_MSG
        cfg = dict(base)
        cur = list(wl)
        for t in targets:
            if sub == "add" and t not in cur:
                cur.append(t)
            elif sub == "del" and t in cur:
                cur.remove(t)
        cfg["whitelistGroups"] = cur
        err = cmd.write_back(account_id, "heartflow", cfg, adapter=adapter)
        if err:
            return err

        # ⚠️ 这里也走**盘上那份**（不是 resolve()）：写回是整体替换 ⇒ 空底稿会把
        #    群聊白名单清成只剩新加的那个群。
        gal = cmd._allowlist_items(account_id, "groupAllowFrom")
        if sub == "add":
            if gal is None:
                return (f"✅ 心流群白名单已添加: {', '.join(targets)}"
                        f"\n⚠️ 但**读不到账号文件**，群聊白名单没能补上 ——"
                        f" 那个群仍收不到消息。重发一次这条命令即可")
            added = [t for t in targets if t not in gal]
            if added:
                err2 = cmd.write_back(account_id, "groupAllowFrom", [*gal, *added],
                                      adapter=adapter)
                if err2:
                    return (f"{err2}\n⚠️ 但心流群白名单**已经写入**（{', '.join(cur)}）"
                            f"—— 群聊白名单没跟上，那个群仍收不到消息。重发一次这条命令即可")
                gal = [*gal, *added]
        log.info("[WPP] /heartflow group %s %s (account=%s, 已写回账号配置)",
                 sub, targets, account_id)
        gal_line = "读不到" if gal is None else f"{len(gal)} 个"
        return (f"✅ 心流群白名单已{'添加' if sub == 'add' else '移除'}: {', '.join(targets)}"
                f"\n当前心流群 ({len(cur)}): {', '.join(cur) or '(空)'}"
                f"\n(add 自动补群聊白名单; del 不删群聊白名单,"
                f" 当前群聊白名单 {gal_line})"
                "\n已写入账号配置，当次生效、重启不回退")

    return ("用法: /heartflow group add <群ID> [群ID...]"
            "\n  或 /heartflow group del <群ID> [群ID...]"
            "\n  或 /heartflow group list")


# --------------------------------------------------------------------- profile


def _profile(account_id: str, args: Sequence[str]) -> str:
    """``/heartflow profile <群ID>``：该群画像 + 实测统计。"""
    from ..data import repo_hf

    gid = args[1].strip() if len(args) > 1 else ""
    if not gid:
        return ("用法: /heartflow profile <群ID>"
                "\n(画像由 sweep 每日自动生成; 样本不足的群暂无画像)")
    row = repo_hf.get_profile(account_id, gid)
    if not row:
        return (f"该群暂无画像 (account={account_id}):\n{gid}"
                "\n画像在 sweep 里每日生成; 需近 14 天有足够群消息。")
    hf_cfg = _hf_cfg(account_id)
    p = hf_profile.parse_hf_group_profile_row(
        {"profile_json": _dumps(row.get("profile"))})
    apply_quiet = hf_profile.resolve_hf_profile_cfg(hf_cfg).apply_quiet_hours
    st = row.get("stats") if isinstance(row.get("stats"), Mapping) else {}
    hist = st.get("hourHist") if isinstance(st.get("hourHist"), (list, tuple)) else []
    derived = (st.get("derivedActiveHours")
               if isinstance(st.get("derivedActiveHours"), (list, tuple)) else [])
    lines = [
        f"群画像 (account={account_id}, v{row.get('version') or 1},"
        f" {_fmt_ts(row.get('generated_at'))}, {row.get('model') or '?'})",
        f"群: {gid}",
        f"群性质: {p.nature or '-'}" if p else "⚠️ 画像解析失败 (只展示统计)",
        f"语言风格: {p.style or '-'}" if p else "",
        f"我在该群的角色: {p.bot_role or '-'}" if p else "",
        f"宜接话题: {'、'.join(p.engage)}" if p and p.engage else "",
        f"忌接话题: {'、'.join(p.avoid)}" if p and p.avoid else "",
        f"画像说活跃时段: {','.join(str(h) + '时' for h in p.active_hours)}"
        if p and p.active_hours else "",
        f"实测活跃时段: {','.join(str(h) + '时' for h in derived)}" if derived else "",
        (f"建议静默段: {','.join(f'{s}-{e}时' for s, e in p.quiet_hours)}"
         f" ({'已生效' if apply_quiet else '未生效, 仅建议'})")
        if p and p.quiet_hours else "",
        f"建议阈值下限: {p.band} (只会抬高阈值, 不会下压)" if p and p.band is not None else "",
        f"建议预算收紧: {_dumps(p.budget)}" if p and p.budget else "",
        (f"样本: {row.get('sample_msgs') or 0} 条 / 窗口内共 {st.get('total', '?')} 条"
         f" / 活跃 {st.get('activeDays', '?')} 天 / 均长 {st.get('avgLen', '?')} 字"),
        ("每小时消息量: " + " ".join(
            f"{h}时{n}" for h, n in enumerate(hist) if n > 0)) if hist else "",
    ]
    return "\n".join(x for x in lines if x)


# ------------------------------------------------------------------------- why


def _why(account_id: str, args: Sequence[str]) -> str:
    """``/heartflow why <群ID>``：解释该群上一条为什么回/不回。

    ⚠️ 归因行与生效值都走**唯一入口**（``resolve_hf_threshold_decision`` /
    ``resolve_hf_effective_threshold``），不许在这里现算 —— v1.10.0 之前这里写过
    第三份 ``max(base, 画像下限)``，于是 judge 按 0.85 判、这页显示 0.60，
    停摆期间"看起来完全正常"（见那两个函数的 docstring）。
    """
    from ..data import repo_hf

    gid = args[1].strip() if len(args) > 1 else ""
    if not gid:
        return ("用法: /heartflow why <群ID>"
                "\n(显示该群最近一次 judge 的五维打分/有效阈值/命中信号 + 当前预算与画像)")
    hf_cfg = _hf_cfg(account_id)
    last = repo_hf.get_ledger_last(account_id, gid)
    learned = hf_learn.get_learned_threshold(account_id, gid)
    d = hf_learn.resolve_hf_threshold_decision(account_id, gid, hf_cfg)
    E = hf_learn.resolve_hf_effective_threshold(account_id, gid, hf_cfg)
    anchor = hf_learn.hf_layer_anchor_for(account_id, gid, hf_cfg)
    base = E.override if E.override is not None else hf_learn.resolve_reply_threshold(hf_cfg)
    eff = E.threshold

    if d.source == "layer":
        src_name = f"分层[{d.layer_key}]"
    elif d.source == "group":
        src_name = f"群级 learned {_fixed(learned, 2) if learned is not None else '-'}"
    else:
        src_name = "账号级"

    attr = f"生效阈值 {_fixed(eff, 2)} = {src_name} {_fixed(base, 2)}"
    if d.layer_key:
        rate = "无样本" if d.layer_rate is None else f"rate={_fixed(d.layer_rate, 3)}"
        attr += (f" [段 {d.layer_key} n={d.layer_n} {rate}"
                 f"{' 生效' if d.apply else ' 未生效(影子)'}]")
    attr += (" ← 群级 "
             + (_fixed(learned, 2) if learned is not None else f"- (锚点 {_fixed(anchor, 2)})")
             + f" / 账号 {_num_text(hf_cfg.get('replyThreshold'))}")
    if E.band is not None:
        if E.band_capped:
            attr += (f" / 画像建议 {_fixed(E.band, 2)}"
                     f" ⇒ 抬到 {_fixed(base + hf_profile.resolve_hf_profile_cfg(hf_cfg).max_raise, 2)}"
                     " (上限截断)")
        else:
            attr += f" / 画像建议 {_fixed(E.band, 2)} (未抬高)"
    if E.reach_info is not None:
        attr += (f"\n可达性天花板: 压到 {_fixed(E.reach_cap, 2)}"
                 f" (近 7 天 {E.reach_info.n} 条判定最高只 {_fixed(E.reach_info.max_score, 2)}"
                 " —— 阈值够不着 ⇒ 心流等于停摆)")

    LAY = hf_layer.resolve_hf_layered_cfg(hf_cfg)
    if d.shadow is not None:
        shadow = (f"影子建议: 分层[{d.shadow.layer_key}] n={d.shadow.n}"
                  f" rate={_fixed(d.shadow.rate, 3)} ⇒ {_fixed(d.shadow.threshold, 2)}"
                  " (layered.apply=false, 未生效)")
    elif d.layer_key and d.layer_n > 0 and not d.apply:
        r = "-" if d.layer_rate is None else _fixed(d.layer_rate, 3)
        shadow = f"影子分层: 段 {d.layer_key} n={d.layer_n} rate={r} ⇒ 死区内/不可动, 无建议"
    elif d.layer_key:
        shadow = (f"影子分层: 段 {d.layer_key} n=0 ⇒ 样本不足 (门槛 {LAY.min_samples})")
    else:
        shadow = ""

    b = hf_budget.get_budget_state(account_id, gid, _now())
    budget_str = f"预算(本群): 本小时 {b.hour_count} 条 / 今日 {b.day_count} 条"
    if b.last_reply_at_sec:
        budget_str += f" / 上次发言 {_half_up((_now() - b.last_reply_at_sec) / 60)} 分钟前"

    prof = hf_profile.get_hf_profile_prompt_text(account_id, gid)
    head = _why_head(last)
    lines = [f"为什么 (account={account_id}):", f"群: {gid}", *head, attr, shadow,
             budget_str, f"画像摘要:\n{prof}" if prof else "画像: 无 (未生成 / 未预热)"]
    return "\n".join(x for x in lines if x)


def _why_head(last: Mapping[str, Any] | None) -> list[str]:
    """``why`` 的台账头部（最近一次 judge 的五维 + 结论 + 沉默原因）。"""
    if not last:
        return ["最近无 judge 记录 (该群近 7 天没攒到台账行)"]
    out = [f"最近一次 judge: {_fmt_ts(last.get('judged_at'))} 状态={last.get('status')}"]
    overall = last.get("judge_overall")
    if overall is not None:
        eth = last.get("effective_threshold")
        out.append(f"综合分 {overall} vs 当时阈值 {eth if eth is not None else '?'} ⇒ "
                   f"{'过阈' if float(overall) >= float(eth if eth is not None else 0.6) else '未过阈(沉默)'}")
    else:
        out.append("无打分 (judge 未产出结果)")
    out.append(f"五维: 相关 {_val(last.get('dim_r'))} / 意愿 {_val(last.get('dim_w'))}"
               f" / 社交 {_val(last.get('dim_s'))} / 时机 {_val(last.get('dim_t'))}"
               f" / 连贯 {_val(last.get('dim_c'))}")
    engaged = last.get("engaged")
    out.append(f"精力 {_val(last.get('energy'))}"
               f" / 被接话 {'(不含在样本内)' if engaged is None else ('是' if engaged else '否')}"
               f" / 信号 {last.get('engage_signal') or '(旧行/无)'}")
    if last.get("suppressed_reason"):
        out.append(f"沉默原因: {last['suppressed_reason']}")
    out.append(f"消息: {str(last.get('content_head') or '')[:40]}")
    return out


def _val(v: Any) -> str:
    """台账字段展示：``None`` → ``-``（TS 的 ``?? "-"``）。"""
    return "-" if v is None else str(v)


def _num_text(v: Any) -> str:
    """原始数值展示（``hfCfg.replyThreshold ?? "-"``）：**不补零**，与 TS ``String(v)`` 同形。"""
    n = _num_of(v)
    return "-" if n is None else f"{n:g}"


def _dumps(v: Any) -> str | None:
    """``JSON.stringify`` 的等价（``separators`` 与 JS 一致：无空格）。"""
    import json

    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------- layers


def _layers(account_id: str, args: Sequence[str]) -> str:
    """``/heartflow layers [群ID]``：每群各时段的分层统计 + 影子建议 + 每段样本进度。"""
    from ..data import repo_hf

    gid = args[1].strip() if len(args) > 1 else ""
    hf_cfg = _hf_cfg(account_id)
    LAY = hf_layer.resolve_hf_layered_cfg(hf_cfg)
    now = _now()
    cur_key = hf_layer.hf_layer_key_for(hf_budget.hf_local_hour(now), LAY.buckets)
    rows = repo_hf.list_layer_stats(account_id)
    all_groups = sorted({str(r["group_id"]) for r in rows})
    groups = [g for g in all_groups if g == gid] if gid else all_groups
    if not groups:
        tail = "\n(统计由 sweep 每 5 分钟重算一次; 需先有心流已收敛样本)"
        if gid:
            return f"该群暂无分层统计 (account={account_id}):\n{gid}{tail}"
        return f"暂无分层统计 (account={account_id}){tail}"

    shown = groups[:HF_STATUS_MAX_GROUPS]
    mode = ("❌ 关闭" if not LAY.enabled
            else ("✅ 生效" if LAY.apply else "影子 (apply=false, 只记录不生效)"))
    lines = [
        f"分层统计 (群×时段; account={account_id})",
        f"模式: {mode} / 每段门槛 n≥{LAY.min_samples} / 窗口 {LAY.window_days} 天"
        f" / {'允许放宽' if LAY.allow_loosen else '只许收紧'}",
        "段: " + " / ".join(f"{s}-{e}时" for s, e in LAY.buckets)
        + f" · 当前时段 {cur_key or '(未被任何段覆盖)'}",
    ]
    for g in shown:
        anchor = hf_learn.hf_layer_anchor_for(account_id, g, hf_cfg)
        lines.append(f"群 {g}: 群级锚点 {_fixed(anchor, 2)}")
        for s, e in LAY.buckets:
            key = f"{s}-{e}"
            r = next((x for x in rows
                      if str(x["group_id"]) == g and str(x["layer_key"]) == key
                      and str(x["layer_kind"]) == "daypart"), None)
            n = int(r.get("n") or 0) if r is not None else 0
            engaged = int(r.get("engaged") or 0) if r is not None else 0
            step = hf_learn.hf_layer_step(anchor, n, engaged, hf_cfg)
            line = (f"  {key}时: n={n}/{LAY.min_samples}"
                    + ("" if n >= LAY.min_samples else " (样本不足)")
                    + f" rate={_fixed(engaged / n, 3) if n > 0 else '-'}")
            if r is not None and r.get("ambient_p") is not None:
                line += f" 本底={_fixed(r['ambient_p'], 3)}"
            line += (f" ⇒ 建议 {_fixed(step.threshold, 2)}{' (生效)' if LAY.apply else ' (未生效)'}"
                     if step.changed else " ⇒ 无建议 (死区/样本不足)")
            if key == cur_key:
                line += " ← 当前时段"
            lines.append(line)
    if len(groups) > len(shown):
        lines.append(f"…其余 {len(groups) - len(shown)} 群")
    return "\n".join(lines)


# ------------------------------------------------------------------------ veto


def _veto(account_id: str, args: Sequence[str]) -> str:
    """``/heartflow veto [群ID]``：一键"这条不该回" = 人工强负样本。

    不给群 ID 时**推断**最近那个群，但只在**唯一**命中时推断 —— 写错群不可撤销
    （错误样本会在 60 天分层窗口里持续污染），所以多群歧义时报错要人显式指定。
    """
    from ..data import repo_hf

    now = _now()
    gid = args[1].strip() if len(args) > 1 else ""
    if not gid:
        rows = repo_hf.list_sent_counts_recent(
            account_id, now - VETO_INFER_WINDOW_SEC, now - VETO_INFER_WINDOW_SEC)
        active = [str(r["group_id"]) for r in rows]
        if not active:
            return (f"最近 15 分钟没有心流发言可否决 (account={account_id})"
                    "\n用法: /heartflow veto [群ID]")
        if len(active) >= 2:
            return (f"最近 15 分钟有 {len(active)} 个群发过言, 无法判断是哪一条"
                    " ⇒ 请显式指定群 ID:\n"
                    + "\n".join(f"- {g}" for g in active)
                    + "\n用法: /heartflow veto <群ID>")
        gid = active[0]

    r = repo_hf.mark_veto(account_id, gid, now)
    if not r:
        return (f"没找到可否决的发言 (account={account_id}):\n{gid}"
                "\n(只对\"已发出/已收敛\"的心流发言生效; 已收敛的更早发言不在\"最近一条\")")
    # ⚠️ 必须删内存开窗：否则随后有人引用那条 bot 消息会把 veto 覆盖成 engaged=1
    #    —— 那比不点这一下更糟（把一个明确的人工负样本改成了正样本）。
    hf_learn.forget_hf_open_window(account_id, gid)
    log.info("[WPP] /heartflow veto 群 %s (account=%s)", gid, account_id)
    return (f"已标记「这条不该回」(account={account_id}):\n群: {gid}"
            f"\n那条: {str(r.get('content_head') or '(无内容记录)')[:40]}"
            f"\n发出: {_fmt_ts(r.get('sent_at'))}"
            "\n已作为强负样本参与分层与调阈 (signal=veto, 不受本底过滤)。")


# ---------------------------------------------------------------------- report


def _report(account_id: str, args: Sequence[str]) -> str:
    """``/heartflow report [天数]``：占比/接话率/被制止率/重复率/分层/预算/外环复盘。

    ★ 每一块的算法都是**复用闸那一条**（``hf_bot_share`` / ``hf_engagement_summary`` /
    ``hf_repeat_rate`` / ``hf_layer_step``）—— 报告里报出的每个百分点都等于"换到今天会被
    同一道闸拦下"，所以它才能当调参依据。这里只做取数与拼装，不写第二套判定。
    """
    from ..data import repo_hf, repo_messages

    raw = _num_of(args[1]) if len(args) > 1 else None
    days = (REPORT_DAYS_DEFAULT if raw is None
            else min(max(int(math.floor(raw)), REPORT_DAYS_MIN), REPORT_DAYS_MAX))
    now = _now()
    since = now - days * DAY_SEC

    hf_cfg = _hf_cfg(account_id)
    LRN = hf_learn.resolve_hf_learning(hf_cfg)
    LAY = hf_layer.resolve_hf_layered_cfg(hf_cfg)
    DED = hf_dedupe.resolve_dedupe_cfg(hf_cfg)
    base_budget = hf_budget.resolve_budget(hf_cfg)

    # ① 发言占比（wpp_messages 按群 × 方向计数；chat_id 空则归 peer_id）
    agg: dict[str, dict[str, int]] = {}
    for r in repo_messages.list_bot_msg_share(account_id, since):
        g = r.get("gid")
        if not g:
            continue
        cur = agg.setdefault(str(g), {"inbound": 0, "outbound": 0})
        n = int(r.get("n") or 0)
        if str(r.get("direction")) == "outbound":
            cur["outbound"] += n
        else:
            cur["inbound"] += n
    shares = sorted(
        (hf_observe.HfDigestShare(g, v["inbound"], v["outbound"])
         for g, v in agg.items()),
        key=lambda s: hf_observe.hf_bot_share(s.inbound, s.outbound), reverse=True)

    # ② 接话率（已收敛样本；逐样本用它自己那一小时的本底 ⇒ 与 sweep 的调阈同源）
    ambient = hf_layer.ambient_p_from_hour_counts(
        hf_layer.load_hf_group_hour_counts(account_id, now), LRN["labelWindowSec"])
    closed = repo_hf.list_closed_since(account_id, since, hf_layer.HF_LAYER_DB_ROW_CAP)
    engagement = hf_observe.hf_engagement_summary(closed, ambient, LRN["ambientMax"])

    # ③ 被制止率（judged_at 口径；legacy 旧标签行不计入分母）
    sig = repo_hf.count_engage_signals(account_id, since)
    stopped_total = sum(v for k, v in sig.items() if k != "legacy")

    # ④ 重复率（与闸同源：同一份 hf_repeat_verdict）
    outbound = repo_messages.list_outbound_texts(account_id, since, 2000)
    repeat = hf_observe.hf_repeat_rate(
        [hf_observe.HfRepeatItem(str(r["gid"]), r["content"], int(r["at_sec"]))
         for r in outbound if r.get("gid")], DED)

    # ⑤ 分层（只看**当前时段**，与 /heartflow layers 同一个 hf_layer_step）
    layer_rows = repo_hf.list_layer_stats(account_id)
    cur_key = hf_layer.hf_layer_key_for(hf_budget.hf_local_hour(now), LAY.buckets)
    layers: list[hf_observe.HfDigestLayer] = []
    if cur_key:
        for g in sorted({str(r["group_id"]) for r in layer_rows}):
            r = next((x for x in layer_rows
                      if str(x["group_id"]) == g and str(x["layer_key"]) == cur_key
                      and str(x["layer_kind"]) == "daypart"), None)
            n = int(r.get("n") or 0) if r is not None else 0
            engaged = int(r.get("engaged") or 0) if r is not None else 0
            step = hf_learn.hf_layer_step(
                hf_learn.hf_layer_anchor_for(account_id, g, hf_cfg), n, engaged, hf_cfg)
            layers.append(hf_observe.HfDigestLayer(
                group_id=g, layer_key=cur_key, n=n, engaged=engaged,
                rate=(engaged / n) if n > 0 else None,
                suggestion=step.threshold if step.changed else None,
                applied=bool(LAY.apply and LAY.enabled and n >= LAY.min_samples and step.changed),
            ))
        layers.sort(key=lambda x: x.n, reverse=True)

    return hf_observe.build_hf_digest(hf_observe.HfDigestInput(
        now_sec=now, days=days, shares=shares, engagement=engagement,
        stopped=hf_observe.HfStoppedStat(
            negative=int(sig.get("negative", 0)), veto=int(sig.get("veto", 0)),
            total=stopped_total),
        repeat=repeat, repeat_sample_total=len(outbound), layers=layers,
        budget=hf_budget.hf_budget_blocked_snapshot(),
        share_guard=hf_observe.hf_share_guard_snapshot(account_id, base_budget, hf_cfg, now),
    ))
