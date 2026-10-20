"""心流图片配文（v1.16.0）—— TS ``src/inbound/heartflow-caption.ts`` 的 pytest 译本。

## 这一批最容易"假绿"的五处，各配一条行为级回归门

1. **配文是增强件，不是依赖件**：caption 整个挂掉（DB 炸 / LLM 炸 / 端点不支持 vision）
   ⇒ 画像照旧用旧样本形态生成。这条由 :func:`test_caption_pass_failure_never_blocks_profile`
   真跑 ``maybe_generate_hf_group_profiles`` 钉住。
2. **anthropic 端点必须挡在调用前**：它丢弃 images，模型会对着「描述这张图片」凭空胡说。
   只在 openai 格式端点才发配文请求。
3. **失败要出声**：只落 ``fail_count`` 会有「图一直没配文却查不出为什么」的静默盲区。
4. **同图跨群只配一次**：幂等键是 ``sha1(URL)``，跨群共享 → 主键 ``url_hash``。
5. **样本里不许再出现图片 URL**：有配文换成 ``[图] <配文>``，无配文换成 ``收到一张图片``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import pytest

from plugin.data import repo_hf, repo_messages
from plugin.inbound.heartflow import caption as C
from plugin.inbound.heartflow import profile as P

IMG = "https://oss.example.com/a.jpg"
IMG2 = "https://oss.example.com/b.jpg"

GOOD_PROFILE = json.dumps({
    "nature": "手机零售商客户售后群",
    "style": "短句、口语",
    "bot_role": "答疑的客服",
    "summary": "群里问售后就答",
    "engage": ["晒单", "问价"],
    "avoid": ["广告"],
}, ensure_ascii=False)

NOW = 1_800_000_000


def img_msg(group_id: str = "g1@chatroom", url: str = IMG, n: int = 1) -> dict:
    """给配文队列源的行 —— 形状与 ``repo_hf.list_hf_image_msgs`` 的 SELECT 逐列一致。"""
    return {"id": n, "group_id": group_id, "content": f"收到一张图片\n[图片] {url}"}


def creds(fmt: str = "openai"):
    from plugin.llm.judge import JudgeCreds

    return JudgeCreds(api_key="k", base_url="https://judge.example/v1", format=fmt)


# ===== 1. 参数 =====


def test_resolve_cfg_defaults_match_ts():
    c = C.resolve_hf_caption_cfg(None)
    assert (c.enabled, c.per_group_max, c.max_fails, c.scan_limit, c.timeout_ms) == \
        (True, 6, 2, 150, 8000)


def test_resolve_cfg_clamps_dirty_values():
    """脏配置一律回落缺省 / 夹紧 —— 不许 ``NaN`` 或负数流下去（本仓一贯口径）。"""
    c = C.resolve_hf_caption_cfg({
        "enabled": False, "perGroupMax": -3, "maxFails": 0,
        "scanLimit": 10_000, "timeoutMs": 10,
    })
    assert c.enabled is False
    assert c.per_group_max == 0, "perGroupMax 负数 ⇒ 夹到 0（= 关闭补配文）"
    assert c.max_fails == 1, "maxFails 至少 1（0 会让失败行永远可重试）"
    assert c.scan_limit == 500, "scanLimit 上限 500"
    assert c.timeout_ms == 1000, "timeoutMs 下限 1000"

    dirty = C.resolve_hf_caption_cfg({
        "perGroupMax": "abc", "maxFails": True, "scanLimit": float("nan"),
        "timeoutMs": None, "enabled": "false",
    })
    assert (dirty.per_group_max, dirty.max_fails, dirty.scan_limit, dirty.timeout_ms) == \
        (6, 2, 150, 8000), "脏值回落缺省（bool 不算数、NaN 不算数）"
    assert dirty.enabled is True, "enabled 只认真/假语义的缺省（'false' 与 TS 一致按真处理）"


def test_profile_cfg_carries_caption_subconfig():
    """``profile.caption`` 子映射必须进 ``HfProfileConfig``（生成路径要用它）。"""
    d = P.resolve_hf_profile_cfg(None)
    assert d.caption == C.resolve_hf_caption_cfg(None)
    o = P.resolve_hf_profile_cfg({"profile": {"caption": {"perGroupMax": 60, "enabled": False}}})
    assert o.caption.per_group_max == 60 and o.caption.enabled is False


# ===== 2. 纯函数 =====


def test_hash_is_sha1_of_url():
    assert C.hf_caption_hash(IMG) == hashlib.sha1(IMG.encode()).hexdigest()
    assert len(C.hf_caption_hash("x")) == 40


def test_the_same_url_under_two_accounts_gets_different_keys():
    """表 PK 只有 ``url_hash`` ⇒ 账号必须编进 key，否则另一账号永远读不到自己那行。"""
    a = C.hf_caption_hash(IMG, "acc1")
    b = C.hf_caption_hash(IMG, "acc2")
    assert a != b and len(a) == len(b) == 40
    # 长度前缀防拼接歧义
    assert C.hf_caption_hash("bc", "a") != C.hf_caption_hash("c", "ab")


def test_select_tasks_skips_captioned_and_exhausted():
    msgs = [img_msg(url=IMG), img_msg(url=IMG2)]
    existing = [
        {"url_hash": C.hf_caption_hash(IMG), "caption": "已配好", "fail_count": 0},
        {"url_hash": C.hf_caption_hash(IMG2), "caption": None, "fail_count": 2},
    ]
    assert C.select_hf_caption_tasks(
        msgs, existing, max_tasks=10, max_fails=2) == [], "已配文 / 试死 ⇒ 都跳过"

    existing[1]["fail_count"] = 1
    tasks = C.select_hf_caption_tasks(msgs, existing, max_tasks=10, max_fails=2)
    assert [(t.url, t.url_hash) for t in tasks] == \
        [(IMG2, C.hf_caption_hash(IMG2))], "未到上限的失败行可重试"


def test_select_tasks_dedupes_same_url_across_messages_and_caps():
    msgs = [img_msg(url=IMG, n=1), img_msg(url=IMG, n=2), img_msg(url=IMG2, n=3)]
    tasks = C.select_hf_caption_tasks(msgs, [], max_tasks=1, max_fails=2)
    assert len(tasks) == 1 and tasks[0].url == IMG, "同 URL 只出一个任务 + maxTasks 截断"
    assert tasks[0].group_id == "g1@chatroom"


def test_clean_caption_collapses_whitespace_and_caps_60():
    assert C.clean_hf_caption("  晒单\n 手机  1999元 ") == "晒单 手机 1999元"
    assert len(C.clean_hf_caption("长" * 100)) == 60
    assert C.clean_hf_caption(None) == ""


def test_sample_line_uses_caption_or_placeholder_and_drops_url():
    caps = {C.hf_caption_hash(IMG): "晒单: 手机 1999 元"}
    assert C.hf_caption_sample_line(f"收到一张图片\n[图片] {IMG}", caps) == \
        "[图] 晒单: 手机 1999 元"
    assert C.hf_caption_sample_line(f"收到一张图片\n[图片] {IMG}", {}) == "收到一张图片"
    assert C.hf_caption_sample_line("纯文本消息", caps) == "纯文本消息"


# ===== 3. IO 编排 =====


class FakeCaptionIo:
    """只替换 IO（repo_hf 三条 + call_judge），编排逻辑真跑。"""

    def __init__(self, *, msgs=(), existing=(), caption_text="晒单: 手机 1999 元",
                 fail_urls=()):
        self.msgs = list(msgs)
        self.existing = list(existing)
        self.caption_text = caption_text
        self.fail_urls = set(fail_urls)
        self.calls: list[dict] = []
        self.upserts: list[dict] = []
        self.listed: list[tuple] = []
        self.queried: list[str] = []

    # ---- 数据层
    def list_hf_image_msgs(self, account_id, group_ids, since_sec, limit=150, conn=None):
        self.listed.append((account_id, list(group_ids), since_sec, limit))
        return [dict(m) for m in self.msgs]

    def get_hf_captions_by_hashes(self, account_id, hashes, conn=None):
        self.queried.extend(hashes)
        hs = set(hashes)
        return [dict(r) for r in self.existing if r["url_hash"] in hs]

    def upsert_hf_image_caption(self, account_id, url_hash, **kw):
        self.upserts.append(dict(account_id=account_id, url_hash=url_hash, **kw))
        return 1

    # ---- LLM
    async def call_judge(self, **kw):
        self.calls.append(kw)
        url = (kw.get("images") or [None])[0]
        if url in self.fail_urls:
            raise RuntimeError("OSS 403")
        return self.caption_text

    def install(self, monkeypatch):
        from plugin.llm import judge as judge_mod

        monkeypatch.setattr(repo_hf, "list_hf_image_msgs", self.list_hf_image_msgs)
        monkeypatch.setattr(repo_hf, "get_hf_captions_by_hashes",
                            self.get_hf_captions_by_hashes)
        monkeypatch.setattr(repo_hf, "upsert_hf_image_caption",
                            self.upsert_hf_image_caption)
        monkeypatch.setattr(judge_mod, "call_judge", self.call_judge)
        return self


def run_caption(io: FakeCaptionIo, *, cfg=None, cred=None):
    return asyncio.run(C.caption_pending_hf_images(
        "acc1", "g1@chatroom", NOW - 14 * 86400,
        cfg or C.resolve_hf_caption_cfg(None), "m1", cred or creds()))


def test_caption_pass_calls_vision_and_stores(monkeypatch):
    io = FakeCaptionIo(msgs=[img_msg()]).install(monkeypatch)
    r = run_caption(io)
    assert (r.tasks, r.ok, r.fail) == (1, 1, 0)
    assert io.listed == [("acc1", ["g1@chatroom"], NOW - 14 * 86400, 150)]
    call = io.calls[0]
    assert call["images"] == [IMG], "必须把图交给视觉端点（不是只发文字）"
    assert call["timeout_ms"] == 8000 and call["max_tokens"] == 120
    up = io.upserts[0]
    assert up["url_hash"] == C.hf_caption_hash(IMG, "acc1"), \
        "配文键必须带账号（否则另一账号的同 URL 会抢同一行）"
    assert up["caption"] == "晒单: 手机 1999 元" and up["fail_delta"] == 0
    assert up["url_sample"] == IMG and up["model"] == "m1"


def test_caption_pass_skips_anthropic_endpoint_entirely(monkeypatch):
    """anthropic 分支会丢弃 images ⇒ 模型凭空胡说，必须挡在调用前（v1.11.0 同款边界）。"""
    io = FakeCaptionIo(msgs=[img_msg()]).install(monkeypatch)
    r = run_caption(io, cred=creds("anthropic"))
    assert (r.tasks, r.ok, r.fail) == (0, 0, 0)
    assert io.calls == [] and io.upserts == [] and io.listed == [], "连 DB 都不查"


def test_caption_pass_disabled_or_zero_quota_makes_no_io(monkeypatch):
    io = FakeCaptionIo(msgs=[img_msg()]).install(monkeypatch)
    assert run_caption(io, cfg=C.resolve_hf_caption_cfg({"enabled": False})).tasks == 0
    assert run_caption(io, cfg=C.resolve_hf_caption_cfg({"perGroupMax": 0})).tasks == 0
    assert io.calls == [] and io.listed == []


def test_caption_failure_is_loud_and_records_fail_delta(monkeypatch, caplog):
    io = FakeCaptionIo(msgs=[img_msg()], fail_urls={IMG}).install(monkeypatch)
    with caplog.at_level(logging.WARNING):
        r = run_caption(io)
    assert (r.tasks, r.ok, r.fail) == (1, 0, 1)
    assert any("caption failed" in rec.message for rec in caplog.records), \
        "失败必须出声（只落 fail_count 会有静默盲区）"
    up = io.upserts[0]
    assert up["caption"] is None and up["fail_delta"] == 1


def test_already_captioned_image_is_not_recalled(monkeypatch):
    io = FakeCaptionIo(
        msgs=[img_msg()],
        existing=[{"url_hash": C.hf_caption_hash(IMG, "acc1"), "caption": "旧配文", "fail_count": 0}],
    ).install(monkeypatch)
    r = run_caption(io)
    assert r.tasks == 0 and io.calls == [] and io.upserts == []


def test_a_legacy_pure_url_row_still_counts_as_already_captioned(monkeypatch):
    """换账号隔离 hash 前的历史行（纯 URL sha1）必须继续命中，否则旧图会被重配一遍。"""
    io = FakeCaptionIo(
        msgs=[img_msg()],
        existing=[{"url_hash": C.hf_caption_hash(IMG), "caption": "旧配文", "fail_count": 0}],
    ).install(monkeypatch)
    r = run_caption(io)
    assert r.tasks == 0 and io.calls == [], "旧行命中 ⇒ 不重复调用视觉模型"
    legacy = C.hf_caption_hash(IMG)
    assert legacy in io.queried, "查询必须同时带上旧 hash"


def test_load_captions_maps_bodies_and_swallows_db_failure(monkeypatch):
    io = FakeCaptionIo(
        existing=[{"url_hash": C.hf_caption_hash(IMG, "acc1"), "caption": "晒单", "fail_count": 0}],
    )
    monkeypatch.setattr(repo_hf, "get_hf_captions_by_hashes", io.get_hf_captions_by_hashes)
    caps = C.load_hf_captions_for_bodies("acc1", [f"收到一张图片\n[图片] {IMG}", "纯文本"])
    assert caps == {C.hf_caption_hash(IMG, "acc1"): "晒单"}

    def boom(*a, **kw):
        raise RuntimeError("DB 炸了")

    monkeypatch.setattr(repo_hf, "get_hf_captions_by_hashes", boom)
    assert C.load_hf_captions_for_bodies("acc1", [f"[图片] {IMG}"]) == {}, \
        "查配文失败 ⇒ 空 Map ⇒ 样本退回旧形态（增强件不是依赖件）"


def test_load_captions_maps_a_legacy_row_onto_the_account_scoped_key(monkeypatch):
    """旧行读回来必须挂到**新** hash 上 —— 调用方（画像样本）只按新 hash 查。"""
    io = FakeCaptionIo(
        existing=[{"url_hash": C.hf_caption_hash(IMG), "caption": "旧晒单", "fail_count": 0}],
    )
    monkeypatch.setattr(repo_hf, "get_hf_captions_by_hashes", io.get_hf_captions_by_hashes)
    caps = C.load_hf_captions_for_bodies("acc1", [f"[图片] {IMG}"])
    assert caps == {C.hf_caption_hash(IMG, "acc1"): "旧晒单"}
    assert C.hf_caption_hash(IMG) in io.queried


# ===== 4. 与画像生成主路径的接线 =====


class FakeProfileIo(FakeCaptionIo):
    """画像生成 + 配文共用的假数据层（配文在生成**前**跑，样本要用到它）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.order: list[str] = []
        self.prompts: list[str] = []
        self.writes: list[dict] = []

    def list_profiles(self, account_id, conn=None):
        return []

    def get_hf_captions_by_hashes(self, account_id, hashes, conn=None):
        """配文 pass 刚写入的 **同一轮** 就要能被样本装配读到（否则「配文跑了但没用上」会假绿）。"""
        hs = set(hashes)
        out = [dict(r) for r in self.existing if r["url_hash"] in hs]
        for up in self.upserts:
            if up["url_hash"] in hs and up.get("caption") is not None:
                out.append({"url_hash": up["url_hash"], "caption": up["caption"],
                            "fail_count": 0})
        return out

    def upsert_profile(self, account_id, group_id, profile, **kw):
        self.order.append("profile")
        self.writes.append(dict(group_id=group_id, profile=profile, **kw))
        return 1

    def list_group_msg_hour_buckets(self, account_id, since_sec, local_offset_sec, conn=None):
        return [{"group_id": "g1@chatroom", "h": 9, "n": 5}]

    def get_hf_group_message_stats(self, account_id, group_id, since_sec, conn=None):
        return {"total": 30, "activeDays": 5, "avgLen": 20,
                "hourHist": [0] * 9 + [30] + [0] * 14,
                "topSenders": [], "typeHist": {"text": 20, "image": 10}}

    def list_messages_by_peer(self, account_id, peer_id, limit=20, conn=None):
        return [{"direction": "inbound", "content": f"收到一张图片\n[图片] {IMG}",
                 "from_wxid": "wxid_abcdefg", "msg_type": "image"}]

    async def call_judge(self, **kw):
        if (kw.get("images") or []):
            self.order.append("caption")
            return await super().call_judge(**kw)
        self.prompts.append(kw["user_prompt"])
        return GOOD_PROFILE

    def install(self, monkeypatch):
        from plugin.llm import judge as judge_mod

        for name in ("list_hf_image_msgs", "get_hf_captions_by_hashes",
                     "upsert_hf_image_caption"):
            monkeypatch.setattr(repo_hf, name, getattr(self, name))
        monkeypatch.setattr(repo_hf, "list_profiles", self.list_profiles)
        monkeypatch.setattr(repo_hf, "upsert_profile", self.upsert_profile)
        monkeypatch.setattr(repo_messages, "list_group_msg_hour_buckets",
                            self.list_group_msg_hour_buckets)
        monkeypatch.setattr(repo_messages, "get_hf_group_message_stats",
                            self.get_hf_group_message_stats)
        monkeypatch.setattr(repo_messages, "list_messages_by_peer",
                            self.list_messages_by_peer)
        monkeypatch.setattr(judge_mod, "call_judge", self.call_judge)
        monkeypatch.setattr(judge_mod, "resolve_judge_creds", lambda: creds())
        return self


def test_profile_samples_use_caption_instead_of_image_url(monkeypatch):
    """v1.16.0 的整条因果链：配文 → 库 → 样本行 ``[图] <配文>``（URL 噪声清掉）。"""
    io = FakeProfileIo(msgs=[img_msg()]).install(monkeypatch)
    r = asyncio.run(P.maybe_generate_hf_group_profiles(
        "acc1", {"model": "m1"}, NOW))
    assert r.generated == 1
    assert io.order == ["caption", "profile"], "配文必须在画像生成前（同一轮用上）"
    assert io.upserts and io.upserts[0]["caption"] == "晒单: 手机 1999 元"
    prompt = io.prompts[0]
    assert "[图] 晒单: 手机 1999 元" in prompt
    assert IMG not in prompt, "样本里不许再出现图片 URL（噪声吃截断预算）"


def test_caption_pass_failure_never_blocks_profile(monkeypatch):
    """红线：caption 挂了（这里 DB 查询直接炸）⇒ 画像照旧生成，样本退回旧形态。"""
    io = FakeProfileIo(msgs=[img_msg()]).install(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("DB 炸了")

    monkeypatch.setattr(repo_hf, "list_hf_image_msgs", boom)
    r = asyncio.run(P.maybe_generate_hf_group_profiles(
        "acc1", {"model": "m1"}, NOW))
    assert r.generated == 1, "增强件失败不许阻断画像生成"
    assert "收到一张图片" in io.prompts[0]
    assert "[图片]" not in io.prompts[0], "无配文时换成占位（不留 URL）"
