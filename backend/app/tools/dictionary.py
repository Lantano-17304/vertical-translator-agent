import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import chromadb
from langchain_core.tools import tool

# ChromaDB 向量库（进程内，启动时从 data/terms/*.json 分库注入）
chroma_client = chromadb.Client()
collection = chroma_client.get_or_create_collection(name="term_dictionary")

TERMS_DIR = Path(__file__).resolve().parents[1] / "data" / "terms"
DEFAULT_CATEGORY = "general"

# 请求级作用域：YouTube 翻译时按视频背景限定检索 domain
_active_domains: ContextVar[list[str] | None] = ContextVar("active_term_domains", default=None)


def _term_to_document(term: dict, domain: str) -> str:
    aliases = " / ".join(term.get("aliases", []))
    examples = "；".join(term.get("examples", []))
    return (
        f"术语：{term['term']}\n"
        f"领域库：{domain}\n"
        f"别名：{aliases}\n"
        f"推荐译法：{term['translation']}\n"
        f"领域分类：{term.get('category', DEFAULT_CATEGORY)}\n"
        f"含义：{term['meaning']}\n"
        f"例句：{examples}\n"
        f"翻译注意：{term.get('notes', '')}"
    )


def _load_domain_terms() -> list[tuple[str, dict]]:
    """从 data/terms/{domain}.json 加载全部词条。文件名即 domain。"""
    items: list[tuple[str, dict]] = []
    if not TERMS_DIR.is_dir():
        return items

    for path in sorted(TERMS_DIR.glob("*.json")):
        if path.stem.startswith("_"):
            continue
        domain = path.stem
        with path.open("r", encoding="utf-8") as f:
            terms = json.load(f)
        if not isinstance(terms, list):
            continue
        for term in terms:
            if term.get("term"):
                items.append((domain, term))
    return items


def _seed_dictionary() -> int:
    items = _load_domain_terms()
    if not items:
        return 0

    collection.upsert(
        documents=[_term_to_document(term, domain) for domain, term in items],
        metadatas=[
            {
                "domain": domain,
                "category": term.get("category", DEFAULT_CATEGORY),
                "term": term["term"],
            }
            for domain, term in items
        ],
        ids=[
            f"{domain}_{index:04d}_{term['term']}"
            for index, (domain, term) in enumerate(items, start=1)
        ],
    )
    return len(items)


def reload_dictionary() -> int:
    """清空并重新从 data/terms/*.json 注入向量库，返回词条数量。"""
    global collection
    try:
        chroma_client.delete_collection(name="term_dictionary")
    except Exception:
        pass
    collection = chroma_client.get_or_create_collection(name="term_dictionary")
    return _seed_dictionary()


@contextmanager
def term_domains_scope(domains: list[str] | None):
    """在 scope 内限定 search_term_dict 只检索指定 domain（None = 全库）。"""
    token = _active_domains.set(domains)
    try:
        yield
    finally:
        _active_domains.reset(token)


def _query_terms(query: str, n_results: int = 4) -> list[str]:
    domains = _active_domains.get()
    if domains:
        filtered = collection.query(
            query_texts=[query],
            n_results=n_results,
            where={"domain": {"$in": domains}},
        )
        if filtered["documents"] and filtered["documents"][0]:
            return filtered["documents"][0]

    results = collection.query(query_texts=[query], n_results=n_results)
    if results["documents"] and results["documents"][0]:
        return results["documents"][0]
    return []


_seed_dictionary()


@tool
def search_term_dict(query: str) -> str:
    """查询垂直领域专有名词、黑话、俗语的意思。
    字幕来自 YouTube 自动语音识别时，错字/同音词很常见；若某词像 ASR 误听或读音相近的专名，也应调用本工具核实。
    参数是你要查询的名词（可用字幕中的错字写法）。"""
    documents = _query_terms(query, n_results=4)
    if documents:
        return "\n".join(documents)
    return "本地字典中未找到该词的专属定义，请根据通用知识推断。"
