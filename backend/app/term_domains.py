"""根据 YouTube 视频背景推断应加载的术语库 domain。"""

from __future__ import annotations

# domain 名与 data/terms/{domain}.json 文件名一一对应
ALL_DOMAINS = ("gaming", "cooking", "general", "vtuber")

# 关键词命中计分：在标题/简介/标签/频道名中搜索（不区分大小写）
DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gaming": (
        "ゲーム", "game", "gaming", "ガチャ", "gacha", "原神", "genshin",
        "攻略", "ボス", "boss", "rpg", "acg", "アニメ", "anime", "二次",
        "手游", "ソシャゲ", "mmorpg", "fps", "任天堂", "nintendo",
        "playstation", "xbox", "steam", "esports", "eスポーツ",
        "デバフ", "バフ", "抽卡",
    ),
    "cooking": (
        "料理", "レシピ", "recipe", "cooking", "ジャム", "jam", "グルメ",
        "gourmet", "食", "厨房", "調理", "ベーキング", "baking", "お菓子",
        "スイーツ", "sweets", "下茹で", "煮込", "フライパン", "オーブン",
        "食材", "味付け", "マリネ", "发酵", "発酵",
    ),
    "vtuber": (
        "にじさんじ", "nijisanji", "彩虹社", "2434", "vtuber", "v tuber",
        "バーチャル", "virtual", "liver", "ライバー", "配信", "実況",
        "雑談", "歌枠", "凸待", "同時視聴", "葛葉", "葛叶", "叶", "kanae",
        "月ノ美兎", "月之美兔", "樋口楓", "樋口枫", "社築", "社筑",
    ),
    "general": (
        "vlog", "日常", "chat", "podcast", "ポッドキャスト",
    ),
}


def _context_blob(ctx: dict) -> str:
    parts = [
        ctx.get("title") or "",
        ctx.get("description") or "",
        ctx.get("tags") or "",
        ctx.get("channel") or "",
    ]
    return " ".join(parts).lower()


def infer_term_domains(ctx: dict | None) -> list[str] | None:
    """从视频背景推断术语 domain 列表。

    返回 None 表示不过滤（使用全部词库），用于无背景的单句/文件翻译。
    返回 list 时 search_term_dict 只检索对应 domain；若无命中会自动回退全库。
    """
    if not ctx:
        return None

    blob = _context_blob(ctx)
    if not blob.strip():
        return None

    scores: dict[str, int] = {domain: 0 for domain in ALL_DOMAINS}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in blob:
                scores[domain] += 1

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_score = ranked[0][1] if ranked else 0

    if top_score == 0:
        # 无明确信号：仍加载全部 domain 文件，但优先 general + gaming（项目默认受众）
        return ["general", "gaming", "cooking", "vtuber"]

    selected = [domain for domain, score in ranked if score > 0]

    # 跨领域视频：若第二名分数达到第一名一半，一并加载
    if len(selected) > 1 and ranked[1][1] >= max(1, top_score // 2):
        pass  # selected already has all with score > 0
    elif len(selected) == 1 and selected[0] != "general":
        # 单一强领域时附加 general 作兜底（口语/通用表达）
        selected.append("general")

    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for domain in selected:
        if domain not in seen:
            seen.add(domain)
            ordered.append(domain)
    return ordered


def format_domain_selection(domains: list[str] | None) -> str:
    if not domains:
        return "术语库：全库检索（未限定 domain）"
    return f"术语库：已按视频背景加载 domain → {', '.join(domains)}"
