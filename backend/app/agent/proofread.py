"""校对 Agent：无工具 LLM 直连，由 main.py 注入上下文后调用。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.proofread_prompts import PROOFREAD_SYSTEM

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()


def proofread_enabled() -> bool:
    return os.environ.get("ENABLE_PROOFREAD_AGENT", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def get_proofread_llm() -> ChatOpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    model_name = (
        "deepseek-chat"
        if (base_url and "deepseek" in base_url.lower())
        else "gpt-3.5-turbo"
    )
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url if base_url else None,
        model=model_name,
        streaming=False,
    )


def _extract_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


async def invoke_proofread(human_prompt: str) -> str:
    """执行一次校对 LLM 调用，返回原始文本。"""
    llm = get_proofread_llm()
    result = await llm.ainvoke(
        [
            SystemMessage(content=PROOFREAD_SYSTEM),
            HumanMessage(content=human_prompt),
        ]
    )
    return _extract_content(result.content)
