"""心流分层学习回归 —— TS ``tests/unit/heartflow-layer.test.mjs`` (516 行) 的 pytest 译本。

## 这一批最容易"假绿"的三处，各配一条**行为级**回归门（不是文本断言）

1. **分层桶必须是每轮全量重算**（I3）：同一批输入连跑两次，第二次必须 **0 写**。
   累加实现会在第二次又写一遍（``n`` 翻倍）—— 那条断言就是门。
2. **veto 必须真能进统计**（``veto`` 漏出信号值域 ⇒ ``as_hf_engage_signal`` 返回 ``None``
   ⇒ 整行被当 legacy 丢掉，**静默无效**）。值域本身由 ``test_label.py`` 逐个字面钉住。
3. **读侧判定必须纯内存**（judge 热路径零 DB IO）：源级断言体内无 import / 无 SQL。

真实日期/生产数字一律不进本文件（公开仓红线）；样本全是构造的。

## 与 TS 版的对应

=======  ====================================================================
1–5      本文件 §1–§5 一一对应（分段 / 聚合 / 幂等 / 读侧 / 配置面）
6       TS 的 §5「源级接线守卫」拆成两侧：
        · **本文件 §6**（layer 侧）：不许反向 import learn、SQL 单表、
          判定热路径无 IO、加载期无第三方依赖、无真实群 ID；
        · **``test_learn.py``**（learn 侧）：sweep 顺序（画像 < 分层 < learning 开关）、
          ``resolveHfThresholdDecision`` 的七条决策用例、``heartflow.ts`` 的
          ``import type``/``layered`` 子块落点 —— 那些的主语是 learn/配置域，还没移植。
        **故意不放"永远 skip"的壳**：跳过看起来像通过，比没有更糟。
=======  ====================================================================
"""

from __future__ import annotations

import ast
import logging
import math
import time
from datetime import datetime
from pathlib import Path

import pytest

from plugin.data import repo_hf, repo_messages
from plugin.inbound.heartflow import layer as L
from plugin.inbound.heartflow import label as LBL
from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

LAYER_SRC = Path(L.__file__).read_text(encoding="utf-8")
REPO_HF_SRC = Path(repo_hf.__file__).read_text(encoding="utf-8")


def at(h: int, m: int = 0, day: int = 20) -> int:
    """本地时刻 → unix 秒（用本地字段构造，与 ``hf_local_hour`` 同语义）。"""
    return int(datetime(2026, 9, day, h, m, 0).timestamp())


NOW = at(20, 30)                  # 20:30 ⇒ 落在 18-24 段
W60 = NOW - 60 * 86400            # 默认 window_days=60 的窗口起点
DEFAULT_B = ((0, 7), (7, 12), (12, 18), (18, 24))


def sample(gid: str, signal: str | None, hour: int, engaged: int = 0,
           minute: int = 0) -> L.HfLayerSample:
    """给**纯函数**用的样本对象。"""
    return L.HfLayerSample(group_id=gid, engaged=engaged, engage_signal=signal,
                           sent_at=at(hour, minute))


def dbrow(gid: str, signal: str | None, hour: int, engaged: int = 0,
        minute: int = 0) -> dict:
    """给**假数据层**用的行 —— 形状与 ``repo_hf.list_closed_since`` 的返回**逐列一致**
    （少一列就会在 ``from_db_row`` 那层被静默吞成默认值，测试也就跟着失真）。"""
    return dict(group_id=gid, engaged=engaged, engage_signal=signal,
                sent_at=at(hour, minute))


def opts(ambient: dict[str, float] | None = None, **kw) -> L.HfLayerAggregateOpts:
    return L.HfLayerAggregateOpts(
        buckets=kw.pop("buckets", DEFAULT_B),
        ambient_p_by_group_hour=ambient or {},
        ambient_max=kw.pop("ambient_max", 0.5),
        window_start=kw.pop("window_start", W60),
        **kw,
    )


def cell(rows, gid: str, key: str):
    for r in rows:
        if r.group_id == gid and r.layer_key == key:
            return r
    return None


# ===== 1. 纯函数：分段 =====


def test_default_buckets_are_four_and_left_closed_right_open():
    B = L.normalize_hf_buckets(None)
    assert B == DEFAULT_B, "默认四段"
    assert L.hf_layer_key_for(0, B) == "0-7", "0 点归 0-7"
    assert L.hf_layer_key_for(6, B) == "0-7"
    assert L.hf_layer_key_for(7, B) == "7-12", "7 点归下一段（右开）"
    assert L.hf_layer_key_for(18, B) == "18-24"
    assert L.hf_layer_key_for(23, B) == "18-24"
    # 24 点不存在（0-23）；越界与非法输入返回 None（不抛）
    assert L.hf_layer_key_for(24, B) is None
    assert L.hf_layer_key_for(float("nan"), B) is None
    assert L.hf_layer_key_for("20", B) is None, "非数不猜"
    assert L.hf_layer_key_for(3, ((9, 17),)) is None, "未被任何段覆盖 ⇒ None（该样本不进任何桶）"


@pytest.mark.parametrize("bad", [
    [[7, 3]],                        # 倒置
    [[0, 7], [6, 12]],               # 重叠
    [[0, 25]],                       # 越界
    [[-1, 7]],                       # 负数
    [[1.5, 7]],                      # 非整数
    [[0, 7, 9]],                     # 不是二元组
    [],                              # 空
    [[0, 3], [3, 6], [6, 9], [9, 12], [12, 15], [15, 18], [18, 21]],   # 超 6 段
    "x", 3,                          # 非数组
    [[None, 7]],                     # JS 会当 0-7，Py 侧故意判非法（见 _as_int_like）
    [[True, 7]],                     # JS 会当 1-7，同上
])
def test_normalize_falls_back_wholesale_on_dirty_input(bad):
    """非法输入必须**整体**回落默认 —— 半套分段会让 ``layer_key`` 的语义不可预测。"""
    assert L.normalize_hf_buckets(bad) == DEFAULT_B


def test_normalize_accepts_legal_input_sorted_and_numeric_strings():
    assert L.normalize_hf_buckets([[18, 24], [0, 7]]) == ((0, 7), (18, 24)), "合法输入按起始排序"
    assert L.normalize_hf_buckets([["18", "24"], [0, 7]]) == ((0, 7), (18, 24)), (
        "数字字符串算整数（对齐 JS Number()）")


def test_max_buckets_is_six():
    assert L.HF_LAYER_MAX_BUCKETS == 6
    six = [[i, i + 1] for i in range(6)]
    assert L.normalize_hf_buckets(six) == tuple((i, i + 1) for i in range(6))


# ===== 2. 纯函数：聚合取舍 =====


def test_aggregate_drops_legacy_no_time_unknown_and_out_of_whitelist():
    rows = L.aggregate_hf_layer_stats(
        [
            sample("gA", "silence", 20, engaged=0),
            sample("gA", "short-window", 21, engaged=1),
            sample("gA", None, 20, engaged=1),           # 旧行（无信号）⇒ 丢
            L.HfLayerSample("gA", 1, "veto", None),     # 无时刻 ⇒ 丢
            sample("gA", "bogus", 20, engaged=1),        # 未知信号 ⇒ 丢
            L.HfLayerSample("gA", 1, "quote", 0),        # sent_at=0 ⇒ 丢
            sample("gB", "quote", 20, engaged=1),        # 强信号
            sample("gB", "short-window", 20, engaged=0),  # 弱 + 本底高 ⇒ 丢
            sample("gC", "silence", 3, engaged=0),       # 白名单外 ⇒ 整群不出行
            sample("", "quote", 20, engaged=1),          # 空 group_id ⇒ 丢
        ],
        opts({"gB|20": 0.9}, group_filter=lambda g: g != "gC", also_groups=["gD", "gC", ""]),
    )
    assert cell(rows, "gA", "18-24").n == 2, "gA 18-24: silence + short-window（旧行/无时刻/未知被丢）"
    assert cell(rows, "gA", "18-24").engaged == 1
    assert cell(rows, "gA", "0-7").n == 0, "空段也要出行（前端要显示进度）"
    assert cell(rows, "gA", "7-12").n == 0, "无时刻的那行不许落到 7-12（sent_at=0 是 epoch 0）"
    # 总数把这群**所有**段加起来 —— 逐段点名的写法会漏掉"多出一行落在没点到的段里"，
    #   这一次就漏过一次：`sent_at=0` 在本地时区是 08:00 而不是 00:00，落到 7-12 段，
    #   而当时只点名断言了 0-7 ⇒ 变异存活。
    assert sum(r.n for r in rows if r.group_id == "gA") == 2, "gA 全部段落合计只能是 2 条样本"
    assert cell(rows, "gB", "18-24").n == 1, "gB 本底 0.9：弱信号被丢，强信号(quote)留下"
    assert cell(rows, "gB", "18-24").engaged == 1
    assert cell(rows, "gB", "18-24").ambient_p == 0.9, "段本底 = 该段各小时 ambientP 的最大值"
    assert cell(rows, "gC", "0-7") is None, "白名单外的群整群不出行（即使用 also_groups 点名）"
    assert cell(rows, "gD", "0-7") is not None and cell(rows, "gD", "0-7").n == 0, "空群也出行"
    assert all(r.layer_kind == "daypart" for r in rows)
    assert all(r.window_start == W60 for r in rows)
    assert [r.group_id for r in rows] == sorted(r.group_id for r in rows), "群按名排序（输出确定）"


def test_aggregate_filters_ambient_per_sample_hour_not_bucket_average():
    """本底过滤是**逐样本用自己那一小时**的 ambientP —— 不是段平均值。

    段内 23 点冷清、18 点热闹：用平均值（或段最大值）会把 23 点那条**本可采信**的弱样本
    一起丢掉，于是冷清时段的统计永远是 0（分层学不到东西，而且**没有任何报错**）。
    """
    rows = L.aggregate_hf_layer_stats(
        [
            sample("gA", "short-window", 23, engaged=1),   # 23 点冷清 ⇒ 采信
            sample("gA", "short-window", 18, engaged=0),   # 18 点热闹 ⇒ 丢
        ],
        opts({"gA|18": 0.9}),
    )
    assert cell(rows, "gA", "18-24").n == 1, "只有 23 点那条留下"
    assert cell(rows, "gA", "18-24").engaged == 1
    assert cell(rows, "gA", "18-24").ambient_p == 0.9, "段本底仍取该段最大值（展示用）"


def test_veto_counts_as_engaged_zero_and_ignores_ambient():
    """veto 是人工强负样本，**恒可采信**（冷清群里恰恰最需要被记住）。"""
    rows = L.aggregate_hf_layer_stats(
        [sample("gA", "veto", 9, engaged=0)],
        opts({"gA|9": 0.99}, ambient_max=0.5),
    )
    c = cell(rows, "gA", "7-12")
    assert c.n == 1, "veto 不受本底过滤"
    assert c.engaged == 0
    assert LBL.is_hf_sample_informative("veto", 0.99, 0.5) is True, "口径来自 label 的强信号表"


def test_aggregate_without_group_filter_allows_everyone():
    rows = L.aggregate_hf_layer_stats([sample("gZ", "quote", 20, engaged=1)], opts())
    assert cell(rows, "gZ", "18-24").n == 1


# ===== 3. 幂等（核心回归门：防"累加桶"复活）+ 取数缓存 =====


class FakeRepo:
    """假数据层：记调用、可注入返回值；**签名按位置固定**（错位会立刻炸）。"""

    def __init__(self, *, samples=(), buckets=(), layer_rows=(), hour_raises=False,
                 closed_raises=False):
        self.samples = list(samples)
        self.buckets = list(buckets)
        self.layer_rows = list(layer_rows)
        self.hour_raises = hour_raises
        self.closed_raises = closed_raises
        self.closed_calls: list[tuple] = []
        self.upserts: list[dict] = []
        self.hour_calls: list[tuple] = []
        self.layer_calls: list[str] = []

    def list_closed_since(self, account_id, since_sec, limit):
        self.closed_calls.append((account_id, since_sec, limit))
        if self.closed_raises:
            raise RuntimeError("boom-closed")
        return list(self.samples)

    def upsert_layer_stat(self, account_id, group_id, layer_kind, layer_key, n, engaged,
                          window_start, *, ambient_p=None):
        self.upserts.append(dict(account_id=account_id, group_id=group_id, layer_kind=layer_kind,
                                 layer_key=layer_key, n=n, engaged=engaged,
                                 window_start=window_start, ambient_p=ambient_p))

    def list_layer_stats(self, account_id, layer_kind=None):
        self.layer_calls.append(account_id)
        return list(self.layer_rows)

    def list_group_msg_hour_buckets(self, account_id, since_sec, local_offset_sec):
        self.hour_calls.append((account_id, since_sec, local_offset_sec))
        if self.hour_raises:
            raise RuntimeError("boom-hour")
        return list(self.buckets)


@pytest.fixture
def fake(monkeypatch):
    f = FakeRepo()
    monkeypatch.setattr(repo_hf, "list_closed_since", f.list_closed_since)
    monkeypatch.setattr(repo_hf, "upsert_layer_stat", f.upsert_layer_stat)
    monkeypatch.setattr(repo_hf, "list_layer_stats", f.list_layer_stats)
    monkeypatch.setattr(repo_messages, "list_group_msg_hour_buckets",
                        f.list_group_msg_hour_buckets)
    return f


def recompute(account_id="acct", cfg=None, now=NOW, **kw):
    return L.maybe_recompute_hf_layer_stats(
        account_id, cfg, now,
        label_window_sec=kw.pop("label_window_sec", 60),
        ambient_max=kw.pop("ambient_max", 0.5),
        **kw)


def test_recompute_is_idempotent_second_run_writes_zero(fake: FakeRepo):
    """**I3 的落点**：同样输入连跑两次 ⇒ 第二次 0 写。

    累加实现/闭包持有旧状态的实现会在第二次再写一遍（``n`` 翻倍），
    而 sweep 每 300s 跑一次 ⇒ ``min_samples`` 门槛形同虚设且**永远无法自愈**。
    """
    fake.samples = [dbrow("gA", "silence", 20), dbrow("gA", "silence", 21),
                    dbrow("gA", "short-window", 22, engaged=1)]
    n1 = recompute()
    assert n1 == 4, "首轮必须写满 4 段（3 段空 + 1 段有样本）"
    assert len(fake.upserts) == n1, "写库次数 === 返回值"
    assert fake.closed_calls[0] == ("acct", W60, L.HF_LAYER_DB_ROW_CAP), "取数与窗口参数"

    eighteen = [w for w in fake.upserts if w["layer_key"] == "18-24"][0]
    assert eighteen["n"] == 3, "18-24 段 n=3（三个样本落在 20/21/22 点）"
    assert eighteen["engaged"] == 1
    assert eighteen["account_id"] == "acct" and eighteen["layer_kind"] == "daypart"
    assert eighteen["window_start"] == W60

    n2 = recompute()
    assert n2 == 0, "第二轮必须 0 写 —— 这一条就是防线"
    assert len(fake.upserts) == n1

    got = L.get_hf_layer_stat("acct", "gA", NOW, DEFAULT_B)
    assert got is not None and got.key == "18-24"
    assert got.stat.n == 3, "缓存里也是重算值（读侧与库一致）"


def test_window_roll_rewrites_every_row(fake: FakeRepo):
    """``window_start`` 必须进写库签名 —— 它每轮都在滚。

    签名里漏掉它，窗口滚动后行**永不重写**：``n`` 停在旧值，而"窗口上限 60 天"这个
    防锁死机制就名存实亡（旧样本永远留在桶里，正是 I3 要防的第二个不可逆点）。
    """
    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    assert recompute() == 4
    fake.upserts.clear()
    assert recompute(now=NOW + 86400) == 4, "窗口起点变了 ⇒ 四段全部重写"
    assert all(w["window_start"] == NOW + 86400 - 60 * 86400 for w in fake.upserts)


def test_recompute_disabled_sends_no_query_at_all(fake: FakeRepo):
    n = recompute(cfg={"layered": {"enabled": False}})
    assert n == 0
    assert fake.closed_calls == [], "开关关掉就不该打 DB（连查询都不发）"
    assert fake.hour_calls == []


def test_recompute_passes_cap_to_query_and_warns_when_hit(fake: FakeRepo, monkeypatch, caplog):
    monkeypatch.setattr(L, "HF_LAYER_DB_ROW_CAP", 2)
    fake.samples = [dbrow("gA", "quote", 20, engaged=1), dbrow("gA", "quote", 21, engaged=1)]
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        recompute()
    assert fake.closed_calls[0][2] == 2, "上限必须透传给取数（两处口径同一份）"
    assert any("触顶" in r.message for r in caplog.records), "触顶要吵，不能静默偏高"


def test_recompute_window_days_flows_into_window_start(fake: FakeRepo):
    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    recompute(cfg={"layered": {"windowDays": 7}})
    assert fake.closed_calls[0][1] == NOW - 7 * 86400
    assert fake.upserts[0]["window_start"] == NOW - 7 * 86400


def test_ambient_failure_degrades_to_no_filter(fake: FakeRepo, caplog):
    """本底取数失败**不该**拖垮统计：退化成"不过滤本底"，并如实 warn。"""
    fake.hour_raises = True
    fake.samples = [dbrow("gA", "short-window", 20, engaged=1)]
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        n = recompute()
    assert n == 4, "本底拿不到也照样出统计"
    row = [w for w in fake.upserts if w["layer_key"] == "18-24"][0]
    assert row["n"] == 1, "不过滤 ⇒ 弱信号也算"
    assert any("本底取数失败" in r.message for r in caplog.records)


def test_also_groups_keeps_previous_groups_visible(fake: FakeRepo):
    """上一轮有样本、这一轮没有的群，仍要出行（否则 ``/heartflow layers`` 里那行会"凭空消失"）。"""
    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    n1 = recompute()
    assert [w["n"] for w in fake.upserts if w["layer_key"] == "18-24"] == [1]

    fake.upserts.clear()
    fake.samples = []
    n2 = recompute()
    assert n2 == 1, "只有 18-24 段的内容变了（1→0），其余三段本来就是 0 ⇒ 不重复写"
    assert [w["n"] for w in fake.upserts if w["layer_key"] == "18-24"] == [0], "该段的 n 归零"
    assert len(fake.upserts) == 1 and fake.upserts[0]["group_id"] == "gA", "该群没被踢出输出"
    assert L.hf_layer_keys_cached("acct", "gA") == ["0-7", "12-18", "18-24", "7-12"], "群仍在缓存里"
    assert n1 > 0 and L.get_hf_layer_stat("acct", "gA", NOW, DEFAULT_B).stat.n == 0, "读侧同步归零"


def test_hour_counts_are_cached_within_ttl(fake: FakeRepo):
    """同一轮 sweep 的两个 pass 只查一次 DB（TTL 120s）。"""
    fake.buckets = [{"group_id": "gA", "h": 20, "n": 5}]
    a = L.load_hf_group_hour_counts("acctC", NOW)
    b = L.load_hf_group_hour_counts("acctC", NOW + 5)
    assert len(fake.hour_calls) == 1, "120s 内的第二次调用必须命中缓存"
    assert a == {"gA|20": 5} and b == {"gA|20": 5}
    assert fake.hour_calls[0][0] == "acctC"
    assert fake.hour_calls[0][1] == NOW - 14 * 86400, "回看 14 天（与调阈侧同源常量）"
    assert fake.hour_calls[0][2] == time.localtime(NOW).tm_gmtoff, (
        "本地偏移用该时刻的时区规则算（含夏令时），不写死")
    # TTL 边界要**逐个秒**踩到：119 秒还算命中、120 秒必须重查。
    #   只测"5 秒内命中 / 1 小时后重查"会把 TTL 长度本身放过去（5s 还是 120s 都全绿）。
    L.load_hf_group_hour_counts("acctC", NOW + 6)
    assert len(fake.hour_calls) == 1
    L.load_hf_group_hour_counts("acctC", NOW + 119)
    assert len(fake.hour_calls) == 1, "119s < 120s ⇒ 仍命中"
    L.load_hf_group_hour_counts("acctC", NOW + 120)
    assert len(fake.hour_calls) == 2, "120s 不再 < 120s ⇒ 重查"


def test_hour_counts_failure_leaves_no_dirty_cache(fake: FakeRepo):
    fake.hour_raises = True
    with pytest.raises(RuntimeError):
        L.load_hf_group_hour_counts("acctD", NOW)
    fake.hour_raises = False
    fake.buckets = [{"group_id": "gA", "h": 1, "n": 2}]
    assert L.load_hf_group_hour_counts("acctD", NOW) == {"gA|1": 2}, "失败不留脏缓存"


def test_ambient_p_from_hour_counts_uses_poisson_formula():
    """口径写死在测试里（不是从被测函数反推）：泊松 ``1 - e^(-rate*window)``，回看 14 天。"""
    out = L.ambient_p_from_hour_counts({"g|20": 300, "g|3": 0}, 120)
    expected = 1.0 - math.exp(-(300 / (14 * 3600)) * 120)
    assert out["g|20"] == pytest.approx(expected)
    assert out["g|3"] == 0.0
    assert L.HF_AMBIENT_LOOKBACK_DAYS == 14, "两侧必须同一个回看天数"


# ===== 4. 读侧（judge 热路径）=====


def test_get_layer_stat_returns_none_when_unknown_or_uncovered(fake: FakeRepo):
    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    recompute()
    assert L.get_hf_layer_stat("acct", "gNOPE", NOW, DEFAULT_B) is None, "没有这个群"
    assert L.get_hf_layer_stat("acct", "gA", NOW, ((9, 17),)) is None, "当前时刻未被任何段覆盖"
    assert L.get_hf_layer_stat("other", "gA", NOW, DEFAULT_B) is None, "账号隔离"


def test_reset_clears_stats_signatures_and_hour_counts(fake: FakeRepo):
    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    fake.buckets = [{"group_id": "gA", "h": 20, "n": 5}]
    recompute()
    L.load_hf_group_hour_counts("acct", NOW)
    assert L.hf_layer_cache_size() > 0
    L.reset_hf_layer_cache()
    assert L.hf_layer_cache_size() == 0
    assert L.hf_layer_keys_cached("acct", "gA") == []
    assert L.get_hf_layer_stat("acct", "gA", NOW, DEFAULT_B) is None
    # 签名也要清 —— 否则下一轮 recompute 会因为"签没变"而 0 写（内存空、库有值 ⇒ 判定读到 None）
    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    assert recompute() == 4, "清签名后必须重写，不许被陈旧签名挡住"


def preheat_rows(n_by_key: dict[str, int], engaged_by_key: dict[str, int] | None = None):
    """按"库里已有的段统计"造行 —— 四个段**都要有**，才算真正的"重启后稳态"。"""
    eng = engaged_by_key or {}
    return [dict(account_id="acct", group_id="gA", layer_kind="daypart", layer_key=k,
                 n=n, engaged=eng.get(k, 0), ambient_p=0.0, window_start=W60)
            for k, n in n_by_key.items()]


def test_preheat_loads_rows_and_suppresses_rewrite(fake: FakeRepo):
    """预热 = judge 热路径零 DB 读的前提；且预热后**立即**达到稳态（0 写）。

    这条同时钉住"写库签名"必须由预热一起恢复：只灌统计、不灌签名的话，账号每次重启
    都会把四个段整轮重写一遍（写库量不致命，但 300s 一轮的稳态被打破，"0 写"这个可观测
    信号就失去了意义 —— 而它正是发现"累加桶复活"的唯一手段）。
    """
    fake.layer_rows = preheat_rows({"0-7": 0, "7-12": 0, "12-18": 0, "18-24": 3},
                                   {"18-24": 1})
    assert L.load_hf_layer_stats("acct") == 4
    got = L.get_hf_layer_stat("acct", "gA", NOW, DEFAULT_B)
    assert got is not None and got.stat.n == 3, "预热后判定只看内存"
    fake.samples = [dbrow("gA", "silence", 20), dbrow("gA", "silence", 21),
                    dbrow("gA", "short-window", 22, engaged=1)]
    assert recompute() == 0, "预热内容与重算一致 ⇒ 0 写（重启不会白刷一遍库）"


def test_preheat_prunes_stale_rows_of_that_account_only(fake: FakeRepo):
    fake.layer_rows = [
        dict(account_id="acct", group_id="gA", layer_kind="daypart", layer_key="18-24",
             n=1, engaged=1, ambient_p=None, window_start=W60),
        dict(account_id="acct", group_id="gA", layer_kind="daypart", layer_key="0-7",
             n=9, engaged=9, ambient_p=None, window_start=W60),
        dict(account_id="other", group_id="gX", layer_kind="daypart", layer_key="0-7",
             n=7, engaged=7, ambient_p=None, window_start=W60),
    ]
    L.load_hf_layer_stats("other")
    assert L.hf_layer_keys_cached("other", "gX") == ["0-7"]
    L.load_hf_layer_stats("acct")
    assert L.hf_layer_keys_cached("acct", "gA") == ["0-7", "18-24"], "首次预热照单全收"
    fake.layer_rows = [fake.layer_rows[0]]           # 库里只剩 18-24
    assert L.load_hf_layer_stats("acct") == 1
    assert L.hf_layer_keys_cached("acct", "gA") == ["18-24"], "陈旧行必须被清掉（账号内全量替换）"
    assert L.hf_layer_keys_cached("other", "gX") == ["0-7"], "不许动别的账号"


def test_preheat_failure_degrades_to_empty_without_clearing(fake: FakeRepo, monkeypatch, caplog):
    from plugin.data import repo_hf as rh

    fake.samples = [dbrow("gA", "quote", 20, engaged=1)]
    recompute()

    def boom(account_id, layer_kind=None):
        raise RuntimeError("boom-preheat")

    monkeypatch.setattr(rh, "list_layer_stats", boom)
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.load_hf_layer_stats("acct") == 0, "预热失败降级为 0，不抛"
    assert L.hf_layer_keys_cached("acct", "gA") != [], "失败**不清**缓存（旧值仍可用）"
    assert any("预热失败" in r.message for r in caplog.records)


def test_load_converts_db_row_shapes(fake: FakeRepo):
    """DB 行 → 内存行：数值列一律收窄（库可能给 Decimal/str/None）。"""
    fake.layer_rows = [
        dict(account_id="acct", group_id="gA", layer_kind="daypart", layer_key="18-24",
             n="3", engaged=None, ambient_p="0.25", window_start=None),
    ]
    assert L.load_hf_layer_stats("acct") == 1
    got = L.get_hf_layer_stat("acct", "gA", NOW, DEFAULT_B)      # 20:30 落在 18-24
    assert got is not None
    assert (got.stat.n, got.stat.engaged, got.stat.ambient_p, got.stat.window_start) == (3, 0, 0.25, 0)
    assert got.stat.layer_kind == "daypart"


# ===== 5. 配置面 =====


def test_defaults_are_shadow_and_tighten_only():
    D = L.HF_LAYERED_DEFAULTS
    assert D["enabled"] is True, "统计默认开（观测价值无损）"
    assert D["apply"] is False, "生效默认关 = 影子（老板看够日报再手动开）"
    assert D["allowLoosen"] is False, "默认只许收紧"
    assert D["minSamples"] == 30
    assert D["windowDays"] == 60, "窗口上限：防止旧样本永久锁死速率"
    assert D["buckets"] == DEFAULT_B
    cfg = L.resolve_hf_layered_cfg(None)
    assert (cfg.enabled, cfg.apply, cfg.allow_loosen) == (True, False, False)
    assert (cfg.min_samples, cfg.window_days) == (30, 60)
    assert cfg.buckets == DEFAULT_B


def test_resolve_reads_config_and_clamps():
    cfg = L.resolve_hf_layered_cfg({"layered": {
        "enabled": False, "apply": True, "allowLoosen": True,
        "minSamples": 3.9, "windowDays": 0, "buckets": [[9, 12], [12, 18]],
    }})
    assert (cfg.enabled, cfg.apply, cfg.allow_loosen) == (False, True, True)
    assert cfg.min_samples == 3, "向下取整"
    assert cfg.window_days == 1, "0 天 ⇒ 钳到 1（0 天窗口=永远没样本）"
    assert cfg.buckets == ((9, 12), (12, 18))


@pytest.mark.parametrize("dirty", ["abc", None, True, float("nan"), float("inf"), {}])
def test_dirty_numeric_config_converges_to_default_not_nan(dirty):
    """脏值收敛成缺省，**不许**变成 NaN —— NaN 会顺着 ``window_start`` 传染整轮聚合。"""
    cfg = L.resolve_hf_layered_cfg({"layered": {"minSamples": dirty, "windowDays": dirty}})
    assert (cfg.min_samples, cfg.window_days) == (30, 60)
    assert math.isfinite(cfg.window_days)


# ===== 6. 源级接线守卫（layer 侧）=====


def test_layer_never_imports_learn():
    """反向依赖会成环：``learn`` 要 import 本模块，本模块**不得** import learn。

    这条在 TS 侧是硬约束（``heartflow-learn`` 依赖分层决策）。成环后最先炸的是
    加载期（ImportError），但因为注册链上的异常会被 ``_step`` 吞成 WARNING，
    真实表现是**整批工具静默不注册**（F5 的形态）。
    """
    tree = ast.parse(LAYER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[-1] != "learn" for a in node.names), "不许 import learn"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert ".learn" not in mod and mod != "learn", f"不许 from {mod} import ..."
            assert all(a.name != "learn" for a in node.names), "不许 from . import learn"


def test_module_level_imports_have_no_third_party_dependency():
    """加载期只许 stdlib + 本包相对导入。

    本模块将来会被 ``heartflow``（gate）import，而 gate 在注册链上；模块级的
    ``pymysql``/``aiohttp`` 之类会让"注册探针缺依赖"变成**静默不注册**。
    数据层一律在函数体内延迟导入（``from ...data import repo_hf``）。
    """
    tree = ast.parse(LAYER_SRC)
    top: list[str] = []
    for node in tree.body:                       # 只看模块顶层，不看函数体
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append(("." * node.level) + (node.module or ""))
    stdlib = {"__future__", "logging", "math", "threading", "dataclasses", "datetime", "typing"}
    for name in top:
        assert name.startswith(".") or name.split(".")[0] in stdlib, f"模块级第三方依赖: {name}"
    assert "from ...data import repo_hf" in LAYER_SRC, "数据层仍是延迟导入"


def test_judge_hot_path_get_layer_stat_has_no_io():
    """判定读口体内无 import / 无 SQL —— judge 热路径零 DB IO（V7）。"""
    start = LAYER_SRC.index("def get_hf_layer_stat(")
    rest = LAYER_SRC[start + 10:]
    end = rest.index("\ndef ")
    body = rest[:end]
    assert len(body) > 200, "切片自检：函数体必须被切出来（否则这条断言是假绿）"
    for banned in ("import ", "SELECT", "get_conn", "await ", "repo_"):
        assert banned not in body, f"判定体内不得出现 {banned!r}"
    assert "hf_layer_key_for(" in body, "判定必须自己算段名"
    assert "_layer_stats" in body, "判定必须读内存缓存"


@pytest.mark.parametrize("fn", ["list_closed_since", "upsert_layer_stat", "list_layer_stats"])
def test_layer_sql_is_single_table(fn: str):
    """分层 SQL **不得 JOIN** ``wpp_messages``：两张表 collation 不同 ⇒ MariaDB 报
    ``Illegal mix of collations``（整轮 sweep 失败）。"""
    start = REPO_HF_SRC.index(f"def {fn}(")
    rest = REPO_HF_SRC[start:]
    end = rest.index("\ndef ", 1)
    body = rest[:end]
    assert len(body) > 40, f"切片自检：{fn} 必须被切出来"
    assert "JOIN" not in body.upper(), f"{fn} 不得 JOIN"


def test_hour_bucket_sql_is_single_table():
    src = Path(repo_messages.__file__).read_text(encoding="utf-8")
    start = src.index("def list_group_msg_hour_buckets(")
    rest = src[start:]
    body = rest[:rest.index("\ndef ", 1)]
    assert len(body) > 40
    assert "JOIN" not in body.upper()


def test_fake_row_shape_matches_the_real_query():
    """假数据层的行形状必须与 ``list_closed_since`` 的 ``SELECT`` **逐列一致**。

    少一列的话，``from_db_row`` 会把它静默补成默认值（``engage_signal=None`` ⇒ 整行被当
    legacy 丢掉），于是**所有用假数据层的用例一起失真**却仍然全绿 —— 这正是本仓最怕的
    "绿色但不是证据"。改了真查询就得同步改这里，这个断言就是那次提醒。
    """
    start = REPO_HF_SRC.index("def list_closed_since(")
    body = REPO_HF_SRC[start:REPO_HF_SRC.index("\ndef ", start + 1)]
    assert set(dbrow("g", "quote", 20)) == {"group_id", "engaged", "engage_signal", "sent_at"}
    assert L.HfLayerSample.from_db_row(dbrow("g", "quote", 20)).sent_at == at(20)
    for col in set(dbrow("g", "quote", 20)):
        assert col in body, f"假行的列 {col!r} 不在真查询里（两边已漂移）"


def test_pinned_shared_constants():
    """跨模块共用的数字**逐个字面钉住** —— 一侧改动、另一侧没跟上 = n 永远对不上。"""
    assert L.HF_AMBIENT_LOOKBACK_DAYS == 14
    assert L.HF_LAYER_DB_ROW_CAP == 5000
    assert L.HF_LAYER_KIND == "daypart"
    assert L.HF_HOUR_COUNTS_TTL_SEC == 120
    assert L.HF_LAYER_MAX_BUCKETS == 6


def test_no_real_group_ids_in_sources():
    """R2 红线：群 ID 形态可断言；门店/品牌/人名**不能**写在这里（写出来这条测试自己就成了泄漏），
    那些由发布链的脱敏规则门负责。"""
    import re

    pat = re.compile(r"\d{9,}@chatroom")
    for src in (LAYER_SRC, Path(__file__).read_text(encoding="utf-8")):
        assert not pat.search(src)


def test_cache_is_registered_in_registry():
    assert "layer" in CACHE_REGISTRY.names()
    CACHE_REGISTRY.reset("layer")


# ===== 7. 未译部分（等 ``learn.py`` / ``heartflow.py`` 落地）
#
# TS §5 剩下的三条主语都还没到：
#   · ``sweep`` 顺序（画像 pass < 分层 pass < ``learning.enabled`` 判定）—— ``learn.py``
#   · ``resolveHfThresholdDecision`` 的七条决策用例（层生效 / 影子 / 样本不足 / 死区 /
#     allowLoosen / band 钳制 / 账号级回落）—— ``learn.py``
#   · ``heartflow.ts`` 只以 ``import type`` 回指 layer、``layered`` 子块不进默认容器 ——
#     ``heartflow.py``（Py 侧对应"函数体内延迟导入 + 反向不 import layer"）
#
# **故意不放"永远 skip"的壳**：跳过看起来像通过，比没有更糟。
