---
name: wpp-friendcircle-view
description: "查看好友朋友圈并总结。拉取某人朋友圈列表，逐条理解内容(文字/图片/视频)，按时间线汇总要点、动态、趋势。用于「看看某某最近发了什么」「某客户朋友圈最近动态」「帮我总结下张三的朋友圈」。"
---

# wpp-friendcircle-view — 好友朋友圈查看 + 总结

> 查看某好友/客户的朋友圈并**总结要点**（不是列原始记录）。用于了解客户近况、竞品动态、重要联系人状态。

## 核心工具

```javascript
// 某人的朋友圈 (返回 ObjectList 按时间倒序)
wpp_fc_by_user({ towxid: "wxid_abc123" })
// 某条详情 (看图/视频完整内容)
wpp_fc_by_snsid({ id: "12345" })
// 首页 (自己的/通用)
wpp_fc_list({})
```
> ⚠️ Hermes 命名：OpenClaw 旧名 getFriendCircleByUser/getFriendCircleBySnsId/getFriendCircleList 已废弃；参数名也改了对齐 vendor（towxid/id）。

## 总结方法

### 逐条理解
每条朋友圈 ObjectDesc 解析：
- `contentDesc` — 文字内容（关键信息）
- `ContentObject.contentStyle` — `1`=图文 `15`=视频
- `mediaList` — 图片/视频（用 `mmx vision describe` 理解图片内容）
- `CreateTime` — 发布时间（判断时效/频率）

### 总结框架（按时间线）
```
📅 最近动态 (近 N 条)
1. [日期] 内容要点 (图/视频/文字)
2. ...
📊 趋势分析
- 发布频率: 近X天 N 条
- 内容方向: 主发 XX (业务/生活/促销)
- 变化: 近段时间是否活跃/发新品/做活动
```

## 工作流

### 「看看张三最近发了什么」
```
1. 确认张三 wxid (wpp-identity 匹配)
2. wpp_fc_by_user({towxid}) → ObjectList
3. 逐条解析: 文字内容 + 图片(mmx 看图) + 视频
4. 总结: 时间线 + 趋势
```

### 「某客户朋友圈最近动态」
```
1. wpp-identity 查客户 wxid
2. 拉朋友圈 → 总结近况
3. 输出: 客户最近在忙什么 / 发什么产品 / 活跃度
```

### 「总结我的朋友圈发布情况」
```
1. wpp_fc_list({}) 拉自己的
2. 总结: 发了什么 / 反馈如何
```

## 注意
- **图片理解**：用 mmx vision 看图描述，别只看文件名
- **视频**：朋友圈视频 URL 7 天过期（wpp-history 提醒），播放需及时
- **隐私**：总结展示给老板本人，不外发
- **好友不可见**：对方设置仅三天/不可见 → 返回空，说明即可

## 相关
- wpp-identity：wxid↔昵称匹配
- wpp-friendcircle-stats：量化统计分析
- wpp-friendcircle：发布
