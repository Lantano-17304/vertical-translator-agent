import io
import re
import json
import os
import tempfile
import asyncio
import time
import uuid
import shutil
from pathlib import Path
from dotenv import load_dotenv

# .env 放在仓库根目录；从 backend/ 启动 uvicorn 时也要能读到
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv()

import pysrt
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse, PlainTextResponse, FileResponse
from app.agent.core import agent_executor
from app.agent.proofread import invoke_proofread, proofread_enabled
from app.proofread_prompts import proofread_batch_user, proofread_line_user
from app.term_domains import infer_term_domains, format_domain_selection
from app.wiki_franchise import (
    infer_franchise_hints,
    wiki_franchise_scope,
    format_franchise_selection,
)
from app.term_glossary import (
    build_session_glossary,
    collect_session_glossary_hits,
    format_glossary_thought,
)
from app.tools.dictionary import term_domains_scope, reload_dictionary
from app.translation_prompts import ASR_AWARE_RULES
from app.translation_sanitize import (
    finalize_translation_line as _finalize_translation_line,
    line_has_japanese as _line_has_japanese,
    needs_retranslation as _needs_retranslation,
    normalize_translation_line as _normalize_translation_line,
    strip_thinking_markers as _strip_thinking_markers,
)
from app.youtube_utils import (
    extract_video_id,
    fetch_transcript,
    fetch_video_context,
    format_video_context,
    download_video_file,
    list_video_quality_options,
)
from urllib.parse import quote

app = FastAPI(title="Translator Agent API")

# YouTube 字幕可能很长，按字符数分段交给 Agent，避免单次上下文过长。
YOUTUBE_TRANSLATE_CHUNK_CHARS = int(os.environ.get("YOUTUBE_TRANSLATE_CHUNK_CHARS", "1800"))

# 生成 SRT / 批量翻译文件时，一次交给 Agent 多少行字幕（保持行与时间轴一一对应）。
TRANSLATE_BATCH_SIZE = int(os.environ.get("TRANSLATE_BATCH_SIZE", "20"))

# 漏译检测后的逐行补译重试次数
TRANSLATE_RETRY_MAX = int(os.environ.get("TRANSLATE_RETRY_MAX", "2"))

# 导出 SRT 时：超长字幕按句末标点拆条，并在「本条开始 ~ 下一条开始」间按字数比例分配时间。
SRT_SPLIT_LONG_CUES = os.environ.get("SRT_SPLIT_LONG_CUES", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SRT_SPLIT_MIN_CHARS = int(os.environ.get("SRT_SPLIT_MIN_CHARS", "30"))
SRT_SPLIT_MIN_GAP_SEC = float(os.environ.get("SRT_SPLIT_MIN_GAP_SEC", "1.5"))
SRT_SPLIT_MIN_PART_DURATION = float(os.environ.get("SRT_SPLIT_MIN_PART_DURATION", "0.8"))

# 可选：是否允许下载 YouTube 原视频（默认关闭，避免误用与资源占用）
ENABLE_YOUTUBE_VIDEO_DOWNLOAD = os.environ.get("ENABLE_YOUTUBE_VIDEO_DOWNLOAD", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# 流式下载完成后暂存文件，供前端按 token 拉取（默认 1 小时过期）
_VIDEO_DOWNLOAD_CACHE_TTL = int(os.environ.get("VIDEO_DOWNLOAD_CACHE_TTL", "3600"))
_video_download_cache: dict[str, dict] = {}


def _require_youtube_video_download() -> None:
    if not ENABLE_YOUTUBE_VIDEO_DOWNLOAD:
        raise HTTPException(
            status_code=403,
            detail="已禁用“下载原视频”功能。请在 .env 设置 ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1 后重启后端。",
        )


def _purge_expired_video_cache() -> None:
    now = time.time()
    expired = [
        token
        for token, entry in _video_download_cache.items()
        if now - float(entry.get("created", 0)) > _VIDEO_DOWNLOAD_CACHE_TTL
    ]
    for token in expired:
        _remove_video_cache_entry(token)


def _remove_video_cache_entry(token: str) -> None:
    entry = _video_download_cache.pop(token, None)
    if not entry:
        return
    tmpdir = entry.get("tmpdir")
    if not tmpdir:
        return
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


def _store_video_download(file_path: Path, download_name: str, tmpdir: Path) -> str:
    _purge_expired_video_cache()
    token = uuid.uuid4().hex
    _video_download_cache[token] = {
        "path": file_path,
        "filename": download_name,
        "tmpdir": tmpdir,
        "created": time.time(),
    }
    return token


def _sse(payload: dict) -> str:
    """打包成一条 SSE 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _thought(content: str) -> str:
    return _sse({"type": "thought", "content": content})


def _token(content: str) -> str:
    return _sse({"type": "token", "content": content})


def _progress(done: int, total: int, message: str = "") -> str:
    return _sse({"type": "progress", "done": done, "total": total, "message": message})


def _video_progress(payload: dict) -> str:
    return _sse({"type": "video_progress", **payload})


def _video_ready(token: str, filename: str) -> str:
    return _sse({"type": "video_ready", "token": token, "filename": filename})


def _srt_ready(filename: str, content: str) -> str:
    return _sse({"type": "srt", "filename": filename, "content": content})


# ------------------------- 编号批量翻译（保留行与时间轴对齐） -------------------------

# 匹配模型返回的 “[3] 译文” 这种带行号的行
_NUMBERED_LINE_RE = re.compile(r"^\s*[\[【]\s*(\d+)\s*[\]】]\s?(.*)$")

_SUBTITLE_OUTPUT_RULES = (
    "5. 工具调用在后台完成，最终输出中禁止出现「查一下」「萌娘百科」「维基」「术语库」等过程描述；\n"
    "6. 禁止「原文→译文」对照格式，只输出单行中文字幕；\n"
    "7. 禁止保留日文假名，语气词也要译成中文。"
)


def _chunk_text_content(chunk) -> str:
    """从流式 chunk 取出正文，忽略 reasoning_content 等非译文通道。"""
    content = chunk.content
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
    return ""


def _normalize_agent_output(text: str) -> str:
    """清洗 Agent 最终输出，供批量/单行翻译解析使用。"""
    return _strip_thinking_markers(text)


def _parse_numbered(output: str, expected: int) -> list[str] | None:
    """解析模型返回的带行号译文。行号必须正好覆盖 1..expected，否则返回 None 触发降级。"""
    mapping: dict[int, str] = {}
    for raw in output.splitlines():
        m = _NUMBERED_LINE_RE.match(raw)
        if m:
            mapping[int(m.group(1))] = m.group(2).strip()

    if len(mapping) != expected:
        return None
    result: list[str] = []
    for i in range(1, expected + 1):
        if i not in mapping:
            return None
        result.append(mapping[i])
    return result


def _join_rolling_translation(prev_trans: str, suffix_trans: str) -> str:
    """拼接滚动字幕：已译前缀 + 新译后缀。"""
    left = (prev_trans or "").strip()
    right = (suffix_trans or "").strip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith(("、", "，", "。", "！", "？", "…", "—", "-", "：", ":")):
        return left + right
    return left + right


def _tool_start_thought(tool_name: str) -> str:
    if tool_name == "lookup_wiki":
        return f"\n【动作】本地术语库未覆盖，调用 Wiki 查询 [{tool_name}]...\n"
    return f"\n【动作】调用术语工具 [{tool_name}] 检索本地词典...\n"


def _tool_end_thought(tool_name: str) -> str:
    if tool_name == "lookup_wiki":
        return "\n【Wiki】在线查询完成，已将条目摘要交给翻译 Agent。\n\n"
    return "\n【RAG】术语检索完成，已将相关术语作为上下文交给翻译 Agent。\n\n"


async def _collect_agent_output(prompt: str):
    """运行 Agent 并流式产出思考过程帧，最终返回最后一轮 LLM 的译文 output。

    Agent 多轮循环时，中间轮次常含工具决策/推理文本；只保留最后一轮，
    避免思考内容混入 SRT。
    """
    final_output = ""
    current_run_parts: list[str] = []
    async for event in agent_executor.astream_events({"input": prompt}, version="v2"):
        kind = event["event"]
        if kind == "on_tool_start":
            tool_name = event["name"]
            yield _thought(_tool_start_thought(tool_name)), None
        elif kind == "on_tool_end":
            tool_name = event["name"]
            yield _thought(_tool_end_thought(tool_name)), None
        elif kind == "on_chat_model_start":
            current_run_parts = []
        elif kind == "on_chat_model_stream":
            text = _chunk_text_content(event["data"]["chunk"])
            if text:
                current_run_parts.append(text)
        elif kind == "on_chat_model_end":
            if current_run_parts:
                final_output = "".join(current_run_parts)
    yield None, _normalize_agent_output(final_output)


def _inject_prompt_context(
    body: str,
    task_hint: str,
    *,
    video_context: str | None = None,
    glossary_block: str | None = None,
) -> str:
    """把视频背景与术语表拼进用户 prompt。"""
    segments: list[str] = []
    if video_context:
        segments.append(video_context)
        segments.append(f"请结合以上视频背景，{task_hint}：")
    elif glossary_block:
        segments.append(f"请{task_hint}：")
    if glossary_block:
        segments.append(glossary_block)
    if segments:
        return "\n\n".join(segments) + "\n\n" + body
    return body


def _batch_prompt(
    batch: list[str],
    video_context: str | None = None,
    glossary_block: str | None = None,
) -> str:
    numbered = "\n".join(f"[{i + 1}] {line}" for i, line in enumerate(batch))
    body = (
        f"{ASR_AWARE_RULES}\n\n"
        "下面是按行号排列的视频字幕，请逐行翻译成地道、口语化的中文。\n"
        "严格要求：\n"
        "1. 必须保留每行的 [序号] 前缀，且序号与原文对应；\n"
        "2. 输出行数必须和输入完全一致，禁止合并、拆分、新增或删除行；\n"
        "3. 每行只输出该行译文，不要任何额外解释；\n"
        "4. 单行若明显是 ASR 错字，按推断出的真实语义译，勿照抄错字含义。\n"
        f"{_SUBTITLE_OUTPUT_RULES}\n\n"
        f"{numbered}"
    )
    return _inject_prompt_context(
        body,
        "翻译下列字幕行",
        video_context=video_context,
        glossary_block=glossary_block,
    )


def _line_prompt(
    line: str,
    video_context: str | None = None,
    glossary_block: str | None = None,
    *,
    prev_line: str = "",
    next_line: str = "",
    strict: bool = False,
) -> str:
    context_lines = ""
    if prev_line.strip() or next_line.strip():
        context_lines = (
            f"上一句字幕：{prev_line.strip() or '（无）'}\n"
            f"下一句字幕：{next_line.strip() or '（无）'}\n\n"
        )
    body = (
        f"{ASR_AWARE_RULES}\n\n"
        "请将下列内容翻译成地道、口语化的中文。\n"
        "严格要求：\n"
        "1. 只输出译文本身（单行），不要任何解释、不要分点、不要 Markdown；\n"
        "2. 不要输出“翻译：/译文：/参考翻译：”等前缀；\n"
        "3. 若原文不完整或缺上下文，也必须给出尽量贴近原意的一行译文，不要提问；\n"
        "4. 必须输出中文，禁止保留日文假名原文；\n"
        "5. 若本句明显是 ASR 错字，按推断出的真实语义译，勿照抄错字含义。\n"
        f"{_SUBTITLE_OUTPUT_RULES}\n\n"
        f"{context_lines}"
        f"待翻译句：{line}"
    )
    if strict:
        body += (
            "\n\n【重要】上次输出含工具/查资料描述，已被丢弃。"
            "请直接给出该行口语化中文字幕，禁止任何过程说明。"
        )
    return _inject_prompt_context(
        body,
        "翻译以下内容",
        video_context=video_context,
        glossary_block=glossary_block,
    )


async def _proofread_line_wrapped(
    source: str,
    draft: str,
    video_context: str | None = None,
    glossary_block: str | None = None,
    *,
    prev_line: str = "",
    next_line: str = "",
) -> str:
    """校对单行译文；空初译直接返回。"""
    if not (draft or "").strip():
        return draft
    body = proofread_line_user(
        source, draft, prev_line=prev_line, next_line=next_line
    )
    prompt = _inject_prompt_context(
        body,
        "校对以下译文",
        video_context=video_context,
        glossary_block=glossary_block,
    )
    raw = await invoke_proofread(prompt)
    return _finalize_translation_line(raw)


async def _proofread_batch_wrapped(
    batch: list[str],
    outputs: list[str],
    video_context: str | None = None,
    glossary_block: str | None = None,
) -> list[str]:
    """校对一批译文；解析失败则回退初译。"""
    if not batch or len(batch) != len(outputs):
        return outputs
    body = proofread_batch_user(list(zip(batch, outputs)))
    prompt = _inject_prompt_context(
        body,
        "校对下列字幕译文",
        video_context=video_context,
        glossary_block=glossary_block,
    )
    raw = await invoke_proofread(prompt)
    parsed = _parse_numbered(_normalize_agent_output(raw), len(batch))
    if parsed is None:
        return outputs
    return [_finalize_translation_line(line) for line in parsed]


async def _translate_single_line(
    line: str,
    video_context: str | None = None,
    glossary_block: str | None = None,
    *,
    prev_line: str = "",
    next_line: str = "",
    strict: bool = False,
) -> str:
    """逐行翻译（用于批量失败后的补译）。"""
    prompt = _line_prompt(
        line,
        video_context,
        glossary_block,
        prev_line=prev_line,
        next_line=next_line,
        strict=strict,
    )
    result = await agent_executor.ainvoke({"input": prompt})
    return _finalize_translation_line(str(result.get("output", "")))


async def _repair_translations(
    lines: list[str],
    translations: list[str],
    video_context: str | None = None,
    glossary_block: str | None = None,
) -> int:
    """补译漏翻行：滚动字幕增量拼接 → 同文复用 → 邻句上下文逐行重试。"""
    if len(lines) != len(translations):
        return 0

    repaired = 0

    for i in range(1, len(lines)):
        src = lines[i].strip()
        if not _needs_retranslation(src, translations[i]):
            continue
        prev_src = lines[i - 1].strip()
        prev_trans = (translations[i - 1] or "").strip()
        if (
            prev_src
            and src.startswith(prev_src)
            and prev_trans
            and not _needs_retranslation(prev_src, prev_trans)
        ):
            suffix = src[len(prev_src) :].lstrip()
            if not suffix:
                translations[i] = prev_trans
            else:
                suffix_trans = await _translate_single_line(
                    suffix, video_context, glossary_block
                )
                translations[i] = _join_rolling_translation(prev_trans, suffix_trans)
            if not _needs_retranslation(src, translations[i]):
                repaired += 1
                continue

    cache: dict[str, str] = {}
    for src, trans in zip(lines, translations):
        key = src.strip()
        if key and not _needs_retranslation(key, trans):
            cache[key] = trans

    for i, (src, trans) in enumerate(zip(lines, translations)):
        key = src.strip()
        if not key or not _needs_retranslation(key, trans):
            continue
        if key in cache:
            translations[i] = cache[key]
            repaired += 1

    for i, (src, trans) in enumerate(zip(lines, translations)):
        if not _needs_retranslation(src, trans):
            continue
        prev_line = lines[i - 1] if i > 0 else ""
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        for _ in range(TRANSLATE_RETRY_MAX):
            fixed = await _translate_single_line(
                src,
                video_context,
                glossary_block,
                prev_line=prev_line,
                next_line=next_line,
                strict=True,
            )
            if not _needs_retranslation(src, fixed):
                translations[i] = fixed
                repaired += 1
                cache[src.strip()] = fixed
                break

    return repaired


def _preview_prompt(
    chunk: str,
    video_context: str | None = None,
    glossary_block: str | None = None,
) -> str:
    body = (
        f"{ASR_AWARE_RULES}\n\n"
        "以下为连续字幕原文。请理解说话人实际想表达的内容，"
        "按口语化中文输出译文（可分段，保持自然断句）：\n\n"
        f"{chunk}"
    )
    return _inject_prompt_context(
        body,
        "理解并翻译以下字幕内容",
        video_context=video_context,
        glossary_block=glossary_block,
    )


async def _translate_one_batch(
    batch: list[str],
    video_context: str | None = None,
    glossary_block: str | None = None,
) -> list[str]:
    """翻译一批字幕行，返回与输入一一对应的译文列表。

    优先用“带行号一次性翻译”省调用；若模型乱删/合并行导致行数对不上，
    自动降级为逐行翻译，保证时间轴对齐绝不串行。
    """
    prompt = _batch_prompt(batch, video_context, glossary_block)
    result = await agent_executor.ainvoke({"input": prompt})
    parsed = _parse_numbered(_normalize_agent_output(str(result.get("output", ""))), len(batch))
    outputs = (
        [_finalize_translation_line(line) for line in parsed]
        if parsed is not None
        else None
    )

    if outputs is None:
        outputs = []
        for line in batch:
            single = await agent_executor.ainvoke(
                {"input": _line_prompt(line, video_context, glossary_block)}
            )
            outputs.append(_finalize_translation_line(str(single.get("output", ""))))

    for j, (src, out) in enumerate(zip(batch, outputs)):
        if _needs_retranslation(src, out):
            outputs[j] = await _translate_single_line(
                src, video_context, glossary_block, strict=True
            )

    if proofread_enabled():
        outputs = await _proofread_batch_wrapped(
            batch, outputs, video_context, glossary_block
        )
    return outputs


async def _translate_one_batch_streaming(
    batch: list[str],
    video_context: str | None = None,
    glossary_block: str | None = None,
):
    """带思考过程流式输出的批量翻译；yield 思考帧，最终 yield (None, 译文列表)。"""
    prompt = _batch_prompt(batch, video_context, glossary_block)
    full_output = ""
    async for frame, text in _collect_agent_output(prompt):
        if frame is not None:
            yield frame, None
        else:
            full_output = text or ""

    parsed = _parse_numbered(full_output, len(batch))
    outputs = (
        [_finalize_translation_line(line) for line in parsed]
        if parsed is not None
        else None
    )

    if outputs is None:
        outputs = []
        for line in batch:
            yield _thought(f"\n【降级】逐行翻译：{line[:40]}{'…' if len(line) > 40 else ''}\n"), None
            line_output = ""
            async for frame, text in _collect_agent_output(
                _line_prompt(line, video_context, glossary_block)
            ):
                if frame is not None:
                    yield frame, None
                else:
                    line_output = text or ""
            outputs.append(_finalize_translation_line(line_output))

    for j, (src, out) in enumerate(zip(batch, outputs)):
        if _needs_retranslation(src, out):
            yield _thought(f"\n【补译】第 {j + 1} 行疑似漏译，正在重试…\n"), None
            outputs[j] = await _translate_single_line(
                src, video_context, glossary_block, strict=True
            )

    if proofread_enabled():
        yield (
            _thought(
                "\n【校对 Agent】正在复审本批译文（专名一致 / 去元叙述 / ASR 复核）...\n"
            ),
            None,
        )
        outputs = await _proofread_batch_wrapped(
            batch, outputs, video_context, glossary_block
        )

    yield None, outputs


async def translate_lines_keep_index(
    lines: list[str],
    batch_size: int = TRANSLATE_BATCH_SIZE,
    video_context: str | None = None,
    glossary_block: str | None = None,
):
    """把多行文本分批翻译，结果与输入逐行对齐（无流式进度）。"""
    total = len(lines)
    for start in range(0, total, batch_size):
        batch = lines[start:start + batch_size]
        translated = await _translate_one_batch(batch, video_context, glossary_block)
        yield (min(start + len(batch), total), total, start, translated)


async def translate_lines_streaming(
    lines: list[str],
    batch_size: int = TRANSLATE_BATCH_SIZE,
    video_context: str | None = None,
    glossary_block: str | None = None,
):
    """分批翻译并流式推送进度与 Agent 思考过程。"""
    total = len(lines)
    batch_total = max(1, (total + batch_size - 1) // batch_size)
    batch_index = 0

    for start in range(0, total, batch_size):
        batch_index += 1
        batch = lines[start:start + batch_size]
        done = min(start + len(batch), total)
        yield ("progress", done, total, batch_index, batch_total)

        translated: list[str] | None = None
        async for frame, result in _translate_one_batch_streaming(
            batch, video_context, glossary_block
        ):
            if frame is not None:
                yield ("thought", frame)
            else:
                translated = result
        if translated is None:
            translated = []
        yield ("batch", start, translated)


def _context_thought(video_context: str) -> str:
    """思考区展示已加载的视频背景摘要。"""
    preview = video_context.strip()
    if len(preview) > 400:
        preview = preview[:400] + "…"
    return f"\n【视频背景】已注入翻译上下文：\n{preview}\n\n"


async def _fetch_youtube_payload(url: str) -> tuple[str, list[dict], str, dict]:
    """并行抓取字幕与视频背景，返回 (video_id, snippets, formatted_context, raw_context)。"""
    video_id = extract_video_id(url)
    snippets, ctx = await asyncio.gather(
        asyncio.to_thread(fetch_transcript, video_id),
        asyncio.to_thread(fetch_video_context, video_id),
    )
    return video_id, snippets, format_video_context(ctx), ctx


def _domain_thought(raw_ctx: dict) -> str:
    domains = infer_term_domains(raw_ctx)
    return f"\n【术语库】{format_domain_selection(domains)}\n\n"


def _franchise_thought(raw_ctx: dict) -> str:
    hints = infer_franchise_hints(raw_ctx)
    return f"\n【Wiki 上下文】{format_franchise_selection(hints)}\n\n"


# ------------------------- SRT 生成 -------------------------

# 在句末标点后切分（保留标点在前一段末尾）
_SRT_PUNCT_SPLIT_RE = re.compile(r"(?<=[。！？!?…])")


def _split_text_by_punctuation(text: str) -> list[str]:
    """按句末标点拆成多条字幕文案；无标点时返回整段。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SRT_PUNCT_SPLIT_RE.split(text) if p.strip()]
    return parts if parts else [text]


def _allocate_times_by_char_weight(
    start: float,
    end: float,
    weights: list[int],
    min_part: float,
) -> list[tuple[float, float]]:
    """在 [start, end] 内按权重（通常为每段字数）分配子时间轴。"""
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [(start, end)]

    span = end - start
    if span <= 0:
        span = min_part * n
        end = start + span

    wsum = sum(max(1, w) for w in weights)
    durations = [span * (max(1, w) / wsum) for w in weights]

    # 保证每段不低于最短显示时长，再按比例缩放回总 span
    durations = [max(min_part, d) for d in durations]
    total = sum(durations)
    if total > span:
        scale = span / total
        durations = [d * scale for d in durations]

    windows: list[tuple[float, float]] = []
    cursor = start
    for i, dur in enumerate(durations):
        if i == n - 1:
            windows.append((cursor, end))
        else:
            part_end = cursor + dur
            windows.append((cursor, part_end))
            cursor = part_end
    return windows


def _compute_snippet_times(snippets: list[dict], min_duration: float = 0.1) -> list[tuple[float, float]]:
    """根据 YouTube 片段计算每条的起止时间，并截断与下一条的重叠。"""
    count = len(snippets)
    times: list[tuple[float, float]] = []
    for i, snip in enumerate(snippets):
        start = float(snip.get("start") or 0.0)
        duration = snip.get("duration")
        if duration is not None:
            end = start + float(duration)
        elif i + 1 < count:
            end = float(snippets[i + 1].get("start") or (start + 2.0))
        else:
            end = start + 2.0
        if end <= start:
            end = start + 1.0
        times.append((start, end))

    for i in range(count - 1):
        start_i, end_i = times[i]
        next_start = times[i + 1][0]
        if end_i > next_start:
            end_i = next_start
        if end_i <= start_i:
            end_i = start_i + min_duration
        times[i] = (start_i, end_i)
    return times


def _expand_split_cues(
    times: list[tuple[float, float]],
    translations: list[str],
    snippets: list[dict],
) -> list[tuple[float, float, str]]:
    """将超长字幕按标点拆条，并把时间铺到「下一条开始」前的空隙里。"""
    count = len(times)
    expanded: list[tuple[float, float, str]] = []

    for i in range(count):
        start, end = times[i]
        text = (translations[i] or "").strip()
        if not text or _needs_retranslation(str(snippets[i].get("text", "")), text):
            continue

        if not SRT_SPLIT_LONG_CUES or len(text) < SRT_SPLIT_MIN_CHARS:
            expanded.append((start, end, text))
            continue

        parts = _split_text_by_punctuation(text)
        if len(parts) < 2:
            expanded.append((start, end, text))
            continue

        if i + 1 < count:
            next_start = times[i + 1][0]
            gap = next_start - end
            alloc_end = next_start if gap >= SRT_SPLIT_MIN_GAP_SEC else end
        else:
            alloc_end = end

        if alloc_end <= start:
            alloc_end = start + SRT_SPLIT_MIN_PART_DURATION * len(parts)

        windows = _allocate_times_by_char_weight(
            start,
            alloc_end,
            [len(p) for p in parts],
            SRT_SPLIT_MIN_PART_DURATION,
        )
        for (ws, we), part in zip(windows, parts):
            expanded.append((ws, we, part))

    return expanded


def _format_srt_time(seconds: float) -> str:
    """秒 -> SRT 时间戳 HH:MM:SS,mmm。"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _build_srt(snippets: list[dict], translations: list[str]) -> str:
    """用原字幕的时间轴 + 译文拼出标准 SRT 文本。

    YouTube 滚动字幕片段常有重叠；导出前把每条结束时间截到下一条开始之前，
    避免标准播放器出现双线交替/叠显。
    超长译文可按句末标点拆条，并在到下一条字幕开始前的空隙内按字数比例分配时间。
    译文与源文本均为空白的条目会跳过（B 站等平台不接受空字幕块）。
    """
    if not snippets:
        return ""

    times = _compute_snippet_times(snippets)
    cues = _expand_split_cues(times, translations, snippets)

    blocks: list[str] = []
    for seq, (start, end, line) in enumerate(cues, start=1):
        blocks.append(
            f"{seq}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{line}\n"
        )
    return "\n".join(blocks)


def _chunk_transcript(snippets: list[dict], max_chars: int = YOUTUBE_TRANSLATE_CHUNK_CHARS) -> list[str]:
    """把完整字幕按字幕片段合并成多个翻译块，不再只截取开头。"""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for snippet in snippets:
        text = str(snippet.get("text", "")).strip()
        if not text:
            continue
        projected_len = current_len + len(text) + 1
        if current and projected_len > max_chars:
            chunks.append(" ".join(current))
            current = [text]
            current_len = len(text)
        else:
            current.append(text)
            current_len = projected_len

    if current:
        chunks.append(" ".join(current))

    return chunks


async def stream_agent_events(text: str):
    """复用的核心：把 Agent 的思考/工具调用/最终翻译流式转成 SSE 帧。

    被 /stream_translate 和 /stream_translate_youtube 共用。
    """
    async for event in agent_executor.astream_events({"input": text}, version="v2"):
        kind = event["event"]

        # 侦听：Agent 正决定调用工具 (Tool Use)
        if kind == "on_tool_start":
            tool_name = event["name"]
            yield _thought(_tool_start_thought(tool_name))

        # 侦听：工具调用完毕，得到 RAG 知识点
        elif kind == "on_tool_end":
            tool_name = event["name"]
            yield _thought(_tool_end_thought(tool_name))

        # 侦听：Agent 思考完毕，利用 LLM 开始打出真正的翻译结果！
        elif kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if chunk.content:
                yield _token(chunk.content)


@app.get("/")
def read_root():
    return {"message": "Hello from Python Backend"}


@app.post("/admin/reload-dictionary")
def admin_reload_dictionary():
    count = reload_dictionary()
    return {"ok": True, "count": count}


@app.get("/stream_translate")
async def stream_translate(text: str):
    """挂载了 LangChain Agent 和 RAG 检索的单句流式翻译接口。"""
    async def event_generator():
        yield _thought("【LLM大脑】已接收任务，开始思考分析...\n")
        glossary_block = build_session_glossary([text], None, None)
        glossary_hits = collect_session_glossary_hits([text], None, None)
        yield _thought(format_glossary_thought(glossary_hits))
        try:
            prompt = _line_prompt(text, glossary_block=glossary_block)
            if proofread_enabled():
                draft = ""
                async for frame, collected in _collect_agent_output(prompt):
                    if frame is not None:
                        yield frame
                    else:
                        draft = collected or ""
                draft = _finalize_translation_line(draft)
                if _needs_retranslation(text, draft):
                    draft = await _translate_single_line(
                        text, glossary_block=glossary_block, strict=True
                    )
                yield _thought("\n【校对 Agent】正在复审...\n")
                final = await _proofread_line_wrapped(
                    text, draft, glossary_block=glossary_block
                )
                if final:
                    yield _token(final)
            else:
                async for frame in stream_agent_events(prompt):
                    yield frame
        except Exception as e:
            yield _thought(f"\n【程序异常】{str(e)}")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/stream_translate_youtube")
async def stream_translate_youtube(url: str):
    """输入 YouTube 链接 -> 抓取字幕 -> 调用领域 Agent 流式翻译。"""
    async def event_generator():
        try:
            yield _thought("【YouTube】正在解析链接，并行抓取字幕与视频背景...\n")
            video_id, snippets, video_context, raw_ctx = await _fetch_youtube_payload(url)

            if not snippets:
                yield _thought("\n【YouTube】未获得可用文本。（未找到该视频的字幕）")
                yield "data: [DONE]\n\n"
                return

            if video_context:
                yield _thought(_context_thought(video_context))
            else:
                yield _thought("\n【视频背景】未能获取标题/简介，将仅依据字幕翻译。\n\n")

            yield _thought(_domain_thought(raw_ctx))
            yield _thought(_franchise_thought(raw_ctx))

            transcript_chunks = _chunk_transcript(snippets)
            term_domains = infer_term_domains(raw_ctx)
            franchise_hints = infer_franchise_hints(raw_ctx)
            all_lines = [s["text"] for s in snippets]
            glossary_hits = collect_session_glossary_hits(all_lines, raw_ctx, term_domains)
            glossary_block = build_session_glossary(all_lines, raw_ctx, term_domains)
            yield _thought(format_glossary_thought(glossary_hits))

            yield _thought(
                f"\n【YouTube】已抓取 {len(snippets)} 段字幕（视频ID：{video_id}），"
                f"将完整字幕拆成 {len(transcript_chunks)} 个块进行翻译...\n\n"
            )

            with term_domains_scope(term_domains):
                with wiki_franchise_scope(franchise_hints):
                    for index, transcript_chunk in enumerate(transcript_chunks, start=1):
                        if len(transcript_chunks) > 1:
                            yield _token(f"\n\n【第 {index}/{len(transcript_chunks)} 段】\n")
                        yield _thought(f"—— 原文第 {index}/{len(transcript_chunks)} 段 ——\n{transcript_chunk}\n\n")
                        async for frame in stream_agent_events(
                            _preview_prompt(transcript_chunk, video_context, glossary_block)
                        ):
                            yield frame
        except Exception as e:
            yield _thought(
                f"\n【抓取/翻译失败】{type(e).__name__}: {str(e)}\n"
                "（提示：国内访问 YouTube 需代理，请在 .env 配置 YOUTUBE_PROXY，"
                "例如 http://127.0.0.1:10809 或 socks5://127.0.0.1:10808）"
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/download_translated_srt")
async def download_translated_srt(url: str):
    """输入 YouTube 链接 -> 抓字幕(含时间轴) -> 批量翻译 -> 返回可下载的 .srt 文件。

    供脚本/直连下载；网页请用 /stream_translate_youtube_srt 以获取进度与思考过程。
    """
    try:
        video_id, snippets, video_context, raw_ctx = await _fetch_youtube_payload(url)
    except Exception as e:
        return PlainTextResponse(
            f"抓取字幕失败：{type(e).__name__}: {e}\n"
            "（国内访问 YouTube 需在 .env 配置 YOUTUBE_PROXY）",
            status_code=502,
        )

    if not snippets:
        return PlainTextResponse("未找到该视频的字幕。", status_code=404)

    lines = [s["text"] for s in snippets]
    translations: list[str] = [""] * len(lines)
    term_domains = infer_term_domains(raw_ctx)
    franchise_hints = infer_franchise_hints(raw_ctx)
    glossary_block = build_session_glossary(lines, raw_ctx, term_domains)
    with term_domains_scope(term_domains):
        with wiki_franchise_scope(franchise_hints):
            async for _done, _total, start, batch in translate_lines_keep_index(
                lines, video_context=video_context, glossary_block=glossary_block
            ):
                for offset, text in enumerate(batch):
                    translations[start + offset] = text
            await _repair_translations(lines, translations, video_context, glossary_block)

    srt_content = _build_srt(snippets, translations)
    encoded_filename = quote(f"{video_id}_zh.srt")
    return PlainTextResponse(
        content=srt_content,
        media_type="application/x-subrip",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{encoded_filename}"},
    )


@app.get("/youtube_video_formats")
async def youtube_video_formats(url: str):
    """列出 YouTube 视频可选画质（供前端下拉选择）。"""
    _require_youtube_video_download()
    try:
        return list_video_quality_options(url)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"获取视频格式失败：{type(e).__name__}: {e}\n"
                "（提示：国内访问 YouTube 需在 .env 配置 YOUTUBE_PROXY；"
                "若提示登录/校验，请设置 YOUTUBE_COOKIES_FROM_BROWSER 并确保已登录。）"
            ),
        )


@app.get("/download_youtube_video")
async def download_youtube_video(url: str, background: BackgroundTasks, quality: str = "best"):
    """下载 YouTube 原视频（可选功能，默认关闭）。

    返回二进制文件下载。会下载到临时目录，响应结束后自动清理。
    """
    _require_youtube_video_download()

    tmpdir = Path(tempfile.mkdtemp(prefix="yt_video_"))

    def _cleanup():
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    background.add_task(_cleanup)

    try:
        file_path, download_name = download_video_file(url, output_dir=tmpdir, quality=quality)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"下载视频失败：{type(e).__name__}: {e}\n"
                "（提示：国内访问 YouTube 需在 .env 配置 YOUTUBE_PROXY；"
                "若提示登录/校验，请设置 YOUTUBE_COOKIES_FROM_BROWSER 并确保已登录；"
                "必要时更新 yt-dlp。）"
            ),
        )

    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=download_name,
        background=background,
    )


@app.get("/download_youtube_video_file")
async def download_youtube_video_file(token: str, background: BackgroundTasks):
    """按 token 拉取已缓存的 YouTube 视频文件（配合流式下载端点）。"""
    _require_youtube_video_download()
    _purge_expired_video_cache()
    entry = _video_download_cache.get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="下载链接已过期或不存在，请重新下载。")

    file_path = entry["path"]
    download_name = entry["filename"]
    tmpdir = entry["tmpdir"]

    def _cleanup():
        _remove_video_cache_entry(token)

    background.add_task(_cleanup)

    return FileResponse(
        path=str(file_path),
        media_type="application/octet-stream",
        filename=download_name,
        background=background,
    )


@app.get("/stream_download_youtube_video")
async def stream_download_youtube_video(url: str, quality: str = "best"):
    """流式下载 YouTube 原视频：推送 yt-dlp 进度，完成后下发 token 供拉取文件。"""
    _require_youtube_video_download()

    async def event_generator():
        tmpdir = Path(tempfile.mkdtemp(prefix="yt_video_"))
        progress_state: dict = {"status": "starting", "message": "正在解析视频信息…", "percent": 0}
        last_key = ""
        done = asyncio.Event()
        error_holder: list[Exception] = []
        result_holder: list[tuple[Path, str]] = []

        def on_progress(payload: dict) -> None:
            progress_state.clear()
            progress_state.update(payload)

        def _run_download():
            try:
                result_holder.append(
                    download_video_file(
                        url,
                        output_dir=tmpdir,
                        quality=quality,
                        progress_callback=on_progress,
                    )
                )
            except Exception as exc:
                error_holder.append(exc)
            finally:
                done.set()

        yield _video_progress(dict(progress_state))
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _run_download)

        while not done.is_set():
            key = json.dumps(progress_state, sort_keys=True, default=str)
            if key != last_key:
                yield _video_progress(dict(progress_state))
                last_key = key
            await asyncio.sleep(0.35)

        if error_holder:
            shutil.rmtree(tmpdir, ignore_errors=True)
            exc = error_holder[0]
            yield _sse(
                {
                    "type": "error",
                    "message": (
                        f"下载视频失败：{type(exc).__name__}: {exc}\n"
                        "（提示：国内访问 YouTube 需在 .env 配置 YOUTUBE_PROXY；"
                        "若提示登录/校验，请设置 YOUTUBE_COOKIES_FROM_BROWSER 并确保已登录；"
                        "必要时更新 yt-dlp。）"
                    ),
                }
            )
            yield "data: [DONE]\n\n"
            return

        file_path, download_name = result_holder[0]
        yield _video_progress({"status": "finished", "message": "下载完成，准备传送文件…", "percent": 100})
        token = _store_video_download(file_path, download_name, tmpdir)
        yield _video_ready(token, download_name)
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/stream_translate_youtube_srt")
async def stream_translate_youtube_srt(url: str):
    """YouTube 链接 -> 流式翻译并导出 SRT（推送进度 + Agent 思考过程，结束时下发文件内容）。"""
    async def event_generator():
        try:
            yield _thought("【SRT 导出】正在解析链接，并行抓取字幕与视频背景...\n")
            video_id, snippets, video_context, raw_ctx = await _fetch_youtube_payload(url)

            if not snippets:
                yield _thought("\n【SRT 导出】未获得可用文本。（未找到该视频的字幕）\n")
                yield "data: [DONE]\n\n"
                return

            if video_context:
                yield _thought(_context_thought(video_context))
            else:
                yield _thought("\n【视频背景】未能获取标题/简介，将仅依据字幕翻译。\n\n")

            yield _thought(_domain_thought(raw_ctx))
            yield _thought(_franchise_thought(raw_ctx))

            lines = [s["text"] for s in snippets]
            translations: list[str] = [""] * len(lines)
            batch_total = max(1, (len(lines) + TRANSLATE_BATCH_SIZE - 1) // TRANSLATE_BATCH_SIZE)
            term_domains = infer_term_domains(raw_ctx)
            franchise_hints = infer_franchise_hints(raw_ctx)
            glossary_hits = collect_session_glossary_hits(lines, raw_ctx, term_domains)
            glossary_block = build_session_glossary(lines, raw_ctx, term_domains)
            yield _thought(format_glossary_thought(glossary_hits))

            yield _thought(
                f"\n【SRT 导出】已抓取 {len(snippets)} 条字幕（视频ID：{video_id}），"
                f"将分 {batch_total} 批翻译（保留时间轴、去重叠）...\n\n"
            )

            with term_domains_scope(term_domains):
                with wiki_franchise_scope(franchise_hints):
                    async for event in translate_lines_streaming(
                        lines, video_context=video_context, glossary_block=glossary_block
                    ):
                        if event[0] == "progress":
                            _, done, total, batch_idx, batches = event
                            pct = int(done * 100 / total) if total else 0
                            yield _progress(
                                done,
                                total,
                                f"翻译进度 {done}/{total} 行（第 {batch_idx}/{batches} 批，{pct}%）",
                            )
                        elif event[0] == "thought":
                            yield event[1]
                        elif event[0] == "batch":
                            start, batch = event[1], event[2]
                            for offset, text in enumerate(batch):
                                translations[start + offset] = text

                    repaired = await _repair_translations(
                        lines, translations, video_context, glossary_block
                    )
                    if repaired:
                        yield _thought(f"\n【SRT 导出】补译完成，修复 {repaired} 条漏翻字幕。\n")

            srt_content = _build_srt(snippets, translations)
            filename = f"{video_id}_zh.srt"
            yield _thought(f"\n【SRT 导出】翻译完成，正在生成 {filename}...\n")
            yield _srt_ready(filename, srt_content)
        except Exception as e:
            yield _thought(
                f"\n【SRT 导出失败】{type(e).__name__}: {str(e)}\n"
                "（提示：国内访问 YouTube 需在 .env 配置 YOUTUBE_PROXY，"
                "例如 http://127.0.0.1:10809 或 socks5://127.0.0.1:10808）"
            )
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/translate-srt")
async def translate_srt(file: UploadFile = File(...)):
    """接收上传的 SRT/TXT 文件，批量翻译后返回译文文件（SRT 保留原时间轴）。"""
    content = await file.read()

    # 解析文件编码
    def decode_content(data: bytes) -> str:
        for encoding in ['utf-8', 'utf-8-sig', 'utf-16', 'gbk']:
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return data.decode('utf-8', errors='ignore')

    text_content = decode_content(content)
    encoded_filename = quote(f"translated_{file.filename}")
    cd_header = f"attachment; filename*=utf-8''{encoded_filename}"

    # SRT 字幕：保留时间轴，只批量翻译每条文本
    if file.filename.endswith(".srt"):
        subs = pysrt.from_string(text_content)
        originals = [sub.text for sub in subs]
        translated = [""] * len(originals)
        glossary_block = build_session_glossary(originals, None, None)
        async for _done, _total, start, batch in translate_lines_keep_index(
            originals, glossary_block=glossary_block
        ):
            for offset, text in enumerate(batch):
                translated[start + offset] = text
        await _repair_translations(originals, translated, glossary_block=glossary_block)
        kept: list = []
        for sub, text in zip(subs, translated):
            line = (text or "").strip()
            if not line:
                continue
            sub.text = line
            kept.append(sub)
        for i, sub in enumerate(kept, 1):
            sub.index = i

        output_io = io.StringIO()
        pysrt.SubRipFile(items=kept).write_into(output_io)
        return PlainTextResponse(
            content=output_io.getvalue(),
            media_type="text/plain",
            headers={"Content-Disposition": cd_header},
        )

    # 普通 TXT：保留空行，批量翻译非空行
    raw_lines = text_content.split("\n")
    non_empty_idx = [i for i, ln in enumerate(raw_lines) if ln.strip()]
    to_translate = [raw_lines[i].strip() for i in non_empty_idx]

    translated_map: dict[int, str] = {}
    glossary_block = build_session_glossary(to_translate, None, None)
    async for _done, _total, start, batch in translate_lines_keep_index(
        to_translate, glossary_block=glossary_block
    ):
        for offset, text in enumerate(batch):
            translated_map[non_empty_idx[start + offset]] = text

    out_lines = [translated_map.get(i, "") for i in range(len(raw_lines))]
    return PlainTextResponse(
        content="\n".join(out_lines),
        media_type="text/plain",
        headers={"Content-Disposition": cd_header},
    )