"""WPP 缺失工具补全定义（2026-08-31 wpp-openclaw 完整对照）。

自动比对 OpenClaw agent-tools 258 工具 vs Hermes 已注册，补齐 101 个缺失端点。
handler 由 tools.py 通用注册器生成（调 client.call(endpoint, body)）。
"""
from __future__ import annotations

# ------------------------------------------------------------------ group 域补全
EXTRA_GROUP_TOOLS = [
    {
        "name": "wpp_group_invite_member",
        "description": "邀请群成员 (40 人以上, 走邀请链)",
        "endpoint": "/Group/InviteChatRoomMember",
        "params": {
            "ChatRoomName": {"type": "string", "required": True, "desc": "群 @chatroom 标识"},
            "ToWxids": {"type": "string", "required": True, "desc": "成员 wxid，多个逗号分隔"},
        },
    },
    {
        "name": "wpp_group_pat",
        "description": "群拍一拍",
        "endpoint": "/Group/SendPat",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群 @chatroom 标识"},
            "ToUserName": {"type": "string", "required": True, "desc": "被拍成员 wxid"},
            "Scene": {"type": "integer", "desc": "场景"},
        },
    },
    {
        "name": "wpp_group_scan_into",
        "description": "扫码进群 (url 是 group qr url)",
        "endpoint": "/Group/ScanIntoGroup",
        "params": {"Url": {"type": "string", "required": True, "desc": "群二维码 URL"}},
    },
    {
        "name": "wpp_group_facing_create",
        "description": "创建面对面群 (基于经纬度). latitude/longitude 是数字坐标",
        "endpoint": "/Group/FacingCreateChatRoom",
        "params": {
            "Latitude": {"type": "number", "required": True, "desc": "纬度"},
            "Longitude": {"type": "number", "required": True, "desc": "经度"},
            "OpCode": {"type": "string", "required": True, "desc": "操作码"},
            "Password": {"type": "string", "desc": "密码"},
        },
    },
    {
        "name": "wpp_group_scan_enterprise",
        "description": "扫码进群 (企业). url 是群二维码",
        "endpoint": "/Group/ScanIntoGroupEnterprise",
        "params": {"Url": {"type": "string", "required": True, "desc": "二维码 URL"}},
    },
    {
        "name": "wpp_group_consent_join",
        "description": "同意进入群聊邀请",
        "endpoint": "/Group/ConsentToJoin",
        "params": {"Url": {"type": "string", "required": True, "desc": "入群申请 URL"}},
    },
    {
        "name": "wpp_group_move_contract",
        "description": "群保存到通讯录. Val 1=保存 0=取消",
        "endpoint": "/Group/MoveContractList",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群 @chatroom 标识"},
            "Val": {"type": "string", "required": True, "desc": "分组值"},
        },
    },
    {
        "name": "wpp_group_set_access_verify",
        "description": "群聊邀请开关 (true=需要验证, false=直接进)",
        "endpoint": "/Group/SetChatroomAccessVerify",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群 @chatroom 标识"},
            "Enable": {"type": "boolean", "required": True, "desc": "是否启用（1=开 0=关）"},
        },
    },
]

# ------------------------------------------------------------------ friend 域补全
EXTRA_FRIEND_TOOLS = [
    {
        "name": "wpp_friend_state",
        "description": "查询好友状态 (在线/性别/地区)",
        "endpoint": "/Friend/GetFriendstate",
        "params": {
            "toWxid": {"type": "string", "required": True, "desc": "联系人 wxid"},
            "opCode": {"type": "integer", "desc": "查询类型：1=好友状态 2=陌生人状态，默认 1"},
        },
    },
    {
        "name": "wpp_friend_lbs",
        "description": "附近的人",
        "endpoint": "/Friend/LbsFind",
        "params": {
            "latitude": {"type": "number", "required": True, "desc": "纬度"},
            "longitude": {"type": "number", "required": True, "desc": "经度"},
            "opCode": {"type": "integer", "desc": "操作类型：1=查找附近的人，默认 1"},
        },
    },
]

# ------------------------------------------------------------------ friendcircle 域补全
EXTRA_FRIENDCIRCLE_TOOLS = [
    {
        "name": "wpp_fc_upload_media",
        "description": "上传朋友圈媒体 (发朋友圈时用). base64 是要上传的图片/视频内容, key 是媒体标识",
        "endpoint": "/FriendCircle/Upload",
        "params": {
            "key": {"type": "string", "required": True, "desc": "媒体标识"},
            "base64": {"type": "string", "required": True, "desc": "媒体 base64"},
        },
    },
    {
        "name": "wpp_fc_upload_video",
        "description": "上传朋友圈视频 (发视频朋友圈前置). videoData=视频 base64, thumbData=缩略图 base64",
        "endpoint": "/FriendCircle/UploadVideo",
        "params": {
            "videoData": {"type": "string", "required": True, "desc": "视频 base64"},
            "thumbData": {"type": "string", "required": True, "desc": "缩略图 base64"},
        },
    },
    {
        "name": "wpp_fc_upload_image",
        "description": "上传单张朋友圈图片. imageData=图片 base64",
        "endpoint": "/FriendCircle/UploadImage",
        "params": {"imageData": {"type": "string", "required": True, "desc": "图片 base64"}},
    },
    {
        "name": "wpp_fc_upload_images",
        "description": "批量上传朋友圈图片. imageDataList=图片 base64 数组",
        "endpoint": "/FriendCircle/UploadImages",
        "params": {"imageDataList": {"type": "array", "required": True, "desc": "图片 base64 数组", "items": {"type": "string"}}},
    },
    {
        "name": "wpp_fc_collect_circle",
        "description": "读取收藏的朋友圈动态详情. sourceId=收藏来源标识",
        "endpoint": "/FriendCircle/GetCollectCircle",
        "params": {"sourceId": {"type": "string", "required": True, "desc": "来源 ID"}},
    },
    {
        "name": "wpp_fc_send_fav_item",
        "description": "从收藏项发布朋友圈. favItemId=收藏项ID(数字), sourceId=收藏来源",
        "endpoint": "/FriendCircle/SendFavItemCircle",
        "params": {
            "favItemId": {"type": "string", "required": True, "desc": "收藏项 ID"},
            "sourceId": {"type": "string", "required": True, "desc": "来源 ID"},
            "blackList": {"type": "string", "desc": "黑名单"},
            "locationMode": {"type": "string", "desc": "定位模式"},
        },
    },
    {
        "name": "wpp_fc_send_one_id",
        "description": "通过已有动态 id 再发朋友圈 (支持文字/图片/视频/链接). id=原动态id",
        "endpoint": "/FriendCircle/SendOneIdCircle",
        "params": {
            "id": {"type": "string", "required": True, "desc": "ID"},
            "blackList": {"type": "string", "desc": "黑名单"},
            "locationMode": {"type": "string", "desc": "定位模式"},
        },
    },
    {
        "name": "wpp_fc_set_days",
        "description": "设置朋友圈可见范围. range=three_days/one_month/six_months/all",
        "endpoint": "/FriendCircle/SetFriendCircleDays",
        "params": {"range": {"type": "string", "required": True, "desc": "范围（如 3/30/全部）"}},
    },
    {
        "name": "wpp_fc_active_tasks",
        "description": "查询正在执行的朋友圈评论转发任务",
        "endpoint": "/FriendCircle/ActiveTasks",
        "params": {},
    },
    {
        "name": "wpp_fc_push_comment",
        "description": "启动评论检查后台任务, 转发 callback 形式的评论事件",
        "endpoint": "/FriendCircle/PushCommnet",
        "params": {},
    },
    {
        "name": "wpp_fc_download_video",
        "description": "下载视频",
        "endpoint": "/Tools/DownloadVideo",
        "params": {
            "aesKey": {"type": "string", "required": True, "desc": "AES 密钥"},
            "fileId": {"type": "string", "required": True, "desc": "文件 ID"},
        },
    },
    {
        "name": "wpp_fc_cdn_download_video",
        "description": "朋友圈下载 CDN 视频（key/url 来自朋友圈详情返回）。",
        "endpoint": "/FriendCircle/DownloadVideo",
        "params": {
            "key": {"type": "string", "required": True, "desc": "媒体访问凭据（media_key_from_circle_detail）"},
            "url": {"type": "string", "required": True, "desc": "媒体地址"},
        },
    },
    {
        "name": "wpp_fc_publish_raw",
        "description": "发布朋友圈（带可见范围：黑名单/指定可见/提醒查看；content 为原始发布内容）。",
        "endpoint": "/FriendCircle/MessagesRaw",
        "params": {
            "content": {"type": "string", "required": True, "desc": "发布内容"},
            "private": {"type": "integer", "desc": "0=公开 1=私密，默认 0"},
            "withUserList": {"type": "string", "desc": "指定可见用户 wxid，逗号分隔"},
            "blackList": {"type": "string", "desc": "不可见用户 wxid，逗号分隔"},
            "groupUserList": {"type": "string", "desc": "提醒查看用户 wxid，逗号分隔"},
        },
    },
    {
        "name": "wpp_fc_set_background",
        "description": "设置朋友圈背景图。",
        "endpoint": "/FriendCircle/SetBackgroundImage",
        "params": {
            "url": {"type": "string", "required": True, "desc": "背景大图地址"},
            "thumbUrl": {"type": "string", "desc": "背景缩略图地址（留空用 url）"},
        },
    },
]

# ------------------------------------------------------------------ search 域补全
EXTRA_SEARCH_TOOLS = [
    {"name": "wpp_search_channels", "description": "视频号内容搜索", "endpoint": "/Search/Channels",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "cursor": {"type": "string", "desc": "分页游标"}, "limit": {"type": "integer", "desc": "数量限制"}}},
    {"name": "wpp_search_images", "description": "图片搜索", "endpoint": "/Search/Images",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "cursor": {"type": "string", "desc": "分页游标"}, "limit": {"type": "integer", "desc": "数量限制"}}},
    {"name": "wpp_search_news", "description": "新闻搜索", "endpoint": "/Search/News",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "cursor": {"type": "string", "desc": "分页游标"}, "limit": {"type": "integer", "desc": "数量限制"}}},
    {"name": "wpp_search_baike", "description": "百科搜索", "endpoint": "/Search/Baike",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "cursor": {"type": "string", "desc": "分页游标"}, "limit": {"type": "integer", "desc": "数量限制"}}},
    {"name": "wpp_search_books", "description": "读书搜索", "endpoint": "/Search/Books",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "cursor": {"type": "string", "desc": "分页游标"}, "limit": {"type": "integer", "desc": "数量限制"}}},
    {"name": "wpp_search_emoji", "description": "表情搜索 (可分页)", "endpoint": "/Search/Emoji",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "cursor": {"type": "string", "desc": "分页游标"}, "limit": {"type": "integer", "desc": "数量限制"}}},
    {"name": "wpp_search_ai", "description": "AI 搜索 (深度问答增强)", "endpoint": "/Search/AI",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "model": {"type": "string", "desc": "模型，默认 deepseek"}, "session_id": {"type": "string", "desc": "会话 ID"}, "turn": {"type": "integer", "desc": "轮次，默认 1"}}},
    {"name": "wpp_search_gateway", "description": "兼容旧版搜一搜网页网关", "endpoint": "/Search/Gateway",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}}},
    {"name": "wpp_search_query", "description": "通用分类搜索. category 可选 (空=全部)", "endpoint": "/Search/Query",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}, "category": {"type": "string", "desc": "分类"}, "cursor": {"type": "string", "desc": "分页游标"}}},
    {"name": "wpp_search_channels_detail", "description": "获取视频号内容详情. contentToken 来自视频号搜索结果", "endpoint": "/Search/Channels/Detail",
     "params": {"content_token": {"type": "string", "required": True, "desc": "内容 token（来自视频号搜索结果）"}}},
    {"name": "wpp_search_channels_comments", "description": "获取视频号评论. commentToken 来自搜索结果, cursor 翻页, rootCommentId 看一级评论的回复", "endpoint": "/Search/Channels/Comments",
     "params": {"comment_token": {"type": "string", "required": True, "desc": "评论 token（来自搜索结果）"}, "cursor": {"type": "string", "desc": "分页游标"}, "root_comment_id": {"type": "string", "desc": "根评论 ID（看一级评论回复）"}}},
    {"name": "wpp_search_channels_resolve", "description": "解析视频号分享链接 (weixin.qq.com/sph/... 分享链接), 返回可直接使用的业务字段", "endpoint": "/Search/Channels/ResolveShare",
     "params": {"url": {"type": "string", "required": True, "desc": "分享 URL"}}},
    {"name": "wpp_search_ai_followup", "description": "AI 搜索追问 (用首问返回的 sessionId 继续对话)", "endpoint": "/Search/AI/FollowUp",
     "params": {"session_id": {"type": "string", "required": True, "desc": "会话 ID（首问返回）"}, "query": {"type": "string", "required": True, "desc": "搜索词"}, "client_message_id": {"type": "string", "desc": "客户端消息 ID"}}},
    {"name": "wpp_search_wechat_index", "description": "微信指数搜索（查询关键词的微信指数相关结果，营销分析用）。", "endpoint": "/Search/WeChatIndex",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索关键词（至少 2 字符）"}, "limit": {"type": "integer", "desc": "每页数量 1-100，默认 10"}, "offset": {"type": "integer", "desc": "首页 0；续页传 next_offset"}, "cursor": {"type": "string", "desc": "续页时原样传回"}, "search_id": {"type": "string", "desc": "续页时原样传回"}}},
    {"name": "wpp_search_live", "description": "直播搜索（返回主播、标题和直播状态）。", "endpoint": "/Search/Live",
     "params": {"query": {"type": "string", "required": True, "desc": "搜索关键词（至少 2 字符）"}, "limit": {"type": "integer", "desc": "每页数量 1-100，默认 10"}, "offset": {"type": "integer", "desc": "首页 0；续页传 next_offset"}, "cursor": {"type": "string", "desc": "续页时原样传回"}, "search_id": {"type": "string", "desc": "续页时原样传回"}}},
]

# ------------------------------------------------------------------ tenpay 域补全
EXTRA_TENPAY_TOOLS = [
    {"name": "wpp_tenpay_ge_ma_skd_qcode", "description": "自定义个人收款单 (商家微信收款码)", "endpoint": "/TenPay/GeMaSkdPayQCode",
     "params": {"Money": {"type": "number", "required": True, "desc": "金额(分)"}, "Name": {"type": "string", "desc": "名称"}, "Remark": {"type": "string", "desc": "备注"}, "Wxid": {"type": "string", "required": True, "desc": "收款 wxid"}}},
    {"name": "wpp_tenpay_sj_skd_qcode", "description": "自定义商家收款单", "endpoint": "/TenPay/SjSkdPayQCode",
     "params": {"Money": {"type": "number", "required": True, "desc": "金额(分)"}, "Name": {"type": "string", "desc": "名称"}, "Remark": {"type": "string", "desc": "备注"}, "Wxid": {"type": "string", "required": True, "desc": "收款 wxid"}}},
    {"name": "wpp_tenpay_open_redpacket", "description": "拆开红包 (redPacketId 来自 inbound 红包事件)", "endpoint": "/TenPay/Openwxhb",
     "params": {"redPacketId": {"type": "string", "required": True, "desc": "红包 ID"}}},
    {"name": "wpp_tenpay_get_encrypt_info", "description": "获取红包/支付的加密信息 (解密 inbound 红包事件)", "endpoint": "/TenPay/GetEncryptInfo",
     "params": {"info": {"type": "string", "required": True, "desc": "信息"}}},
    {"name": "wpp_tenpay_confirm_pre_transfer", "description": "确认支付", "endpoint": "/TenPay/ConfirmPreTransferApi",
     "params": {"wxid": {"type": "string", "required": True, "desc": "wxid"}, "transferId": {"type": "string", "required": True, "desc": "转账 ID"}, "bankSerial": {"type": "string", "desc": "银行流水"}, "bankType": {"type": "string", "desc": "银行类型"}, "payPassword": {"type": "string", "desc": "支付密码"}, "reqKey": {"type": "string", "desc": "请求 key"}}},
    {"name": "wpp_tenpay_generate_pay_qcode", "description": "生成自定义收款二维码", "endpoint": "/TenPay/GeneratePayQCode",
     "params": {"money": {"type": "string", "required": True, "desc": "收款金额，单位元，最多两位小数"}, "name": {"type": "string", "required": True, "desc": "收款项目名称"}, "wxid": {"type": "string", "desc": "wxid"}}},
    {"name": "wpp_tenpay_redpacket_list", "description": "查看红包领取列表", "endpoint": "/TenPay/GetRedPacketListApi",
     "params": {"wxid": {"type": "string", "required": True, "desc": "wxid"}, "xml": {"type": "string", "desc": "XML"}, "offset": {"type": "integer", "desc": "偏移"}, "size": {"type": "integer", "desc": "数量"}}},
    {"name": "wpp_tenpay_open_hongbao_params", "description": "抢红包 (完整参数). SendId=红包ID, SendUserName=发送者, TimingIdentifier=定时标识, Xml=红包消息", "endpoint": "/TenPay/OpenHongBaoWithParams",
     "params": {"SendId": {"type": "string", "required": True, "desc": "发送 ID"}, "SendUserName": {"type": "string", "desc": "发送者名"}, "TimingIdentifier": {"type": "string", "desc": "定时标识"}, "Xml": {"type": "string", "desc": "XML"}}},
    {"name": "wpp_tenpay_receive_no_encryption", "description": "打开红包 (无加密兼容模式). xml=红包消息内容", "endpoint": "/TenPay/ReceivewxhbWithoutEncryption",
     "params": {"Xml": {"type": "string", "required": True, "desc": "XML"}}},
]

# ------------------------------------------------------------------ tools 域补全
EXTRA_TOOLS_TOOLS = [
    {"name": "wpp_tool_download_img", "description": "下载高清图片 (从 CDN). fileId 来自消息 content xml 的 image tag", "endpoint": "/Tools/DownloadImg",
     "params": {"aesKey": {"type": "string", "required": True, "desc": "AES 密钥"}, "fileId": {"type": "string", "required": True, "desc": "文件 ID"}}},
    {"name": "wpp_tool_download_video", "description": "下载视频", "endpoint": "/Tools/DownloadVideo",
     "params": {"aesKey": {"type": "string", "required": True, "desc": "AES 密钥"}, "fileId": {"type": "string", "required": True, "desc": "文件 ID"}}},
    {"name": "wpp_tool_download_voice", "description": "下载语音", "endpoint": "/Tools/DownloadVoice",
     "params": {"aesKey": {"type": "string", "required": True, "desc": "AES 密钥"}, "fileId": {"type": "string", "required": True, "desc": "文件 ID"}, "durationMs": {"type": "integer", "desc": "时长毫秒"}}},
    {"name": "wpp_tool_download_file", "description": "下载文件 (v1.2.1 P1-fix: 需 appID/attachId, vendor v1 文件消息不提供, 可能失败)", "endpoint": "/Tools/DownloadFile",
     "params": {"appID": {"type": "string", "required": True, "desc": "appID"}, "attachId": {"type": "string", "required": True, "desc": "附件 ID"}, "userName": {"type": "string", "desc": "用户名"}}},
    {"name": "wpp_tool_cdn_download_image", "description": "CDN 单独下载高清图片", "endpoint": "/Tools/CdnDownloadImage",
     "params": {"aesKey": {"type": "string", "required": True, "desc": "AES 密钥"}, "fileId": {"type": "string", "required": True, "desc": "文件 ID"}}},
    {"name": "wpp_tool_upload_file", "description": "上传文件. fileType 例: image/png, video/mp4", "endpoint": "/Tools/UploadFile",
     "params": {"fileBase64": {"type": "string", "required": True, "desc": "文件 base64"}, "fileType": {"type": "string", "desc": "文件类型"}}},
    {"name": "wpp_tool_cdn_dns", "description": "获取 CDN 服务器 DNS 信息", "endpoint": "/Tools/GetCdnDns",
     "params": {}},
    {"name": "wpp_tool_set_step_count", "description": "修改微信运动步数 (当天步数, 最高 98000)", "endpoint": "/Tools/setproxy",
     "params": {"steps": {"type": "integer", "required": True, "desc": "步数（最高 98000）"}}},
    {"name": "wpp_tool_download_file_binary", "description": "完整下载微信文件 (二进制). fileNo 来自 file.download_context", "endpoint": "/Tools/DownloadFileBinary",
     "params": {"fileNo": {"type": "string", "required": True, "desc": "文件号"}, "fileName": {"type": "string", "desc": "文件名"}, "toWxid": {"type": "string", "desc": "目标 wxid"}}},
    {"name": "wpp_tool_download_voice_binary", "description": "下载微信语音原文件 (二进制). msgId/newMsgId 来自语音消息", "endpoint": "/Tools/DownloadVoiceBinary",
     "params": {"msg_id": {"type": "integer", "required": True, "desc": "消息 ID"}, "new_msg_id": {"type": "string", "desc": "新消息 ID"}, "to_user_name": {"type": "string", "desc": "接收人 wxid"}, "from_user_name": {"type": "string", "desc": "发送人 wxid"}, "chat_room_name": {"type": "string", "desc": "群标识（群语音）"}, "file_name": {"type": "string", "desc": "文件名"}}},
]

# ------------------------------------------------------------------ user 域补全
EXTRA_USER_TOOLS = [
    {"name": "wpp_user_del_device", "description": "删除登录设备", "endpoint": "/User/DelSafetyInfo",
     "params": {"uuid": {"type": "string", "required": True, "desc": "设备 UUID"}}},
    {"name": "wpp_user_set_privacy", "description": "隐私设置. opt 见 vendor 文档 (e.g. 4=加好友权限)", "endpoint": "/User/PrivacySettings",
     "params": {"opt": {"type": "string", "required": True, "desc": "选项"}, "value": {"type": "string", "required": True, "desc": "值"}}},
    {"name": "wpp_user_change_password", "description": "修改自己的微信登录密码", "endpoint": "/User/SetPasswd",
     "params": {"newPwd": {"type": "string", "required": True, "desc": "新密码"}}},
    {"name": "wpp_user_verify_password", "description": "验证当前密码 (用于敏感操作前)", "endpoint": "/User/VerifyPasswd",
     "params": {"password": {"type": "string", "required": True, "desc": "密码"}}},
    {"name": "wpp_user_report_motion", "description": "上报步数 (微信运动)", "endpoint": "/User/ReportMotion",
     "params": {"steps": {"type": "integer", "required": True, "desc": "步数"}}},
    {"name": "wpp_user_bind_mobile", "description": "换绑手机号", "endpoint": "/User/BindingMobile",
     "params": {"mobile": {"type": "string", "required": True, "desc": "手机号"}, "code": {"type": "string", "required": True, "desc": "验证码"}}},
    {"name": "wpp_user_send_verify_code", "description": "发送手机验证码", "endpoint": "/User/SendVerifyMobile",
     "params": {"mobile": {"type": "string", "required": True, "desc": "手机号"}}},
    {"name": "wpp_user_bind_qq", "description": "绑定 QQ 到当前微信号", "endpoint": "/User/BindQQ",
     "params": {"qq": {"type": "string", "required": True, "desc": "QQ号"}, "password": {"type": "string", "required": True, "desc": "密码"}}},
    {"name": "wpp_user_bind_email", "description": "绑定邮箱", "endpoint": "/User/BindingEmail",
     "params": {"email": {"type": "string", "required": True, "desc": "邮箱"}}},
    {"name": "wpp_user_friend_verification", "description": "设置「加我为朋友时需要验证」. enabled=true 需验证, false 关闭", "endpoint": "/User/FriendVerification",
     "params": {"enabled": {"type": "boolean", "required": True, "desc": "是否启用"}}},
    {"name": "wpp_user_add_me_methods", "description": "设置「添加我的方式」(微信: 我→设置→朋友权限→添加我的方式). 只传要修改的字段, true=允许该方式添加", "endpoint": "/User/AddMeMethods",
     "params": {"opts": {"type": "string", "required": True, "desc": "参数对象"}}},
]

# ------------------------------------------------------------------ login 域补全
EXTRA_LOGIN_TOOLS = [
    {"name": "wpp_login_qr_pad_cloud", "description": "获取 Pad 云登录二维码。", "endpoint": "/Login/GetQRPadCloud",
     "params": {"DeviceName": {"type": "string", "desc": "设备名，默认“我的 iPad”"}, "oversea": {"type": "boolean", "desc": "是否海外"}}},
    {"name": "wpp_login_qr_pad_ppmt", "description": "获取 Pad PPMT 登录二维码。", "endpoint": "/Login/GetQRPadPPMT",
     "params": {"DeviceName": {"type": "string", "desc": "设备名，默认“我的 iPad”"}, "oversea": {"type": "boolean", "desc": "是否海外"}}},
]

# ------------------------------------------------------------------ msg 域补全（特殊处理）
EXTRA_MSG_TOOLS = [
    {"name": "wpp_share_video_msg", "description": "发送分享视频消息", "endpoint": "/Msg/ShareVideo",
     "params": {"ToWxid": {"type": "string", "required": True, "desc": "目标 wxid"}, "Xml": {"type": "string", "required": True, "desc": "视频 XML"}}},
    {"name": "wpp_send_cdn_file", "description": "发送 CDN 文件 (转发, 非上传). fileUrl 是 vendor 已上传的 cdnUrl", "endpoint": "/Msg/SendCDNFile",
     "params": {"Content": {"type": "string", "required": True, "desc": "收到文件消息的 XML"}, "ToWxid": {"type": "string", "required": True, "desc": "目标 wxid"}}},
    {"name": "wpp_start_auto_sync", "description": "启动自动同步（vendor 推消息到 webhook）。", "endpoint": "/Msg/StartAutoSync",
     "params": {"TargetURL": {"type": "string", "desc": "回调 URL（可忽略，用已配置 webhook）"}}},
    {"name": "wpp_send_app_msg", "description": "发送 App 消息（Type: 5=链接 33=小程序 36=文件 42=视频 48=定位）。", "endpoint": "/Msg/SendApp",
     "params": {"ToWxid": {"type": "string", "required": True, "desc": "接收方 wxid 或群 ID"}, "Type": {"type": "integer", "required": True, "desc": "应用消息类型 5/33/36/42/48"}, "Xml": {"type": "string", "required": True, "desc": "业务卡片或已接收消息的应用消息内容"}}},
]

# ------------------------------------------------------------------ voice 域补全
EXTRA_VOICE_TOOLS = [
    {"name": "wpp_voice_result", "description": "获取语音转写结果。", "endpoint": "/Voice/Result",
     "params": {"voice_id": {"type": "string", "required": True, "desc": "语音 ID（转写返回的 voice_id）"}}},
]

# ------------------------------------------------------------------ officialaccounts 域补全
EXTRA_OA_TOOLS = [
    {"name": "wpp_oa_history_message", "description": "获取公众号历史消息 HTML (for 文章抓取)", "endpoint": "/OfficialAccounts/GetMpHistoryMessage",
     "params": {"url": {"type": "string", "required": True, "desc": "文章 URL"}}},
    {"name": "wpp_oa_article_ext", "description": "阅读公众号文章, 返回在看 / 点赞 / 阅读数据", "endpoint": "/OfficialAccounts/GetAppMsgExt",
     "params": {"url": {"type": "string", "required": True, "desc": "文章 URL"}}},
    {"name": "wpp_oa_article_like", "description": "点赞公众号文章, 返回分享 / 在看 / 阅读数据", "endpoint": "/OfficialAccounts/GetAppMsgExtLike",
     "params": {"url": {"type": "string", "required": True, "desc": "文章 URL"}}},
    {"name": "wpp_oa_jsapi_preverify", "description": "公众号 JSAPI 预验证 (用于网页/小程序授权前置)", "endpoint": "/OfficialAccounts/JSAPIPreVerify",
     "params": {"appId": {"type": "string", "required": True, "desc": "appId"}}},
    {"name": "wpp_oa_oauth_authorize", "description": "公众号 OAuth 授权 (url 是授权链接)", "endpoint": "/OfficialAccounts/OauthAuthorize",
     "params": {"url": {"type": "string", "required": True, "desc": "授权 URL"}}},
    {"name": "wpp_oa_qr_authorize", "description": "公众号二维码授权请求 (获取授权二维码)", "endpoint": "/OfficialAccounts/QRConnectAuthorize",
     "params": {"url": {"type": "string", "required": True, "desc": "授权 URL"}}},
    {"name": "wpp_oa_qr_authorize_confirm", "description": "公众号二维码授权确认 (url 是确认链接)", "endpoint": "/OfficialAccounts/QRConnectAuthorizeConfirm",
     "params": {"url": {"type": "string", "required": True, "desc": "授权 URL"}}},
    {"name": "wpp_oa_article_read", "description": "解析公众号文章链接 (短链转正文 Markdown + 图片)", "endpoint": "/OfficialAccounts/ArticleRead",
     "params": {"url": {"type": "string", "required": True, "desc": "文章 URL"}}},
]

# ------------------------------------------------------------------ qwcontact 域补全
EXTRA_QW_TOOLS = [
    {"name": "wpp_qw_apply", "description": "申请添加企微联系人。", "endpoint": "/QWContact/QWApplyAddContact",
     "params": {"username": {"type": "string", "required": True, "desc": "用户名"}, "v1": {"type": "string", "desc": "v1 参数"}, "context": {"type": "string", "desc": "上下文"}}},
]

# ------------------------------------------------------------------ finder 域补全
EXTRA_FINDER_TOOLS = [
    {"name": "wpp_finder_live_detail", "description": "获取视频号直播详情", "endpoint": "/Finder/FinderLiveDetail",
     "params": {"FinderObjectID": {"type": "string", "required": True, "desc": "objectId"}, "FinderNonceID": {"type": "string", "required": True, "desc": "nonceId"}}},
    {"name": "wpp_finder_decrypt_comment", "description": "解密视频号评论内容 (encryptedContent 是加密串)", "endpoint": "/Finder/Decrypt",
     "params": {"Content": {"type": "string", "required": True, "desc": "加密内容"}}},
    {"name": "wpp_finder_msg_session", "description": "获取视频号私信会话 ID", "endpoint": "/Finder/FinderGetMsgSessionId",
     "params": {"FinderUsername": {"type": "string", "required": True, "desc": "目标视频号 ID"}}},
    {"name": "wpp_finder_search_list", "description": "获取视频号搜索列表", "endpoint": "/Finder/FinderSearchList",
     "params": {}},
    {"name": "wpp_finder_topic_list", "description": "获取视频号主题列表", "endpoint": "/Finder/Findergettopiclist",
     "params": {"TopTitle": {"type": "string", "desc": "话题标题"}, "LastBuffer": {"type": "string", "desc": "分页游标"}}},
    {"name": "wpp_finder_comment_list", "description": "获取视频号评论列表 (rootCommentId 可选用于翻页)", "endpoint": "/Finder/GetCommentList",
     "params": {"Id": {"type": "string", "required": True, "desc": "视频号 ID"}, "RootCommentId": {"type": "string", "desc": "根评论 ID"}}},
    {"name": "wpp_finder_comment_detail", "description": "获取视频号评论详情", "endpoint": "/Finder/GetCommentDetail",
     "params": {"FinderUsername": {"type": "string", "required": True, "desc": "用户名"}, "Id": {"type": "string", "required": True, "desc": "ID"}, "RootCommentId": {"type": "string", "desc": "根评论 ID"}}},
    {"name": "wpp_finder_play_video", "description": "播放视频号视频. objectId=视频内容Id, finderUsername=作者, playUrl=播放地址 (选传); loop=true 循环播放", "endpoint": "/Finder/PlayVideo",
     "params": {"object_id": {"type": "string", "desc": "内容 objectId"}, "finder_username": {"type": "string", "desc": "视频号 username"}, "play_url": {"type": "string", "desc": "播放地址"}, "loop": {"type": "boolean", "desc": "是否循环，默认 false"}, "loop_count": {"type": "integer", "desc": "循环次数，默认 0"}, "play_seconds": {"type": "integer", "desc": "播放秒数，默认 0"}, "async": {"type": "boolean", "desc": "是否异步，默认 true"}}},
    {"name": "wpp_finder_play_video_stop", "description": "停止视频号播放任务. taskId=playVideo 返回的任务 ID", "endpoint": "/Finder/PlayVideoStop",
     "params": {"task_id": {"type": "string", "required": True, "desc": "任务 ID（PlayVideo 返回）"}}},
]

# ------------------------------------------------------------------ wxapp 域补全
EXTRA_WXAPP_TOOLS = [
    {"name": "wpp_wxapp_js_login", "description": "授权小程序 (定制)", "endpoint": "/Wxapp/JSLogin",
     "params": {"appId": {"type": "string", "required": True, "desc": "appId"}}},
    {"name": "wpp_wxapp_js_operate_data", "description": "小程序操作 (data 是 JSON.stringify)", "endpoint": "/Wxapp/JSOperateWxData",
     "params": {"appId": {"type": "string", "required": True, "desc": "小程序 appId"}, "data": {"type": "string", "desc": "JSON 业务参数（JSON 字符串）"}, "opt": {"type": "integer", "desc": "操作类型：1=写入 2=读取"}}},
    {"name": "wpp_wxapp_verify_plugin", "description": "小程序获取 HostSign", "endpoint": "/Wxapp/Verifyplugin",
     "params": {"appId": {"type": "string", "required": True, "desc": "appId"}, "url": {"type": "string", "desc": "URL"}}},
    {"name": "wpp_wxapp_pull_pay", "description": "推送小程序支付请求", "endpoint": "/Wxapp/Wxapp/GetpullPay",
     "params": {"appId": {"type": "string", "required": True, "desc": "appId"}}},
    {"name": "wpp_wxapp_delete_oauth", "description": "移除小程序授权. appid=小程序 appid", "endpoint": "/Wxapp/DeleteOauthApp",
     "params": {"appid": {"type": "string", "required": True, "desc": "appid"}}},
    {"name": "wpp_wxapp_js_login_customized", "description": "小程序定制登录. appid=小程序 appid", "endpoint": "/Wxapp/JSLoginCustomized",
     "params": {"appid": {"type": "string", "required": True, "desc": "appid"}}},
]

# ------------------------------------------------------------------ xiaowei 域补全
EXTRA_XIAOWEI_TOOLS = [
    {"name": "wpp_xw_create_session", "description": "小微创建会话。", "endpoint": "/XiaoWei/Chat/Sessions",
     "params": {"client_request_id": {"type": "string", "desc": "去重标识"}, "room_id": {"type": "string", "desc": "房间 ID"}, "welcome_text": {"type": "string", "desc": "欢迎语"}}},
    {"name": "wpp_xw_history_fill", "description": "小微填充历史。", "endpoint": "/XiaoWei/History/Fill",
     "params": {"items": {"type": "array", "items": {"type": "object"}, "required": True, "desc": "结构化卡片数组"}, "operation_type": {"type": "integer", "desc": "操作类型，默认 0"}}},
    {"name": "wpp_xw_history_delete", "description": "小微删除历史。", "endpoint": "/XiaoWei/History/Delete",
     "params": {"delete_item_lists": {"type": "array", "items": {"type": "object"}, "required": True, "desc": "按会话分组的删除列表"}}},
    {"name": "wpp_xw_reddots_read", "description": "小微红点已读。", "endpoint": "/XiaoWei/RedDots/Read",
     "params": {"reddot_id": {"type": "string", "required": True, "desc": "红点 ID（来自 Query）"}, "last_read_timestamp": {"type": "integer", "desc": "最后读取时间戳"}}},
    {"name": "wpp_xw_card_users", "description": "小微卡片用户。", "endpoint": "/XiaoWei/Cards/Users",
     "params": {"card_type": {"type": "string", "required": True, "desc": "卡片类型"}, "page_context": {"type": "string", "desc": "分页上下文"}}},
    {"name": "wpp_xw_card_screenshot_check", "description": "小微卡片截图安全校验。", "endpoint": "/XiaoWei/Cards/ScreenshotSecurityCheck",
     "params": {"message_id": {"type": "string", "required": True, "desc": "消息 ID"}, "app_id": {"type": "string", "required": True, "desc": "appId"}, "media": {"type": "array", "items": {"type": "object"}, "desc": "媒体列表"}, "trace_message_id": {"type": "string", "desc": "追踪 ID"}}},
    {"name": "wpp_xw_suggestions", "description": "小微会话建议。", "endpoint": "/XiaoWei/Conversations/Suggestions",
     "params": {"share_type": {"type": "integer", "desc": "分享类型，默认 0"}, "ui_state": {"type": "integer", "desc": "UI 状态，默认 0"}}},
]

ALL_EXTRA_TOOLS = (
    EXTRA_GROUP_TOOLS + EXTRA_FRIEND_TOOLS + EXTRA_FRIENDCIRCLE_TOOLS + EXTRA_SEARCH_TOOLS
    + EXTRA_TENPAY_TOOLS + EXTRA_TOOLS_TOOLS + EXTRA_USER_TOOLS + EXTRA_LOGIN_TOOLS
    + EXTRA_MSG_TOOLS + EXTRA_VOICE_TOOLS + EXTRA_OA_TOOLS + EXTRA_QW_TOOLS
    + EXTRA_FINDER_TOOLS + EXTRA_WXAPP_TOOLS + EXTRA_XIAOWEI_TOOLS
)
