"""离线验证：校对批量解析与失败回退逻辑（无需 API Key）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import _proofread_batch_wrapped  # noqa: E402


async def test_fallback_on_bad_parse() -> None:
    batch = ["原文1", "原文2"]
    drafts = ["初译1", "初译2"]
    with patch(
        "app.main.invoke_proofread",
        new_callable=AsyncMock,
        return_value="乱格式，无行号",
    ):
        result = await _proofread_batch_wrapped(batch, drafts, None, None)
    assert result == drafts, f"expected fallback, got {result}"


async def test_parse_ok() -> None:
    batch = ["原文1", "原文2"]
    drafts = ["初译1", "初译2"]
    with patch(
        "app.main.invoke_proofread",
        new_callable=AsyncMock,
        return_value="[1] 修订1\n[2] 修订2",
    ):
        result = await _proofread_batch_wrapped(batch, drafts, None, None)
    assert result == ["修订1", "修订2"], f"unexpected {result}"


def main() -> None:
    asyncio.run(test_fallback_on_bad_parse())
    asyncio.run(test_parse_ok())
    print("verify_proofread_parse: OK")


if __name__ == "__main__":
    main()
