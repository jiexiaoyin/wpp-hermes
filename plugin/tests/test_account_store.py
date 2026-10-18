"""账号写回（W6）—— ``plugin/core/account_store.py`` + 命令面/适配器接线。

## 这个文件钉的四件事（每一条都对应一次真实失效）

1. **凭证字段逐字节不变**。账号文件里有 ``authcode`` / ``tokenKey`` /
   ``webhookSecret`` / ``webhookPathToken``。写回是**读原始 dict 改一个键**，
   不是从 ``Account`` dataclass 反序列化 —— 后者会把本树没建模的键**静默删掉**
   （``groupContextEnabled`` / ``llmIntentModel`` / 各种阈值），
   那是"一次改开关，配置少了半页"。
2. **凭证键**结构性**不可写**（不是靠调用方自觉）：写 ``authcode`` 直接 raise。
   改坏它 = 收信立刻停，而命令回执还会说 ✅。
3. **读不出来就不写**。文件缺失 / JSON 坏 / 根不是对象 ⇒ raise 且**一个字节都不动**
   —— 半份配置写回去 = 平台再也起不来。
4. **落盘之后当次生效**。写文件只解决"持久"，不解决"生效"：
   ``core.accounts.resolve()`` 的 ③ 层（内存覆盖）压在上面，残留的覆盖会把新值
   **影子掉**；而且 WS/webhook 回调是用默认参数闭包绑定的**旧 Account 快照**。
   所以本文件同时钉住：写回要清同键覆盖、``_on_raw`` 要取最新那份、管线 cfg 要刷。

## 判据为什么都下在"盘上 / 解析结果"上

``account_store`` 的失败形态是"命令回 ✅ 而什么都没发生"（旧实现正是如此）。
所以用例不测"调用了 write"，而是**读回文件逐键对**、**跑一遍 ``resolve()`` 看实际生效值**。
"""
from __future__ import annotations

import ast
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from plugin.core import account_store as S
from plugin.core import accounts as A
from plugin.core import overrides as OV
from plugin.platform import commands

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_REL = "plugin/platform/adapter.py"

#: 一份"生产形状"的账号文件：有凭证、有本树**不建模**的键、有嵌套 dict。
SECRET_CODE = "authcode-VALUE-must-never-move"
SECRET_TOKEN_KEY = "tokenKey-VALUE-must-never-move"
SECRET_HOOK = "webhookSe-CRET-must-never-move"
SECRET_PATH = "pathTOKEN-must-never-move"

CREDS = {
    "authcode": SECRET_CODE,
    "tokenKey": SECRET_TOKEN_KEY,
    "webhookSecret": SECRET_HOOK,
    "webhookPathToken": SECRET_PATH,
}


def _cfg(**over) -> dict:
    cfg = {
        "agent": "wpp-wechat-test",
        "enabled": True,
        "selfWxid": "wxid_testbot",
        "nickname": "测试助手",
        # ↓ 本树**没有**建模的键：写回必须原样留着（约束 1）
        "groupContextEnabled": True,
        "llmIntentModel": "some-model-name",
        "unknownNested": {"a": 1, "b": [1, 2, {"c": 3}]},
        "heartflow": {"enabled": True, "replyThreshold": 0.6,
                      "whitelistGroups": ["g1@chatroom"]},
        **CREDS,
    }
    cfg.update(over)
    return cfg


@pytest.fixture
def adir(tmp_path, monkeypatch):
    """隔离的账号目录（autouse 夹具已指好，这里只把它建出来）。"""
    d = tmp_path / "accounts"
    d.mkdir(exist_ok=True)
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_JSON", raising=False)
    OV._runtime_overrides.clear()
    yield d
    OV._runtime_overrides.clear()


@pytest.fixture
def bdir(tmp_path, monkeypatch):
    d = tmp_path / "account-backups"
    monkeypatch.setenv(S.BACKUP_DIR_ENV, str(d))
    return d


def _write(d: Path, account_id: str, cfg: dict) -> Path:
    p = d / f"{account_id}.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ================================================ 1. 只改一个键，别的键一个不动

def test_only_the_target_key_changes(adir, bdir):
    """★ 写入的**唯一**副作用是目标键 —— 其余键（含本树不建模的）逐键相等。"""
    p = _write(adir, "default", _cfg())
    before = _read(p)
    got = S.write_account_key("default", "heartflow",
                              {"enabled": False, "replyThreshold": 0.9})
    after = _read(p)
    assert after["heartflow"] == {"enabled": False, "replyThreshold": 0.9}
    assert after == {**before, "heartflow": after["heartflow"]}
    assert got == after
    # 未建模的键必须还在（从 Account dataclass 反序列化回写就会丢它们）
    assert after["groupContextEnabled"] is True
    assert after["llmIntentModel"] == "some-model-name"
    assert after["unknownNested"] == {"a": 1, "b": [1, 2, {"c": 3}]}


def test_a_new_key_is_added_without_touching_the_rest(adir, bdir):
    p = _write(adir, "default", _cfg())
    before = _read(p)
    S.write_account_key("default", "groupAllowFrom", ["g9@chatroom"])
    after = _read(p)
    assert after["groupAllowFrom"] == ["g9@chatroom"]
    assert {k: v for k, v in after.items() if k != "groupAllowFrom"} == before


# ==================================================== 2. 凭证：不写、不动、不落日志

def test_credential_fields_are_byte_identical_after_a_write(adir, bdir):
    """★ 计划验收项 13②：写回前后**凭证字段逐字节相同**。"""
    p = _write(adir, "default", _cfg())
    S.write_account_key("default", "affection", {"enabled": True})
    after = _read(p)
    for k, v in CREDS.items():
        assert after[k] == v, f"{k} 被改动了"


@pytest.mark.parametrize("key", sorted(S.FORBIDDEN_KEYS))
def test_credential_keys_are_structurally_unwritable(adir, bdir, key):
    """凭证键（含 ``*Env`` 间接键）**一律拒写** —— 且文件一个字节都不动。"""
    p = _write(adir, "default", _cfg())
    before = p.read_bytes()
    with pytest.raises(S.AccountWriteError):
        S.write_account_key("default", key, "whatever")
    assert p.read_bytes() == before


def test_writing_a_credential_key_does_not_echo_the_value(adir, bdir):
    """拒写时的异常文本里不许出现**待写的那个值**（异常会进日志与回执）。"""
    _write(adir, "default", _cfg())
    with pytest.raises(S.AccountWriteError) as ei:
        S.write_account_key("default", "authcode", "super-secret-value")
    assert "super-secret-value" not in str(ei.value)


def test_credentials_never_reach_the_logs(adir, bdir, caplog):
    """成功与失败两条路都不许把凭证打进日志（日志会进 journald）。"""
    p = _write(adir, "default", _cfg())
    with caplog.at_level(logging.DEBUG):
        S.write_account_key("default", "affection", {"enabled": True})
        with pytest.raises(S.AccountWriteError):
            S.write_account_key("default", "authcode", SECRET_CODE)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in CREDS.values():
        assert secret not in text


# ==================================================== 3. 读不出来就不写

def test_a_missing_account_file_is_refused_and_not_created(adir, bdir):
    """文件不存在 ⇒ raise，**且不许凭空造一份**（造出来的那份没有凭据/agent）。"""
    with pytest.raises(S.AccountWriteError):
        S.write_account_key("ghost", "heartflow", {"enabled": True})
    assert not (adir / "ghost.json").exists()


def test_reading_a_missing_account_file_raises_on_its_own(adir, bdir):
    """★ 读那一步**自己**就要拦住，不许指望后面的备份兜底。

    为什么单独钉：文件缺失时"备份失败"也会让 ``write_account_key`` raise，所以
    上面那条**看不见**读的守卫坏没坏 —— 而 ``read_account_json`` 是公开入口，
    任何"读了再决定"的调用方（不止写回）都直接吃它的返回值。返回空 dict 的后果是
    把"文件没了"当成"配置是空的"，接着往上写一份只有本次那个键的残缺配置。
    """
    with pytest.raises(S.AccountWriteError, match="不存在"):
        S.read_account_json("ghost")


def test_a_corrupt_account_file_is_left_untouched(adir, bdir):
    p = adir / "default.json"
    p.write_text("{ this is not json", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(S.AccountWriteError) as ei:
        S.write_account_key("default", "heartflow", {"enabled": True})
    assert p.read_bytes() == before, "坏配置不许被覆盖（那会把能手工救回来的东西毁掉）"
    # 报错只说位置，不回显原文 —— 这份文件里有 authcode
    assert "this is not json" not in str(ei.value)
    assert "行" in str(ei.value) or "line" in str(ei.value)


def test_a_non_object_root_is_refused(adir, bdir):
    p = adir / "default.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(S.AccountWriteError):
        S.write_account_key("default", "heartflow", {"enabled": True})
    assert p.read_text(encoding="utf-8") == "[1, 2, 3]"


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", ".."])
def test_path_traversal_in_the_account_id_is_refused(adir, bdir, bad):
    with pytest.raises(S.AccountWriteError):
        S.account_path(bad)


# ==================================================== 4. 原子替换 / 权限 / 备份

def test_the_file_ends_up_mode_600(adir, bdir):
    """★ 这份文件里有 authcode。实测 ``.183`` 上原来是 644（同机任何人可读）。"""
    p = _write(adir, "default", _cfg())
    os.chmod(p, 0o644)
    S.write_account_key("default", "heartflow", {"enabled": True})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_no_temp_file_is_left_behind(adir, bdir):
    _write(adir, "default", _cfg())
    S.write_account_key("default", "heartflow", {"enabled": True})
    leftovers = [f.name for f in adir.iterdir() if f.name != "default.json"]
    assert leftovers == [], f"临时文件没清干净: {leftovers}"


def test_a_backup_of_the_previous_content_is_kept(adir, bdir):
    """★ 写前必须备份（老板定的备份根，命名沿用 ``<name>.pre-<tag>-<epoch>``）。"""
    p = _write(adir, "default", _cfg())
    before = p.read_bytes()
    S.write_account_key("default", "heartflow", {"enabled": False})
    baks = sorted(bdir.glob("default.json.pre-writeback-*"))
    assert len(baks) == 1, f"备份没生成或重名: {list(bdir.iterdir())}"
    assert baks[0].read_bytes() == before, "备份必须是**改动前**的内容"
    assert stat.S_IMODE(baks[0].stat().st_mode) == 0o600, "备份里也有 authcode"


def test_two_writes_in_the_same_second_do_not_overwrite_the_backup(adir, bdir):
    _write(adir, "default", _cfg())
    S.write_account_key("default", "affection", {"enabled": True})
    S.write_account_key("default", "jargon", {"enabled": True})
    assert len(list(bdir.glob("default.json.pre-writeback-*"))) == 2


def test_a_failed_backup_aborts_the_write(adir, bdir):
    """备份目录建不出来 ⇒ **不写**（宁可这次没生效，也不能"改了没得回退"）。"""
    p = _write(adir, "default", _cfg())
    before = p.read_bytes()
    blocker = adir.parent / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    os.environ[S.BACKUP_DIR_ENV] = str(blocker / "sub")
    try:
        with pytest.raises(S.AccountWriteError):
            S.write_account_key("default", "heartflow", {"enabled": True})
    finally:
        os.environ[S.BACKUP_DIR_ENV] = str(bdir)
    assert p.read_bytes() == before


def test_a_failed_copy_during_backup_aborts_the_write(adir, bdir, monkeypatch):
    """★ 目录建得出来、**拷贝那一步**失败 ⇒ 同样一个字都不许动。

    为什么单列：上面那条在 ``mkdir`` 就被拦下了，**走不到** ``copyfile`` —— 于是
    "拷贝失败就放行"这个形态（备份声明了却在盘上没有，等于没备份）它抓不到。
    写回是"改了就没得回退"的场景，备份的半途失败必须和备份的缺席一样硬。
    """
    p = _write(adir, "default", _cfg())
    before = p.read_bytes()

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(S.shutil, "copyfile", boom)
    with pytest.raises(S.AccountWriteError, match="备份失败"):
        S.write_account_key("default", "heartflow", {"enabled": True})
    assert p.read_bytes() == before, "备份没成 ⇒ 一个字节都不许动"
    assert list(bdir.glob("*.pre-writeback-*")) == [], "半份备份也不许留在盘上"


# ==================================================== 5. 清影子覆盖（当次生效）

def test_writing_clears_the_shadowing_override(adir, bdir):
    """★ ``resolve()`` 的 ③ 层压在 ① 之上：残留覆盖会把刚写的新值**影子掉**。

    判据走 ``resolve()``（**实际生效值**），不是"覆盖字典里有没有那个键"。
    """
    _write(adir, "default", _cfg())
    OV.set_override("default", "heartflow", {"enabled": True, "replyThreshold": 0.99})
    assert A.resolve("default").heartflow["replyThreshold"] == 0.99     # 覆盖生效中

    S.write_account_key("default", "heartflow",
                        {"enabled": False, "replyThreshold": 0.3})
    acct = A.resolve("default")
    assert acct.heartflow == {"enabled": False, "replyThreshold": 0.3}, \
        "覆盖没清 ⇒ 链路读到的还是旧值（文件改了、行为照旧）"
    assert acct.enabled is True


def test_an_override_on_another_key_is_untouched(adir, bdir):
    """只清**同一个键**的覆盖 —— 别的键的覆盖是别人的正事，不该被顺手抹掉。"""
    _write(adir, "default", _cfg())
    OV.set_override("default", "allowFrom", ["wxid_someone"])
    S.write_account_key("default", "heartflow", {"enabled": True})
    assert OV.get_override("default").get("allowFrom") == ["wxid_someone"]


# =================================================== 6. 命令面：写的是文件，不是内存

def test_the_command_layer_writes_the_file_and_not_an_override(adir, bdir):
    """``commands.write_back`` 落盘 + 清覆盖（同键）。"""
    p = _write(adir, "default", _cfg())
    assert commands.write_back("default", "heartflow", {"enabled": False}) == ""
    assert _read(p)["heartflow"] == {"enabled": False}
    assert OV.get_override("default").get("heartflow") is None


def test_write_back_reports_our_own_error_honestly(adir, bdir, monkeypatch):
    """★ ``AccountWriteError`` ⇒ 回复必须**说没生效**，并把原因带上。

    为什么单列这条：``AccountWriteError`` 是我们自己写的文案（不含凭证），所以它
    **整个第一行**都该给用户看 —— 用户得知道是"磁盘满"还是"文件不是 JSON"，否则
    只能看到一句"失败了"。而"写不进去还回 ✅"正是本项要消灭的形态（旧实现就是
    回 ✅ 的），所以这里同时钉住：**一个字都不许落盘**。
    """
    p = _write(adir, "default", _cfg())
    before = p.read_bytes()

    def _boom(*a, **kw):
        raise S.AccountWriteError("磁盘满：/dev/vdb 没有空间了")

    monkeypatch.setattr(S, "write_account_key", _boom)
    out = commands.write_back("default", "heartflow", {"enabled": True})
    assert "❌" in out and "没有生效" in out
    assert "磁盘满" in out, "我们自己写的失败原因要带给用户（它不是凭证）"
    assert p.read_bytes() == before, "写失败了还动了文件 ⇒ 回执说的「没生效」是假的"


def test_write_back_reports_the_failure_and_leaks_nothing(adir, bdir, monkeypatch):
    """★ 未预期异常只报**类名**：回执要走微信，异常文本可能带着文件内容。"""
    _write(adir, "default", _cfg())

    def _boom(*a, **kw):
        raise RuntimeError(f"boom {SECRET_CODE}")

    monkeypatch.setattr(S, "write_account_key", _boom)
    out = commands.write_back("default", "heartflow", {"enabled": True})
    assert "❌" in out and "没有生效" in out
    assert "RuntimeError" in out
    assert SECRET_CODE not in out, "未知异常的文本不许原样发到微信里"


def test_reload_now_without_an_adapter_is_not_an_error(adir, bdir):
    """没有适配器句柄（纯工具模式 / 单测）**合法**，只是当次不生效（记 warn）。"""
    assert commands.reload_now("default", None) is False


def test_reload_now_reports_adapter_failures(adir, bdir):
    class _Bad:
        def reload_account(self, account_id):        # noqa: ANN001
            raise RuntimeError("解析炸了")

    assert commands.reload_now("default", _Bad()) is False


# =================================================== 7. 适配器接线（AST + 行为）

def _adapter_src() -> str:
    return (ROOT / ADAPTER_REL).read_text(encoding="utf-8")


def _method(name: str) -> ast.AST:
    for node in ast.parse(_adapter_src()).body:
        if isinstance(node, ast.ClassDef) and node.name == "WppAdapter":
            for n in node.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                    return n
    raise AssertionError(f"{ADAPTER_REL} 里找不到 {name}")


def _code(node: ast.AST) -> str:
    """函数体源码（**去 docstring** —— 里面写满了反例，搜不得）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def test_handle_one_reads_the_latest_account_snapshot():
    """★ ``_on_raw`` 必须取 ``self._accounts`` 里**最新**那份。

    WS/webhook 的回调是 ``lambda payload, a=acct`` 闭包绑定的旧快照 ⇒ 不重新取，
    命令改过的白名单/群策略永远走不到这条唯一活着的入站路径上。
    """
    code = _code(_method("_on_raw"))
    assert "self._accounts.get(acct.id" in code, \
        "入站必须重新取当前 Account（闭包里那份是 connect() 时的旧快照）"


def test_the_command_call_site_passes_the_adapter():
    """``_maybe_command`` 必须把 adapter 传下去，否则写回是"落地不生效"（又一句谎话）。"""
    code = _code(_method("_maybe_command"))
    assert "commands.handle_command(acct.id, content, adapter=self)" in code


def test_reload_refreshes_the_pipeline_cfg_without_rebuilding_it():
    """★ 就地刷 ``pipeline.cfg``、**不重建** ``InboundPipeline``。

    重建会把 ``seen`` 去重器一起丢掉 ⇒ 重连/重载窗口里的消息被二次处理。
    """
    code = _code(_method("reload_account"))
    assert "pipeline.cfg = self._pipeline_cfg(acct)" in code
    assert "InboundPipeline(" not in code, "重建管线会丢掉去重器（seen）"
    assert "self._accounts[account_id] = acct" in code
    # 不连接/不断开账号：enabled 翻转要等下一次 connect()
    assert "self._clients[" not in code and "self._start_ws" not in code
    assert "disconnect" not in code


@pytest.fixture(scope="module")
def adapter_mod(stubbed):        # noqa: ARG001 —— 靠它装上 gateway 空壳
    from plugin.platform import adapter as adapter_mod

    return adapter_mod


class _Acct:
    id = "default"
    self_wxid = "wxid_testbot"


class _Pipeline:
    """最小管线替身：只关心 ``cfg`` 会不会被就地换掉、``seen`` 还在不在。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.seen = object()


def test_reload_account_swaps_the_account_and_keeps_the_pipeline_object(
        adapter_mod, adir, bdir, monkeypatch):
    """行为判据（不靠 AST）：换 ``Account``、就地刷 cfg、**同一个管线对象**还在。

    ⚠️ 壳里必须给全 ``reload_account`` 会碰的每一个属性（W7-6 之后多了 ``_debounce``）——
    这个夹具**绕过了 ``__init__``**，所以"对象真实长什么样"得在这里手工维持：
    漏一个就是 ``AttributeError``，看着像"重载炸了"，其实只是壳没跟上。
    """
    from plugin.inbound.debounce import InboundDebouncer

    _write(adir, "default", _cfg(heartflow={"enabled": False, "whitelistGroups": []},
                                 debounceMs=1500))
    ad = adapter_mod.WppAdapter.__new__(adapter_mod.WppAdapter)   # 不走 __init__（免 Hermes 基类）
    pipe = _Pipeline({"groupAllowFrom": []})
    #: 真聚合器（不是替身）⇒ 顺带钉住 W7-6 的热加载：窗口要跟着账号文件走、**不重建**
    db = InboundDebouncer("default", lambda _batch: None, interval_ms=500)
    ad._accounts = {"default": object()}
    ad._pipelines = {"default": pipe}
    ad._debounce = {"default": db}
    ad.config = type("C", (), {"extra": {}})()

    assert ad.reload_account("default") is True
    assert ad._accounts["default"].heartflow["enabled"] is False
    assert ad._pipelines["default"] is pipe, "管线被重建了 ⇒ 去重器丢了"
    assert pipe.seen is not None
    assert pipe.cfg["heartflow"]["enabled"] is False, "准入判据读的就是这份快照"
    assert db.interval_ms == 1500, "窗口没跟着账号文件走 ⇒ debounceMs 又成了一个死键"
    assert ad._debounce["default"] is db, "聚合器被重建 ⇒ 已缓冲的那批被丢掉（真丢消息）"


def test_reload_account_keeps_the_old_account_when_the_config_is_bad(
        adapter_mod, adir, bdir):
    """配置改坏了（缺 agent）⇒ 只 warn，**沿用**内存里那份（命令链路不该因此崩）。"""
    _write(adir, "default", _cfg(agent=""))
    old = object()
    ad = adapter_mod.WppAdapter.__new__(adapter_mod.WppAdapter)
    ad._accounts = {"default": old}
    ad._pipelines = {}
    ad._debounce = {}                # W7-6 之后也是对象形状的一部分（见上一条的说明）
    ad.config = type("C", (), {"extra": {}})()

    assert ad.reload_account("default") is False
    assert ad._accounts["default"] is old, "解析失败时不许把好账号换掉"


# =================================================== 8. 文档不许再自称"内存覆盖"

def test_the_command_module_no_longer_advertises_memory_overrides():
    """回复文案里的**承诺**必须与实现一致（这句曾经是"重启回退"，现在不是了）。"""
    src = (ROOT / "plugin/platform/commands.py").read_text(encoding="utf-8")
    assert "overrides.set_override" not in src, \
        "命令面不许再写内存覆盖（写回账号文件才是 SSOT）"
    hf = (ROOT / "plugin/platform/hf_commands.py").read_text(encoding="utf-8")
    assert "overrides.set_override" not in hf
    assert "重启后回退" not in hf, "文案还在承诺回退 ⇒ 与实现相反"
