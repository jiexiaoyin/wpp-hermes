"""出站闸（``WECHATPRO_SEND_ALLOWLIST``）必须**盖住工具面**（C1/C5）。

## 这条线为什么是 P0

``WECHATPRO_SEND_ALLOWLIST`` 是老板指定的调试保险："微信是生产环境，发出去撤不回"。
修前它只挂在**适配器自己**的三条发送路径上（``send_message`` / ``send_media`` /
``_standalone_send``），而模型能调的工具面 —— ``plugin/toolkit/msg.py`` 的发送工具 +
``plugin/toolkit/registry.py`` 那张 219 条 vendor 端点表 —— **一次都不判定**。于是
"保险"是假的：调试窗口里把白名单设成测试群，模型仍可经 ``wpp_msg_send_txt`` 或
``/Finder/FinderSendText`` 往生产群发消息，日志一切正常。

修法不是"再补几处调用"（下次新增工具照样忘），而是**收口到唯一的厂商请求出处**
``WppClient._raw_url``：任何工具、任何路径，只要发请求就经过它。

## 本文件验四件事

1. **分类判据**（只读 vs 写）：含三个必须记住的反例（见下面用例）；
2. **端点表的完整性**：218 条端点里凡是"把内容发出去"的，一条都不许被判成只读；
3. **目标提取**：单值/多值/嵌套都要抓到，``blackList`` 这类"不是收件人"的键不许误抓；
4. **接线**：``_raw_url`` 处调用判据、三个 helper 都把请求体传进去、且全仓再无第二处
   自行拼厂商 URL 的地方（否则收口就是口号）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from plugin.core import netguard, outbound_guard
from plugin.core.api_client import WppClient
from plugin.core.outbound_guard import (
    READ_WORDS, TARGET_KEYS, WRITE_WORDS, camel_words, guard, is_read_only, targets_in,
)

ROOT = Path(__file__).resolve().parents[2]
ENV = netguard.SEND_ALLOWLIST_ENV

#: 一个**只读**端点、一个**写**端点 —— 大部分用例只关心这两类。
READ_EP = "/Msg/Sync"            # 入站轮询：调试期也必须能跑
WRITE_EP = "/Msg/SendTxt"        # 发消息

ALLOWED = "room_test@chatroom"
OUTSIDE = "room_prod@chatroom"


@pytest.fixture
def allowlist(monkeypatch):
    """把白名单设成 ``room_test@chatroom``（and 返回设置/清空的小工具）。"""
    def _set(raw: str) -> None:
        monkeypatch.setenv(ENV, raw)
    _set(ALLOWED)
    return _set


# ===== 1. 只读 / 写 分类 =====


@pytest.mark.parametrize("endpoint,want_read", [
    # --- 读：调试期必须放行，否则连"活着没"都问不出来 ---
    ("/Msg/Sync", True),
    ("/Login/HeartBeat", True),
    ("/Login/AutoHeartBeat", True),
    ("/Login/GetLoginStatus", True),
    ("/User/GetOnlineInfo", True),
    ("/Tools/CdnDownloadImage", True),
    ("/Tools/DownloadFileBinary", True),
    ("/Voice/Result", True),
    ("/Group/GetChatRoomInfo", True),
    ("/Search/Images", True),
    ("/OfficialAccounts/ArticleMarkdown", True),
    # --- 写/发送：**必须**受闸 ---
    ("/Msg/SendTxt", False),
    ("/Msg/UploadImg", False),
    ("/Msg/ShareCard", False),
    ("/Msg/SendGroupMassMsgText", False),
    ("/FriendCircle/UploadVideo", False),
    ("/Friend/SendRequest", False),
    ("/Group/InviteChatRoomMember", False),
    ("/Webhook/Set", False),
    ("/TenPay/OpenHongBaoWithParams", False),
    ("/Webhook/Remove", False),
    ("/Label/Add", False),
    ("/User/SetAlisa", False),
])
def test_is_read_only(endpoint: str, want_read: bool):
    assert is_read_only(endpoint) is want_read


def test_a_read_sounding_name_that_actually_sends_is_guarded():
    """``FinderSendText`` 词面以 ``Find`` 开头（像读词）。

    只看"首词是不是读词"的写法会把它判成只读 ⇒ 调试期能往任意人发私信。写词优先正是
    为了堵这个：命中 ``Send`` 即判写。
    """
    words = camel_words("/Finder/FinderSendText")
    assert "Send" in words and "Find" not in words, f"词切得不对：{words}"
    assert is_read_only("/Finder/FinderSendText") is False


def test_a_read_sounding_name_that_retargets_sync_is_guarded():
    """``/Msg/StartAutoSync`` 含 ``Sync``（像读），但它把同步目标指向任意 ``TargetURL``。

    判成只读 ⇒ 调试期能把消息流引到外部地址（等于把消息搬走），所以 ``Start`` 是写词。
    """
    assert is_read_only("/Msg/StartAutoSync") is False


def test_unknown_words_default_to_guarded():
    """两边的词都不认识 ⇒ 判**写**（fail-closed），不是判读。"""
    assert is_read_only("/X/Y/Zzzyx") is False


def test_a_path_with_a_write_word_anywhere_is_guarded():
    """写词在**路径任何一段**都算 —— ``/FriendCircle/SendOneIdCircle`` 的写词在中段。"""
    assert is_read_only("/FriendCircle/SendOneIdCircle") is False


def test_read_and_write_lexicons_do_not_overlap():
    """两个词表不许有交集：有交集的话"写优先"就退化成看遍历顺序。"""
    assert not (READ_WORDS & WRITE_WORDS)


# ===== 2. 端点表完整性（这条是"漏一个就是洞"的机器化） =====

#: 交付类动词 —— 命中它的端点就是把内容发给谁/发到某处，**一律不许**判只读。
_DELIVERY_RE = re.compile(
    r"(Send|Upload|Share|Publish|Post|Forward|Revoke|RedPacket|HongBao|SayHello"
    r"|Comment|Like|Follow|Pat|Invite|Mass)")

#: 关键端点（C1/C5 回归名单）：这些**必须**受闸，任何一条变只读都直接红。
CRITICAL_OUTBOUND = (
    "/Finder/FinderSendText",       # 私信
    "/Msg/UploadImg",               # 上传即发送
    "/Msg/ShareVideo",
    "/Msg/SendApp",
    "/FriendCircle/UploadVideo",    # 朋友圈视频
    "/FriendCircle/UploadImage",
    "/FriendCircle/SendOneIdCircle",
    "/FriendCircle/SendFavItemCircle",
    "/Friend/SendRequest",          # 加好友
    "/Group/InviteChatRoomMember",
    "/Group/SendPat",               # 拍一拍
    "/Translate/Send",
    "/User/SendVerifyMobile",
    "/TenPay/OpenHongBao",          # 抢红包（钱）
    "/Wxapp/JSOperateWxData",
    "/Webhook/Set",                 # 改回调地址
    "/Msg/StartAutoSync",           # 改同步目标
)


def _table_endpoints() -> set[str]:
    """从两张生成表里取全部端点（同时证明"表真的加载了"，防止空跑）。"""
    from plugin.toolkit import vendor_defs, vendor_defs_extra

    eps: set[str] = set()
    for mod in (vendor_defs, vendor_defs_extra):
        for name, val in vars(mod).items():
            if name.startswith("_") or not isinstance(val, (tuple, list)):
                continue
            for td in val:
                if isinstance(td, dict) and "endpoint" in td:
                    eps.add(td["endpoint"])
    return eps


def test_for_every_known_delivery_endpoint_the_guard_applies():
    """218 条端点表里，凡命中交付动词的 —— **一条都不许判只读**。

    单列成一条（而不是只在 CRITICAL_OUTBOUND 上断言），是因为漏的往往不是名单里那几条，
    而是有人后来往表里加的新端点。
    """
    eps = _table_endpoints()
    assert len(eps) >= 200, f"端点表没加载全（只 {len(eps)} 条）—— 这条会变成空断言"
    leaked = sorted(e for e in eps if _DELIVERY_RE.search(e) and is_read_only(e))
    assert leaked == [], f"这些端点会把内容发出去却判成了只读：{leaked}"


def test_critical_outbound_endpoints_are_all_guarded():
    for endpoint in CRITICAL_OUTBOUND:
        assert is_read_only(endpoint) is False, f"{endpoint} 必须受闸"


def test_the_classification_covers_the_whole_table_without_error():
    """两条极端：全表都能分类（不抛），且只读/受闸两侧都非空。"""
    eps = _table_endpoints()
    read = [e for e in eps if is_read_only(e)]
    assert read, "一个只读端点都没有 ⇒ 分类退化成'全拒'，没人敢开这个闸"
    assert len(read) < len(eps), "全都判只读 ⇒ 闸等于不存在"


# ===== 3. 目标提取 =====


@pytest.mark.parametrize("body,want", [
    ({"ToWxid": "wxid_a"}, ("wxid_a",)),
    ({"toWxid": "wxid_a"}, ("wxid_a",)),
    ({"to_wxid": "wxid_a"}, ("wxid_a",)),
    ({"ToWxids": ["a", "b"]}, ("a", "b")),
    ({"ToIds": ["a", "b"]}, ("a", "b")),
    ({"ChatRoomName": "g@chatroom"}, ("g@chatroom",)),
    ({"QID": "g@chatroom", "ToUserName": "wxid_x"}, ("g@chatroom", "wxid_x")),
    ({"ToWxid": "a", "toWxid": "a"}, ("a",)),                 # 去重
    ({"ToWxid": 12345}, ("12345",)),                          # 数字也认
    ({"ToWxid": "  a  "}, ("a",)),                            # 去空白
    ({"Items": [{"ToWxid": "a"}, {"ToWxid": "b"}]}, ("a", "b")),   # 嵌套列表
    ({"Data": {"Items": [{"toWxid": "a"}]}}, ("a",)),
    ({"blackList": ["bystander"]}, ()),                       # 不是收件人
    ({"ToWxid": None, "ToWxids": []}, ()),
    ({"ToWxid": True}, ()),                                   # bool 不当目标
    ({"Content": "hello"}, ()),
])
def test_targets_in(body: dict, want: tuple[str, ...]):
    assert targets_in(body) == want


def test_blacklist_is_not_treated_as_a_target():
    """朋友圈 ``blackList`` 是"**不给谁看**"—— 拿它当收件人会让白名单判反。"""
    assert "blackList" not in TARGET_KEYS
    assert targets_in({"blackList": ["x"], "ToWxid": "y"}) == ("y",)


def test_targets_are_capped():
    """病态载荷不许把闸变成 CPU 放大器。"""
    many = {"ToWxids": [f"u{i}" for i in range(500)]}
    assert len(targets_in(many)) <= 64


# ===== 4. 判据本身 =====


def test_no_allowlist_means_no_op(monkeypatch):
    """**生产行为必须一字不变**：没设白名单时，往哪发都不拦（与 TS 同行为）。"""
    monkeypatch.delenv(ENV, raising=False)
    guard(WRITE_EP, {"ToWxid": OUTSIDE}, "acct")          # 不抛
    guard("/Webhook/Set", {"url": "https://evil"}, "acct")  # 不抛


def test_allowlisted_peer_passes(allowlist):
    guard(WRITE_EP, {"ToWxid": ALLOWED}, "acct")


def test_account_scoped_form_passes(allowlist):
    """白名单里可以写 ``acct:peer``（``netguard`` 的既有语义，别在收口处丢掉）。"""
    allowlist(f"acct:{ALLOWED}")
    guard(WRITE_EP, {"ToWxid": ALLOWED}, "acct")


def test_peer_outside_the_allowlist_raises(allowlist):
    with pytest.raises(netguard.GuardError):
        guard(WRITE_EP, {"ToWxid": OUTSIDE}, "acct")


def test_one_target_outside_is_enough_to_reject(allowlist):
    """多目标（改标签、拉人进群）里只要有一个在白名单外 ⇒ 整条拒绝。"""
    with pytest.raises(netguard.GuardError):
        guard("/Label/UpdateList", {"LabelID": "1", "ToWxids": [ALLOWED, OUTSIDE]}, "acct")


def test_read_endpoint_is_exempt(allowlist):
    """只读端点不受闸 —— 否则调试期心跳/同步/状态查询全被自己拒掉，闸就没人敢开。"""
    guard(READ_EP, {}, "acct")
    guard("/Login/HeartBeat", {}, "acct")
    guard("/Group/GetChatRoomInfo", {"QID": OUTSIDE}, "acct")


def test_write_endpoint_without_a_target_is_rejected(allowlist):
    """说不清发给谁/对谁操作 ⇒ 白名单期一律拒绝（fail-closed）。"""
    for endpoint, body in (("/Friend/SendRequest", {"v1": "x", "v2": "y"}),
                           ("/Webhook/Set", {"url": "https://evil"}),
                           ("/FriendCircle/UploadImage", {"imageData": "AAA"}),
                           ("/TenPay/OpenHongBaoWithParams", {"SendId": "x", "Xml": "<xml/>"})):
        with pytest.raises(netguard.GuardError) as caught:
            guard(endpoint, body, "acct")
        assert "没有任何可核对的目标" in str(caught.value), endpoint


# ===== 5. 接线：收口是真的收口 =====

API_CLIENT = ROOT / "plugin" / "core" / "api_client.py"


def _client_src() -> str:
    return API_CLIENT.read_text(encoding="utf-8")


def _method_code(src: str, cls: str, name: str) -> str:
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                    body = list(fn.body)
                    if body and isinstance(body[0], ast.Expr) \
                            and isinstance(body[0].value, ast.Constant) \
                            and isinstance(body[0].value.value, str):
                        body = body[1:]
                    return "\n".join(ast.unparse(s) for s in body)
    raise AssertionError(f"找不到 {cls}.{name}")


def test_the_guard_is_called_in_the_single_url_builder():
    code = _method_code(_client_src(), "WppClient", "_raw_url")
    assert "outbound_guard.guard(endpoint, body, self.account_id" in code, \
        "判据必须挂在 _raw_url 上（所有厂商请求的唯一出处）"
    assert "allowed=self.send_allowlist" in code, \
        "必须用连接期捕获的白名单，不能在发送任务里重新读 ambient env"
    assert "path_targets=path_targets" in code, \
        "路径目标必须交给闸核对"


def test_every_request_helper_passes_the_body_into_it():
    """三个 helper 都必须把**请求体**传进 ``_raw_url`` —— 不传 body 就取不到目标，
    受闸端点会全部变成"无目标 ⇒ 拒绝"（工具面直接全瘫）。"""
    src = _client_src()
    for name in ("_request", "_post_bytes", "_post_json_b64"):
        code = _method_code(src, "WppClient", name)
        assert "_raw_url(endpoint, body" in code, f"{name} 没把 body 传给 _raw_url"


def test_raw_url_is_the_only_place_that_builds_a_vendor_url():
    """全仓只允许一处拼厂商 URL（``{self.base_url}/api…``）。

    别处出现第二处 = 收口是口号：那条路既不判白名单，也不带审计。
    """
    hits = []
    for path in (ROOT / "plugin").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "__pycache__" in rel or rel.startswith("plugin/tests/"):
            continue          # 测试自己会引用这个串（本条断言用的就是它）
        if "{self.base_url}" in path.read_text(encoding="utf-8"):
            hits.append(rel)
    assert hits == ["plugin/core/api_client.py"], f"别处也在拼厂商 URL：{hits}"


def test_tool_surface_reaches_the_client_call_entrypoint():
    """工具面两条路都必须落在 ``WppClient`` 上（否则收口空转）：

    * 通用端点表 handler → ``client.call(endpoint, …)``；
    * msg 域工具 → ``client.send_*``（typed 方法内部都走 ``call``）。
    """
    registry = (ROOT / "plugin" / "toolkit" / "registry.py").read_text(encoding="utf-8")
    assert registry.count("await client.call(") == 2, \
        "两个通用 handler（generic_handler / register_generic_tools）都必须经 client.call"
    msg = (ROOT / "plugin" / "toolkit" / "msg.py").read_text(encoding="utf-8")
    # quote_reply 现在走 ``client.call("/Msg/ShareLink", …)``（type=57 引用卡片），
    # 不再是 ``client.send_text`` ⇒ typed 调用点从 8 降到 7。
    assert msg.count("client.send_") >= 7, "msg 域发送工具必须经 typed 方法"


def test_call_funnels_into_request():
    code = _method_code(_client_src(), "WppClient", "call")
    assert "self._request(" in code, "call 必须汇聚到 _request（否则审计/闸都漏）"


def test_end_to_end_through_the_client_without_network(allowlist):
    """真造一个 ``WppClient`` 调 ``_raw_url``（不联网）：白名单外直接抛，白名单内出 URL。"""
    client = WppClient(base_url="https://vendor.test", authcode="fake", account_id="acct")
    with pytest.raises(netguard.GuardError):
        client._raw_url("/Msg/SendTxt", {"ToWxid": OUTSIDE, "Content": "hi"})
    url = client._raw_url("/Msg/SendTxt", {"ToWxid": ALLOWED, "Content": "hi"})
    assert url == "https://vendor.test/api/Msg/SendTxt?authcode=fake"
    # 账号作用域形式：只有把 account_id 真的传进判据才对得上（多账号下按账号隔离）
    allowlist(f"acct:{ALLOWED}")
    client._raw_url("/Msg/SendTxt", {"ToWxid": ALLOWED, "Content": "hi"})


def test_end_to_end_without_allowlist_is_unaffected(monkeypatch):
    """没白名单时 URL 形状**逐字不变**（生产路径零改动）。"""
    monkeypatch.delenv(ENV, raising=False)
    client = WppClient(base_url="https://vendor.test", authcode="fake", account_id="acct")
    assert client._raw_url("/Msg/SendTxt", {"ToWxid": OUTSIDE}) == \
        "https://vendor.test/api/Msg/SendTxt?authcode=fake"
