"""OSS 上传（W7-1）—— ``plugin/inbound/media_oss.py``。

## 这一面的失效形态（为什么判据都下在"盘上/日志里真出现了什么"）

上传这一截**失败是常态**（缺凭据 / 缺 ossutil / 网络抖），而设计上它就该**安静降级**：
消息照常处理，只是不带 URL。所以"没报错"这条零信息量 —— 真出问题的是下面四种：

* **凭据进日志**：本机 ``~/.hermes/.env`` 与那三把生产密钥是红线，而
  ``ossutil`` 的 stderr 在某些错误下会回显 key ⇒ 日志脱敏坏掉就等于泄漏；
* **降级变成抛**：媒体富化失败把整条消息带崩（TS 六个 enrich 入口都写着"不抛"）；
* **临时文件不删**：媒体热路径每次留一个文件，磁盘慢慢被吃光，没有任何告警；
* **"没有凭据"被缓存**：老板补上凭据文件后仍然不带 URL，表现为"改了没反应"（重启才生效）。

所以判据是"日志记录里**不含**密钥""字节真的落了盘又真的被删了""补上文件后**当次**就读到"。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
from pathlib import Path

import pytest

from plugin.inbound import media_oss as M


def _sync(coro_fn):
    """本仓没有 pytest-asyncio（见 ``test_sync_fallback.py:36`` 同款说明）⇒ 手工 ``asyncio.run``。"""
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


#: 造凭据文件用的假密钥 —— **形状像真的**（长、字母数字），这样才能真验"日志里不许出现它"。
FAKE_AK = "LTAI5tFakeAccessKeyId0000001"
FAKE_SK = "FakeSecret00000000000000000000002"


@pytest.fixture(autouse=True)
def _no_cache_leak():
    """每个用例前后都清缓存：缓存是模块级的，不清就会串味（这正是它在生产的用法）。"""
    M.clear_oss_config_cache()
    yield
    M.clear_oss_config_cache()


@pytest.fixture
def cred_file(tmp_path) -> Path:
    p = tmp_path / "oss-credentials.json"
    p.write_text(json.dumps({"accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK,
                             "bucket": "openclaw-a", "endpoint": "oss-cn-hangzhou.aliyuncs.com"}),
                 encoding="utf-8")
    return p


def _fake_oss() -> M.OssConfig:
    return M.OssConfig(FAKE_AK, FAKE_SK, "openclaw-a", "oss-cn-hangzhou.aliyuncs.com")


# ============================================================ 1. 凭据读取

def test_load_reads_a_complete_credential_file(cred_file):
    cfg = M.load_oss_config(path=str(cred_file))
    assert cfg == _fake_oss()


def test_a_missing_file_is_no_config_not_an_error(tmp_path):
    assert M.load_oss_config(path=str(tmp_path / "nope.json")) is None


def test_a_corrupt_file_is_no_config(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    assert M.load_oss_config(path=str(p)) is None


@pytest.mark.parametrize("drop", ["accessKeyId", "accessKeySecret", "bucket"])
def test_a_half_filled_file_is_no_config(tmp_path, drop):
    """半份凭据传上去只会得到 403 —— 不如当场判"没有凭据"（错误信息更有用）。"""
    d = {"accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK, "bucket": "openclaw-a"}
    d.pop(drop)
    p = tmp_path / "half.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert M.load_oss_config(path=str(p)) is None


@pytest.mark.parametrize("blank", ["accessKeyId", "accessKeySecret", "bucket"])
def test_an_empty_string_field_is_no_config(tmp_path, blank):
    """空串也是缺（TS 是 ``!d.accessKeyId`` 的真值判断，空串为假）。"""
    d = {"accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK, "bucket": "openclaw-a"}
    d[blank] = ""
    p = tmp_path / "blank.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert M.load_oss_config(path=str(p)) is None


def test_endpoint_defaults_when_absent(tmp_path):
    p = tmp_path / "noep.json"
    p.write_text(json.dumps({"accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK,
                             "bucket": "b"}), encoding="utf-8")
    cfg = M.load_oss_config(path=str(p))
    assert cfg is not None and cfg.endpoint == M.DEFAULT_ENDPOINT


def test_a_valid_config_is_cached_for_the_ttl(cred_file):
    """★ 缓存命中时**不再读盘** —— 判据是"把文件删了仍然给得出配置"。"""
    first = M.load_oss_config(path=str(cred_file))
    assert first is not None
    os.unlink(cred_file)
    assert M.load_oss_config(path=str(cred_file)) == first, "TTL 内必须命中缓存"


def test_the_cache_expires(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK,
                             "bucket": "b"}), encoding="utf-8")
    assert M.load_oss_config(path=str(p), now=100.0) is not None
    os.unlink(p)
    assert M.load_oss_config(path=str(p), now=100.0 + M.CRED_TTL_S - 0.1) is not None
    assert M.load_oss_config(path=str(p), now=100.0 + M.CRED_TTL_S + 0.1) is None, \
        "过期后必须重读盘（否则凭据轮换要重启才生效）"


def test_a_previously_missing_file_is_not_cached(tmp_path):
    """★ INVARIANT 第 2 条：**只有齐全的才进缓存**。

    坏法：把"没有凭据"也缓存 30s（或永久）⇒ 老板把文件放上去之后仍然不带 URL，
    表现为"改了没反应"，只能靠重启 —— 这类"静默不生效"是本仓反复吃过亏的形态。
    """
    p = tmp_path / "late.json"
    assert M.load_oss_config(path=str(p), now=500.0) is None
    p.write_text(json.dumps({"accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK,
                             "bucket": "b"}), encoding="utf-8")
    assert M.load_oss_config(path=str(p), now=500.0) is not None, \
        "补上凭据文件后当次就该读到（不能把'没有凭据'缓存起来）"


def test_the_cache_is_bounded(cred_file, tmp_path):
    """条数上限 2（TS ``maxSize:2``）—— 别让几十个路径把内存撑起来。"""
    for i in range(5):
        p = tmp_path / f"c{i}.json"
        p.write_text(cred_file.read_text(encoding="utf-8"), encoding="utf-8")
        assert M.load_oss_config(path=str(p)) is not None
    assert len(M._CACHE) <= M.CRED_CACHE_MAX


def test_env_var_points_at_the_file(monkeypatch, cred_file):
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    assert M.oss_credentials_path() == str(cred_file)
    assert M.load_oss_config() is not None


def test_default_path_is_under_hermes_state(monkeypatch):
    monkeypatch.delenv(M.OSS_CREDENTIALS_ENV, raising=False)
    assert M.oss_credentials_path().endswith(".hermes/state/credentials/oss-credentials.json")


def test_env_credentials_take_precedence(monkeypatch, cred_file):
    monkeypatch.delenv(M.OSS_CREDENTIALS_ENV, raising=False)
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", FAKE_AK)
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", FAKE_SK)
    monkeypatch.setenv("OSS_BUCKET", "openclaw-a")
    monkeypatch.setenv("OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com")
    cfg = M.load_oss_config()
    assert cfg == _fake_oss()


def test_env_credentials_are_read_through_the_profile_scope(monkeypatch):
    """★ 多 profile 网关：``OSS_*`` 在 Hermes 的 profile secret scope，不在进程 env。

    2026-10-18 真机 bug：``media_oss`` 直接读 ``os.environ`` ⇒ 凭据明明配在 profile
    ``.env`` 里，日志却报「没有 OSS 凭据（文件不存在或字段不全）」⇒ 所有媒体都上不了
    OSS（回复侧也拿不到公网直链）。这里清空进程 env、只留 scope，钉住读取口径。
    """
    for name in ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    scope = {"OSS_ACCESS_KEY_ID": FAKE_AK, "OSS_ACCESS_KEY_SECRET": FAKE_SK,
             "OSS_BUCKET": "openclaw-a", "OSS_ENDPOINT": "oss-cn-hangzhou.aliyuncs.com"}
    monkeypatch.setattr(M, "secret_env", lambda name, default="": scope.get(name, default))
    assert M.load_oss_config() == _fake_oss()


# ============================================================ 2. 纯函数

def test_build_oss_key_uses_the_agreed_structure():
    assert M.build_oss_key("default", "images", "abc.jpg", today="2026-10-02") == \
        "wpp/default/images/2026-10-02/abc.jpg"


def test_build_oss_key_falls_back_to_default_account():
    """空账号名不能拼出 ``wpp//images/...``（那是另一个目录）。"""
    assert M.build_oss_key("", "files", "x.bin", today="2026-10-02").startswith("wpp/default/files/")


def test_build_oss_key_today_is_utc():
    """⚠️ 日期按 **UTC**（TS ``toISOString``）—— 本机 +08，08:00 前的文件归前一天。

    照抄的目的是两套实现产出同一批目录；改成本地日期会让检索脚本对不上。
    """
    import time as _t
    assert M.build_oss_key("a", "images", "f.jpg").split("/")[3] == \
        _t.strftime("%Y-%m-%d", _t.gmtime())


@pytest.mark.parametrize("good", ["0123456789abcdef", "AABBCCDD", "a" * 64])
def test_sanitize_keeps_hex_and_lowercases(good):
    assert M.sanitize_filename_part(good) == good.lower()


@pytest.mark.parametrize("bad", ["../../etc/passwd", "hello world", "短", "a" * 7, "g" * 16,
                                 "/abs/path", "", None, 12345])
def test_sanitize_replaces_anything_that_is_not_hex(bad):
    """v1 的 md5 是**自由字符串** ⇒ 不净化就能污染 OSS 路径（``../`` 之类）。"""
    out = M.sanitize_filename_part(bad)
    assert re.fullmatch(r"[a-f0-9]{16}", out), out


def test_the_unsafe_name_class_is_ascii_only():
    """★ Python 的 ``\\w`` 认中文、JS 的 ``\\w`` 不认 ⇒ 不显式定 ASCII 就会与 TS 分叉。

    （单独钉这个字符类，是因为它只在 ``oss_upload_buffer`` 的 ``safe_name`` 那条路上生效——
    走 ``sanitize_filename_part`` 验不到它：那条路整串不认就直接换随机 hex 了。）
    """
    assert M._UNSAFE_NAME_RE.sub("_", "我的 文件.pdf") == "_____.pdf"
    assert M._UNSAFE_NAME_RE.sub("_", "a-b_c.d.mp4") == "a-b_c.d.mp4", "合法字符不许动"


def test_sanitize_fallback_is_random_not_predictable():
    """不认的输入给它**随机**替身 —— 不给攻击者一个可预测的名字。"""
    assert M.sanitize_filename_part("hello world") != M.sanitize_filename_part("hello world")


def test_sanitize_fallback_length_follows_the_argument():
    assert len(M.sanitize_filename_part("bad!", fallback_len=32)) == 32
    assert len(M.sanitize_filename_part("bad!", fallback_len=15)) == 15


def test_public_url_shape():
    assert M.public_url(_fake_oss(), "wpp/default/images/2026-10-02/a.jpg") == \
        "https://openclaw-a.oss-cn-hangzhou.aliyuncs.com/wpp/default/images/2026-10-02/a.jpg"


# ============================================================ 3. 上传

@_sync
async def test_a_missing_ossutil_binary_fails_fast(monkeypatch):
    """★ 与 TS 的有意偏离：缺二进制**不空转 3 次**（1s+2s 白等），一句话说清原因。

    判据下在"调用次数"上：真去跑了说明没走快速失败那条路。
    """
    calls: list[list[str]] = []

    async def _boom(args, timeout_s):  # pragma: no cover —— 走到这里就是失败
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(M, "_run_ossutil", _boom)
    monkeypatch.setattr(M.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError) as ei:
        await M.upload_to_oss(_fake_oss(), "/tmp/x", "k")
    assert "ossutil" in str(ei.value)
    assert calls == [], "缺二进制时一次都不该去试"


@_sync
async def test_upload_retries_then_succeeds(monkeypatch, tmp_path):
    """前两次失败、第三次成功 ⇒ 返 URL，并且退避是 1s、2s（TS ``1000 * attempt``）。"""
    tries: list[int] = []
    sleeps: list[float] = []
    local = tmp_path / "a.bin"
    local.write_bytes(b"x")

    async def _flaky(args, timeout_s):
        tries.append(1)
        return (1, "连接被重置") if len(tries) < 3 else (0, "")

    async def _sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(M, "_run_ossutil", _flaky)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setattr(M.asyncio, "sleep", _sleep)
    url = await M.upload_to_oss(_fake_oss(), str(local), "wpp/default/images/2026-10-02/a.jpg")
    assert url == "https://openclaw-a.oss-cn-hangzhou.aliyuncs.com/wpp/default/images/2026-10-02/a.jpg"
    assert len(tries) == 3
    assert sleeps == [1.0, 2.0], "退避必须是 1s/2s（第三次失败后不再睡）"


@_sync
async def test_upload_gives_up_after_three_and_raises(monkeypatch, tmp_path):
    local = tmp_path / "a.bin"
    local.write_bytes(b"x")
    n: list[int] = []

    async def _always_fail(args, timeout_s):
        n.append(1)
        return 42, "AccessDenied"

    async def _sleep(_s):
        return None

    monkeypatch.setattr(M, "_run_ossutil", _always_fail)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setattr(M.asyncio, "sleep", _sleep)
    with pytest.raises(RuntimeError) as ei:
        await M.upload_to_oss(_fake_oss(), str(local), "k")
    assert len(n) == M.UPLOAD_ATTEMPTS
    assert "AccessDenied" in str(ei.value) and "42" in str(ei.value), \
        "错误里要带 vendor 的原文与退出码 —— 否则排障只剩一句「失败」"


@_sync
async def test_upload_buffer_puts_the_bytes_on_disk_then_cleans_up(monkeypatch, cred_file):
    """★ 临时文件**用完必删** —— 不删就是媒体热路径上每次留一个文件，磁盘悄悄被吃光。"""
    seen: dict = {}

    async def _spy(args, timeout_s):
        seen["args"] = args
        seen["bytes"] = Path(args[2]).read_bytes()          # args = [bin, cp, local, oss://…, …]
        seen["existed_at_call"] = Path(args[2]).is_file()
        return 0, ""

    monkeypatch.setattr(M, "_run_ossutil", _spy)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    url = await M.oss_upload_buffer(b"hello", "x.jpg", "wpp",
                                    account_id="default", kind="images")
    assert seen["bytes"] == b"hello"
    assert seen["existed_at_call"] is True
    assert Path(seen["args"][2]).exists() is False, "临时文件必须删掉"
    assert url.endswith("-x.jpg") and "/wpp/default/images/" in url


@_sync
async def test_the_secret_never_travels_on_the_command_line(monkeypatch, cred_file):
    """★ 密钥**只走 ossutil 自己的配置**（``~/.ossutilconfig``），**不进 argv**。

    命令行是全局可见的（``ps`` / ``/proc/<pid>/cmdline`` / 审计日志 / 报错截图），
    而 ``ossutil`` 本来就有一条专门读配置文件的鉴权路 ⇒ 把 AK/SK 摆进参数，等于为了
    省一份配置文件而把凭据发给全机。这条判据钉的是"别顺手加个 ``-i``/``-k``"。

    ⚠️ 它同时是一份**如实记录**：本模块把 AK/SK 读进 ``OssConfig`` 之后**一个字节都不往下传**
    （与 TS 同款），真机上能上传成功的那一半**全靠 ``~/.ossutilconfig``** ——
    2026-10-03 在 ``.183`` 落 OSS 时就是靠这条才想明白"只放 JSON 为什么不够"。
    """
    seen: list[list[str]] = []

    async def _spy(args, timeout_s):
        seen.append(list(args))
        return 0, ""

    monkeypatch.setattr(M, "_run_ossutil", _spy)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    cfg = M.load_oss_config()
    assert cfg is not None and cfg.access_key_id and cfg.access_key_secret  # 凭据确实读到了
    await M.oss_upload_buffer(b"a", "x.jpg", "wpp", account_id="acc", kind="images")
    argv = seen[0]
    assert not any(cfg.access_key_id in a or cfg.access_key_secret in a for a in argv), argv
    assert not ({"-i", "--access-key-id", "-k", "--access-key-secret"} & set(argv)), argv


@_sync
async def test_upload_buffer_key_carries_a_hash_of_the_filename(monkeypatch, cred_file):
    """``{md5(filename)[:12]}-{safe_name}``：同名的不同消息不会互相覆盖。

    ★ 这里顺带钉住一个**只有跨语言移植才会踩到**的坑：JS 的 ``\\w`` 是 ASCII 的
    ``[A-Za-z0-9_]``，**Python 的 ``\\w`` 是 Unicode 的** ⇒ 不加 ``re.ASCII`` 时
    ``我的 文件.pdf`` 里的中文会**原样进 OSS key**（与 TS 产出的不是同一个键）。
    这条断言就是把它按住的：非 ASCII 一律变 ``_``。
    """
    keys: list[str] = []

    async def _spy(args, timeout_s):
        keys.append(args[3])
        return 0, ""

    monkeypatch.setattr(M, "_run_ossutil", _spy)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    await M.oss_upload_buffer(b"a", "我的 文件.pdf", "wpp", account_id="acc", kind="files")
    assert re.fullmatch(r"oss://openclaw-a/wpp/acc/files/\d{4}-\d{2}-\d{2}/"
                        r"[a-f0-9]{12}-_____.pdf", keys[0]), keys[0]
    assert not any(ord(c) > 127 for c in keys[0]), "OSS key 必须是 ASCII（URL 里要百分号编码）"


@_sync
async def test_upload_buffer_without_account_or_kind_uses_the_legacy_prefix(monkeypatch, cred_file):
    """两个参数**都给**才走新结构；否则退回老 ``{prefix}/{hash}-{name}``（TS 兼容老调用）。"""
    keys: list[str] = []

    async def _spy(args, timeout_s):
        keys.append(args[3])
        return 0, ""

    monkeypatch.setattr(M, "_run_ossutil", _spy)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    await M.oss_upload_buffer(b"a", "x.png", "wpp/miniprogram")
    assert re.fullmatch(r"oss://openclaw-a/wpp/miniprogram/[a-f0-9]{12}-x\.png", keys[0]), keys[0]


@_sync
async def test_upload_buffer_without_credentials_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(tmp_path / "nope.json"))
    with pytest.raises(RuntimeError):
        await M.oss_upload_buffer(b"a", "x.jpg", "wpp")


# ============================================================ 4. 唯一出口：降级

@_sync
async def test_upload_media_bytes_returns_none_and_never_raises(monkeypatch, tmp_path):
    """★ INVARIANT 第 3 条：**降级是单条消息级的**。

    坏法：这里把异常放出去 ⇒ 媒体富化失败把整条消息处理带崩（收不到回复，
    而日志里只有一条 media 的错误 —— 看起来像"这个用户没说话"）。
    """
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(tmp_path / "nope.json"))
    assert await M.upload_media_bytes(b"abc", "x.jpg",
                                      account_id="a", kind="images") is None


@_sync
async def test_upload_media_bytes_returns_none_for_empty_input(monkeypatch, cred_file):
    """0 字节不传 —— 传上去的是一个空文件，模型和用户都会以为拿到了东西。"""
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    assert await M.upload_media_bytes(b"", "x.jpg", account_id="a", kind="images") is None


@_sync
async def test_upload_media_bytes_returns_the_url_on_success(monkeypatch, cred_file):
    async def _ok(args, timeout_s):
        return 0, ""

    monkeypatch.setattr(M, "_run_ossutil", _ok)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    url = await M.upload_media_bytes(b"abc", "x.jpg", account_id="a", kind="images")
    assert url is not None and url.startswith("https://openclaw-a.oss-cn-hangzhou.aliyuncs.com/wpp/a/images/")


@_sync
async def test_upload_media_bytes_swallows_a_non_runtimeerror(monkeypatch, cred_file):
    """连"意想不到的异常"也不许出去（``ossutil`` 可能回一堆怪东西，JSON 解码器也可能炸）。"""
    async def _weird(args, timeout_s):
        raise ValueError("谁也想不到的错")

    monkeypatch.setattr(M, "_run_ossutil", _weird)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    assert await M.upload_media_bytes(b"abc", "x.jpg", account_id="a", kind="images") is None


# ============================================================ 5. 凭据不进日志

@_sync
async def test_the_secret_never_reaches_the_log(monkeypatch, cred_file, caplog):
    """★ INVARIANT 第 1 条：**日志里不许出现密钥**，哪怕 vendor 把它回显在 stderr 里。

    这条不是假想：``ossutil`` 在参数写错时会把命令原样回显，而命令里带 ``--endpoint``
    之类的参数一旦日后被加上 ``--access-key-id``，stderr 就变成一条泄漏。
    所以脱敏闸下在"所有要进日志的外部文本"上，用**像真的**假密钥来验。
    """
    async def _leaky(args, timeout_s):
        return 3, f"--access-key-id {FAKE_AK} --access-key-secret {FAKE_SK} 参数错误"

    async def _sleep(_s):
        return None

    monkeypatch.setattr(M, "_run_ossutil", _leaky)
    monkeypatch.setattr(M.shutil, "which", lambda _n: "/usr/bin/ossutil")
    monkeypatch.setattr(M.asyncio, "sleep", _sleep)
    monkeypatch.setenv(M.OSS_CREDENTIALS_ENV, str(cred_file))
    with caplog.at_level(logging.DEBUG):
        assert await M.upload_media_bytes(b"abc", "x.jpg",
                                          account_id="a", kind="images") is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_AK not in joined and FAKE_SK not in joined, \
        f"密钥泄漏进日志了：{joined[:400]}"
    assert "参数错误" in joined, "脱敏不能把有用信息一起吃掉（要能看出是参数错）"


def test_repr_of_the_config_hides_the_keys():
    """有人 ``log.info("%s", cfg)`` 时也不许把密钥打出去（手滑兜底）。"""
    text = repr(_fake_oss())
    assert FAKE_AK not in text and FAKE_SK not in text
    assert "openclaw-a" in text and "已隐去" in text


# ============================================================ 6. 中立层注入（P0-7）

def test_neutral_load_accepts_an_injected_env_dict():
    """中立层不认 Hermes profile scope —— 调用方把读好的 dict 注入即可。"""
    from plugin.neutral import media_oss as N

    cfg = N.load_oss_config("<injected>", raw={
        "accessKeyId": FAKE_AK, "accessKeySecret": FAKE_SK, "bucket": "b"})
    assert cfg is not None
    assert cfg.endpoint == N.DEFAULT_ENDPOINT
    assert cfg.bucket == "b"


@_sync
async def test_neutral_upload_uses_the_injected_key_builder(monkeypatch):
    """key 结构不再写死 ``wpp/…``：注入什么就用什么（别的通道可以有自己的前缀）。"""
    from plugin.neutral import media_oss as N

    seen: list[list[str]] = []

    async def _spy(args, timeout_s):
        seen.append(args)
        return 0, ""

    cfg = N.OssConfig(FAKE_AK, FAKE_SK, "openclaw-a", N.DEFAULT_ENDPOINT)
    monkeypatch.setattr(N.shutil, "which", lambda _n: "/usr/bin/ossutil")
    url = await N.oss_upload_buffer(
        b"x", "f.jpg", "ignored", account_id="chan", kind="images",
        key_builder=lambda acct, kind, name: f"{acct}/{kind}/{name}",
        load_config=lambda: cfg,
        uploader=lambda oss, path, key: N.upload_to_oss(oss, path, key,
                                                        run_ossutil=_spy),
    )
    assert seen[0][3].startswith("oss://openclaw-a/chan/images/"), seen[0]
    assert "ignored" not in url and "/chan/images/" in url


def test_the_invariant_block_says_all_three_things():
    """不变量标记留在**源码的家**里 —— 改这一带的人必须先看到这三条。"""
    src = (Path(__file__).resolve().parents[1] / "inbound" / "media_oss.py").read_text(encoding="utf-8")
    assert "INVARIANT: W7-1" in src
    for phrase in ("凭据只从", "只有", "进缓存", "降级是单条消息级的"):
        assert phrase in src, phrase
    assert "不代拷" in src, "「凭据不随代码走」这条要留在源码里（deploy.sh 也不同步凭据）"
