"""Optional YouTube audio transcription via faster-whisper.

This module is intentionally lazy: faster-whisper is imported only when the
user explicitly enables Whisper mode on the page. The base application can
start without installing the optional dependencies.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import yt_dlp

from app.ytdlp_config import build_ytdlp_opts, youtube_ytdlp_setup_hint

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "ja").strip() or None
WHISPER_BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))
WHISPER_BEST_OF = int(os.environ.get("WHISPER_BEST_OF", "5"))
WHISPER_VAD_FILTER = os.environ.get("WHISPER_VAD_FILTER", "1").lower() not in {"0", "false", "no"}
WHISPER_MAX_DURATION_SECONDS = int(os.environ.get("WHISPER_MAX_DURATION_SECONDS", "1800"))
# 引导听写风格，减轻游戏黑话被听错（可整段覆盖）
_DEFAULT_JA_PROMPT = (
    "以下は日本語のゲーム実況・解説です。"
    "デバフ、バフ、ガチャ、エグい、凸、周回、引く、当たり、原神、攻略 などの用語が出ます。"
)
WHISPER_INITIAL_PROMPT = os.environ.get("WHISPER_INITIAL_PROMPT", _DEFAULT_JA_PROMPT).strip() or None

# 与「油管视频下载」脚本一致：优先 ba/best
_DEFAULT_AUDIO_FORMAT = os.environ.get("WHISPER_YTDLP_FORMAT", "ba/best").strip()

# faster-whisper 在 HuggingFace 上的模型仓库名
_HF_MODEL_REPOS: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large": "Systran/faster-whisper-large-v3",
}

_REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")

_model = None
_model_key: tuple[str, str, str, str | None] | None = None
_model_lock = threading.Lock()


def _validate_local_model_dir(path: Path) -> list[str]:
    """Return list of missing required filenames (empty = OK)."""
    return [name for name in _REQUIRED_MODEL_FILES if not (path / name).is_file()]


def check_whisper_setup() -> dict:
    """Inspect Whisper optional setup for UI / diagnostics (does not load weights)."""
    details: list[str] = []
    try:
        import faster_whisper  # type: ignore[import-not-found]  # noqa: F401

        details.append("faster-whisper 已安装")
    except ImportError:
        return {
            "ready": False,
            "mode": "not_installed",
            "message": "未安装 faster-whisper 可选依赖",
            "model_path": "",
            "missing_files": [],
            "details": [
                "在 backend 目录执行：.venv\\Scripts\\pip install -r requirements-whisper.txt",
                "详见 docs/WHISPER_SETUP.md",
            ],
        }

    local_path = os.environ.get("WHISPER_MODEL_PATH", "").strip()
    if local_path:
        path = Path(local_path)
        if not path.is_dir():
            return {
                "ready": False,
                "mode": "local_invalid",
                "message": "WHISPER_MODEL_PATH 指向的目录不存在",
                "model_path": local_path,
                "missing_files": list(_REQUIRED_MODEL_FILES),
                "details": details + [f"请检查路径：{local_path}"],
            }
        missing = _validate_local_model_dir(path)
        if missing:
            return {
                "ready": False,
                "mode": "local_incomplete",
                "message": "本地模型目录缺少必要文件",
                "model_path": str(path),
                "missing_files": missing,
                "details": details + [
                    "需下载完整文件夹（不仅是 model.bin）",
                    "镜像：https://hf-mirror.com/Systran/faster-whisper-small",
                ],
            }
        return {
            "ready": True,
            "mode": "local",
            "message": "本地 Whisper 模型已就绪",
            "model_path": str(path),
            "missing_files": [],
            "details": details + [f"使用目录：{path}"],
        }

    # 未配置本地路径：首次将尝试从 HuggingFace 在线下载
    repo = _HF_MODEL_REPOS.get(WHISPER_MODEL.lower(), f"Systran/faster-whisper-{WHISPER_MODEL}")
    hf_endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    online_details = details + [
        f"将在线下载模型：{WHISPER_MODEL}（{repo}）",
        "国内建议在 .env 设置 HF_ENDPOINT=https://hf-mirror.com",
        "或下载完整模型后设置 WHISPER_MODEL_PATH=本地目录",
    ]
    if hf_endpoint:
        online_details.append(f"HF_ENDPOINT={hf_endpoint}")
    if os.environ.get("HF_PROXY", "").strip():
        online_details.append(f"HF_PROXY={os.environ.get('HF_PROXY', '').strip()}")

    return {
        "ready": False,
        "mode": "online",
        "message": "未配置本地模型，首次识别需联网下载（易超时）",
        "model_path": "",
        "missing_files": [],
        "details": online_details,
    }


def _configure_hf_hub() -> None:
    """Configure HuggingFace Hub before faster-whisper downloads weights."""
    endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint.rstrip("/")

    timeout = os.environ.get("HF_HUB_DOWNLOAD_TIMEOUT", "").strip()
    if timeout:
        os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = timeout
    else:
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")

    proxy = (
        os.environ.get("HF_PROXY", "").strip()
        or os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("https_proxy", "").strip()
    )
    if not proxy:
        yt_proxy = os.environ.get("YOUTUBE_PROXY", "").strip()
        if yt_proxy.startswith(("http://", "https://")):
            proxy = yt_proxy

    if proxy:
        os.environ.setdefault("HTTPS_PROXY", proxy)
        os.environ.setdefault("HTTP_PROXY", proxy)
        os.environ.setdefault("ALL_PROXY", proxy)


def _hf_model_download_hint() -> str:
    model = WHISPER_MODEL.lower()
    repo = _HF_MODEL_REPOS.get(model, f"Systran/faster-whisper-{model}")
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    mirror_note = ""
    if "hf-mirror" not in endpoint:
        mirror_note = (
            "\n国内建议在 .env 增加：HF_ENDPOINT=https://hf-mirror.com"
            f"\n镜像页：https://hf-mirror.com/{repo}"
        )
    else:
        mirror_note = f"\n镜像页：{endpoint}/{repo}"

    return (
        "无法从 HuggingFace 下载 Whisper 模型（连接超时或被墙）。\n"
        "请任选一种方式：\n"
        "1. .env 设置 HF_ENDPOINT=https://hf-mirror.com 后重启后端\n"
        "2. .env 设置 HF_PROXY=http://127.0.0.1:你的HTTP代理端口（Clash 常用 7890）\n"
        "3. 浏览器下载模型文件夹后，设置 WHISPER_MODEL_PATH=本地目录绝对路径\n"
        f"   官方仓库：{repo}"
        f"{mirror_note}"
    )


def _resolve_model_source() -> tuple[str, str | None]:
    """Return (model_id_or_path, download_root)."""
    local_path = os.environ.get("WHISPER_MODEL_PATH", "").strip()
    if local_path:
        path = Path(local_path)
        if not path.is_dir():
            raise RuntimeError(f"WHISPER_MODEL_PATH 不是有效目录：{local_path}")
        missing = _validate_local_model_dir(path)
        if missing:
            raise RuntimeError(
                f"WHISPER_MODEL_PATH 目录缺少文件：{', '.join(missing)}。"
                "请下载完整模型文件夹，见 docs/WHISPER_SETUP.md"
            )
        return str(path), None

    download_root = os.environ.get("WHISPER_MODEL_DIR", "").strip() or None
    return WHISPER_MODEL, download_root


def _load_model():
    global _model, _model_key

    model_source, download_root = _resolve_model_source()
    key = (model_source, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, download_root)
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model

        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "尚未安装 faster-whisper 可选依赖。请在 backend 目录运行："
                " .venv\\Scripts\\pip install -r requirements-whisper.txt"
            ) from exc

        _configure_hf_hub()
        try:
            _model = WhisperModel(
                model_source,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
                download_root=download_root,
            )
        except Exception as exc:
            err_name = type(exc).__name__
            err_text = str(exc)
            if (
                "LocalEntryNotFound" in err_name
                or "ConnectTimeout" in err_text
                or "HuggingFace" in err_text
                or "Hub" in err_text
            ):
                raise RuntimeError(_hf_model_download_hint()) from exc
            raise

        _model_key = key
        return _model


def _check_duration(url: str) -> None:
    with yt_dlp.YoutubeDL(build_ytdlp_opts(skip_download=True)) as ydl:
        info = ydl.extract_info(url, download=False)

    duration = info.get("duration")
    if duration and duration > WHISPER_MAX_DURATION_SECONDS:
        minutes = WHISPER_MAX_DURATION_SECONDS // 60
        raise RuntimeError(
            f"视频时长超过 Whisper 上限 {minutes} 分钟，请先调大 WHISPER_MAX_DURATION_SECONDS"
        )


def _list_audio_candidates(tmpdir: str, video_id: str) -> list[Path]:
    skip_suffixes = {".json", ".part", ".ytdl", ".description", ".info"}
    return [
        path
        for path in Path(tmpdir).glob(f"{video_id}.*")
        if path.is_file() and path.suffix.lower() not in skip_suffixes
    ]


def _make_download_hook(progress_cb):
    state = {"last": 0.0}

    def hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - state["last"] < 1.0:
                return
            state["last"] = now
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            progress_cb(f"【Whisper】下载音频 {pct} {speed}")
        elif status == "finished":
            progress_cb("【Whisper】音频下载完成，准备识别...")

    return hook


def _download_audio(video_id: str, tmpdir: str, progress_cb=None) -> Path:
    url = f"https://www.youtube.com/watch?v={video_id}"
    if progress_cb:
        progress_cb("【Whisper】正在读取视频信息...")
    _check_duration(url)

    outtmpl = str(Path(tmpdir) / "%(id)s.%(ext)s")
    format_attempts = [
        _DEFAULT_AUDIO_FORMAT,
        "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/ba/best",
        "best[acodec!=none]/best",
        "best/worst",
    ]
    seen: set[str] = set()
    format_attempts = [f for f in format_attempts if f and not (f in seen or seen.add(f))]

    hooks = [_make_download_hook(progress_cb)] if progress_cb else None

    last_error: Exception | None = None
    for fmt in format_attempts:
        try:
            opts = build_ytdlp_opts(outtmpl=outtmpl, format_selector=fmt)
            if hooks:
                opts["progress_hooks"] = hooks
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            candidates = _list_audio_candidates(tmpdir, video_id)
            if candidates:
                return max(candidates, key=lambda path: path.stat().st_size)
        except Exception as exc:
            last_error = exc
            continue

    detail = f"yt-dlp 下载音频失败：{last_error}" if last_error else "yt-dlp 未能下载可转写的音频文件"
    raise RuntimeError(f"{detail}\n\n{youtube_ytdlp_setup_hint()}") from last_error


def transcribe_youtube_audio(video_id: str, progress_cb=None) -> list[dict]:
    """Download YouTube audio and return subtitle-like snippets.

    progress_cb(msg) 可选：用于把下载/识别进度回传给上层（线程内调用）。
    """
    def report(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    report(f"【Whisper】正在加载本地模型（{WHISPER_MODEL}）...")
    model = _load_model()
    transcribe_kwargs: dict = {
        "language": WHISPER_LANGUAGE,
        "beam_size": WHISPER_BEAM_SIZE,
        "best_of": WHISPER_BEST_OF,
        "vad_filter": WHISPER_VAD_FILTER,
        "condition_on_previous_text": True,
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.0,
        "no_speech_threshold": 0.6,
    }
    if WHISPER_INITIAL_PROMPT:
        transcribe_kwargs["initial_prompt"] = WHISPER_INITIAL_PROMPT

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = _download_audio(video_id, tmpdir, progress_cb)
        report(
            "【Whisper】开始语音识别（CPU 较慢；质量通常低于 YouTube 官方字幕，"
            "有 CC 时请取消「强制 Whisper」）..."
        )
        segments, info = model.transcribe(str(audio_path), **transcribe_kwargs)

        total = float(getattr(info, "duration", 0.0) or 0.0)
        snippets: list[dict] = []
        last_report = 0.0
        for segment in segments:
            end = float(segment.end or 0.0)
            if end - last_report >= 20.0:
                last_report = end
                if total > 0:
                    pct = min(99, int(end * 100 / total))
                    report(f"【Whisper】识别中 {int(end)}s/{int(total)}s（约 {pct}%），已 {len(snippets)} 段")
                else:
                    report(f"【Whisper】识别中，已 {len(snippets)} 段")

            text = (segment.text or "").strip()
            if not text:
                continue
            start = float(segment.start or 0.0)
            end = float(segment.end or start + 2.0)
            snippets.append({
                "text": text,
                "start": start,
                "duration": max(0.1, end - start),
            })

        report(f"【Whisper】识别完成，共 {len(snippets)} 段")

    if not snippets:
        raise RuntimeError("Whisper 识别完成，但未产生有效文本")
    return snippets
