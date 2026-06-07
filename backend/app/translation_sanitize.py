"""翻译输出清洗：去掉 Agent 元叙述、对照式箭头等，避免污染 SRT。"""
from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JP_KANA_RE = re.compile(r"[ぁ-んァ-ヴー]")

_THINKING_BLOCK_RE = re.compile(
    r"``|``|\[think\].*?\[/think\]",
    re.DOTALL | re.IGNORECASE,
)
_LEADING_JUNK_RE = re.compile(
    r"^\s*(?:[-*#>\s]*)(?:翻译(?:结果)?[:：]|译文[:：]|参考翻译[:：])\s*",
    re.IGNORECASE,
)
_ANNOTATION_SPLIT_RE = re.compile(r"\s*(?:→|->)\s*")

# Agent 工具/推理过程泄漏进字幕的常见模式
_META_LEAK_RE = re.compile(
    r"(?:"
    r"查一下|查一查|检索一下|让我再?查|再查一下|尝试查|去查"
    r"|萌娘百科|维基百科|wikipedia|wiki"
    r"|术语库|search_term|lookup_wiki"
    r"|我没有.{0,6}把握|更可能的是|简写或打字错误|常见指代"
    r"|在这个语境下|根据查到的|工具查"
    r")",
    re.IGNORECASE,
)


def strip_thinking_markers(text: str) -> str:
    if not text:
        return ""
    return _THINKING_BLOCK_RE.sub("", text).strip()


def is_agent_meta_output(text: str) -> bool:
    """译文是否像 Agent 过程描述而非观众字幕。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return bool(_META_LEAK_RE.search(cleaned))


def extract_chinese_from_annotation(text: str) -> str:
    """「ないですね。→ 没有吧。」只保留箭头后的中文侧。"""
    if "→" not in text and "->" not in text:
        return text
    parts = [part.strip() for part in _ANNOTATION_SPLIT_RE.split(text) if part.strip()]
    if not parts:
        return text
    best = max(parts, key=lambda part: len(_CJK_RE.findall(part)))
    return best.strip()


def normalize_translation_line(text: str) -> str:
    """把模型输出收敛成单行译文。"""
    cleaned = strip_thinking_markers(text or "")
    if not cleaned:
        return ""

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""

    def is_noise(line: str) -> bool:
        if line in {"---", "—", "——"}:
            return True
        if line.startswith(("【", "（", "(", "注：", "说明", "意译", "直译", "结合")):
            return True
        if "翻译说明" in line or "意译说明" in line:
            return True
        return False

    candidates = [line for line in lines if not is_noise(line)]
    picked = (candidates[-1] if candidates else lines[-1]).strip()
    picked = picked.lstrip("> ").strip()
    picked = _LEADING_JUNK_RE.sub("", picked).strip()
    return picked.strip("*").strip()


def finalize_translation_line(text: str) -> str:
    """清洗 + 去对照箭头，供 SRT/TXT 使用。"""
    line = normalize_translation_line(text)
    line = extract_chinese_from_annotation(line)
    return line.strip()


def line_has_japanese(text: str) -> bool:
    return bool(_JP_KANA_RE.search(text or ""))


def needs_retranslation(source: str, translated: str) -> bool:
    """判断该行是否仍需补译。"""
    src = (source or "").strip()
    out = finalize_translation_line(translated or "")
    if not src:
        return False
    if not out:
        return True
    if is_agent_meta_output(out):
        return True
    if "→" in (translated or "") or "->" in (translated or ""):
        return True
    if out == src:
        return line_has_japanese(src)
    if not line_has_japanese(src):
        return False
    out_cn = len(_CJK_RE.findall(out))
    out_jp = len(_JP_KANA_RE.findall(out))
    if out_cn >= 3 and out_jp <= 2:
        return False
    if out_jp >= 1 and out_cn < out_jp * 3:
        return True
    if out_jp >= 3 and out_cn < out_jp * 2:
        return True
    return False
