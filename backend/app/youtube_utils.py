r"""YouTube 字幕抓取工具。

负责：
1. 从各种 YouTube 链接格式中解析出 11 位 videoId；
2. 通过 youtube-transcript-api 抓取字幕，支持走代理（国内访问 YouTube 通常必需）；
3. 当轻量 API 被 YouTube 拦截时，回退到 yt-dlp 只抓字幕轨道（不下载视频）；
4. 通过 yt-dlp 抓取标题/简介等视频背景，注入翻译 prompt。

代理通过环境变量 YOUTUBE_PROXY 配置，例如：
    YOUTUBE_PROXY=http://127.0.0.1:10809      # Clash/V2Ray 的 HTTP 代理端口
    YOUTUBE_PROXY=socks5://127.0.0.1:10808    # SOCKS5 端口(需 PySocks)
未设置时直连。

如果 YouTube 返回 "Sign in to confirm" / No video formats found，可配置：
    YOUTUBE_COOKIES_FROM_BROWSER=firefox
并安装 Node.js，保持 YOUTUBE_REMOTE_COMPONENTS=ejs:github（默认启用）。
或：
    YOUTUBE_COOKIE_FILE=C:\path\to\cookies.txt
"""

import os
import re
import json
import tempfile
import time
from collections.abc import Callable
from html import unescape
from pathlib import Path
from typing import Any

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

from app.ytdlp_config import build_ytdlp_opts

# 注入翻译 prompt 的简介最大字符数（过长会占满上下文）
YOUTUBE_CONTEXT_MAX_CHARS = int(os.environ.get("YOUTUBE_CONTEXT_MAX_CHARS", "1000"))

# 抓取字幕时的语言优先级：日语优先(游戏区主题)，依次回退
PREFERRED_LANGUAGES = ["ja", "en", "zh-Hans", "zh-Hant", "zh", "ko", "es"]

_VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")


def extract_video_id(url_or_id: str) -> str:
    """从 YouTube 链接或裸 ID 中解析出 videoId。

    支持 watch?v=、youtu.be/、shorts/、embed/、live/ 等常见形式。
    解析失败时抛出 ValueError。
    """
    if not url_or_id:
        raise ValueError("未提供 YouTube 链接")

    candidate = url_or_id.strip()

    # 已经是裸 videoId
    if _VIDEO_ID_RE.match(candidate):
        return candidate

    patterns = [
        r"(?:v=|/v/)([0-9A-Za-z_-]{11})",          # watch?v=ID  /v/ID
        r"youtu\.be/([0-9A-Za-z_-]{11})",            # youtu.be/ID
        r"/shorts/([0-9A-Za-z_-]{11})",              # /shorts/ID
        r"/embed/([0-9A-Za-z_-]{11})",               # /embed/ID
        r"/live/([0-9A-Za-z_-]{11})",                # /live/ID
    ]
    for pat in patterns:
        m = re.search(pat, candidate)
        if m:
            return m.group(1)

    raise ValueError(f"无法从输入中解析出 YouTube 视频 ID: {url_or_id}")


def _build_api() -> YouTubeTranscriptApi:
    proxy = os.environ.get("YOUTUBE_PROXY")
    if proxy:
        proxy_config = GenericProxyConfig(http_url=proxy, https_url=proxy)
        return YouTubeTranscriptApi(proxy_config=proxy_config)
    return YouTubeTranscriptApi()


def _proxy_url() -> str | None:
    proxy = os.environ.get("YOUTUBE_PROXY")
    return proxy.strip() if proxy and proxy.strip() else None


def _base_ydl_opts() -> dict:
    """yt-dlp 通用选项：只拉 metadata/字幕，不下载视频。"""
    return build_ytdlp_opts(skip_download=True)


def _truncate_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 12].rstrip() + "…(简介已截断)"


def fetch_video_context(video_id: str) -> dict:
    """用 yt-dlp 抓取视频标题、频道、简介等背景（不下载视频）。

    失败时返回空 dict，不阻断字幕翻译流程。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(_base_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return {}

    description = _truncate_text(info.get("description") or "", YOUTUBE_CONTEXT_MAX_CHARS)
    tags = info.get("tags") or []
    tag_preview = ", ".join(str(t) for t in tags[:8]) if tags else ""

    return {
        "video_id": video_id,
        "title": (info.get("title") or "").strip(),
        "channel": (info.get("channel") or info.get("uploader") or "").strip(),
        "description": description,
        "tags": tag_preview,
    }


def format_video_context(ctx: dict) -> str:
    """把视频元数据格式化为可注入翻译 prompt 的背景文本。"""
    if not ctx:
        return ""

    parts = ["【视频背景】"]
    if ctx.get("title"):
        parts.append(f"标题：{ctx['title']}")
    if ctx.get("channel"):
        parts.append(f"频道：{ctx['channel']}")
    if ctx.get("description"):
        parts.append(f"简介：{ctx['description']}")
    if ctx.get("tags"):
        parts.append(f"标签：{ctx['tags']}")

    if len(parts) == 1:
        return ""
    return "\n".join(parts)


def _normalize_snippets(fetched) -> list[dict]:
    snippets = []
    for snip in fetched:
        text = getattr(snip, "text", None)
        if text is None and isinstance(snip, dict):
            text = snip.get("text", "")
        start = getattr(snip, "start", None)
        if start is None and isinstance(snip, dict):
            start = snip.get("start", 0.0)
        # duration 用于生成 SRT 的结束时间戳；拿不到时留 None，由上层兜底推算。
        duration = getattr(snip, "duration", None)
        if duration is None and isinstance(snip, dict):
            duration = snip.get("duration")
        snippets.append({
            "text": (text or "").strip(),
            "start": start or 0.0,
            "duration": duration,
        })
    return [s for s in snippets if s["text"]]


def _fetch_with_transcript_api(video_id: str) -> list[dict]:
    api = _build_api()
    try:
        fetched = api.fetch(video_id, languages=PREFERRED_LANGUAGES)
    except Exception:
        # 回退：列出该视频所有可用字幕，取第一个能抓到的
        transcript_list = api.list(video_id)
        fetched = None
        for transcript in transcript_list:
            try:
                fetched = transcript.fetch()
                break
            except Exception:
                continue
        if fetched is None:
            raise
    return _normalize_snippets(fetched)


def _select_caption_track(captions: dict) -> dict | None:
    if not captions:
        return None

    def find_lang(lang: str) -> str | None:
        if lang in captions:
            return lang
        # YouTube 自动字幕有时会返回 ja-orig / en-US 这类 key
        for key in captions:
            if key.startswith(lang + "-"):
                return key
        return None

    selected_lang = None
    for lang in PREFERRED_LANGUAGES:
        selected_lang = find_lang(lang)
        if selected_lang:
            break
    if selected_lang is None:
        selected_lang = next(iter(captions))

    tracks = captions.get(selected_lang) or []
    for preferred_ext in ("json3", "vtt"):
        for track in tracks:
            if track.get("ext") == preferred_ext and track.get("url"):
                return track
    for track in tracks:
        if track.get("url"):
            return track
    return None


def _select_caption_lang(captions: dict) -> str | None:
    if not captions:
        return None

    # yt-dlp 可能把直播聊天回放暴露成 live_chat，这不是字幕，不能拿来翻译。
    valid_captions = {
        lang: tracks
        for lang, tracks in captions.items()
        if lang != "live_chat"
        and any(track.get("url") for track in tracks)
    }
    if not valid_captions:
        return None

    for lang in PREFERRED_LANGUAGES:
        if lang in valid_captions:
            return lang
        for key in valid_captions:
            if key.startswith(lang + "-"):
                return key
    return next(iter(valid_captions), None)


def _parse_json3_caption(content: str) -> list[dict]:
    data = json.loads(content)
    snippets = []
    for event in data.get("events", []):
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        text = unescape(text)
        if text:
            dur_ms = event.get("dDurationMs")
            snippets.append({
                "text": text,
                "start": (event.get("tStartMs") or 0) / 1000,
                "duration": (dur_ms / 1000) if dur_ms else None,
            })
    return snippets


# VTT 时间轴行：00:00:01.000 --> 00:00:03.000
_VTT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)


def _vtt_ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _parse_vtt_caption(content: str) -> list[dict]:
    if "window.WIZ_global_data" in content or "ytcfg.set" in content or "<html" in content.lower():
        raise RuntimeError("下载到的是 YouTube 网页而不是字幕文件")

    snippets: list[dict] = []
    cur_start: float | None = None
    cur_end: float | None = None
    buffer: list[str] = []

    def flush():
        nonlocal buffer, cur_start, cur_end
        if buffer and cur_start is not None:
            text = " ".join(buffer).strip()
            text = re.sub(r"<[^>]+>", "", text)
            text = unescape(text).strip()
            # 自动字幕常有滚动重复行，去掉与上一条完全相同的内容
            if text and (not snippets or snippets[-1]["text"] != text):
                duration = (cur_end - cur_start) if cur_end is not None else None
                snippets.append({"text": text, "start": cur_start, "duration": duration})
        buffer = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        time_match = _VTT_TIME_RE.search(line)
        if time_match:
            flush()
            cur_start = _vtt_ts_to_seconds(*time_match.group(1, 2, 3, 4))
            cur_end = _vtt_ts_to_seconds(*time_match.group(5, 6, 7, 8))
            continue
        if (
            not line
            or line == "WEBVTT"
            or line.startswith(("Kind:", "Language:", "NOTE"))
            or line.isdigit()
        ):
            continue
        buffer.append(line)
    flush()
    return snippets


def _fetch_with_ytdlp(video_id: str) -> list[dict]:
    """备用方案：yt-dlp 只提取字幕轨道，不下载视频。"""
    ydl_opts = _base_ydl_opts()
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    selected_lang = _select_caption_lang(info.get("subtitles") or {})
    if selected_lang is None:
        selected_lang = _select_caption_lang(info.get("automatic_captions") or {})
    if selected_lang is None:
        raise RuntimeError("yt-dlp 未找到该视频可用字幕轨道")

    with tempfile.TemporaryDirectory() as tmpdir:
        subtitle_opts = {
            **ydl_opts,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [selected_lang],
            "subtitlesformat": "json3/vtt/best",
            "outtmpl": str(Path(tmpdir) / "%(id)s.%(ext)s"),
        }
        with yt_dlp.YoutubeDL(subtitle_opts) as ydl:
            ydl.download([url])

        files = sorted(Path(tmpdir).glob(f"{video_id}.*"))
        caption_files = [
            p for p in files
            if p.suffix.lower() in {".json3", ".vtt"} and selected_lang in p.name
        ]
        if not caption_files:
            raise RuntimeError(f"yt-dlp 未能下载 {selected_lang} 字幕文件")

        caption_file = caption_files[0]
        content = caption_file.read_text(encoding="utf-8", errors="ignore")
        if caption_file.suffix.lower() == ".json3":
            snippets = _parse_json3_caption(content)
        else:
            snippets = _parse_vtt_caption(content)

    if not snippets:
        raise RuntimeError("yt-dlp 找到了字幕轨道，但字幕内容为空")
    return snippets


def fetch_transcript(video_id: str) -> list[dict]:
    """抓取字幕，返回 [{"text": ..., "start": ...}, ...]。

    优先 youtube-transcript-api，失败则回退 yt-dlp 字幕轨道。
    """
    try:
        return _fetch_with_transcript_api(video_id)
    except Exception as primary_error:
        try:
            return _fetch_with_ytdlp(video_id)
        except Exception as fallback_error:
            raise RuntimeError(
                "两种字幕抓取方式都失败："
                f"youtube-transcript-api={type(primary_error).__name__}: {primary_error}; "
                f"yt-dlp={type(fallback_error).__name__}: {fallback_error}"
            ) from fallback_error


# 网页可选画质（id, 展示名, yt-dlp format 选择器）
VIDEO_QUALITY_PRESETS: list[tuple[str, str, str]] = [
    ("best", "最佳画质（自动）", "bv*+ba/b"),
    ("1080", "1080p", "bv*[height<=1080]+ba/b[height<=1080]/bv*[height<=1080]+ba/b"),
    ("720", "720p", "bv*[height<=720]+ba/b[height<=720]/bv*[height<=720]+ba/b"),
    ("480", "480p", "bv*[height<=480]+ba/b[height<=480]/bv*[height<=480]+ba/b"),
    ("360", "360p", "bv*[height<=360]+ba/b[height<=360]/bv*[height<=360]+ba/b"),
    ("audio", "仅音频", "ba/b"),
]


def resolve_video_format_selector(quality: str | None = None) -> str:
    """将前端 quality id 解析为 yt-dlp format 选择器。"""
    q = (quality or "").strip().lower()
    for qid, _label, selector in VIDEO_QUALITY_PRESETS:
        if q == qid:
            return selector
    if q.isdigit():
        h = int(q)
        return (
            f"bv*[height<={h}]+ba/b[height<={h}]/"
            f"bv*[height<={h}]+ba/b"
        )
    env_default = os.environ.get("YOUTUBE_VIDEO_FORMAT", "").strip()
    return env_default or "bv*+ba/b"


def list_video_quality_options(url_or_id: str) -> dict[str, Any]:
    """列出可选画质（含该视频实际最高分辨率）。"""
    video_id = extract_video_id(url_or_id)
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = _base_ydl_opts()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = (info.get("title") or video_id).strip()
    heights: set[int] = set()
    for fmt in info.get("formats") or []:
        h = fmt.get("height")
        if h and fmt.get("vcodec") not in (None, "none"):
            heights.add(int(h))
    max_height = max(heights) if heights else None

    options: list[dict[str, Any]] = []
    for qid, label, selector in VIDEO_QUALITY_PRESETS:
        entry: dict[str, Any] = {
            "id": qid,
            "label": label,
            "format_selector": selector,
        }
        if qid.isdigit() and max_height is not None:
            entry["available"] = int(qid) <= max_height
        else:
            entry["available"] = True
        options.append(entry)

    return {
        "video_id": video_id,
        "title": title,
        "max_height": max_height,
        "options": options,
    }


def _normalize_progress_event(raw: dict[str, Any]) -> dict[str, Any]:
    """把 yt-dlp progress_hook 事件规范化为前端可消费的字段。"""
    status = raw.get("status") or ""
    if status == "downloading":
        total = raw.get("total_bytes") or raw.get("total_bytes_estimate")
        downloaded = int(raw.get("downloaded_bytes") or 0)
        percent: float | None = None
        if total:
            percent = min(100.0, downloaded / float(total) * 100.0)
        speed = raw.get("speed")
        eta = raw.get("eta")
        parts = []
        if percent is not None:
            parts.append(f"{percent:.1f}%")
        if speed:
            parts.append(f"{speed / 1024 / 1024:.1f} MB/s")
        if eta is not None and eta >= 0:
            parts.append(f"剩余约 {eta}s")
        return {
            "status": "downloading",
            "percent": percent,
            "downloaded_bytes": downloaded,
            "total_bytes": int(total) if total else None,
            "speed_bytes": float(speed) if speed else None,
            "eta_sec": int(eta) if eta is not None and eta >= 0 else None,
            "message": "下载中… " + " · ".join(parts) if parts else "下载中…",
        }
    if status == "finished":
        return {"status": "processing", "message": "合并/转码中…"}
    return {"status": status or "working", "message": status or "处理中…"}


def download_video_file(
    url_or_id: str,
    *,
    output_dir: Path,
    quality: str | None = None,
    format_selector: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, str]:
    """下载 YouTube 原视频到指定目录，返回 (文件路径, 建议文件名)。

    注意：这是可选功能，通常用于“用户明确想下载原视频”的场景。
    代理/cookies/Node.js/ejs 组件等配置复用 build_ytdlp_opts 的环境变量。
    """
    video_id = extract_video_id(url_or_id)
    url = f"https://www.youtube.com/watch?v={video_id}"

    output_dir.mkdir(parents=True, exist_ok=True)
    # 让 yt-dlp 自己决定扩展名；同一 video_id 下只会生成一个主视频文件
    outtmpl = str(output_dir / f"{video_id}.%(ext)s")
    selector = format_selector or resolve_video_format_selector(quality)

    ydl_opts = build_ytdlp_opts(
        skip_download=False,
        outtmpl=outtmpl,
        format_selector=selector,
        quiet=True,
    )
    # 降低资源占用：不写字幕、不写描述、不写缩略图
    ydl_opts.update(
        {
            "writesubtitles": False,
            "writeautomaticsub": False,
            "writethumbnail": False,
            "writeinfojson": False,
            "overwrites": True,
        }
    )
    if progress_callback is not None:
        def _hook(raw: dict[str, Any]) -> None:
            progress_callback(_normalize_progress_event(raw))

        ydl_opts["progress_hooks"] = [_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # yt-dlp 可能输出 m4a/webm 等不同容器；从 outtmpl 推断实际文件
    candidates = sorted(output_dir.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        # 避免极端情况下下载到了临时碎片却没合成
        time.sleep(0.2)
        candidates = sorted(output_dir.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("视频下载完成后未找到输出文件")

    file_path = candidates[0]
    title = (info.get("title") or video_id).strip()
    safe_title = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()[:120] or video_id
    download_name = f"{safe_title}.{file_path.suffix.lstrip('.')}"
    return file_path, download_name
