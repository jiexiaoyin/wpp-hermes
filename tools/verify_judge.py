#!/usr/bin/env python3
"""第 5 步（LLM 层）验收：judge 的双格式、凭证链、关思考、空正文抛错、看图白名单。

**不用真网络**：装一个假的 aiohttp 替身，把请求体原样抓下来逐字段断言。
这样验的是"我们发出去了什么"，而不是"厂商怎么回" —— 后者属于第 9 步真机。
用法::

    python3 tools/verify_judge.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
NPASS = 0

#: 假 aiohttp 抓到的**最后一次**请求
CAPTURED: dict = {}


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  ❌ {msg}")


def ok(msg: str) -> None:
    global NPASS
    NPASS += 1
    print(f"  ✅ {msg}")


def eq(got, want, label: str) -> None:
    if got == want:
        ok(f"{label}: {got!r}")
    else:
        fail(f"{label}: 得到 {got!r}，期望 {want!r}")


# ------------------------------------------------------- 假 aiohttp（抓请求体）

def _install_fake_aiohttp(reply: dict | None = None, status: int = 200,
                          raw_text: str | None = None) -> None:
    """装一个只够 judge 用的 aiohttp 替身，把请求记进 ``CAPTURED``。

    ``reply`` 是 ``resp.json()`` 要回的对象；``raw_text`` 给"响应不是 JSON"用例。
    """
    mod = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class ClientError(Exception):
        pass

    class _Resp:
        def __init__(self, payload, st):
            self._payload = payload
            self.status = st

        async def text(self):
            return raw_text if raw_text is not None else json.dumps(self._payload)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Sess:
        def post(self, url, data=None, headers=None, timeout=None):
            CAPTURED.clear()
            CAPTURED.update(url=url, headers=headers, timeout=timeout,
                            body=json.loads(data.decode("utf-8")),
                            # 原始字节：差分门禁要拿它跟 TS 的 JSON.stringify **逐字节**比
                            raw_body=data.decode("utf-8"))
            return _Resp(reply or {}, status)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def ClientSession(*a, **k):
        return _Sess()

    mod.ClientTimeout = ClientTimeout          # type: ignore[attr-defined]
    mod.ClientError = ClientError              # type: ignore[attr-defined]
    mod.ClientSession = ClientSession          # type: ignore[attr-defined]
    sys.modules["aiohttp"] = mod


def _uninstall_fake_aiohttp() -> None:
    sys.modules.pop("aiohttp", None)


# ------------------------------------------------------------------ 用例

def case_creds_chain() -> None:
    print("\n① 凭证链：JUDGE_API_KEY → MINIMAX_CN_API_KEY → MINIMAX_API_KEY")
    import os

    from plugin.llm.judge import resolve_judge_creds
    saved = {k: os.environ.get(k) for k in
             ("JUDGE_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_CN_API_KEY", "MINIMAX_API_KEY")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        c = resolve_judge_creds()
        eq(c.format, "anthropic", "三个 key 都没有 ⇒ 格式")
        eq(bool(c.api_key), False, "三个 key 都没有 ⇒ 空 key")

        os.environ["MINIMAX_API_KEY"] = "mk"
        c = resolve_judge_creds()
        eq((c.api_key, c.format), ("mk", "anthropic"), "只有 MINIMAX_API_KEY")
        eq(c.base_url, "https://api.minimaxi.com/anthropic", "MiniMax 默认端点")

        os.environ["MINIMAX_CN_API_KEY"] = "cn"
        c = resolve_judge_creds()
        eq(c.api_key, "cn", "MINIMAX_CN_API_KEY 优先于 MINIMAX_API_KEY（旧 Py 侧行为）")

        # ⚠️ v1.14.0 破坏性改名：旧名 DEEPSEEK_API_KEY **不再读**。这里钉死它，
        #    否则「改了名但旧变量仍悄悄生效」会让灰度期的凭证链无法解释。
        os.environ["DEEPSEEK_API_KEY"] = "ds"
        c = resolve_judge_creds()
        eq((c.api_key, c.format), ("cn", "anthropic"), "DEEPSEEK_API_KEY 不再读取（v1.14.0 改名）")

        os.environ["JUDGE_API_KEY"] = "jk"
        c = resolve_judge_creds()
        eq((c.api_key, c.format), ("jk", "openai"), "JUDGE_API_KEY 压倒一切")
        eq(c.base_url, "https://api.deepseek.com", "DeepSeek 默认端点（openai 兼容）")

        # 尾部斜杠必须剥掉，否则拼出 ``//chat/completions``
        c = resolve_judge_creds(judge_base_url="https://x.example.com///")
        eq(c.base_url, "https://x.example.com", "base_url 尾部斜杠已剥")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def case_openai_body() -> None:
    print("\n② openai 分支请求体（DeepSeek）")
    from plugin.llm.judge import JudgeCreds, call_judge

    async def run():
        _install_fake_aiohttp({"choices": [{"message": {"content": "  {\"a\":1}  "}}]})
        try:
            return await call_judge(
                model="deepseek-v4-flash", user_prompt="打分",
                system_prompt="只回 JSON",
                creds=JudgeCreds(api_key="k", base_url="https://api.deepseek.com",
                                 format="openai"),
            )
        finally:
            _uninstall_fake_aiohttp()

    text = asyncio.run(run())
    eq(text, '  {"a":1}  ', "原样返回（不做 strip，交调用方）")
    eq(CAPTURED["url"], "https://api.deepseek.com/chat/completions", "端点")
    eq(CAPTURED["headers"]["authorization"], "Bearer k", "Authorization 头")
    b = CAPTURED["body"]
    eq(b["thinking"], {"type": "disabled"}, "关思考写进请求体")
    eq(b["temperature"], 0, "temperature")
    eq(b["max_tokens"], 300, "max_tokens 默认 300")
    eq(b["messages"][0], {"role": "system", "content": "只回 JSON"}, "system 走 messages")
    eq(b["messages"][1]["content"], "打分", "**无图 ⇒ content 是纯字符串**（老路径零回归）")
    eq(list(b.keys()), ["model", "max_tokens", "temperature", "thinking", "messages"],
       "键序（与 TS 蓝本一致，便于逐字节对照）")


def case_no_think_off() -> None:
    print("\n③ no_think=False ⇒ 请求体里不许有 thinking")
    from plugin.llm.judge import JudgeCreds, call_judge

    async def run():
        _install_fake_aiohttp({"choices": [{"message": {"content": "x"}}]})
        try:
            await call_judge(
                model="m", user_prompt="p",
                creds=JudgeCreds(api_key="k", base_url="https://api.deepseek.com",
                                 format="openai", no_think=False))
        finally:
            _uninstall_fake_aiohttp()

    asyncio.run(run())
    eq("thinking" in CAPTURED["body"], False, "no_think=False ⇒ 无 thinking 键")


def case_anthropic_body() -> None:
    print("\n④ anthropic 分支请求体（MiniMax）")
    from plugin.llm.judge import JudgeCreds, call_judge

    async def run():
        _install_fake_aiohttp({"content": [{"type": "text", "text": "hi"}]})
        try:
            return await call_judge(
                model="mini", user_prompt="打分", system_prompt="SYS",
                creds=JudgeCreds(api_key="k", base_url="https://api.minimaxi.com/anthropic",
                                 format="anthropic"),
            )
        finally:
            _uninstall_fake_aiohttp()

    eq(asyncio.run(run()), "hi", "取 content 里第一个 text 块")
    # ⚠️ 不是 ``api.minimaxi.com/v1/messages``：MiniMax 的 anthropic 兼容端点是
    #    ``/anthropic`` **前缀**，再拼 ``/v1/messages``。TS 与旧 Py 侧都产出这个串。
    eq(CAPTURED["url"], "https://api.minimaxi.com/anthropic/v1/messages", "端点")
    eq(CAPTURED["headers"]["x-api-key"], "k", "x-api-key 头")
    eq(CAPTURED["headers"]["anthropic-version"], "2023-06-01", "anthropic-version 头")
    b = CAPTURED["body"]
    eq(b["thinking"], {"type": "disabled"}, "关思考（anthropic 分支同样带上）")
    eq(b["system"], "SYS", "system 走顶层 system 字段")
    eq(b["messages"], [{"role": "user", "content": "打分"}], "messages")


def case_empty_body_raises() -> None:
    print("\n⑤ 空正文必须 raise（09-11 静默瘫 3 天的直接对策）")
    from plugin.core import metrics
    from plugin.llm.judge import JudgeCreds, JudgeError, call_judge

    oc = JudgeCreds(api_key="k", base_url="https://api.deepseek.com", format="openai")
    ac = JudgeCreds(api_key="k", base_url="https://api.minimaxi.com/anthropic",
                    format="anthropic")

    async def run(reply, creds):
        _install_fake_aiohttp(reply)
        try:
            return await call_judge(model="m", user_prompt="p", creds=creds)
        finally:
            _uninstall_fake_aiohttp()

    # openai：截断（reasoning 吃光预算的典型形态）
    metrics.reset_all()
    try:
        asyncio.run(run({"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                         "usage": {"completion_tokens_details": {"reasoning_tokens": 299}}}, oc))
        fail("openai 空正文**没有**抛错")
    except JudgeError as e:
        s = str(e)
        if "finish_reason=length" in s and "reasoning_tokens=299" in s and "截断" in s:
            ok(f"openai 空正文抛 JudgeError 且带 finish_reason/reasoning_tokens: {s}")
        else:
            fail(f"openai 空正文的报错信息不够定位: {s}")
    eq((metrics.get_counter("judge_calls_total"),
        metrics.get_counter("judge_failures_total")), (1, 1), "一次失败 ⇒ calls=1 failures=1")

    # anthropic：同样必须抛
    metrics.reset_all()
    try:
        asyncio.run(run({"content": [], "stop_reason": "max_tokens"}, ac))
        fail("anthropic 空正文**没有**抛错")
    except JudgeError as e:
        s = str(e)
        if "stop_reason=max_tokens" in s and "截断" in s:
            ok(f"anthropic 空正文抛 JudgeError 且带 stop_reason: {s}")
        else:
            fail(f"anthropic 空正文的报错信息不够定位: {s}")
    eq((metrics.get_counter("judge_calls_total"),
        metrics.get_counter("judge_failures_total")), (1, 1),
       "两个分支的计数对称（各 1 次调用 1 次失败）")


def case_images() -> None:
    print("\n⑥ 看图（v1.11.0）：白名单 + 只 openai 生效")
    from plugin.llm.judge import JudgeCreds, call_judge, filter_image_urls

    # ⚠️ 这里**故意只做前缀白名单**，不做 IP 校验 —— 与 TS 蓝本逐字一致。
    #    理由：这些 URL 只作为**数据**发给厂商，**我们不抓取**；厂商从它自己的网络去取，
    #    内网地址对它是不可达的，因此"我们被 SSRF"这条路径不存在。
    #    我们自己**真去抓**的那条路（``inbound/media.py`` → ``netguard.safe_fetch_url``）
    #    有 IP 校验 —— 安全闸拦在抓取处，不是拦在这个纯字符串过滤器里。
    eq(filter_image_urls(["https://a/x.jpg", "data:image/png;base64,AA",  # type: ignore[list-item]
                          "file:///etc/passwd", "http://169.254.169.254/", "",
                          "ftp://a/b"]),
       ["https://a/x.jpg", "data:image/png;base64,AA", "http://169.254.169.254/"],
       "前缀白名单只放行 http(s)/data:image（不做 IP 校验，见上方注释）")
    eq(filter_image_urls(None), [], "None ⇒ 空")

    oc = JudgeCreds(api_key="k", base_url="https://api.deepseek.com", format="openai")

    async def run(creds, images):
        _install_fake_aiohttp({"choices": [{"message": {"content": "x"}}]}
                              if creds.format == "openai" else
                              {"content": [{"type": "text", "text": "x"}]})
        try:
            return await call_judge(model="m", user_prompt="P", creds=creds, images=images)
        finally:
            _uninstall_fake_aiohttp()

    # openai + 有图 ⇒ 内容块数组
    asyncio.run(run(oc, ["https://a/x.jpg"]))
    content = CAPTURED["body"]["messages"][-1]["content"]
    eq(content, [{"type": "text", "text": "P"},
                 {"type": "image_url", "image_url": {"url": "https://a/x.jpg"}}],
       "openai 有图 ⇒ 文本块 + image_url 块")

    # openai + 全是非法图 ⇒ 退回纯字符串（**逐字节等同无图**）
    asyncio.run(run(oc, ["file:///etc/passwd"]))
    eq(CAPTURED["body"]["messages"][-1]["content"], "P",
       "图全被白名单丢掉 ⇒ 退回纯字符串")

    # anthropic + 有图 ⇒ 忽略图片，content 仍是字符串
    ac = JudgeCreds(api_key="k", base_url="https://api.minimaxi.com/anthropic",
                    format="anthropic")
    asyncio.run(run(ac, ["https://a/x.jpg"]))
    eq(CAPTURED["body"]["messages"], [{"role": "user", "content": "P"}],
       "anthropic 忽略图片（与 TS 蓝本一致）")


def case_errors() -> None:
    print("\n⑦ 其它失败面：无 key / 非 2xx / 超时 / 非 JSON / SSRF")
    from plugin.llm.judge import JudgeCreds, JudgeError, call_judge

    async def run(creds, **kw):
        return await call_judge(model="m", user_prompt="p", creds=creds, **kw)

    # 无 key
    try:
        asyncio.run(run(JudgeCreds(api_key="", base_url="https://api.deepseek.com",
                                   format="openai")))
        fail("无 key 没抛错")
    except JudgeError as e:
        ok(f"无 key 抛 JudgeError: {str(e)[:60]}")

    # 非 2xx
    _install_fake_aiohttp({}, status=429, raw_text="rate limited")
    try:
        asyncio.run(run(JudgeCreds(api_key="k", base_url="https://api.deepseek.com",
                                   format="openai")))
        fail("HTTP 429 没抛错")
    except JudgeError as e:
        eq(str(e), "HTTP 429: rate limited", "非 2xx ⇒ 带状态码与错误体")
    finally:
        _uninstall_fake_aiohttp()

    # 响应不是 JSON
    _install_fake_aiohttp(raw_text="<html>502</html>")
    try:
        asyncio.run(run(JudgeCreds(api_key="k", base_url="https://api.deepseek.com",
                                   format="openai")))
        fail("非 JSON 响应没抛错")
    except JudgeError as e:
        ok(f"非 JSON 响应 ⇒ JudgeError: {str(e)[:60]}")
    finally:
        _uninstall_fake_aiohttp()

    # SSRF：字面量内网地址
    for bad in ("http://127.0.0.1:8000", "http://169.254.169.254",
                "http://192.168.1.1", "http://localhost:9999"):
        try:
            asyncio.run(run(JudgeCreds(api_key="k", base_url=bad, format="openai")))
            fail(f"端点闸没拦住 {bad}")
        except JudgeError as e:
            if "安全闸" in str(e):
                ok(f"端点闸拦住 {bad}")
            else:
                fail(f"{bad} 报错原因不对: {e}")

    # 计数：每一次失败都已计入
    from plugin.core import metrics
    ok(f"累计 judge_calls_total={metrics.get_counter('judge_calls_total')} "
       f"judge_failures_total={metrics.get_counter('judge_failures_total')}")


def case_deferred_import() -> None:
    print("\n⑧ aiohttp 必须是**函数体内**导入（注册链路不吃第三方依赖）")
    import ast
    src = (ROOT / "plugin" / "llm" / "judge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_imports: list[str] = []
    for node in tree.body:                       # 只看模块级
        if isinstance(node, ast.Import):
            top_imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_imports.append(node.module or "")
    if any(n.split(".")[0] == "aiohttp" for n in top_imports):
        fail("aiohttp 出现在模块级 import —— R2 会 FAIL，注册链路会被拉进第三方依赖")
    else:
        ok(f"模块级无 aiohttp（只有 {[n for n in top_imports if n]!r}）")

    # 真验一次：把 aiohttp 从 sys.modules 清掉再导入本模块
    saved = sys.modules.pop("aiohttp", None)
    for m in [k for k in sys.modules if k.startswith("plugin.llm.judge")]:
        del sys.modules[m]
    try:
        import importlib
        mod = importlib.import_module("plugin.llm.judge")
        ok("aiohttp 不可用时本模块**照样能导入**（延迟生效）")
        if hasattr(mod, "aiohttp"):
            fail("模块级泄漏了 aiohttp 名字")
        else:
            ok("无模块级 aiohttp 名字泄漏")
    except Exception as exc:  # noqa: BLE001 —— 异常不得冒泡打断整批（收尾/探针路径）
        fail(f"缺 aiohttp 时导入失败: {type(exc).__name__}: {exc}")
    finally:
        if saved is not None:
            sys.modules["aiohttp"] = saved


def main() -> int:
    print(f"检查树: {ROOT}")
    case_creds_chain()
    case_openai_body()
    case_no_think_off()
    case_anthropic_body()
    case_empty_body_raises()
    case_images()
    case_errors()
    case_deferred_import()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项未过")
        return 1
    print(f"judge 验收：全绿（{NPASS} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
