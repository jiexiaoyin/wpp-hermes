# wpp_* 工具清单

本插件在 Hermes 中注册 **292 个 `wpp_*` 工具**，覆盖微信消息、群聊、通讯录、朋友圈、视频号、公众号、搜索、小程序、支付、账号与媒体等能力。模型会根据你的自然语言请求自动选择工具；你也可以在提示里直接点名工具。

约定：

- 除少数明确说明的工具外，参数都可选传 `account_id`（默认 `default`）；多账号部署时应显式指定。
- `toWxid` / `QID` 等目标既可以是用户 wxid，也可以是群 `xxx@chatroom`。
- 资金、群管理、朋友圈发布、小微等高风险工具受管理员或账号开关约束，未开启时会直接拒绝执行。
- 本页按功能域分组；同一工具可能同时出现在多个调用场景中，以实际注册名为准。

## 目录

- [消息收发与互动](#messaging)（26）
- [群聊管理](#groups)（24）
- [好友与通讯录](#contacts)（20）
- [历史检索、黑话与身份识别](#insight)（3）
- [朋友圈](#moments)（29）
- [视频号](#channels)（19）
- [公众号](#official)（15）
- [微信搜索](#search)（23）
- [小程序](#miniprogram)（12）
- [支付与红包](#payments)（16）
- [语音与媒体文件](#media)（20）
- [账号、登录与资料](#account)（37）
- [收藏](#favorites)（4）
- [企业微信联系人](#wecom)（3）
- [小微智能体](#xiaowei)（20）
- [MCP 兼容工具](#mcp)（10）
- [Webhook 管理](#webhook)（6）
- [分享与集成](#integration)（3）
- [插件自身](#plugin)（2）

## 消息收发与互动

发送文本、图片、文件、语音、视频、链接、名片、位置、小程序等消息，以及引用回复、撤回、同步、翻译、打招呼。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_send_text` | 发送文本消息到微信 | `toWxid`, `content` |
| `wpp_send_image` | 发送图片到微信 | `toWxid`, `imgBase64` |
| `wpp_send_location` | 发送定位卡片到微信 (Type 48 位置消息) | `toWxid`, `latitude`, `longitude` |
| `wpp_send_emoji` | 发送 Emoji 表情到微信 (用 md5) | `toWxid`, `emojiMd5` |
| `wpp_send_card` | 发送微信名片到微信 (分享联系人) | `toWxid`, `cardWxid` |
| `wpp_send_mini_program` | 发送小程序消息到微信 | `toWxid`, `title`, `url`, `appId` |
| `wpp_send_link` | 分享链接卡片到微信 | `toWxid`, `title`, `linkUrl` |
| `wpp_send_file_v2` | 发送文件到微信 (自动上传) | `toWxid`, `fileName`, `base64` |
| `wpp_send_file` | 发送文件到微信 | `toWxid`, `fileUrl`, `fileName` |
| `wpp_send_voice` | 发送语音到微信 | `toWxid` |
| `wpp_send_video` | 发送视频到微信 | `toWxid`, `videoUrl` |
| `wpp_send_ai_image` | AI 生成图片并自动发送到微信（Hermes image_gen → 百炼 qwen-image 生成 → 自动 send_image）。一步完成「生成海报/配图 → 发微信」 | `toWxid`, `prompt` |
| `wpp_send_ai_voice` | AI 合成语音并自动发送到微信（Hermes TTS / 阿里 Token Plan，按当前会话情绪自动选择语气）。会转成微信语音气泡；超过 60 秒自动分段发送 | `toWxid`, `text` |
| `wpp_send_message` | 统一发送消息. toWxid 目标(群id或对方wxid), type 类型(text/image/video/voice/file/link/card/location/miniprogram/emoji), con… | `toWxid`, `type`, `content` |
| `wpp_send_cdn_file` | 发送 CDN 文件 (转发, 非上传). fileUrl 是 vendor 已上传的 cdnUrl | `Content`, `ToWxid` |
| `wpp_send_cdn_image` | 发送 CDN 图片（转发图片）. imgUrl 来自 cdnDownloadImage 流程或上传回调. | `toWxid`, `imgUrl` |
| `wpp_send_cdn_video` | 发送 CDN 视频（转发视频）. | `toWxid`, `videoUrl` |
| `wpp_send_app_message` | 发送结构化应用卡片（链接/小程序/音乐/文件）. items 数组, 单次最多 20 项. | `items` |
| `wpp_quote_reply` | 引用回复微信消息. 自动从 DB 取被引用消息构造完整引用卡片（文本/图片通用），群聊自动 @ 被回复人. | `toWxid`, `content`, `msgId` |
| `wpp_revoke_msg` | 撤回微信消息 | `msgId`, `toWxid` |
| `wpp_sync_message` | 同步微信消息增量 | — |
| `wpp_translate_text` | 翻译文本，可指定 source_lang/target_lang。 | `text`, `target_lang` |
| `wpp_translate_send` | 翻译后直接发到指定会话（to_wxid）。 | `text`, `target_lang`, `to_wxid` |
| `wpp_sayhello_v1` | 打招呼 v1（url + 验证语）。老流程，新场景优先 v2/v3。 | `url` |
| `wpp_sayhello_v2` | 按微信号或手机号申请加好友（content 是验证语）。 | `content`, `toUserName` |
| `wpp_sayhello_v3` | 用搜索结果申请加好友（v3/v4 来自搜索返回，不能手填）。 | `v3`, `v4` |

## 群聊管理

建群、拉人/踢人、改群名与公告、管理员、群主转让、群二维码、群信息与成员查询、群发。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_group_info` | 查群基本信息（群名/群主/成员数）。QID 是群 id，形如 12345678@chatroom。要读群公告请用 wpp_group_info_detail。 | `QID` |
| `wpp_group_info_detail` | 查群详情，含群公告与群状态。读群规/公告时用这个；只要人数和群名用 wpp_group_info。 | `QID` |
| `wpp_group_member_detail` | 查某个成员在该群的详情（群昵称/入群时间）。需要 QID + 成员 wxid。 | `QID` |
| `wpp_group_list` | 拉取当前账号加入的全部群列表。清点群、找 QID 时先调这个。 | — |
| `wpp_group_list_compat` | 获取群列表（GET 兼容路由）. | — |
| `wpp_group_create` | 新建群聊并拉人。至少 2 个 wxid，逗号分隔。 | `ToWxids` |
| `wpp_group_add_member` | 邀请好友进群（40 人以内直邀）。超过 40 人用 wpp_group_invite_member 走邀请链；被邀请人需与操作账号是好友。 | `ChatRoomName`, `ToWxids` |
| `wpp_group_del_member` | 把成员移出群（踢人）。不可撤销，调用前确认群 id 与 wxid；仅群主/管理员有权限。 | `ChatRoomName`, `ToWxids` |
| `wpp_group_invite_member` | 邀请进群（40 人以上走邀请链，需对方同意）。40 人以内用 wpp_group_add_member。 | `ChatRoomName`, `ToWxids` |
| `wpp_group_quit` | 当前账号退出该群。退出后不再收该群消息，需别人重新拉回才能恢复。 | `QID` |
| `wpp_group_set_name` | 改群名称（群主/管理员）。Content 是新群名，会同步给全体成员。 | `QID`, `Content` |
| `wpp_group_set_announcement` | 设置/更新群公告。公告会推送给群成员，属于对外可见变更，慎用。 | `QID`, `Content` |
| `wpp_group_set_remarks` | 设置群备注（仅自己可见，不影响其他成员）。 | `QID`, `Content` |
| `wpp_group_operate_admin` | 群管理员操作：Val=1 加管理员，2 撤管理员，3 转让群主。转让群主不可逆，务必确认 ToWxids。 | `QID`, `ToWxids`, `Val` |
| `wpp_group_transfer_owner` | 转让群主给指定 wxid。不可逆，仅现任群主可操作。 | `QID`, `NewOwnerUserName` |
| `wpp_group_qrcode` | 取群二维码（拉人扫码进群用）。二维码有效期由微信决定。 | `QID` |
| `wpp_group_mass_text` | 群发文本到多个群 | `toIds`, `content` |
| `wpp_group_pat` | 群内拍一拍。ToUserName 是成员 wxid，Scene 是场景值。 | `QID`, `ToUserName` |
| `wpp_group_scan_into` | 扫码进群 (url 是 group qr url) | `Url` |
| `wpp_group_facing_create` | 创建面对面群 (基于经纬度). latitude/longitude 是数字坐标 | `Latitude`, `Longitude`, `OpCode` |
| `wpp_group_scan_enterprise` | 扫码进群 (企业). url 是群二维码 | `Url` |
| `wpp_group_consent_join` | 同意入群邀请。Url 来自入群邀请事件，不能构造。 | `Url` |
| `wpp_group_move_contract` | 把群保存到通讯录：Val=1 保存，0 取消。 | `QID`, `Val` |
| `wpp_group_set_access_verify` | 群聊邀请开关 (true=需要验证, false=直接进) | `QID`, `Enable` |

## 好友与通讯录

好友列表与详情、备注、搜索、加好友/通过验证、删除与拉黑、好友申请、标签管理。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_friend_list` | 全量拉取通讯录好友，一次性返回，量大。只查个别人用 wpp_friend_search。 | — |
| `wpp_friend_contract_detail` | 按 wxid 查单个好友的详细资料（昵称/备注/地区）。 | `userName` |
| `wpp_friend_set_remarks` | 给好友设备注（仅自己可见，对方看不到）。 | `toWxid`, `remarks` |
| `wpp_friend_search` | 按昵称/备注/微信号关键字搜索已有联系人。 | `keyword` |
| `wpp_friend_send_request` | 添加联系人（发好友请求）. 新路径: 直接传 /Friend/Search 返回的 sourceContext 即可; 旧路径: opcode+scene+v1+v2 必须同时提供. verifyContent 是验证… | — |
| `wpp_friend_pass_verify` | 通过别人的好友申请。v1/v2 必须取自 inbound 好友请求事件，不能手填。 | — |
| `wpp_friend_delete` | 删除好友（单向：对方仍保留你）。不可撤销，删前确认 wxid。 | `toWxid` |
| `wpp_friend_blacklist` | 拉黑/取消拉黑：val=1 拉黑，0 取消。拉黑后对方消息不再送达。 | `toWxid`, `val` |
| `wpp_friend_gh_list` | 通讯录完整拉取 (分页+批量补齐名称/备注/头像). 比 getContactList 更全 | — |
| `wpp_friend_request_list` | 获取好友申请列表（status: pending 待处理/accepted 已通过/rejected 已拒绝）。 | — |
| `wpp_friend_state` | 查好友状态（在线/性别/地区）。toWxid + opCode。 | `toWxid` |
| `wpp_friend_lbs` | 附近的人（经纬度 + opCode）。会暴露当前定位。 | `latitude`, `longitude` |
| `wpp_friend_set_auto_accept` | 配置自动通过好友申请（默认关闭）. enabled=true 才会生效; scenes 是允许自动通过的来源场景白名单, 留空=所有场景（不推荐）. | `enabled` |
| `wpp_label_list` | 取标签列表（含 labelId）。其它标签操作都要先拿到 labelId。 | — |
| `wpp_label_add` | 新建标签，LabelName 是标签名。 | `LabelName` |
| `wpp_label_delete` | 删除标签。只删标签本身，不影响好友关系；不可恢复。 | `labelId` |
| `wpp_label_update_name` | 重命名标签（labelId + 新名字）。 | `labelId`, `labelName` |
| `wpp_label_update_list` | 给好友批量打/移除标签（LabelID + ToWxids）。 | `LabelID`, `ToWxids` |
| `wpp_label_friend_list` | 取某个标签下的全部好友。 | `labelId` |
| `wpp_label_update_order` | 更新通讯录标签的显示顺序. 数组顺序=目标显示顺序, 未列出的标签顺序不变. 标签 ID 用 wpp_label_list 拿. | `LabelIDs` |

## 历史检索、黑话与身份识别

按群/联系人/关键词检索本地消息历史，查询群黑话，昵称与 wxid 互查。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_history_search` | 查微信消息历史 (入站/出站消息)。调用: wpp_history_search(target='张三', keyword='红包', limit=20)。target 可以是昵称/wxid/群名。返回消息列表 JSO… | — |
| `wpp_jargon_query` | 查询群黑话含义（自主学习统计的词频/上下文）。 | `groupId` |
| `wpp_identity_lookup` | 匹配微信昵称↔wxid (本地通讯录)。调用: wpp_identity_lookup(name='张三') 或 wpp_identity_lookup(wxid='wxid_xxx')。用于识别 '这个wxid是谁'… | — |

## 朋友圈

发图文/视频/纯文字朋友圈、评论、点赞、列表与详情、同步、导出、上传媒体、自动跟发。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_fc_publish_images` | 发布图片朋友圈 (1-9张，带权限校验) | `title`, `imageBase64List` |
| `wpp_fc_publish_video` | 发布视频朋友圈 (带权限校验) | `title`, `videoBase64`, `thumbBase64` |
| `wpp_fc_publish_text` | 发布文字朋友圈 (带权限校验) | `title` |
| `wpp_fc_publish_ai` | AI 配图并发布朋友圈（Hermes image_gen → 百炼 qwen-image 生成 1-9 张配图 → 自动发布图文朋友圈）。一步完成「写文案 + 自动配图 + 发朋友圈」 | `title` |
| `wpp_fc_list` | 拉取朋友圈时间线。firstPageMd5 用于翻页，首屏不传。 | — |
| `wpp_fc_by_user` | 看指定人的朋友圈（需对方 wxid 且对你可见）。 | `towxid` |
| `wpp_fc_by_snsid` | 获取特定 snsId 详情 | `id` |
| `wpp_fc_comment` | 朋友圈点赞/评论. type: 1=点赞 2=文本评论 3=消息 4=with 5=陌生人点赞; 点赞传 content=👍 + type=1. ⚠️ 朋友圈点赞不要用 wpp_finder_like (那是视频号 F… | `id`, `type` |
| `wpp_fc_operate` | 操作朋友圈. type: 1=删除朋友圈 2=设为隐私 3=设为公开 4=删除评论 5=取消点赞; id 是朋友圈 snsId; 删除评论需传 commnetId | `id`, `type` |
| `wpp_fc_sync` | 查询朋友圈正在评论/转发的 sns ID (用于同步评论事件) | — |
| `wpp_fc_comments` | 取某条朋友圈的全部评论，需要该条的 xmlData。 | — |
| `wpp_fc_upload_media` | 上传朋友圈媒体 (发朋友圈时用). base64 是要上传的图片/视频内容, key 是媒体标识 | `key`, `base64` |
| `wpp_fc_upload_video` | 上传朋友圈视频 (发视频朋友圈前置). videoData=视频 base64, thumbData=缩略图 base64 | `videoData`, `thumbData` |
| `wpp_fc_upload_image` | 上传单张朋友圈图片（base64）。发布前置步骤。 | `imageData` |
| `wpp_fc_upload_images` | 批量上传朋友圈图片. imageDataList=图片 base64 数组 | `imageDataList` |
| `wpp_fc_collect_circle` | 读取收藏的朋友圈动态详情. sourceId=收藏来源标识 | `sourceId` |
| `wpp_fc_send_fav_item` | 从收藏项发布朋友圈. favItemId=收藏项ID(数字), sourceId=收藏来源 | `favItemId`, `sourceId` |
| `wpp_fc_send_one_id` | 通过已有动态 id 再发朋友圈 (支持文字/图片/视频/链接). id=原动态id | `id` |
| `wpp_fc_set_days` | 设置朋友圈可见范围. range=three_days/one_month/six_months/all | `range` |
| `wpp_fc_active_tasks` | 查正在执行的朋友圈评论/转发任务。 | — |
| `wpp_fc_push_comment` | 启动评论检查后台任务, 转发 callback 形式的评论事件 | `id`, `forwardAddr` |
| `wpp_fc_cdn_download_video` | 朋友圈下载 CDN 视频（key/url 来自朋友圈详情返回）。 | `key`, `url` |
| `wpp_fc_set_background` | 设置朋友圈背景图（url + 缩略图 url）。 | `url` |
| `wpp_fc_set_privacy` | 朋友圈隐私设置. scope 查 vendor 文档. | `scope` |
| `wpp_fc_export` | 建朋友圈批量导出任务（导出某人的朋友圈为 zip/md）. 返回 task_id, 之后用 wpp_fc_export_status 轮询, 完成后用 wpp_fc_export_download 取文件. | `towxid` |
| `wpp_fc_export_status` | 查询朋友圈批量导出任务进度. | `task_id` |
| `wpp_fc_export_download` | 下载朋友圈批量导出的产物文件（任务完成后才可下载）. name 可选: 朋友圈.zip（默认） / 朋友圈.md / 导出信息.json. 返回文件字节数与文件名. | `task_id` |
| `wpp_fc_auto_forward` | 配置朋友圈自动跟发（把 source_wxid 的新公开动态自动同步发到自己朋友圈）. enabled=true 时必须传 content_owner_authorized=true 确认已获授权, 且账号级 frie… | `enabled` |
| `wpp_fc_auto_forward_status` | 查询朋友圈自动跟发策略（开关/来源账号）与最近同步状态. | — |

## 视频号

搜索视频号内容、关注/点赞/评论/私信、用户主页、直播、话题与评论列表、视频播放控制。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_finder_search` | 搜索视频号用户或内容。keyword 支持昵称/主题词；要内容列表用 wpp_finder_search_list。 | `keyword` |
| `wpp_finder_recommend` | 拉取视频号推荐流（无需参数，返回推荐内容列表）。 | — |
| `wpp_finder_follow` | 关注视频号用户. finderId 是视频号 ID（只支持关注，无取关） | `finderUsername` |
| `wpp_finder_like` | 点赞视频号内容. Id 是作品 ID（只支持点赞，无取消） | `Id`, `finderUsername` |
| `wpp_finder_comment` | 评论视频号作品。回复某条评论时传 RootCommentId + ReplyCommentId。 | `Username`, `Id`, `Content` |
| `wpp_finder_send_dm` | 给视频号作者发私信。发之前先用 wpp_finder_msg_session 建会话。 | `FinderUsername`, `Text` |
| `wpp_finder_user_page` | 取指定视频号用户的主页数据（简介/作品数）。Target 是用户名或 ID。 | `Target` |
| `wpp_finder_mine` | 取当前账号自己的视频号中心信息。 | — |
| `wpp_finder_live_detail` | 取视频号直播详情（标题/在线人数/状态）。需 FinderObjectID + FinderNonceID，来自直播入口。 | `FinderObjectID`, `FinderNonceID` |
| `wpp_finder_decrypt_comment` | 解密视频号评论内容 (encryptedContent 是加密串) | `Content` |
| `wpp_finder_msg_session` | 取与视频号作者的私信会话 ID。发私信前必须先调这个。 | `FinderUsername` |
| `wpp_finder_search_list` | 视频号搜索列表（按 TopTitle 分类，LastBuffer 翻页）。 | — |
| `wpp_finder_topic_list` | 取视频号话题列表，用于按话题找内容。 | — |
| `wpp_finder_comment_list` | 获取视频号评论列表 (rootCommentId 可选用于翻页) | `finderUsername`, `Id` |
| `wpp_finder_comment_detail` | 取单条视频号评论的详情。 | `FinderUsername`, `Id` |
| `wpp_finder_play_video` | 播放视频号视频. objectId=视频内容Id, finderUsername=作者, playUrl=播放地址 (选传); loop=true 循环播放 | — |
| `wpp_finder_play_video_stop` | 停止视频号播放任务. taskId=playVideo 返回的任务 ID | `task_id` |
| `wpp_finder_play_video_status` | 查询视频号播放任务状态. task_id=任务 ID. | `task_id` |
| `wpp_finder_play_video_tasks` | 列出视频号播放任务（运行中 + 24h 内已结束）. | — |

## 公众号

关注/取关公众号、读历史文章与正文、点赞、OAuth 授权、JS-SDK 预校验。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_oa_follow` | 关注或取关公众号：operation=follow\|unfollow，biz 是公众号 __biz 标识。 | `biz` |
| `wpp_oa_quit` | 取消关注公众号（biz 是 __biz）。不可逆，需要时用 wpp_oa_follow 重新关注。 | `biz` |
| `wpp_oa_history` | 取公众号历史消息列表。url 是公众号主页链接。 | `url` |
| `wpp_oa_article_markdown` | 把公众号文章 URL 转成 Markdown (返回标题/公众号/正文/图片) | `url` |
| `wpp_oa_article_list` | 获取公众号文章列表. accountId=公众号 __biz 标识 或 historyUrl=历史页链接 (二选一), limit=数量 | — |
| `wpp_oa_auth_mp_login` | 公众号网页登录授权（扫码）。用于拿公众号后台会话。 | `url` |
| `wpp_oa_history_message` | 取公众号历史消息 HTML（抓文章正文用，比 json 接口全）。 | `url` |
| `wpp_oa_article_ext` | 读文章并返回在看/点赞/阅读数（数据面，不发内容）。 | `url` |
| `wpp_oa_article_like` | 给公众号文章点赞，返回分享/在看/阅读数据。会改变文章状态。 | `url` |
| `wpp_oa_jsapi_preverify` | 公众号 JSAPI 预验证 (用于网页/小程序授权前置) | `appId` |
| `wpp_oa_oauth_authorize` | 公众号 OAuth 授权。url 是授权链接，会跳转并回填授权态。 | `url`, `appid` |
| `wpp_oa_qr_authorize` | 公众号二维码授权请求 (获取授权二维码) | `url` |
| `wpp_oa_qr_authorize_confirm` | 公众号二维码授权确认 (url 是确认链接) | `url` |
| `wpp_oa_article_read` | 解析公众号文章链接 (短链转正文 Markdown + 图片) | `url` |
| `wpp_oa_mp_get_a8key` | 获取公众号文章 key 和 uin. | `url` |

## 微信搜索

综合搜索与分频道搜索：公众号、文章、朋友圈、小程序、视频号、图片、新闻、百科、书籍、表情、直播。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_search_all` | 微信综合搜索（文章/公众号/小程序一起返回）。不知道归哪类时用它。 | `query` |
| `wpp_search_official_accounts` | 搜索公众号账号，返回账号名/微信号/简介。要搜文章正文用 wpp_search_articles（账号 ≠ 文章）。 | `query` |
| `wpp_search_moments` | 按关键词搜朋友圈动态（命中好友发过的内容）。只看某个好友的时间线请用朋友圈列表工具。 | `query` |
| `wpp_search_mini_programs` | 搜索小程序，返回名称/AppID。调用其它小程序能力前先用它拿 AppID。 | `query` |
| `wpp_search_articles` | 搜索公众号文章，返回标题/摘要/链接。按关键词找素材或核实信息时用它。 | `query` |
| `wpp_search_channels` | 搜视频号内容（query，cursor/limit 翻页）。要详情用 wpp_search_channels_detail。 | `query` |
| `wpp_search_images` | 搜图片（cursor/limit 翻页）。 | `query` |
| `wpp_search_news` | 搜新闻，时效性内容优先。 | `query` |
| `wpp_search_baike` | 搜百科词条，适合查概念/名词解释。 | `query` |
| `wpp_search_books` | 搜微信读书里的书，返回书名/作者/评分。找书用这个（不是商品库）。 | `query` |
| `wpp_search_emoji` | 搜微信表情包，支持 cursor 分页。选好表情后用发送表情的能力发出去。 | `query` |
| `wpp_search_ai` | AI 搜索（深度问答）。返回 session_id，追问用 wpp_search_ai_followup。 | `query` |
| `wpp_search_gateway` | 旧版搜一搜网页网关，仅兼容用；优先用分类搜索工具。 | `query` |
| `wpp_search_query` | 通用分类搜索。category 留空=全部，cursor 翻页。 | `query` |
| `wpp_search_channels_detail` | 获取视频号内容详情. contentToken 来自视频号搜索结果 | `content_token` |
| `wpp_search_channels_comments` | 获取视频号评论. commentToken 来自搜索结果, cursor 翻页, rootCommentId 看一级评论的回复 | `comment_token` |
| `wpp_search_channels_resolve` | 解析视频号分享链接 (weixin.qq.com/sph/... 分享链接), 返回可直接使用的业务字段 | `url` |
| `wpp_search_ai_followup` | AI 搜索追问 (用首问返回的 sessionId 继续对话) | `session_id`, `query` |
| `wpp_search_wechat_index` | 微信指数搜索（查询关键词的微信指数相关结果，营销分析用）。 | `query` |
| `wpp_search_live` | 直播搜索（返回主播、标题和直播状态）。 | `query` |
| `wpp_search_capabilities` | 查看通用搜索支持的分类. | — |
| `wpp_search_channels_media` | 获取视频号媒体流. media_token 来自搜索结果, download=1 下载 / 0 预览播放. | `media_token` |
| `wpp_search_ai_conversation` | 获取 AI 搜索会话（问答轮次/Markdown 答案/参考资料/建议追问）. | `session_id` |

## 小程序

获取 openid、云函数调用、登录态、授权管理、支付、插件校验。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_wxapp_openid` | 查询小程序用户的 openId. toWxId=目标用户 wxid/username. | `toWxId`, `appid` |
| `wpp_wxapp_record` | 获取小程序记录. | — |
| `wpp_wxapp_cloud_call` | 小程序云函数调用（云开发）. data=云函数请求 JSON 字符串（函数名与参数都在里面）; 厂商契约只有 {appid,data}. | `appid`, `data` |
| `wpp_wxapp_js_session` | 获取小程序 sessionid | `appId` |
| `wpp_wxapp_oauth_list` | 获取小程序授权管理列表 | — |
| `wpp_wxapp_unionpay` | 云闪付支付 | `appid`, `sessionid`, `timeStamp`, `nonceStr`, `package`, `paySign` |
| `wpp_wxapp_js_login` | 授权小程序 (定制) | `appId` |
| `wpp_wxapp_js_operate_data` | 小程序 JSAPI 通用通道. data 传 JSAPI 请求 JSON 字符串, 形如 {"api_name":"webapi_getwxaasyncsecinfo","data":{},"opt":1}; opt:… | `appId`, `data` |
| `wpp_wxapp_verify_plugin` | 小程序获取 HostSign | `appId` |
| `wpp_wxapp_pull_pay` | 推送小程序支付请求 | `appid`, `sessionid`, `timeStamp`, `nonceStr`, `package`, `paySign` |
| `wpp_wxapp_delete_oauth` | 移除小程序授权. appid=小程序 appid | `appid` |
| `wpp_wxapp_js_login_customized` | 小程序定制登录. appid=小程序 appid | `appid` |

## 支付与红包

收款、红包创建/查询/领取、转账、付款码与状态查询。资金类工具默认受管理员门禁约束。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_tenpay_collect_money` | 确认收款. 推荐传 contextId；否则传 toUserName（付款方微信标识）。 | — |
| `wpp_tenpay_open_hongbao` | 抢红包. 推荐传 contextId（红包消息中的上下文标识）；否则传 Xml + SendUserName。 | — |
| `wpp_tenpay_query_hongbao` | 查看红包详情. Xml=红包消息内容。 | `Xml` |
| `wpp_tenpay_receive_hongbao` | 接收红包（无 key 流程，vendor 自动）. Xml=红包消息内容。 | `Xml` |
| `wpp_tenpay_ge_ma_skd_qcode` | 生成商家个人收款单/收款码（指定金额、备注）。会真实产生收款单，涉及资金，先确认金额与对象。 | `Money`, `Name` |
| `wpp_tenpay_sj_skd_qcode` | 生成商家收款单（商户维度）。涉及资金，调用前确认金额与备注。 | `Money`, `Name` |
| `wpp_tenpay_open_redpacket` | 拆开已领取的红包. Xml=红包消息内容, SendUserName=红包发送人, TimingIdentifier=领取结果返回的时序标识。 | `Xml`, `SendUserName`, `TimingIdentifier` |
| `wpp_tenpay_get_encrypt_info` | 解密红包/支付的加密信息，取得后续拆包/收款所需参数。 | `info` |
| `wpp_tenpay_confirm_pre_transfer` | 确认支付（完成 createPreTransfer 的预订单）. bankSerial/bankType/payPassword/reqKey 缺一不可，全部来自预支付响应与银行卡列表。 | `bankSerial`, `bankType`, `payPassword`, `reqKey` |
| `wpp_tenpay_generate_pay_qcode` | 生成自定义金额的收款二维码。 | `money`, `name` |
| `wpp_tenpay_redpacket_list` | 查看红包领取列表. xml 来自 inbound 红包事件。 | `xml` |
| `wpp_tenpay_open_hongbao_params` | 抢红包 (完整参数). SendId=红包ID, SendUserName=发送者, TimingIdentifier=定时标识, Xml=红包消息 | `SendId` |
| `wpp_tenpay_receive_no_encryption` | 打开红包的无加密兼容模式。Xml 是红包消息原文。 | `Xml` |
| `wpp_tenpay_create_pre_transfer` | 创建转账预订单（只创建不扣款）. fee 单位是分（100=1.00元）. 成功后用返回的 req_key 调 wpp_tenpay_confirm_pre_transfer 完成支付. | `toUserName`, `fee` |
| `wpp_tenpay_pay_qcode_status` | 查询收款二维码的支付状态（轮询是否真到账）. 只有 status=paid 且 paid=true 才算已付款; 503/PAYMENT_STATUS_UNAVAILABLE 时必须稍后重试, 不得判为失败. | — |
| `wpp_tenpay_create_redpacket` | 创建红包（微信红包）. 接收人是群 ID 或单人 wxid. | `amount`, `content`, `count`, `username` |

## 语音与媒体文件

语音转写（按 base64 或消息 ID）、媒体上传下载、CDN 下载、视频号/小程序封面下载。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_voice_transcribe` | 语音转文字（传 base64 音频）。已拿到音频字节时用。 | `audio_base64` |
| `wpp_voice_message_transcribe` | 按消息 ID 转写语音。消息已在库里时优先用这个，省得自己下载。 | `msg_id` |
| `wpp_voice_result` | 取异步语音转写结果，voice_id 来自 transcribe 调用。 | `voice_id` |
| `wpp_tool_download_img` | 下载图片（分片）. toWxid=会话ID, msgId=消息ID, dataLen=图片字节数. | `toWxid`, `msgId`, `dataLen` |
| `wpp_tool_download_video` | 下载视频（分片）. toWxid=会话ID, msgId=消息ID, dataLen=视频字节数. | `toWxid`, `msgId`, `dataLen` |
| `wpp_tool_download_voice` | 下载语音. fromUserName=发送人, msgId=消息ID, length=语音字节数. | `fromUserName`, `msgId`, `length` |
| `wpp_tool_download_file` | 下载文件. attachId=文件标识, userName=发送人/群ID, dataLen=文件字节数. | `attachId`, `userName`, `dataLen` |
| `wpp_tool_cdn_download_image` | CDN 下载高清图片. fileAesKey/fileNo 来自 image.cdn_download_contexts. | `fileAesKey`, `fileNo` |
| `wpp_tool_upload_file` | 上传文件（base64 内容）. | `base64` |
| `wpp_tool_cdn_dns` | 取 CDN 服务器 DNS 信息，下载失败时排障用。 | — |
| `wpp_tool_download_file_binary` | 完整下载微信文件（二进制）. attachId/userName/dataLen 来自 file.download_context. | `attachId`, `userName`, `dataLen` |
| `wpp_tool_download_voice_binary` | 下载微信语音原文件（二进制）. msgId/newMsgId 来自语音消息. | `msgId`, `newMsgId` |
| `wpp_tool_download_mini_program_cover` | 下载小程序卡片封面图. 传 url, 或 url 为空时传 fileNo+fileAesKey. 返回 Data.Image (base64 图片). | — |
| `wpp_upload_image` | 上传图片拿 imgUrl. 上传后用 sendCDNImage 转发. | `imgBase64`, `toWxid` |
| `wpp_tool_get_a8key` | 公众号 A8 Key（打开文章用）. | `url` |
| `wpp_tool_generate_pay_qrcode` | 生成支付二维码（无参 GET）。 | — |
| `wpp_tool_band_card_list` | 取当前账号绑定的银行卡与余额信息。 | — |
| `wpp_tool_bound_hard_devices` | 取绑定的硬件设备列表。 | — |
| `wpp_tool_set_step_count` | 改当天微信运动步数（上限 98000）。会写真实账号数据。 | `step` |
| `wpp_tool_set_proxy` | 设置/删除代理IP（传空字符串恢复直连，格式 host:port）。 | `proxy` |

## 账号、登录与资料

登录二维码、心跳、连接状态、退出、个人资料、头像、隐私、密码、绑定手机/QQ/邮箱、设备管理。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_login_get_qr` | 获取登录二维码. deviceType 默认 ipad. | — |
| `wpp_login_check_qr` | 检测二维码状态（uuid）。 | `uuid` |
| `wpp_login_heartbeat` | 单次心跳。 | — |
| `wpp_login_heartbeat_long` | 长连接心跳包。 | — |
| `wpp_login_heartbeat_logs` | 获取心跳日志。 | — |
| `wpp_login_auto_heartbeat` | 开启自动心跳。 | — |
| `wpp_login_cache_info` | 获取本地登录缓存。 | — |
| `wpp_login_longlink_status` | 查询长连接状态。 | — |
| `wpp_login_logout` | 退出登录。 | — |
| `wpp_login_newinit` | 初始化登录。 | — |
| `wpp_login_get_status` | 查询登录状态（GET）. autoLogin=true 时返回自动登录信息. | — |
| `wpp_login_check_can_set_alias` | 检测能否设置微信号。 | — |
| `wpp_login_submit_verify_code` | 提交扫码登录验证码。 | `code` |
| `wpp_login_qr_pad_cloud` | 获取 iPad 云登录二维码. deviceName 默认「我的 iPad」. | — |
| `wpp_login_qr_pad_ppmt` | 获取 iPad PPMT 登录二维码. deviceName 默认「我的 iPad」. | — |
| `wpp_login_62data_qr_verify` | 62 数据二维码验证会话. Url=验证链接. | `Url` |
| `wpp_user_my_profile` | 获取联系人资料（wxid 留空=自己）. | — |
| `wpp_user_online_info` | 获取账号在线状态信息。 | — |
| `wpp_user_qrcode` | 取个人二维码 | — |
| `wpp_user_set_alias` | 设置微信号（别名）. | `alias` |
| `wpp_user_update_profile` | 修改个人资料（昵称/签名/性别）. | — |
| `wpp_user_upload_headimage` | 上传头像图片. | `imgBase64` |
| `wpp_user_safety_info` | 登录设备管理 (列出已登录设备) | — |
| `wpp_user_del_device` | 删除登录设备 | `uuid` |
| `wpp_user_set_privacy` | 隐私设置. opt=功能项, value=值（均按厂商文档）. | `opt`, `value` |
| `wpp_user_change_password` | 修改微信密码. ticket 由风控验证流程返回. | `newPwd`, `ticket` |
| `wpp_user_verify_password` | 验证当前密码 (用于敏感操作前) | `password` |
| `wpp_user_report_motion` | 上报微信运动步数. | `steps` |
| `wpp_user_bind_mobile` | 绑定手机号. code=短信验证码. | `mobile`, `code` |
| `wpp_user_send_verify_code` | 发送手机验证码 | `mobile` |
| `wpp_user_bind_qq` | 绑定 QQ 号. | `qq`, `password` |
| `wpp_user_bind_email` | 绑定邮箱 | `email` |
| `wpp_user_friend_verification` | 设置「加我为朋友时需要验证」. enabled=true 需验证, false 关闭 | `enabled` |
| `wpp_user_add_me_methods` | 设置「添加我的方式」开关（只传要改的项）. | — |
| `wpp_user_can_set_alias` | 检测当前是否可以设置微信号（GET）. | — |
| `wpp_accounts_context` | 查询所有微信账号的上下文元信息（账号ID/绑定agent/selfWxid/连接状态/白名单），不含authcode。用于多账号场景下跨账号路由前探查 | — |
| `wpp_customized_auth_batch` | 定制化批量鉴权（统一）。 | `Username` |

## 收藏

同步、查看、删除微信收藏。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_fav_sync` | 同步微信收藏（拉取最新收藏列表）。找收藏内容前先 sync。 | — |
| `wpp_fav_info` | 查单条收藏的元信息（标题/类型/来源）。 | — |
| `wpp_fav_item` | 取收藏条目的完整内容。 | `favId` |
| `wpp_fav_del` | 删除收藏。不可恢复。 | `favId` |

## 企业微信联系人

搜索、添加企业微信联系人并发送申请。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_qw_search` | 搜索企业微信联系人。 | `username` |
| `wpp_qw_add` | 添加企业微信联系人（username + v1）。 | `username` |
| `wpp_qw_apply` | 申请添加企微联系人，可带上下文说明（context）。 | `username` |

## 小微智能体

小微会话、事件流、历史、红点、邀请、建议、消息发送与重生成。受账号级开关控制。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_xw_events` | 读取小微会话 SSE 事件流（最多等 10 秒）. after_sequence=断线续传用. | `session_id` |
| `wpp_xw_invite` | 邀请进入小微会话。 | `wxids` |
| `wpp_xw_history_list` | 获取小微历史列表。 | — |
| `wpp_xw_reddots` | 查询小微红点。 | — |
| `wpp_xw_create_session` | 小微创建会话。 | — |
| `wpp_xw_history_fill` | 小微填充历史。 | `items` |
| `wpp_xw_history_delete` | 小微删除历史。 | `delete_item_lists` |
| `wpp_xw_reddots_read` | 小微红点已读。 | `reddot_id` |
| `wpp_xw_card_users` | 小微卡片用户。 | `card_type` |
| `wpp_xw_card_screenshot_check` | 小微卡片截图安全校验。 | `message_id`, `app_id` |
| `wpp_xw_suggestions` | 小微会话建议。 | — |
| `wpp_xw_get_session` | 获取小微会话状态（当前消息/房间/最后事件序号）. | `session_id` |
| `wpp_xw_send_message` | 向小微会话发送消息（text 必填; 回答经 Events 事件流返回）. | `session_id`, `text` |
| `wpp_xw_cancel` | 取消小微当前回答（事件流返回 cancelled, 会话恢复 ready）. | `session_id` |
| `wpp_xw_regenerate` | 重新生成小微回答. message_id=要重生成的回答消息 ID. | `session_id`, `message_id` |
| `wpp_xw_switch_room` | 切换小微会话房间（会话 ready 时）. | `session_id`, `room_id` |
| `wpp_xw_invite_candidates` | 可邀请使用小微的好友列表. | — |
| `wpp_xw_invite_info` | 查询小微邀请额度. | — |
| `wpp_xw_permission` | 查询账号是否已开通小微（GET）. | — |
| `wpp_xw_a2a_list` | 多智能体对话列表. | — |

## MCP 兼容工具

给支持 MCP 的调用方使用的精简只读/互动工具集；需账号显式设置 `mcpEnabled: true`。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_mcp_account_status` | 查询当前微信账号状态（在线/设备模式/昵称）. 无需参数. | — |
| `wpp_mcp_list_contacts` | 分页列出微信联系人. 用 next_offset 翻页. | — |
| `wpp_mcp_list_groups` | 分页列出微信群. 用 next_offset 翻页. | — |
| `wpp_mcp_recent_messages` | 查询最近 24 小时收到的消息（最多 500 条）. | — |
| `wpp_mcp_get_contact` | 查询单个微信联系人详情（需 list/search 返回的 id）. | `id` |
| `wpp_mcp_get_group` | 查询单个微信群详情（id, 群名, 群主, 成员数, 公告）. | `id` |
| `wpp_mcp_search` | 搜索微信（公众号/文章/小程序/视频号/朋友圈等）. category 选类别, query 关键词. | `query`, `category` |
| `wpp_mcp_translate` | 翻译文本（自动检测源语言，不发送消息）。target_lang 如 en/zh/ja/fr. | `text`, `target_lang` |
| `wpp_mcp_group_members` | 分页列出群成员（安全 DTO）。member_id 可用于 wpp_mcp_send_pat。 | `group_id` |
| `wpp_mcp_send_pat` | 在群里拍一拍某个成员（互动用；member_id 先用 wpp_mcp_group_members 取）。 | `group_id`, `member_id` |

## Webhook 管理

查询/设置/删除入站回调，配置业务回调并发送测试。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_webhook_set` | 设置回调地址。⚠️ 改错会直接收不到消息；调试期请配合出站白名单。 | `url` |
| `wpp_webhook_get` | 读当前回调配置，确认 vendor 侧指向哪里。 | — |
| `wpp_webhook_remove` | 删除回调配置。删后 vendor 不再推消息，等于平台静默掉线。 | — |
| `wpp_webhook_business_set` | 设置业务回调（登录/掉线等事件）。 | `url` |
| `wpp_webhook_test` | 测试发送 Webhook 消息（按授权码，验证回调配置是否生效）。 | — |
| `wpp_webhook_business_get` | 获取业务回调 URL. | — |

## 分享与集成

视频消息分享、自动同步启动、排行榜点赞等零散集成能力。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_share_video_msg` | 发送分享视频消息。Xml 是分享卡片原文，来自转发事件。 | `ToWxid`, `Xml` |
| `wpp_start_auto_sync` | 启动自动同步，让 vendor 把消息推到 TargetURL。⚠️ 改错会把消息流引到外部地址。 | — |
| `wpp_other_rank_like` | 获取微信运动排行的点赞统计（排行、点赞用户、展示信息）. rankId 留空查最新榜单. | — |

## 插件自身

查看全部工具定义、检查插件配置状态与获取配置引导。

| 工具 | 说明 | 必填参数 |
|---|---|---|
| `wpp_tools_list` | 列出全部 wpp 工具定义（本地查询，不调用 vendor）。可按 domain 或 keyword 过滤，返回 endpoint/method/params。 | — |
| `wpp_setup` | 检查 WeChatPadPro 插件配置状态并给出安装/配置引导。不读取、不回传 authcode/tokenKey 等密钥。 | — |
