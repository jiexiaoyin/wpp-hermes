"""小程序卡片（W7-2，移植 TS ``inbound/app-card.ts`` 267 行）。

## 为什么值得单独一个模块

v1.6.1 那次实证（TS 文件头记着）：老板转发"国补"小程序卡片给小助理，小助理**只看到一行标题**
（"快来领取政府家电数码补贴，单件最高补1500元"）并回「转发时只带了文字」—— **那个归因是错的**。
生产库里同一条 49 消息的 ``raw_payload`` 里 ``app.mini_program.{app_id,page_path,username,
source_display_name}`` + ``description`` + ``cover_image`` 全都在，只是入站链路除了接龙
（``category=app_message``，见 :mod:`inbound.relay`）之外**对 49 没有任何解析**，
``app`` 块只落进 raw 审计 ⇒ 从不进 LLM，模型拿到手的只有厂商 ``content`` 字段（=标题）。

⇒ 本模块把「这是哪个小程序的哪个页面 + 卡片说明 + 封面/图标」变成正文的一部分。

## 边界（别指望更多）

小程序**页面内容**拿不到 —— 页面是客户端渲染 + 需要登录态，厂商推送里没有正文。
本模块的天花板 = 卡片元信息（哪个小程序 / 哪个页面 / 活动 id）+ 封面图。
读「补贴档位 / 领取流程」必须换路子（搜公开政策 / 看图），靠解析消息做不到。

## INVARIANT: W7-2（三条）

1. **只认 ``app.category == "mini_program"``**（逐字等值，大小写敏感 —— TS 是 ``===``）。
   同族的 49 消息不会被误命中：接龙/历史记录 = ``app_message``、文件 = ``file``、
   引用 = ``quote``、转账 = ``payment_notice``。
   ⚠️ py2 原先把这个类型落到 ``_media_tag`` 的最后一道兜底 ``[文件]`` —— 那是个**错标签**，
   正是本项要修的形态（"小助理说转发只带了文字"是同一类误判的另一面）。
2. **文本化是"追加"，不改厂商 content**；``[小程序卡片] 标题`` 这一行只在正文里
   **还没有**这个标题时才打（TS ``omitTitle``）—— 否则标题会出现两遍。
3. **资产富化永不抛**：缺凭据 / 厂商返错 / 上传失败一律降级成"这条消息不带图"，
   与 TS 六个 enrich 入口同款。⚠️ 它**必须**降级：拿不到封面图的卡片照样是有效消息。

## 未移植的一截（**有意不搬，不是漏了**）

TS 的 ``coverThumbToken``（``app-card.ts:110``）+ ``formatThumbToken`` / ``parseThumbToken``
（``send/msg.ts:299-330``）没搬，连 ``[小程序封面凭据]`` 那一行也**不注入**。理由是一条链：

* 令牌（``xcxthumb:…``）的唯一消费者是 TS **出站**的 ``buildMiniProgramCardXml`` ——
  把凭据解进 ``<appattach>`` 再发 ``/Msg/SendXCX {ToWxid, Content: <XML>}``；
* py2 的出站是**结构化**的另一条路（``api_client.send_xcx`` → ``{toWxid, xcxTitle, xcxDesc,
  xcxUrl, xcxAppId, thumbUrl}``），工具面连 ``pagePath`` / ``username`` 参数都没有，
  ``thumbUrl`` **原样**发给厂商；
* ⇒ 令牌写进 prompt 会得到一句"照做就坏"的指令：模型把 ``xcxthumb:…`` 填进 ``thumbUrl``，
  厂商按 URL 解析失败 ⇒ 卡片是**灰块**，而模型以为自己保住了原封面。

**这就是本计划开头批的那种"说谎的开关"再上一层**（那句提示语本身在说谎）⇒ 宁可不给。
:func:`format_mini_program_card` 仍保留 ``cover_token`` 形参（与 TS 签名一致、有单测覆盖），
将来补出站 XML 卡片（TS v1.6.2 / v1.6.4 那条线）时**只需在调用点补一个实参**。
:func:`mini_program_block` 里有一条测试钉着"输出里永不出现 ``xcxthumb:``"。

## 另两条与 TS 有意不齐（都写在这里）

* **下载路在真卡片上恒失败**（TS 实测：端点返 ``异常：小程序封面下载失败：CDN 返回错误码
  -5103017``；同凭据打 ``/Tools/CdnDownloadImage`` 同样失败）⇒ 实际能拿到的通常是
  ``app.icon_url`` 的 140×140 图标。这是**厂商服务端实现**的问题，不是凭据问题
  （同一组凭据原样透传给 ``/Msg/SendXCX`` 时，老板手机上显示的就是原卡片那张 720×576 封面）。
  所以本模块照 TS 先试封面、失败降级到图标，并在结果里**如实标出拿到的是哪一种**
  （``kind``）—— 混标会让模型以为看到了卡片大图。
* **日志级别**：每次尝试失败按 TS 打 ``warning``（封面→图标的降级过程看得见），
  成功打 ``debug``；调用方（adapter）在拿不到图时补一行 ``info``。
"""

from __future__ import annotations

import base64
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from .media_oss import load_oss_config, oss_upload_buffer, sanitize_filename_part

log = logging.getLogger(__name__)

#: 厂商封面/图标下载端点（TS ``app-card.ts:239``）。返回 ``Data.Image`` = base64。
DOWNLOAD_COVER_ENDPOINT = "/Tools/DownloadMiniProgramCover"

#: 卡片里那个"小程序"分类的**唯一**判据值。
CATEGORY_MINI_PROGRAM = "mini_program"

#: OSS 目录名（TS ``ossUploadBuffer(buf, filename, "wpp/miniprogram", {type:"miniprogram"})``）。
OSS_PREFIX = "wpp/miniprogram"
OSS_KIND = "miniprogram"

#: 图太小就认为不是图（TS ``if (buf.length < 100) throw``）。
MIN_ASSET_BYTES = 100

_DIGITS_RE = re.compile(r"^\d+$")


# ------------------------------------------------------------------ 取值原语


def _as_record(value: Any) -> dict:
    """``dict`` 才算 record（TS ``asRecord``：排除数组与 null）。取不到返空 dict。"""
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str:
    """非空字符串（trim 后）才算数，否则空串。

    TS ``str()`` 返 ``undefined``；py2 用**空串**当"没有"这个哨兵（本模块所有可选字段
    都是 ``str`` 且有默认值，不用 ``None`` —— 少一层判空，且 ``"" or x`` 的短路与 TS 一致）。
    """
    if isinstance(value, str):
        s = value.strip()
        if s:
            return s
    return ""


def _num(value: Any) -> int | float | None:
    """数字（TS ``num()``：有限 number，或纯数字字符串）。

    ⚠️ **``bool`` 要先挡掉**：Python 里 ``isinstance(True, int)`` 为真，而 JS 的
    ``typeof true === "boolean"`` ⇒ TS 返 ``undefined``。不挡的话 ``version: true``
    会变成 `1` 混进令牌/格式串（W3 在 ``groupContextWindow`` 上踩过同一个坑）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and _DIGITS_RE.match(value.strip()):
        return int(value.strip())
    return None


def _first(*values: Any) -> str:
    """``a or b`` 链：取第一个非空字符串（TS ``str(x) ?? str(y)``）。"""
    for v in values:
        s = _str(v)
        if s:
            return s
    return ""


def _first_num(*values: Any) -> int | float | None:
    for v in values:
        n = _num(v)
        if n is not None:
            return n
    return None


# ---------------------------------------------------------------------- 数据


@dataclass(frozen=True)
class MiniProgramCover:
    """卡片封面 / 图标（TS ``MiniProgramCoverCtx``）。字段缺失都是空串 / ``None``。"""

    url: str = ""
    file_no: str = ""
    file_aes_key: str = ""
    md5: str = ""
    #: 封面字节数（``cdn_length``）—— 转发时 ``cdnthumblength`` 用
    length: int | float | None = None
    width: int | float | None = None
    height: int | float | None = None
    #: ``app.icon_url`` —— 140×140 小程序图标（封面下不动时的降级资产）
    icon_url: str = ""


@dataclass(frozen=True)
class MiniProgramCard:
    """结构化后的卡片信息（TS ``MiniProgramCardInfo``）。"""

    title: str = ""
    description: str = ""
    app_id: str = ""
    page_path: str = ""
    #: ``gh_xxx@app``（小程序 username）
    username: str = ""
    source_display_name: str = ""
    #: ``weappinfo.version``（卡片自带的小程序版本号）
    version: int | float | None = None
    cover: MiniProgramCover | None = None


@dataclass(frozen=True)
class AssetAttempt:
    """一次下载尝试：``cover`` = 卡片封面，``icon`` = 小程序图标。"""

    kind: str
    body: dict


@dataclass(frozen=True)
class AssetResult:
    """富化结果（TS ``MiniProgramAssetResult``）：``media_url`` 为空 ⇒ 这条消息不带图。"""

    media_url: str = ""
    media_size: int | None = None
    #: "cover" / "icon" —— **如实标出**拿到的是哪一种（空 = 没拿到）
    kind: str = ""
    error: str = ""


# ---------------------------------------------------------------------- 解析


def is_mini_program_card(raw: Any) -> bool:
    """是不是小程序卡片：``raw.app.category == "mini_program"``。**不抛**（形状随便给）。"""
    return _str(_as_record(_as_record(raw).get("app")).get("category")) == CATEGORY_MINI_PROGRAM


def parse_mini_program_card(raw: Any) -> MiniProgramCard | None:
    """``app`` 块 → :class:`MiniProgramCard`。字段缺失不抛；不是小程序卡片返 ``None``。"""
    app = _as_record(_as_record(raw).get("app"))
    if _str(app.get("category")) != CATEGORY_MINI_PROGRAM:
        return None
    mp = _as_record(app.get("mini_program"))
    cover_raw = app.get("cover_image")
    cover = _as_record(cover_raw)
    # download_context = {endpoint, file_no, file_aes_key, variant} —— 可直接当下载端点请求体
    dc = _as_record(cover.get("download_context"))
    cdn = _as_record(cover.get("cdn"))
    parsed_cover = None
    # ⚠️ 判"这个块在不在"必须看**原值**是不是 dict，不能用 ``if cover:`` ——
    #    JS 里 ``{}`` 是**真值**、Python 里是**假值**，同一份 payload 会在这里分叉
    #    （与 ``_num`` 挡 ``bool``、``media_oss`` 的 ``re.ASCII`` 同一族：JS/Python 的真值/字符类不同）。
    if isinstance(cover_raw, dict):
        parsed_cover = MiniProgramCover(
            url=_first(dc.get("url"), cover.get("url")),
            file_no=_first(dc.get("file_no"), cover.get("file_no")),
            file_aes_key=_first(dc.get("file_aes_key"), cover.get("aes_key")),
            md5=_str(cover.get("md5")),
            length=_first_num(cover.get("cdn_length"), cdn.get("length")),
            width=_num(cover.get("width")),
            height=_num(cover.get("height")),
            icon_url=_str(app.get("icon_url")),
        )
    return MiniProgramCard(
        title=_str(app.get("title")),
        description=_str(app.get("description")),
        app_id=_str(mp.get("app_id")),
        page_path=_str(mp.get("page_path")),
        username=_str(mp.get("username")),
        source_display_name=_str(mp.get("source_display_name")),
        version=_num(mp.get("version")),
        cover=parsed_cover,
    )


# -------------------------------------------------------------------- 文本化


THUMB_TOKEN_PREFIX = "xcxthumb:"
"""TS 的凭据令牌前缀（``send/msg.ts:283``）。

⚠️ 本模块**只认这个值用来"永不产出它"**（见模块 docstring「未移植的一截」）——
没有 ``format_thumb_token`` / ``parse_thumb_token``：它们在 py2 没有消费者，
写出来就是死码（本计划批的就是这个形态）。
"""


def format_mini_program_card(
    info: MiniProgramCard,
    *,
    cover_url: str = "",
    icon_url: str = "",
    cover_token: str = "",
    omit_title: bool = False,
) -> str:
    """文本化（追加进 content，与 ``[图片]`` / ``[文件]`` 注入同一约定）。

    ``omit_title``：厂商 ``content`` 本身就是标题 ⇒ 调用方传 ``True`` 去重。
    描述与小程序名相同时只打一行（国补卡片实测两者都是「国家消费品换新家电补贴微信端」）。
    """
    lines: list[str] = []
    if info.title and not omit_title:
        lines.append(f"[小程序卡片] {info.title}")

    who = info.source_display_name or info.username or "小程序"
    # TS v1.6.2 XCX-PAGEPATH：username（gh_xxx@app）必须一起打出来 —— 它是"按原样转发给客户"
    #   的必备参数；只给 appid 时转不出内部页面，而模型自己编不出 gh_ 值（它只在 raw 里）。
    ids = [f"appid {info.app_id}" if info.app_id else "", info.username]
    id_part = f" ({', '.join(x for x in ids if x)})" if any(ids) else ""
    lines.append(f"[小程序] {who}{id_part}")
    if info.page_path:
        lines.append(f"页面路径: {info.page_path}")
    if info.description and info.description != info.source_display_name:
        lines.append(f"说明: {info.description}")
    # 封面（720×576 卡片图）与图标（140×140 logo）必须分开标注 —— 混标会让模型以为看到了大图。
    if cover_url:
        lines.append(f"[小程序封面] {cover_url}")
    if icon_url:
        lines.append(f"[小程序图标] {icon_url}")
    # ⚠️ 这一行当前**没有调用方会传** ``cover_token``（理由见模块 docstring）。保留形参是
    #    为了与 TS 签名一致：补上出站 XML 卡片后，调用点补一个实参即可，不必动这里。
    if cover_token:
        lines.append(f"[小程序封面凭据] {cover_token} (转发时原样填 thumbUrl)")
    return "\n".join(lines)


def mini_program_block(msg: Any) -> str:
    """消息 → 追加块（非小程序卡片返空串）。**封面 URL 由 adapter 富化后写进 ``msg.media``。**

    这是本模块对入站链路的**唯一入口**：``prepare_text`` 在打了 ``[图片]`` 一类标记之后
    调它。读 ``msg.media`` 的两个键（都由 ``_fetch_media`` 的 W7-2 分支写入）::

        mp_asset_url  —— 富化到的公网 URL（拿不到就没有这个键）
        mp_asset_kind —— "cover" / "icon"，决定这 URL 该标成封面还是图标

    ⚠️ ``omit_title`` 与 TS 一样**拿厂商 content 原文**比（不是去 @ 之后的正文）：
    TS 那边比的就是 ``m.content``，两处口径必须一致，否则同一张卡片在两边打出的行数不同。
    """
    card = parse_mini_program_card(getattr(msg, "raw", None))
    if card is None:
        return ""
    media = getattr(msg, "media", None)
    media = media if isinstance(media, dict) else {}
    url = _str(media.get("mp_asset_url"))
    kind = _str(media.get("mp_asset_kind"))
    return format_mini_program_card(
        card,
        cover_url=url if kind == "cover" else "",
        icon_url=url if kind == "icon" else "",
        # 有意不传 cover_token —— 见模块 docstring「未移植的一截」。
        omit_title=(not card.title) or (card.title in _str(getattr(msg, "content", ""))),
    )


# -------------------------------------------------------------------- 下载路


def plan_asset_attempts(cover: MiniProgramCover | None) -> list[AssetAttempt]:
    """按优先级排下载尝试（纯函数）：

    ① cover：``download_context.url`` 优先（厂商文档说是"已校验的官方封面直链"），
       否则 ``file_no + file_aes_key``；
    ② icon：``app.icon_url``（只在①拿不到时降级）。
    """
    if cover is None:
        return []
    attempts: list[AssetAttempt] = []
    if cover.url:
        attempts.append(AssetAttempt("cover", {"url": cover.url}))
    elif cover.file_no and cover.file_aes_key:
        attempts.append(AssetAttempt("cover", {"file_no": cover.file_no,
                                               "file_aes_key": cover.file_aes_key}))
    if cover.icon_url and cover.icon_url != cover.url:
        attempts.append(AssetAttempt("icon", {"url": cover.icon_url}))
    return attempts


def sniff_image_ext(buf: bytes) -> str:
    """图片魔数 → 扩展名（端点返回格式未文档化，认不出按 ``jpg``）。"""
    if len(buf) >= 3 and buf[0] == 0xFF and buf[1] == 0xD8 and buf[2] == 0xFF:
        return "jpg"
    if len(buf) >= 8 and buf[0] == 0x89 and buf[1] == 0x50 and buf[2] == 0x4E and buf[3] == 0x47:
        return "png"
    if len(buf) >= 6 and buf[:3] == b"GIF":
        return "gif"
    if len(buf) >= 12 and buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def _vendor_message(resp: Any) -> str:
    """厂商响应里的 ``Message``（排障关键）。

    实测 ``-8`` 时只有 ``Message`` 写明「CDN 返回错误码 -5103017」，只看 ``Code`` 查不出原因。
    ``Code`` / ``CodeValue`` 在顶层，``Message`` 也在顶层（TS 注释写"不在 WppApiResponse 上、
    在 raw 里"，指的是 TS 那个响应包装 —— py2 的 ``client.call`` 直接返原 dict）。
    """
    return _str(_as_record(resp).get("Message"))[:120]


async def enrich_mini_program_asset(client: Any, account_id: str,
                                    cover: MiniProgramCover | None) -> AssetResult:
    """下载封面（→ 降级图标）并传 OSS。**任何失败一律返 ``media_url=""``，永不抛。**

    调用方按非致命处理：不阻塞入库与派发（TS ``handler.ts`` 就是把它包在 try/catch 里的）。
    """
    attempts = plan_asset_attempts(cover)
    if not attempts:
        return AssetResult(error="no cover url/file_no/icon_url")
    # ⚠️ 先查凭据**再**打厂商：没 OSS 就没必要下载（TS 同序 —— 少一次白跑的外呼）。
    if load_oss_config() is None:
        return AssetResult(error="oss credentials missing")

    last_error = ""
    for attempt in attempts:
        try:
            resp = await client.call(DOWNLOAD_COVER_ENDPOINT, attempt.body)
            b64 = _str(_as_record(_as_record(resp).get("Data")).get("Image"))
            if not b64:
                raise RuntimeError(
                    f"missing Image: code={_as_record(resp).get('Code')} "
                    f"value={_str(_as_record(resp).get('CodeValue'))} "
                    f"msg={_vendor_message(resp)}")
            buf = base64.b64decode(b64, validate=False)
            if len(buf) < MIN_ASSET_BYTES:
                raise RuntimeError(f"too small (len={len(buf)})")
            filename = f"{sanitize_filename_part(cover.md5 if cover else '')}.{sniff_image_ext(buf)}"
            url = await oss_upload_buffer(buf, filename, OSS_PREFIX,
                                          account_id=account_id, kind=OSS_KIND)
            log.debug("[WPP MINIPROGRAM-ASSET] ok kind=%s: %s (%d 字节)",
                      attempt.kind, url, len(buf))
            return AssetResult(media_url=url, media_size=len(buf), kind=attempt.kind)
        except Exception as exc:  # noqa: BLE001 —— 富化失败绝不能冒泡成"消息处理失败"
            last_error = f"{attempt.kind}: {exc}"
            log.warning("[WPP MINIPROGRAM-ASSET] %s 失败（非致命）: %s", attempt.kind, exc)
    return AssetResult(error=last_error)


__all__ = [
    "AssetAttempt",
    "AssetResult",
    "CATEGORY_MINI_PROGRAM",
    "DOWNLOAD_COVER_ENDPOINT",
    "MIN_ASSET_BYTES",
    "MiniProgramCard",
    "MiniProgramCover",
    "OSS_KIND",
    "OSS_PREFIX",
    "THUMB_TOKEN_PREFIX",
    "enrich_mini_program_asset",
    "format_mini_program_card",
    "is_mini_program_card",
    "mini_program_block",
    "parse_mini_program_card",
    "plan_asset_attempts",
    "sniff_image_ext",
]
