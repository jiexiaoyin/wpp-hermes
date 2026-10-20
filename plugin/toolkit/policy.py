"""工具策略层 —— **注册期**的收口处（撤注册 / 纠错 / 补注册）。

## 为什么单独成模块，而不是改生成物

``vendor_defs.py`` / ``vendor_defs_extra.py`` 是 ``tools/gen_vendor_defs.py`` 从**冻结蓝本**
机械生成的，带「**数据保真**」契约 —— ``gen_vendor_defs.py --check`` 会与源表逐字节比对，
``deploy.sh`` 把它当回归门。所以那里一个字都不能改（改了 ``--check`` 就红）。

但 py2 **对标的是 TS 生产插件的裁决**，而蓝本是更早的旧树，两者在少数工具上**故意不同**：
TS 把某些端点**主动封闭**（AI 不该有原始 XML 能力），又在别处**纠正过端点绑定**。
这些差异必须落在**注册期**，于是有了本模块 —— 它是「冻结蓝本 → 实际注册」之间唯一的一层。

## 三张表

- :data:`DENY_TOOLS` —— **撤注册**。工具从 ``ctx`` 里彻底消失，AI 根本看不到它。
  对齐 TS 的两条明确裁决（见各条注释里的出处）。
- :data:`PATCH_TOOLS` —— **纠错**。覆盖生成表里的 ``endpoint`` / ``params``。
- :data:`ADDED_GROUP` —— **补注册**。TS 有、蓝本没有的工具。

## 查证方式

每一条都写了 TS 侧的 ``file:line``。**不许凭印象加**：这里是安全面，
一条错判要么放开一个危险能力，要么让一个正常工具静默消失。

## 运行期门禁（W7-5，2026-10-02）

上面三张表都作用在**注册期**（工具在不在 AI 的工具表里）。但有一类开关必须在
**调用期**判：``/xiaowei on|off`` 要**当次**改变行为，而注册只发生一次 —— 若把它做成
注册期筛选，"开着却调不动"与"关了却还能调"会随重启时机随机出现。

所以这一节是「按账号开关屏蔽**一组**工具」的运行期门禁：工具照常注册（AI 看得见、
描述里写着默认关闭），每次调用先过门禁；开关关着就**不执行**，只回一句说明。

**唯一的执行点**是 :func:`plugin.toolkit.hermes_contract.wrap_ctx` —— 它已经是
"所有工具、所有注册路径"都必经的那一层（W0 为调用契约收的口）。把门禁挂在那里，
将来新增的工具**忘不掉**：门禁按**工具名**匹配，新名字落在某个受管前缀里就会被
``tests/platform/test_tool_gate_wiring.py`` 的名单判据当场抓出来。

**fail-closed**：读不到账号配置（没有适配器 / 账号不存在 / ``raw`` 不是 dict）时一律
**按关闭处理**，并在回话里说明原因是"读不出来"而不是"没开" —— 两者混为一谈就等于
把"我读不到"说成"你没开"。⚠️ 这一条与 ``repo_*`` 那批"失败只 warn"刚好相反，
与 W6 的写回失败必须出声同向：**门禁读不到东西时不能默认放行**。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

log = logging.getLogger(__name__)

#: 工具面没拿到 ``account_id`` 时用的账号 id。
#: 与 ``registry.register_generic_tools`` / ``tools/msg.py`` 里的字面量同源
#: （工具层的既有口径；``platform/adapter.py`` 的 ``DEFAULT_ACCOUNT`` 是同一个值，
#: 但 tools 层引入 platform 会绕一圈依赖，故在此单列并保持同值）。
DEFAULT_TOOL_ACCOUNT = "default"

#: **撤注册**的工具名集合。
#:
#: 两条都来自 TS 的**明确裁决**，不是"顺手清理"：
#:
#: * ``wpp_fc_publish_raw`` → ``/FriendCircle/MessagesRaw``（原始 XML 发朋友圈）。
#:   TS ``src/dispatch/agent-tools/friendcircle-meta.ts:158``：
#:   「v1.3.63 P1: publishCircleRaw 已移除 — AI 不该有原始 XML 发布能力
#:   （原绕过 guard 无 callerWxid 白名单）」。
#:   ⚠️ 别与另外 4 个 ``wpp_fc_publish_{images,video,text,ai}`` 混为一谈 ——
#:   那 4 个**有守卫**（``tools/msg.py`` 里四处 ``friendCirclePublishEnabled`` 默认 false
#:   + ``callerWxid`` 白名单），口径与本条不同。
#: * ``wpp_send_app_msg`` → ``/Msg/SendApp``（原始 XML 发应用消息）。
#:   TS ``src/send/msg.ts:605-621``：「② 不注册 agent 工具（与 publishCircleRaw 同则：
#:   AI 不该有原始 XML 发送能力）」。
#:
#: 蓝本当初把这两条都注册成了 AI 可调工具，且**没有任何守卫** —— 这是 py2 安全面
#: 比 TS **更宽**的两处，撤掉才是对齐。
DENY_TOOLS: frozenset[str] = frozenset({
    "wpp_fc_publish_raw",
    "wpp_send_app_msg",
    #: 2026-10-18：``wpp_fc_download_video`` 是蓝本里的**幽灵重复项** —— 它打的是
    #: ``/Tools/DownloadVideo`` 且发旧树的 ``aesKey/fileId``，与 ``wpp_tool_download_video``
    #: 完全重复；TS 朋友圈那条对应的是 ``wpp_fc_cdn_download_video``（``/FriendCircle/DownloadVideo``
    #: ``{key,url}``）。留着只会让 AI 在两条名字里随机挑一条、且旧键恒被厂商忽略。
    #: TS 工具表里没有第三个下载视频入口（``tools-meta.ts`` 的 ``downloadVideo`` +
    #: ``friendcircle-meta.ts`` 的 ``downloadCircleVideo``）⇒ 撤掉才是对齐。
    "wpp_fc_download_video",
})

# ------------------------------------------------------- 请求体整形（PATCH_TOOLS 用）


def _two_decimal_yuan(value: Any) -> Any:
    """金额 → 线上要求的「单位元、两位小数字符串」（TS ``yuan()`` 同款，如 ``"10.00"``）。

    厂商这几个 model 把金额声明为 **string**，而 Go 反序列化对类型严格
    （TS 实测：给 int64 传 string 直接 ``Code=-8 INVALID_ARGUMENT``）⇒ 旧码发 number 必失败。
    非法输入原样透传（让厂商报错，别在本地吞成 0.00 —— 那会把"填错了"伪装成"收 0 元"）。
    """
    if value is None or isinstance(value, bool):
        return value
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return value


def _tenpay_money(body: dict) -> dict:
    """把 ``Money`` / ``money`` 转成两位小数字符串，并剔除死字段 wxid。"""
    out = _tenpay_drop_wxid(body)
    for key in ("Money", "money"):
        if key in out:
            out[key] = _two_decimal_yuan(out[key])
    return out


def _tenpay_drop_wxid(body: dict) -> dict:
    """剔除线上 model 里**不存在**的 wxid / Wxid（厂商静默忽略的"死负载"）。"""
    return {k: v for k, v in body.items() if k not in ("wxid", "Wxid")}


def _tenpay_open_hongbao(body: dict) -> dict:
    """``/TenPay/OpenHongBao`` 二选一：给 ``contextId`` 就只发它，否则发兼容模式。

    照抄 TS ``openHongBao``（v1.16.1）：混发三字段没有依据 —— 厂商 model 是
    ``{SendUserName, Xml, contextId}``，推荐用法只有 contextId 一个字段。
    """
    context_id = body.get("contextId")
    if context_id:
        return {"contextId": context_id}
    return {"SendUserName": body.get("SendUserName") or "", "Xml": body.get("Xml") or ""}


def _tenpay_collect_money(body: dict) -> dict:
    """``/TenPay/Collectmoney`` 二选一：``contextId`` 优先，否则发兼容字段（去掉空值）。"""
    context_id = body.get("contextId")
    if context_id:
        return {"contextId": context_id}
    return {k: body[k] for k in ("toUserName", "transFerId", "transactionId", "invalidTime")
            if body.get(k)}


# ------------------------------------------------- 2026-10-18 TS v1.16.2 契约对齐
#
# 背景：``vendor_defs*.py`` 是从**更早的旧树**机械生成的，而 TS 在 v1.6.0
# (SWAGGER-323) / v1.6.5 / v1.16.1 连续纠正过一批端点的**请求体字段** ——
# 旧码发的是 swagger 里不存在的键，Go 静默忽略 ⇒ 工具"成功"返回但什么都没做。
# 逐条出处都写在下面每个函数/条目的注释里（``src/send/*.ts`` + ``*-meta.ts``）。
#
# 约定：**工具层参数名**尽量照 TS meta（``downloadImg`` 的 ``toWxid/msgId/dataLen``），
# 整形函数只负责把它翻成**厂商线上键**（``to_wxid/msg_id/data_len`` + ``section``）。
# 这样模型看到的是 TS 那套语义，而不是厂商的 wire key。


def _tools_download_media(body: dict) -> dict:
    """TS ``tools.ts::downloadImg/downloadVideo`` —— ``{to_wxid,msg_id,data_len,
    compress_type,section}``（``section={start_pos:0,data_len}``）。"""
    data_len = body.get("dataLen", 0)
    return {
        "to_wxid": body.get("toWxid", ""),
        "msg_id": body.get("msgId", 0),
        "data_len": data_len,
        "compress_type": body.get("compressType", 0),
        "section": {"start_pos": 0, "data_len": data_len},
    }


def _tools_download_voice(body: dict) -> dict:
    """TS ``tools.ts::downloadVoice`` —— ``{fromUserName,msgId,length,bufid}``。"""
    return {
        "fromUserName": body.get("fromUserName", ""),
        "msgId": body.get("msgId", 0),
        "length": body.get("length", 0),
        "bufid": body.get("bufid", ""),
    }


def _tools_download_file(body: dict) -> dict:
    """TS ``tools.ts::downloadFile`` —— ``{app_id,attach_id,data_len,section,user_name}``。"""
    data_len = body.get("dataLen", 0)
    return {
        "app_id": body.get("appId", ""),
        "attach_id": body.get("attachId", ""),
        "data_len": data_len,
        "section": {"start_pos": 0, "data_len": data_len},
        "user_name": body.get("userName", ""),
    }


def _tools_download_file_binary(body: dict) -> dict:
    """TS ``tools.ts::downloadFileBinary`` —— 比 ``DownloadFile`` 多一个 ``file_name``。"""
    out = _tools_download_file(body)
    out["file_name"] = body.get("fileName", "")
    return out


def _tools_download_voice_binary(body: dict) -> dict:
    """TS ``tools.ts::downloadVoiceBinary`` —— ``{msg_id,new_msg_id,to_user_name,
    from_user_name,length,format}``（``format`` 默认 4）。"""
    return {
        "msg_id": body.get("msgId", 0),
        "new_msg_id": body.get("newMsgId", ""),
        "to_user_name": body.get("toUserName", ""),
        "from_user_name": body.get("fromUserName", ""),
        "length": body.get("length", 0),
        "format": body.get("format", 4),
    }


def _tools_cdn_download_image(body: dict) -> dict:
    """TS ``tools.ts::cdnDownloadImage`` —— ``{file_aes_key,file_no}``。"""
    return {"file_aes_key": body.get("fileAesKey", ""), "file_no": body.get("fileNo", "")}


def _tools_get_a8key(body: dict) -> dict:
    """TS ``tools.ts::getA8Key`` —— 线上键是 ``reqUrl``（旧码发 ``url`` 完全对不上）。"""
    return {"reqUrl": body.get("url", "")}


def _mini_program_cover(body: dict) -> dict:
    """TS ``tools.ts::downloadMiniProgramCover`` —— ``url`` 或 ``file_no+file_aes_key``。"""
    out: dict = {}
    if body.get("url"):
        out["url"] = body["url"]
    if body.get("fileNo"):
        out["file_no"] = body["fileNo"]
    if body.get("fileAesKey"):
        out["file_aes_key"] = body["fileAesKey"]
    return out


def _user_bind_mobile(body: dict) -> dict:
    """TS ``user.ts::bindingMobile`` —— 线上键 ``verifycode``（旧码发 ``code``）。"""
    return {"mobile": body.get("mobile", ""), "verifycode": body.get("code", "")}


def _user_bind_qq(body: dict) -> dict:
    """TS ``user.ts::bindQQ`` —— 线上键 ``account``（数字），旧码发 ``qq``。"""
    qq = body.get("qq", "")
    try:
        account: Any = int(qq)
    except (TypeError, ValueError):
        account = qq
    return {"account": account, "password": body.get("password", "")}


def _user_change_password(body: dict) -> dict:
    """TS ``user.ts::setPasswd`` —— ``{newPassword,ticket}``（旧码只发 ``newPwd``）。"""
    return {"newPassword": body.get("newPwd", ""), "ticket": body.get("ticket", "")}


def _user_report_motion(body: dict) -> dict:
    """TS ``user.ts::reportMotion`` —— ``{deviceId,deviceType,stepCount}``。"""
    return {
        "deviceId": body.get("deviceId", ""),
        "deviceType": body.get("deviceType", "ipad"),
        "stepCount": body.get("steps", 0),
    }


def _user_upload_head(body: dict) -> dict:
    """TS ``user.ts::uploadHeadImage`` —— ``{base64}``（旧码发 ``imgBase64``）。"""
    return {"base64": body.get("imgBase64", "")}


def _user_set_alias(body: dict) -> dict:
    """TS ``user.ts::setAlisa`` —— 厂商把字段拼成了 ``alisa``，按厂商拼写发。"""
    return {"alisa": body.get("alias", "")}


def _user_set_privacy(body: dict) -> dict:
    """TS ``user.ts::privacySettings`` —— ``{function,value}``（工具侧叫 ``opt``）。"""
    return {"function": body.get("opt"), "value": body.get("value")}


def _user_update_profile(body: dict) -> dict:
    """TS ``user.ts::updateProfile`` —— 六个字段全发，未给的填空（与 TS 默认一致）。"""
    return {
        "nickName": body.get("nickname", ""),
        "signature": body.get("signature", ""),
        "sex": body.get("sex", 0),
        "city": "",
        "country": "",
        "province": "",
    }


def _user_my_profile(body: dict) -> dict:
    """TS ``user.ts::getContractProfile`` —— ``{wxid}``（留空取自己）。"""
    return {"wxid": body.get("wxid", "")}


def _user_add_me_methods(body: dict) -> dict:
    """TS ``user.ts::addMeMethods`` —— 只传调用方给出的布尔项。"""
    return {k: body[k] for k in ("phone", "wechat_id", "group_chat", "qr_code", "contact_card")
            if k in body}


def _wxapp_openid(body: dict) -> dict:
    """TS ``wxapp.ts::getUserOpenId`` —— ``{toWxId,appid}``（旧码只发 ``appId``）。"""
    return {"toWxId": body.get("toWxId", ""), "appid": body.get("appid", "")}


def _wxapp_cloud_call(body: dict) -> dict:
    """TS ``wxapp.ts::cloudCallFunction`` —— 只有 ``{appid,data}``；旧码多发死字段
    ``functionName``（函数名在 data 里）。"""
    return {"appid": body.get("appid", ""), "data": body.get("data", "")}


def _wxapp_operate(body: dict) -> dict:
    """TS ``wxapp.ts::jsOperateWxData`` —— ``{appid,data}`` + 可选 ``opt``。

    工具层参数是 ``appId``（TS meta 的写法），线上键是 ``appid``（厂商 swagger）——
    两者差一个大小写，别在这里再踩一次「键名对不上、厂商静默忽略」。
    """
    out = {"appid": body.get("appId", body.get("appid", "")),
           "data": body.get("data", "")}
    if body.get("opt") is not None:
        out["opt"] = body["opt"]
    return out


def _finder_search(body: dict) -> dict:
    """TS ``finder.ts::search`` —— ``{FinderUsername, Value}``（旧码发 ``keyword``，厂商不认）。"""
    return {"FinderUsername": body.get("finderUsername", ""),
            "Value": body.get("keyword", body.get("value", ""))}


def _finder_follow(body: dict) -> dict:
    """TS ``finder.ts::follow`` —— ``{FinderUsername, OpType:1, Userver:1}``。"""
    return {"FinderUsername": body.get("finderUsername", ""),
            "OpType": int(body.get("opType") or 1),
            "Userver": int(body.get("userver") or 1)}


def _finder_like(body: dict) -> dict:
    """TS ``finder.ts::like`` —— 10 字段，缺省按 vendor 描述补零值。"""
    return {
        "Id": body.get("Id", ""),
        "FinderUsername": body.get("finderUsername", ""),
        "CommentId": body.get("commentId", ""),
        "CurLikeCount": int(body.get("curLikeCount") or 0),
        "LikeId": body.get("likeId", ""),
        "LikeUsername": body.get("likeUsername", ""),
        "ObjectNonceId": body.get("objectNonceId", ""),
        "OpType": int(body.get("opType") or 1),
        "Scene": int(body.get("scene") or 1),
        "SessionBuffer": body.get("sessionBuffer", ""),
    }


def _finder_comment_list(body: dict) -> dict:
    """TS ``finder.ts::getCommentList`` —— 与 GetCommentDetail 共用 param doc。"""
    return {
        "FinderUsername": body.get("finderUsername", ""),
        "Id": int(body.get("Id") or 0),
        "LastBuffer": body.get("lastBuffer", ""),
        "ObjectNonceId": body.get("objectNonceId", ""),
        "RootCommentId": int(body.get("rootCommentId") or 0),
    }


def _label_update_name(body: dict) -> dict:
    """TS ``label.ts::updateName`` —— ``{LabelID, NewName}``（旧码发 ``labelName``）。"""
    return {"LabelID": body.get("labelId", body.get("LabelID", "")),
            "NewName": body.get("labelName", body.get("NewName", ""))}


def _oa_article_list(body: dict) -> dict:
    """TS ``officialaccounts.ts::articleList`` —— ``{account_id?, history_url?, limit, offset}``。

    ⚠️ 线上键就是 snake_case 的 ``account_id``；工具层的 ``account_id`` 是**账号路由**
    （registry 会 pop 掉），所以工具参数另起名 ``mpAccountId``。
    """
    out: dict[str, Any] = {}
    if body.get("mpAccountId"):
        out["account_id"] = body["mpAccountId"]
    if body.get("historyUrl"):
        out["history_url"] = body["historyUrl"]
    out["limit"] = int(body.get("limit") or 20)
    out["offset"] = int(body.get("offset") or 0)
    return out


def _wxapp_pay(body: dict) -> dict:
    """支付六件套（GetUnionPay / GetpullPay / JSGetSessionidQRcode 共用）。"""
    return {k: body.get(k, "") for k in
            ("appid", "sessionid", "timeStamp", "nonceStr", "package", "paySign")}


def _oa_oauth_authorize(body: dict) -> dict:
    """TS ``officialaccounts.ts::oauthAuthorize`` —— ``{url, appid}``，appid 必填。"""
    return {"url": body.get("url", ""), "appid": body.get("appid", "")}


def _webhook_set(body: dict) -> dict:
    """TS ``webhook.ts::set`` —— 缺 ``enabled:true`` 时 Go bool 零值 = false，
    webhook 会"设了但未启用"（v1.1.17 P0-E 事故）。"""
    types = body.get("messageTypes")
    return {
        "url": body.get("url", ""),
        "enabled": True if body.get("enabled") is None else bool(body.get("enabled")),
        "retryCount": int(body.get("retryCount") or 3),
        "timeout": int(body.get("timeout") or 10),
        "messageTypes": types if isinstance(types, list) and types else ["all"],
    }


def _webhook_test(body: dict) -> dict:
    """TS ``webhook.ts::test`` —— 只发 ``{msg}``；旧码发的 MessageType/TestData 不是契约字段。"""
    msg = body.get("msg")
    return {"msg": msg if isinstance(msg, dict) and msg else {"hello": "world"}}


def _fc_push_comment(body: dict) -> dict:
    """TS ``friendcircle.ts::pushComment`` —— ``{id, forwardAddr}`` 都必填。"""
    return {"id": body.get("id", ""), "forwardAddr": body.get("forwardAddr", "")}


def _fc_list(body: dict) -> dict:
    """TS ``friendcircle.ts::getList`` —— ``{fristpagemd5, maxid}``。

    ``fristpagemd5`` 是**厂商的拼写**（少一个 ``i``），照抄不要"纠正"；``maxid``
    是第二页起的游标。旧生成物 ``params: {}`` ⇒ 永远只看得到第一页。
    """
    return {"fristpagemd5": body.get("firstPageMd5", ""),
            "maxid": body.get("maxid", "")}


def _login_get_qr(body: dict) -> dict:
    """TS ``login.ts::loginGetQR`` —— 线上键是小写 ``deviceType``（旧码发 ``DeviceName``）。"""
    return {"deviceType": body.get("deviceType") or "ipad"}


def _login_get_status(body: dict) -> dict:
    """TS ``login.ts::loginGetStatus`` —— GET 查询参数 ``autoLogin``（小写 true/false）。"""
    return {"autoLogin": "true" if body.get("autoLogin", True) else "false"}


def _login_pad_cloud(body: dict) -> dict:
    """TS ``login.ts::loginGetQRPadCloud/PPMT`` —— ``{DeviceName,oversea}``。"""
    return {
        "DeviceName": body.get("deviceName") or "我的 iPad",
        "oversea": bool(body.get("oversea", False)),
    }


def _login_62data(body: dict) -> dict:
    """TS ``login.ts::login62dataQRCodeVerify`` —— 线上键 ``Url``。"""
    return {"Url": body.get("url", "")}


def _send_cdn_media(body: dict, *, key: str) -> dict:
    """TS ``msg.ts::sendCDNImg/sendCDNVideo`` —— ``{ToWxid,Content:<url>}``。"""
    return {"ToWxid": body.get("toWxid", ""), "Content": body.get(key, "")}


def _send_cdn_image(body: dict) -> dict:
    return _send_cdn_media(body, key="imgUrl")


def _send_cdn_video(body: dict) -> dict:
    return _send_cdn_media(body, key="videoUrl")


def _upload_file(body: dict) -> dict:
    """TS ``tools.ts::uploadFile`` —— swagger 只有 ``{base64}``（旧码发 ``fileBase64``）。"""
    return {"base64": body.get("base64", "")}


def _upload_image(body: dict) -> dict:
    """TS ``msg.ts::uploadImg`` —— ``{ToWxid,Base64}``。"""
    return {"ToWxid": body.get("toWxid", ""), "Base64": body.get("imgBase64", "")}


#: **纠错**表：工具名 → 要覆盖的字段。
#:
#: ``wpp_tool_set_step_count`` 的端点绑错了 —— 蓝本把它打在 ``/Tools/setproxy`` 且发
#: ``{steps}``。那正是 TS 从 v1.4.1 起错了三个多月的**权宜**，v1.6.0 已纠正
#: （``src/send/tools.ts:91-109``，原文照录）：
#:
#:   ``/Tools/setproxy`` = **设置/删除代理IP**（必填 ``proxy``，传空串恢复直连）—— 本来就是，
#:   厂商从没换过语义；``/Tools/SetStep`` = 修改微信步数（必填 ``step``）。两个端点一直并存。
#:   ``setproxy`` 只读 ``proxy``，未知字段 ``steps`` 被 Go 静默忽略 ⇒ ``proxy`` 取零值空串
#:   ⇒ **恢复直连**，厂商照常回成功码。所以那次调用**既没改步数、还可能把账号的出口代理清了**。
#:
#: 所以纠正分两步：① 步数回 ``/Tools/SetStep``；② ``setproxy`` 按其真实语义单列成
#: :data:`ADDED_GROUP` 里的 ``wpp_tool_set_proxy``。
#:
#: ⚠️ ``params`` 要**整体替换**、且键名必须是 ``step``（不是 ``steps``）：py2 的通用
#: handler 直接用 schema 的键名拼请求体（``registry.register_generic_tools``：
#: ``body = {k: v for k, v in params.items()}``），写成 ``steps`` 会原样发出去 —— 那就又回到
#: "被 Go 静默忽略"的老路上。
PATCH_TOOLS: Mapping[str, Mapping[str, Any]] = {
    "wpp_tool_set_step_count": {
        "endpoint": "/Tools/SetStep",
        "params": {
            "step": {"type": "integer", "required": True, "desc": "步数（最高 98000）"},
        },
    },
    # ------------------------------------------------ 视频号 / 标签 / 公众号 / 支付
    #
    # 2026-10-18 全量 wire 审阅（TS v1.16.2 逐条比对）：下面这些工具原来发的键名
    # 厂商**根本不认**。Go 对未知字段静默忽略 ⇒ 调用"成功"但什么都没做。逐条出处
    # 见各 transform 的 docstring。
    "wpp_finder_search": {
        "params": {
            "keyword": {"type": "string", "required": True, "desc": "搜索关键词"},
            "finderUsername": {"type": "string", "desc": "搜索来源 username（可空）"},
        },
        "transform": _finder_search,
    },
    "wpp_finder_follow": {
        "params": {
            "finderUsername": {"type": "string", "required": True,
                               "desc": "视频号 username（注意不是 finderId）"},
            "opType": {"type": "integer", "desc": "1=关注（默认）"},
            "userver": {"type": "integer", "desc": "userver（默认 1，尽量传上一响应的值）"},
        },
        "transform": _finder_follow,
    },
    "wpp_finder_like": {
        "params": {
            "Id": {"type": "string", "required": True, "desc": "作品 ID"},
            "finderUsername": {"type": "string", "required": True, "desc": "作者 username"},
            "commentId": {"type": "string", "desc": "评论 ID"},
            "curLikeCount": {"type": "integer", "desc": "当前点赞数"},
            "likeId": {"type": "string", "desc": "点赞 ID"},
            "likeUsername": {"type": "string", "desc": "点赞者 username"},
            "objectNonceId": {"type": "string", "desc": "内容凭据"},
            "opType": {"type": "integer", "desc": "1=点赞（默认）"},
            "scene": {"type": "integer", "desc": "场景（默认 1）"},
            "sessionBuffer": {"type": "string", "desc": "会话 buffer"},
        },
        "transform": _finder_like,
    },
    "wpp_finder_comment_list": {
        "params": {
            "finderUsername": {"type": "string", "required": True, "desc": "作者 username"},
            "Id": {"type": "integer", "required": True, "desc": "作品 ID（int64）"},
            "lastBuffer": {"type": "string", "desc": "翻页游标（首页留空）"},
            "objectNonceId": {"type": "string", "desc": "内容凭据"},
            "rootCommentId": {"type": "integer", "desc": "根评论 ID（全部评论传 0）"},
        },
        "transform": _finder_comment_list,
    },
    "wpp_label_update_name": {
        "params": {
            "labelId": {"type": "string", "required": True, "desc": "标签 ID"},
            "labelName": {"type": "string", "required": True, "desc": "新标签名"},
        },
        "transform": _label_update_name,
    },
    "wpp_oa_article_list": {
        "params": {
            "mpAccountId": {"type": "string", "desc": "公众号 __biz 标识（与 historyUrl 二选一）"},
            "historyUrl": {"type": "string", "desc": "公众号历史页链接（与 mpAccountId 二选一）"},
            "limit": {"type": "integer", "desc": "数量（默认 20）"},
            "offset": {"type": "integer", "desc": "偏移（默认 0）"},
        },
        "transform": _oa_article_list,
    },
    "wpp_oa_oauth_authorize": {
        "params": {
            "url": {"type": "string", "required": True, "desc": "授权 URL"},
            "appid": {"type": "string", "required": True, "desc": "公众号 AppID"},
        },
        "transform": _oa_oauth_authorize,
    },
    "wpp_wxapp_unionpay": {
        "params": {
            "appid": {"type": "string", "required": True, "desc": "小程序 appid"},
            "sessionid": {"type": "string", "required": True, "desc": "sessionid"},
            "timeStamp": {"type": "string", "required": True, "desc": "时间戳"},
            "nonceStr": {"type": "string", "required": True, "desc": "随机串"},
            "package": {"type": "string", "required": True, "desc": "package"},
            "paySign": {"type": "string", "required": True, "desc": "paySign"},
        },
        "transform": _wxapp_pay,
    },
    "wpp_wxapp_pull_pay": {
        "params": {
            "appid": {"type": "string", "required": True, "desc": "小程序 appid"},
            "sessionid": {"type": "string", "required": True, "desc": "sessionid"},
            "timeStamp": {"type": "string", "required": True, "desc": "时间戳"},
            "nonceStr": {"type": "string", "required": True, "desc": "随机串"},
            "package": {"type": "string", "required": True, "desc": "package"},
            "paySign": {"type": "string", "required": True, "desc": "paySign"},
        },
        "transform": _wxapp_pay,
    },
    "wpp_webhook_set": {
        "params": {
            "url": {"type": "string", "required": True, "desc": "回调 URL"},
            "enabled": {"type": "boolean", "desc": "是否启用（默认 true）"},
            "retryCount": {"type": "integer", "desc": "重试次数（默认 3）"},
            "timeout": {"type": "integer", "desc": "超时秒数（默认 10）"},
            "messageTypes": {"type": "array", "items": {"type": "string"},
                             "desc": "消息类型（默认 [\"all\"]）"},
        },
        "transform": _webhook_set,
    },
    "wpp_webhook_test": {
        "params": {
            "msg": {"type": "object", "desc": "测试消息（默认 {hello:world}）"},
        },
        "transform": _webhook_test,
    },
    "wpp_fc_list": {
        "params": {
            "firstPageMd5": {"type": "string",
                             "desc": "首页游标（翻页用上一响应的 firstPageMd5）"},
            "maxid": {"type": "string", "desc": "翻页游标（第二页起传上一响应的 maxid）"},
        },
        "transform": _fc_list,
    },
    "wpp_fc_push_comment": {
        "params": {
            "id": {"type": "string", "required": True, "desc": "朋友圈 ID"},
            "forwardAddr": {"type": "string", "required": True, "desc": "评论回调地址"},
        },
        "transform": _fc_push_comment,
    },
    # ------------------------------------------------------------ 微信支付/红包
    #
    # 2026-10-17 对齐 TS v1.16.1（以及它顺带纠正的 v1.6.0 SWAGGER-323）：
    # 生成物里的 tenpay 参数表照抄的是**更早的旧树**，其中多条请求体是厂商
    # swagger 里**不存在的字段**；厂商 Go 对未知字段**静默忽略**，所以这些工具
    # 从不报错、也从未生效（"绿灯下的空转"）。逐条出处见下面各条注释。
    #
    # ⚠️ ``params`` 的键名就是厂商请求体的键名（``registry._handler`` 直接按它拼 body），
    #    所以这里必须逐字用 swagger 的大小写（老端点 PascalCase、新端点全小写，厂商自己混用）。
    "wpp_tenpay_ge_ma_skd_qcode": {
        # TS src/send/tenpay.ts:GeMaSkdPayQCode（v1.16.1）：线上
        # TenPay.GeMaSkdPayQCodeParam {Money*, Name*, Remark} 全是 string，**无 Wxid**；
        # Money 必须是「元、两位小数字符串」。Name 必须与已开通经营收款的账户名完全一致。
        "params": {
            "Money": {"type": "number", "required": True,
                      "desc": "金额（元，发送时转两位小数字符串，如 10.00）"},
            "Name": {"type": "string", "required": True,
                     "desc": "经营收款账户名称（必须与微信商户账户名完全一致，不是商品名）"},
            "Remark": {"type": "string", "desc": "备注"},
        },
        "transform": _tenpay_money,
    },
    "wpp_tenpay_sj_skd_qcode": {
        # TS 同上 sjSkdPayQCode：TenPay.SjSkdPayQCodeParam {Money*, Name*, Remark}，无 Wxid。
        "params": {
            "Money": {"type": "number", "required": True,
                      "desc": "金额（元，发送时转两位小数字符串，如 10.00）"},
            "Name": {"type": "string", "required": True,
                     "desc": "商家账户名称（必须与已绑定微信商户账户的完整名称一致）"},
            "Remark": {"type": "string", "desc": "备注"},
        },
        "transform": _tenpay_money,
    },
    "wpp_tenpay_generate_pay_qcode": {
        # TS v1.16.1：TenPay.GeneratePayQCodeModelDoc {money*, name*} 都是 string，
        # money 单位元、两位小数；**无 wxid**（旧注释引的 model 在线上不存在）。
        "params": {
            "money": {"type": "number", "required": True,
                      "desc": "收款金额（单位元，发送时转两位小数字符串）"},
            "name": {"type": "string", "required": True, "desc": "收款项目名称"},
        },
        "transform": _tenpay_money,
    },
    "wpp_tenpay_open_hongbao": {
        # TS v1.16.1：旧工具收 {url,key} 却把两个参数**丢弃**，底层又发线上不存在的
        # SendId/Wxid ⇒ 从未成功过且不报错。线上 TenPay.HongBaoParamDoc
        # {SendUserName, Xml, contextId}：给 contextId 就只发它（厂商标注"推荐"），
        # 否则发兼容模式 SendUserName+Xml。
        "description": "抢红包. 推荐传 contextId（红包消息中的上下文标识）；否则传 Xml + SendUserName。",
        "params": {
            "contextId": {"type": "string",
                          "desc": "红包上下文标识（vendor 推荐；给了它就忽略 Xml/SendUserName）"},
            "Xml": {"type": "string", "desc": "红包消息内容（XML）；与 contextId 二选一"},
            "SendUserName": {"type": "string", "desc": "红包发送人；与 contextId 二选一"},
        },
        "transform": _tenpay_open_hongbao,
    },
    "wpp_tenpay_collect_money": {
        # TS v1.16.1：旧工具只发 {wxid}，而线上 TenPay.CollectmoneyModelDoc
        # {contextId, invalidTime, toUserName, transFerId, transactionId} **没有 wxid**
        # ⇒ 确认收款是空操作。给 contextId 只发它，否则发兼容字段。
        "description": "确认收款. 推荐传 contextId；否则传 toUserName（付款方微信标识）。",
        "params": {
            "contextId": {"type": "string",
                          "desc": "收款上下文标识（vendor 推荐；给了它就忽略其余字段）"},
            "toUserName": {"type": "string", "desc": "付款方微信标识"},
            "transFerId": {"type": "string", "desc": "转账单号（可选）"},
            "transactionId": {"type": "string", "desc": "交易号（可选）"},
            "invalidTime": {"type": "string", "desc": "失效时间（可选）"},
        },
        "transform": _tenpay_collect_money,
    },
    "wpp_tenpay_confirm_pre_transfer": {
        # TS v1.16.1：线上 TenPay.ConfirmPreTransferDoc 四个字段**全必填**且都是 string；
        # 旧工具只暴露线上不存在的 wxid/transferId，四个必填只能填默认空串 ⇒ 不可能成功。
        "description": ("确认支付（完成 createPreTransfer 的预订单）."
                        " bankSerial/bankType/payPassword/reqKey 缺一不可，"
                        "全部来自预支付响应与银行卡列表。"),
        "params": {
            "bankSerial": {"type": "string", "required": True,
                           "desc": "银行卡序列号（来自 /Tools/GetBandCardList）"},
            "bankType": {"type": "string", "required": True, "desc": "银行类型，如 CFT"},
            "payPassword": {"type": "string", "required": True, "desc": "支付密码"},
            "reqKey": {"type": "string", "required": True,
                       "desc": "createPreTransfer 响应里的 req_key"},
        },
    },
    "wpp_tenpay_redpacket_list": {
        # TS v1.16.1：线上 TenPay.HongBaoDetailDoc {xml*, offset, size}，**无 wxid**。
        "description": "查看红包领取列表. xml 来自 inbound 红包事件。",
        "params": {
            "xml": {"type": "string", "required": True,
                    "desc": "红包消息中的原始 XML（来自 inbound 红包事件）"},
            "offset": {"type": "integer", "desc": "领取记录分页偏移（默认 0）"},
            "size": {"type": "integer", "desc": "领取记录分页数量（默认 100）"},
        },
        "transform": _tenpay_drop_wxid,
    },
    # ---- 三个红包端点：v1.6.0 SWAGGER-323（TS 已改，生成物仍是旧形状）----
    "wpp_tenpay_open_redpacket": {
        # TS openwxhb：TenPay.OpenwxhbParamDoc
        # {Xml*, SendUserName*, TimingIdentifier*, Encrypt_key, Encrypt_userinfo}；
        # 旧生成物只发 redPacketId —— 该字段在 swagger 里不存在。
        "description": ("拆开已领取的红包. Xml=红包消息内容, SendUserName=红包发送人,"
                        " TimingIdentifier=领取结果返回的时序标识。"),
        "params": {
            "Xml": {"type": "string", "required": True, "desc": "红包消息内容"},
            "SendUserName": {"type": "string", "required": True, "desc": "红包发送人"},
            "TimingIdentifier": {"type": "string", "required": True,
                                 "desc": "领取结果返回的时序标识"},
            "Encrypt_key": {"type": "string", "desc": "加密 key（可选）"},
            "Encrypt_userinfo": {"type": "string", "desc": "加密用户信息（可选）"},
        },
    },
    "wpp_tenpay_query_hongbao": {
        # TS qrydetailwxhb：TenPay.QrydetailwxhbParamDoc {Xml*, Encrypt_key, Encrypt_userinfo}
        "description": "查看红包详情. Xml=红包消息内容。",
        "params": {
            "Xml": {"type": "string", "required": True, "desc": "红包消息内容"},
            "Encrypt_key": {"type": "string", "desc": "加密 key（可选）"},
            "Encrypt_userinfo": {"type": "string", "desc": "加密用户信息（可选）"},
        },
    },
    "wpp_tenpay_receive_hongbao": {
        # TS receivewxhb：TenPay.ReceivewxhbParamDoc
        # {Xml*, InWay, Encrypt_key, Encrypt_userinfo}
        "description": "接收红包（无 key 流程，vendor 自动）. Xml=红包消息内容。",
        "params": {
            "Xml": {"type": "string", "required": True, "desc": "红包消息内容"},
            "InWay": {"type": "string", "desc": "领取方式（可选）"},
            "Encrypt_key": {"type": "string", "desc": "加密 key（可选）"},
            "Encrypt_userinfo": {"type": "string", "desc": "加密用户信息（可选）"},
        },
    },
    # ------------------------------------------------------------ Tools 域（v1.6.0）
    #
    # 蓝本这批工具的请求体是**旧树**的 `aesKey/fileId` 形态；TS v1.6.0 (SWAGGER-323)
    # 全部改为 swagger 的 `to_wxid/msg_id/data_len/section` 等键（`src/send/tools.ts`）。
    # 旧键被厂商 Go 静默忽略 ⇒ 下载恒失败/空转。下面逐条按 TS 的 meta 参数名暴露，
    # 由 transform 翻成线上键（`section` 是嵌套对象，schema 只能靠 transform 生成）。
    "wpp_tool_download_img": {
        "description": "下载图片（分片）. toWxid=会话ID, msgId=消息ID, dataLen=图片字节数.",
        "endpoint": "/Tools/DownloadImg",
        "params": {
            "toWxid": {"type": "string", "required": True, "desc": "会话 ID（群 id 或对方 wxid）"},
            "msgId": {"type": "integer", "required": True, "desc": "消息 ID"},
            "dataLen": {"type": "integer", "required": True, "desc": "图片字节数"},
            "compressType": {"type": "integer", "desc": "压缩类型, 默认 0"},
        },
        "transform": _tools_download_media,
    },
    "wpp_tool_download_video": {
        "description": "下载视频（分片）. toWxid=会话ID, msgId=消息ID, dataLen=视频字节数.",
        "endpoint": "/Tools/DownloadVideo",
        "params": {
            "toWxid": {"type": "string", "required": True, "desc": "会话 ID（群 id 或对方 wxid）"},
            "msgId": {"type": "integer", "required": True, "desc": "消息 ID"},
            "dataLen": {"type": "integer", "required": True, "desc": "视频字节数"},
            "compressType": {"type": "integer", "desc": "压缩类型, 默认 0"},
        },
        "transform": _tools_download_media,
    },
    "wpp_tool_download_voice": {
        "description": "下载语音. fromUserName=发送人, msgId=消息ID, length=语音字节数.",
        "endpoint": "/Tools/DownloadVoice",
        "params": {
            "fromUserName": {"type": "string", "required": True, "desc": "发送人 wxid"},
            "msgId": {"type": "integer", "required": True, "desc": "消息 ID"},
            "length": {"type": "integer", "required": True, "desc": "语音字节数"},
            "bufid": {"type": "string", "desc": "bufid（可选）"},
        },
        "transform": _tools_download_voice,
    },
    "wpp_tool_download_file": {
        "description": "下载文件. attachId=文件标识, userName=发送人/群ID, dataLen=文件字节数.",
        "endpoint": "/Tools/DownloadFile",
        "params": {
            "attachId": {"type": "string", "required": True, "desc": "文件标识"},
            "userName": {"type": "string", "required": True, "desc": "发送人 wxid / 群 ID"},
            "dataLen": {"type": "integer", "required": True, "desc": "文件字节数"},
            "appId": {"type": "string", "desc": "appId（可选）"},
        },
        "transform": _tools_download_file,
    },
    "wpp_tool_download_file_binary": {
        "description": "完整下载微信文件（二进制）. attachId/userName/dataLen 来自 file.download_context.",
        "endpoint": "/Tools/DownloadFileBinary",
        "params": {
            "attachId": {"type": "string", "required": True, "desc": "文件标识"},
            "userName": {"type": "string", "required": True, "desc": "发送人 wxid / 群 ID"},
            "dataLen": {"type": "integer", "required": True, "desc": "文件字节数"},
            "fileName": {"type": "string", "desc": "文件名（可选）"},
            "appId": {"type": "string", "desc": "appId（可选）"},
        },
        "transform": _tools_download_file_binary,
    },
    "wpp_tool_download_voice_binary": {
        "description": "下载微信语音原文件（二进制）. msgId/newMsgId 来自语音消息.",
        "endpoint": "/Tools/DownloadVoiceBinary",
        "params": {
            "msgId": {"type": "integer", "required": True, "desc": "消息 ID"},
            "newMsgId": {"type": "string", "required": True, "desc": "新消息 ID"},
            "toUserName": {"type": "string", "desc": "接收人 wxid（可选）"},
            "fromUserName": {"type": "string", "desc": "发送人 wxid（可选）"},
            "length": {"type": "integer", "desc": "语音字节数（可选）"},
            "format": {"type": "integer", "desc": "格式, 默认 4"},
        },
        "transform": _tools_download_voice_binary,
    },
    "wpp_tool_cdn_download_image": {
        "description": "CDN 下载高清图片. fileAesKey/fileNo 来自 image.cdn_download_contexts.",
        "endpoint": "/Tools/CdnDownloadImage",
        "params": {
            "fileAesKey": {"type": "string", "required": True, "desc": "图片访问凭据"},
            "fileNo": {"type": "string", "required": True, "desc": "图片文件标识"},
        },
        "transform": _tools_cdn_download_image,
    },
    "wpp_tool_get_a8key": {
        # v1.6.0：线上键是 `reqUrl`（非大小写差异），旧码发 `url` 完全对不上。
        "description": "公众号 A8 Key（打开文章用）.",
        "endpoint": "/Tools/GetA8Key",
        "params": {"url": {"type": "string", "required": True, "desc": "目标 URL"}},
        "transform": _tools_get_a8key,
    },
    "wpp_tool_upload_file": {
        # v1.6.0：swagger Tools.UploadParamDoc 只有 `{base64}`；旧码多发 `fileType` 且
        # 把内容放在 `fileBase64`（厂商读不到）。
        "description": "上传文件（base64 内容）.",
        "endpoint": "/Tools/UploadFile",
        "params": {"base64": {"type": "string", "required": True, "desc": "文件 base64"}},
        "transform": _upload_file,
    },
    # ------------------------------------------------------------ User 域
    "wpp_user_bind_mobile": {
        "description": "绑定手机号. code=短信验证码.",
        "endpoint": "/User/BindingMobile",
        "params": {
            "mobile": {"type": "string", "required": True, "desc": "手机号"},
            "code": {"type": "string", "required": True, "desc": "短信验证码"},
        },
        "transform": _user_bind_mobile,
    },
    "wpp_user_bind_qq": {
        "description": "绑定 QQ 号.",
        "endpoint": "/User/BindQQ",
        "params": {
            "qq": {"type": "string", "required": True, "desc": "QQ 号"},
            "password": {"type": "string", "required": True, "desc": "QQ 密码"},
        },
        "transform": _user_bind_qq,
    },
    "wpp_user_change_password": {
        "description": "修改微信密码. ticket 由风控验证流程返回.",
        "endpoint": "/User/SetPasswd",
        "params": {
            "newPwd": {"type": "string", "required": True, "desc": "新密码"},
            "ticket": {"type": "string", "required": True, "desc": "验证 ticket"},
        },
        "transform": _user_change_password,
    },
    "wpp_user_report_motion": {
        "description": "上报微信运动步数.",
        "endpoint": "/User/ReportMotion",
        "params": {
            "steps": {"type": "integer", "required": True, "desc": "步数"},
            "deviceId": {"type": "string", "desc": "设备 ID（可选）"},
            "deviceType": {"type": "string", "desc": "设备类型, 默认 ipad"},
        },
        "transform": _user_report_motion,
    },
    "wpp_user_upload_headimage": {
        "description": "上传头像图片.",
        "endpoint": "/User/UploadHeadImage",
        "params": {"imgBase64": {"type": "string", "required": True, "desc": "头像图片 base64"}},
        "transform": _user_upload_head,
    },
    "wpp_user_set_alias": {
        "description": "设置微信号（别名）.",
        "endpoint": "/User/SetAlisa",
        "params": {"alias": {"type": "string", "required": True, "desc": "新微信号"}},
        "transform": _user_set_alias,
    },
    "wpp_user_set_privacy": {
        "description": "隐私设置. opt=功能项, value=值（均按厂商文档）.",
        "endpoint": "/User/PrivacySettings",
        # TS user-meta 的 opt/value 都是 Type.Number()，线上键是 function/value；
        # 旧生成物写成 string，模型照抄会发 "4" 而不是 4（Go 的 int 字段直接拒）。
        "params": {
            "opt": {"type": "integer", "required": True, "desc": "功能项（如 4=加好友权限）"},
            "value": {"type": "integer", "required": True, "desc": "设置值"},
        },
        "transform": _user_set_privacy,
    },
    "wpp_user_update_profile": {
        # TS updateProfile 固定发六个键（城市/国家/省份为空串）；旧码只发 NickName/Signature
        # 且键名大写，厂商读不到 nickname/signature。
        "description": "修改个人资料（昵称/签名/性别）.",
        "endpoint": "/User/UpdateProfile",
        "params": {
            "nickname": {"type": "string", "desc": "新昵称"},
            "signature": {"type": "string", "desc": "新签名"},
            "sex": {"type": "integer", "desc": "性别: 0=未知 1=男 2=女"},
        },
        "transform": _user_update_profile,
    },
    "wpp_user_my_profile": {
        "description": "获取联系人资料（wxid 留空=自己）.",
        "endpoint": "/User/GetContractProfile",
        "params": {"wxid": {"type": "string", "desc": "目标 wxid, 留空=自己"}},
        "transform": _user_my_profile,
    },
    "wpp_user_add_me_methods": {
        # TS addMeMethods(opts) 的 opts 是**五个布尔开关**；蓝本把它写成一个字符串
        # `opts`（模型只能瞎猜 JSON），线上五键一个都到不了。
        "description": "设置「添加我的方式」开关（只传要改的项）.",
        "endpoint": "/User/AddMeMethods",
        "params": {
            "phone": {"type": "boolean", "desc": "可通过手机号找到我"},
            "wechat_id": {"type": "boolean", "desc": "可通过微信号找到我"},
            "group_chat": {"type": "boolean", "desc": "可通过群聊添加我"},
            "qr_code": {"type": "boolean", "desc": "可通过二维码添加我"},
            "contact_card": {"type": "boolean", "desc": "可通过名片添加我"},
        },
        "transform": _user_add_me_methods,
    },
    # ------------------------------------------------------------ Wxapp 域
    "wpp_wxapp_openid": {
        # v1.6.5：旧码只发 `appId`，漏了必填的 `toWxId`。
        "description": "查询小程序用户的 openId. toWxId=目标用户 wxid/username.",
        "endpoint": "/Wxapp/GetUserOpenId",
        "params": {
            "toWxId": {"type": "string", "required": True, "desc": "目标用户 wxid"},
            "appid": {"type": "string", "required": True, "desc": "小程序 appid"},
        },
        "transform": _wxapp_openid,
    },
    "wpp_wxapp_cloud_call": {
        # v1.6.5：厂商契约只有 {appid,data}；旧码发 appId+functionName+data，
        # data 还被 meta 丢掉 ⇒ 这条通道 100% 打不通。
        "description": ("小程序云函数调用（云开发）. data=云函数请求 JSON 字符串"
                        "（函数名与参数都在里面）; 厂商契约只有 {appid,data}."),
        "endpoint": "/Wxapp/CloudCallFunction",
        "params": {
            "appid": {"type": "string", "required": True, "desc": "小程序 appid"},
            "data": {"type": "string", "required": True,
                     "desc": "云函数请求 JSON 字符串（含函数名/参数）"},
        },
        "transform": _wxapp_cloud_call,
    },
    "wpp_wxapp_js_operate_data": {
        "description": ("小程序 JSAPI 通用通道. data 传 JSAPI 请求 JSON 字符串, 形如 "
                        '{"api_name":"webapi_getwxaasyncsecinfo","data":{},"opt":1}; '
                        "opt: 1=写入 2=读取."),
        "endpoint": "/Wxapp/JSOperateWxData",
        "params": {
            "appId": {"type": "string", "required": True, "desc": "小程序 appId"},
            "data": {"type": "string", "required": True,
                     "desc": 'JSAPI 请求 JSON 字符串, 形如 {"api_name":"...","data":{},"opt":1}'},
            "opt": {"type": "integer", "desc": "操作类型: 1=写入 2=读取（可选）"},
        },
        "transform": _wxapp_operate,
    },
    "wpp_wxapp_record": {
        # TS getWxAppRecord() 无参数；旧码要求 appId（厂商读不到，恒空转）。
        "description": "获取小程序记录.",
        "endpoint": "/Wxapp/GetWxAppRecord",
        "params": {},
    },
    # ------------------------------------------------------------ Login 域
    "wpp_login_get_qr": {
        # v1.6.0：线上键是小写 `deviceType`；旧码发 `DeviceName` 完全对不上。
        "description": "获取登录二维码. deviceType 默认 ipad.",
        "endpoint": "/Login/GetQR",
        "params": {"deviceType": {"type": "string", "desc": "设备类型, 默认 ipad"}},
        "transform": _login_get_qr,
    },
    "wpp_login_get_status": {
        "description": "查询登录状态（GET）. autoLogin=true 时返回自动登录信息.",
        "endpoint": "/Login/GetLoginStatus", "method": "GET",
        "params": {"autoLogin": {"type": "boolean", "desc": "是否自动登录, 默认 true"}},
        "transform": _login_get_status,
    },
    "wpp_login_qr_pad_cloud": {
        "description": "获取 iPad 云登录二维码. deviceName 默认「我的 iPad」.",
        "endpoint": "/Login/GetQRPadCloud",
        "params": {
            "deviceName": {"type": "string", "desc": "设备名, 默认「我的 iPad」"},
            "oversea": {"type": "boolean", "desc": "是否海外"},
        },
        "transform": _login_pad_cloud,
    },
    "wpp_login_qr_pad_ppmt": {
        "description": "获取 iPad PPMT 登录二维码. deviceName 默认「我的 iPad」.",
        "endpoint": "/Login/GetQRPadPPMT",
        "params": {
            "deviceName": {"type": "string", "desc": "设备名, 默认「我的 iPad」"},
            "oversea": {"type": "boolean", "desc": "是否海外"},
        },
        "transform": _login_pad_cloud,
    },
    # ------------------------------------------------------------ Friend 域
    "wpp_friend_send_request": {
        # TS sendRequest：sourceContext 是推荐的一次性票据；旧路径 opcode+scene+v1+v2
        # 必须同时提供。蓝本把 v1/v2 写成**必填**且没有另外四个键 —— 新路径根本走不了。
        "description": ("添加联系人（发好友请求）. 新路径: 直接传 /Friend/Search 返回的 "
                        "sourceContext 即可; 旧路径: opcode+scene+v1+v2 必须同时提供. "
                        "verifyContent 是验证说明. ⚠️ sourceContext 一次性使用, "
                        "过期或结果不明时重新搜索, 不可重放旧票据."),
        "endpoint": "/Friend/SendRequest",
        "params": {
            "sourceContext": {"type": "string",
                              "desc": "推荐: /Friend/Search 返回的 source_context（一次性）"},
            "opcode": {"type": "integer", "desc": "操作类型（旧路径, 需与 scene/v1/v2 同时提供）"},
            "scene": {"type": "integer", "desc": "好友来源场景（旧路径）"},
            "v1": {"type": "string", "desc": "兼容字段: Search 返回的 v1（旧路径）"},
            "v2": {"type": "string", "desc": "兼容字段: Search 返回的 v2（旧路径）"},
            "verifyContent": {"type": "string",
                              "desc": "验证说明, 如「你好，我是通过名片添加的」"},
        },
    },
}

#: **补注册**的组：``(组名, 工具定义元组)``，形状与 ``vendor_defs*.GROUPS`` 的元素一致，
#: 由 ``registration._register_tools`` 在通用表循环之后追加注册。
#:
#: ``wpp_tool_set_proxy`` —— TS 有 ``setProxy``（``src/send/tools.ts:108``：
#: ``setProxy: (proxy: string) => dispatch("/Tools/setproxy", { proxy })``），蓝本没有
#: （蓝本那个名字近似 ``wpp_tool_set_step_count`` 的工具其实打在这个端点上，见 :data:`PATCH_TOOLS`）。
#: swagger: ``Tools.SetProxyParamDoc {proxy*}`` —— 传空字符串恢复直连，格式 ``host:port``。
def _fc_privacy_scope(body: dict) -> dict:
    """``/FriendCircle/PrivacySettings``：工具面只暴露 ``scope``，线上模型是
    ``{function, value}``（TS ``privacySettings(String(scope), 0)`` 同款）。"""
    return {"function": str(body.get("scope", "")), "value": 0}


ADDED_GROUP: tuple[str, tuple[dict, ...]] = (
    "policy-tools",
    (
        {"name": "wpp_tool_set_proxy",
         "description": "设置/删除代理IP（传空字符串恢复直连，格式 host:port）。",
         "endpoint": "/Tools/setproxy",
         "params": {"proxy": {"type": "string", "required": True,
                              "desc": "代理 host:port；传空串恢复直连"}}},
        # ------------------------------------------------ v1.6.0 / v1.3.67 端点补齐
        # 2026-10-18：TS 工具面有、py2 蓝本没有的端点。全部按 TS 的 send 实现逐字
        # 对齐请求方法/路径/请求体键名（GET 的 query 由 ``WppClient.call`` 统一拼）。
        {"name": "wpp_finder_play_video_status",
         "description": "查询视频号播放任务状态. task_id=任务 ID.",
         "endpoint": "/Finder/PlayVideoStatus", "method": "GET",
         "params": {"task_id": {"type": "string", "required": True, "desc": "任务 ID"}}},
        {"name": "wpp_finder_play_video_tasks",
         "description": "列出视频号播放任务（运行中 + 24h 内已结束）.",
         "endpoint": "/Finder/PlayVideoTasks", "method": "GET", "params": {}},
        {"name": "wpp_friend_set_auto_accept",
         "description": ("配置自动通过好友申请（默认关闭）. enabled=true 才会生效; "
                         "scenes 是允许自动通过的来源场景白名单, 留空=所有场景（不推荐）."),
         "endpoint": "/Friend/AutoAccept",
         "params": {
             "enabled": {"type": "boolean", "required": True, "desc": "是否开启自动通过"},
             "scenes": {"type": "array", "items": {"type": "integer"},
                        "desc": "允许自动通过的来源场景, 留空=所有场景"},
             "delay_seconds": {"type": "integer", "desc": "每条申请通过前的延迟秒数, 0-300"},
             "process_pending": {"type": "boolean", "desc": "开启后立即处理本地已有 pending 申请"},
         }},
        {"name": "wpp_fc_set_privacy",
         "description": "朋友圈隐私设置. scope 查 vendor 文档.",
         "endpoint": "/FriendCircle/PrivacySettings",
         "params": {"scope": {"type": "integer", "required": True, "desc": "隐私范围"}},
         "transform": _fc_privacy_scope},
        {"name": "wpp_fc_export",
         "description": ("建朋友圈批量导出任务（导出某人的朋友圈为 zip/md）. 返回 task_id, "
                         "之后用 wpp_fc_export_status 轮询, 完成后用 wpp_fc_export_download 取文件."),
         "endpoint": "/FriendCircle/BatchDownload",
         "params": {
             "towxid": {"type": "string", "required": True, "desc": "目标用户 wxid"},
             "since": {"type": "string", "desc": "起始日期 YYYY-MM-DD, 默认近六个月"},
             "until": {"type": "string", "desc": "结束日期 YYYY-MM-DD, 默认今天"},
             "max_pages": {"type": "integer", "desc": "最大分页数, 默认/上限 500"},
             "include_original": {"type": "boolean", "desc": "是否下载原图, 默认 true"},
         }},
        {"name": "wpp_fc_export_status",
         "description": "查询朋友圈批量导出任务进度.",
         "endpoint": "/FriendCircle/BatchDownloadStatus", "method": "GET",
         "params": {"task_id": {"type": "string", "required": True,
                                "desc": "wpp_fc_export 返回的 task_id"}}},
        {"name": "wpp_fc_auto_forward_status",
         "description": "查询朋友圈自动跟发策略（开关/来源账号）与最近同步状态.",
         "endpoint": "/FriendCircle/AutoForwardStatus", "method": "GET", "params": {}},
        {"name": "wpp_label_update_order",
         "description": ("更新通讯录标签的显示顺序. 数组顺序=目标显示顺序, 未列出的标签顺序不变. "
                         "标签 ID 用 wpp_label_list 拿."),
         "endpoint": "/Label/UpdateOrder",
         "params": {"LabelIDs": {"type": "array", "items": {"type": "integer"},
                                 "required": True,
                                 "desc": "按目标显示顺序排列的标签 ID 列表"}}},
        {"name": "wpp_other_rank_like",
         "description": "获取微信运动排行的点赞统计（排行、点赞用户、展示信息）. rankId 留空查最新榜单.",
         "endpoint": "/Other/GetUserRankLikeCount",
         "params": {"rankId": {"type": "string", "desc": "排行榜 ID, 留空=最新榜单"}}},
        {"name": "wpp_tenpay_create_pre_transfer",
         "description": ("创建转账预订单（只创建不扣款）. fee 单位是分（100=1.00元）. "
                         "成功后用返回的 req_key 调 wpp_tenpay_confirm_pre_transfer 完成支付."),
         "endpoint": "/TenPay/CreatePreTransfer",
         "params": {
             "toUserName": {"type": "string", "required": True, "desc": "转账接收人微信标识"},
             "fee": {"type": "integer", "required": True, "desc": "转账金额, 单位分"},
             "description": {"type": "string", "desc": "转账备注"},
         }},
        {"name": "wpp_tenpay_pay_qcode_status",
         "description": ("查询收款二维码的支付状态（轮询是否真到账）. 只有 status=paid 且 paid=true "
                         "才算已付款; 503/PAYMENT_STATUS_UNAVAILABLE 时必须稍后重试, 不得判为失败."),
         "endpoint": "/TenPay/PayQCodeStatus",
         "params": {
             "order_id": {"type": "string",
                          "desc": "wpp_tenpay_generate_pay_qcode 响应里的 order_id（推荐）"},
             "pay_url": {"type": "string", "desc": "兼容旧调用: 收款二维码 pay_url"},
         }},
        {"name": "wpp_tenpay_create_redpacket",
         "description": "创建红包（微信红包）. 接收人是群 ID 或单人 wxid.",
         "endpoint": "/TenPay/WXCreateRedPacketApi",
         "params": {
             "amount": {"type": "integer", "required": True, "desc": "红包总金额, 单位分"},
             "content": {"type": "string", "required": True, "desc": "红包祝福语"},
             "count": {"type": "integer", "required": True, "desc": "红包个数"},
             "username": {"type": "string", "required": True,
                          "desc": "接收人 wxid（单人）或群 ID"},
             "redType": {"type": "integer", "desc": "红包类型: 1=普通, 2=群"},
             "from": {"type": "integer", "desc": "红包来源场景, 0=普通"},
         }},
        {"name": "wpp_webhook_business_get",
         "description": "获取业务回调 URL.",
         "endpoint": "/Webhook/Business/Get", "method": "GET", "params": {}},
        {"name": "wpp_user_can_set_alias",
         "description": "检测当前是否可以设置微信号（GET）.",
         "endpoint": "/User/CheckCanSetAlias", "method": "GET", "params": {}},
        {"name": "wpp_oa_mp_get_a8key",
         "description": "获取公众号文章 key 和 uin.",
         "endpoint": "/OfficialAccounts/MpGetA8Key",
         "params": {"url": {"type": "string", "required": True, "desc": "文章 URL"}}},
        {"name": "wpp_search_capabilities",
         "description": "查看通用搜索支持的分类.",
         "endpoint": "/Search/Capabilities", "method": "GET", "params": {}},
        {"name": "wpp_search_channels_media",
         "description": "获取视频号媒体流. media_token 来自搜索结果, download=1 下载 / 0 预览播放.",
         "endpoint": "/Search/Channels/Media", "method": "GET",
         "params": {
             "media_token": {"type": "string", "required": True, "desc": "媒体 token"},
             "download": {"type": "integer", "desc": "1 下载, 0 预览"},
         }},
        {"name": "wpp_search_ai_conversation",
         "description": "获取 AI 搜索会话（问答轮次/Markdown 答案/参考资料/建议追问）.",
         "endpoint": "/Search/AI/Conversation", "method": "GET",
         "params": {"session_id": {"type": "string", "required": True, "desc": "会话 ID"}}},
        {"name": "wpp_group_list_compat",
         "description": "获取群列表（GET 兼容路由）.",
         "endpoint": "/Group/GroupList", "method": "GET", "params": {}},
        {"name": "wpp_login_62data_qr_verify",
         "description": "62 数据二维码验证会话. Url=验证链接.",
         "endpoint": "/Login/62dataQRCodeVerify",
         "params": {"Url": {"type": "string", "required": True, "desc": "验证链接"}}},
        # ------------------------------------------------------------ 小微（缺口 9/10）
        # 第 10 个 ``wpp_xw_events`` 是 SSE 长连接，无法走通用 handler，见
        # ``toolkit/parity.py``。路径参数由 ``WppClient.call`` 的 ``{name}`` 绑定。
        {"name": "wpp_xw_get_session",
         "description": "获取小微会话状态（当前消息/房间/最后事件序号）.",
         "endpoint": "/XiaoWei/Chat/Sessions/{session_id}", "method": "GET",
         "params": {"session_id": {"type": "string", "required": True, "desc": "会话 ID"}}},
        {"name": "wpp_xw_send_message",
         "description": "向小微会话发送消息（text 必填; 回答经 Events 事件流返回）.",
         "endpoint": "/XiaoWei/Chat/Sessions/{session_id}/Messages",
         "params": {
             "session_id": {"type": "string", "required": True, "desc": "会话 ID"},
             "text": {"type": "string", "required": True, "desc": "消息文本"},
             "context": {"type": "array", "items": {"type": "object"},
                         "desc": "文章/链接/图片引用"},
         }},
        {"name": "wpp_xw_cancel",
         "description": "取消小微当前回答（事件流返回 cancelled, 会话恢复 ready）.",
         "endpoint": "/XiaoWei/Chat/Sessions/{session_id}/Cancel",
         "params": {"session_id": {"type": "string", "required": True, "desc": "会话 ID"}}},
        {"name": "wpp_xw_regenerate",
         "description": "重新生成小微回答. message_id=要重生成的回答消息 ID.",
         "endpoint": "/XiaoWei/Chat/Sessions/{session_id}/Regenerate",
         "params": {
             "session_id": {"type": "string", "required": True, "desc": "会话 ID"},
             "message_id": {"type": "string", "required": True, "desc": "回答消息 ID"},
         }},
        {"name": "wpp_xw_switch_room",
         "description": "切换小微会话房间（会话 ready 时）.",
         "endpoint": "/XiaoWei/Chat/Sessions/{session_id}/SwitchRoom",
         "params": {
             "session_id": {"type": "string", "required": True, "desc": "会话 ID"},
             "room_id": {"type": "string", "required": True, "desc": "目标房间 ID"},
         }},
        {"name": "wpp_xw_invite_candidates",
         "description": "可邀请使用小微的好友列表.",
         "endpoint": "/XiaoWei/Invites/Candidates", "method": "GET", "params": {}},
        {"name": "wpp_xw_invite_info",
         "description": "查询小微邀请额度.",
         "endpoint": "/XiaoWei/Invites/Info", "method": "GET", "params": {}},
        {"name": "wpp_xw_permission",
         "description": "查询账号是否已开通小微（GET）.",
         "endpoint": "/XiaoWei/Permission", "method": "GET", "params": {}},
        {"name": "wpp_xw_a2a_list",
         "description": "多智能体对话列表.",
         "endpoint": "/XiaoWei/Conversations/A2A/List",
         "params": {
             "limit": {"type": "integer", "desc": "条数, 默认 20"},
             "page_context": {"type": "string", "desc": "分页上下文"},
         }},
        # ------------------------------------------------ v1.6.0 / v1.3.67 消息面补齐
        # TS 工具面有、蓝本没有的 5 个端点（`msg-meta.ts` / `tools-meta.ts`）。
        # 请求体键名逐字照 TS 的 send 实现；`items` 是结构化卡片数组（原样透传）。
        {"name": "wpp_tool_download_mini_program_cover",
         "description": ("下载小程序卡片封面图. 传 url, 或 url 为空时传 fileNo+fileAesKey. "
                         "返回 Data.Image (base64 图片)."),
         "endpoint": "/Tools/DownloadMiniProgramCover",
         "params": {
             "url": {"type": "string", "desc": "小程序卡片返回的官方封面地址"},
             "fileNo": {"type": "string",
                        "desc": "图片文件标识（url 为空时与 fileAesKey 成对使用）"},
             "fileAesKey": {"type": "string",
                            "desc": "图片访问凭据（url 为空时与 fileNo 成对使用）"},
         },
         "transform": _mini_program_cover},
        {"name": "wpp_send_cdn_image",
         "description": "发送 CDN 图片（转发图片）. imgUrl 来自 cdnDownloadImage 流程或上传回调.",
         "endpoint": "/Msg/SendCDNImg",
         "params": {
             "toWxid": {"type": "string", "required": True, "desc": "目标 wxid / 群 id"},
             "imgUrl": {"type": "string", "required": True, "desc": "CDN 图片地址"},
         },
         "transform": _send_cdn_image},
        {"name": "wpp_send_cdn_video",
         "description": "发送 CDN 视频（转发视频）.",
         "endpoint": "/Msg/SendCDNVideo",
         "params": {
             "toWxid": {"type": "string", "required": True, "desc": "目标 wxid / 群 id"},
             "videoUrl": {"type": "string", "required": True, "desc": "CDN 视频地址"},
             # TS 的 thumbUrl 历史上就没被发送（`sendCDNVideo(toWxid, videoUrl)`），
             # 但 schema 保留了它以免调用方报错 —— 这里同样保留并原样透传。
             "thumbUrl": {"type": "string", "desc": "缩略图 URL（兼容字段，厂商契约不含）"},
         },
         "transform": _send_cdn_video},
        {"name": "wpp_send_app_message",
         "description": ("发送结构化应用卡片（链接/小程序/音乐/文件）. items 数组, "
                         "单次最多 20 项."),
         "endpoint": "/Msg/SendAppMessage",
         "params": {"items": {"type": "array", "items": {"type": "object"},
                              "required": True,
                              "desc": "卡片项数组, 形如 [{kind:'link'|'mini_program'|'music'|'file', ...}]"}}},
        {"name": "wpp_upload_image",
         "description": "上传图片拿 imgUrl. 上传后用 sendCDNImage 转发.",
         "endpoint": "/Msg/UploadImg",
         "params": {
             "imgBase64": {"type": "string", "required": True, "desc": "图片 base64"},
             "toWxid": {"type": "string", "required": True, "desc": "目标 wxid / 群 id"},
         },
         "transform": _upload_image},
    ),
)


def apply_policy(defs: Sequence[Mapping[str, Any]]) -> list[dict]:
    """把 :data:`DENY_TOOLS` 与 :data:`PATCH_TOOLS` 作用到**一组**工具定义上。

    返回**新列表**（不就地改入参）—— 生成物被多个调用方共享（探针、测试、注册），
    就地改会让"谁先跑谁改掉"的隐式顺序依赖钻进来。

    未命中的条目**原样透传**（同一个对象引用），不复制：复制会让 219 条定义白白过一遍内存，
    且会掩盖"生成物对象身份"这类断言。
    """
    out: list[dict] = []
    for td in defs:
        name = td.get("name")
        if name in DENY_TOOLS:
            continue
        patch = PATCH_TOOLS.get(name) if name else None
        out.append({**td, **patch} if patch else td)  # type: ignore[arg-type]
    return out


def iter_groups(groups: Iterable[tuple[str, Sequence[Mapping[str, Any]]]]) -> list[tuple[str, list[dict]]]:
    """对 ``GROUPS`` 整体套一遍 :func:`apply_policy`，保持组名与顺序。

    注册侧用这个即可，不必自己写循环 —— 少一处"忘了套策略"的机会。
    """
    return [(group, apply_policy(defs)) for group, defs in groups]


# --------------------------------------------------------------- 运行期门禁（W7-5）


@dataclass(frozen=True)
class RuntimeGate:
    """一条"按账号开关屏蔽一组工具"的运行期门禁。

    :param key: 账号 ``raw`` cfg 里的键（**camelCase** —— 与账号文件里写的完全一致，
        写成 snake_case 会永远取不到值、门禁恒关）。
    :param tools: 受管工具的**精确名单**。用精确名而不是"匹配前缀就算"：判据要能
        逐条被测（``tests/platform/test_tool_gate_wiring.py`` 会拿前缀去扫工具表，
        确认"落在这个前缀里的工具一个都没漏"）。
    """

    label: str
    key: str
    message: str
    prefixes: tuple[str, ...]
    tools: tuple[str, ...]
    source: str
    #: 非空 ⇒ 这一组还要做**调用方白名单**：值 = 账号 cfg 里的白名单键名。
    #: 调用方**优先取入站 contextvar 的真实发送者**，模型填的 ``callerWxid`` 只在
    #: 没有绑定（CLI）时才采信；白名单非空而调用方为空 ⇒ 拒绝（fail-closed）。
    caller_allow_key: str = ""

    def claims(self, tool_name: str) -> bool:
        return tool_name in self.tools

    def closed(self, reason: str = "") -> str:
        """关闭时回给工具调用方的话。

        ``reason`` 只在"**读不出来**"时才给（调用方从 :func:`raw_account_cfg` 拿到
        ``None``）。两者必须能分辨：把"我读不到配置"说成"你没开这个开关"是撒谎，
        而这条回话会经模型转述给老板。
        """
        if not reason:
            return self.message
        return f"{self.message}（{reason}，按未启用处理）"


#: 小微智能体（TS ``xiaowei-meta.ts:19-32``）：20 个工具**都**过 ``getXiaoWei()``，
#: 里面 ``!state.config.xiaoweiEnabled`` 就抛「未启用」。2026-10-18 补齐蓝本缺的
#: 10 个端点后，20 个全在名单里（TS 那句回话逐字照抄，见 ``message``）。
XIAOWEI_GATE = RuntimeGate(
    label="小微智能体",
    key="xiaoweiEnabled",
    message="小微智能体未启用 (xiaoweiEnabled=false), 用 /xiaowei on 开启",
    prefixes=("wpp_xw_",),
    tools=(
        "wpp_xw_a2a_list",
        "wpp_xw_cancel",
        "wpp_xw_card_screenshot_check",
        "wpp_xw_card_users",
        "wpp_xw_create_session",
        "wpp_xw_events",
        "wpp_xw_get_session",
        "wpp_xw_history_delete",
        "wpp_xw_history_fill",
        "wpp_xw_history_list",
        "wpp_xw_invite",
        "wpp_xw_invite_candidates",
        "wpp_xw_invite_info",
        "wpp_xw_permission",
        "wpp_xw_reddots",
        "wpp_xw_reddots_read",
        "wpp_xw_regenerate",
        "wpp_xw_send_message",
        "wpp_xw_suggestions",
        "wpp_xw_switch_room",
    ),
    source="src/dispatch/agent-tools/xiaowei-meta.ts:19-32（getXiaoWei 的开关闸）",
)

#: 朋友圈发布（py2 蓝本在 ``tools/msg.py`` 四处手写的 ``friendCirclePublishEnabled``
#: 守卫 ⇒ W7-5 收口到这里）。⚠️ 那四处守卫此前**是坏的**：它们调
#: ``adapter.get_account(aid)``，而适配器上根本没有这个方法（全树只在那 4 行出现过）
#: ⇒ 真机上必然 ``AttributeError`` ⇒ 工具回「调用失败: 'WppAdapter' object has no
#: attribute 'get_account'」，既不是那句"未启用"，白名单那一支也**永远走不到**。
#: 收口到本模块顺带把这个 bug 修掉（读配置改走 :func:`raw_account_cfg`）。
#:
#: ⚠️ TS 侧比 py2 **更严**：v1.3.41 起 4 个发布工具**整体从 agent-tools 移除**
#: （``friendcircle-meta.ts:45-47``「朋友圈发布是对外公开操作, 不能任何人触发 …… 工具层
#: 不暴露发布」）⇒ TS 的 AI 根本调不到发布。py2 仍暴露但带开关 + 白名单。
#: 这是**安全面上的既存差异**，本项只做收口、**不擅自撤注册**（撤掉是减少一个 AI 能力，
#: 属于要项目约定的决定）—— 已上报。
FRIEND_CIRCLE_PUBLISH_GATE = RuntimeGate(
    label="朋友圈发布",
    key="friendCirclePublishEnabled",
    message="朋友圈发布未启用 (friendCirclePublishEnabled=false)",
    prefixes=("wpp_fc_publish_",),
    tools=(
        "wpp_fc_publish_images",
        "wpp_fc_publish_video",
        "wpp_fc_publish_text",
        "wpp_fc_publish_ai",
        # 2026-10-19 审阅补齐：评论/点赞同样是**公开发布**（所有人都看得到），
        # 且生产 friendCirclePublishEnabled=true。此前它不在任何门禁里，
        # 一条 prompt injection 就能让 bot 在好友朋友圈下留任意文字/钓鱼链接。
        "wpp_fc_comment",
    ),
    source="src/dispatch/agent-tools/friendcircle-meta.ts:45-47（TS 侧已整体撤出 AI 工具面）",
    caller_allow_key="friendCirclePublishAllowFrom",
)

#: 账号控制面（**默认关闭**）。2026-10-18 安全审阅 P0：
#: webhook / auto-sync / 登录 / 凭据 / 关系链删除这些端点此前和"发条消息"一样
#: 直接暴露给 AI。群成员一句 prompt injection 就能：
#:
#: * ``wpp_webhook_business_set`` + ``wpp_start_auto_sync`` ⇒ 把该账号**完整消息流**
#:   （含私聊）重定向到攻击者服务器（静默全量窃听）；
#: * ``wpp_webhook_remove`` ⇒ 收信中断；
#: * ``wpp_login_get_qr`` ⇒ 拿到登录二维码，扫码即可接管账号；
#: * ``wpp_login_logout`` / ``wpp_user_change_password`` / ``wpp_friend_delete``
#:   ⇒ 账号掉线、凭据被改、关系链被删。
#:
#: TS v1.3.41 已经把朋友圈发布整体撤出 agent-tools；本组仍保留注册（能力不丢），
#: 但**必须由账号配置显式打开** ``adminToolsEnabled: true`` 才可调用。
#: 默认 false = 这些工具对模型不存在（回话与小微门禁同形，可分辨"没开"与"读不到配置"）。
ADMIN_TOOLS_GATE = RuntimeGate(
    label="账号管理面",
    key="adminToolsEnabled",
    message=("账号管理类工具未启用 (adminToolsEnabled=false). "
             "webhook/登录/凭据/删除好友等操作默认不对 AI 开放"),
    prefixes=("wpp_webhook_", "wpp_login_"),
    tools=(
        # webhook / 入站通道
        "wpp_webhook_set",
        "wpp_webhook_get",
        "wpp_webhook_remove",
        "wpp_webhook_business_set",
        "wpp_webhook_business_get",
        "wpp_webhook_test",
        "wpp_start_auto_sync",
        # ★ 2026-10-18：改出口代理等同"把整条消息流（含 authcode）引到第三方"，
        #   与 start_auto_sync 同级的 P0 场景 —— 模型被 prompt injection 指一句就能改。
        "wpp_tool_set_proxy",
        # 登录 / 会话
        "wpp_login_get_qr",
        "wpp_login_check_qr",
        "wpp_login_heartbeat",
        "wpp_login_heartbeat_long",
        "wpp_login_heartbeat_logs",
        "wpp_login_auto_heartbeat",
        "wpp_login_cache_info",
        "wpp_login_longlink_status",
        "wpp_login_logout",
        "wpp_login_newinit",
        "wpp_login_get_status",
        "wpp_login_check_can_set_alias",
        "wpp_login_submit_verify_code",
        "wpp_login_62data_qr_verify",
        "wpp_login_qr_pad_cloud",
        "wpp_login_qr_pad_ppmt",
        # 凭据 / 绑定
        "wpp_user_change_password",
        "wpp_user_verify_password",
        "wpp_user_bind_mobile",
        "wpp_user_bind_qq",
        "wpp_user_bind_email",
        # 不可撤回的关系链操作
        "wpp_friend_delete",
        "wpp_friend_blacklist",
        "wpp_friend_send_request",
        "wpp_finder_send_dm",
        "wpp_qw_add",
        "wpp_qw_apply",
        "wpp_group_quit",
        "wpp_group_transfer_owner",
        "wpp_group_add_member",
        "wpp_group_invite_member",
        "wpp_oa_quit",
        "wpp_wxapp_delete_oauth",
        # ★ 2026-10-19：群发/群设置/资金/授权/账号资料 —— 与原管理面同一风险等级。
        #   群发尤其危险：一次请求打 N 个群且撤不回（handler 另有 64 个目标硬上限）；
        #   转账确认动钱；JSLogin/AuthMpLogin 是第三方授权入口（钓鱼/接管）。
        "wpp_group_mass_text",
        "wpp_group_create",
        "wpp_group_facing_create",
        "wpp_group_set_name",
        "wpp_group_set_announcement",
        "wpp_group_set_access_verify",
        # 2026-10-19 审阅补齐：踢人 / 设管理员 / 转让群主都是**不可撤销**的群管理写操作。
        # 此前只挡了 wpp_group_quit / wpp_group_transfer_owner，这两条漏网。
        "wpp_group_del_member",
        "wpp_group_operate_admin",
        # 2026-10-19 审阅补齐：资金类此前只挡了「确认转账」，创建红包/创建转账预订单
        # 同样能产生真实资金动作（红包创建即发出，拦不住）。整组一起挡。
        "wpp_tenpay_create_redpacket",
        "wpp_tenpay_create_pre_transfer",
        "wpp_tenpay_confirm_pre_transfer",
        "wpp_wxapp_js_login",
        "wpp_wxapp_js_login_customized",
        "wpp_oa_auth_mp_login",
        "wpp_user_set_privacy",
        "wpp_user_add_me_methods",
        "wpp_user_update_profile",
        "wpp_user_set_alias",
        "wpp_user_upload_headimage",
        "wpp_user_send_verify_code",
    ),
    source="2026-10-18 安全审阅 P0-2（webhook/auto-sync/登录/凭据面默认关闭）；"
           "2026-10-19 补齐群发/群管理/资金/授权/资料类写工具",
)

#: 全部运行期门禁。顺序无意义；**同一工具被两条门禁认领**会在建索引时直接抛
#: （静默取其中一条 = "门禁取决于字典顺序"，是最难查的那种）。
RUNTIME_GATES: tuple[RuntimeGate, ...] = (
    XIAOWEI_GATE, FRIEND_CIRCLE_PUBLISH_GATE, ADMIN_TOOLS_GATE,
)


def _build_gate_index(gates: Iterable[RuntimeGate]) -> dict[str, RuntimeGate]:
    index: dict[str, RuntimeGate] = {}
    for gate in gates:
        for name in gate.tools:
            if name in index:
                raise RuntimeError(
                    f"工具 {name!r} 被两条运行期门禁同时认领："
                    f"{index[name].label} / {gate.label}"
                )
            index[name] = gate
    return index


#: 工具名 → 门禁（只读视图：门禁表是安全面，运行期不许被就地改）。
GATED_TOOLS: Mapping[str, RuntimeGate] = MappingProxyType(_build_gate_index(RUNTIME_GATES))


def gate_for(tool_name: str) -> RuntimeGate | None:
    """这个工具受哪条运行期门禁管；不受管返回 ``None``。"""
    return GATED_TOOLS.get(tool_name)


def raw_account_cfg(adapter: Any, account_id: str = "") -> dict | None:
    """取账号的**原始 cfg**（账号文件里那份，camelCase 键；已并入内存覆盖）。

    ``None`` = **读不出来**（没有适配器 / 账号不存在 / ``raw`` 不是 dict），
    与 "读到了、里面没有这个键"（空 dict 或缺键）是**两件事**：
    门禁必须把前者也判成关闭，同时把原因如实说出来。

    ⚠️ 这就是 W7-5 修掉的那个 bug 的替代品：``tools/msg.py`` 原先调
    ``adapter.get_account(aid)``，而适配器上**没有这个方法**（详见
    :data:`FRIEND_CIRCLE_PUBLISH_GATE` 的注释）。
    """
    if adapter is None:
        return None
    aid = account_id or DEFAULT_TOOL_ACCOUNT
    # ★ ``default`` 是 schema 的**占位哨兵**，不是真账号名：单账号部署里账号文件
    #   叫 ``boss2`` 时，按字面量 ``default`` 去取会被判"账号不存在" ⇒ **所有**
    #   受管工具一律 fail-closed（朋友圈/管理面/心流全哑），而日志只有一句 debug。
    #   只在"请求的就是 default 且恰好只加载了一个账号"时回退（与工具侧
    #   ``registry.resolve_account_id`` 同一条规则；显式填错名字绝不回退）。
    accounts = getattr(adapter, "_accounts", None) or {}
    if str(aid) == DEFAULT_TOOL_ACCOUNT and aid not in accounts and len(accounts) == 1:
        aid = next(iter(accounts))
    try:
        acct = adapter.account(aid)
    except Exception as exc:  # noqa: BLE001 —— 账号不存在/适配器未就绪都走"读不出来"
        log.debug("[WPP] 门禁读账号配置失败 account=%s: %s", aid, exc)
        return None
    raw = getattr(acct, "raw", None)
    return dict(raw) if isinstance(raw, Mapping) else None


def make_tool_gate(adapter_getter: Callable[[], Any]) -> Callable[[str], Callable[[Any], str | None] | None]:
    """造一个**注册期问一次、调用期问每次**的门禁工厂（给 ``wrap_ctx`` 用）。

    形状刻意是 ``factory(tool_name) -> pre 或 None``：

    * 注册期：``None`` ⇒ 这个工具不是受管工具，调用链**一个包装都不加**（299 个工具里
      只有 14 个会多一层）—— 少一层间接就少一处与 W0 契约判据打架的地方；
    * 调用期：``pre(args)`` 返回一句回话（拒）或 ``None``（放行）。

    ``adapter_getter`` 是**零参可调用**（与注册期给工具的那一个同源），且每次调用都
    重新问它 —— 账号配置重载（``/xiaowei on``）之后，下一个工具调用读到的就是新值。
    """

    def factory(tool_name: str) -> Callable[[Any], str | None] | None:
        gate = gate_for(tool_name)
        if gate is None:
            return None

        def pre(args: Any) -> str | None:
            account_id = ""
            if isinstance(args, Mapping):
                account_id = str(args.get("account_id") or "")
            # 入站绑定优先补齐 account_id（CLI 无绑定时保持原行为）
            try:
                from ..core import inbound_context

                if isinstance(args, dict):
                    reject = inbound_context.apply_account(args)
                    if reject:
                        return reject
                    account_id = str(args.get("account_id") or account_id)
            except Exception:
                log.debug("[WPP] 门禁读不到入站上下文 tool=%s", tool_name, exc_info=True)
            try:
                adapter = adapter_getter()
            except Exception as exc:  # noqa: BLE001 —— 取不到适配器 = 读不到配置 = 关闭
                log.warning("[WPP] 门禁取不到适配器 tool=%s: %s", tool_name, exc)
                return gate.closed("取不到适配器")
            cfg = raw_account_cfg(adapter, account_id)
            if cfg is None:
                log.warning("[WPP] 门禁读不到账号配置 tool=%s account=%s ⇒ 按关闭处理",
                            tool_name, account_id or DEFAULT_TOOL_ACCOUNT)
                return gate.closed("读不到账号配置")
            if not cfg.get(gate.key):
                log.info("[WPP] 门禁拒绝 tool=%s（%s=false）", tool_name, gate.key)
                return gate.closed()
            # ★ 调用方白名单（朋友圈发布）：真实发送者来自 contextvar，模型填的
            #   callerWxid 只在 CLI（无绑定）时采信；白名单非空而调用方为空 ⇒ 拒绝。
            if gate.caller_allow_key:
                allow = (cfg.get(gate.caller_allow_key)
                         or cfg.get("adminUsers") or [])
                allow = [str(x) for x in allow if str(x).strip()]
                try:
                    from ..core import inbound_context

                    real = inbound_context.sender()
                except Exception:  # noqa: BLE001
                    real = ""
                #   有入站绑定时只用**真实**发送者；没有绑定（CLI/cron）时才采信
                #   ``callerWxid``。两者都没有而白名单非空 ⇒ **拒绝**（fail-closed）：
                #   否则"不传 callerWxid"就是绕过朋友圈白名单的那条路。
                supplied = str(args.get("callerWxid") or "") if isinstance(args, Mapping) else ""
                caller = real or supplied
                if allow and (not caller or caller not in allow):
                    log.warning("[WPP] 门禁拒绝 tool=%s：调用方 %r 不在白名单",
                                tool_name, caller)
                    return (f"无权限：调用方 {caller or '未知'} 不在白名单 "
                            f"{gate.caller_allow_key} 内")
                if real and isinstance(args, dict):
                    # 覆盖模型可填的 callerWxid —— handler 里再判一次也用真值
                    args["callerWxid"] = real
            return None

        return pre

    return factory
