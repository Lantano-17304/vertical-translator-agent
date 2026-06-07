"""翻译前术语预扫描与 glossary prompt 构建（Push 式注入）。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from app.translation_prompts import GLOSSARY_ASR_NOTE

DATA_DIR = Path(__file__).resolve().parent / "data"
TERMS_DIR = DATA_DIR / "terms"

# 简繁常见变体（用于频道名/别名匹配）
_TRAD_SIMP_PAIRS = (
    ("葉", "叶"),
    ("楓", "枫"),
    ("見", "见"),
    ("樂", "乐"),
    ("渋", "涩"),
    ("國", "国"),
    ("學", "学"),
    ("築", "筑"),
    ("兎", "兔"),
    ("観", "观"),
    ("覺", "觉"),
    ("體", "体"),
    ("關", "关"),
    ("畫", "画"),
    ("廣", "广"),
    ("經", "经"),
    ("網", "网"),
    ("視", "视"),
    ("聲", "声"),
    ("優", "优"),
    ("價", "价"),
    ("藝", "艺"),
    ("戰", "战"),
    ("醫", "医"),
    ("氣", "气"),
    ("電", "电"),
    ("龍", "龙"),
    ("馬", "马"),
    ("車", "车"),
    ("東", "东"),
    ("絲", "丝"),
    ("歲", "岁"),
    ("戀", "恋"),
    ("戲", "戏"),
    ("譯", "译"),
)

GlossaryHit = tuple[str, dict, str]


def glossary_max_terms() -> int:
    return int(os.environ.get("GLOSSARY_MAX_TERMS", "30"))


def normalize_query(text: str) -> str:
    """规范化查询词：去空白、小写、统一简繁变体。"""
    if not text:
        return ""
    normalized = text.strip().lower()
    for trad, simp in _TRAD_SIMP_PAIRS:
        normalized = normalized.replace(trad, simp)
        normalized = normalized.replace(trad.lower(), simp)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def is_pending_file(path: Path) -> bool:
    return path.stem.startswith("_")


def load_domain_terms(domains: list[str] | None = None) -> list[tuple[str, dict]]:
    """从 data/terms/{domain}.json 加载词条，跳过 _ 前缀文件。"""
    root = TERMS_DIR
    items: list[tuple[str, dict]] = []
    if not root.is_dir():
        return items

    domain_set = set(domains) if domains else None
    for path in sorted(root.glob("*.json")):
        if is_pending_file(path):
            continue
        domain = path.stem
        if domain_set is not None and domain not in domain_set:
            continue
        with path.open("r", encoding="utf-8") as f:
            terms = json.load(f)
        if not isinstance(terms, list):
            continue
        for term in terms:
            if term.get("term"):
                items.append((domain, term))
    return items


def iter_surface_forms(term: dict) -> list[str]:
    forms = [term.get("term", "")]
    forms.extend(term.get("aliases", []))
    return [f for f in forms if f]


def _dedupe_hits(candidates: list[GlossaryHit]) -> list[GlossaryHit]:
    """按 term 去重，保留 matched_form 最长的一条。"""
    best: dict[str, GlossaryHit] = {}
    for domain, term, matched_form in candidates:
        key = normalize_query(term.get("term", ""))
        if not key:
            continue
        prev = best.get(key)
        if prev is None or len(matched_form) > len(prev[2]):
            best[key] = (domain, term, matched_form)
    hits = list(best.values())
    hits.sort(key=lambda item: len(item[2]), reverse=True)
    return hits[: glossary_max_terms()]


def find_glossary_hits(text: str, domains: list[str] | None = None) -> list[GlossaryHit]:
    """在文本中做字面子串匹配，返回命中词条。"""
    if not text:
        return []
    candidates: list[GlossaryHit] = []
    for domain, term in load_domain_terms(domains):
        for form in iter_surface_forms(term):
            if form in text:
                candidates.append((domain, term, form))
    return _dedupe_hits(candidates)


def find_context_hits(raw_ctx: dict | None, domains: list[str] | None = None) -> list[GlossaryHit]:
    """对频道/标题/标签做字面匹配（频道锚定）。"""
    if not raw_ctx:
        return []
    blob = " ".join(
        part
        for part in (
            raw_ctx.get("channel") or "",
            raw_ctx.get("title") or "",
            raw_ctx.get("tags") or "",
        )
        if part
    )
    return find_glossary_hits(blob, domains)


def collect_session_glossary_hits(
    lines: list[str],
    raw_ctx: dict | None,
    domains: list[str] | None,
) -> list[GlossaryHit]:
    """合并字幕全文与视频元数据命中。"""
    text = "\n".join(line for line in lines if line)
    candidates = find_glossary_hits(text, domains) + find_context_hits(raw_ctx, domains)
    return _dedupe_hits(candidates)


def build_glossary_block(hits: list[GlossaryHit]) -> str:
    if not hits:
        return ""
    lines = ["【本视频专名术语表 — 译文必须遵守，禁止意译】"]
    for _domain, term, matched_form in hits:
        src = term.get("term", matched_form)
        trans = term.get("translation", "")
        if trans and normalize_query(src) != normalize_query(trans):
            lines.append(f"- {src}（{matched_form}）→ {trans}")
        else:
            lines.append(f"- {src}（{matched_form}）→ {trans or src}")
    lines.append(GLOSSARY_ASR_NOTE)
    return "\n".join(lines)


def build_session_glossary(
    lines: list[str],
    raw_ctx: dict | None,
    domains: list[str] | None,
) -> str:
    hits = collect_session_glossary_hits(lines, raw_ctx, domains)
    return build_glossary_block(hits)


def format_glossary_thought(hits: list[GlossaryHit]) -> str:
    if not hits:
        return "\n【术语表】词库预扫描：本视频未命中已知专名。\n\n"
    preview = [hit[1].get("term", "") for hit in hits[:8] if hit[1].get("term")]
    names = "、".join(preview)
    if len(hits) > len(preview):
        names = f"{names}…" if names else ""
    return f"\n【术语表】已从词库预加载 {len(hits)} 条专名（{names}）\n\n"
