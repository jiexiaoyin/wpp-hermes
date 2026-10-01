#!/usr/bin/env python3
"""第 5 步差分门禁：**拿 TS 真产物**跑同样的用例，与 Python 侧逐字节比请求体。

比的是"我们真正发出去的字节"，不是"我读懂了 TS 源码没有" —— 后者不是证据。
（同 :mod:`tools.verify_tools_port` 的"真跑差异"、第 3 步分片器的"真 dist 差分"。）

## 用哪个产物

**必须**传路由到部署目录的 ``dist/llm-judge.js``。仓库里的
``<repo>/src/llm-judge.js`` 是 **9-05 的陈旧 tsc 产物**（``src/**/*.js`` 在 .gitignore
里，没人重编），既无 v1.6.1 的 ``thinking`` 也无 v1.11.0 的 ``image_url``；
拿它比会"全线不一致"，但那是旧构建 vs 新源码，纯噪声。

默认路径：``/root/.openclaw/extensions/wechatpadpro/dist/llm-judge.js``

用法::

    python3 tools/verify_judge_diff.py                 # 用默认 dist 路径
    python3 tools/verify_judge_diff.py <llm-judge.js>
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

DEFAULT_TS_DIST = pathlib.Path(
    "/root/.openclaw/extensions/wechatpadpro/dist/llm-judge.js")

#: 与 ``tools/_ts_judge_oracle.mjs`` 的 CASES **必须同名同参**。
#: 名字对不上就 fail —— 否则"少比了几个用例"会被静默吞掉。
CASES: dict[str, dict] = {
    "openai-text": dict(
        model="deepseek-v4-flash", user_prompt="打分", system_prompt="只回 JSON",
        max_tokens=300, timeout_ms=5000,
        creds=("k", "https://api.deepseek.com", "openai")),
    "openai-text-nosystem": dict(
        model="m", user_prompt="P",
        creds=("k", "https://api.deepseek.com", "openai")),
    "openai-nothink-off": dict(
        model="m", user_prompt="P",
        creds=("k", "https://api.deepseek.com", "openai"), no_think=False),
    "openai-images": dict(
        model="m", user_prompt="P", system_prompt="S",
        creds=("k", "https://api.deepseek.com", "openai"),
        images=["https://a/x.jpg", "data:image/png;base64,AA", "file:///etc/passwd", ""]),
    "anthropic-text": dict(
        model="mini", user_prompt="打分", system_prompt="SYS",
        max_tokens=300, timeout_ms=5000,
        creds=("k", "https://api.minimaxi.com/anthropic", "anthropic")),
    "anthropic-nosystem": dict(
        model="m", user_prompt="P",
        creds=("k", "https://api.minimaxi.com/anthropic", "anthropic")),
    "anthropic-images-ignored": dict(
        model="m", user_prompt="P",
        creds=("k", "https://api.minimaxi.com/anthropic", "anthropic"),
        images=["https://a/x.jpg"]),
}

#: 每个用例喂给厂商的假响应（两端一致，免得"TS 走了一条返回路径"造成假差异）
REPLIES = {
    "openai": {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
    "anthropic": {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"},
}


def run_ts(js_path: pathlib.Path) -> dict[str, dict]:
    proc = subprocess.run(
        ["node", str(TOOLS / "_ts_judge_oracle.mjs"), str(js_path)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"TS oracle 失败 (exit {proc.returncode}):\n{proc.stderr[:2000]}")
    out: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        rec = json.loads(line)
        if "error" in rec:
            raise RuntimeError(f"TS 用例 {rec['case']} 抛错: {rec['error']}")
        out[rec["case"]] = rec
    return out


def run_py() -> dict[str, dict]:
    import verify_judge as VJ          # 复用同一套假 aiohttp 夹具，免得两处漂移
    from plugin.llm.judge import JudgeCreds, call_judge

    out: dict[str, dict] = {}
    CAPTURED = VJ.CAPTURED
    for name, c in CASES.items():
        key, base, fmt = c["creds"]
        creds = JudgeCreds(api_key=key, base_url=base, format=fmt,
                           no_think=c.get("no_think", True))
        VJ._install_fake_aiohttp(REPLIES[fmt])
        try:
            asyncio.run(call_judge(
                model=c["model"], user_prompt=c["user_prompt"],
                system_prompt=c.get("system_prompt"),
                max_tokens=c.get("max_tokens", 300),
                timeout_ms=c.get("timeout_ms", 5000),
                creds=creds, images=c.get("images"),
            ))
        finally:
            VJ._uninstall_fake_aiohttp()
        out[name] = {
            "case": name,
            "url": CAPTURED["url"],
            "headers": {k.lower(): v for k, v in (CAPTURED["headers"] or {}).items()},
            "body": CAPTURED["raw_body"],
        }
    return out


def main(argv: list[str]) -> int:
    js_path = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT_TS_DIST
    if not js_path.is_file():
        print(f"❌ 找不到 TS 产物: {js_path}")
        print("   （这是**部署目录**的 dist，不是仓库 src —— 后者是陈旧构建）")
        return 1

    print(f"TS 产物: {js_path}")
    print(f"用例数: {len(CASES)}\n")

    ts = run_ts(js_path)
    py = run_py()

    missing = sorted(set(CASES) - set(ts))
    if missing:
        print(f"❌ TS oracle 少跑了用例（名字对不上？）: {missing}")
        return 1

    fails = 0
    for name in CASES:
        t, p = ts[name], py[name]
        diffs = []
        if t["url"] != p["url"]:
            diffs.append(f"url:\n      TS  {t['url']}\n      PY  {p['url']}")
        t_h = {k.lower(): v for k, v in (t["headers"] or {}).items()}
        if t_h != p["headers"]:
            diffs.append(f"headers:\n      TS  {t_h}\n      PY  {p['headers']}")
        if t["body"] != p["body"]:
            diffs.append(f"body（逐字节）:\n      TS  {t['body']}\n      PY  {p['body']}")
        if diffs:
            fails += 1
            print(f"  ❌ {name}")
            for d in diffs:
                print(f"      {d}")
        else:
            print(f"  ✅ {name}  url/headers/body 逐字节一致")

    print()
    if fails:
        print(f"{fails}/{len(CASES)} 个用例不一致")
        return 1
    print(f"judge 差分：全绿（{len(CASES)} 个用例，与 TS 真产物逐字节一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
