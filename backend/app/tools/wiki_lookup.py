"""MediaWiki 定向查询：萌娘百科、日/中文维基。供 Agent 翻译时核实专名。"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import requests
from langchain_core.tools import tool

from app.tools.dictionary import _active_domains

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

_USER_AGENT = (
    "TranslatorAgent/1.0 (local offline translator; "
    "+https://github.com; wiki-lookup for term verification)"
)


def _wiki_enabled() -> bool:
    return os.environ.get("ENABLE_WIKI_LOOKUP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _wiki_timeout() -> float:
    return float(os.environ.get("WIKI_LOOKUP_TIMEOUT", "10"))


def _wiki_proxies() -> dict[str, str] | None:
    proxy = os.environ.get("WIKI_PROXY", "").strip() or os.environ.get("YOUTUBE_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _resolve_source_order(source: str) -> list[str]:
    if source != "auto" and source in WIKI_SOURCES:
        return [source]
    domains = _active_domains.get() or []
    if domains:
        order: list[str] = []
        for domain in domains:
            for key in _DOMAIN_SOURCE_ORDER.get(domain, []):
                if key not in order:
                    order.append(key)
        if order:
            return order
    return ["moegirl", "ja_wiki", "zh_wiki"]


def _mediawiki_search_extract(
    api_url: str,
    site_url: str,
    query: str,
    *,
    limit: int = 2,
    extract_chars: int = 480,
) -> list[dict[str, str]]:
    params: dict[str, Any] = {
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
    }
    resp = requests.get(
        api_url,
        params=params,
        timeout=_wiki_timeout(),
        proxies=_wiki_proxies(),
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    pages = data.get("query", {}).get("pages", {})
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
                "url": f"{site_url}/wiki/{quote(title.replace(' ', '_'))}",
            }
        )
    return results


def lookup_wiki_sources(query: str, source: str = "auto") -> str:
    """查询 Wiki 并格式化为 Agent 可读文本。"""
    if not _wiki_enabled():
        return "在线 Wiki 查询已关闭（.env 中 ENABLE_WIKI_LOOKUP=0）。请仅用本地术语库或通用知识翻译。"

    q = query.strip()
    if not q:
        return "查询词为空。"

    order = _resolve_source_order(source)
    blocks: list[str] = []
    any_hit = False

    for key in order:
        meta = WIKI_SOURCES[key]
        try:
            hits = _mediawiki_search_extract(meta["api"], meta["site"], q)
        except requests.RequestException as exc:
            blocks.append(f"【{meta['name']}】查询失败：{exc}")
            continue

        if not hits:
            blocks.append(f"【{meta['name']}】未找到与「{q}」相关的条目。")
            continue

        any_hit = True
        for hit in hits:
            blocks.append(
                f"【{meta['name']}】{hit['title']}\n"
                f"链接：{hit['url']}\n"
                f"摘要：{hit['extract']}"
            )

    if not any_hit:
        return (
            f"萌娘百科/维基均未找到「{q}」的有效条目。\n"
            + "\n\n".join(blocks)
            + "\n请结合原文语境谨慎翻译，勿编造组织归属或人名。"
        )
    return "\n\n".join(blocks)


@tool
def lookup_wiki(query: str, source: str = "auto") -> str:
    """查询萌娘百科或维基百科，获取专有名词释义、中文译名与背景。

    当本地术语库 (search_term_dict) 未命中，或需核实 VTuber/游戏/ACG 专名时使用。
    query: 要查询的词条（日语/中文/英文均可）。
    source: 数据源。auto=按视频领域自动选择；也可 moegirl / ja_wiki / zh_wiki。
    """
    return lookup_wiki_sources(query, source)
