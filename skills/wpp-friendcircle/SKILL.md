---
name: wpp-friendcircle
description: "微信朋友圈发布。wechatpadpro 插件暴露朋友圈能力，本技能封装「文字/图文/视频」3 条发布路径 + 发布铁律（图片 1-9 张、视频需封面、发布前必须确认、内容脱敏）。用于「发个朋友圈」「发张图到朋友圈」「发视频朋友圈」。⚠️ 发布是对外公开操作，必须老板确认后才执行。"
---

# wpp-friendcircle — 朋友圈发布

## 适用 agent

| Agent | 是否适用 | 原因 |
|-------|---------|------|
| main | ✅ | 适用 |
| wpp-wechat | ✅ | 适用 |
| wecom | ❌ | 不适用 (wpp-friendcircle 与该 agent 职责不匹配) |

> **Note**: 朋友圈发布 — main (老板朋友圈) + wpp-wechat (微信主通道)


> 朋友圈是**对外公开**操作，发布后所有好友可见。**发布前必须确认**。本技能封装 3 条发布路径，AI 按用户意图选择。

## 3 条发布路径

| 场景 | 使用工具 | 说明 |
|---|---|---|
| **文字** | `wpp_fc_publish_text` | 直接发纯文字 |
| **图文** | `wpp_fc_publish_images` | **一条龙**：传图(1-9张) + 发布 |
| **视频** | `wpp_fc_publish_video` | **一条龙**：传视频 + 封面 + 发布 |

> ✅ **优先用复合工具**（wpp_fc_publish_images / wpp_fc_publish_video）：内部自动完成上传→发布，一次搞定。
> ❌ 不要拆开用 `wpp_fc_upload_image` / `wpp_fc_upload_video`（那是单步上传，返回 publishItem 还需手动发布，易出错）。
> ⚠️ Hermes 命名：OpenClaw 旧名 publishFriendCircle/publishImageCircle/publishVideoCircle 已废弃。

## 发布铁律（违反即 bug）

1. **发布前必须确认**：展示内容预览 → 明确问「确认发布这条朋友圈吗？」→ 老板确认才发
2. **图片 1-9 张**：0 张 / 10 张都拒绝
3. **视频需封面缩略图**：发视频朋友圈必须有 thumbData（封面）
4. **内容脱敏**：不发内部数据 / 毛利 / 销售额（引用 phoneerp 脱敏规则）；不发未确认的敏感内容
5. **不自动发布**：除非老板明确说"发"，否则只准备预览
6. **视频朋友圈 vendor 缺陷**：经 API 发的视频会拉成竖屏（vendor UploadVideo 不转码，2026-08-11 发现）——发视频前提醒老板此限制，或建议手机发

## 工具签名

### wpp_fc_publish_text（文字）
```javascript
wpp_fc_publish_text({ title: "朋友圈文字内容" })
```

### wpp_fc_publish_images（图文，推荐）
```javascript
wpp_fc_publish_images({
  title: "朋友圈文字",
  imageBase64List: ["<图片base64>", "..."]  // 1-9 张
})
```
> 图片 base64 来源：本地文件读 / 网络图下载 / 用户发来的图

### wpp_fc_publish_video（视频）
```javascript
wpp_fc_publish_video({
  title: "朋友圈文字",
  videoBase64: "<视频base64>",
  thumbBase64: "<封面缩略图base64>"
})
```

## 工作流

### 「发个朋友圈：X」
```
1. 确认内容 + 是否配图/视频
2. 准备媒体（读图/视频 base64）
3. 展示预览给老板
4. 老板确认 → wpp_fc_publish_images / wpp_fc_publish_video / wpp_fc_publish_text
5. 报告结果（成功/失败）
```

### 「把这张图发到朋友圈」
```
1. 拿到图片 → base64
2. 确认配文（老板给定或建议）
3. 预览确认 → wpp_fc_publish_images({title, imageBase64List:[base64]})
```

### 「发个视频朋友圈」
```
1. 拿视频 + 生成封面缩略图（ffmpeg 抽帧）
2. 预览确认 → wpp_fc_publish_video({title, videoBase64, thumbBase64})
3. ⚠️ 提醒：视频可能拉成竖屏（vendor 缺陷），建议手机发
```

## 点赞 / 评论

> **朋友圈点赞/评论正确接口是 `/FriendCircle/Comment`，对应工具 `wpp_fc_comment`。**
> ⚠️ **严禁用 `wpp_finder_like` / `wpp_finder_comment` / 任何 `Finder` 相关工具点赞朋友圈** —— 那是视频号(视频动态)的点赞工具，不是朋友圈，接口完全不同（2026-08-11 老板实测发现 AI 用错）。

### wpp_fc_comment（点赞 / 评论，正确）
```javascript
wpp_fc_comment({
  id: "<朋友圈动态ID>",   // 注意 Hermes 参数名是 id（不是 snsId）
  content: "👍",          // 点赞传 👍 表情；评论传文字
  type: 1                 // 1=点赞，2=文本评论（vendor API 语义）
})
```
> `id` 来源：`wpp_fc_by_user({towxid})` 或 `wpp_fc_list` 返回的 `Id`。
> 注意：`type` 的 vendor 语义是 **1=点赞、2=文本**（不是"1=文字 2=表情"）。

## 相关
- 插件工具：`wpp_fc_publish_text` / `wpp_fc_publish_images` / `wpp_fc_publish_video`（复合发布，带权限校验）
- 查看：`wpp_fc_list`（首页）/ `wpp_fc_by_user`（某人）/ `wpp_fc_by_snsid`（详情）
- 互动：`wpp_fc_comment`（点赞/评论，走 `/FriendCircle/Comment`）/ `wpp_fc_operate`（删除/设顶）
