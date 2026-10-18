"""入站落库（W1）—— ``plugin/inbound/persist.py`` + 适配器 ④z 接线。

## 这个文件钉的四件事（每一条都对应一次真实失效）

1. **私聊写得进去**：``WppMessage.peer_kind`` 对私聊返回 ``"dm"``，而
   ``wpp_messages.peer_kind`` 是 ``ENUM('direct','group','room')``。
   不映射 ⇒ ``ERROR 1265 Data truncated`` ⇒ 被"永不抛"吞成一条 warn
   ⇒ **私聊一条都不落库，而日志看着正常**。
2. **群聊落的是裸群 id**：``list_group_messages`` 查
   ``chat_id = <裸群 id> OR (chat_id IS NULL AND peer_id = <裸群 id>)``。
   写 ``accountId:peerId`` 那种编码 ⇒ 两个分支都不命中 ⇒ 那条消息对"按群查历史"
   **整个隐形**（出站群消息今天就处在这个状态，本文件同时钉住它被修好）。
3. **``new_msg_id`` 从原始 payload 取**，不是从 ``msg_id`` 复制一个：
   ``WppMessage`` **没有** ``new_msg_id`` 字段 —— ``heartflow.py:1168`` 那句
   ``getattr(msg, "new_msg_id", "")`` 恒空、是条静默空操作。这里不许重蹈。
4. **落库失败绝不打断收信**：对齐 TS ``enrich.ts:58-63`` 的"吞错返 saved:false"。

## 为什么"落库失败"也要有反向判据

本模块的失败形态是**最坏的一种**：异常被吞、日志是一行 warn、链路照跑。
所以用例不是"调用了 save_message"，而是**把写下去的 14 列逐列对上**
（走真 ``save_message`` 的 SQL 与参数，只把连接换成记录器）。

## 接线用 AST 验

``plugin/platform/adapter.py`` 在本机 import 不了（模块级 ``from gateway.config import
Platform`` 是 Hermes 独有），所以 ④z 的**位置**（在 ``should_drop`` 之后、
``_hf_note_group`` 之前）只能按源码形状验 —— 与 ``test_outbound_send_result.py`` 同款。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from plugin.data import repo_messages
from plugin.inbound import persist as P
from plugin.inbound.parser import parse_message

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


# --------------------------------------------------------------------- 夹具

class _Recorder:
    """假 connection：只记 ``(sql, params)``，不做任何 IO。

    ``db.Connection`` 的形状（``plugin/data/db.py:132``）只有 ``execute``/``query``
    这几个方法；``save_message`` 走的是 ``execute``。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params=()) -> int:      # noqa: ANN001
        self.calls.append((sql, tuple(params)))
        return 1

    @property
    def only(self) -> tuple[str, tuple]:
        assert len(self.calls) == 1, f"应当只写一条，实际 {len(self.calls)} 条"
        return self.calls[0]


_GROUP_COLS = ("account_id", "msg_id", "new_msg_id", "direction", "peer_kind",
               "peer_id", "peer_name", "chat_id", "msg_type", "content",
               "raw_payload", "from_wxid", "ts", "create_time")


def _cols(params: tuple) -> dict:
    """参数元组 → ``{列名: 值}``（列序与 ``save_message`` 的 INSERT 一一对应）。"""
    assert len(params) == len(_GROUP_COLS), f"列数不对：{len(params)}"
    return dict(zip(_GROUP_COLS, params))


class _Acct:
    id = "default"


def _group_msg(**over):
    src = {"msgId": "m-1", "fromWxid": "wxid_alice", "fromNickname": "小李",
           "content": "有没有现货", "ChatroomId": "111@chatroom", "MsgType": 1,
           "ts": 1700000000, "NewMsgId": "9001"}
    src.update(over)
    return parse_message("default", src)


def _dm_msg(**over):
    src = {"msgId": "m-2", "fromWxid": "wxid_bob", "content": "在吗",
           "MsgType": 1, "ts": 1700000005}
    src.update(over)
    return parse_message("default", src)


# ==================================================== 1. 群聊：14 列逐列对上

def test_a_group_message_lands_every_column():
    """群消息落库：**14 列逐列**对（这是"真生效"的判据，不是"调用过"）。"""
    conn = _Recorder()
    assert P.persist_inbound(_Acct(), _group_msg(), conn=conn) is True
    sql, params = conn.only
    assert "INSERT INTO wpp_messages" in sql
    assert _cols(params) == {
        "account_id": "default",
        "msg_id": "m-1",
        "new_msg_id": "9001",
        "direction": "inbound",
        "peer_kind": "group",
        "peer_id": "111@chatroom",
        "peer_name": "小李",
        "chat_id": "111@chatroom",
        "msg_type": "1",
        "content": "有没有现货",
        "raw_payload": params[_GROUP_COLS.index("raw_payload")],   # JSON 串，下面单测
        "from_wxid": "wxid_alice",
        "ts": 1700000000,
        # create_time 由 ts 派生：本地墙钟 YYYYMMDDHHMMSS，与 2026-08-11 前的历史行同形
        "create_time": repo_messages.legacy_create_time(1700000000),
    }


def test_msg_type_is_stored_as_a_string():
    """对齐 TS ``String(msgType)`` —— 列是 VARCHAR(32)，别把 int 直塞。"""
    conn = _Recorder()
    P.persist_inbound(_Acct(), _group_msg(MsgType=49), conn=conn)
    assert _cols(conn.only[1])["msg_type"] == "49"


def test_create_time_is_written_in_the_legacy_wall_clock_format():
    """``create_time`` 不再是"新行恒 NULL"：按 ``ts`` 写本地墙钟 ``YYYYMMDDHHMMSS``。

    生产实测历史行：``create_time=20260811094227`` ↔ ``ts=2026-08-11 09:42:27``（+08:00）。
    新行必须与它同形，且**不能**写成 epoch（那会让同一列混两种尺度）。
    """
    conn = _Recorder()
    P.persist_inbound(_Acct(), _group_msg(ts=1700000000), conn=conn)
    ct = _cols(conn.only[1])["create_time"]
    assert isinstance(ct, int) and ct > 10**13, "必须是 14 位墙钟串，不是 10 位 epoch"
    assert ct == repo_messages.legacy_create_time(1700000000)
    assert len(str(ct)) == 14, f"YYYYMMDDHHMMSS 应为 14 位，实际 {ct}"


def test_raw_payload_is_json_encoded():
    conn = _Recorder()
    msg = _group_msg()
    P.persist_inbound(_Acct(), msg, conn=conn)
    raw = _cols(conn.only[1])["raw_payload"]
    assert isinstance(raw, str) and raw.startswith("{")
    assert "wxid_alice" in raw


# ============================================ 2. 两个真 bug：peer_kind / chat_id

def test_a_direct_message_lands_as_direct_not_dm():
    """★ 真 bug ①：私聊写 ``"dm"`` ⇒ ENUM 拒收 ⇒ **私聊一条都落不下来**。

    判据钉在**写下去的列值**上：必须是 ``direct``（ENUM 里有的那个）。
    """
    conn = _Recorder()
    assert P.persist_inbound(_Acct(), _dm_msg(), conn=conn) is True
    cols = _cols(conn.only[1])
    assert cols["peer_kind"] == "direct", \
        "写 dm 会被 ENUM('direct','group','room') 拒收（1265），且被吞成一条 warn"
    assert cols["chat_id"] is None, "私聊 chat_id 必须是 NULL（列注释：群 ID）"


def test_a_group_message_uses_the_bare_group_id_as_chat_id():
    """★ 真 bug ②：``chat_id`` 写编码串（``accountId:peerId``）⇒ 按群查历史查不到。

    ``list_group_messages`` 的判据是 ``chat_id = <裸群 id>`` ⇒ 这里必须是**裸** id。
    """
    conn = _Recorder()
    P.persist_inbound(_Acct(), _group_msg(), conn=conn)
    cols = _cols(conn.only[1])
    assert cols["chat_id"] == "111@chatroom"
    assert ":" not in (cols["chat_id"] or ""), "带 ':' 就是编码串，按群查历史会整个查不到"


def test_db_peer_kind_maps_both_message_layer_names():
    """映射函数本身：``group`` 不变，其余（``dm``/空/怪值）一律 ``direct``。"""
    assert P.db_peer_kind("group") == "group"
    assert P.db_peer_kind("dm") == "direct"
    assert P.db_peer_kind("") == "direct"
    assert P.db_peer_kind(None) == "direct"
    assert P.db_peer_kind("GROUP") == "group"


def test_db_chat_id_only_for_groups():
    assert P.db_chat_id("111@chatroom", is_group=True) == "111@chatroom"
    assert P.db_chat_id("wxid_bob", is_group=False) is None
    assert P.db_chat_id("", is_group=True) is None


# ============================================= 3. direction：ENUM 只认两个值

@pytest.mark.parametrize("raw,want", [
    ("incoming", "inbound"),      # 解析器默认值 —— 直写会被 ENUM 拒收
    ("", "inbound"),
    ("inbound", "inbound"),
    ("outgoing", "outbound"),
    ("outbound", "outbound"),
])
def test_direction_maps_to_the_enum(raw, want):
    assert P.db_direction(raw) == want


# ============================================= 4. new_msg_id：从 raw 取，不冒充

def test_the_new_msg_id_comes_from_the_raw_payload():
    msg = _group_msg(NewMsgId=7483920192837465)
    assert P.pick_new_msg_id(msg.raw) == "7483920192837465"
    assert P.to_db_record("default", msg)["new_msg_id"] == "7483920192837465"


def test_a_wrapped_new_msg_id_is_unwrapped():
    """business callback 会把每个字段包成 ``{"string": …}``；不解包就是**假 id**。"""
    msg = _group_msg()
    msg.raw["NewMsgId"] = {"string": "555"}
    assert P.pick_new_msg_id(msg.raw) == "555"


def test_the_other_spellings_are_recognised():
    msg = _group_msg()
    for key in ("newMsgId", "new_msg_id", "newmsgid"):
        msg.raw.pop("NewMsgId", None)
        msg.raw[key] = "777"
        assert P.pick_new_msg_id(msg.raw) == "777", key
        msg.raw.pop(key)


def test_a_missing_new_msg_id_is_empty_not_the_client_id():
    """★ 取不到就写空串 —— **绝不**退回 ``msg_id``。

    退回就把客户端 id 冒充成服务端 id：群友引用 bot 消息带的是 svrid，
    冒充了**永远匹配不上**（``_persist_outbound`` 的 docstring 记着这条）。

    ⚠️ 写空串而不是 NULL：``uk_account_msg`` 含 ``new_msg_id``，而 MySQL 的 UNIQUE
    **允许多行含 NULL** ⇒ 同一 (account, msg_id, NULL) 可以重复入库（三通道重放时
    去重整个失效）。``save_message`` 再把空串原样写进唯一键。
    """
    msg = _dm_msg()                     # 这条 payload 里没有任何 NewMsgId 拼法
    rec = P.to_db_record("default", msg)
    assert rec["new_msg_id"] is None, "上层保持 None（没人用的字段别伪造值）"
    assert rec["msg_id"] == "m-2", "msg_id 照旧写客户端 id"


def test_save_message_normalizes_a_missing_new_msg_id_into_the_unique_key():
    """★ 落库那一层必须把 None 变成 ``''``（NULL 绕过唯一键 = 去重失效）。"""
    from plugin.data import repo_messages as MSG
    from plugin.data import db as _db

    class _Conn:
        def __init__(self) -> None:
            self.params = None

        def execute(self, sql, params):
            self.params = params
            return 1

    conn = _Conn()
    MSG.save_message(
        "acct", direction="inbound", peer_kind="direct", peer_id="wxid_x",
        msg_id="m1", new_msg_id=None, conn=conn,  # type: ignore[arg-type]
    )
    assert conn.params[2] == "", "唯一键那一列必须是空串（NULL 不参与唯一性比较）"


def test_the_no_id_fallback_msg_id_is_still_persisted():
    """无 id 的 vendor 消息：解析器会合成一个 id（``parser.py:302-308``），照落。

    这类行去重不了（每次合成都不同）—— 这是本树既有的"宁可重放、不可静默吞"，
    落库沿用同一条；但**不能留空串**（空串会让所有无 id 消息共用去重键）。
    """
    src = {"fromWxid": "wxid_carol", "content": "接龙", "MsgType": 1, "ts": 1700000009}
    msg = parse_message("default", src)
    assert msg is not None and msg.msg_id
    assert P.to_db_record("default", msg)["msg_id"] == msg.msg_id


# ============================================= 5. 永不抛（TS enrich 的同款语义）

def test_a_db_failure_becomes_a_warning_not_an_exception(monkeypatch, caplog):
    """★ 落库失败**只 warn** —— 收信链路不许因为"审计写不进去"而断。"""
    from plugin.data import repo_messages

    def _boom(*a, **kw):
        raise RuntimeError("1265 Data truncated for column 'peer_kind'")

    monkeypatch.setattr(repo_messages, "save_message", _boom)
    with caplog.at_level(logging.WARNING):
        assert P.persist_inbound(_Acct(), _dm_msg()) is False
    assert any("入站落库失败" in r.getMessage() for r in caplog.records)


def test_a_broken_message_object_also_just_warns():
    """``msg`` 本身怪（缺属性）也不许抛 —— 同样的判据。"""
    class Broken:
        @property
        def is_group(self):
            raise ValueError("boom")

    assert P.persist_inbound(_Acct(), Broken()) is False


# ==================================================== 6. 适配器接线（AST 形状）

def _adapter_src() -> str:
    return (ROOT / ADAPTER_REL).read_text(encoding="utf-8")


def _methods() -> dict[str, ast.AST]:
    """类方法 + **模块级函数**（2026-10-18 起落库本体提成模块级
    ``_persist_outbound_record``，由 live/cron/工具出站共用；断言仍看它）。"""
    module_fn: dict[str, ast.AST] = {}
    cls_fn: dict[str, ast.AST] = {}
    for node in ast.parse(_adapter_src()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_fn[node.name] = node
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            cls_fn = {n.name: n for n in node.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not cls_fn:
        raise AssertionError(f"{ADAPTER_REL} 里找不到 class {ADAPTER_CLS}")
    return {**module_fn, **cls_fn}


def _code(node: ast.AST) -> str:
    """函数体源码（**去 docstring** —— docstring 里写满了反例，搜不得）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def test_handle_one_persists_before_the_hf_buffer():
    """★ ④z 的位置：**在 ``should_drop`` 之后**（否则黑名单群/非白名单会话也落库，
    隐私口径就破了）、**在 ``_hf_note_group`` 之前**（心流缓冲与 judge 从这里取粮）。"""
    lines = [_l for _l in _adapter_src().splitlines()]
    drop_line = next(i for i, l in enumerate(lines) if "should_drop(ok, reason)" in l)
    # 2026-10-18 起落库挪进了线程池：``asyncio.to_thread(persist_inbound, acct, msg)``。
    persist_line = next(i for i, l in enumerate(lines)
                        if "persist_inbound" in l and "acct, msg" in l)
    hf_line = next(i for i, l in enumerate(lines) if "_hf_note_group(acct, msg" in l)
    assert drop_line < persist_line < hf_line, \
        f"顺序不对：should_drop@{drop_line} persist@{persist_line} hf@{hf_line}"


def test_persist_is_not_called_on_the_deny_path():
    """AST 判据：``persist_inbound`` 必须挂在 ``if should_drop(...): return`` **之后**。

    比的是**顶层语句的下标**（不是字符串位置 —— ``ast.unparse`` 一个嵌套节点会丢掉
    外面的缩进，字符串查找会假红）。
    """
    node = _methods()["_handle_one"]
    stmts = [s for s in node.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]          # 去掉 docstring
    guard = next(s for s in stmts
                 if isinstance(s, ast.If) and "should_drop" in ast.unparse(s.test))
    assert any(isinstance(s, ast.Return) for s in guard.body), "那道门必须早退"
    assert "persist_inbound" not in ast.unparse(guard), \
        "落库不许挂在'被丢弃'这一支里（那样黑名单群/非白名单会话也会入库）"
    i_guard = stmts.index(guard)
    i_persist = next(i for i, s in enumerate(stmts) if "persist_inbound" in ast.unparse(s))
    assert i_guard < i_persist, f"顺序不对：门@{i_guard} 落库@{i_persist}"


def test_persist_outbound_uses_the_shared_mapping():
    """★ 出站那两个真 bug 的收口：``peer_kind``/``chat_id`` 必须走**同一对**函数。

    两处各写一份就是漂移的起点（W1 之前正是各写一份，且两份都错）。
    """
    # 映射本体在模块级 ``_persist_outbound_record``（live/cron/工具出站共用）
    code = _code(_methods()["_persist_outbound_record"])
    assert "db_peer_kind(" in code, "出站也必须映射（写 dm 会被 ENUM 拒收）"
    assert "db_chat_id(" in code, "出站必须写裸群 id（写 accountId:peerId 就查不到）"
    assert "self._chat_id(" not in code, "退回编码串 = 出站群消息对按群查历史隐形"
    assert '"dm"' in code or "'dm'" in code, \
        "调用点照旧写消息层口径 'dm'，由 db_peer_kind 映射 —— 写死 'direct' 就绕过了收口"
