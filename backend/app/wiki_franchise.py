"""根据 YouTube 视频背景推断 Wiki 查询应注入的作品 IP 上下文。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypedDict

# 请求级作用域：YouTube 翻译时按视频背景限定 Wiki 查询扩展
_active_franchise_hints: ContextVar[list[str] | None] = ContextVar(
    "active_franchise_hints", default=None
)


class FranchiseMeta(TypedDict, total=False):
    label: str
    keywords: tuple[str, ...]
    search_prefixes: tuple[str, ...]
    preferred_sources: tuple[str, ...]
    moegirl_category: str


FRANCHISE_REGISTRY: dict[str, FranchiseMeta] = {
    "uma_musume": {
        "label": "赛马娘 Pretty Derby",
        "keywords": (
            "ウマ娘",
            "赛马娘",
            "pretty derby",
            "ウマ娘プリティーダービー",
            "umamusume",
        ),
        "search_prefixes": ("ウマ娘", "赛马娘 Pretty Derby"),
        "preferred_sources": ("moegirl", "zh_wiki", "ja_wiki"),
        "moegirl_category": "赛马娘 Pretty Derby",
    },
    "genshin": {
        "label": "原神",
        "keywords": ("原神", "genshin impact", "genshin"),
        "search_prefixes": ("原神", "Genshin Impact"),
        "preferred_sources": ("moegirl", "zh_wiki", "ja_wiki"),
        "moegirl_category": "原神",
    },
    "nijisanji": {
        "label": "彩虹社 NIJISANJI",
        "keywords": ("にじさんじ", "nijisanji", "2434", "彩虹社"),
        "search_prefixes": ("にじさんじ", "NIJISANJI"),
        "preferred_sources": ("moegirl", "zh_wiki", "ja_wiki"),
        "moegirl_category": "彩虹社",
    },
    "general_acg": {
        "label": "ACG/手游",
        "keywords": (),  # 仅作兜底，不通过关键词匹配
        "search_prefixes": (),
        "preferred_sources": ("moegirl", "zh_wiki", "ja_wiki"),
    },
}

# general_acg 兜底触发：gaming domain 且无更具体 IP 命中时使用
_GAMING_SIGNAL_KEYWORDS = (
    "ゲーム",
    "game",
    "gaming",
    "ガチャ",
    "gacha",
    "acg",
    "アニメ",
    "anime",
    "二次",
    "手游",
    "ソシャゲ",
)


def _context_blob(ctx: dict) -> str:
    parts = [
        ctx.get("title") or "",
        ctx.get("description") or "",
        ctx.get("tags") or "",
        ctx.get("channel") or "",
    ]
    return " ".join(parts).lower()


def get_franchise_meta(key: str) -> FranchiseMeta | None:
    return FRANCHISE_REGISTRY.get(key)


def get_active_franchise_hints() -> list[str]:
    return _active_franchise_hints.get() or []


@contextmanager
def wiki_franchise_scope(hints: list[str] | None):
    """在 scope 内让 lookup_wiki 读取 franchise hints 做查询扩展。"""
    token = _active_franchise_hints.set(hints)
    try:
        yield
    finally:
        _active_franchise_hints.reset(token)


def infer_franchise_hints(ctx: dict | None) -> list[str]:
    """从视频元数据推断作品 IP 列表（按匹配强度排序）。"""
    if not ctx:
        return []

    blob = _context_blob(ctx)
    if not blob.strip():
        return []

    scores: dict[str, int] = {}
    for key, meta in FRANCHISE_REGISTRY.items():
        if key == "general_acg":
            continue
        score = sum(1 for kw in meta.get("keywords", ()) if kw.lower() in blob)
        if score > 0:
            scores[key] = score

    if scores:
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        return [key for key, _ in ranked]

    if any(kw.lower() in blob for kw in _GAMING_SIGNAL_KEYWORDS):
        return ["general_acg"]

    return []


def format_franchise_selection(hints: list[str]) -> str:
    if not hints:
        return "Wiki 上下文：未识别具体作品 IP（查询不自动扩展前缀）"
    labels = []
    for key in hints:
        meta = FRANCHISE_REGISTRY.get(key)
        labels.append(meta.get("label", key) if meta else key)
    primary = hints[0]
    meta = FRANCHISE_REGISTRY.get(primary, {})
    prefixes = meta.get("search_prefixes") or ()
    prefix_note = f"（查询将自动加「{prefixes[0]}」前缀，优先萌娘百科）" if prefixes else "（优先萌娘百科）"
    return f"Wiki 上下文：已识别作品 IP → {', '.join(labels)}{prefix_note}"
