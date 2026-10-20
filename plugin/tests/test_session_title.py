"""WebUI 会话标题：群聊=群名（一个群一条会话），私聊=联系人昵称。

老板 2026-10-18：「wpp-wechat 下的会话……使用群聊的名称、私聊的昵称作为会话标题，
便于在 webui 快速定位」；随后放宽为「群聊用群ID或群名称，私聊用 wxid 或昵称，
按最容易的来」。实现取**名字优先、ID 兜底**。

同一天老板又追加：「同一个群的会话，不管是哪一个成员触发的会话，只计入统一会话。」
所以群会话由 ``group_sessions_per_user: false`` 合并成一条（发送者身份由 Hermes
的共享会话模式在消息前加 ``[昵称]`` 保留），标题就只是群名。

三条不变量：

1. 一个群只有一条会话 ⇒ 标题就是群名，不带发送者；
2. 查不到名字绝不报错 —— 标题退回群 ID / wxid，消息照发；
3. 只写 ``user`` provenance，且只在新会话首轮写；用户手动改名后不再覆盖。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from plugin.inbound import session_title as T


GROUP = "21354014635@chatroom"
GROUP_NAME = "🇨🇳移动业务对接群Ⅱ示例机友"


def _origin(**kw) -> dict:
    base = {
        "platform": "wechatpadpro",
        "chat_id": f"default:{GROUP}",
        "chat_name": GROUP,
        "chat_type": "group",
        "user_id": "wxid_tsyoek04xgd822",
        "user_name": "wxid_tsyoek04xgd822",
    }
    base.update(kw)
    return base


@pytest.fixture
def names(monkeypatch):
    """把三张名字表换成内存字典，测试不碰 MariaDB。"""
    monkeypatch.setattr(T, "group_name", lambda a, g: GROUP_NAME if a == "default" else "")
    monkeypatch.setattr(
        T, "group_member_name",
        lambda a, g, w: {"wxid_tsyoek04xgd822": "戴永刚"}.get(w, ""),
    )
    monkeypatch.setattr(
        T, "contact_name",
        lambda a, w: {"wxid_owner_demo": "示例联系人"}.get(w, ""),
    )
    T.clear_name_cache()
    yield
    T.clear_name_cache()


def test_name_cache_is_bounded(monkeypatch):
    """长跑进程里群/成员只会越来越多：缓存必须有上限，不能只靠 5 分钟 TTL。"""
    monkeypatch.setattr(T, "_NAME_CACHE_MAX", 8)
    T.clear_name_cache()
    for i in range(50):
        T._cached(("member", "a", "g", f"w{i}"), lambda i=i: f"name{i}")
    assert len(T._NAME_CACHE) <= 8, "名称缓存无上限 ⇒ 内存随联系人数量增长"
    assert ("member", "a", "g", "w0") not in T._NAME_CACHE, "最老的条目应被淘汰"
    assert ("member", "a", "g", "w49") in T._NAME_CACHE, "最新条目必须保留"
    T.clear_name_cache()


def test_group_title_is_just_the_group_name(names):
    """一个群一条会话 ⇒ 标题不带发送者，否则同群不同成员的会话会撞名。"""
    assert T.build_session_title(_origin()) == GROUP_NAME


def test_the_sender_never_leaks_into_a_group_title(names):
    origin = _origin(user_id="wxid_owner_demo", user_name="示例联系人")
    assert T.build_session_title(origin) == GROUP_NAME


def test_group_title_falls_back_to_group_id(monkeypatch):
    """名字全查不到时退回群 ID —— 老板明确接受「群ID / wxid」作为兜底。"""
    monkeypatch.setattr(T, "group_name", lambda a, g: "")
    monkeypatch.setattr(T, "group_member_name", lambda a, g, w: "")
    monkeypatch.setattr(T, "contact_name", lambda a, w: "")
    assert T.build_session_title(_origin()) == GROUP


def test_dm_title_is_the_contact_nickname(names):
    origin = _origin(chat_id="default:wxid_owner_demo", chat_name="wxid_owner_demo",
                     chat_type="dm", user_id="wxid_owner_demo", user_name="wxid_owner_demo")
    assert T.build_session_title(origin) == "示例联系人"


def test_dm_title_uses_the_pushed_nickname_when_the_db_is_cold(monkeypatch):
    monkeypatch.setattr(T, "contact_name", lambda a, w: "")
    origin = _origin(chat_id="default:wxid_new", chat_name="wxid_new",
                     chat_type="dm", user_id="wxid_new", user_name="新朋友")
    assert T.build_session_title(origin) == "新朋友"


def test_dm_title_falls_back_to_wxid(monkeypatch):
    monkeypatch.setattr(T, "contact_name", lambda a, w: "")
    origin = _origin(chat_id="default:wxid_new", chat_name="wxid_new",
                     chat_type="dm", user_id="wxid_new", user_name="wxid_new")
    assert T.build_session_title(origin) == "wxid_new"


def test_title_for_session_rejects_other_platforms():
    row = {"origin_json": json.dumps(_origin(platform="telegram", chat_type="dm")),
           "session_key": ""}
    assert T.title_for_session(row) == ""


class _FakeDB:
    """记录标题写入；``taken`` 里的标题模拟 Hermes 的唯一约束冲突。"""

    def __init__(self, taken=(), title_source="derived"):
        self.taken = set(taken)
        self.title_source = title_source
        self.written: list[str] = []
        self.closed = False

    def get_session(self, session_id):
        return {
            "origin_json": json.dumps(_origin()),
            "session_key": f"agent:wpp-wechat:wechatpadpro:group:default:{GROUP}:"
                           f"wxid_tsyoek04xgd822",
            "title_source": self.title_source,
        }

    def set_session_title(self, session_id, title):
        if title in self.taken:
            raise ValueError("duplicate title")
        self.taken.add(title)
        self.written.append(title)
        return True

    def close(self):
        self.closed = True


def test_a_duplicate_title_gets_a_numeric_suffix():
    db = _FakeDB(taken=[GROUP_NAME])
    assert T.write_title(db, "s1", GROUP_NAME) == f"{GROUP_NAME} #2"


def test_the_hook_writes_a_user_title_on_the_first_turn(monkeypatch, names):
    db = _FakeDB()
    monkeypatch.setattr(T, "_open_session_db", lambda sid: db)
    T.on_pre_llm_call(session_id="s1", platform="wechatpadpro", is_first_turn=True)
    assert db.written == [GROUP_NAME]
    assert db.closed


@pytest.mark.parametrize("kw", [
    {"session_id": "s1", "platform": "wechatpadpro", "is_first_turn": False},
    {"session_id": "s1", "platform": "telegram", "is_first_turn": True},
    {"session_id": "", "platform": "wechatpadpro", "is_first_turn": True},
])
def test_the_hook_is_a_no_op_outside_its_case(monkeypatch, kw):
    db = _FakeDB()
    monkeypatch.setattr(T, "_open_session_db", lambda sid: db)
    T.on_pre_llm_call(**kw)
    assert db.written == []


def test_a_manually_titled_session_is_never_rewritten(monkeypatch, names):
    db = _FakeDB(title_source="user")
    monkeypatch.setattr(T, "_open_session_db", lambda sid: db)
    T.on_pre_llm_call(session_id="s1", platform="wechatpadpro", is_first_turn=True)
    assert db.written == []


def test_a_db_failure_never_reaches_the_caller(monkeypatch):
    """标题只是展示层：库打不开时 hook 必须静默返回，不能把这一轮对话带崩。"""
    def boom(sid):
        raise RuntimeError("db down")

    monkeypatch.setattr(T, "_open_session_db", boom)
    T.on_pre_llm_call(session_id="s1", platform="wechatpadpro", is_first_turn=True)


# ============================================================ 跨 profile 隔离

class _FakeSessionDB:
    """只记录打开过的路径；``target`` 上的库才"包含"该 session。"""

    opened: list[tuple[Path | None, bool]] = []
    target: Path | None = Path("/__no_such_db__")

    def __init__(self, path=None, read_only=False):
        self.path = Path(path).resolve() if path is not None else None
        type(self).opened.append((self.path, read_only))

    def get_session(self, session_id):
        return {"session_id": session_id} if self.path == self.target else None

    def close(self):
        return None


def _install_fake_hermes_state(monkeypatch):
    _FakeSessionDB.opened = []
    monkeypatch.setitem(sys.modules, "hermes_state",
                        types.SimpleNamespace(SessionDB=_FakeSessionDB))


def test_open_session_db_only_probes_the_registered_profile_home(monkeypatch, tmp_path):
    """兜底只许读本 profile 的 state.db —— 不再扫 ``profiles/*/state.db``。"""
    home = tmp_path / "profiles" / "wpp-wechat"
    home.mkdir(parents=True)
    (home / "state.db").write_bytes(b"")
    other = tmp_path / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "state.db").write_bytes(b"")
    _FakeSessionDB.target = (home / "state.db").resolve()
    _install_fake_hermes_state(monkeypatch)
    monkeypatch.setattr(T, "_hook_profile_home", lambda: home)

    db = T._open_session_db("s1")
    assert db is not None
    assert all(path is None or path.parent == home.resolve()
               for path, _ro in _FakeSessionDB.opened), \
        "不许打开别的 profile 的 state.db"
    assert (other / "state.db").resolve() not in [p for p, _ in _FakeSessionDB.opened]


def test_open_session_db_does_not_guess_when_no_profile_home_is_registered(
        monkeypatch, tmp_path):
    _FakeSessionDB.target = Path("/__no_such_db__")
    _install_fake_hermes_state(monkeypatch)
    monkeypatch.setattr(T, "_hook_profile_home", lambda: None)
    assert T._open_session_db("s1") is None
    assert _FakeSessionDB.opened == [(None, False)], \
        "只试默认库；没有登记 home 就停手"


def test_set_profile_home_can_be_cleared():
    T.set_profile_home("/tmp/example-profile")
    assert T._PROFILE_HOME == Path("/tmp/example-profile").resolve()
    T.set_profile_home(None)
    assert T._PROFILE_HOME is None
