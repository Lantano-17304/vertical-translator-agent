import io
import re
import json
import os
import tempfile
import asyncio
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
from app.term_domains import infer_term_domains, format_domain_selection
from app.tools.dictionary import term_domains_scope
from app.youtube_utils import (
    extract_video_id,
    fetch_transcript,
    fetch_video_context,
    format_video_context,
    download_video_file,
)
from urllib.parse import quote

app = FastAPI(title="Translator Agent API")

# YouTube 字幕可能很长，按字符数分段交给 Agent，避免单次上下文过长。
YOUTUBE_TRANSLATE_CHUNK_CHARS = int(os.environ.get("YOUTUBE_TRANSLATE_CHUNK_CHARS", "1800"))

# 生成 SRT / 批量翻译文件时，一次交给 Agent 多少行字幕（保持行与时间轴一一对应）。
TRANSLATE_BATCH_SIZE = int(os.environ.get("TRANSLATE_BATCH_SIZE", "20"))

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


def _sse(payload: dict) -> str:
    """打包成一条 SSE 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _thought(content: str) -> str:
    return _sse({"type": "thought", "content": content})


def _token(content: str) -> str:
    return _sse({"type": "token", "content": content})


def _progress(done: int, total: int, message: str = "") -> str:
    return _sse({"type": "progress", "done": done, "total": total, "message": message})


def _srt_ready(filename: str, content: str) -> str:
    return _sse({"type": "srt", "filename": filename, "content": content})


# ------------------------- 编号批量翻译（保留行与时间轴对齐） -------------------------

# 匹配模型返回的 “[3] 译文” 这种带行号的行
_NUMBERED_LINE_RE = re.compile(r"^\s*[\[【]\s*(\d+)\s*[\]】]\s?(.*)$")

# 模型“思考链”常见包裹标记（DeepSeek / R1 等）
_THINKING_BLOCK_RE = re.compile(
    r"``|``|\[think\].*?\[/think\]",
    re.DOTALL | re.IGNORECASE,
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


def _strip_thinking_markers(text: str) -> str:
    """去掉模型推理块，避免思考内容进入 SRT。"""
    if not text:
        return ""
    cleaned = _THINKING_BLOCK_RE.sub("", text)
    return cleaned.strip()


_LEADING_JUNK_RE = re.compile(
    r"^\s*(?:[-*#>\s]*)(?:翻译(?:结果)?[:：]|译文[:：]|参考翻译[:：])\s*",
    re.IGNORECASE,
)


def _normalize_translation_line(text: str) -> str:
    """把模型输出收敛成“单行译文”，用于 SRT/TXT 逐行翻译兜底。

    兜底场景下模型偶尔会输出解释/分段/Markdown；这里做保守清洗：
    - 去掉 <think> 块
    - 丢弃明显是“说明/分隔符”的行
    - 优先取最后一条像译文的非空行
    """
    cleaned = _strip_thinking_markers(text or "")
    if not cleaned:
        return ""

    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
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

    candidates = [ln for ln in lines if not is_noise(ln)]
    picked = (candidates[-1] if candidates else lines[-1]).strip()
    picked = picked.lstrip("> ").strip()
    picked = _LEADING_JUNK_RE.sub("", picked).strip()
    # 去掉常见加粗/引用符号
    picked = picked.strip("*").strip()
    return picked


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
            yield _thought(f"\n【动作】可能存在生词，调用字典工具 [{tool_name}] 进行术语检索...\n"), None
        elif kind == "on_tool_end":
            yield _thought("\n【RAG】术语检索完成，已将相关术语作为上下文交给翻译 Agent。\n\n"), None
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


def _inject_video_context(video_context: str | None, text: str, task_hint: str) -> str:
    """把视频背景拼进用户 prompt。"""
    if not video_context:
        return text
    return f"{video_context}\n\n请结合以上视频背景，{task_hint}：\n{text}"


def _batch_prompt(batch: list[str], video_context: str | None = None) -> str:
    numbered = "\n".join(f"[{i + 1}] {line}" for i, line in enumerate(batch))
    body = (
        "下面是按行号排列的视频字幕，请逐行翻译成地道、口语化的中文。\n"
        "严格要求：\n"
        "1. 必须保留每行的 [序号] 前缀，且序号与原文对应；\n"
        "2. 输出行数必须和输入完全一致，禁止合并、拆分、新增或删除行；\n"
        "3. 每行只输出该行译文，不要任何额外解释。\n\n"
        f"{numbered}"
    )
    return _inject_video_context(video_context, body, "翻译下列字幕行")


def _line_prompt(line: str, video_context: str | None = None) -> str:
    body = (
        "请将下列内容翻译成地道、口语化的中文。\n"
        "严格要求：\n"
        "1. 只输出译文本身（单行），不要任何解释、不要分点、不要 Markdown；\n"
        "2. 不要输出“翻译：/译文：/参考翻译：”等前缀；\n"
        "3. 若原文不完整或缺上下文，也必须给出尽量贴近原意的一行译文，不要提问。\n\n"
        f"{line}"
    )
    return _inject_video_context(video_context, body, "翻译以下内容")


def _preview_prompt(chunk: str, video_context: str | None = None) -> str:
    return _inject_video_context(video_context, chunk, "理解并翻译以下字幕内容")


async def _translate_one_batch(batch: list[str], video_context: str | None = None) -> list[str]:
    """翻译一批字幕行，返回与输入一一对应的译文列表。

    优先用“带行号一次性翻译”省调用；若模型乱删/合并行导致行数对不上，
    自动降级为逐行翻译，保证时间轴对齐绝不串行。
    """
    prompt = _batch_prompt(batch, video_context)
    result = await agent_executor.ainvoke({"input": prompt})
    parsed = _parse_numbered(_normalize_agent_output(str(result.get("output", ""))), len(batch))
    if parsed is not None:
        return parsed

    fallback: list[str] = []
    for line in batch:
        single = await agent_executor.ainvoke({"input": _line_prompt(line, video_context)})
        fallback.append(_normalize_translation_line(str(single.get("output", ""))))
    return fallback


async def _translate_one_batch_streaming(batch: list[str], video_context: str | None = None):
    """带思考过程流式输出的批量翻译；yield 思考帧，最终 yield (None, 译文列表)。"""
    prompt = _batch_prompt(batch, video_context)
    full_output = ""
    async for frame, text in _collect_agent_output(prompt):
        if frame is not None:
            yield frame, None
        else:
            full_output = text or ""

    parsed = _parse_numbered(full_output, len(batch))
    if parsed is not None:
        yield None, parsed
        return

    fallback: list[str] = []
    for line in batch:
        yield _thought(f"\n【降级】逐行翻译：{line[:40]}{'…' if len(line) > 40 else ''}\n"), None
        line_output = ""
        async for frame, text in _collect_agent_output(_line_prompt(line, video_context)):
            if frame is not None:
                yield frame, None
            else:
                line_output = text or ""
        fallback.append(_normalize_translation_line(line_output))
    yield None, fallback


async def translate_lines_keep_index(
    lines: list[str],
    batch_size: int = TRANSLATE_BATCH_SIZE,
    video_context: str | None = None,
):
    """把多行文本分批翻译，结果与输入逐行对齐（无流式进度）。"""
    total = len(lines)
    for start in range(0, total, batch_size):
        batch = lines[start:start + batch_size]
        translated = await _translate_one_batch(batch, video_context)
        yield (min(start + len(batch), total), total, start, translated)


async def translate_lines_streaming(
    lines: list[str],
    batch_size: int = TRANSLATE_BATCH_SIZE,
    video_context: str | None = None,
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
        async for frame, result in _translate_one_batch_streaming(batch, video_context):
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
        if not text:
            text = str(snippets[i].get("text", "")).strip()
        if not text:
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
            yield _thought(f"\n【动作】可能存在生词，调用字典工具 [{tool_name}] 进行术语检索...\n")

        # 侦听：工具调用完毕，得到 RAG 知识点
        elif kind == "on_tool_end":
            yield _thought("\n【RAG】术语检索完成，已将相关术语作为上下文交给翻译 Agent。\n\n")

        # 侦听：Agent 思考完毕，利用 LLM 开始打出真正的翻译结果！
        elif kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"]
            if chunk.content:
                yield _token(chunk.content)


@app.get("/")
def read_root():
    return {"message": "Hello from Python Backend"}


@app.get("/stream_translate")
async def stream_translate(text: str):
    """挂载了 LangChain Agent 和 RAG 检索的单句流式翻译接口。"""
    async def event_generator():
        yield _thought("【LLM大脑】已接收任务，开始思考分析...\n")
        try:
            async for frame in stream_agent_events(text):
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

            transcript_chunks = _chunk_transcript(snippets)
            term_domains = infer_term_domains(raw_ctx)

            yield _thought(
                f"\n【YouTube】已抓取 {len(snippets)} 段字幕（视频ID：{video_id}），"
                f"将完整字幕拆成 {len(transcript_chunks)} 个块进行翻译...\n\n"
            )

            with term_domains_scope(term_domains):
                for index, transcript_chunk in enumerate(transcript_chunks, start=1):
                    if len(transcript_chunks) > 1:
                        yield _token(f"\n\n【第 {index}/{len(transcript_chunks)} 段】\n")
                    yield _thought(f"—— 原文第 {index}/{len(transcript_chunks)} 段 ——\n{transcript_chunk}\n\n")
                    async for frame in stream_agent_events(_preview_prompt(transcript_chunk, video_context)):
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
    with term_domains_scope(term_domains):
        async for _done, _total, start, batch in translate_lines_keep_index(lines, video_context=video_context):
            for offset, text in enumerate(batch):
                translations[start + offset] = text

    srt_content = _build_srt(snippets, translations)
    encoded_filename = quote(f"{video_id}_zh.srt")
    return PlainTextResponse(
        content=srt_content,
        media_type="application/x-subrip",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{encoded_filename}"},
    )


@app.get("/download_youtube_video")
async def download_youtube_video(url: str, background: BackgroundTasks):
    """下载 YouTube 原视频（可选功能，默认关闭）。

    返回二进制文件下载。会下载到临时目录，响应结束后自动清理。
    """
    if not ENABLE_YOUTUBE_VIDEO_DOWNLOAD:
        raise HTTPException(
            status_code=403,
            detail="已禁用“下载原视频”功能。请在 .env 设置 ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1 后重启后端。",
        )

    tmpdir = Path(tempfile.mkdtemp(prefix="yt_video_"))

    def _cleanup():
        try:
            for p in tmpdir.glob("*"):
                try:
                    p.unlink()
                except Exception:
                    pass
            tmpdir.rmdir()
        except Exception:
            pass

    background.add_task(_cleanup)

    try:
        file_path, download_name = download_video_file(url, output_dir=tmpdir)
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

            lines = [s["text"] for s in snippets]
            translations: list[str] = [""] * len(lines)
            batch_total = max(1, (len(lines) + TRANSLATE_BATCH_SIZE - 1) // TRANSLATE_BATCH_SIZE)
            term_domains = infer_term_domains(raw_ctx)

            yield _thought(
                f"\n【SRT 导出】已抓取 {len(snippets)} 条字幕（视频ID：{video_id}），"
                f"将分 {batch_total} 批翻译（保留时间轴、去重叠）...\n\n"
            )

            with term_domains_scope(term_domains):
                async for event in translate_lines_streaming(lines, video_context=video_context):
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
        async for _done, _total, start, batch in translate_lines_keep_index(originals):
            for offset, text in enumerate(batch):
                translated[start + offset] = text
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
    async for _done, _total, start, batch in translate_lines_keep_index(to_translate):
        for offset, text in enumerate(batch):
            translated_map[non_empty_idx[start + offset]] = text

    out_lines = [translated_map.get(i, "") for i in range(len(raw_lines))]
    return PlainTextResponse(
        content="\n".join(out_lines),
        media_type="text/plain",
        headers={"Content-Disposition": cd_header},
    )