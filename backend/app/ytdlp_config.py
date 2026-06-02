"""Shared yt-dlp options aligned with a working YouTube downloader setup.

Modern YouTube often requires:
- browser cookies (cookiesfrombrowser)
- Node.js runtime (js_runtimes) for n/signature challenges
- remote_components ejs:github for challenge solver scripts
"""

from __future__ import annotations

import os
import shutil
from typing import Any


def build_ytdlp_opts(
    *,
    skip_download: bool = False,
    outtmpl: str | None = None,
    format_selector: str | None = None,
    quiet: bool = True,
) -> dict[str, Any]:
    """Build yt-dlp options with proxy, cookies, Node.js, and EJS components."""
    opts: dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noplaylist": True,
        "retries": int(os.environ.get("YOUTUBE_YTDLP_RETRIES", "10")),
        "fragment_retries": int(os.environ.get("YOUTUBE_FRAGMENT_RETRIES", "20")),
        "extractor_retries": int(os.environ.get("YOUTUBE_EXTRACTOR_RETRIES", "5")),
        "skip_unavailable_fragments": True,
        "socket_timeout": int(os.environ.get("YOUTUBE_SOCKET_TIMEOUT", "30")),
        "http_headers": {
            "User-Agent": os.environ.get(
                "YOUTUBE_USER_AGENT",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
                "Gecko/20100101 Firefox/128.0",
            ),
        },
    }

    remote = os.environ.get("YOUTUBE_REMOTE_COMPONENTS", "ejs:github").strip()
    if remote.lower() not in {"0", "false", "no", "off", ""}:
        opts["remote_components"] = [part.strip() for part in remote.split(",") if part.strip()]

    node_path = os.environ.get("YOUTUBE_NODE_PATH", "").strip() or shutil.which("node")
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}

    proxy = os.environ.get("YOUTUBE_PROXY", "").strip()
    if proxy:
        opts["proxy"] = proxy

    browser = os.environ.get("YOUTUBE_COOKIES_FROM_BROWSER", "").strip()
    cookie_file = os.environ.get("YOUTUBE_COOKIE_FILE", "").strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    if cookie_file:
        opts["cookiefile"] = cookie_file

    if skip_download:
        opts["skip_download"] = True
        opts["ignore_no_formats_error"] = True
        opts["format"] = format_selector or "best/bestaudio/bestvideo"
    elif format_selector:
        opts["format"] = format_selector
        opts["ignore_no_formats_error"] = True
        opts["hls_prefer_native"] = True

    if outtmpl:
        opts["outtmpl"] = outtmpl

    return opts


def youtube_ytdlp_setup_hint() -> str:
    """Human-readable checklist when yt-dlp cannot list formats."""
    browser = os.environ.get("YOUTUBE_COOKIES_FROM_BROWSER", "").strip() or "firefox"
    has_node = bool(os.environ.get("YOUTUBE_NODE_PATH", "").strip() or shutil.which("node"))
    lines = [
        "YouTube 未能解析到可下载格式，常见原因与处理：",
        f"1. 安装 Node.js 并确保在 PATH 中（当前{'已' if has_node else '未'}检测到）",
        f"2. 在 .env 设置 YOUTUBE_COOKIES_FROM_BROWSER={browser}，并在该浏览器登录 YouTube",
        "3. 保持 YOUTUBE_REMOTE_COMPONENTS=ejs:github（默认已启用）",
        "4. 更新 yt-dlp：pip install -U yt-dlp",
        "5. 确认 YOUTUBE_PROXY 可用",
    ]
    return "\n".join(lines)
