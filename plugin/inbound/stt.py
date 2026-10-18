"""入站语音 → 文字：**vendor 转写优先，缺了才自己转**（对齐 TS v1.3.22）。

## 两条路，顺序不能反

1. **vendor 自带转写**（``voice.transcript``，微信官方出的）—— 已由
   :func:`plugin.inbound.media.extract_media_meta` 取到 ``media["vendor_transcript"]``。
   免费、即时、比第三方模型准 ⇒ **有就不下载、不转写**（TS ``voice.ts:100`` 同）。
2. **插件自己转**（本模块）—— silk 二进制 → 解码成 PCM → 套 44 字节 WAV 头 →
   发 STT 端点。两套后端：SiliconFlow（multipart，默认）与 Token Plan 原生
   ASR（JSON + base64，``WPP_STT_BACKEND=dashscope``，模型
   ``qwen-audio-3.0-asr-flash``）。要下载、要网络、要 key ⇒ 只在 ① 缺席时走。

TS 的两处实现供对照：``src/storage/stt.ts``（本模块的蓝图，含重试与 WAV 头）、
``src/storage/silk.ts:37 decodeSilkToPcm``（shell 出去调解码器，10s 超时）。

## 依赖：缺哪样都只是"没转写"，绝不挡住消息

===========================  ==============================================  ================
依赖                          来源                                            ``.183`` 现状
===========================  ==============================================  ================
silk 解码器二进制             ``WPP_SILK_DECODER_PATH``                        **不存在**
                              （默认 ``/root/silk_decoder/silk/decoder``）
STT key                      ``WECHATPRO_STT_API_KEY``                        **未配**
                              （``SILICONFLOW_API_KEY`` 兜底）
===========================  ==============================================  ================

⇒ ``.183`` 上"没有 vendor 转写的语音"转不出文字，模型只看到 ``[语音]`` 一行。
这**不是静默的**：:func:`unavailable_reason` 探测缺哪一样，调用方在**第一条**这样的
语音上打一条 WARNING（之后降 DEBUG —— 每条语音都喊会把日志淹掉，一条都不喊才是
本仓反复吃过的亏）。⚠️ 出站方向的 encoder 在 :mod:`plugin.platform.silk_encoder`。

## 为什么解码要外部二进制

微信语音是 SILK（SKP_SILK），不是 ffmpeg 认的常规编码 —— TS 侧同样是 shell 出去调
``decodeSilkToPcm``。纯 Python 重写 SKP_SILK 不划算，且 .183 上本来就没有。

## 为什么 HTTP 收在 ``_post_wav`` 一层

``aiohttp`` 在**函数体内**导入（判据同 :mod:`plugin.llm.judge`：``register()`` 的那个
解释器未必装了它，模块级 import 会让注册静默失败），并且把"发一次请求"与
"重试/分类/解析"分开 ⇒ 后者（真正会出错的地方）在本机（无 aiohttp、默认禁网）
也能被测到 —— 见 ``plugin/tests/test_voice.py``。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import tempfile

log = logging.getLogger(__name__)

#: silk 解码器二进制。默认值与 TS ``WPP_SILK_DECODER_PATH`` 一致（与 encoder 同目录）。
SILK_DECODER_PATH = os.environ.get("WPP_SILK_DECODER_PATH", "/root/silk_decoder/silk/decoder")

#: SiliconFlow STT 端点与模型（与 TS ``stt.ts`` 的默认值逐字一致）。
STT_URL = os.environ.get("SILICONFLOW_STT_URL", "https://api.siliconflow.cn/v1/audio/transcriptions")
STT_MODEL = os.environ.get("SILICONFLOW_STT_MODEL", "FunAudioLLM/SenseVoiceSmall")

#: Token Plan（阿里云百炼原生）STT：同一个 ``multimodal-generation`` 端点，
#: 请求体是 JSON + ``input_audio.data``（URL 或 ``data:audio/wav;base64,...``）。
#: ``WPP_STT_BACKEND`` 显式选路；不写时按 URL 里有没有 ``token-plan`` 自动判断，
#: 其余一律走 SiliconFlow，保持旧部署行为不变。
DASHSCOPE_STT_URL = os.environ.get(
    "WPP_STT_URL",
    "https://token-plan.cn-beijing.maas.aliyuncs.com"
    "/api/v1/services/aigc/multimodal-generation/generation",
)
DASHSCOPE_STT_MODEL = os.environ.get("WPP_STT_MODEL", "qwen-audio-3.0-asr-flash")

#: WAV 参数：24kHz / 单声道 / 16bit（TS ``stt.ts`` 的三个常量）。
WAV_SAMPLE_RATE = 24_000
WAV_CHANNELS = 1
WAV_BITS = 16

#: 重试次数与退避（TS：``for attempt = 1..3``，等待 ``attempt * 1000`` ms）。
MAX_ATTEMPTS = 3
RETRY_SLEEP_S = 1.0

#: TS ``stt.ts`` 里**值得重试**的状态码：401 / 429 / >=500。
#:
#: 401 重试看着奇怪（凭证错重试不会变对），但这是 TS 的原样语义：401 在这个 vendor
#: 上出现过"抖动"（同 key 隔一会儿就通）。照搬它，代价是配错 key 时每条语音多等 3 秒，
#: 收益是与蓝本行为一致 —— 改这里要先在 .183 上验，别凭直觉。
RETRYABLE_STATUSES = frozenset({401, 429})

#: 解码器超时（TS ``silk.ts``：10s，防 native binary hang）。
DECODE_TIMEOUT_S = 10.0
#: 单次 HTTP 超时（TS：60s）。
POST_TIMEOUT_S = 60.0


# ------------------------------------------------------------------ key / 探测


def stt_backend() -> str:
    """当前 STT 后端：``dashscope``（Token Plan 原生）或 ``siliconflow``（默认）。"""
    explicit = (os.environ.get("WPP_STT_BACKEND") or "").strip().lower()
    if explicit in ("dashscope", "tokenplan", "token-plan", "bailian"):
        return "dashscope"
    if explicit in ("siliconflow", "sensevoice"):
        return "siliconflow"
    if "token-plan" in (os.environ.get("SILICONFLOW_STT_URL") or "").lower():
        return "dashscope"
    return "siliconflow"


def siliconflow_api_key() -> str:
    """SiliconFlow STT key。``WECHATPRO_STT_API_KEY`` 优先（``plugin.yaml`` 声明的就是它），
    ``SILICONFLOW_API_KEY`` 兜底（TS 与旧 py 树用的是这个名字）。"""
    from ..core.env_compat import secret_env

    return (secret_env("WECHATPRO_STT_API_KEY")
            or secret_env("SILICONFLOW_API_KEY")
            or "").strip()


def dashscope_api_key() -> str:
    """Token Plan STT key：``WECHATPRO_STT_API_KEY`` 优先，百炼/网关 key 兜底。"""
    from ..core.env_compat import secret_env

    return (secret_env("WECHATPRO_STT_API_KEY")
            or secret_env("JUDGE_API_KEY")
            or secret_env("DASHSCOPE_API_KEY")
            or "").strip()


def stt_api_key() -> str:
    """按后端选 key —— 不让 SiliconFlow 的 key 被发给 Token Plan，反之亦然。"""
    return dashscope_api_key() if stt_backend() == "dashscope" else siliconflow_api_key()


def unavailable_reason() -> str | None:
    """为什么现在**不可能**转写出文字 —— ``None`` 表示两样依赖都在。纯探测，无副作用。

    调用方拿它决定"还值不值得试"，也拿它写降级日志（缺什么写什么，别只说"失败了"）。
    """
    if not os.path.exists(SILK_DECODER_PATH):
        return f"silk 解码器不存在: {SILK_DECODER_PATH}（装它，或配 WPP_SILK_DECODER_PATH）"
    if not os.access(SILK_DECODER_PATH, os.X_OK):
        return f"silk 解码器不可执行: {SILK_DECODER_PATH}"
    if not stt_api_key():
        if stt_backend() == "dashscope":
            return "未配 STT key（WECHATPRO_STT_API_KEY / JUDGE_API_KEY / DASHSCOPE_API_KEY）"
        return "未配 STT key（WECHATPRO_STT_API_KEY / SILICONFLOW_API_KEY）"
    return None


#: "降级日志只喊一次"的闸。缺依赖是**部署问题**，每条语音喊一次等于把日志淹掉。
_WARNED_DEGRADED = False


def note_degradation_once(reason: str) -> None:
    """记一条"语音转不了"的降级日志：**第一次 WARNING，之后 DEBUG**。

    为什么不每条都 WARNING：老板的群里语音不少，一条语音一行 WARNING 会把真正的
    异常淹掉。为什么不能一条都不喊：转不了写不进去东西，模型收到的就是 ``[语音]``
    一行 —— 这正是本仓最怕的**静默能力丢失**（09-11 judge 静瘫 3 天同一族）。
    """
    global _WARNED_DEGRADED
    if _WARNED_DEGRADED:
        log.debug("[STT] 语音转写仍不可用: %s", reason)
        return
    _WARNED_DEGRADED = True
    log.warning("[STT] 语音无法转写：%s ⇒ 这类语音在模型眼里只有 ``[语音]`` 一行", reason)


def reset_degradation_latch() -> None:
    """清掉"喊过了"的闸（测试用；也让 .183 上手动重新探测成为可能）。"""
    global _WARNED_DEGRADED
    _WARNED_DEGRADED = False


def should_attempt_stt(media: dict) -> bool:
    """还该不该**自己**转：vendor 已经给过转写就不该（省一次下载 + 一次 API 调用）。

    这个判断单独拎出来，是因为它只有"两个来源取哪个"这一件事 —— 混在调用方里就
    成了拿不到测试的那种代码（"来源反了"要等模型收到两遍文字才发现）。
    """
    return not str((media or {}).get("vendor_transcript") or "").strip()


# ---------------------------------------------------------------------- WAV


def build_wav_buffer(pcm: bytes) -> bytes:
    """PCM → WAV（44 字节 RIFF/WAVE 头 + 数据），24kHz 单声道 16bit。

    逐字段对齐 TS ``buildWavBuffer``（那一版有单测：v1.3.74 P2-2）。字段错一个
    （采样率、块对齐、两处长度）SiliconFlow 就返回 400 或识别成噪声 —— 而且听不见，
    只表现为"转写结果是乱码"，很难往回查到头部这 44 字节。
    """
    byte_rate = WAV_SAMPLE_RATE * WAV_CHANNELS * (WAV_BITS // 8)
    block_align = WAV_CHANNELS * (WAV_BITS // 8)
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, WAV_CHANNELS, WAV_SAMPLE_RATE, byte_rate, block_align, WAV_BITS
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


# ------------------------------------------------------------------- 解码


async def decode_silk_to_pcm(silk: bytes) -> bytes | None:
    """silk → PCM（调外部 decoder）。失败一律 ``None`` + WARNING（调用方降级）。"""
    if not os.path.exists(SILK_DECODER_PATH):
        log.warning("[STT] silk 解码器不存在: %s ⇒ 无法转写", SILK_DECODER_PATH)
        return None

    silk_path = pcm_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as fh:
            fh.write(silk)
            silk_path = fh.name
        pcm_path = silk_path + ".pcm"

        proc = await asyncio.create_subprocess_exec(
            SILK_DECODER_PATH, silk_path, pcm_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=DECODE_TIMEOUT_S)
        if proc.returncode != 0 or not os.path.isfile(pcm_path):
            log.warning("[STT] silk 解码失败（rc=%s）: %s",
                        proc.returncode, (err or b"")[:200])
            return None

        with open(pcm_path, "rb") as fh:
            pcm = fh.read()
        # 空 PCM 别往下送：套出来的 WAV 是 0 字节数据，API 只会回 400
        return pcm or None
    except Exception as exc:  # noqa: BLE001 —— 解码失败绝不能冒泡成"消息处理失败"
        log.warning("[STT] silk 解码异常: %s", exc)
        return None
    finally:
        for path in (silk_path, pcm_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# --------------------------------------------------------------------- HTTP


async def _post_wav(url: str, api_key: str, wav: bytes, timeout_s: float) -> tuple[int, str]:
    """POST **一次** multipart，返回 ``(status, body_text)``；网络异常上抛给调用方分类重试。

    字段顺序与名字对齐 TS ``stt.ts``：``file``（``voice.wav``/``audio/wav``）→
    ``model`` → ``response_format=json``。
    """
    import aiohttp  # 延迟导入，见模块 docstring

    form = aiohttp.FormData()
    form.add_field("file", wav, filename="voice.wav", content_type="audio/wav")
    form.add_field("model", STT_MODEL)
    form.add_field("response_format", "json")
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            url, data=form,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            return resp.status, await resp.text()


def build_dashscope_payload(wav: bytes) -> dict:
    """WAV → Token Plan 原生 ASR 请求体。

    ``input_audio.data`` 收 URL 或 ``data:audio/<fmt>;base64,...``；语音来自
    silk 解码，本地没有公网 URL，所以只能内联 base64。采样率必须与
    :func:`build_wav_buffer` 写进 WAV 头的值一致（24kHz），否则识别成噪声。
    """
    encoded = base64.b64encode(wav).decode("ascii")
    return {
        "model": DASHSCOPE_STT_MODEL,
        "input": {
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {"data": f"data:audio/wav;base64,{encoded}"},
                }],
            }],
        },
        "parameters": {"format": "wav", "sample_rate": str(WAV_SAMPLE_RATE)},
    }


async def _post_dashscope(url: str, api_key: str, wav: bytes, timeout_s: float) -> tuple[int, str]:
    """POST **一次** JSON 到 Token Plan 原生 ASR 端点，返回 ``(status, body_text)``。"""
    import aiohttp  # 延迟导入，同 _post_wav

    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            url, json=build_dashscope_payload(wav),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            return resp.status, await resp.text()


def _extract_text(body: str) -> str:
    """从响应体里取文本。TS 取 ``text || transcript``（SiliconFlow 两种都见过）。"""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("text") or data.get("transcript") or "").strip()


async def _attempt(url: str, api_key: str, wav: bytes) -> tuple[str | None, bool, str]:
    """一次尝试 → ``(文本, 是否值得重试, 失败描述)``。**不抛异常**（连超时也不抛）。"""
    post = _post_dashscope if stt_backend() == "dashscope" else _post_wav
    try:
        status, body = await post(url, api_key, wav, POST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 —— 网络/超时都按"值得重试"处理（TS 同）
        return None, True, f"{type(exc).__name__}: {exc}"

    if status == 200:
        text = _extract_text(body)
        # 200 却空：重试也是一样的结果（对方已经收下了），所以**不值得重试**
        return (text or None), False, ("" if text else "HTTP 200 但正文里没有 text")

    retryable = status in RETRYABLE_STATUSES or status >= 500
    return None, retryable, f"HTTP {status}: {body[:200]}"


async def transcribe_silk_buffer(silk: bytes, api_key: str = "") -> str | None:
    """silk buffer → 转写文字；**任何失败都返回 ``None``**（调用方决定正文怎么写）。

    ⚠️ 返回 ``None`` 与返回空串是**两件事**，本函数只返回 ``None``：
    空串会让调用方以为"转出来是空的"，从而在正文里写下一行空的 ``[转写]``。
    """
    backend = stt_backend()
    key = api_key.strip() or stt_api_key()
    if not key:
        if backend == "dashscope":
            log.warning("[STT] 未配 Token Plan STT key"
                        "（WECHATPRO_STT_API_KEY / JUDGE_API_KEY / DASHSCOPE_API_KEY）⇒ 跳过转写")
        else:
            log.warning("[STT] 未配 STT key（WECHATPRO_STT_API_KEY / SILICONFLOW_API_KEY）⇒ 跳过转写")
        return None

    pcm = await decode_silk_to_pcm(silk)
    if not pcm:
        return None
    wav = build_wav_buffer(pcm)
    url = DASHSCOPE_STT_URL if backend == "dashscope" else STT_URL

    detail = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text, retryable, detail = await _attempt(url, key, wav)
        if text:
            return text
        if not retryable or attempt == MAX_ATTEMPTS:
            break
        log.warning("[STT] 第 %d/%d 次失败（%s），%d 秒后重试",
                    attempt, MAX_ATTEMPTS, detail, int(attempt * RETRY_SLEEP_S))
        await asyncio.sleep(attempt * RETRY_SLEEP_S)

    log.warning("[STT] 转写失败: %s", detail)
    return None
