---
name: wpp-friendcircle-stats
description: "朋友圈数据统计分析。拉取好友/自己朋友圈列表，统计发布频率、内容类型(文字/图文/视频)、图片数、时间分布、活跃周期。用于「谁最近发朋友圈多」「某客户活跃度」「竞品门店最近发什么」「统计我的朋友圈发布情况」。"
---

# wpp-friendcircle-stats — 朋友圈数据统计

> 从朋友圈列表数据统计**发布行为**，用于客户活跃度 / 竞品分析 / 自我复盘。数据来自 `getFriendCircleList` / `getFriendCircleByUser` 返回的 ObjectList。

## 数据字段（可统计）

每条朋友圈 ObjectDesc 含：
- `Id` / `CreateTime`（发布时间，秒）— 时间分布
- `contentDesc`（文字内容）— 关键词/话题
- `ContentObject.contentStyle`：`1`=图文 `15`=视频 — 内容类型
- `mediaList` 的 media 数量 — 图片数（多图）
- `Username` / `Nickname` — 发布者

## 统计维度

| 维度 | 怎么算 |
|---|---|
| 发布频率 | 周期内条数 / 天数 |
| 内容类型 | 按 contentStyle 分组（1 图文 / 15 视频 / 纯文字）|
| 图片数 | mediaList 里 media 数量分布（1/3/9 张）|
| 时间分布 | 按 CreateTime 小时/星期分组 |
| 活跃周期 | 相邻发布间隔中位数/最长间隔 |
| 关键词 | contentDesc 高频词 |

## 工具（先拉数据）

```javascript
// 首页
wpp_fc_list({})  // 或 { fristpagemd5: "翻页" }（注意 vendor typo）
// 某人
wpp_fc_by_user({ towxid: "wxid_abc123" })
```

> 数据量：单次返回约 10 条（ObjectCount），**翻页**用 `fristpagemd5`（首页返回的 FirstPageMd5，vendor typo）。
> ⚠️ Hermes 命名：OpenClaw 旧名 getFriendCircleList/getFriendCircleByUser 已废弃；参数名对齐 vendor（towxid/fristpagemd5）。

## 工作流

### 「统计某人的朋友圈活跃度」
```
1. wpp_fc_by_user({towxid}) → 拿 ObjectList
2. 解析每条: CreateTime/contentStyle/media数/contentDesc
3. 统计: 近30天发布 N 条 (均 X 条/周), 图文为主(70%)视频(20%), 常在晚上 20-22 点发
4. 报告: 客户/竞品活跃度
```

### 「最近谁朋友圈发得多」
```
1. 遍历联系人（或从已知活跃用户开始）
2. 逐个 wpp_fc_by_user → 统计条数/频率
3. 排序 → 输出 TOP N
```

### 「我自己的朋友圈发布复盘」
```
1. wpp_fc_list({}) 翻页拉自己的
2. 统计: 本月发布 N 条 / 图文 vs 视频比例 / 最佳发布时间
3. 报告
```

## 注意
- **翻页**：一次只回 ~10 条，统计大周期要翻页（fristpagemd5 递归）
- **脱敏**：统计内容含毛利/销售额等敏感字段时，不展示（引用脱敏规则）
- **好友朋友圈**：可能因对方设置不可见而拿不到（wpp_fc_by_user 返回空）
- **时间**：CreateTime 是秒级 Unix，转可读 `new Date(ts*1000)`

## 相关
- wpp-friendcircle：发布 + 查看
- wpp-history：消息历史（可配合分析互动）
