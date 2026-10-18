"""W6 写回语义 **.183 真机**验收（计划第 13 项 / 验收点 13）。

**只碰一份临时副本**：把真机账号文件拷进一个 0700 临时目录，用
``WECHATPRO_ACCOUNTS_DIR`` 把它当成"账号目录"，然后走**真命令** ``/blacklist add``
（→ ``_handle_allowlist`` → ``commands.write_back`` → ``account_store.write_account_key``
→ ``adapter.reload_account``）。**在用那份账号文件一个字都不动。**

⚠️ 全程只打印 **长度 + sha256[:12] 指纹**，不打印任何凭据。

用法（.183）::

    sh <tree>/tools/hermes_run.sh <tree>/tools/probe_w6_writeback.py <tree> \
        <accounts>/live-test.json

⚠️ 本探针**只碰临时副本**（``WECHATPRO_ACCOUNTS_DIR`` 指向 0700 沙箱），在用的账号文件一个字都不动；
   全程只打印长度 + sha256[:12] 指纹。与 ``probe_v2b_repos.py`` 同款：要真机环境才能跑，不进 ``gates.sh``。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

TREE = Path(sys.argv[1]).resolve()
for p in ("/root/.hermes/hermes-agent", str(TREE)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("WPP_TOOLS_ONLY", "1")

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: object, detail: object = "") -> None:
    RESULTS.append((label, bool(cond), str(detail)))


def fp(value: object) -> str:
    """指纹：长度 + sha256[:12]（**绝不打印值本身**）。"""
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return f"len={len(raw)} sha256={hashlib.sha256(raw).hexdigest()[:12]}"


CREDS = ("authcode", "tokenKey", "webhookSecret", "webhookPathToken")
GROUP = "99999999999@chatroom"          # 明显的假群，不会与任何真群碰撞
SHADOW = "shadow@chatroom"              # 只用于"影子覆盖机制存在"的只读演示


def main() -> int:
    src = Path(sys.argv[2]) if len(sys.argv) > 2 else TREE / "plugin" / "accounts" / "live-test.json"
    if not src.is_file():
        print(f"❌ 真机账号文件不在：{src}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="w6probe-"))
    os.chmod(tmp, 0o700)
    dst = tmp / "w6probe.json"
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o644)                       # 故意放宽：写完必须被收紧成 600
    os.environ["WECHATPRO_ACCOUNTS_DIR"] = str(tmp)
    aid = "w6probe"
    print(f"# 沙箱账号目录 {tmp}（源 {src.name}，副本权限初始 644）")

    before_raw = dst.read_bytes()
    before = json.loads(before_raw)
    before_creds = {k: fp(before[k]) for k in CREDS if k in before}
    before_keys = set(before)
    print(f"# 键数={len(before)}  凭证键={sorted(before_creds)}  "
          f"初始权限={oct(stat.S_IMODE(dst.stat().st_mode))}")
    for k, v in before_creds.items():
        print(f"    {k}: {v}")
    check("前提：源账号文件的 blacklistGroups 是空的（否则 ① 的动作看不见）",
          before.get("blacklistGroups") == [], str(before.get("blacklistGroups")))

    from plugin.core import account_store, overrides
    from plugin.core import accounts as acc_mod
    from plugin.inbound.debounce import InboundDebouncer
    from plugin.platform import commands as cmd_mod
    from plugin.platform.adapter import WppAdapter

    # ── 模拟网关 connect() 时的解析：内存里那份 Account 得先建起来
    sh_ad = WppAdapter.__new__(WppAdapter)     # 只补 reload_account 要读的那几个属性
    sh_ad.config = SimpleNamespace(extra={})
    sh_ad._accounts = {aid: acc_mod.resolve(aid)}
    sh_ad._pipelines = {}
    db_before = InboundDebouncer(aid, lambda batch: None, interval_ms=999)
    sh_ad._debounce = {aid: db_before}

    check("前提：写回前内存里的 Account 也不含这个假群",
          GROUP not in sh_ad._accounts[aid].blacklist_groups,
          str(sh_ad._accounts[aid].blacklist_groups))

    # 「影子覆盖」机制**只做只读演示**：`resolve()` 的优先级是 ①文件 → ②yaml → ③覆盖。
    # ⚠️ 故意**不**带着覆盖去跑命令：`_allowlist_items` 的底稿是「文件 ⊕ 覆盖」，
    #    覆盖非空时写回会把演示值一起落盘（那是设计语义，会把本探针的断言搅浑）。
    overrides.set_override(aid, "blacklistGroups", [SHADOW])
    check("演示：内存覆盖确实会盖在账号文件之上（这就是 W6 要消灭的失败形态）",
          SHADOW in acc_mod.resolve(aid).blacklist_groups,
          str(acc_mod.resolve(aid).blacklist_groups))
    overrides.clear_override(aid, "blacklistGroups")
    check("演示后清干净（下面走的是无覆盖的干净路径）",
          overrides.get_override(aid).get("blacklistGroups") is None,
          repr(overrides.get_override(aid)))

    # ───────────────────────────────────────────── ① 走**真命令**
    # ⚠️ 签名是 handle_command(account_id, content, *, adapter) —— 顺序别反。
    reply = cmd_mod.handle_command(aid, f"/blacklist add {GROUP}", adapter=sh_ad)
    print(f"# /blacklist add 回执: {reply!r}")
    check("① 真命令回执没有 ❌（说明落盘成功）", "❌" not in reply, reply)
    check("① 真命令回执声明了「已写入账号配置，当次生效」", "已写入账号配置" in reply, reply)

    after_raw = dst.read_bytes()
    after = json.loads(after_raw)
    after_creds = {k: fp(after[k]) for k in CREDS if k in after}
    after_mode = stat.S_IMODE(dst.stat().st_mode)

    check("① 盘上真的变了：blacklistGroups 恰是这个群",
          after.get("blacklistGroups") == [GROUP], str(after.get("blacklistGroups")))
    changed = {k for k in before_keys | set(after) if before.get(k) != after.get(k)}
    check("① 只有 blacklistGroups 一个键变了（没有静默改动别的键）",
          changed == {"blacklistGroups"}, f"变了 {sorted(changed)}")
    check("① 键数不变（不丢任何本树不建模的键）",
          set(after) == before_keys, f"{len(before_keys)} → {len(set(after))}；"
          f"少了 {sorted(before_keys - set(after))} 多了 {sorted(set(after) - before_keys)}")

    # ───────────────────────────────────────────── ② 凭证逐字节不变
    check("② 凭证字段**逐字节**不变（值→JSON 逐字节 + 指纹）",
          after_creds == before_creds, f"{before_creds} → {after_creds}")

    # ───────────────────────────────────────────── ③ 当次生效（不需要重启）
    live = sh_ad.account(aid)
    check("③ 网关读的那份（adapter.account）当次就是新值",
          GROUP in live.blacklist_groups, str(live.blacklist_groups))
    check("③ 写回把同键内存覆盖清掉了（W6 的核心契约）",
          overrides.get_override(aid).get("blacklistGroups") is None,
          repr(overrides.get_override(aid)))
    check("③ 聚合窗口跟着账号文件走（W7-6 的 reload 分支：999 → 文件里的值）",
          sh_ad._debounce[aid].interval_ms == int(live.debounce_ms),
          f"{sh_ad._debounce[aid].interval_ms} vs {live.debounce_ms}")
    check("③ 聚合器是**就地改**、没被重建（重建会丢掉已缓冲的那批）",
          sh_ad._debounce[aid] is db_before,
          "对象被换了 ⇒ connect() 之后已缓冲的那批会静默丢失")

    # ───────────────────────────────────────────── ⑤ 权限 600
    check("⑤ 写回后文件权限被收紧到 600", after_mode == 0o600, oct(after_mode))

    # ───────────────────────────────────────────── 备份（老板规矩：放 /data）
    bdir = account_store.backup_dir()
    baks = sorted(bdir.glob("w6probe.json.pre-writeback-*"))
    print(f"# 备份根 {bdir} → {[b.name for b in baks]}")
    check("写回前建了备份、且备份根是 /data", bool(baks) and str(bdir).startswith("/data"),
          f"{bdir} → {[b.name for b in baks]}")
    check("备份内容与写回**前**的原文件逐字节相同（真能拿回来）",
          bool(baks) and baks[-1].read_bytes() == before_raw,
          f"{baks[-1].name if baks else '无备份'}")
    check("备份文件权限 600", bool(baks) and stat.S_IMODE(baks[-1].stat().st_mode) == 0o600)

    # ─────────────────────────── ④ 跨重启仍生效：**另起一个进程**读盘
    code = ("import json,sys;d=json.load(open(sys.argv[1]));"
            "print(json.dumps({'blacklistGroups': d.get('blacklistGroups'),'keys': len(d)}))")
    out = subprocess.run([sys.executable, "-c", code, str(dst)],
                         capture_output=True, text=True, env=dict(os.environ), check=False)
    print(f"# 第二个进程读盘: {out.stdout.strip()}")
    check("④ 另起一个进程读到的就是新值（盘是唯一真相 ⇒ 跨重启仍在）",
          out.returncode == 0 and json.loads(out.stdout or "{}").get("blacklistGroups") == [GROUP],
          out.stderr.strip()[-200:])

    # ─────────────────────────── 凭证键：结构性不可写（真机负向）
    for bad_key in ("authcode", "tokenKeyEnv", "webhookSecret"):
        raw_now = dst.read_bytes()
        try:
            account_store.write_account_key(aid, bad_key, "probe-should-never-land")
            check(f"负向：写凭证键 {bad_key} 必须被拒", False, "居然写成功了")
        except account_store.AccountWriteError as exc:
            check(f"负向：写凭证键 {bad_key} 被拒且文件一个字节没动",
                  dst.read_bytes() == raw_now, str(exc)[:120])

    # ─────────────────────────── del / list 往返
    reply = cmd_mod.handle_command(aid, f"/blacklist del {GROUP}", adapter=sh_ad)
    after2 = json.loads(dst.read_bytes())
    check("往返：del 之后盘上回到空、且内存那份同步",
          after2.get("blacklistGroups") == [] and not sh_ad.account(aid).blacklist_groups,
          f"{after2.get('blacklistGroups')} / {sh_ad.account(aid).blacklist_groups}")
    check("往返：两个来回之后凭证仍然逐字节不变",
          {k: fp(after2[k]) for k in CREDS if k in after2} == before_creds)
    check("往返：键数仍然不变", set(after2) == before_keys, str(len(set(after2))))

    reply = cmd_mod.handle_command(aid, "/blacklist list", adapter=sh_ad)
    check("list 能读出来（不是报错、且报 0 条）", "❌" not in reply and "(0)" in reply, reply[:120])

    # 「没变化就不写」：同一条 add 再来一次 ⇒ 只多印一句 (均已存在)，盘上不动
    cmd_mod.handle_command(aid, f"/blacklist add {GROUP}", adapter=sh_ad)
    m1 = dst.stat().st_mtime_ns
    raw1 = dst.read_bytes()
    reply = cmd_mod.handle_command(aid, f"/blacklist add {GROUP}", adapter=sh_ad)
    check("幂等：同一个群加第二次**不写盘**（不多留备份、回话也不提「已写入」）",
          dst.read_bytes() == raw1 and dst.stat().st_mtime_ns == m1 and "已写入" not in reply,
          f"{reply!r}")

    # 清场：沙箱目录与**本探针自己产生的备份**都带真凭据，必须删干净。
    # ⚠️ 生产里写回**故意**留备份（写错了能拿回来）—— 那是契约；但探针跑的是沙箱账号
    #    （`w6probe`），它的备份只是同一份真配置的**多余副本**，留着等于把凭据多铺一份。
    shutil.rmtree(tmp, ignore_errors=True)
    leftovers = sorted(account_store.backup_dir().glob("w6probe.json.pre-writeback-*"))
    for bak in leftovers:
        bak.unlink(missing_ok=True)
    check("沙箱目录已删（副本里有真凭据，不留痕）", not tmp.exists())
    check("探针自己的备份副本也已删（生产留备份是契约，探针的只是多余凭据副本）",
          not sorted(account_store.backup_dir().glob("w6probe.json.pre-writeback-*")),
          str([b.name for b in leftovers]))

    ok = sum(1 for _, c, _ in RESULTS if c)
    for label, cond, detail in RESULTS:
        print(f"  {'✅' if cond else '❌'} {label}" + (f"\n       ← {detail}" if detail and not cond else ""))
    print(f"\n{ok}/{len(RESULTS)} 通过")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
