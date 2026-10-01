#!/usr/bin/env python3
"""V8 / V9 端到端冒烟 —— 在 ``.183`` 上跑**部署版**代码（不是 dev 树）。

## 判据（计划第 9 步）

| 项 | 判据 |
|---|---|
| **V9** | 含「红包」字样的接龙**不被静默吞掉**（09-27 事故的回归）；真红包照旧静默 |
| **V8** | 合成入站消息走完 解析 → 闸门 → judge → 台账，``wpp_hf_ledger`` 出现 ``status='judged'`` 且 ``judge_overall`` 在 0–1 |
| **V7** | 这一路的**闸门 + judge 段零 DB 读**（``db_call_count()`` == 0），台账写入是唯一的库动作 |

## 「live 半段」：**已于 2026-10-01 19:41 由人工窗口流程验过，本脚本仍不执行**

真消息 + 真 judge 需要：① 一个**测试账号**的 authcode（走独立的
``accounts/live-test.json``，**绝不**用那份引用了生产 bot selfWxid 的 ``default.json``）；
② 一个 LLM key。**两条 2026-10-01 都临时就位过**（老板解禁 ``a0da3904…`` +
``.183`` 装 ``DEEPSEEK_API_KEY``），复测在**生产群** ``57737516566@chatroom`` 真发成功
（台账 id=8 ``status='sent'`` / ``judge_overall=0.7750``，网关 0 条 ``Unauthorized``），
**19:43 窗口即收回**（``live-test.json`` → ``enabled:false``、三个 env 全删）。

所以本节**只报现场读数**，不重复真发：真发消息到生产群是红线，
**本脚本永不发**（那条链得由人按"开窗—复测—收回"的流程走）。
历史 BLOCKED 的成因与两层授权根因见
``progress-2026-10-01-wpp-hermes-py2-v8-live-done``。

## 用法（在 .183 上）

    /root/.hermes/installs/*/environments/*/venv/bin/python3 \\
        tools/verify_e2e_183.py --plugin /root/.hermes/plugins/wechatpadpro \\
        --pkg wechatpadpro --env-file /root/.hermes/.env [--db wechatpro]

退出码 0 = 全绿。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path

_FAILS: list[str] = []
_NOTES: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" —— {detail}" if detail else ""))
        _FAILS.append(label)
    return bool(cond)


def load_env(path: str) -> None:
    p = Path(path)
    if not p.exists():
        print(f"  ⚠️  env 文件不存在: {path}")
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- V9


def phase_v9(mods: dict) -> None:
    print("① V9 接龙不被吞（09-27 事故回归）")
    parser, triggers, hongbao, relay = (mods["parser"], mods["triggers"],
                                        mods["hongbao"], mods["relay"])
    MT_APP = parser.MT_APP

    #: 与 dev 侧回归用例**同一批样本**（09-27 那晚真被吞掉的那批形状）
    relay_content = ("#接龙 💥抓住新机，全力冲刺 🔥使命必达 今日开售，提升业绩，"
                     "领取红包🧧 红包100元 1. 门店＋型号 2. A店 watch6pro 3. B店 watch6pro")

    def _msg(content, msg_type=MT_APP, raw=None):
        # ⚠️ 必须带 ``chatroom_id``：不带就是**私聊**，私聊走 fail-closed
        #    （无 allowFrom ⇒ (False, "dm_fail_closed")）—— 第一次跑就踩了这个，
        #    它长得像"接龙被吞"，其实是我样本没搭成群消息。
        return parser.WppMessage(account_id="e2e-synth", msg_id="e2e-m1",
                                 chatroom_id="e2e-synth@chatroom",
                                 from_wxid="wxid_synth_alice", to_wxid="wxid_synth_bot",
                                 content=content, msg_type=msg_type, raw=raw or {})

    m = _msg(relay_content)
    check(relay.is_relay_message(m) is True, "前提：样本确实是接龙")
    check(hongbao.is_red_packet_message(m) is False,
          "★ 带「红包」字样的接龙**不是**红包（这一条就是 09-27 的修复）")

    cfg = {"selfWxid": "wxid_synth_bot", "nickname": "小助理"}
    ok, reason = triggers.should_process(m, cfg)
    check(ok is True and reason == "group_solitaire",
          "★ 接龙走「100% 必回」那一路（原因串也是口径，降级成心流候选同样算回归）",
          f"got={ok}/{reason}")

    # 反向：真红包照旧静默（修 bug 不能把静默面一起放开）
    ok2, reason2 = triggers.should_process(_msg("微信红包"), cfg)
    check(ok2 is False and reason2 == "red_packet",
          "真红包卡片仍静默（不是把红包闸拆了）", f"got={ok2}/{reason2}")


# --------------------------------------------------------------------------- V8/V7


def _fake_judge(judge_mod, overall_dims: float):
    """把 ``call_judge`` / ``resolve_judge_creds`` 换成假件（假 LLM，不联网）。"""
    dims = {k: overall_dims for k in ("relevance", "willingness", "social",
                                      "timing", "continuity")}
    payload = json.dumps(dims, ensure_ascii=False)
    calls: list[dict] = []

    async def _call(**kw):
        calls.append(kw)
        return payload

    judge_mod.call_judge = _call
    judge_mod.resolve_judge_creds = lambda: judge_mod.JudgeCreds(
        api_key="e2e-fake-key", base_url="http://judge.invalid/v1", format="openai")
    return calls


class _Acct:
    def __init__(self, cfg):
        self.id = "e2e-synth"
        self.self_wxid = "wxid_synth_bot"
        self.nickname = "小助理"
        self.heartflow = cfg


class _Msg:
    def __init__(self, msg_id, content, group: str = "e2e-synth@chatroom"):
        # ⚠️ 每个阶段用**不同群**：judge 频率闸按群记 ``_last_judge_at``，
        #    同群连跑第二条会被冷却闸挡掉（于是台账根本不会写，用例假红）
        self.chatroom_id = group
        self.peer_id = group
        self.content = content
        self.msg_id = msg_id
        self.new_msg_id = msg_id + "n"
        self.from_wxid = "wxid_synth_alice"
        self.from_nickname = "阿丽"
        self.msg_type = "text"
        self.timestamp = int(time.time())


def phase_v8_v7(mods: dict, db_name: str) -> None:
    db, HF, L, P, B = mods["db"], mods["hf"], mods["learn"], mods["profile"], mods["budget"]

    def q(sql, params=()):
        """读一行 —— 模块级没有 ``db.query``，连接挂在 ``transaction()`` 上。"""
        with db.transaction() as conn:
            return conn.query(sql, params)

    def x(sql, params=()):
        with db.transaction() as conn:
            return conn.execute(sql, params)

    print("② 目标库")
    print(f"  {db.describe()}")
    check(db.config()["database"] == db_name, "连的是预期库", db.config()["database"])

    cfg = HF.default_hf_config()
    cfg.update({"enabled": True, "model": "e2e-fake-model", "replyThreshold": 0.5})
    acct = _Acct(cfg)
    judge_mod = mods["judge"]

    # --- V7：闸门 + judge 段零 DB（台账写入被换成记录器 ⇒ 整条链路应当 0 次）
    print("③ V7 热路径零 DB（真部署代码 + 冷缓存）")
    for reset in (P.reset_hf_profile_cache, L.reset_learned_threshold_cache,
                  B.reset_budget_cache, HF.reset_heartflow_cache):
        reset()
    calls = _fake_judge(judge_mod, 10)
    ledger: list[tuple] = []
    orig_judged, orig_below = L.persist_hf_judged, L.persist_hf_judged_below_threshold
    L.persist_hf_judged = lambda rec: ledger.append(("judged", rec))
    L.persist_hf_judged_below_threshold = lambda rec, at: ledger.append(("below", rec, at))
    try:
        db.reset_db_call_count()
        engaged = asyncio.run(HF.maybe_engage(object(), acct, _Msg("e2e-v7", "这批货什么时候到")))
        n = db.db_call_count()
    finally:
        L.persist_hf_judged, L.persist_hf_judged_below_threshold = orig_judged, orig_below
    check(bool(calls), "前提：judge 真被调到了（否则这条没验到东西）")
    check(n == 0, "★ 闸门 + judge 全程 0 次 DB（台账已被换成记录器）", f"实际 {n} 次")
    check(bool(ledger), "前提：台账缝被触发（编排走到底）", f"ledger={ledger[:1]}")

    # --- V8：真台账落库（真 DB、假 LLM）
    print("④ V8 合成入站 → 真台账")
    msg_id = f"e2e-synth-{int(time.time())}"
    _fake_judge(judge_mod, 10)
    db.reset_db_call_count()
    engaged = asyncio.run(HF.maybe_engage(object(), acct, _Msg(msg_id, "这批货什么时候到")))
    n2 = db.db_call_count()
    check(engaged is True, "★ 满分判定 ⇒ 接管（should_reply=True）")
    print(f"  ℹ️  台账写入路径共 {n2} 次 get_conn（写库是热路径之外的动作，不要求为 0）")

    row = None
    for _ in range(5):
        rows = q("SELECT status, judge_overall, effective_threshold FROM wpp_hf_ledger "
                 "WHERE account_id=%s AND inbound_msg_id=%s", ("e2e-synth", msg_id))
        if rows:
            row = rows[0]
            break
        time.sleep(0.2)
    check(row is not None, "★ wpp_hf_ledger 出现该行（端到端真落库）")
    if row:
        status = str(row.get("status") or "")
        overall = row.get("judge_overall")
        check(status == "judged", "★ status == 'judged'", f"got={status!r}")
        check(overall is not None and 0.0 <= float(overall) <= 1.0,
              "★ judge_overall 是 0–1 小数（尺度对）", f"got={overall!r}")
        check(float(overall) >= 0.99, "满分样本 ⇒ overall ≈ 1.0", f"got={overall!r}")

    # --- V8 反向：判定没过 ⇒ 也留一行（below-threshold），不静默
    print("⑤ V8 反向：判定没过也留痕（09-13 静默瘫的教训）")
    msg_id2 = f"e2e-synth-low-{int(time.time())}"
    _fake_judge(judge_mod, 1)      # 5 维全 1 分 ⇒ overall 0.1 < 0.5
    engaged2 = asyncio.run(HF.maybe_engage(object(), acct, _Msg(msg_id2, "今天天气不错",
                                                          "e2e-synth-low@chatroom")))
    check(engaged2 is False, "低分 ⇒ 不接管")
    low = q("SELECT status, suppressed_reason FROM wpp_hf_ledger "
            "WHERE account_id=%s AND inbound_msg_id=%s", ("e2e-synth", msg_id2))
    check(bool(low), "★ 低分也落一行（不然「判了没过」在库里零留痕）")
    if low:
        check(str(low[0].get("status") or "") == "suppressed",
              "低分行的 status='suppressed'", f"got={low[0]!r}")
        check(str(low[0].get("suppressed_reason") or "") == "below-threshold",
              "原因 = below-threshold", f"got={low[0]!r}")

    # 收尾：把合成行删掉，别在库里留垃圾
    x("DELETE FROM wpp_hf_ledger WHERE account_id=%s", ("e2e-synth",))
    print("  ℹ️  合成台账行已清理（account_id='e2e-synth'）")


# --------------------------------------------------------------------------- main


def wire(plugin_dir: str, pkg: str) -> dict:
    """把部署目录挂上 sys.path，按**目录名**导入包。

    部署侧包名是 ``wechatpadpro``（dev 侧是 ``plugin``）—— 生产代码里没有绝对
    ``plugin.*`` 导入，所以两边都能跑；本脚本按目录名导入，正是为了**顺带验证这一点**。
    """
    # ⚠️ 挂的是**父目录**：包名等于目录名（部署侧 ``wechatpadpro``），
    #    把包目录自己塞进 sys.path 是找不到 ``wechatpadpro`` 这个包的（踩过）。
    sys.path.insert(0, str(Path(plugin_dir).resolve().parent))
    mods = {}
    for key, sub in {
        "parser": "inbound.parser", "triggers": "inbound.triggers",
        "hongbao": "inbound.hongbao", "relay": "inbound.relay",
        "db": "data.db", "hf": "inbound.heartflow.heartflow",
        "learn": "inbound.heartflow.learn", "profile": "inbound.heartflow.profile",
        "budget": "inbound.heartflow.budget", "judge": "llm.judge",
    }.items():
        mods[key] = importlib.import_module(f"{pkg}.{sub}")
    return mods


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", default="/root/.hermes/plugins/wechatpadpro")
    ap.add_argument("--pkg", default="wechatpadpro")
    ap.add_argument("--env-file", default="/root/.hermes/.env")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    print("=" * 62)
    print(" V8/V9 端到端冒烟 —— 跑在**部署版**代码上（假 LLM + 假微信）")
    print("=" * 62)
    load_env(args.env_file)
    if args.db:
        os.environ["WECHATPRO_DB_NAME"] = args.db

    mods = wire(args.plugin, args.pkg)
    print(f"  插件包 {args.pkg} ← {args.plugin}")
    print(f"  DB     {mods['db'].describe()}")
    print()

    phase_v9(mods)
    print()
    phase_v8_v7(mods, os.environ.get("WECHATPRO_DB_NAME", "wechatpro"))
    print()

    # live 半段：本脚本**不执行**（真发是红线），只报现场读数。
    print("⑥ live 半段（真消息 + 真 judge）—— 本脚本不执行（真发是红线）")
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("MINIMAX_API_KEY")
                   or os.environ.get("WECHATPRO_LLM_API_KEY"))
    acct_files = sorted(Path(args.plugin, "accounts").glob("*.json"))
    real_accts = [p for p in acct_files if not p.name.endswith(".example.json")]
    print(f"  LLM key: {'有' if has_key else '无'}；账号文件: "
          f"{[p.name for p in acct_files] or '无'}（非 example: {len(real_accts)}）")
    # 窗口是否还开着 —— 只看**启用位**，不去连厂商（本脚本只读文件、不发消息）
    live = Path(args.plugin, "accounts", "live-test.json")
    if live.exists():
        try:
            _en = json.loads(live.read_text(encoding="utf-8")).get("enabled")
        except Exception:                                     # noqa: BLE001
            _en = "读不出"
        print(f"  测试账号窗口: enabled={_en}"
              f"{'（已按计划收回）' if _en is False else '（⚠️ enabled 非 false —— 开着的）'}")
    _NOTES.append("live 半段 2026-10-01 19:41 已在生产群真发验过（台账 id=8 status='sent'，"
                  "19:43 窗口已收回）；真发是红线，本脚本永不执行")

    print()
    if _FAILS:
        print(f"FAIL —— 失败 {len(_FAILS)} 条:")
        for f in _FAILS:
            print(f"  - {f}")
        return 1
    print("PASS —— 失败 0 条")
    for n in _NOTES:
        print(f"  ⚠️  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
