"""账号模型与解析 —— 旧 ``config.py`` + ``adapter._build_accounts()`` 的合并重写。

## 解析链（**顺序即优先级，后者覆盖前者**）

======  =============================================================  ========
层      来源                                                           性质
======  =============================================================  ========
①       ``plugin/accounts/<id>.json``                                 静态配置
②       ``config.yaml`` → ``platforms.wechatpadpro.extra.accounts.<id>``  运维覆盖
③       runtime override（filehelper 命令 ``set_override``）            运行期热改
④       env 凭据兜底（``authcode`` / ``tokenKey`` / ``webhookSecret``…） 机密不落盘
======  =============================================================  ========

⚠️ 旧 ``config.py`` 的**模块 docstring 把顺序写反了**（写成 env > yaml > file），
代码实际是上表这个顺序。以**代码**为准，那份注释是错的。

## 与 ``core.credentials`` 的分工（**别把两件事混在一起**）

- ``credentials`` 回答「**有没有**凭据」—— **永不 raise**，Hermes 的凭证门用它。
- ``accounts``   回答「**这个账号能不能用**」—— 配置错就 **raise**，连接前用它。

两者混用会让凭证门因一条坏配置直接炸掉，这正是要避免的。

## 从旧实现原样继承的三条业务规则

1. **无 ``authcode`` ⇒ 该账号不参与连接**（warn，不 raise —— 随仓库分发的空壳
   ``default.json`` 就是这个状态，不能因此让插件起不来）。
2. **``agent`` 必填、且禁止 ``"main"``** ⇒ **raise**。2026-09-01 老板拍板，
   防多账号串号（fallback 到 main 会让两个微信号共用一个 agent 上下文）。
3. **``self.token = 首个账号的 authcode``** ⇒ 供 Hermes 的 multiplex 冲突检测
   （``gateway.run._adapter_credential_fingerprint`` 读 ``token``）识别
   「同一 authcode 被两个 adapter 连」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: ``plugin/`` 目录（本文件在 ``plugin/core/`` 下）
PLUGIN_DIR = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = PLUGIN_DIR / "accounts"

#: 禁止的 agent 名 —— fallback 到 main 会让多账号共用一个 agent 上下文（串号）
FORBIDDEN_AGENT = "main"

#: ⚠️ 生产 bot「益融小助理」。以它为身份的实例**绝不能连厂商** ——
#: 厂商的客户端实例锁会把生产踢下线 + webhook 被劫持（红线 2）。
PRODUCTION_WXID = "wxid_eezdbu1ytws422"
#: 逃生阀：确需在受控环境下以生产身份连接时才置 1（**默认禁止**）
ALLOW_PRODUCTION_ENV = "WECHATPRO_ALLOW_PRODUCTION_WXID"

#: ⚠️ 生产实例**凭据的指纹**（``sha256(authcode)[:12]``，与 :func:`fingerprint` 和厂商的
#: 客户端实例锁**同款口径**）。只存**单向指纹**，不存密钥：它不可用于连接，只用于
#: "这把锁是不是生产那把"的比对（运维还能拿它跟厂商面板上的实例锁对着看）。
#:
#: **为什么光有 wxid 不够**（P2-a / C4）：``selfWxid`` 是配置里**写的一句话** ——
#: 缺省（"还没填"）、写错（复制了别的账号的）、经 ``WECHATPRO_ACCOUNTS_JSON`` 注入的
#: 空壳，都能让它不再是 ``wxid_eezdbu1ytws422``，而**起决定作用的是 authcode**
#: （厂商按客户端密钥加实例锁）：拿着生产 authcode 连上去 = 把生产踢下线 + 劫持它的
#: webhook（红线 2），此时配置里的 selfWxid 写什么都一样。
#:
#: ⚠️ 生产轮换 authcode 后**必须同步更新这张表**，否则这道门退化成"只认 wxid"
#: （那是 fail-open）。所以 :func:`refusal_reason` 的措辞要把**两种触发**分开写，
#: 让运维一眼看出是哪一道门在拦，别改配置绕过去。
PRODUCTION_CREDENTIAL_FINGERPRINTS = frozenset({
    "654c31808b5f",   # 生产 bot「益融小助理」的 authcode（WECHATPRO_AUTHCODE）
})


class AccountError(RuntimeError):
    """账号配置错误 —— 配置问题必须**当场炸**，不许静默降级。"""


# ====================================================================== 工具


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def fingerprint(secret: str) -> str:
    """凭据指纹 —— **日志里只能出现这个，绝不出现 authcode 本身**。

    与厂商的客户端实例锁同构（``sha256(key)[:12]``），便于运维一眼对上
    「哪个实例占着这把锁」，同时不可反推。
    """
    if not secret:
        return "-"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


# ==================================================================== 账号模型


@dataclass(frozen=True)
class Account:
    """一个已解析完成的账号。字段就是全部配置面 —— 不做 ``__getattr__`` 魔法。

    冻结是为了**防止运行期被就地改**（旧实现 ``self._accounts[id]`` 是可变 dict，
    任何一处写入都会影响别处，是最难查的一类 bug）。要改请走 ``resolve()`` 重建。
    """

    id: str
    agent: str
    enabled: bool

    # -- 凭据（来自 env，绝不落盘） --------------------------------
    # ⚠️ ``repr=False`` 是**安全要求**，不是排版偏好：这个 dataclass 会被
    #    ``log.info("账号 %s 已就绪", acct)`` / f-string / 异常消息顺手打印，
    #    默认 repr 会把 authcode 原样写进 journald 与 ``/tmp`` 日志。
    #    ``redacted()`` 只是**主动调用**时才生效，管不住这些被动路径。
    #    与 ``raw``（同样 repr=False）同一个理由。
    authcode: str = field(repr=False)
    token_key: str = field(repr=False)

    # -- 端点 ------------------------------------------------------
    api_base_url: str
    ws_url: str
    self_wxid: str
    nickname: str

    # -- webhook ---------------------------------------------------
    webhook_host: str
    webhook_port: int
    webhook_path: str
    #: 回调地址里的路径令牌 —— 拿到它就能往 webhook 打请求，同样不许进日志
    webhook_path_token: str = field(repr=False)
    webhook_public_url: str
    webhook_secret: str = field(repr=False)
    webhook_business_path: str
    auto_set_webhook: bool
    set_webhook_retries: int

    # -- 准入策略 --------------------------------------------------
    allow_from: tuple[str, ...]
    group_policy: str
    group_allow_from: tuple[str, ...]
    admin_users: tuple[str, ...]
    blacklist_groups: tuple[str, ...]
    require_at_mention: bool
    #: W7-6 起**真在跑**：到达层聚合窗口（同 key 多条合一批），见 ``inbound/debounce.py``。
    debounce_ms: int

    # -- 特色功能开关 ----------------------------------------------
    heartflow: dict[str, Any]
    affection: dict[str, Any]
    jargon: dict[str, Any]
    llm_intent_enabled: bool
    mcp_enabled: bool
    group_context_enabled: bool
    dm_pairing_enabled: bool
    friend_circle_publish_enabled: bool

    #: 合并后的原始 cfg。工具/热加载需要未建模的键时从这里取。
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------ 派生

    @property
    def has_credentials(self) -> bool:
        """能不能连厂商（只看 authcode）。"""
        return bool(self.authcode)

    @property
    def credential_fingerprint(self) -> str:
        """安全日志用的指纹。"""
        return fingerprint(self.authcode)

    @property
    def is_production_identity(self) -> bool:
        """是不是生产 bot 的身份 —— **wxid 或凭据**任一对上就算（C4）。

        两道判据缺一不可：

        1. ``self_wxid`` 命中生产 wxid（原有那道）；
        2. **凭据指纹**命中生产 authcode（新加的那道）—— 见
           :data:`PRODUCTION_CREDENTIAL_FINGERPRINTS` 的说明：selfWxid 只是配置里的
           一句话，缺省/写错/被 env 注入都能绕开第 1 道，而"连上去会怎样"由 authcode
           决定（厂商按客户端密钥上实例锁）。

        这里**不看 enabled** —— 判定"是不是那个身份"与"这次要不要连"是两件事
        （"要不要连"在 :func:`refusal_reason` / :func:`red_line_reason` 里判）。
        """
        if self.self_wxid == PRODUCTION_WXID:
            return True
        ft = self.credential_fingerprint
        return ft != "-" and ft in PRODUCTION_CREDENTIAL_FINGERPRINTS

    @property
    def webhook_url(self) -> str:
        """拼接对外可达的 webhook URL（未配 public url 时返回空串）。"""
        if not self.webhook_public_url:
            return ""
        return self.webhook_public_url.rstrip("/") + self.webhook_path

    def redacted(self) -> dict[str, Any]:
        """**可安全打印**的账号摘要（跨账号上下文探查用，R1 铁律）。

        只出元信息，authcode / tokenKey / webhookSecret **永不出现**。
        """
        return {
            "account_id": self.id,
            "agent": self.agent,
            "enabled": self.enabled,
            "self_wxid": self.self_wxid,
            "nickname": self.nickname,
            "credential": self.credential_fingerprint,
            "api_base_url": self.api_base_url,
            "allow_from": list(self.allow_from),
            "group_policy": self.group_policy,
            "admin_users": list(self.admin_users),
        }


# ================================================================== 加载与解析


_TRUEY = {"1", "true", "yes", "on"}


def _as_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in _TRUEY


def _as_str_tuple(val: Any) -> tuple[str, ...]:
    if not val:
        return ()
    if isinstance(val, str):
        parts = [p.strip() for p in val.split(",")]
    elif isinstance(val, Iterable):
        parts = [str(p).strip() for p in val]
    else:
        return ()
    return tuple(p for p in parts if p)


def _as_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _as_dict(val: Any) -> dict[str, Any]:
    return dict(val) if isinstance(val, dict) else {}


def is_account_file(path: Path) -> bool:
    """是不是一个**账号**配置文件（而非范例/备份/草稿）。

    旧 ``config.py`` 只滤了 ``.example``；这里把 ``_`` 前缀与编辑器残留一起滤掉
    —— 运维手边放个 ``_notes.json`` 不该被当成账号。
    """
    name = path.name
    if not name.endswith(".json") or name.startswith("_"):
        return False
    if ".example" in name or "_example" in name:
        return False
    if name.endswith((".bak", ".tmp", ".swp")):
        return False
    return True


def accounts_dir() -> Path:
    """账号目录（可用 ``WECHATPRO_ACCOUNTS_DIR`` 覆盖 —— 测试用）。"""
    override = _env("WECHATPRO_ACCOUNTS_DIR")
    return Path(override).expanduser() if override else ACCOUNTS_DIR


def list_account_ids(extra_accounts: dict[str, Any] | None = None) -> list[str]:
    """账号 id 列表：``extra.accounts`` 的键优先，否则扫 ``accounts/*.json``。"""
    if extra_accounts:
        return sorted(str(k) for k in extra_accounts)
    d = accounts_dir()
    if d.is_dir():
        return sorted(p.stem for p in d.iterdir() if p.is_file() and is_account_file(p))
    return []


def load_account_file(account_id: str) -> dict[str, Any]:
    """读 ``accounts/<id>.json``。**坏了要出声** —— 返回空 dict 但带 error 日志。"""
    path = accounts_dir() / f"{account_id}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("[WPP] 账号文件 JSON 解析失败 %s: %s", path, exc)
        return {}
    except OSError as exc:
        log.error("[WPP] 账号文件读取失败 %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.error("[WPP] 账号文件不是对象 %s: %s", path, type(data).__name__)
        return {}
    return data


def _resolve_secret(cfg: dict[str, Any], literal_key: str, *fallback_envs: str) -> str:
    """凭据解析：字面值 → ``<key>Env`` 间接 → 全局 env 兜底。

    ⚠️ 必须**强制覆盖**而非 ``setdefault`` —— ``accounts/default.json`` 里
    ``authcode`` 是**空串占位**，``setdefault`` 见键已存在就不动了，凭据永远填不进去。

    ⚠️ ``<key>Env`` 写了名字却读不到值 ⇒ **出声**（WARNING）。以前是静默落空，
    表现为"账号配了 authcodeEnv 却连不上"，而日志里没有任何一行说这件事。
    """
    literal = str(cfg.get(literal_key) or "").strip()
    if literal:
        return literal
    from .env_compat import secret_env  # 多 profile: 凭据必须走 scoped 口径

    indirect = str(cfg.get(f"{literal_key}Env") or "").strip()
    if indirect:
        got = secret_env(indirect)
        if got:
            return got
        log.warning("[WPP] %sEnv=%s 指向的环境变量为空/未设置",
                    literal_key, indirect)
    for name in fallback_envs:
        got = secret_env(name)
        if got:
            return got
    return ""


def resolve(account_id: str, extra: dict[str, Any] | None = None) -> Account:
    """把四层配置合成为一个 ``Account``。

    只对**确定的配置错误** raise（``agent`` 缺失/为 main）；凭据缺失**不 raise**
    —— 交由 ``connectable()`` 过滤，否则一个空壳账号会让整个插件起不来。
    """
    cfg: dict[str, Any] = dict(load_account_file(account_id))
    if extra:
        cfg.update(extra)

    # ③ runtime override（filehelper 命令热改；缺失/出错时静默跳过，不影响主链）
    #    覆盖存储住在 ``core/overrides.py``（**不是** ``platform/commands.py``）：
    #    配置层不能反过来依赖命令层，见该模块 docstring。
    try:
        from . import overrides  # 延迟导入：避免注册期拉起运行期依赖

        override = overrides.get_override(account_id) or {}
        for key, val in override.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **val}
            else:
                cfg[key] = val
    except Exception as exc:  # noqa: BLE001 —— override 是可选增强
        log.debug("[WPP] 账号 %s 无 runtime override（%s）", account_id, exc)

    # ① 凭据
    #    ★ 全局 env 兜底只对 ``default`` 账号生效（或显式开逃生门）。否则多账号
    #      场景下每个**没配凭据**的账号都会静默继承 ``WECHATPRO_AUTHCODE`` ——
    #      同一个微信身份被两个 agent 同时连，厂商侧互踢，插件侧却毫不知情。
    #      非 default 账号必须自带 authcode / authcodeEnv。
    allow_global = (account_id == "default"
                    or _env_bool("WECHATPRO_ALLOW_GLOBAL_CREDENTIALS"))
    authcode = _resolve_secret(cfg, "authcode",
                               *("WECHATPRO_AUTHCODE",) if allow_global else ())
    token_key = _resolve_secret(cfg, "tokenKey",
                                *("WECHATPRO_TOKEN_KEY",) if allow_global else ())
    webhook_secret = _resolve_secret(cfg, "webhookSecret",
                                     *("WECHATPRO_WEBHOOK_SECRET",)
                                     if allow_global else ())

    # ② agent 校验（raise 而非跳过 —— 串号是安全事故，不是配置瑕疵）
    agent = str(cfg.get("agent") or "").strip()
    if not agent:
        raise AccountError(
            f"账号 {account_id!r} 缺 agent 字段。必须配置专属 agent profile"
            f"（如 'wpp-wechat'），见 accounts/*.example.json。"
        )
    if agent == FORBIDDEN_AGENT:
        raise AccountError(
            f"账号 {account_id!r} 的 agent='main' 被禁止（防多账号串号）。"
            f"请改用专属 profile（如 'wpp-wechat' / 'wpp-wechat-boss2'）。"
        )

    webhook_host = _env("WECHATPRO_WEBHOOK_HOST", str(cfg.get("webhookHost") or "127.0.0.1"))
    # ⚠️ 这里**刻意不给默认域名**。旧 config.py:82 硬编码 `https://hermes.juhe.chat`
    #    作兜底，与 plugin.yaml 自称的 "No default is applied" 自相矛盾；未配就是
    #    空串，由 ``Account.webhook_url`` 显式处理。
    public_url = _resolve_secret(cfg, "webhookPublicUrl", "WECHATPRO_WEBHOOK_PUBLIC_URL")

    # ③ ``ai.judgeModel`` / ``ai.timeoutMs`` 作为三个 LLM 功能的默认值
    #    （逐字对齐 TS ``config-ai.ts:17 resolveAiConfig``）：功能自己配了就优先，
    #    没配才用 ai 块。ts 侧只在运行时合并；py2 在 Account 构建处合并一次 ⇒
    #    心流 / 黑话 / 好感度三条链、以及 ``/heartflow status`` 看到的是同一个生效值。
    _ai = _as_dict(cfg.get("ai"))

    def _with_ai_defaults(section: dict[str, Any]) -> dict[str, Any]:
        merged = dict(section)
        if _ai.get("judgeModel") and not merged.get("model"):
            merged["model"] = _ai["judgeModel"]
        if _ai.get("timeoutMs") and not merged.get("timeoutMs"):
            merged["timeoutMs"] = _ai["timeoutMs"]
        return merged

    return Account(
        id=account_id,
        agent=agent,
        enabled=_as_bool(cfg.get("enabled"), True),
        authcode=authcode,
        token_key=token_key,
        api_base_url=str(cfg.get("apiBaseUrl") or "").strip()
        or _env("WECHATPRO_API_BASE", "https://wx.juhe.chat"),
        ws_url=str(cfg.get("wsUrl") or "").strip()
        or _env("WECHATPRO_WS_URL", "wss://wx.juhe.chat/ws/sync"),
        self_wxid=str(cfg.get("selfWxid") or "").strip(),
        nickname=str(cfg.get("nickname") or "").strip() or "微信助手",
        webhook_host=webhook_host,
        webhook_port=_as_int(cfg.get("webhookPort"), 4398),
        webhook_path=str(cfg.get("webhookPath") or "").strip()
        or f"/wechatpadpro/{account_id}/webhook",
        webhook_path_token=str(cfg.get("webhookPathToken") or "").strip(),
        webhook_public_url=public_url,
        webhook_secret=webhook_secret,
        webhook_business_path=str(cfg.get("webhookBusinessPath") or "").strip()
        or f"/wechatpadpro/{account_id}/webhook/business",
        auto_set_webhook=_as_bool(cfg.get("autoSetWebhook"), True),
        set_webhook_retries=_as_int(cfg.get("setWebhookRetries"), 3),
        allow_from=_as_str_tuple(cfg.get("allowFrom")),
        group_policy=str(cfg.get("groupPolicy") or "open").strip(),
        group_allow_from=_as_str_tuple(cfg.get("groupAllowFrom")),
        admin_users=_as_str_tuple(cfg.get("adminUsers")),
        blacklist_groups=_as_str_tuple(cfg.get("blacklistGroups")),
        require_at_mention=_as_bool(cfg.get("requireAtMention"), False),
        # W7-6 起这个键**真的生效**（到达层聚合窗口）。默认值对齐 TS ``core/constants.ts:62``
        # 的 ``DEFAULT_DEBOUNCE_MS = 500`` —— 本树原先写 1500 是 v1.3.74 **之前**的旧值
        # （2026-08-22 老板拍板降到 500：webhook 到达延迟中位数 < 200ms，等 1.5s 纯属浪费）。
        # 那时这键没人读，写多少都不影响；现在它进了收信链路，**按 TS 的现值**才对齐。
        debounce_ms=_as_int(cfg.get("debounceMs"), 500),
        heartflow=_with_ai_defaults(_as_dict(cfg.get("heartflow"))),
        affection=_with_ai_defaults(_as_dict(cfg.get("affection"))),
        jargon=_with_ai_defaults(_as_dict(cfg.get("jargon"))),
        llm_intent_enabled=_as_bool(cfg.get("llmIntentEnabled"), False),
        # TS ``mcp-meta.ts:mcpEnabledForCurrentAccount`` 是 ``!== false`` —— 缺省**开**，
        # 只有显式 false 才关。旧 py2 解析成默认 false 且没人消费（死键），
        # 2026-10-18 接上 vendor MCP 后按 TS 语义改回默认开。
        mcp_enabled=_as_bool(cfg.get("mcpEnabled"), True),
        group_context_enabled=_as_bool(cfg.get("groupContextEnabled"), False),
        dm_pairing_enabled=_as_bool(cfg.get("dmPairingEnabled"), False),
        friend_circle_publish_enabled=_as_bool(cfg.get("friendCirclePublishEnabled"), False),
        raw=cfg,
    )


def load_all(extra_accounts: dict[str, Any] | None = None) -> dict[str, Account]:
    """解析全部账号 → ``{id: Account}``。

    单个账号配置错**只跳过它自己**并记 error —— 一个坏账号不该让整插件起不来。
    """
    out: dict[str, Account] = {}
    for account_id in list_account_ids(extra_accounts):
        try:
            out[account_id] = resolve(account_id, (extra_accounts or {}).get(account_id))
        except AccountError as exc:
            log.error("[WPP] 账号 %s 配置错误，已跳过: %s", account_id, exc)
    _warn_inert_keys(out.values())
    return out


#: 已就"死键"提醒过的账号（每进程每人一条，防日志刷屏）
_INERT_WARNED: set[str] = set()


def reset_inert_warnings() -> None:
    """清掉去重表（**唯一用途是测试**，与心流那些 ``reset_*_cache`` 同款）。"""
    _INERT_WARNED.clear()


def _warn_inert_keys(accounts: Iterable[Account]) -> None:
    """配置里出现**本树不认的键**时，每人每进程提醒一次（审阅 A8）。

    目前只有 ``requireAtMention`` 一个，它在 TS 侧**也不生效**：

    * 唯一实现 ``src/inbound/group-policy.ts`` 只被 ``handleWebhookPayload`` 调用，
      而那个函数**零调用者**；
    * 活路径 ``createWppInboundHandler`` → ``shouldTrigger`` 把 ``requireAtMention``
      **硬编码成 false**（``triggers.ts``），``enrich.ts`` 的注释也明说
      非 @ 消息照样会进心流；
    * 生产 TS 账号写着 ``requireAtMention: true``，而心流在群里一直正常工作。

    所以"只在被 @ 时回复"这个语义**从来没生效过**，本树照抄"不生效"才是
    **行为对齐**（实现成硬门反而是引入分叉 —— 群里会静默不出声）。

    但**不实现不等于可以静默**：配了 ``true`` 的人以为群里只在被 @ 时才说话，
    实际每条群消息都进心流 —— 这中间隔着一次"它怎么乱插话"的事故。宁可打一行 WARN。
    """
    for acct in accounts:
        if not acct.require_at_mention or acct.id in _INERT_WARNED:
            continue
        _INERT_WARNED.add(acct.id)
        log.warning(
            "[WPP] 账号 %s 配了 requireAtMention=true，但本插件**不生效**"
            "（与 TS 行为对齐：TS 的活路径也把它硬编码成 false，唯一实现挂在"
            "零调用者的 handleWebhookPayload 上）。群消息仍会全部进心流判定 ——"
            "要收窄「什么时候出声」，请用心流的预算 / 阈值 / 沉默段，或用 "
            "groupPolicy / blacklistGroups 收窄「在哪些群出声」。",
            acct.id,
        )


# ================================================================ 连接前守卫


def _production_reason(acct: Account) -> str | None:
    """生产身份红线的拒因（**没触红线返回 None**，逃生阀已开也返回 None）。"""
    if not acct.is_production_identity or _env_bool(ALLOW_PRODUCTION_ENV):
        return None
    if acct.self_wxid == PRODUCTION_WXID:
        which = (f"selfWxid 就是生产 bot「益融小助理」（{PRODUCTION_WXID}）")
    else:
        which = (f"它的 authcode 是生产实例那把锁（指纹 {acct.credential_fingerprint}，"
                 f"与实例锁 sha256[:12] 同款口径）—— **selfWxid 写对也没用**，"
                 f"连上去照样把生产踢下线")
    return (
        f"账号 {acct.id!r} 触红线 2：{which}。"
        f"以它为身份连接会被厂商实例锁踢下线生产 + 劫持其 webhook。"
        f"确需如此请显式置 {ALLOW_PRODUCTION_ENV}=1。"
    )


def refusal_reason(acct: Account) -> str | None:
    """**为什么不能连** —— 返回 ``None`` 表示可以连。

    纯函数、无副作用，供 ``connect()`` 与测试共用。

    ⚠️ **顺序即优先级**：未启用 → 无凭据 → 红线。前两条是"这个账号没配好"
    （正常跳过，随仓库分发的空壳账号就是这个状态，不能让插件起不来）；
    最后一条是**红线**（唯一会自动把生产踢下线的那一类），
    ``connect()`` 对它走 :func:`assert_no_red_line` **当场 raise**，
    而不是像前两条那样只 warn 一句就跳过。
    """
    if not acct.enabled:
        return f"账号 {acct.id!r} 的 enabled=false（未启用）"
    if not acct.has_credentials:
        return f"账号 {acct.id!r} 无 authcode（authcode / authcodeEnv / WECHATPRO_AUTHCODE 均为空）"
    return _production_reason(acct)


def red_line_reason(acct: Account) -> str | None:
    """**这次会不会因为红线被拦下** —— 只认生产身份那一条（"没配好"返回 ``None``）。

    为什么与 ``refusal_reason`` 分开：后者对"未启用 / 无凭据"是**跳过 + warn**
    （那是正常态），对红线却必须**当场 raise**。两种情况都由同一个函数返回字符串的话，
    调用方没法区分"该跳过"和"该炸" —— 这正是审阅里 C4 说的"守卫是过滤不是 raise"。

    ⚠️ 判据里**含"本来会不会连"**（``enabled`` ∧ 有凭据）：这个问题问的是
    "要不要当场炸"，而 ``enabled=false`` 的账号无论如何都不会连上去，
    把它算成红线只会让 dev 机起不来（``/root/.hermes`` 上那份把 ``selfWxid``
    写成生产号、``enabled=false`` 的 ``default.json`` 就是这种"摆着不连"的形态）。
    **"这个账号的身份是不是生产"**是另一个问题，读 :attr:`Account.is_production_identity`。
    """
    if not acct.enabled or not acct.has_credentials:
        return None
    return _production_reason(acct)


def assert_no_red_line(accounts: Iterable[Account]) -> None:
    """任一账号触红线 ⇒ **raise**（``connect()`` 开头、过滤"可连接"**之前**调用）。

    红线的正确后果只有一个：**别连**（+ 让运维看见），而不是"插件起不来"、
    也不是"只 warn 一句然后照常连别的账号"。判据在 :func:`red_line_reason`。
    """
    for acct in accounts:
        reason = red_line_reason(acct)
        if reason:
            raise AccountError(reason)


def assert_connectable(acct: Account) -> None:
    """连不了就 **raise**。``connect()`` 里对每个账号调用。"""
    reason = refusal_reason(acct)
    if reason:
        raise AccountError(reason)


def connectable(extra_accounts: dict[str, Any] | None = None) -> list[Account]:
    """可连接的账号（过滤掉未启用 / 无凭据 / 触红线的），每条跳过都记日志。"""
    ok: list[Account] = []
    for acct in load_all(extra_accounts).values():
        reason = refusal_reason(acct)
        if reason:
            log.warning("[WPP] 跳过账号: %s", reason)
            continue
        ok.append(acct)
    return ok


def credential_token(accounts: Iterable[Account]) -> str:
    """给 Hermes multiplex 冲突检测用的 ``token``。

    旧实现取 ``next(iter(self._accounts.values()))["authcode"]`` —— 依赖 dict
    插入顺序，多账号下不稳定。这里取**字典序第一个可连接账号**的 authcode，
    结果确定且可复现；没有可连接账号时返回 ``""``（不参与冲突检测）。
    """
    for acct in sorted(accounts, key=lambda a: a.id):
        if acct.has_credentials and acct.enabled:
            return acct.authcode
    return ""
