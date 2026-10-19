"""心流运行时（``runtime.py``）回归 —— **账号启动预热** + **缓存登记表**两块。

## 为什么预热要单独立一套门

预热五条（learned 阈值 / 发言预算计数 / 分层统计 / 复读历史 / 群画像）每一处的**降级路径都长成"静默"**：
加载失败 ⇒ 空缓存启动、计数从 0 起算、分层统计缺席 —— 插件照跑、日志只有一行 warning，
表现是"重启后头 5 分钟判断变了个样"。三种最容易假绿的情形各配一条行为级门：

1. **少调一条**：只调前两条的实现在生产上完全看不出来（第三条的名字都不出现在日志里），
   所以这里断言**每条都真被调到**，且顺序与 TS ``startAccount`` 一致。
2. **一条炸了连坐**：预算表被锁住 / 列改过名 ⇒ 若实现没把每条各自隔离，
   learned 阈值就一起不加载（"一个坏查询毁掉全部预热"）。
3. **预热反过来挡接入**：预热是**旁路**，它失败不许让账号起不来 —— 断言它**不抛**。

## 画像那条的边界（2026-10-18 起）

画像**读**进缓存是预热的一部分（回复侧开始消费画像后，冷启动 300s 会让回复风格
退化成通用人设）；但**生成**仍只许在 sweep 第 1 步。两处各跑一次生成 = 同一群一天
生成两份画像，所以这里正面钉住"预热只读、不生成"。
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

from plugin.inbound.heartflow import dedupe as D
from plugin.inbound.heartflow import layer as LAY
from plugin.inbound.heartflow import learn as L
from plugin.inbound.heartflow import profile as P
from plugin.inbound.heartflow import runtime as R

RUNTIME_SRC = Path(R.__file__).read_text(encoding="utf-8")

#: 预热顺序：前三条是 TS ``startAccount`` 的顺序（learned → budget → layer），不许调换
#: —— 三条互不依赖，但**顺序**是"对齐 TS"这件事本身的一部分（差分时靠它认位）。
#: 第四条 ``dedupe`` 是本侧补的（B7）；第五条 ``profiles`` 是 2026-10-18 补的
#: （回复侧开始消费画像）。两条都只**读**，排在 TS 三条之后。
TS_ORDER = ["learned", "budget", "layer", "dedupe", "profiles"]


class Recorder:
    """把四条加载函数换成记账器。返回的 ``calls`` 顺序即真实调用顺序。"""

    def __init__(self, counts: dict[str, int] | None = None,
                 raises: dict[str, Exception] | None = None) -> None:
        self.calls: list[str] = []
        self.counts = counts or {}
        self.raises = raises or {}

    def make(self, key: str):
        def _load(account_id: str) -> int:
            self.calls.append(key)
            exc = self.raises.get(key)
            if exc is not None:
                raise exc
            return self.counts.get(key, 0)
        return _load


@pytest.fixture
def rec(monkeypatch):
    """五条加载函数全部换成记账器（顶替 ``模块.函数名`` —— 预热是调用时取属性）。"""
    r = Recorder()
    for key, mod, fname in (("learned", L, "load_learned_thresholds"),
                            ("budget", L, "load_hf_budget_seed"),
                            ("layer", LAY, "load_hf_layer_stats"),
                            ("dedupe", D, "load_hf_dedupe_history"),
                            ("profiles", P, "load_hf_profiles")):
        monkeypatch.setattr(mod, fname, r.make(key))
    return r


# ===== 1. 每条都调到、顺序对、入参是账号 =====


def test_preheat_calls_every_loader_in_ts_order(rec):
    out = R.preheat_account("acct1")
    assert rec.calls == TS_ORDER, "五条都要调，且顺序对齐 TS startAccount"
    assert set(out) == set(TS_ORDER)


def test_preheat_passes_the_account_id_through(monkeypatch):
    seen: list[tuple[str, str]] = []
    for key, mod, fname in (("learned", L, "load_learned_thresholds"),
                            ("budget", L, "load_hf_budget_seed"),
                            ("layer", LAY, "load_hf_layer_stats"),
                            ("dedupe", D, "load_hf_dedupe_history"),
                            ("profiles", P, "load_hf_profiles")):
        monkeypatch.setattr(mod, fname,
                            (lambda k: (lambda aid: (seen.append((k, aid)), 0)[1]))(key))
    R.preheat_account("acct7")
    assert seen == [("learned", "acct7"), ("budget", "acct7"), ("layer", "acct7"),
                    ("dedupe", "acct7"), ("profiles", "acct7")]


def test_preheat_returns_each_loader_count(monkeypatch):
    """返回值是"灌进去多少条" —— 适配器/巡检靠它认"预热到底有没有拿到东西"。"""
    r = Recorder(counts={"learned": 3, "budget": 2, "layer": 41, "dedupe": 9,
                         "profiles": 5})
    for key, mod, fname in (("learned", L, "load_learned_thresholds"),
                            ("budget", L, "load_hf_budget_seed"),
                            ("layer", LAY, "load_hf_layer_stats"),
                            ("dedupe", D, "load_hf_dedupe_history"),
                            ("profiles", P, "load_hf_profiles")):
        monkeypatch.setattr(mod, fname, r.make(key))
    assert R.preheat_account("acct1") == {"learned": 3, "budget": 2, "layer": 41,
                                          "dedupe": 9, "profiles": 5}


# ===== 2. 一条失败不连坐，且不许把异常放出来 =====


def test_one_failing_loader_does_not_stop_the_others(monkeypatch, caplog):
    """预算那条炸了 ⇒ learned 与 layer 仍必须加载（否则"一个坏查询毁掉全部预热"）。"""
    r = Recorder(counts={"learned": 5, "layer": 7, "dedupe": 4, "profiles": 2},
                 raises={"budget": RuntimeError("账本表被锁")})
    for key, mod, fname in (("learned", L, "load_learned_thresholds"),
                            ("budget", L, "load_hf_budget_seed"),
                            ("layer", LAY, "load_hf_layer_stats"),
                            ("dedupe", D, "load_hf_dedupe_history"),
                            ("profiles", P, "load_hf_profiles")):
        monkeypatch.setattr(mod, fname, r.make(key))
    with caplog.at_level(logging.WARNING, logger=R.__name__):
        out = R.preheat_account("acct1")
    assert rec_calls(r) == TS_ORDER, "失败那条之后的几条仍要被调到"
    assert out == {"learned": 5, "budget": 0, "layer": 7, "dedupe": 4, "profiles": 2}
    assert any("启动预热失败" in rec.message and "预算" in rec.message
               for rec in caplog.records), "失败必须吵（否则静默少一份缓存）"


def rec_calls(r: Recorder) -> list[str]:
    return list(r.calls)


def test_preheat_never_raises_even_when_everything_fails(monkeypatch, caplog):
    """五条全炸也**不抛** —— 它是旁路，不许挡住账号接入。"""
    r = Recorder(raises={k: RuntimeError("DB 不通") for k in TS_ORDER})
    for key, mod, fname in (("learned", L, "load_learned_thresholds"),
                            ("budget", L, "load_hf_budget_seed"),
                            ("layer", LAY, "load_hf_layer_stats"),
                            ("dedupe", D, "load_hf_dedupe_history"),
                            ("profiles", P, "load_hf_profiles")):
        monkeypatch.setattr(mod, fname, r.make(key))
    with caplog.at_level(logging.WARNING, logger=R.__name__):
        assert R.preheat_account("acct1") == {"learned": 0, "budget": 0, "layer": 0,
                                              "dedupe": 0, "profiles": 0}


def test_preheat_logs_only_nonzero_counts(rec, caplog):
    """0 条不吵（每天重启的常态就是"库里啥都没有"），>0 才记 info。"""
    rec.counts = {"learned": 0, "budget": 4, "layer": 0, "dedupe": 0, "profiles": 0}
    with caplog.at_level(logging.INFO, logger=R.__name__):
        R.preheat_account("acct1")
    infos = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "发言预算计数" in infos[0], "只有 >0 的那条说话"


# ===== 3. 未就位 / 失败时给空结果，而不是炸在导入期 =====


def test_preheat_is_empty_when_heartflow_modules_are_absent(monkeypatch):
    """心流模块取不到 ⇒ 空结果 + 不抛（F5：未就位不该成为接入的阻塞点）。

    造"取不到"要**删父包属性 + ``sys.modules`` 置 ``None``**：``from . import layer``
    先看父包属性，属性留着（哪怕是 ``None``）就会被直接返回、根本不抛。
    """
    import plugin.inbound.heartflow as HF_PKG
    monkeypatch.delattr(HF_PKG, "layer", raising=False)
    monkeypatch.setitem(sys.modules, "plugin.inbound.heartflow.layer", None)
    assert R.preheat_account("acct1") == {}


def test_preheat_step_table_covers_every_key_in_ts_order():
    steps = R._preload_steps()
    assert [k for k, _, _ in steps] == TS_ORDER
    for key, label, fn in steps:
        assert label and callable(fn), f"{key} 这条的形状不对"


# ===== 4. 源级：预热只**读**画像，生成仍归 sweep 第 1 步 =====


def _fn_code(fname: str) -> str:
    tree = ast.parse(RUNTIME_SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fname:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "\n".join(ast.unparse(s) for s in body)
    raise AssertionError(f"runtime.py 里没有 {fname}")


def test_preheat_only_reads_profiles_and_never_generates():
    """预热读画像（冷启动 300s 的回复风格靠它），但**生成**只许在 sweep 第 1 步。"""
    body = _fn_code("preheat_account") + _fn_code("_preload_steps")
    assert "load_hf_profiles" in body, "画像必须读进缓存（回复侧消费画像）"
    assert "maybe_generate_hf_group_profiles" not in body, \
        "预热不许生成画像（两处生成 = 同一群一天两份，见模块 docstring）"


def test_preheat_steps_are_imported_inside_the_function():
    """F5：加载期的第三方/环形 import 会把注册失败吞成 WARNING —— 预热同理。"""
    tree = ast.parse(RUNTIME_SRC)
    for node in tree.body:                     # 只看模块顶层
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") not in ("learn", "layer", "profile"), \
                "learn/layer/profile 只能函数体内导入（环形 + 注册链纪律）"
    assert "from . import layer as hf_layer" in _fn_code("_preload_steps")
    assert "from . import profile as hf_profile" in _fn_code("_preload_steps")


# ===== 5. 缓存登记表（八处）=====

#: 当前登记的八处。**名字是账本**：conftest 的 ``reset_all`` 按名字清，
#: 漏登记一处的表现是跨用例污染（上一个用例的阈值漂到下一个用例里），
#: 那种失败看起来像业务 bug，所以宁可在这里被挡下。
#:
#: ``style`` 是 2026-10-19 加的：说话风格学习的缓存独立于七域，但同样登记到
#: 这张表，好让测试/运维的"全部清缓存"不会漏掉它（功能开关仍然各自独立）。
KNOWN_CACHES = {"budget", "dedupe", "heartflow", "layer", "learn", "observe",
                "profile", "style"}


def test_the_cache_registry_holds_exactly_the_known_caches():
    """★ 登记表必须与这份名单**逐名相等**。

    两个方向都有意义：**多了** ⇒ 某个模块忘了登记（静默污染）；
    **少了** ⇒ 谁删掉了登记项（要么是有意的，那就改这里；要么是踩坏了）。

    ``runtime`` 曾在这张表里 —— 它那份"配置单例"没有任何读者（配置的唯一来源是
    ``resolve().heartflow``），2026-10-01 随单例一起删掉。**这就是"少一处"的正当前提**，
    所以名单改动必须同时改这里与各 docstring 里的「N 处」。
    """
    # import 即登记（各模块底部的 CACHE_REGISTRY.register），所以先把八处都拉起来。
    # ``style`` 不进 heartflow 包，但同样在模块底部登记 —— 必须在这里显式 import，
    # 否则"登记表逐名相等"会随测试文件的执行顺序时绿时红。
    from plugin.inbound.heartflow import (  # noqa: F401
        budget, dedupe, heartflow, observe, profile,
    )
    from plugin.inbound import style_learn  # noqa: F401
    names = R.CACHE_REGISTRY.names()
    assert len(names) == len(set(names)), f"登记表里有重名: {names}"
    assert set(names) == KNOWN_CACHES, (
        f"登记表与账本不符：多 {sorted(set(names) - KNOWN_CACHES)}，"
        f"少 {sorted(KNOWN_CACHES - set(names))}"
    )


def test_resetting_an_unregistered_cache_is_loud():
    """清一个没登记的名字必须**抛** —— 静默放过会让"清干净了"是个假象。"""
    with pytest.raises(KeyError):
        R.CACHE_REGISTRY.reset("no-such-cache")


def test_the_config_singleton_is_gone():
    """★ 反向断言：只写不读的全局配置表**不许回来**（TS 那份 Map 各复制一份的事故）。

    它回来的代价不是"多一个 dict"，而是**配置有了第二个真源**：某处顺手写进去，
    另一处读到的就不是 ``resolve()`` 的结果（或反之）—— 与 TS 那 7 个测试失败同型。
    """
    for gone in ("get_runtime", "set_runtime_config", "clear_runtime"):
        assert not hasattr(R, gone), f"runtime.{gone} 又被加回来了"
    assert not hasattr(R, "_runtime"), "全局配置表又被加回来了（见模块 docstring）"
