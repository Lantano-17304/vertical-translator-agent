"""MediaWiki 定向查询：萌娘百科、日/中文维基。供 Agent 翻译时核实专名。"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import requests
from langchain_core.tools import tool

from app.tools.dictionary import _active_domains
from app.wiki_franchise import FRANCHISE_REGISTRY, get_active_franchise_hints

WIKI_SOURCES: dict[str, dict[str, str]] = {
    "moegirl": {
        "name": "萌娘百科",
        "api": "https://zh.moegirl.org.cn/api.php",
        "site": "https://zh.moegirl.org.cn",
    },
    "ja_wiki": {
        "name": "日文维基百科",
        "api": "https://ja.wikipedia.org/w/api.php",
        "site": "https://ja.wikipedia.org",
    },
    "zh_wiki": {
        "name": "中文维基百科",
        "api": "https://zh.wikipedia.org/w/api.php",
        "site": "https://zh.wikipedia.org",
    },
}

_DOMAIN_SOURCE_ORDER: dict[str, list[str]] = {
    "vtuber": ["moegirl", "zh_wiki", "ja_wiki"],
    "gaming": ["moegirl", "zh_wiki", "ja_wiki"],
    "cooking": ["zh_wiki", "ja_wiki", "moegirl"],
    "general": ["ja_wiki", "zh_wiki", "moegirl"],
}

_NOISE_SUFFIXES = re.compile(
    r"\s*(?:ゲーム|game|攻略|実況|配信|動画|video|stream)\s*$",
    re.IGNORECASE,
)

_USER_AGENT = (
    "TranslatorAgent/1.0 (local offline translator; "
    "+https://github.com; wiki-lookup for term verification)"
)

_MAX_QUERY_VARIANTS = 4
_MAX_HTTP_ATTEMPTS = 27  # 3 variants × 3 sources × 3 strategies (budget cap)


def _wiki_enabled() -> bool:
    return os.environ.get("ENABLE_WIKI_LOOKUP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _wiki_timeout() -> float:
    return float(os.environ.get("WIKI_LOOKUP_TIMEOUT", "10"))


def _wiki_search_limit() -> int:
    return max(1, int(os.environ.get("WIKI_SEARCH_LIMIT", "5")))


def _wiki_extract_chars() -> int:
    return max(120, int(os.environ.get("WIKI_EXTRACT_CHARS", "720")))


def _wiki_query_expand_enabled() -> bool:
    return os.environ.get("WIKI_QUERY_EXPAND", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _wiki_multi_strategy_enabled() -> bool:
    return os.environ.get("WIKI_MULTI_STRATEGY", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _wiki_proxies() -> dict[str, str] | None:
    proxy = os.environ.get("WIKI_PROXY", "").strip() or os.environ.get("YOUTUBE_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _http_get(api_url: str, params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(
        api_url,
        params=params,
        timeout=_wiki_timeout(),
        proxies=_wiki_proxies(),
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    return resp.json()


def _page_url(site_url: str, title: str) -> str:
    return f"{site_url}/wiki/{quote(title.replace(' ', '_'))}"


def _resolve_source_order(source: str) -> list[str]:
    if source != "auto" and source in WIKI_SOURCES:
        return [source]

    franchise_hints = get_active_franchise_hints()
    if franchise_hints:
        order: list[str] = []
        for key in franchise_hints:
            meta = FRANCHISE_REGISTRY.get(key, {})
            for src in meta.get("preferred_sources", ()):
                if src in WIKI_SOURCES and src not in order:
                    order.append(src)
        if order:
            return order

    domains = _active_domains.get() or []
    if domains:
        order = []
        for domain in domains:
            for key in _DOMAIN_SOURCE_ORDER.get(domain, []):
                if key not in order:
                    order.append(key)
        if order:
            return order
    return ["moegirl", "ja_wiki", "zh_wiki"]


def _strip_noise(query: str) -> str:
    stripped = _NOISE_SUFFIXES.sub("", query).strip()
    return stripped or query.strip()


def _build_query_variants(query: str) -> list[str]:
    """生成查询变体：原始词、IP 前缀、去噪+前缀、萌娘分类限定。"""
    q = query.strip()
    if not q:
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        item = item.strip()
        if item and item not in seen and len(variants) < _MAX_QUERY_VARIANTS:
            seen.add(item)
            variants.append(item)

    _add(q)

    if not _wiki_query_expand_enabled():
        return variants

    franchise_hints = get_active_franchise_hints()
    denoised = _strip_noise(q)

    for key in franchise_hints:
        meta = FRANCHISE_REGISTRY.get(key, {})
        for prefix in meta.get("search_prefixes", ()):
            _add(f"{prefix} {q}")
            if denoised != q:
                _add(f"{prefix} {denoised}")

        category = meta.get("moegirl_category")
        if category:
            nickname = denoised if denoised else q
            _add(f"incategory:{category} {nickname}")

    return variants


def _hits_from_pages(
    pages: dict[str, Any],
    site_url: str,
    *,
    extract_chars: int,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for page in pages.values():
        if int(page.get("ns", 0)) != 0:
            continue
        title = page.get("title", "")
        extract = (page.get("extract") or "").strip()
        if not title or not extract:
            continue
        results.append(
            {
                "title": title,
                "extract": extract[:extract_chars],
                "url": _page_url(site_url, title),
            }
        )
    return results


def _mediawiki_titles_extract(
    api_url: str,
    site_url: str,
    title: str,
    *,
    extract_chars: int,
) -> list[dict[str, str]]:
    data = _http_get(
        api_url,
        {
            "action": "query",
            "titles": title,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "exchars": extract_chars,
            "redirects": 1,
            "format": "json",
        },
    )
    pages = data.get("query", {}).get("pages", {})
    return _hits_from_pages(pages, site_url, extract_chars=extract_chars)


def _mediawiki_opensearch_extract(
    api_url: str,
    site_url: str,
    query: str,
    *,
    limit: int,
    extract_chars: int,
) -> list[dict[str, str]]:
    data = _http_get(
        api_url,
        {
            "action": "opensearch",
            "search": query,
            "limit": limit,
            "namespace": 0,
            "format": "json",
        },
    )
    if not isinstance(data, list) or len(data) < 2:
        return []
    titles = data[1] if isinstance(data[1], list) else []
    if not titles:
        return []

    pipe = "|".join(titles[:limit])
    detail = _http_get(
        api_url,
        {
            "action": "query",
            "titles": pipe,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "exchars": extract_chars,
            "redirects": 1,
            "format": "json",
        },
    )
    pages = detail.get("query", {}).get("pages", {})
    return _hits_from_pages(pages, site_url, extract_chars=extract_chars)


def _mediawiki_search_extract(
    api_url: str,
    site_url: str,
    query: str,
    *,
    limit: int,
    extract_chars: int,
) -> list[dict[str, str]]:
    data = _http_get(
        api_url,
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": limit,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "exchars": extract_chars,
            "redirects": 1,
            "format": "json",
        },
    )
    pages = data.get("query", {}).get("pages", {})
    return _hits_from_pages(pages, site_url, extract_chars=extract_chars)


def _search_one_variant(
    api_url: str,
    site_url: str,
    variant: str,
    *,
    per_source_limit: int,
    extract_chars: int,
) -> list[dict[str, str]]:
    if _wiki_multi_strategy_enabled():
        strategies = (
            lambda: _mediawiki_titles_extract(
                api_url, site_url, variant, extract_chars=extract_chars
            ),
            lambda: _mediawiki_opensearch_extract(
                api_url,
                site_url,
                variant,
                limit=per_source_limit,
                extract_chars=extract_chars,
            ),
            lambda: _mediawiki_search_extract(
                api_url,
                site_url,
                variant,
                limit=per_source_limit,
                extract_chars=extract_chars,
            ),
        )
        for strategy in strategies:
            hits = strategy()
            if hits:
                return hits[:per_source_limit]
        return []

    return _mediawiki_search_extract(
        api_url,
        site_url,
        variant,
        limit=per_source_limit,
        extract_chars=extract_chars,
    )


def lookup_wiki_sources(query: str, source: str = "auto") -> str:
    """查询 Wiki 并格式化为 Agent 可读文本。"""
    if not _wiki_enabled():
        return "在线 Wiki 查询已关闭（.env 中 ENABLE_WIKI_LOOKUP=0）。请仅用本地术语库或通用知识翻译。"

    q = query.strip()
    if not q:
        return "查询词为空。"

    order = _resolve_source_order(source)
    variants = _build_query_variants(q)
    global_limit = _wiki_search_limit()
    extract_chars = _wiki_extract_chars()

    franchise_hints = get_active_franchise_hints()
    meta_lines: list[str] = []
    if franchise_hints:
        labels = [
            FRANCHISE_REGISTRY.get(k, {}).get("label", k) for k in franchise_hints
        ]
        meta_lines.append(
            f"【检索说明】已根据视频背景识别 IP：{', '.join(labels)}；"
            f"已尝试查询变体：{' / '.join(variants)}"
        )
    elif len(variants) > 1 or variants[0] != q:
        meta_lines.append(f"【检索说明】已尝试查询变体：{' / '.join(variants)}")

    blocks: list[str] = list(meta_lines)
    any_hit = False
    seen_keys: set[tuple[str, str]] = set()
    http_attempts = 0

    for key in order:
        if len(seen_keys) >= global_limit:
            break
        meta = WIKI_SOURCES[key]
        source_hits: list[dict[str, str]] = []
        source_errors: list[str] = []

        for variant in variants:
            if len(seen_keys) >= global_limit:
                break
            if http_attempts >= _MAX_HTTP_ATTEMPTS:
                break
            try:
                http_attempts += 1
                hits = _search_one_variant(
                    meta["api"],
                    meta["site"],
                    variant,
                    per_source_limit=global_limit,
                    extract_chars=extract_chars,
                )
            except requests.RequestException as exc:
                source_errors.append(str(exc))
                continue

            for hit in hits:
                dedupe_key = (key, hit["title"])
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                source_hits.append(hit)
                if len(seen_keys) >= global_limit:
                    break

        if source_errors and not source_hits:
            blocks.append(f"【{meta['name']}】查询失败：{source_errors[0]}")
            continue

        if not source_hits:
            blocks.append(f"【{meta['name']}】未找到与「{q}」相关的条目。")
            continue

        any_hit = True
        for hit in source_hits:
            blocks.append(
                f"【{meta['name']}】{hit['title']}\n"
                f"链接：{hit['url']}\n"
                f"摘要：{hit['extract']}"
            )

    if not any_hit:
        prefix = "\n\n".join(blocks)
        if prefix:
            prefix += "\n\n"
        return (
            f"萌娘百科/维基均未找到「{q}」的有效条目。\n"
            + prefix
            + "请结合原文语境谨慎翻译，勿编造组织归属或人名。"
        )
    return "\n\n".join(blocks)


@tool
def lookup_wiki(query: str, source: str = "auto") -> str:
    """查询萌娘百科或维基百科，获取专有名词释义、中文译名与背景。

    当本地术语库 (search_term_dict) 未命中，或需核实 VTuber/游戏/ACG 专名时使用。
    query: 要查询的词条（日语/中文/英文均可）。
    source: 数据源。auto=按视频领域/作品 IP 自动选择；也可 moegirl / ja_wiki / zh_wiki。
    """
    return lookup_wiki_sources(query, source)
