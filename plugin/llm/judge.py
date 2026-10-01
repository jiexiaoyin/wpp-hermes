"""统一 LLM judge 调用器 —— TS ``src/llm-judge.ts`` (v1.11.0) 的 Python 侧。

心流 / 黑话 / 好感 / enrich 四机制**共用这一个入口**。双格式：

============  ==========================================  =================================
``openai``    DeepSeek / 任意 OpenAI 兼容端点              ``Authorization: Bearer`` + ``/chat/completions``
``anthropic`` MiniMax / Anthropic 兼容端点                 ``x-api-key`` + ``anthropic-version`` + ``/v1/messages``
============  ==========================================  =================================

## 三条历史事故焊在这里（改这个文件前先读）

1. **judge 静默瘫 3 天（2026-09-11）**。根因：``deepseek-flash`` 是推理模型，
   ``reasoning_content`` 与 ``content`` **共享** ``max_tokens``；judge 只要短 JSON，
   不关思考时 300 tokens 被思考吃光 ⇒ ``content=""``，而旧代码把空串**当正常返回**，
   调用方只看到 ``unparseable: ``（冒号后空白）。=> 两道焊死：
   - 请求带 ``thinking: {"type": "disabled"}``（``enable_thinking: false``
     **服务端不认**，实测；这是唯一有效写法）；
   - **空正文/被截断必须 raise**，且错误串里带 ``finish_reason`` 与 ``reasoning_tokens``。
2. **推理 token 与正文抢预算**：同一件事的另一面 —— 第 1 条的报错信息是为了让下次
   一眼看出"是思考吃掉了"，而不是再去翻三天日志。
3. **09-26 心流自锁停摆**：与 judge 相关的部分是"阈值数字进 prompt" ⇒ 那属于 prompt
   构造（:mod:`plugin.inbound.heartflow.heartflow` 的 ``build_prompt``），不在本模块。

## 凭证链（两个 key 名都要认）

``DEEPSEEK_API_KEY`` → ``MINIMAX_CN_API_KEY`` → ``MINIMAX_API_KEY``。

TS 只有后两个里的 ``MINIMAX_API_KEY``，**旧 Python 侧先认 ``MINIMAX_CN_API_KEY``**
（国内站 key 与海外站 key 是两套）—— 这里取并集：先 CN 后通用，保持旧 Python 行为。
选中 DeepSeek 走 ``openai`` 格式；否则走 ``anthropic``（base 默认 MiniMax 的 anthropic 端点）。

## ⚠️ ``aiohttp`` 必须在**函数体内**导入（不许搬到模块级）

本模块会被 ``platform/adapter.py`` 的加载链路拉起（adapter → 心流 → judge），而
``platform/adapter.py`` 是模块级被 ``registration._register_platform`` 导入的 ⇒
**本模块的加载期依赖就是注册期依赖**。``plugin_validate`` 的 probe 在 scratch 解释器里
跑 ``register()``，那里未必装了 aiohttp ⇒ 模块级 import 会让注册**静默失败**
（已由 ``tools/verify_tree.py`` 的 R2 硬拦，与 ``core/api_client.py`` 同一条理由）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence

from ..core import metrics
from ..core.netguard import GuardError, assert_endpoint_public

log = logging.getLogger(__name__)

#: 默认端点。⚠️ DeepSeek 走 **openai 兼容**路径（``{base}/chat/completions``），
#: 不是旧 Python 侧的 ``https://api.deepseek.com/anthropic`` —— 按 TS v1.11.0 蓝本。
DEFAULT_JUDGE_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com",
    "minimax": "https://api.minimaxi.com/anthropic",
}

#: 旧 Python 侧用的 DeepSeek anthropic 端点。**只**用于给 ``--`` 排障时对照日志，
#: 运行时不用（见 DEFAULT_JUDGE_ENDPOINTS 的注释）。
LEGACY_DEEPSEEK_ANTHROPIC_BASE = "https://api.deepseek.com/anthropic"

#: 图片 URL 只认这两种前缀；其余（``file://``、内网地址、空串）一律丢弃 ——
#: 防把本地路径或内网端点当图片交给厂商端点。仅 ``openai`` 格式生效。
_IMAGE_URL_RE = re.compile(r"^(https?://|data:image/)", re.IGNORECASE)

DEFAULT_MAX_TOKENS = 300
DEFAULT_TIMEOUT_MS = 5000

#: HTTP 错误体在异常里最多带多少字符（够定位，不至于把整页 HTML 灌进日志）
_ERR_BODY_CAP = 120


class JudgeError(RuntimeError):
    """judge 调用失败。**空正文也走这里**（见模块 docstring 事故 1）。"""


@dataclass(frozen=True)
class JudgeCreds:
    """解析好的 judge 凭证。"""

    api_key: str
    base_url: str
    format: str          # "openai" | "anthropic"
    #: 关思考（默认 ``True``）。judge 只输出短 JSON 打分，不需要思维链；
    #: 推理模型的思考与正文共享 ``max_tokens`` ⇒ 不关就是花几倍钱 + 时而返回空正文。
    #: 仅当端点不支持该参数时才置 ``False``。
    no_think: bool = True

    def __post_init__(self) -> None:
        if self.format not in ("openai", "anthropic"):
            raise ValueError(f"format 只能是 openai/anthropic，得到 {self.format!r}")


def resolve_judge_creds(
    *,
    deepseek_base_url: str | None = None,
    minimax_base_url: str | None = None,
) -> JudgeCreds:
    """按环境变量链解析凭证：``DEEPSEEK_API_KEY`` → ``MINIMAX_CN_API_KEY`` → ``MINIMAX_API_KEY``。

    注意选的是**哪个 key 存在**，不是"模型名里有没有 deepseek" ——
    旧 Python 侧按模型名分支，于是模型改名就换错端点（记忆里的 judge 事故之一）。
    """
    deepseek_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if deepseek_key:
        base = deepseek_base_url or DEFAULT_JUDGE_ENDPOINTS["deepseek"]
        return JudgeCreds(api_key=deepseek_key, base_url=base.rstrip("/"), format="openai")

    minimax_key = (
        (os.environ.get("MINIMAX_CN_API_KEY") or "").strip()
        or (os.environ.get("MINIMAX_API_KEY") or "").strip()
    )
    base = minimax_base_url or DEFAULT_JUDGE_ENDPOINTS["minimax"]
    return JudgeCreds(api_key=minimax_key, base_url=base.rstrip("/"), format="anthropic")


def has_judge_creds(creds: JudgeCreds | None = None) -> bool:
    """有没有可用的 key。调用方据此**跳过而不是报错**（无 key 不是故障）。"""
    return bool((creds or resolve_judge_creds()).api_key)


def filter_image_urls(images: Sequence[str] | None) -> list[str]:
    """挑出合法的图片 URL（``http(s)://`` 或 ``data:image/``），其余丢弃。

    ⚠️ **故意不做 IP 校验**（只做前缀匹配），与 TS 蓝本逐字一致。别顺手"加固"：
    这些 URL 只作为**数据**发给厂商端点，**我们不抓取**；厂商从它自己的网络去取，
    内网地址对厂商不可达 ⇒ "我们被 SSRF"这条路径不存在。
    真正由我们**抓取**的那条路（``inbound/media.py``）走 ``netguard.safe_fetch_url``，
    **IP 校验在抓取处**，不在这个纯字符串过滤器里。
    """
    if not images:
        return []
    return [u for u in images if isinstance(u, str) and _IMAGE_URL_RE.match(u)]


async def call_judge(
    *,
    model: str,
    user_prompt: str,
    system_prompt: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    creds: JudgeCreds | None = None,
    images: Sequence[str] | None = None,
) -> str:
    """调 judge，返回模型文本。**失败一律 raise**（``JudgeError``）。

    观测包装：计数在**四机制公共入口**这里，最省埋点且不会漏 ——
    各机制自己 catch 掉的错误也已经被计入。
    """
    metrics.inc_judge_call()
    try:
        return await _call_judge_inner(
            model=model,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
            creds=creds or resolve_judge_creds(),
            images=images,
        )
    except Exception:
        metrics.inc_judge_failure()
        raise


async def _call_judge_inner(
    *,
    model: str,
    user_prompt: str,
    system_prompt: str | None,
    max_tokens: int,
    timeout_ms: int,
    creds: JudgeCreds,
    images: Sequence[str] | None,
) -> str:
    if not creds.api_key:
        raise JudgeError(
            "judge: 无 apiKey（DEEPSEEK_API_KEY / MINIMAX_CN_API_KEY / MINIMAX_API_KEY 都没设）")
    if not creds.base_url:
        raise JudgeError("judge: base_url 为空")

    # 端点闸（不做 DNS，见 netguard.assert_endpoint_public 的 docstring）
    try:
        assert_endpoint_public(creds.base_url)
    except GuardError as exc:
        raise JudgeError(f"judge: 端点被安全闸拒绝: {exc}") from exc

    no_think = creds.no_think
    if creds.format == "openai":
        url = f"{creds.base_url}/chat/completions"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {creds.api_key}",
        }
        # ⚠️ 无图时 ``content`` 保持**纯字符串** —— 纯文本路径的请求体逐字节不变，
        #    老行为零回归（v1.11.0 看图改动的前提）。
        imgs = filter_image_urls(images)
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({
            "role": "user",
            "content": (
                [{"type": "text", "text": user_prompt},
                 *({"type": "image_url", "image_url": {"url": u}} for u in imgs)]
                if imgs else user_prompt
            ),
        })
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        if no_think:
            # 唯一有效的关思考写法（``enable_thinking: false`` 服务端不认，实测）
            body["thinking"] = {"type": "disabled"}
        body["messages"] = messages
        data = await _post_json(url, headers, body, timeout_ms)
        return _extract_openai_text(data)

    # anthropic（图片**故意忽略** —— TS 蓝本同样只在 openai 分支看图）
    url = f"{creds.base_url}/v1/messages"
    headers = {
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": creds.api_key,
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if no_think:
        body["thinking"] = {"type": "disabled"}
    if system_prompt:
        body["system"] = system_prompt
    body["messages"] = [{"role": "user", "content": user_prompt}]
    data = await _post_json(url, headers, body, timeout_ms)
    return _extract_anthropic_text(data)


def _extract_openai_text(data: dict[str, Any]) -> str:
    """⚠️ **INVARIANT I10**：空正文必须**显式报错**，绝不静默返回 ``""``。

    09-11 静默瘫 3 天就是这一条：reasoning 与正文共享 ``max_tokens`` ⇒ 正文恒空 ⇒
    5 维全 0 ⇒ 一条不回；而"静默返回空串"会让上层看到一条**0 分的有效判定**，
    台账/日志/指标全正常。守卫：``tests/invariants/test_invariants.py`` 的 ``test_i10_*``。
    """
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if not content:
        # ⚠️ 空正文必须**显式报错**，绝不静默返回 ""（事故 1：静默瘫 3 天）
        rt = ((data.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens")
        reason = choice.get("finish_reason")
        msg = f"empty content (finish_reason={reason or '?'}"
        if rt is not None:
            msg += f", reasoning_tokens={rt}"
        msg += ")"
        if reason == "length":
            msg += " — 输出被 max_tokens 截断，思考未关？见 JudgeCreds.no_think"
        raise JudgeError(msg)
    return content


def _extract_anthropic_text(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    text = ""
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text") or ""
            break
    if not text:
        reason = data.get("stop_reason")
        msg = f"empty content (stop_reason={reason or '?'})"
        if reason == "max_tokens":
            msg += " — 输出被 max_tokens 截断，思考未关？见 JudgeCreds.no_think"
        raise JudgeError(msg)
    return text


async def _post_json(url: str, headers: dict[str, str], body: dict[str, Any],
                     timeout_ms: int) -> dict[str, Any]:
    """一次 JSON POST。非 2xx 与超时都转成 :class:`JudgeError`。"""
    import aiohttp  # 延迟导入，见模块 docstring

    # ⚠️ ``separators=(",", ":")`` 不是省字节，是**对齐 TS 的 ``JSON.stringify``**：
    #    Python 默认 ``", "`` / ``": "`` 会多出空格 ⇒ 请求体与线上 TS 产物**逐字节不同**。
    #    差分门禁（tools/verify_judge_diff.py）比的就是字节，两端必须一样才有意义。
    #    ``ensure_ascii=False`` 同理 —— 中文按 UTF-8 原样出，与 TS 一致。
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                url, data=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise JudgeError(f"HTTP {resp.status}: {text[:_ERR_BODY_CAP]}")
    except asyncio.TimeoutError as exc:
        raise JudgeError(f"timeout after {timeout_ms}ms") from exc
    except aiohttp.ClientError as exc:
        raise JudgeError(f"HTTP 请求失败: {type(exc).__name__}: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"响应不是 JSON: {text[:_ERR_BODY_CAP]}") from exc
    if not isinstance(data, dict):
        raise JudgeError(f"响应不是 JSON 对象: {text[:_ERR_BODY_CAP]}")
    return data
