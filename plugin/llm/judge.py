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

## 凭证链（v1.14.0 改名后）

``JUDGE_API_KEY`` → ``MINIMAX_CN_API_KEY`` → ``MINIMAX_API_KEY``。

TS v1.14.0 起旧名 ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL`` **彻底作废、不再读取**
（留着兼容别名 = 把"阿里 key 发到 api.deepseek.com"那条路继续开着）。旧名只用于
启动告警（:func:`lingering_legacy_judge_vars`），绝不拿它当凭证。

``MINIMAX_CN_API_KEY`` 是本树保留的 Python 侧并集（国内站 key 与海外站 key 是两套），
排在通用名之前，保持旧 Python 行为。选中主 key 走 ``openai`` 格式；否则走
``anthropic``（base 默认 MiniMax 的 anthropic 端点）。

## 二级端点兜底（v1.13.0）

主端点**任何端点级故障**（无 key / HTTP 非 2xx / 空正文 / 网络 / 超时）⇒ 用
``JUDGE_FALLBACK_BASE_URL`` / ``JUDGE_FALLBACK_API_KEY`` / ``JUDGE_FALLBACK_MODEL``
重试**一次**（格式恒 ``openai``）。三项缺一不生效：全缺 = off（正常），缺一两项 =
partial（启动 WARNING 点名，且不重试）。**只兜端点故障，不兜"分数不好"**。

``judge_failures_total`` 语义随之收紧为**最终失败次数**：兜底救回来的那次不计失败，
改由 ``judge_fallback_total`` / ``judge_fallback_ok_total`` 出声。

## ⚠️ ``aiohttp`` 必须在**函数体内**导入（不许搬到模块级）

本模块会被 ``platform/adapter.py`` 的加载链路拉起（adapter → 心流 → judge），而
``platform/adapter.py`` 是模块级被 ``registration._register_platform`` 导入的 ⇒
**本模块的加载期依赖就是注册期依赖**。``plugin_validate`` 的 probe 在 scratch 解释器里
跑 ``register()``，那里未必装了 aiohttp ⇒ 模块级 import 会让注册**静默失败**
（已由 ``tools/verify_tree.py`` 的 R2 硬拦，与 ``core/api_client.py`` 同一条理由）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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


#: v1.14.0 主端点 env 名（与 ``JUDGE_FALLBACK_*`` 对称）。
JUDGE_MAIN_VARS = {
    "base_url": "JUDGE_BASE_URL",
    "api_key": "JUDGE_API_KEY",
}

#: 兜底端点的三个 env（缺一个都不算启用，见 :func:`resolve_judge_fallback`）。
JUDGE_FALLBACK_VARS = {
    "base_url": "JUDGE_FALLBACK_BASE_URL",
    "api_key": "JUDGE_FALLBACK_API_KEY",
    "model": "JUDGE_FALLBACK_MODEL",
}

#: v1.14.0 旧名 —— **只用于启动告警，绝不再当凭证读**。留着兼容别名就等于把
#: "阿里 key 被框架当 DeepSeek key 用"那条路继续开着。
LEGACY_JUDGE_VARS = ("DEEPSEEK_BASE_URL", "DEEPSEEK_API_KEY")


@dataclass(frozen=True)
class JudgeFallback:
    """兜底端点（换一套 baseUrl/key **和** model —— 模型名跟端点是一对）。"""

    creds: JudgeCreds
    model: str


@dataclass(frozen=True)
class JudgeFallbackState:
    """兜底配置三态：``off``（全没设）/ ``partial``（半配，必须出声）/ ``on``。"""

    kind: str
    missing: tuple[str, ...] = ()
    target: JudgeFallback | None = None


def resolve_judge_creds(
    *,
    judge_base_url: str | None = None,
    minimax_base_url: str | None = None,
) -> JudgeCreds:
    """按环境变量链解析凭证：``JUDGE_API_KEY`` → ``MINIMAX_CN_API_KEY`` → ``MINIMAX_API_KEY``。

    注意选的是**哪个 key 存在**，不是"模型名里有没有 deepseek" ——
    旧 Python 侧按模型名分支，于是模型改名就换错端点（记忆里的 judge 事故之一）。

    主端点 baseUrl 链：显式覆盖 > ``JUDGE_BASE_URL`` > ``DEFAULT_JUDGE_ENDPOINTS["deepseek"]``。
    旧名 ``DEEPSEEK_BASE_URL`` / ``DEEPSEEK_API_KEY`` **不读**（v1.14.0 破坏性改名）。
    """
    judge_key = (os.environ.get(JUDGE_MAIN_VARS["api_key"]) or "").strip()
    if judge_key:
        base = (
            judge_base_url
            or (os.environ.get(JUDGE_MAIN_VARS["base_url"]) or "").strip()
            or DEFAULT_JUDGE_ENDPOINTS["deepseek"]
        )
        return JudgeCreds(api_key=judge_key, base_url=base.rstrip("/"), format="openai")

    minimax_key = (
        (os.environ.get("MINIMAX_CN_API_KEY") or "").strip()
        or (os.environ.get("MINIMAX_API_KEY") or "").strip()
    )
    base = minimax_base_url or DEFAULT_JUDGE_ENDPOINTS["minimax"]
    return JudgeCreds(api_key=minimax_key, base_url=base.rstrip("/"), format="anthropic")


def resolve_judge_fallback() -> JudgeFallbackState:
    """解析兜底端点（每次调用现读 env，不缓存 —— 同 TS，缓存会让测试互相污染）。

    格式恒 ``openai``：兜底点的既定用途就是 DeepSeek 系，与主端点同协议。
    """
    base_url = (os.environ.get(JUDGE_FALLBACK_VARS["base_url"]) or "").strip()
    api_key = (os.environ.get(JUDGE_FALLBACK_VARS["api_key"]) or "").strip()
    model = (os.environ.get(JUDGE_FALLBACK_VARS["model"]) or "").strip()
    missing: list[str] = []
    if not base_url:
        missing.append(JUDGE_FALLBACK_VARS["base_url"])
    if not api_key:
        missing.append(JUDGE_FALLBACK_VARS["api_key"])
    if not model:
        missing.append(JUDGE_FALLBACK_VARS["model"])
    if not missing:
        return JudgeFallbackState(
            kind="on",
            target=JudgeFallback(
                creds=JudgeCreds(
                    api_key=api_key, base_url=base_url.rstrip("/"), format="openai"),
                model=model,
            ),
        )
    if len(missing) == 3:
        return JudgeFallbackState(kind="off")
    return JudgeFallbackState(kind="partial", missing=tuple(missing))


def describe_judge_endpoint(creds: JudgeCreds) -> str:
    """一行端点自述 —— 只含 format / host / 路径 / key 有无，**绝不含 key 本身**。"""
    key_state = "set" if creds.api_key else "MISSING"
    try:
        u = urlsplit(creds.base_url)
        origin = f"{u.scheme}://{u.netloc}"
        path = (u.path or "").rstrip("/")
        return f"{creds.format} {origin}{path} key={key_state}"
    except ValueError:
        return f"{creds.format} {creds.base_url} (非法 baseUrl!) key={key_state}"


def describe_judge_fallback(state: JudgeFallbackState | None = None) -> str:
    """一行兜底自述（绝不含 key）。"""
    st = state or resolve_judge_fallback()
    if st.kind == "off":
        return "none"
    if st.kind == "partial":
        return f"MISCONFIGURED (缺 {', '.join(st.missing)})"
    assert st.target is not None
    return f"{describe_judge_endpoint(st.target.creds)} model={st.target.model}"


def lingering_legacy_judge_vars() -> list[str]:
    """还残留在进程 env 里的旧名（启动自述用；空列表 = 干净）。只点名，不打印值。"""
    return [name for name in LEGACY_JUDGE_VARS if (os.environ.get(name) or "").strip()]


_CONFIG_LOGGED = False


def log_judge_config_once() -> None:
    """启动自述：主端点 / 兜底状态 / 旧名残留。失败绝不影响调用方。"""
    global _CONFIG_LOGGED
    if _CONFIG_LOGGED:
        return
    _CONFIG_LOGGED = True
    try:
        creds = resolve_judge_creds()
        state = resolve_judge_fallback()
        log.info(
            "[WPP JUDGE] endpoint=%s fallback=%s",
            describe_judge_endpoint(creds), describe_judge_fallback(state),
        )
        if state.kind == "partial":
            log.warning(
                "[WPP JUDGE] 兜底端点半配、**不会启用**：缺 %s（三项必须同时给全）",
                ", ".join(state.missing),
            )
        legacy = lingering_legacy_judge_vars()
        if legacy:
            log.warning(
                "[WPP JUDGE] 旧 env 已作废、不会被读取：%s —— 请改名成 %s / %s",
                ", ".join(legacy), JUDGE_MAIN_VARS["base_url"], JUDGE_MAIN_VARS["api_key"],
            )
    except Exception:
        log.debug("[WPP JUDGE] 启动自述失败", exc_info=True)


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
    primary = creds or resolve_judge_creds()
    try:
        return await _call_judge_inner(
            model=model,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
            creds=primary,
            images=images,
        )
    except Exception as primary_err:
        state = resolve_judge_fallback()
        if state.kind != "on":
            # 兜底未配/半配 ⇒ 行为与 v1.12.0 逐字一致（原异常原样抛出）。
            metrics.inc_judge_failure()
            raise
        metrics.inc_judge_fallback()
        # 兜底被用到 = 主端点此刻是坏的，这是运维必须看见的事 ⇒ WARNING
        log.warning(
            "[WPP JUDGE] 主端点失败，转兜底端点重试: %s → %s | %s",
            describe_judge_endpoint(primary), describe_judge_fallback(state), primary_err,
        )
        assert state.target is not None
        try:
            out = await _call_judge_inner(
                model=state.target.model,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                timeout_ms=timeout_ms,
                creds=state.target.creds,
                images=images,
            )
            metrics.inc_judge_fallback_ok()
            return out
        except Exception as fallback_err:
            metrics.inc_judge_failure()
            # 两段都失败：报错必须同时点名两个端点（describe* 不含 key）。
            raise JudgeError(
                "judge: 主端点与兜底端点均失败"
                f" | primary({describe_judge_endpoint(primary)}) = {primary_err}"
                f" | fallback({describe_judge_fallback(state)}) = {fallback_err}"
            ) from fallback_err


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
            "judge: 无 apiKey（JUDGE_API_KEY / MINIMAX_CN_API_KEY / MINIMAX_API_KEY 都没设；"
            "旧名 DEEPSEEK_API_KEY 自 v1.14.0 起不再读取）")
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
    except TimeoutError as exc:
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
