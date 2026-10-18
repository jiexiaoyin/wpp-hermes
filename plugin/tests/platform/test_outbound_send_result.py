"""发送结果解析回归（``send_ok`` / ``extract_outbound_msg_ids``）—— 2026-10-01 补。

## 为什么这个文件是"补"而不是"新功能"

这两条判据本树**一条都没有**，而蓝本有、且都是**真实故障复盘**换来的：

1. ``v1.5.6 SEND-LIST-RET``（蓝本 ``src/dispatch/outbound.ts:40 isSendOk``，
   已在**部署产物** ``dist/send/msg.js:99`` 里）：vendor 逐条结果在 ``Data.List[].Ret``，
   ``Code=0 + BaseResponse.ret=0`` 但 ``List[0].Ret=-2`` ⇒ **消息实际被拒收**
   （2026-09-09 某业务晨报 30362 群根本没收到）。只看 ``BaseResponse.ret`` 就会把
   "被拒收"当成功 ⇒ 报成功 + 落**幽灵**出站记录 + 台账写 ``sent`` + **白消费一次发言预算**。
2. ``v1.3.57 P1-2``（蓝本 ``src/dispatch/outbound.ts:261``）：**「vendor 响应在
   ``Data.List[0].NewMsgId``，顶层 msgId 是 undefined」** ⇒ 不读 ``List`` 就永远取不到 id。
   真机表现（2026-10-01 两次发送）：台账 ``bot_msg_id`` 与 ``wpp_messages.msg_id``
   全是 NULL —— **静默降级，一句日志都没有**。

失效形态都是"全绿但不对"，所以本文件配了变异套件
（``tools/mutation_check.py`` 的 ``outbound-send-result`` / ``adapter-outbound-ids``）：
判据拆掉必须当场红。

## 边界（别误判）

- 网关/vendor **真实响应样本**不在本仓：这里按蓝本语义构造，真机由
  ``tools/probe_adapter_contract.py``（``.183`` 上 70 项真机契约，含本文件这五条）
  与后续真机发送复核。
- ``createTime`` 蓝本也取（撤回要用），本树**没有**撤回路径（``wpp_revoke_msg`` 的 id
  来自调用方参数）⇒ 故意不搬，别当漏项补。
- 适配器接线按 **AST 源码形状**验（``platform/adapter.py`` 本机 import 不了，
  模块级 ``from gateway.config import Platform`` 是 Hermes 独有）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from plugin.platform import outbound as O

#: 仓根（``plugin/tests/platform/...`` 往上三层）
ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


# ============================================================ 1. send_ok：Code 层

def test_code_gate_unchanged():
    assert O.send_ok({"Code": 0}) is True
    assert O.send_ok({"Code": 200}) is True
    assert O.send_ok({"Code": None}) is True       # 老 vendor 不回 Code
    assert O.send_ok({"Code": 500}) is False
    assert O.send_ok({}) is True                   # Code 缺失 —— 既有口径
    assert O.send_ok("nope") is False              # 非 dict


# ============================================================ 2. send_ok：BaseResponse 层

def test_base_response_ret_gate():
    assert O.send_ok({"Code": 0, "Data": {"BaseResponse": {"ret": 0}}}) is True
    assert O.send_ok({"Code": 0, "Data": {"BaseResponse": {"ret": 101}}}) is False
    assert O.send_ok({"Code": 0, "Data": {"BaseResponse": {"ret": "0"}}}) is True   # 本树宽松口径
    assert O.send_ok({"Code": 0, "Data": {"BaseResponse": {"ret": None}}}) is True  # 字段缺失
    assert O.send_ok({"Code": 0, "Data": {}}) is True
    assert O.send_ok({"Code": 0, "Data": "nope"}) is True


# ============================================================ 3. send_ok：★ 逐条 Ret

def test_list_ret_all_zero_is_success():
    resp = {"Code": 0, "Data": {"List": [{"Ret": 0}, {"Ret": 0}]}}
    assert O.send_ok(resp) is True


def test_list_ret_missing_entries_are_skipped():
    """缺 ``Ret`` 的条目**不算失败**（兼容不回该字段的 vendor）。"""
    assert O.send_ok({"Code": 0, "Data": {"List": [{"MsgId": 1}, {"Ret": 0}]}}) is True


def test_list_ret_negative_is_rejection():
    """★ v1.5.6 的原样场景：``Code=0`` + ``BaseResponse.ret=0`` 但 ``List[0].Ret=-2``。

    只看 ``BaseResponse`` 会判成功 —— 而群里**根本没收到**。
    """
    resp = {"Code": 0,
            "Data": {"BaseResponse": {"ret": 0}, "List": [{"Ret": -2, "NewMsgId": 5}]}}
    assert O.send_ok(resp) is False


def test_list_any_nonzero_is_rejection():
    resp = {"Code": 0, "Data": {"List": [{"Ret": 0}, {"Ret": -2}]}}
    assert O.send_ok(resp) is False


def test_list_wins_over_base_response():
    """★ 有 ``List`` 时**不再看** ``BaseResponse.ret``（蓝本语义，逐条结果就是终审）。

    这条**故意**让 ``BaseResponse.ret=101`` 不生效 —— 否则两条判据会互相打架，
    谁先谁说都行，那就等于没有判据。
    """
    resp = {"Code": 0, "Data": {"BaseResponse": {"ret": 101}, "List": [{"Ret": 0}]}}
    assert O.send_ok(resp) is True


def test_empty_or_non_list_falls_back_to_base_response():
    """空 ``List`` / 不是 list ⇒ 退回 ``BaseResponse``（不能因为"有个空 List"就免检）。"""
    assert O.send_ok({"Code": 0, "Data": {"List": [], "BaseResponse": {"ret": 101}}}) is False
    assert O.send_ok({"Code": 0, "Data": {"List": "nope", "BaseResponse": {"ret": 101}}}) is False


def test_oddly_shaped_list_items_do_not_fail_a_success():
    """非 dict 条目没有 ``Ret`` 可判 ⇒ 跳过（形状怪异不该把成功判成失败）。"""
    assert O.send_ok({"Code": 0, "Data": {"List": ["x", None, {"Ret": 0}]}}) is True


# ============================================================ 4. ★ 取 id：List[0]

def test_new_msg_id_comes_from_list_head():
    """★ v1.3.57 的原样场景：id 只在 ``Data.List[0]``，**顶层没有**。

    旧实现不看 ``List`` ⇒ 这里取到 ``None``（真机上就是那个 NULL）。
    """
    resp = {"Code": 0, "Data": {"List": [{"NewMsgId": 7483920192837465}]}}
    ids = O.extract_outbound_msg_ids(resp)
    assert ids.new_msg_id == "7483920192837465"
    assert ids.msg_id == "7483920192837465"
    assert ids.client_msg_id is None


def test_client_msg_id_wins_over_new_msg_id():
    """★ 蓝本 ``msgId = clientMsgId ?? newMsgId`` —— **客户端 id 优先**，别写反。"""
    resp = {"Code": 0, "Data": {"List": [{"NewMsgId": 111, "ClientMsgid": 222}]}}
    ids = O.extract_outbound_msg_ids(resp)
    assert ids.msg_id == "222"
    assert ids.new_msg_id == "111"
    assert ids.client_msg_id == "222"


def test_list_head_msg_id_is_a_client_id():
    """``List[0].MsgId`` 归到 client 一侧（蓝本如此），不归 ``new_msg_id``。"""
    resp = {"Code": 0, "Data": {"List": [{"MsgId": 333}]}}
    ids = O.extract_outbound_msg_ids(resp)
    assert ids.client_msg_id == "333"
    assert ids.new_msg_id is None
    assert ids.msg_id == "333"


def test_legacy_top_level_fields_still_work():
    """旧实现认的字段**一个都不许丢** —— .183 上 ``probe_adapter_contract`` 直接验它们。"""
    assert O.extract_msg_id({"Data": {"NewMsgId": 7}}) == "7"
    assert O.extract_msg_id({"Data": {"Newmsgid": 8}}) == "8"
    assert O.extract_msg_id({"Data": {"Msgid": 9}}) == "9"
    assert O.extract_msg_id({"Data": {"BaseResponse": {"NewMsgId": 10}}}) == "10"
    assert O.extract_msg_id({"Data": {"msgId": 11}}) == "11"


def test_list_head_wins_over_legacy_top_level():
    """同时存在时以 ``List[0]`` 为准（蓝本链的前项优先）。"""
    resp = {"Data": {"NewMsgId": 1, "List": [{"NewMsgId": 2}]}}
    assert O.extract_msg_id(resp) == "2"


def test_missing_or_odd_shapes_yield_none():
    assert O.extract_outbound_msg_ids({"Code": 0}) == O.OutboundMsgIds(None, None, None)
    assert O.extract_outbound_msg_ids({"Data": "nope"}) == O.OutboundMsgIds(None, None, None)
    assert O.extract_outbound_msg_ids("nope") == O.OutboundMsgIds(None, None, None)
    assert O.extract_msg_id({}) is None


def test_zero_and_empty_are_not_ids():
    """``0`` / ``""`` 不成其为消息 id —— 不能让它们顶掉后面的真候选。"""
    assert O.extract_msg_id({"Data": {"List": [{"NewMsgId": 0}]}}) is None
    assert O.extract_msg_id({"Data": {"NewMsgId": 0, "Newmsgid": 5}}) == "5"
    assert O.extract_msg_id({"Data": {"NewMsgId": ""}}) is None


# ============================================================ 5. 适配器接线（AST）

def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _methods() -> dict[str, ast.AST]:
    """类方法 + **模块级函数**（2026-10-18 起落库本体提成模块级
    ``_persist_outbound_record``，由 live/cron/工具出站共用；断言仍看它）。"""
    module_fn: dict[str, ast.AST] = {}
    cls_fn: dict[str, ast.AST] = {}
    for node in ast.parse(_src(ADAPTER_REL)).body:
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


def test_send_chunk_extracts_ids_once():
    """★ ``_send_chunk`` 必须调 ``extract_outbound_msg_ids``（**一次**取全），
    不能退回只取单值的 ``extract_msg_id`` —— 那样 ``new_msg_id`` 就永远丢。"""
    node = _methods()["_send_chunk"]
    assert "extract_outbound_msg_ids" in _code(node)
    assert "extract_msg_id(" not in _code(node), "别退回单值版（会丢掉 new_msg_id）"


def test_send_chunk_passes_ids_to_persist():
    """传的必须是**整个 ids 对象** —— 只传 ``ids.msg_id`` 就把 ``new_msg_id`` 丢在门外了。"""
    assert "_persist_outbound(acct, peer_id, chunk, ids)" in _code(_methods()["_send_chunk"])


def test_persist_outbound_stores_server_id():
    """★ 落库取法照搬蓝本 ``msg.ts:121``：``msg_id = newMsgId ?? msgId``、``new_msg_id = newMsgId``。

    群友引用 bot 消息带的是 ``svrid``（≙ ``newMsgId``）—— 落成客户端 id 就永远匹配不上。
    """
    code = _code(_methods()["_persist_outbound_record"])
    assert "new_msg_id=ids.new_msg_id" in code, "必须把 new_msg_id 落进库"
    assert "ids.new_msg_id or ids.msg_id" in code, "msg_id 必须服务端 id 优先"


def test_send_chunk_reports_msg_id_as_message_id():
    """``SendResult.message_id`` = 蓝本 ``msgId``（``clientMsgId ?? newMsgId``）——
    心流台账的 ``bot_msg_id`` 就是从这条线来的。"""
    code = _code(_methods()["_send_chunk"])
    assert "message_id=ids.msg_id" in code
