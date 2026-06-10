# 垂直领域智能翻译 Agent

面向日语游戏 / ACG / VTuber 视频字幕的全栈 AI Agent 翻译产品。采用前后端分离：Node.js BFF 负责 UI 与 SSE 流式转发，Python FastAPI 负责 YouTube 字幕抓取、LangChain Agent 编排、RAG 术语检索与批量字幕翻译。

课程交付说明见 [docs/REPORT.md](docs/REPORT.md)。

## 功能

- **单句领域翻译**
- **YouTube 字幕翻译下载（主要）**：输入链接，自动抓取 YouTube 官方/CC 字幕，按视频背景推断术语 domain 后分块翻译。页面流式生成并下载 `{videoId}_zh.srt`，保留原时间轴；超长条可按标点自动拆条并铺平到下一条开始前的空隙（见 `SRT_SPLIT_*`）。
- **上传 SRT/TXT 批量翻译**：上传字幕文件，返回 `translated_原文件名`。
- **RAG 术语检索**：分领域词库 `backend/app/data/terms/*.json`（`gaming` / `cooking` / `general` / `vtuber`）。
- **Wiki 在线检索**：Agent 可调用 `lookup_wiki`（萌娘百科/维基）；YouTube 翻译时按作品 IP 自动扩展查询。
- **术语预扫描**：批量/YouTube 翻译前从字幕与视频背景匹配词库，注入 glossary prompt（思考区可见）。
- **可选校对 Agent**：`ENABLE_PROOFREAD_AGENT=1` 时串行二次润色（单句、SRT 上传、YouTube SRT 下载；预览路径不受影响）。
- **可选原视频下载**：`ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1` 后页面可选画质下载（默认关闭）。

## 技术栈

- 前端 / BFF：Node.js、Express、Server-Sent Events
- 后端：Python 3.10+、FastAPI、LangChain、ChromaDB
- 字幕：`youtube-transcript-api`、`yt-dlp`
- 大模型：兼容 OpenAI API（已适配 DeepSeek）

## 项目结构

```text
.
├── backend/
│   ├── app/
│   │   ├── agent/          # 翻译 Agent + 可选校对 Agent
│   │   ├── tools/          # RAG 术语 + Wiki 检索
│   │   ├── data/terms/     # 分领域术语 JSON
│   │   ├── main.py         # FastAPI 编排
│   │   └── ...             # 字幕、domain 推断、术语预扫描等
│   └── requirements.txt
├── frontend/
│   ├── public/index.html
│   └── server.js           # BFF :3000
├── docs/
│   ├── architecture.md     # 架构与完整 API
│   └── REPORT.md           # 课程交付报告
├── scripts/
├── start.bat / start.ps1 / start.sh
└── .env.example
```

## 快速开始

**环境**：Python 3.10+、Node.js；复制 `.env.example` 为 `.env` 并填写 `OPENAI_API_KEY`（及国内 YouTube 所需的 `YOUTUBE_PROXY`、`YOUTUBE_COOKIES_FROM_BROWSER`）。

**Windows**（项目根目录）：

```powershell
.\start.ps1
# 或双击 start.bat；停止：start.bat stop
```

**Linux / macOS / Git Bash**：

```bash
chmod +x start.sh && ./start.sh
```

脚本会检查/生成 `.env`、创建 `backend\.venv`、安装依赖，启动后端 **8000** 与 BFF **3000**，并打开 [http://localhost:3000](http://localhost:3000) 。


| 参数                                | 说明                 |
| --------------------------------- | ------------------ |
| `-SkipInstall` / `--skip-install` | 跳过 pip、npm（二次启动更快） |
| `-NoBrowser` / `--no-browser`     | 不自动打开浏览器           |
| `-Stop` / `--stop`                | 释放 8000、3000 端口    |


**健康检查**：[http://127.0.0.1:8000/](http://127.0.0.1:8000/) 应返回 `{"message":"Hello from Python Backend"}`。

## 环境变量

根目录 `.env` 主要项（完整列表见 `.env.example`）：

```env
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=https://api.deepseek.com/v1

# 国内访问 YouTube 通常需要
YOUTUBE_PROXY=socks5://127.0.0.1:10808
YOUTUBE_COOKIES_FROM_BROWSER=firefox

# Agent 防死循环（可选）
AGENT_MAX_ITERATIONS=5
AGENT_MAX_EXECUTION_TIME=60

# Wiki 在线检索（默认开启）
ENABLE_WIKI_LOOKUP=1

# 翻译后启用校对 Agent（默认关闭；LLM 调用约翻倍）
ENABLE_PROOFREAD_AGENT=0

# 原视频下载（默认关闭）
ENABLE_YOUTUBE_VIDEO_DOWNLOAD=0
```

`.env` 已加入 `.gitignore`，勿提交真实密钥。

## 本地运行（手动）

需先在一键脚本中生成 `backend\.venv`，或自行 `python -m venv backend\.venv` 并 `pip install -r backend/requirements.txt`。

**终端 1 — 后端**（在 `backend` 目录）：

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
# Linux/macOS: .venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

**终端 2 — 前端**（在 `frontend` 目录）：

```powershell
npm install
npm run dev
```

访问 [http://localhost:3000](http://localhost:3000) 。

## 页面功能说明


| 入口           | 说明                           |
| ------------ | ---------------------------- |
| 开始翻译         | 单句 SSE 流式翻译                  |
| 页面预览翻译       | YouTube 链接，流式显示译文            |
| 下载翻译字幕 (SRT) | 全量字幕翻译后下载 `{videoId}_zh.srt` |
| 上传并翻译文件      | `.srt` / `.txt` 批量翻译         |
| 下载原视频        | 需 `ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1`；可选画质，流式进度条 |


默认只显示译文；勾选「显示 Agent 思考过程」可查看推理与工具调用。

## 验证用例

**单句** — 输入：

```text
このゲームのデバフがエグい
```

预期：Agent 查询 `デバフ`、`エグい`，译文体现「减益/负面状态」与「强得离谱」等含义。

**YouTube** — 使用有字幕的链接。常见问题：

- `RequestBlocked`：会自动回退 `yt-dlp`，检查代理与 cookies。
- `Sign in to confirm`：在浏览器登录 YouTube，设置 `YOUTUBE_COOKIES_FROM_BROWSER`。
- `未找到字幕轨道`：确认视频有官方/CC 字幕，或换有字幕的链接。

**文件** — 上传 `.srt` / `.txt`，下载 `translated_*` 文件。

**校对 Agent（可选）** — 在 `.env` 设 `ENABLE_PROOFREAD_AGENT=1` 后重启后端：

- 单句翻译：思考区出现「校对 Agent」阶段，译文区只显示终稿。
- SRT/TXT 上传、YouTube SRT 下载：每批翻译后多一次校对调用；思考区可见「校对 Agent」提示。
- YouTube 页面预览翻译不受影响（仍为单 Agent）。

**原视频下载（可选）** — 设 `ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1` 后重启后端，YouTube 区出现画质选择与「下载原视频」按钮。

## API 摘要

浏览器经 BFF（`:3000`）访问；直连 Python 后端为 `:8000`。


| 方法     | BFF 路径                          | 后端路径                            | 说明             |
| ------ | ------------------------------- | ------------------------------- | -------------- |
| `GET`  | `/api/translate`                | `/stream_translate`             | 单句 SSE         |
| `GET`  | `/api/translate-youtube`        | `/stream_translate_youtube`     | YouTube 预览 SSE |
| `GET`  | `/api/translate-youtube-srt`    | `/stream_translate_youtube_srt` | 流式翻译并下发 SRT    |
| `POST` | `/api/translate-srt`            | `/api/translate-srt`            | 上传 SRT/TXT     |
| `GET`  | `/api/youtube-video-formats`    | `/youtube_video_formats`        | 画质列表（需视频下载开关） |
| `GET`  | `/api/stream-download-youtube-video` | `/stream_download_youtube_video` | 流式下载原视频 |
| `GET`  | —                               | `/download_translated_srt`      | 直连下载 SRT（无进度流） |
| `GET`  | —                               | `/`                             | 健康检查           |


完整 API 与管理端点见 [docs/architecture.md](docs/architecture.md)。
