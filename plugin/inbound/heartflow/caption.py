"""心流图片配文（v1.16.0，对齐 TS ``src/inbound/heartflow-caption.ts``）。

老板 2026-10-16 的问题：「喂给画像 LLM 的样本能否加入图片识别后的数据？」—— 能，且这是画像
失真的结构根：全员群 14 天 44/78 条是图片，而画像 prompt 的样本只有
``sender: 收到一张图片 [图片] https://…`` ⇒ 归纳模型**永远看不见晒单图长什么样**，
只能从剩余 28 条文本归纳出「冷静通报群」⇒ engage 清单缺晒单口径 ⇒ judge 给晒单图 0.635
（对照已含晒单口径的群 0.775）。

## 机制（只在画像生成前跑，每群每次生成最多补几张；judge 热路径零新增开销）

``list_hf_image_msgs``（近 ``lookbackDays`` 图片入站）→ ``extract_hf_image_urls`` 抠 URL
→ sha1 幂等键 → 批量查 ``wpp_hf_image_caption`` 已试集合 → 未配文且 ``fail_count < 上限``
的送 ``call_judge(images=[url])`` → 成功存配文；失败 ``fail_count + 1`` **止损不重试到死**
（同图跨群共享 hash，只配一次）。``profile._load_sample_lines`` 再把
``收到一张图片 [图片] URL`` 换成 ``[图] <配文>``（顺带把 URL 噪声从样本里清掉）。

## 三条边界（如实声明）

1. **配文是有损投影** —— 40 字进不了成交截图的全部细节；它服务于「这群日常在发什么」的
   归纳，不服务于像素级复核。
2. **vision 端点与 judge 同凭据**（``JUDGE_*``）；端点坏了 caption 静默失败 ⇒ 样本退回旧形态
   （无配文=占位），画像生成**不受阻塞** —— 这是红线：增强件不许变成依赖件。
3. ``perGroupMax`` 截的是「单次生成补做额度」，不是「群里只配这么多图」—— 老图随
   ``lookbackDays`` 窗口轮换排队补。⚠️ 上线首日实测（TS 2026-10-16）：全员群画像样本窗跨
   13.9 天、含 44 张图（占 55%），配额 6 张/次 ⇒ 存量排完要 ~15 天；稳态到达 3.1 张/天 < 配额
   6，追得上。**慢的只有首轮存量** ⇒ 上线/新加群后应手动跑一次排空（``perGroupMax=60``）。

## Py 侧与 TS 的一致点（别当疏漏）

- ``images`` 仅 ``openai`` 格式端点生效；``anthropic`` 分支会丢弃图片，模型对着「描述这张图片」
  凭空胡说 ⇒ 必须挡在调用前（v1.11.0 看图边界同款）。
- 失败原因**必须出声**：只落 ``fail_count`` 会有「图一直没配文却查不出为什么」的静默盲区
  （端点不支持 vision / OSS 403 / 超时 在库里长得一模一样）。单图最多 ``maxFails`` 次，不担心刷屏。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: 图片配文缺省参数（挂 ``heartflow.profile.caption``，不进 UI schema）
HF_CAPTION_DEFAULTS: dict[str, Any] = {
    #: 总开关（默认 True —— 老板 2026-10-16 拍板「直接优化到位」；False 可整体关掉，样本退回旧形态）
    "enabled": True,
    #: 单次画像生成前最多补配几张（默认 6；防一次生成打爆 LLM 配额）
    "perGroupMax": 6,
    #: 同一张图最多试几次（默认 2；到次数即止损，不再重试）
    "maxFails": 2,
    #: 队列源扫描的近 N 条图片入站消息（默认 150 条，按 id 倒序）
    "scanLimit": 150,
    #: 单张配文调用超时（毫秒；默认 8000 —— 比 judge 5s 宽，比画像 20s 严）
    "timeoutMs": 8000,
}


@dataclass(frozen=True)
class HfCaptionConfig:
    """**已解析**的配文参数（缺省已填满）—— 对应 TS 的 ``Required<HfCaptionConfig>``。"""

    enabled: bool
    per_group_max: int
    max_fails: int
    scan_limit: int
    timeout_ms: int


def _num(v: Any) -> float | None:
    """``Number.isFinite`` 等价：``bool`` **不算数**（本仓脏配置口径，见 ``profile._is_num``）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _int_or(v: Any, fallback: int, *, low: int, high: int) -> int:
    """取整并夹到 ``[low, high]``；非数 / ``bool`` / ``NaN`` ⇒ ``fallback``。

    TS 的 ``Math.max(a, Math.floor(cfg?.x ?? default))`` 遇到脏串会得 ``NaN`` 并一路流下去
    （``NaN <= 0`` 为假 ⇒ 开关失效 / 配额恒空）。Py 侧按本仓一贯口径**回落缺省**。
    """
    n = _num(v)
    if n is None:
        return fallback
    return max(low, min(int(math.floor(n)), high))


def resolve_hf_caption_cfg(cfg: Mapping[str, Any] | None) -> HfCaptionConfig:
    """把 ``heartflow.profile.caption`` 解析成已填满的 :class:`HfCaptionConfig`。

    ``cfg`` 是 **caption 子映射本身**（与 TS ``resolveHfCaptionCfg(P.caption)`` 同）。
    形状脏（整段是字符串/列表）⇒ 全部走缺省。
    """
    D = HF_CAPTION_DEFAULTS
    c = cfg if isinstance(cfg, Mapping) else {}
    return HfCaptionConfig(
        enabled=bool(c.get("enabled", D["enabled"])),
        per_group_max=_int_or(c.get("perGroupMax"), D["perGroupMax"], low=0, high=1000),
        max_fails=_int_or(c.get("maxFails"), D["maxFails"], low=1, high=100),
        scan_limit=_int_or(c.get("scanLimit"), D["scanLimit"], low=1, high=500),
        timeout_ms=_int_or(c.get("timeoutMs"), D["timeoutMs"], low=1000, high=120_000),
    )


# ================================================================ 纯函数


def hf_caption_hash(url: str) -> str:
    """幂等键：``sha1(图片 URL)`` 十六进制。

    URL 是 OSS 直链（内容 hash 已在文件名里，但 URL 字符串本身是稳定标识）。
    """
    return hashlib.sha1(str(url).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HfCaptionTask:
    """单张图待配任务。"""

    url: str
    url_hash: str
    group_id: str


def _extract_urls(text: Any, max_images: int) -> list[str]:
    """延迟导入 ``extract_hf_image_urls`` —— ``heartflow`` 反向 import 本包的 ``profile``，
    模块级 import 会成环。"""
    from .heartflow import extract_hf_image_urls

    return extract_hf_image_urls(text, max_images)


def select_hf_caption_tasks(
    msgs: Sequence[Mapping[str, Any]],
    existing: Sequence[Mapping[str, Any]],
    *,
    max_tasks: int,
    max_fails: int,
) -> list[HfCaptionTask]:
    """从图片消息行挑出「还没配文且没试死」的 URL（纯函数，可单测）。

    去重规则：同 URL 只出一个任务；已有 caption 跳过；无行=首次；
    ``caption=None`` 且 ``fail_count < maxFails``=重试。
    """
    rows = {str(r.get("url_hash") or ""): r for r in existing}
    seen: set[str] = set()
    out: list[HfCaptionTask] = []
    for m in msgs:
        content = m.get("content")
        if not content:
            continue
        for url in _extract_urls(content, 4):
            h = hf_caption_hash(url)
            if h in seen:
                continue
            seen.add(h)
            row = rows.get(h)
            if row is not None:
                if row.get("caption") is not None:
                    continue
                if int(row.get("fail_count") or 0) >= max_fails:
                    continue
            out.append(HfCaptionTask(url=url, url_hash=h,
                                     group_id=str(m.get("group_id") or "")))
            if len(out) >= max_tasks:
                return out
    return out


def clean_hf_caption(text: Any) -> str:
    """配文清洗：压空白 + 限长 60（画像样本行本身限 80 字，配文占大头会挤掉 sender）。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:60]


def hf_caption_sample_line(body: str, captions_by_hash: Mapping[str, str]) -> str:
    """画像样本行装配（纯函数）：图片占位体换配文。

    - 有配文：``[图] <配文>``（URL 噪声一并清掉 —— 它对归纳毫无价值还吃截断预算）
    - 无配文：``收到一张图片``（退回旧信息量，但比留 60 字符 URL 好）
    - 非图片体：原样返回
    """
    if "[图片]" not in (body or ""):
        return body
    urls = _extract_urls(body, 1)
    first = urls[0] if urls else None
    cap = captions_by_hash.get(hf_caption_hash(first)) if first else None
    return f"[图] {cap}" if cap else "收到一张图片"


# ================================================================ IO 编排


@dataclass
class HfCaptionPassResult:
    """一次补配文 pass 的计数（调用方打日志/断言用）。"""

    tasks: int = 0
    ok: int = 0
    fail: int = 0


async def caption_pending_hf_images(
    account_id: str, group_id: str, since_sec: int, cfg: HfCaptionConfig,
    model: str, creds: Any,
) -> HfCaptionPassResult:
    """一群的补配文 pass（在 ``maybe_generate_hf_group_profiles`` 逐群生成前调用）。

    全部失败吞掉返回计数 —— 调用方再包一层 try/except；任何 DB/网络抖动都不许阻断画像生成。
    """
    out = HfCaptionPassResult()
    if not cfg.enabled or cfg.per_group_max == 0:
        return out
    # 与 judge 看图同规：images 仅 openai 格式端点生效；anthropic 分支会丢弃图片，
    # 模型对着「描述这张图片」凭空胡说 ⇒ 必须挡在调用前（v1.11.0 images 边界同款）
    if creds is None or getattr(creds, "format", None) != "openai":
        return out

    from ...data import repo_hf
    from ...llm import judge as judge_mod  # 延迟导入：LLM 层只在真要调用时拉进来

    msgs = repo_hf.list_hf_image_msgs(account_id, [group_id], since_sec, cfg.scan_limit)
    if not msgs:
        return out

    # 先抠出本批全部 URL，一次批量查已试集合（不逐条查库）
    hashes: list[str] = []
    seen: set[str] = set()
    for m in msgs:
        content = m.get("content")
        if not content:
            continue
        for url in _extract_urls(content, 4):
            h = hf_caption_hash(url)
            if h in seen:
                continue
            seen.add(h)
            hashes.append(h)
    if not hashes:
        return out
    existing = repo_hf.get_hf_captions_by_hashes(account_id, hashes)

    tasks = select_hf_caption_tasks(
        msgs, existing, max_tasks=cfg.per_group_max, max_fails=cfg.max_fails)
    out.tasks = len(tasks)

    for t in tasks:
        try:
            text = await judge_mod.call_judge(
                model=model,
                user_prompt=(
                    "用一句话(40字内)描述这张微信群图片的内容要点。若是截图, "
                    "读出关键文字/数字/商品/金额/结果。只回描述本身, 不要前缀或解释。"
                ),
                system_prompt="你是图片内容记录员, 只输出一句中文描述。",
                max_tokens=120,
                timeout_ms=cfg.timeout_ms,
                creds=creds,
                images=[t.url],
            )
            clean = clean_hf_caption(text)
            if not clean:
                raise ValueError("empty caption")
            repo_hf.upsert_hf_image_caption(
                account_id, t.url_hash, group_id=t.group_id,
                url_sample=t.url[:255], caption=clean, model=model, fail_delta=0,
            )
            out.ok += 1
        except Exception as exc:  # noqa: BLE001 —— 单张失败不许阻断整批，更不许阻断画像
            out.fail += 1
            # v1.16.0: 失败原因必须出声 —— 只落 fail_count 会有「图一直没配文却查不出为什么」
            #   的静默盲区（端点不支持 vision / OSS 403 / 超时 在库里长得一模一样）。
            #   单图最多 maxFails 次，不担心刷屏。
            log.warning("[WPP HF] caption failed (%s): %s", t.url_hash[:8], exc)
            # 失败也要落行：caption NULL + fail_count+1 ⇒ 下次按 maxFails 止损
            try:
                repo_hf.upsert_hf_image_caption(
                    account_id, t.url_hash, group_id=t.group_id,
                    url_sample=t.url[:255], caption=None, model=model, fail_delta=1,
                )
            except Exception as db_exc:  # noqa: BLE001
                log.warning("[WPP HF] caption fail-log error: %s", db_exc)
    return out


def load_hf_captions_for_bodies(
    account_id: str, bodies: Sequence[str],
) -> dict[str, str]:
    """样本装配用的批量配文查取（``_load_sample_lines`` 图片行换字面）。

    失败返回空 dict ⇒ 样本退回旧形态（增强件永不变成依赖件）。
    """
    map_: dict[str, str] = {}
    try:
        hashes: list[str] = []
        seen: set[str] = set()
        for b in bodies:
            if not b or "[图片]" not in b:
                continue
            for url in _extract_urls(b, 4):
                h = hf_caption_hash(url)
                if h in seen:
                    continue
                seen.add(h)
                hashes.append(h)
        if not hashes:
            return map_
        from ...data import repo_hf
        rows = repo_hf.get_hf_captions_by_hashes(account_id, hashes)
        for r in rows:
            cap = r.get("caption")
            if cap:
                map_[str(r.get("url_hash") or "")] = str(cap)
    except Exception as exc:  # noqa: BLE001 —— 查不到配文 ≠ 画像生成失败
        log.warning("[WPP HF] caption lookup failed (样本退回旧形态): %s", exc)
    return map_
