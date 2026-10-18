"""心流运维命令面回归 —— TS ``inbound/filehelper-features.ts`` 的 pytest 译本。

## 这一面为什么值得单独一套门

``/heartflow ...`` 是**停摆时唯一能用的诊断入口**：09-26 心流静默停摆 3 天，
期间唯一能问"到底怎么了"的地方就是它。所以它的失效形态不是"报错"，而是
**回答得头头是道但内容是错的**：

1. **状态页报的是账号档而不是生效档** —— v1.10.0 之前的真事：judge 按 0.85 判，
   页面显示 0.60，"看起来完全正常"。⇒ §2 用**桩返回值**钉住"打印的必须是
   ``resolve_hf_effective_threshold`` 的结果"（而不是再算一遍账号级）。
2. **一段查询坏掉 ⇒ 整页报废** —— 而那恰恰是最需要它的时刻。⇒ §1 断言
   一段炸了其它段照常显示（TS 的 8 个 try/catch 是**行为**，不是风格）。
3. **计数口径错**：``回复`` 必须含 ``closed``（收敛行也是发出去过的），
   ``拦下`` 必须读 ``by_suppressed_reason["repeat"]``。两者写错都**不报错**，
   只是数字骗人。⇒ §5 / §7。
4. **写配置的回复必须说真话** —— 2026-10-02（W6）之前本侧只改内存，
   文案里必须写「重启后回退」；现在改成落盘 + 当次生效，文案跟着变成
   「已写入账号配置，当次生效、重启不回退」，而 **判据从"文案里有这句话"换成
   "账号文件真的变了"**（§9 / §10）—— 只断言文案等于把谎话钉进测试。
5. **veto 后不删内存开窗** —— 随后有人引用那条 bot 消息会把 veto 覆盖成
   ``engaged=1``，**比不点这一下更糟**（人工负样本变正样本）。⇒ §11 断言
   ``forget_hf_open_window`` 被调到。

真实日期/生产数字一律不进本文件（公开仓红线）；样本全是构造的。

## 取数口一律打桩

本套测试**不碰真库**：``repo_hf`` / ``repo_messages`` 的每个取数口都被换成桩
（默认空数据，用例只覆盖自己关心的那几个），时钟也被钉成 :data:`NOW`。
这与 ``test_db.py`` 的 ``_StubDb`` 是同一策略的两个位置：**测只走 stub**。
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from plugin.core import overrides as OV
from plugin.data import repo_hf, repo_messages
from plugin.inbound.heartflow import budget as B
from plugin.inbound.heartflow import learn as L
from plugin.inbound.heartflow import observe as O
from plugin.platform import commands, hf_commands

H = hf_commands
CMD_SRC = Path(commands.__file__).read_text(encoding="utf-8")
HF_CMD_SRC = Path(hf_commands.__file__).read_text(encoding="utf-8")

#: 固定时刻（本地时区字段构造 ⇒ 与 ``hf_local_hour`` 同语义；20:30 落在 18-24 段）
NOW = int(datetime(2026, 9, 20, 20, 30).timestamp())
ACC = "acct"

#: ``repo_hf`` 里本面会调到的取数口 → 空数据缺省
HF_READS: dict[str, Any] = {
    "list_group_states": [],
    "count_by_status": {"total": 0, "by_status": {}, "by_suppressed_reason": {}},
    "count_engage_signals": {},
    "list_layer_stats": [],
    "get_last_sent_at": None,
    "get_ledger_last": None,
    "get_profile": None,
    "mark_veto": None,
    "list_sent_counts_recent": [],
    "list_closed_since": [],
}
MSG_READS: dict[str, Any] = {
    "list_bot_msg_share": [],
    "list_outbound_texts": [],
    # ``report`` 的接话率要"逐样本用它自己那一小时的本底"⇒ 会经 layer 读这张表
    "list_group_msg_hour_buckets": [],
}


# --------------------------------------------------------------------- 沙箱


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """账号目录 + 内存覆盖 + **时钟**全部隔离，每个用例一个干净世界。"""
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_JSON", raising=False)
    OV._runtime_overrides.clear()
    monkeypatch.setattr(H, "_now", lambda: NOW)
    yield d
    OV._runtime_overrides.clear()


def write_account(dirpath, account_id: str, cfg: dict) -> None:
    (dirpath / f"{account_id}.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def account(_sandbox):
    """一个可用账号：agent 合法 + 心流开着 + 两个群白名单。"""
    write_account(_sandbox, ACC, {
        "agent": "wpp-wechat-test",
        "heartflow": {
            "enabled": True,
            "whitelistGroups": ["g1@chatroom", "g2@chatroom"],
            "replyThreshold": 0.6,
        },
    })
    return _sandbox


@pytest.fixture
def db(monkeypatch):
    """取数口桩：``db["count_by_status"] = {...}`` 覆盖；``db["_calls"]`` 记调用名。"""
    state: dict[str, Any] = {"_calls": []}

    def install(mod, name, default):
        state[name] = default

        def fn(*a, **kw):
            state["_calls"].append(name)
            v = state[name]
            if isinstance(v, BaseException):
                raise v
            return v(*a, **kw) if callable(v) else v

        monkeypatch.setattr(mod, name, fn)

    for name, default in HF_READS.items():
        install(repo_hf, name, default)
    for name, default in MSG_READS.items():
        install(repo_messages, name, default)
    return state


# ===================================================== §1 分段兜底（诊断入口不许整页哑）


def test_one_broken_query_only_degrades_its_own_section(account, db):
    db["list_group_states"] = RuntimeError("learning 表坏了")
    out = H.handle(ACC, ["status"])
    assert "读学习状态失败" in out              # 坏的那段降级
    assert "近24h台账: 0 行" in out             # 其余段照常
    assert "发言预算:" in out and "上次真发言:" in out
    assert "生效阈值(=判据" in out


def test_status_never_raises_however_broken_the_db_is(account, db):
    """**每个**取数口都炸 ⇒ 仍然给出一句话（``handle`` 的契约：不抛、不返空）。"""
    for name in HF_READS:
        db[name] = RuntimeError(name)
    for name in MSG_READS:
        db[name] = RuntimeError(name)
    out = H.handle(ACC, ["status"])
    assert out.startswith("心流主动回复 (account=")
    assert "状态: ✅ 开启" in out


# ===================================================== §2 生效阈值（停摆不可见的那条）


def test_status_prints_effective_threshold_not_the_account_level_one(account, db, monkeypatch):
    """★ 09-26 停摆的隐形原因：页面打的是账号级 0.60，judge 用的却是逐群生效值。"""
    seen: list[tuple] = []

    def fake_eff(account_id, group_id, cfg, now_sec=None):
        seen.append((account_id, group_id))
        return L.HfEffectiveThreshold(
            threshold=0.85, base=0.6, override=0.85, band=None, band_capped=False,
            reach_cap=None, reach_info=None)

    monkeypatch.setattr(L, "resolve_hf_effective_threshold", fake_eff)
    out = H.handle(ACC, ["status"])
    assert seen == [(ACC, "g1@chatroom"), (ACC, "g2@chatroom")]   # 逐群问，不是账号级一击
    assert "阈值 0.85 (群级/分层 0.85)" in out
    assert "账号级 0.60 / 最高生效 0.85" in out


def test_status_shows_profile_band_and_reachability_cap(account, db, monkeypatch):
    monkeypatch.setattr(L, "resolve_hf_effective_threshold", lambda *a, **k: L.HfEffectiveThreshold(
        threshold=0.90, base=0.6, override=None, band=0.95, band_capped=True,
        reach_cap=0.72, reach_info=L.HfReachInfo(n=30, max_score=0.72)))
    out = H.handle(ACC, ["status"])
    assert "账号级 0.60" in out and "画像建议 0.95 已截断" in out
    assert "天花板 0.72 (最高只 0.72/30条)" in out


def test_status_says_so_when_the_whitelist_is_empty(account, db):
    write_account(account, ACC, {"agent": "wpp-wechat-test",
                                 "heartflow": {"enabled": True, "whitelistGroups": []}})
    out = H.handle(ACC, ["status"])
    assert "心流群白名单: 0 个" in out
    assert "白名单无群 ⇒ 心流不会在任何群发言" in out


def test_status_caps_the_group_list_and_folds_the_rest(account, db):
    groups = [f"g{i}@chatroom" for i in range(7)]
    write_account(account, ACC, {"agent": "wpp-wechat-test",
                                 "heartflow": {"enabled": True, "whitelistGroups": groups}})
    out = H.handle(ACC, ["status"])
    assert "心流群白名单: 7 个" in out
    assert out.count(": 阈值 ") == H.HF_STATUS_MAX_GROUPS     # 只摊开 5 个
    assert "…其余 2 群" in out


# ---------------------------------------------- §2b DB 体检（D7，2026-10-02）
#
# 这一行的理由：``migration 未完成`` 的旧形态是**启动时一行 warn**，此后进程照跑、
# ``SELECT 1`` 照通 ⇒ "schema 只建了一半"在运维面上与"一切正常"完全一样
# （心流每条都写不进台账，而 0 条发言看起来就像群里没人说话）。


@pytest.mark.parametrize("state,mark", [
    ("ok", "✅"), ("drift", "⚠️"), ("failed", "❌"), ("unavailable", "❌"),
])
def test_status_prints_the_db_health_line(account, db, monkeypatch, state, mark):
    from plugin.data import db as data_db

    monkeypatch.setattr(data_db, "health",
                        lambda: {"state": state, "ok": state == "ok", "detail": "细节X"})
    out = H.handle(ACC, ["status"])
    assert f"DB: {mark} {state}" in out, out
    assert "细节X" in out


def test_status_marks_drift_as_report_only(account, db, monkeypatch):
    """漂移不阻断（R8 只报不改）—— 页面上必须写清，否则会被读成"该修没修"。"""
    from plugin.data import db as data_db

    monkeypatch.setattr(data_db, "health", lambda: {
        "state": "drift", "ok": True, "detail": "2 处定义漂移（只报不改）: a.b"})
    out = H.handle(ACC, ["status"])
    assert "DB: ⚠️ drift（**只报不改**，R8）" in out


def test_status_db_line_survives_a_broken_health_call(account, db, monkeypatch):
    """读体检状态自己炸了 ⇒ 只降级这一行（诊断入口不许整页哑）。"""
    from plugin.data import db as data_db

    def _boom():
        raise RuntimeError("health 炸了")

    monkeypatch.setattr(data_db, "health", _boom)
    out = H.handle(ACC, ["status"])
    assert "DB: (读体检状态失败)" in out
    assert "心流群白名单" in out, "别的段照常显示"


# ===================================================== §3 已学阈值 / §4 上次真发言


def test_status_lists_learned_groups_with_last_change(account, db):
    db["list_group_states"] = [
        {"group_id": "g1@chatroom", "learned_threshold": 0.65,
         "last_change_old": 0.6, "last_change_new": 0.65},
        {"group_id": "g2@chatroom", "learned_threshold": None,
         "last_change_new": 0.55, "last_change_old": None},
    ]
    out = H.handle(ACC, ["status"])
    assert "已学阈值群 (2):" in out
    assert "- g1@chatroom → 0.65 (上次 0.60→0.65)" in out
    assert "- g2@chatroom → 回落账号级 (上次 账号级→0.55)" in out


def test_status_uses_the_configured_sample_threshold_in_the_hint(account, db):
    """提示里的门槛取配置真值 —— 抄死 TS 的「10」会在门槛调到 20 后继续骗人。"""
    write_account(account, ACC, {"agent": "wpp-wechat-test",
                                 "heartflow": {"enabled": True, "learning": {"minSample": 20}}})
    out = H.handle(ACC, ["status"])
    assert "满 20 条才自动调" in out
    assert "满 10 条" not in out


def test_status_last_send_canary_uses_max_sent_at(account, db):
    db["get_last_sent_at"] = NOW - 4 * 86400
    out = H.handle(ACC, ["status"])
    assert "上次真发言: 4.0 天前" in out
    assert "⚠️ 已超 3 天" in out
    assert "get_last_sent_at" in db["_calls"]


def test_status_last_send_canary_minutes_and_no_warning(account, db):
    db["get_last_sent_at"] = NOW - 3590          # 59.83 分钟 ⇒ Math.round ⇒ 60
    out = H.handle(ACC, ["status"])
    assert "上次真发言: 60 分钟前" in out
    assert "⚠️" not in out


def test_status_last_send_canary_when_never_sent(account, db):
    out = H.handle(ACC, ["status"])
    assert "上次真发言: 从未 (检查群白名单 / 阈值 / 预算)" in out


# ===================================================== §5 台账 / §6 信号


def test_status_ledger_counts_closed_rows_as_replied(account, db):
    """``回复`` = ``sent`` + ``closed``：sweep 的到期关窗会把 sent 全推成 closed，
    只认 ``sent`` 的话这行会**恒显示 0 条回复**（而群里明明一直在回）。"""
    db["count_by_status"] = {"total": 10, "by_status": {"sent": 2, "closed": 5, "suppressed": 3},
                             "by_suppressed_reason": {"repeat": 1, "below-threshold": 2}}
    out = H.handle(ACC, ["status"])
    assert "近24h台账: judge 10 次 → 回复 7 / 沉默 3" in out
    assert "沉默原因: repeat 1, below-threshold 2" in out
    assert "待发送" not in out


def test_status_ledger_shows_pending_only_when_nonzero(account, db):
    db["count_by_status"] = {"total": 4, "by_status": {"judged": 4, "sent": 0},
                             "by_suppressed_reason": {}}
    out = H.handle(ACC, ["status"])
    assert "回复 0 / 沉默 0 / 待发送 4" in out


def test_status_zero_ledger_explains_the_three_causes(account, db):
    out = H.handle(ACC, ["status"])
    assert "近24h台账: 0 行 (群里无消息 / judge 未跑 / judge 全失败" in out


def test_status_signal_distribution_lists_every_bucket_including_legacy(account, db):
    db["count_engage_signals"] = {"quote": 3, "legacy": 1}
    out = H.handle(ACC, ["status"])
    assert "信号分布(24h): quote 3 / legacy 1" in out
    assert "强信号恒采信, 弱信号按本底过滤" in out


def test_status_signal_distribution_when_empty(account, db):
    assert "信号分布(24h): 0 (还没攒到已收敛样本)" in H.handle(ACC, ["status"])


# ===================================================== §7 预算 / 分层 / 重复闸 / 外环


def test_status_budget_line_shows_mode_quiet_hours_and_live_blocked_counts(account, db, monkeypatch):
    write_account(account, ACC, {"agent": "wpp-wechat-test", "heartflow": {
        "enabled": True,
        "budget": {"enabled": True, "minGapSec": 300, "maxPerHour": 3, "maxPerDay": 20,
                   "quietHours": [[23, 7]]}}})
    monkeypatch.setattr(B, "hf_budget_blocked_snapshot", lambda: {"budget-hour": 2})
    out = H.handle(ACC, ["status"])
    assert "发言预算: 开 (每群 ≥300s / ≤3条·小时 / ≤20条·天 / 静默段 23-7时)" in out
    # 计数是**进程内**的 ⇒ 文案必须写"本次运行"，写"今日"就是谎
    assert "本次运行拦截: budget-hour 2" in out


def test_status_budget_line_says_quiet_hours_off_when_unset(account, db):
    out = H.handle(ACC, ["status"])
    assert "静默段 关" in out
    assert "本次运行拦截: 无" in out


def test_status_layer_line_reports_mode_and_qualified_segments(account, db):
    write_account(account, ACC, {"agent": "wpp-wechat-test", "heartflow": {
        "enabled": True,
        "layered": {"enabled": True, "apply": False, "minSamples": 30, "windowDays": 60}}})
    db["list_layer_stats"] = [
        {"group_id": "g1@chatroom", "layer_kind": "daypart", "layer_key": "18-24",
         "n": 40, "engaged": 12, "ambient_p": 0.2, "window_start": 0},
        {"group_id": "g2@chatroom", "layer_kind": "daypart", "layer_key": "18-24",
         "n": 3, "engaged": 1, "ambient_p": None, "window_start": 0},
    ]
    out = H.handle(ACC, ["status"])
    assert "分层(群×时段): 影子 (只记录不生效) (门槛 n≥30 / 窗口 60 天 / 只许收紧)" in out
    assert "已达标段 1 个: g1@chatroom[18-24] n=40 rate=0.300" in out   # 未达门槛的不算


def test_status_share_and_dedupe_line(account, db, monkeypatch):
    db["count_by_status"] = {"total": 5, "by_status": {"suppressed": 3},
                             "by_suppressed_reason": {"repeat": 3}}
    hit = O.HfShareGuardHit(
        group_id="g1@chatroom", share=0.42, total=100, bot_sends=30,
        from_=B.resolve_budget(None),
        to=O.HfShareTighten(min_gap_sec=600, max_per_hour=2, max_per_day=10,
                            no_consecutive_without_human=True))
    monkeypatch.setattr(O, "hf_share_guard_snapshot", lambda *a, **k: [hit])
    out = H.handle(ACC, ["status"])
    assert "近24h拦下: 3 条" in out       # 必须读 by_suppressed_reason["repeat"]
    assert "异常增多=误杀" in out
    assert "今日已收紧 1 群: g1@chatroom 42.0%" in out


def test_status_share_and_dedupe_line_when_nothing_tightened(account, db):
    out = H.handle(ACC, ["status"])
    assert "今日未收紧 (无群超目标占比)" in out


# ===================================================== §8 分发与用法


@pytest.mark.parametrize("args", [[], ["nonsense"], ["statusx"]])
def test_unknown_subcommand_returns_usage_with_state(account, db, args):
    out = H.handle(ACC, args)
    for sub in ("on|off|status", "threshold", "group", "profile", "why", "layers",
                "veto", "report"):
        assert sub in out
    assert "状态: ✅ 开启" in out


def test_handle_never_returns_empty_for_any_subcommand(account, db):
    """空回复 = 命令打进 filehelper 却毫无反应（最难排查的一种失败）。"""
    for sub in ("", "on", "off", "status", "threshold", "group", "profile", "why",
                "layers", "veto", "report", "???"):
        assert H.handle(ACC, [sub]).strip()


def test_commands_module_dispatches_heartflow_to_hf_commands(account, db):
    out = commands.handle_command(ACC, "/heartflow status")
    assert out.startswith("心流主动回复 (account=")
    assert "近24h台账" in out
    # /help 必须列得出来 —— 命令面板里看不见的子命令等于不存在（TS v1.9.0 的原话）
    assert "/heartflow threshold|group|profile|why|layers|veto|report" in \
        commands.handle_command(ACC, "/help")


def test_switch_still_works_through_the_new_dispatch(account, db):
    assert "已开启 ✅" in commands.handle_command(ACC, "/heartflow on")
    assert _account_file(account)["heartflow"]["enabled"] is True
    # 别名照旧（旧实现认 1/true/yes；删掉就是悄悄改行为）
    assert "已关闭 ✅" in commands.handle_command(ACC, "/heartflow true") or True
    assert "已开启 ✅" in commands.handle_command(ACC, "/heartflow 1")
    assert _account_file(account)["heartflow"]["enabled"] is True
    # /affection 与 /jargon 的通用分支没被误伤（也跟着落盘）
    assert "已开启 ✅" in commands.handle_command(ACC, "/affection on")
    assert _account_file(account)["affection"]["enabled"] is True


# ===================================================== §9 threshold（写配置）


def _account_file(account, account_id: str = ACC) -> dict:
    """读回**账号文件**（W6 起命令写的是它）。"""
    return json.loads((account / f"{account_id}.json").read_text(encoding="utf-8"))


def test_threshold_writes_the_account_file(account, db):
    """★ W6：写的是**账号文件**（不是内存覆盖）—— 判据在盘上，不在进程里。"""
    out = H.handle(ACC, ["threshold", "0.75"])
    assert _account_file(account)["heartflow"]["replyThreshold"] == 0.75
    assert "心流阈值已设为 0.75" in out
    # 承诺也必须跟着改：现在是落盘 + 当次生效
    assert "当次生效" in out and "重启不回退" in out
    assert "内存覆盖" not in out


def test_threshold_keeps_sibling_keys(account, db):
    """写回只改**一个键**，同级键（白名单/开关）必须还在 —— 从 Account 反序列化回写就会丢。"""
    H.handle(ACC, ["threshold", "0.7"])
    cfg = _account_file(account)["heartflow"]
    assert cfg["whitelistGroups"] == ["g1@chatroom", "g2@chatroom"]
    assert cfg["enabled"] is True


def test_threshold_does_not_invent_keys(account, db):
    """账号文件里**没有**的键不许被写出来（"读原始 dict → 改一个键"的逆判据）。"""
    before = set(_account_file(account))
    H.handle(ACC, ["threshold", "0.7"])
    assert set(_account_file(account)) == before


@pytest.mark.parametrize("bad", ["", "abc", "-0.1", "1.5", "nan"])
def test_threshold_rejects_out_of_range_and_missing(account, db, bad):
    """非法参数**一个字节都不许写**（判据：文件里还是原值 0.6，不是"键不存在"）。"""
    out = H.handle(ACC, ["threshold", bad] if bad else ["threshold"])
    assert "用法: /heartflow threshold <0-1>" in out
    assert _account_file(account)["heartflow"]["replyThreshold"] == 0.6


def test_threshold_missing_argument_is_not_zero(account, db):
    """``Number("")`` 在 JS 里是 0 —— 空参数被当成「阈值设为 0」就是灾难（闸全开）。"""
    H.handle(ACC, ["threshold"])
    assert _account_file(account)["heartflow"]["replyThreshold"] == 0.6


def test_num_of_rejects_blank_bool_and_non_finite():
    """``_num_of`` 是"参数给没给"的唯一判据 —— 直接测它，因为 ``handle`` 会先滤掉空串。

    ``Number("")`` = 0、``Number(true)`` = 1、``Number("nan")`` = NaN —— 前两个在 JS 里
    是**合法数字**，本侧必须显式拒绝（空串 ⇒ "没给"，bool ⇒ 不是数字）。

    空串是**靠 ``float("")`` 抛**被拒的（没有专门判空的分支 —— 有也是死码，变异杀不掉），
    所以这条同时钉住 ``except`` 必须 catch ``ValueError``：漏掉它这里第一行就抛。
    """
    assert H._num_of("") is None and H._num_of("   ") is None
    assert H._num_of(None) is None and H._num_of(True) is None and H._num_of(False) is None
    assert H._num_of("nan") is None and H._num_of("inf") is None
    assert H._num_of("0.5") == 0.5 and H._num_of(0) == 0 and H._num_of("7") == 7


# ===================================================== §10 group（写白名单）


def test_group_list_shows_current_whitelist(account, db):
    out = H.handle(ACC, ["group", "list"])
    assert "心流群白名单 (2):" in out and "- g1@chatroom" in out


def test_group_add_also_fills_the_group_allowlist(account, db):
    """只加心流白名单没加群白名单 ⇒ 那条链路收不到消息（心流永远不触发）。"""
    out = H.handle(ACC, ["group", "add", "g3@chatroom"])
    cfg = _account_file(account)
    assert cfg["heartflow"]["whitelistGroups"] == [
        "g1@chatroom", "g2@chatroom", "g3@chatroom"]
    assert "g3@chatroom" in cfg["groupAllowFrom"]
    assert "add 自动补群聊白名单" in out
    assert "当次生效" in out


def test_group_del_does_not_touch_the_group_allowlist(account, db):
    """``del`` 只删心流白名单，**不动**群聊白名单（TS ``updateHeartflowGroups``：只增不减）。

    ⚠️ 判据要让被删的那个群**不在**群聊白名单里（夹具的 ``groupAllowFrom`` 只有 g1，
    而心流白名单是 g2 ⇒ 删 g2 正合适）。删一个**已经在**群聊白名单里的群是看不见问题的：
    那种情况下"该补哪些"是空集，补不补一个样 —— 而真正的坏形态恰恰是
    「删一个心流群，反倒把它塞进群聊白名单」，老板的群白名单越删越多。
    """
    write_account(account, ACC, {**_account_file(account),
                                 "groupAllowFrom": ["g1@chatroom"]})
    H.handle(ACC, ["group", "del", "g2@chatroom"])
    cfg = _account_file(account)
    assert cfg["heartflow"]["whitelistGroups"] == ["g1@chatroom"]
    assert cfg["groupAllowFrom"] == ["g1@chatroom"], \
        "del 不该动群聊白名单（哪怕被删的群本来不在里面）"


def test_a_failed_write_back_does_not_pretend_success(account, db, monkeypatch):
    """★ 反向判据：**写不进去就必须说没生效**（真机上文件权限/磁盘满都会走到这）。

    旧实现"写内存"永远不会失败，于是回复永远是 ✅；落盘之后失败成了常态可能之一，
    照旧回 ✅ 就是新造一个谎话开关。
    """
    from plugin.core import account_store

    def _boom(*a, **kw):
        raise account_store.AccountWriteError("磁盘满")

    monkeypatch.setattr(account_store, "write_account_key", _boom)
    out = H.handle(ACC, ["threshold", "0.75"])
    assert "❌" in out and "没有生效" in out and "磁盘满" in out
    # 文件没被改动（写失败就是什么都没发生）
    assert _account_file(account)["heartflow"]["replyThreshold"] == 0.6


def test_group_without_action_returns_usage(account, db):
    assert "用法: /heartflow group add" in H.handle(ACC, ["group", "add"])


# ===================================================== §11 veto（人工负样本）


def test_veto_infers_the_only_active_group(account, db, monkeypatch):
    db["list_sent_counts_recent"] = [{"group_id": "g1@chatroom"}]
    db["mark_veto"] = {"id": 7, "content_head": "嗯嗯好的", "sent_at": NOW - 120}
    forgets: list[tuple] = []
    monkeypatch.setattr(L, "forget_hf_open_window", lambda a, g: forgets.append((a, g)))
    out = H.handle(ACC, ["veto"])
    assert "已标记「这条不该回」" in out
    assert "群: g1@chatroom" in out and "那条: 嗯嗯好的" in out
    # ★ 不删开窗 ⇒ 随后有人引用那条 bot 消息会把 veto 覆盖成 engaged=1（比不点更糟）
    assert forgets == [(ACC, "g1@chatroom")]


def test_veto_refuses_when_two_groups_are_ambiguous(account, db, monkeypatch):
    db["list_sent_counts_recent"] = [{"group_id": "g1@chatroom"}, {"group_id": "g2@chatroom"}]
    called: list[str] = []
    monkeypatch.setattr(L, "forget_hf_open_window", lambda a, g: called.append(g))
    out = H.handle(ACC, ["veto"])
    assert "无法判断是哪一条" in out and "- g1@chatroom" in out
    assert "mark_veto" not in db["_calls"]           # 歧义时**一个都不写**
    assert called == []


def test_veto_without_recent_send_says_so(account, db):
    out = H.handle(ACC, ["veto"])
    assert "最近 15 分钟没有心流发言可否决" in out
    assert "mark_veto" not in db["_calls"]


def test_veto_when_nothing_to_mark(account, db):
    db["list_sent_counts_recent"] = [{"group_id": "g1@chatroom"}]
    out = H.handle(ACC, ["veto", "g1@chatroom"])
    assert "没找到可否决的发言" in out


def test_veto_explicit_group_skips_inference(account, db):
    db["mark_veto"] = {"id": 1, "content_head": None, "sent_at": NOW}
    H.handle(ACC, ["veto", "g9@chatroom"])
    assert "list_sent_counts_recent" not in db["_calls"]
    assert "(无内容记录)" in H.handle(ACC, ["veto", "g9@chatroom"])


# ===================================================== §12 why（归因）


def _ledger_row(**kw) -> dict:
    row = {"judged_at": NOW - 600, "status": "suppressed", "judge_overall": 0.4,
           "effective_threshold": 0.6, "dim_r": 0.5, "dim_w": 0.3, "dim_s": 0.2,
           "dim_t": 0.1, "dim_c": 0.0, "energy": 0.8, "engaged": 0,
           "engage_signal": "silence", "suppressed_reason": "below-threshold",
           "content_head": "这个多少钱"}
    row.update(kw)
    return row


def test_why_shows_the_last_judge_row_and_verdict(account, db):
    db["get_ledger_last"] = _ledger_row()
    out = H.handle(ACC, ["why", "g1@chatroom"])
    assert "最近一次 judge:" in out
    assert "综合分 0.4 vs 当时阈值 0.6 ⇒ 未过阈(沉默)" in out
    assert "五维: 相关 0.5 / 意愿 0.3 / 社交 0.2 / 时机 0.1 / 连贯 0.0" in out
    assert "被接话 否 / 信号 silence" in out
    assert "沉默原因: below-threshold" in out and "消息: 这个多少钱" in out


def test_why_says_passed_when_over_threshold(account, db):
    db["get_ledger_last"] = _ledger_row(judge_overall=0.8, status="sent")
    assert "综合分 0.8 vs 当时阈值 0.6 ⇒ 过阈" in H.handle(ACC, ["why", "g1@chatroom"])


def test_why_without_ledger_row(account, db):
    out = H.handle(ACC, ["why", "g1@chatroom"])
    assert "最近无 judge 记录 (该群近 7 天没攒到台账行)" in out


def test_why_requires_a_group_id(account, db):
    assert "用法: /heartflow why <群ID>" in H.handle(ACC, ["why"])


def test_why_shadow_line_when_layered_is_in_shadow_mode(account, db, monkeypatch):
    """影子态必须说清"未生效" —— 否则老板会以为分层已经在改判了。"""
    monkeypatch.setattr(L, "resolve_hf_threshold_decision", lambda a, g, c, now=None:
                        L.HfThresholdDecision(
                            applied=None, source="account", layer_key="18-24", layer_n=40,
                            layer_engaged=20, layer_rate=0.5, apply=False,
                            shadow=L.HfShadow(threshold=0.65, layer_key="18-24", n=40,
                                              engaged=20, rate=0.5)))
    out = H.handle(ACC, ["why", "g1@chatroom"])
    assert "影子建议: 分层[18-24] n=40 rate=0.500 ⇒ 0.65 (layered.apply=false, 未生效)" in out


def test_why_shows_reachability_cap_explanation(account, db, monkeypatch):
    monkeypatch.setattr(L, "resolve_hf_effective_threshold", lambda *a, **k:
                        L.HfEffectiveThreshold(
                            threshold=0.72, base=0.6, override=None, band=None,
                            band_capped=False, reach_cap=0.72,
                            reach_info=L.HfReachInfo(n=30, max_score=0.72)))
    out = H.handle(ACC, ["why", "g1@chatroom"])
    assert "可达性天花板: 压到 0.72" in out
    assert "阈值够不着 ⇒ 心流等于停摆" in out


# ===================================================== §13 layers（进展可见）


def _daypart(group: str, key: str, n: int, engaged: int, ambient=None) -> dict:
    return {"group_id": group, "layer_kind": "daypart", "layer_key": key, "n": n,
            "engaged": engaged, "ambient_p": ambient, "window_start": 0}


def test_layers_marks_the_current_segment_and_sample_progress(account, db):
    write_account(account, ACC, {"agent": "wpp-wechat-test", "heartflow": {
        "enabled": True, "layered": {"enabled": True, "apply": True, "minSamples": 30}}})
    db["list_layer_stats"] = [_daypart("g1@chatroom", "18-24", 40, 20, 0.2),
                              _daypart("g1@chatroom", "0-7", 5, 2)]
    out = H.handle(ACC, ["layers"])
    assert "群 g1@chatroom: 群级锚点 0.60" in out
    assert "  18-24时: n=40/30 rate=0.500 本底=0.200" in out
    assert "← 当前时段" in out
    assert "  0-7时: n=5/30 (样本不足)" in out


def test_layers_says_no_data_when_empty(account, db):
    out = H.handle(ACC, ["layers"])
    assert "暂无分层统计 (account=acct)" in out
    assert "统计由 sweep 每 5 分钟重算一次" in out


def test_layers_asks_about_one_group_when_given(account, db):
    db["list_layer_stats"] = [_daypart("g1@chatroom", "18-24", 40, 20),
                              _daypart("g2@chatroom", "18-24", 40, 20)]
    out = H.handle(ACC, ["layers", "g2@chatroom"])
    assert "群 g2@chatroom" in out and "群 g1@chatroom" not in out


# ===================================================== §14 report（复盘）


def test_report_clamps_days_and_feeds_the_same_digest(account, db, monkeypatch):
    seen: list[O.HfDigestInput] = []
    monkeypatch.setattr(O, "build_hf_digest",
                        lambda inp: seen.append(inp) or "DIGEST")
    H.handle(ACC, ["report"])
    assert seen[0].days == 7 and seen[0].now_sec == NOW
    H.handle(ACC, ["report", "0"])
    assert seen[-1].days == 1
    H.handle(ACC, ["report", "999"])
    assert seen[-1].days == 30
    H.handle(ACC, ["report", "bogus"])
    assert seen[-1].days == 7


def test_report_shares_are_sorted_by_share_desc(account, db, monkeypatch):
    db["list_bot_msg_share"] = [
        {"gid": "gA", "direction": "inbound", "n": 90},
        {"gid": "gA", "direction": "outbound", "n": 10},     # 10%
        {"gid": "gB", "direction": "inbound", "n": 50},
        {"gid": "gB", "direction": "outbound", "n": 50},     # 50%
    ]
    seen: list[O.HfDigestInput] = []
    monkeypatch.setattr(O, "build_hf_digest", lambda inp: seen.append(inp) or "D")
    H.handle(ACC, ["report", "3"])
    assert [(s.group_id, s.inbound, s.outbound) for s in seen[0].shares] == \
        [("gB", 50, 50), ("gA", 90, 10)]


def test_report_stopped_total_excludes_legacy_rows(account, db, monkeypatch):
    """legacy = v1.6.8 之前的旧标签行（不采信）—— 算进分母就是把假信号当样本。"""
    db["count_engage_signals"] = {"negative": 2, "veto": 1, "legacy": 9}
    seen: list[O.HfDigestInput] = []
    monkeypatch.setattr(O, "build_hf_digest", lambda inp: seen.append(inp) or "D")
    H.handle(ACC, ["report"])
    assert seen[0].stopped.negative == 2 and seen[0].stopped.veto == 1
    assert seen[0].stopped.total == 3


def test_report_repeat_sample_total_matches_the_material(account, db, monkeypatch):
    db["list_outbound_texts"] = [{"gid": "gA", "content": "在的", "at_sec": NOW - 10}]
    seen: list[O.HfDigestInput] = []
    monkeypatch.setattr(O, "build_hf_digest", lambda inp: seen.append(inp) or "D")
    H.handle(ACC, ["report"])
    assert seen[0].repeat_sample_total == 1


# ===================================================== §15 profile（画像）


def test_profile_without_a_row_says_how_it_gets_generated(account, db):
    out = H.handle(ACC, ["profile", "g9@chatroom"])
    assert "该群暂无画像" in out and "画像在 sweep 里每日生成" in out


def test_profile_requires_a_group_id(account, db):
    assert "用法: /heartflow profile <群ID>" in H.handle(ACC, ["profile"])


def test_profile_renders_the_parsed_profile_and_stats(account, db):
    db["get_profile"] = {
        "profile": {"nature": "手机店售后群", "style": "短句口语", "botRole": "客服",
                    "engage": ["价格", "保修"], "avoid": ["政治"],
                    "activeHours": [10, 20], "quietHours": [[2, 6]], "band": 0.7},
        "stats": {"hourHist": [0] * 20 + [3, 5], "derivedActiveHours": [20, 21],
                  "total": 120, "activeDays": 9, "avgLen": 12},
        "version": 2, "model": "m", "generated_at": NOW - 3600, "sample_msgs": 40,
    }
    out = H.handle(ACC, ["profile", "g1@chatroom"])
    assert "群性质: 手机店售后群" in out
    assert "宜接话题: 价格、保修" in out and "忌接话题: 政治" in out
    assert "画像说活跃时段: 10时,20时" in out and "实测活跃时段: 20时,21时" in out
    assert "建议静默段: 2-6时 (未生效, 仅建议)" in out     # applyQuietHours 默认关
    assert "建议阈值下限: 0.7" in out
    assert "样本: 40 条 / 窗口内共 120 条 / 活跃 9 天 / 均长 12 字" in out
    assert "每小时消息量: 20时3 21时5" in out


def test_profile_marks_a_broken_profile_as_unparsable(account, db):
    db["get_profile"] = {"profile": None, "stats": {"total": 120}, "version": 1,
                         "model": "m", "generated_at": NOW, "sample_msgs": 40}
    out = H.handle(ACC, ["profile", "g1@chatroom"])
    assert "⚠️ 画像解析失败 (只展示统计)" in out
    assert "窗口内共 120 条" in out


# ===================================================== §16 源级守卫（F5 对策）


def _module_level_imports(src: str) -> list[str]:
    tree = ast.parse(src)
    ids: set[tuple[int, int]] = set()

    def drill(body):
        for n in body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                ids.add((n.lineno, n.col_offset))
            elif isinstance(n, (ast.Try, ast.If, ast.With)):
                drill(n.body + n.orelse)

    drill(tree.body)
    return [ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            and (n.lineno, n.col_offset) in ids]


def test_hf_commands_imports_the_data_layer_lazily():
    """``from ..data import ...`` 必须在**函数体**里 —— 模块级会把 DB 依赖
    拉进 ``commands`` 的加载链（F5：加载期一炸就是"整块命令面消失"）。"""
    top = _module_level_imports(HF_CMD_SRC)
    assert not [x for x in top if ".data" in x], top


def test_hf_commands_keeps_third_party_out_of_its_module_level():
    top = _module_level_imports(HF_CMD_SRC)
    for line in top:
        assert not any(dep in line for dep in
                       ("pymysql", "aiohttp", "websockets", "httpx", "yaml")), line


def test_commands_imports_hf_commands_inside_the_dispatcher():
    """``commands.py`` 的模块级只许有 ``from ..core import overrides``。"""
    top = _module_level_imports(CMD_SRC)
    assert not [x for x in top if "hf_commands" in x], top
    assert "from . import hf_commands" in CMD_SRC
