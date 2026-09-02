"""WPP 通用工具定义表（迁移自 wpp-openclaw agent-tools 各 meta）。

每个工具: {name, description, endpoint, method, params: {name: {type, required, desc}}}
handler 由 tools.py 通用注册器生成（调 client.call(endpoint, body)）。
"""
from __future__ import annotations

# ------------------------------------------------------------------ group 域
GROUP_TOOLS = [
    {
        "name": "wpp_group_info",
        "description": "获取群信息（名称/成员数等）。QID 是群 @chatroom 标识。",
        "endpoint": "/Group/GetChatRoomInfo",
        "params": {"QID": {"type": "string", "required": True, "desc": "群 @chatroom 标识"}},
    },
    {
        "name": "wpp_group_info_detail",
        "description": "获取群详情 (含公告)",
        "endpoint": "/Group/GetChatRoomInfoDetail",
        "params": {"QID": {"type": "string", "required": True, "desc": "群 @chatroom 标识"}},
    },
    {
        "name": "wpp_group_member_detail",
        "description": "获取群单个成员详情",
        "endpoint": "/Group/GetChatRoomMemberDetail",
        "params": {"QID": {"type": "string", "required": True, "desc": "群 @chatroom 标识"}},
    },
    {
        "name": "wpp_group_add_member",
        "description": "邀请群成员 (40 人以内). wxidList 用逗号分隔 vendor 私聊 wxid",
        "endpoint": "/Group/AddChatRoomMember",
        "params": {
            "ChatRoomName": {"type": "string", "required": True, "desc": "群 @chatroom 标识"},
            "ToWxids": {"type": "string", "required": True, "desc": "成员 wxid，多个逗号分隔"},
        },
    },
    {
        "name": "wpp_group_del_member",
        "description": "删除群成员",
        "endpoint": "/Group/DelChatRoomMember",
        "params": {
            "ChatRoomName": {"type": "string", "required": True, "desc": "群 @chatroom 标识"},
            "ToWxids": {"type": "string", "required": True, "desc": "成员 wxid，多个逗号分隔"},
        },
    },
    {
        "name": "wpp_group_create",
        "description": "创建群聊. wxidList 用逗号分隔",
        "endpoint": "/Group/CreateChatRoom",
        "params": {"ToWxids": {"type": "string", "required": True, "desc": "成员 wxid，逗号分隔"}},
    },
    {
        "name": "wpp_group_quit",
        "description": "退出群聊",
        "endpoint": "/Group/Quit",
        "params": {"QID": {"type": "string", "required": True, "desc": "群标识"}},
    },
    {
        "name": "wpp_group_list",
        "description": "获取群列表 (GET 业务路由)",
        "endpoint": "/Group/List",
        "method": "GET",
        "params": {},
    },
    {
        "name": "wpp_group_set_name",
        "description": "设置群名称。",
        "endpoint": "/Group/SetChatRoomName",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群标识"},
            "Content": {"type": "string", "required": True, "desc": "新群名"},
        },
    },
    {
        "name": "wpp_group_set_announcement",
        "description": "设置群公告。",
        "endpoint": "/Group/SetChatRoomAnnouncement",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群标识"},
            "Content": {"type": "string", "required": True, "desc": "公告内容"},
        },
    },
    {
        "name": "wpp_group_set_remarks",
        "description": "设置群备注。",
        "endpoint": "/Group/SetChatRoomRemarks",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群标识"},
            "Content": {"type": "string", "required": True, "desc": "备注"},
        },
    },
    {
        "name": "wpp_group_operate_admin",
        "description": "群管理操作. Val: 1=添加管理员 2=删除管理员 3=转让群主",
        "endpoint": "/Group/OperateChatRoomAdmin",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群标识"},
            "ToWxids": {"type": "string", "required": True, "desc": "成员 wxid"},
            "Val": {"type": "integer", "required": True, "desc": "1=管理员 2=取消 3=转移群主"},
        },
    },
    {
        "name": "wpp_group_transfer_owner",
        "description": "转让群主 (新OwnerUserName)",
        "endpoint": "/Group/SendTransferGroupOwner",
        "params": {
            "QID": {"type": "string", "required": True, "desc": "群标识"},
            "NewOwnerUserName": {"type": "string", "required": True, "desc": "新群主 wxid"},
        },
    },
    {
        "name": "wpp_group_qrcode",
        "description": "获取群二维码",
        "endpoint": "/Group/GetQRCode",
        "params": {"QID": {"type": "string", "required": True, "desc": "群标识"}},
    },
]

# ------------------------------------------------------------------ user 域
USER_TOOLS = [
    {
        "name": "wpp_user_my_profile",
        "description": "取自己个人信息",
        "endpoint": "/User/GetContractProfile",
        "params": {},
    },
    {
        "name": "wpp_user_online_info",
        "description": "获取账号在线状态信息。",
        "endpoint": "/User/GetOnlineInfo",
        "method": "GET",
        "params": {},
    },
    {
        "name": "wpp_user_qrcode",
        "description": "取个人二维码",
        "endpoint": "/User/GetQRCode",
        "params": {},
    },
    {
        "name": "wpp_user_set_alias",
        "description": "设置自己的微信号 (一次性)",
        "endpoint": "/User/SetAlisa",
        "params": {"alias": {"type": "string", "required": True, "desc": "新微信号"}},
    },
    {
        "name": "wpp_user_update_profile",
        "description": "修改自己昵称/签名/性别. sex: 0=未知, 1=男, 2=女",
        "endpoint": "/User/UpdateProfile",
        "params": {
            "NickName": {"type": "string", "desc": "新昵称"},
            "Signature": {"type": "string", "desc": "新签名"},
        },
    },
    {
        "name": "wpp_user_upload_headimage",
        "description": "修改自己头像",
        "endpoint": "/User/UploadHeadImage",
        "params": {"imgBase64": {"type": "string", "required": True, "desc": "头像图片 base64"}},
    },
    {
        "name": "wpp_user_safety_info",
        "description": "登录设备管理 (列出已登录设备)",
        "endpoint": "/User/GetSafetyInfo",
        "params": {},
    },
]

# ------------------------------------------------------------------ friend 域
FRIEND_TOOLS = [
    {
        "name": "wpp_friend_list",
        "description": "获取通讯录好友列表 (一次性全量)",
        "endpoint": "/Friend/GetContractList",
        "params": {},
    },
    {
        "name": "wpp_friend_contract_detail",
        "description": "获取指定 wxid 的好友详情",
        "endpoint": "/Friend/GetContractDetail",
        "params": {"userName": {"type": "string", "required": True, "desc": "好友 wxid"}},
    },
    {
        "name": "wpp_friend_set_remarks",
        "description": "设置好友备注",
        "endpoint": "/Friend/SetRemarks",
        "params": {
            "toWxid": {"type": "string", "required": True, "desc": "好友 wxid"},
            "remarks": {"type": "string", "required": True, "desc": "备注名"},
        },
    },
    {
        "name": "wpp_friend_search",
        "description": "按关键字搜索联系人",
        "endpoint": "/Friend/Search",
        "params": {"keyword": {"type": "string", "required": True, "desc": "搜索关键词"}},
    },
    {
        "name": "wpp_friend_send_request",
        "description": "添加联系人 (发好友请求). content 留空也允许",
        "endpoint": "/Friend/SendRequest",
        "params": {
            "v1": {"type": "string", "required": True, "desc": "验证参数 v1（从搜索/扫码得到）"},
            "v2": {"type": "string", "required": True, "desc": "验证参数 v2"},
        },
    },
    {
        "name": "wpp_friend_pass_verify",
        "description": "通过好友请求 (v1/v2 来自 inbound 事件 payload)",
        "endpoint": "/Friend/PassVerify",
        "params": {
            "opcode": {"type": "integer", "desc": "操作码，默认 1"},
            "scene": {"type": "integer", "desc": "场景，默认 1"},
            "v1": {"type": "string", "desc": "验证 v1"},
            "v2": {"type": "string", "desc": "验证 v2"},
        },
    },
    {
        "name": "wpp_friend_delete",
        "description": "删除好友",
        "endpoint": "/Friend/Delete",
        "params": {"toWxid": {"type": "string", "required": True, "desc": "好友 wxid"}},
    },
    {
        "name": "wpp_friend_blacklist",
        "description": "加入/移除黑名单. val: 1=拉黑 0=取消",
        "endpoint": "/Friend/Blacklist",
        "params": {
            "toWxid": {"type": "string", "required": True, "desc": "好友 wxid"},
            "val": {"type": "integer", "required": True, "desc": "1=拉黑 0=取消"},
        },
    },
    {
        "name": "wpp_friend_gh_list",
        "description": "通讯录完整拉取 (分页+批量补齐名称/备注/头像). 比 getContactList 更全",
        "endpoint": "/Friend/GetGHList",
        "params": {},
    },
    {
        "name": "wpp_friend_request_list",
        "description": "获取好友申请列表（status: pending 待处理/accepted 已通过/rejected 已拒绝）。",
        "endpoint": "/Friend/GetFriendRequestList",
        "params": {
            "page": {"type": "integer", "desc": "页码，从 1 开始，默认 1"},
            "limit": {"type": "integer", "desc": "每页数量，默认 20"},
            "status": {"type": "string", "desc": "状态筛选：pending/accepted/rejected，默认 pending"},
        },
    },
]

# ------------------------------------------------------------------ label 域
LABEL_TOOLS = [
    {"name": "wpp_label_list", "description": "获取标签列表。", "endpoint": "/Label/GetList", "params": {}},
    {"name": "wpp_label_add", "description": "创建标签。", "endpoint": "/Label/Add", "params": {"LabelName": {"type": "string", "required": True, "desc": "标签名"}}},
    {"name": "wpp_label_delete", "description": "删除标签。", "endpoint": "/Label/Delete", "params": {"labelId": {"type": "string", "required": True, "desc": "标签ID"}}},
    {"name": "wpp_label_update_name", "description": "修改标签名。", "endpoint": "/Label/UpdateName", "params": {"labelId": {"type": "string", "required": True, "desc": "标签ID"}, "labelName": {"type": "string", "required": True, "desc": "新标签名"}}},
    {"name": "wpp_label_update_list", "description": "给好友打/移除标签。", "endpoint": "/Label/UpdateList", "params": {"LabelID": {"type": "string", "required": True, "desc": "标签ID"}, "ToWxids": {"type": "string", "required": True, "desc": "好友 wxid，逗号分隔"}}},
    {"name": "wpp_label_friend_list", "description": "获取标签下的好友。", "endpoint": "/Label/GetWXFriendListByLabel", "params": {"labelId": {"type": "integer", "required": True, "desc": "标签ID"}}},
]

# ------------------------------------------------------------------ translate 域
TRANSLATE_TOOLS = [
    {"name": "wpp_translate_text", "description": "翻译文本。", "endpoint": "/Translate/Text", "params": {"text": {"type": "string", "required": True, "desc": "要翻译的文本"}, "source_lang": {"type": "string", "desc": "源语言，默认 auto"}, "target_lang": {"type": "string", "required": True, "desc": "目标语言，如 en/zh"}}},
    {"name": "wpp_translate_send", "description": "翻译并发送消息。", "endpoint": "/Translate/Send", "params": {"text": {"type": "string", "required": True, "desc": "待翻译并发送的文字，最多 5000 字符"}, "target_lang": {"type": "string", "required": True, "desc": "目标语言代码"}, "to_wxid": {"type": "string", "required": True, "desc": "接收人 wxid、群 ID 或 filehelper"}, "source_lang": {"type": "string", "desc": "源语言代码，省略时自动识别，默认 zh"}, "at": {"type": "string", "desc": "群消息中需要 @ 的成员 wxid"}}},
]

# ------------------------------------------------------------------ tools 域
TOOLS_TOOLS = [
    {"name": "wpp_tool_get_a8key", "description": "公众号 A8 Key (open 文章用)", "endpoint": "/Tools/GetA8Key", "params": {"url": {"type": "string", "required": True, "desc": "URL"}}},
    {"name": "wpp_tool_generate_pay_qrcode", "description": "生成支付二维码 (GET)", "endpoint": "/Tools/GeneratePayQCode", "method": "GET", "params": {}},
    {"name": "wpp_tool_band_card_list", "description": "获取余额和银行卡信息", "endpoint": "/Tools/GetBandCardList", "params": {}},
    {"name": "wpp_tool_bound_hard_devices", "description": "获取绑定的硬件设备。", "endpoint": "/Tools/GetBoundHardDevices", "params": {}},
]

# ------------------------------------------------------------------ tenpay 域
TENPAY_TOOLS = [
    {"name": "wpp_tenpay_collect_money", "description": "确认收款", "endpoint": "/TenPay/Collectmoney", "params": {"wxid": {"type": "string", "required": True, "desc": "对方 wxid"}}},
    {"name": "wpp_tenpay_open_hongbao", "description": "抢红包 (带参数, 接收 url + key 自动拆)", "endpoint": "/TenPay/OpenHongBao", "params": {"url": {"type": "string", "required": True, "desc": "红包 url（入站红包事件带）"}, "key": {"type": "string", "required": True, "desc": "红包解密 key"}}},
    {"name": "wpp_tenpay_query_hongbao", "description": "查看红包详情", "endpoint": "/TenPay/Qrydetailwxhb", "params": {"redPacketId": {"type": "string", "required": True, "desc": "红包ID"}}},
    {"name": "wpp_tenpay_receive_hongbao", "description": "接收红包 (无 key 流程, vendor 自动)", "endpoint": "/TenPay/Receivewxhb", "params": {"redPacketId": {"type": "string", "required": True, "desc": "红包ID"}}},
]

# ------------------------------------------------------------------ friendcircle 域
FRIENDCIRCLE_TOOLS = [
    {"name": "wpp_fc_list", "description": "获取朋友圈首页 (firstPageMd5 翻页)", "endpoint": "/FriendCircle/GetList", "params": {}},
    {"name": "wpp_fc_by_user", "description": "获取特定人朋友圈", "endpoint": "/FriendCircle/GetDetail", "params": {"towxid": {"type": "string", "required": True, "desc": "好友 wxid"}}},
    {"name": "wpp_fc_by_snsid", "description": "获取特定 snsId 详情", "endpoint": "/FriendCircle/GetIdDetail", "params": {"id": {"type": "string", "required": True, "desc": "朋友圈 snsId"}, "towxid": {"type": "string", "desc": "好友 wxid"}}},
    {"name": "wpp_fc_comment", "description": "朋友圈点赞/评论. type: 1=点赞 2=文本评论 3=消息 4=with 5=陌生人点赞; 点赞传 content=👍 + type=1. ⚠️ 朋友圈点赞不要用 wpp_finder_like (那是视频号 Finder 的工具)", "endpoint": "/FriendCircle/Comment", "params": {"content": {"type": "string", "desc": "评论文字（点赞留空）"}, "id": {"type": "string", "required": True, "desc": "朋友圈 snsId"}, "type": {"type": "integer", "required": True, "desc": "1=点赞 2=文本评论 3=消息 4=with 5=陌生人点赞"}, "replyCommnetId": {"type": "integer", "desc": "要回复的评论 ID；新评论传 0"}, "toWxid": {"type": "string", "desc": "发布者 wxid"}}},
    {"name": "wpp_fc_operate", "description": "操作朋友圈. type: 1=删除朋友圈 2=设为隐私 3=设为公开 4=删除评论 5=取消点赞; id 是朋友圈 snsId; 删除评论需传 commnetId", "endpoint": "/FriendCircle/Operation", "params": {"id": {"type": "string", "required": True, "desc": "朋友圈 snsId"}, "type": {"type": "integer", "required": True, "desc": "1=删除朋友圈 2=设为隐私 3=设为公开 4=删除评论 5=取消点赞"}, "commnetId": {"type": "integer", "desc": "评论 ID（删除评论时用）"}}},
    {"name": "wpp_fc_sync", "description": "查询朋友圈正在评论/转发的 sns ID (用于同步评论事件)", "endpoint": "/FriendCircle/MmSnsSync", "params": {}},
    {"name": "wpp_fc_comments", "description": "获取某朋友圈的所有评论", "endpoint": "/FriendCircle/GetCommnet", "params": {"xmlData": {"type": "string", "desc": "XML 数据"}}},
]

# ------------------------------------------------------------------ finder 域
FINDER_TOOLS = [
    {"name": "wpp_finder_search", "description": "搜索视频号用户", "endpoint": "/Finder/Search", "params": {"keyword": {"type": "string", "required": True, "desc": "搜索关键词"}}},
    {"name": "wpp_finder_recommend", "description": "获取视频号推荐流", "endpoint": "/Finder/GetRecommend", "params": {}},
    {"name": "wpp_finder_follow", "description": "关注视频号用户. finderId 是视频号 ID（只支持关注，无取关）", "endpoint": "/Finder/Follow", "params": {"finderId": {"type": "string", "required": True, "desc": "视频号 ID"}}},
    {"name": "wpp_finder_like", "description": "点赞视频号内容. Id 是作品 ID（只支持点赞，无取消）", "endpoint": "/Finder/Like", "params": {"Id": {"type": "string", "required": True, "desc": "作品 ID"}}},
    {"name": "wpp_finder_comment", "description": "评论视频号内容", "endpoint": "/Finder/Comment", "params": {"Username": {"type": "string", "required": True, "desc": "内容作者 username"}, "Id": {"type": "string", "required": True, "desc": "内容 ID"}, "Content": {"type": "string", "required": True, "desc": "评论文字"}, "OpType": {"type": "integer", "desc": "操作类型，默认 1"}, "RootCommentId": {"type": "string", "desc": "根评论 ID（回复评论时用）"}, "ReplyCommentId": {"type": "string", "desc": "要回复的评论 ID"}}},
    {"name": "wpp_finder_send_dm", "description": "发视频号私信", "endpoint": "/Finder/FinderSendText", "params": {"FinderUsername": {"type": "string", "required": True, "desc": "视频号"}, "Text": {"type": "string", "required": True, "desc": "私信内容"}}},
    {"name": "wpp_finder_user_page", "description": "获取指定视频号用户主页数据", "endpoint": "/Finder/TargetUserPage", "params": {"Target": {"type": "string", "required": True, "desc": "视频号目标"}}},
    {"name": "wpp_finder_mine", "description": "获取当前账号的视频号中心信息", "endpoint": "/Finder/UserPrepare", "params": {}},
]

# ------------------------------------------------------------------ officialaccounts 域
OA_TOOLS = [
    {"name": "wpp_oa_follow", "description": "关注公众号. operation: follow|unfollow", "endpoint": "/OfficialAccounts/Follow", "params": {"biz": {"type": "string", "required": True, "desc": "公众号 biz"}, "operation": {"type": "integer", "desc": "操作"}}},
    {"name": "wpp_oa_quit", "description": "取消关注公众号", "endpoint": "/OfficialAccounts/Quit", "params": {"biz": {"type": "string", "required": True, "desc": "公众号 biz"}}},
    {"name": "wpp_oa_history", "description": "获取公众号历史消息", "endpoint": "/OfficialAccounts/GetMpHistory", "params": {"url": {"type": "string", "required": True, "desc": "公众号文章 URL"}}},
    {"name": "wpp_oa_article_markdown", "description": "把公众号文章 URL 转成 Markdown (返回标题/公众号/正文/图片)", "endpoint": "/OfficialAccounts/ArticleMarkdown", "params": {"url": {"type": "string", "required": True, "desc": "文章 URL"}}},
    {"name": "wpp_oa_article_list", "description": "获取公众号文章列表. accountId=公众号 __biz 标识 或 historyUrl=历史页链接 (二选一), limit=数量", "endpoint": "/OfficialAccounts/ArticleList", "params": {"url": {"type": "string", "required": True, "desc": "公众号 URL"}}},
    {"name": "wpp_oa_auth_mp_login", "description": "授权公众号登录 (web 扫码)", "endpoint": "/OfficialAccounts/AuthMpLogin", "params": {"url": {"type": "string", "required": True, "desc": "授权 URL"}}},
]

# ------------------------------------------------------------------ qwcontact 域
QWC_TOOLS = [
    {"name": "wpp_qw_search", "description": "搜索企业微信联系人。", "endpoint": "/QWContact/SearchQWContact", "params": {"username": {"type": "string", "required": True, "desc": "企微账号"}}},
    {"name": "wpp_qw_add", "description": "添加企业微信联系人。", "endpoint": "/QWContact/QWAddContact", "params": {"username": {"type": "string", "required": True, "desc": "企微账号"}, "v1": {"type": "string", "desc": "验证 v1"}}},
]

# ------------------------------------------------------------------ favorites 域
FAVORITES_TOOLS = [
    {"name": "wpp_fav_sync", "description": "同步收藏。", "endpoint": "/Favor/Sync", "params": {}},
    {"name": "wpp_fav_info", "description": "获取收藏信息。", "endpoint": "/Favor/GetFavInfo", "params": {"favId": {"type": "integer", "desc": "收藏 ID"}}},
    {"name": "wpp_fav_item", "description": "获取收藏条目。", "endpoint": "/Favor/GetFavItem", "params": {"favId": {"type": "integer", "required": True, "desc": "收藏 ID"}}},
    {"name": "wpp_fav_del", "description": "删除收藏。", "endpoint": "/Favor/Del", "params": {"favId": {"type": "integer", "required": True, "desc": "收藏 ID"}}},
]

# ------------------------------------------------------------------ xiaowei 域
XIAOWEI_TOOLS = [
    {"name": "wpp_xw_invite", "description": "邀请进入小微会话。", "endpoint": "/XiaoWei/Invites", "params": {"wxids": {"type": "array", "items": {"type": "string"}, "required": True, "desc": "wxid 列表"}}},
    {"name": "wpp_xw_history_list", "description": "获取小微历史列表。", "endpoint": "/XiaoWei/History/List", "params": {"scroll_type": {"type": "integer", "desc": "滚动类型"}}},
    {"name": "wpp_xw_reddots", "description": "查询小微红点。", "endpoint": "/XiaoWei/RedDots/Query", "params": {}},
]

# ------------------------------------------------------------------ wxapp 域
WXAPP_TOOLS = [
    {"name": "wpp_wxapp_openid", "description": "查询小程序用户的 openId", "endpoint": "/Wxapp/GetUserOpenId", "params": {"appId": {"type": "string", "required": True, "desc": "小程序 appId"}}},
    {"name": "wpp_wxapp_record", "description": "获取小程序使用记录。", "endpoint": "/Wxapp/GetWxAppRecord", "params": {"appId": {"type": "string", "required": True, "desc": "小程序 appId"}}},
    {"name": "wpp_wxapp_cloud_call", "description": "小程序云函数调用 (云开发)", "endpoint": "/Wxapp/CloudCallFunction", "params": {"appId": {"type": "string", "required": True, "desc": "小程序 appId"}, "functionName": {"type": "string", "required": True, "desc": "云函数名"}, "data": {"type": "string", "desc": "云函数 JSON 业务参数（JSON 字符串）"}}},
    {"name": "wpp_wxapp_js_session", "description": "获取小程序 sessionid", "endpoint": "/Wxapp/JSGetSessionid", "params": {"appId": {"type": "string", "required": True, "desc": "小程序 appId"}, "url": {"type": "string", "desc": "URL"}}},
    {"name": "wpp_wxapp_oauth_list", "description": "获取小程序授权管理列表", "endpoint": "/Wxapp/GetOauthList", "params": {}},
    {"name": "wpp_wxapp_unionpay", "description": "云闪付支付", "endpoint": "/Wxapp/GetUnionPay", "params": {"orderId": {"type": "string", "required": True, "desc": "订单 ID"}}},
]

# ------------------------------------------------------------------ voice 域
VOICE_TOOLS = [
    {"name": "wpp_voice_transcribe", "description": "语音转写文字（base64 输入）。", "endpoint": "/Voice/Transcribe", "params": {"audio_base64": {"type": "string", "required": True, "desc": "语音 base64（silk/amr）"}, "from_user_name": {"type": "string", "desc": "发送人 wxid"}, "to_user_name": {"type": "string", "desc": "接收人 wxid"}, "scene": {"type": "integer", "desc": "场景，默认 0"}, "encode_type": {"type": "integer", "desc": "编码类型，默认 2"}, "sample_rate": {"type": "integer", "desc": "采样率，默认 16000"}}},
    {"name": "wpp_voice_message_transcribe", "description": "语音消息转写（按消息 ID）。", "endpoint": "/Voice/MessageTranscribe", "params": {"msg_id": {"type": "integer", "required": True, "desc": "语音消息 ID"}, "new_msg_id": {"type": "string", "desc": "新消息 ID"}, "from_user_name": {"type": "string", "desc": "发送人 wxid"}, "chat_room_name": {"type": "string", "desc": "群标识（群语音时）"}, "client_msg_id": {"type": "string", "desc": "客户端消息 ID"}, "voice_id": {"type": "string", "desc": "语音 ID"}, "length": {"type": "integer", "desc": "语音长度"}}},
]

# ------------------------------------------------------------------ webhook 域
WEBHOOK_TOOLS = [
    {"name": "wpp_webhook_set", "description": "设置 webhook 回调地址。", "endpoint": "/Webhook/Set", "params": {"url": {"type": "string", "required": True, "desc": "回调 URL"}}},
    {"name": "wpp_webhook_get", "description": "读取当前 webhook 配置", "endpoint": "/Webhook/Get", "method": "GET", "params": {}},
    {"name": "wpp_webhook_remove", "description": "删除 webhook 配置", "endpoint": "/Webhook/Remove", "params": {}},
    {"name": "wpp_webhook_business_set", "description": "设置业务 webhook。", "endpoint": "/Webhook/Business/Set", "params": {"url": {"type": "string", "required": True, "desc": "业务回调 URL"}}},
    {"name": "wpp_webhook_test", "description": "测试发送 Webhook 消息（按授权码，验证回调配置是否生效）。", "endpoint": "/Webhook/Test", "params": {"MessageType": {"type": "string", "desc": "测试消息类型，默认 sync_message"}, "TestData": {"type": "object", "desc": "测试数据（发送到配置的 Webhook）"}}},
]

# ------------------------------------------------------------------ sayhello 域
SAYHELLO_TOOLS = [
    {"name": "wpp_sayhello_v1", "description": "打招呼（模型 v1）。", "endpoint": "/SayHello/Modelv1", "params": {"url": {"type": "string", "required": True, "desc": "打招呼链接"}, "verifyContent": {"type": "string", "desc": "验证内容"}}},
    {"name": "wpp_sayhello_v2", "description": "通过微信号/手机号申请好友。", "endpoint": "/SayHello/Modelv2", "params": {"content": {"type": "string", "required": True, "desc": "申请说明"}, "toUserName": {"type": "string", "required": True, "desc": "对方微信号或手机号"}, "scene": {"type": "integer", "desc": "场景，默认 15"}, "fromScene": {"type": "integer", "desc": "来源场景"}, "searchScene": {"type": "integer", "desc": "搜索场景"}}},
    {"name": "wpp_sayhello_v3", "description": "使用搜索结果申请好友。", "endpoint": "/SayHello/Modelv3", "params": {"v3": {"type": "string", "required": True, "desc": "搜索结果 contact_id"}, "v4": {"type": "string", "required": True, "desc": "搜索结果验证凭据"}, "verifyContent": {"type": "string", "desc": "申请说明"}, "scene": {"type": "integer", "desc": "场景，默认 15"}}},
]

# ------------------------------------------------------------------ login 域
LOGIN_TOOLS = [
    {"name": "wpp_login_get_qr", "description": "获取 iPad 登录二维码。", "endpoint": "/Login/GetQR", "params": {"DeviceName": {"type": "string", "desc": "设备名称"}}},
    {"name": "wpp_login_check_qr", "description": "检测二维码状态（uuid）。", "endpoint": "/Login/CheckQR", "params": {"uuid": {"type": "string", "required": True, "desc": "二维码 UUID"}}},
    {"name": "wpp_login_heartbeat", "description": "单次心跳。", "endpoint": "/Login/HeartBeat", "params": {}},
    {"name": "wpp_login_heartbeat_long", "description": "长连接心跳包。", "endpoint": "/Login/HeartBeatLong", "params": {}},
    {"name": "wpp_login_heartbeat_logs", "description": "获取心跳日志。", "endpoint": "/Login/HeartBeatLogs", "method": "GET", "params": {}},
    {"name": "wpp_login_auto_heartbeat", "description": "开启自动心跳。", "endpoint": "/Login/AutoHeartBeat", "params": {}},
    {"name": "wpp_login_cache_info", "description": "获取本地登录缓存。", "endpoint": "/Login/GetCacheInfo", "params": {}},
    {"name": "wpp_login_longlink_status", "description": "查询长连接状态。", "endpoint": "/Login/LongLinkStatus", "method": "GET", "params": {}},
    {"name": "wpp_login_logout", "description": "退出登录。", "endpoint": "/Login/LogOut", "params": {}},
    {"name": "wpp_login_newinit", "description": "初始化登录。", "endpoint": "/Login/Newinit", "params": {"userInfo": {"type": "string", "desc": "用户信息"}}},
    {"name": "wpp_login_get_status", "description": "获取聚合登录状态。", "endpoint": "/Login/GetLoginStatus", "method": "GET", "params": {}},
    {"name": "wpp_login_check_can_set_alias", "description": "检测能否设置微信号。", "endpoint": "/Login/CheckCanSetAlias", "method": "GET", "params": {}},
    {"name": "wpp_login_submit_verify_code", "description": "提交扫码登录验证码。", "endpoint": "/Login/SubmitLoginVerificationCode", "params": {"code": {"type": "string", "required": True, "desc": "验证码"}}},
]

# ------------------------------------------------------------------ customized 域
CUSTOMIZED_TOOLS = [
    {"name": "wpp_customized_auth_batch", "description": "定制化批量鉴权（统一）。", "endpoint": "/Customized/WXCTDUniftyAuthBatch", "params": {"Username": {"type": "string", "required": True, "desc": "用户 wxid"}}},
]

# ------------------------------------------------------------------ search 域
SEARCH_TOOLS = [
    {
        "name": "wpp_search_all",
        "description": "微信综合搜索 (文章/公众号/小程序一起)",
        "endpoint": "/Search/All",
        "params": {
            "query": {"type": "string", "required": True, "desc": "搜索词"},
            "limit": {"type": "integer", "desc": "返回数量上限"},
        },
    },
    {
        "name": "wpp_search_official_accounts",
        "description": "公众号与账号搜索",
        "endpoint": "/Search/OfficialAccounts",
        "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}},
    },
    {
        "name": "wpp_search_moments",
        "description": "朋友圈搜索",
        "endpoint": "/Search/Moments",
        "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}},
    },
    {
        "name": "wpp_search_mini_programs",
        "description": "小程序搜索",
        "endpoint": "/Search/MiniPrograms",
        "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}},
    },
    {
        "name": "wpp_search_articles",
        "description": "公众号文章搜索",
        "endpoint": "/Search/Articles",
        "params": {"query": {"type": "string", "required": True, "desc": "搜索词"}},
    },
]
