"""技能落位守卫：``skills/`` 下每个目录必须能被 Hermes **真的发现并采纳**。

## 它挡的事故形态：**静默丢失**（本仓最贵的一类）

Hermes 0.21.5 实测（``.183``）::

    agent_plugins.py:224  _discover_skills(root) 扫 <plugin_root>/skills/<child>/SKILL.md
                         子目录**没有 SKILL.md** ⇒ 直接 continue，**连一行日志都没有**
    _valid_skill_frontmatter(...)
                         name 必须**等于目录名**且匹配 _SKILL_NAME_RE
                         description 必须 1–1024 字符
                         license / compatibility(≤500) / metadata(str→str) / allowed-tools(str)

⇒ "技能目录搬到 ``skills/`` 了" 与 "技能被 agent 采纳了" 是两件事，中间隔着四处
**不报错**的判据。任何一处不满足，结果是"技能静默消失"：没有异常、没有 WARNING、
``hermes plugins doctor`` 也全绿，只是模型再也看不见这套流程。

## 还有一条只有本仓才有的失配

技能正文里写的工具名是**给模型照抄的**。技能写 ``wpp_fc_list`` 而插件注册的是别的名字，
模型照抄就会调用一个不存在的工具 —— 而且**技能本身照样"加载成功"**。
所以正文里的 ``wpp_*`` / ``phoneerp_*`` / ``wecom_*`` 记号必须能落到
**实注册工具名**或 **``schema.sql`` 里的表名**上，二选一，没有第三种。

> 为什么按"实注册"而不是按 ``plugin.yaml``：``provides_tools`` 只是声明，
> 实注册才是模型能看见的东西（F5 里两者差过 14 个）。真跑 ``register()`` 的
> 空壳环境复用 ``test_registration``，**不另写一份**（写两份迟早分叉）。

## 本机为什么要空壳 / 真机还要看什么

与 ``test_registration`` 同理：``gateway.*`` 只在 Hermes 运行时里存在，这里用空壳顶上。
真机验收（``hermes plugins doctor`` 的 skills 计数 / ``skill_view()`` 能否解析）在
``.183`` 上另跑，本地这份的职责是**在改 SKILL.md 的当下就红**。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

#: 复用注册面那套空壳环境与 ``register()`` 调用（见本文件 docstring 末段，
#: 以及 ``plugin/tests/test_registration.py`` 顶部的分工说明）。
from .test_registration import _register, stubbed  # noqa: F401 —— stubbed 是本模块要用的夹具

ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = ROOT / "skills"

#: 逐字抄自 Hermes 0.21.5 ``agent/skill_utils.py``。**改这里等于改判据**，
#: 必须同时核对 ``.183`` 上的真身（两处不一致时以真身为准）。
SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9]+(?:-[a-z0-9]+)*$")

DESC_MIN, DESC_MAX = 1, 1024
COMPAT_MAX = 500

#: 正文里用反引号引起来、且长得像"工具名"的记号。
TOOL_TOKEN_RE = re.compile(r"`((?:wpp|phoneerp|wecom)_[a-z0-9_]+)`")

#: 本插件应当落位的技能全集。**删掉一整个目录**同样要红 —— 那是静默能力丢失
#: 最彻底的一种（连"技能在"这个事实都没了）。
EXPECTED_SKILLS = {
    "wpp-friendcircle",
    "wpp-friendcircle-stats",
    "wpp-friendcircle-view",
    "wpp-history",
    "wpp-identity",
}


# ----------------------------------------------------------------- 取材料


def _skill_dirs() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        p for p in SKILLS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and p.name != "__pycache__"
    )


_SKILL_DIRS = _skill_dirs()
_IDS = [p.name for p in _SKILL_DIRS]


def _split_frontmatter(path: Path) -> tuple[str, str]:
    """按 Hermes 的口径切开 frontmatter：**文件必须第一行就是** ``---``。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines and lines[0].strip() == "---", (
        f"{path.name}: 第一行不是 `---` ⇒ Hermes 视作没有 frontmatter，技能被静默跳过")
    try:
        end = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
    except StopIteration:
        raise AssertionError(f"{path.name}: frontmatter 没有收尾的 `---`") from None
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1:])


def _frontmatter(path: Path) -> dict[str, Any]:
    head, _ = _split_frontmatter(path)
    data = yaml.safe_load(head)
    assert isinstance(data, dict), f"{path.name}: frontmatter 不是 YAML 映射（{type(data).__name__}）"
    return data


@pytest.fixture(scope="module")
def registered_tools(stubbed) -> set[str]:
    """实注册的工具名（去重）—— 技能正文里的工具记号要能落到这上面。"""
    names, _platforms = _register()
    return set(names)


@pytest.fixture(scope="module")
def schema_tables() -> set[str]:
    """``plugin/data/schema.sql`` 里的表名（技能正文里会直接写表名）。"""
    from plugin.data.migrations import expected_table_names

    return set(expected_table_names())


# ----------------------------------------------------------------- 技能全集


def test_skills_directory_exists() -> None:
    assert SKILLS_DIR.is_dir(), f"整棵 skills/ 没了: {SKILLS_DIR}"


def test_skill_set_is_exactly_expected() -> None:
    """目录**集合**必须逐名一致（多 = 有目录会被 Hermes 静默跳过，少 = 能力丢了）。"""
    actual = {p.name for p in _SKILL_DIRS}
    assert actual == EXPECTED_SKILLS, (
        f"技能集合不一致 —— 缺: {sorted(EXPECTED_SKILLS - actual)}；"
        f"多: {sorted(actual - EXPECTED_SKILLS)}")


# --------------------------------------------------------- 逐个技能：能不能被采纳


@pytest.mark.parametrize("sdir", _SKILL_DIRS, ids=_IDS)
def test_skill_md_exists_and_parses(sdir: Path) -> None:
    """① 有 SKILL.md（没有 ⇒ Hermes 直接 continue，**无日志**）② frontmatter 能解析。"""
    md = sdir / "SKILL.md"
    assert md.is_file(), f"{sdir.name}/ 没有 SKILL.md ⇒ Hermes 静默跳过这个技能（无任何日志）"
    data = _frontmatter(md)
    assert data.get("name"), f"{sdir.name}/SKILL.md: frontmatter 缺 name"


@pytest.mark.parametrize("sdir", _SKILL_DIRS, ids=_IDS)
def test_frontmatter_name_matches_dir_and_pattern(sdir: Path) -> None:
    """Hermes 的两条硬判据：``name == 目录名`` 且匹配 ``_SKILL_NAME_RE``。

    任何一条不满足 ⇒ 技能被丢弃；而**目录还在**，所以肉眼看着"已经落位了"。
    """
    name = str(_frontmatter(sdir / "SKILL.md").get("name", ""))
    assert name == sdir.name, (
        f"{sdir.name}/SKILL.md: name={name!r} != 目录名 {sdir.name!r} ⇒ Hermes 丢弃该技能")
    assert SKILL_NAME_RE.match(name), (
        f"{sdir.name}/SKILL.md: name={name!r} 不匹配 Hermes 的 _SKILL_NAME_RE"
        f"（小写字母/数字 + 单连字符分隔，禁双连字符）")


@pytest.mark.parametrize("sdir", _SKILL_DIRS, ids=_IDS)
def test_description_and_optional_fields_valid(sdir: Path) -> None:
    """description 长度 + 三个可选字段的形状 —— 全是**静默丢弃**的判据。"""
    data = _frontmatter(sdir / "SKILL.md")
    desc = str(data.get("description", ""))
    assert DESC_MIN <= len(desc) <= DESC_MAX, (
        f"{sdir.name}/SKILL.md: description 长度 {len(desc)} 不在 {DESC_MIN}–{DESC_MAX}")

    compat = data.get("compatibility")
    if compat is not None:
        assert isinstance(compat, str) and len(compat) <= COMPAT_MAX, (
            f"{sdir.name}/SKILL.md: compatibility 必须是 ≤{COMPAT_MAX} 字符的字符串")
    meta = data.get("metadata")
    if meta is not None:
        assert isinstance(meta, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in meta.items()
        ), f"{sdir.name}/SKILL.md: metadata 必须是 str→str 映射"
    allowed = data.get("allowed-tools")
    if allowed is not None:
        assert isinstance(allowed, str), f"{sdir.name}/SKILL.md: allowed-tools 必须是字符串"


@pytest.mark.parametrize("sdir", _SKILL_DIRS, ids=_IDS)
def test_tool_references_resolve(sdir: Path, registered_tools: set[str],
                                 schema_tables: set[str]) -> None:
    """正文里的工具/表记号必须落到**实注册工具名**或 **schema 表名**上。

    两端都是真源：工具名来自真跑 ``register()``，表名来自 ``schema.sql``。
    第三种情况（两边都没有）就是"技能教模型调用不存在的工具" —— 技能照样加载成功，
    失效要等到模型真的照抄那一刻。
    """
    _, body = _split_frontmatter(sdir / "SKILL.md")
    unknown = sorted(
        {t for t in TOOL_TOKEN_RE.findall(body) if t not in registered_tools and t not in schema_tables}
    )
    assert not unknown, (
        f"{sdir.name}/SKILL.md 引用了**不存在**的记号: {unknown}\n"
        f"  既不在实注册的 {len(registered_tools)} 个工具里，也不在 schema 的 "
        f"{len(schema_tables)} 张表里 ⇒ 模型照抄会调空。")
