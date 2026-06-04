# 垂直领域智能翻译 Agent

面向日语游戏 / ACG / VTuber 视频字幕的全栈 AI Agent 翻译产品。采用前后端分离：Node.js BFF 负责 UI 与 SSE 流式转发，Python FastAPI 负责 YouTube 字幕抓取、LangChain Agent 编排、RAG 术语检索与批量字幕翻译。

## 功能

- **单句领域翻译**：输入含游戏黑话、专有名词的日语文本，流式输出中文；可展开 ReAct 推理与 RAG 检索过程。
- **YouTube 字幕翻译**：输入链接，自动抓取字幕（可选 Whisper 听写降级），按视频背景推断术语 domain 后分块翻译。
- **翻译字幕下载**：页面流式生成并下载 `{videoId}_zh.srt`，保留原时间轴。
- **SRT/TXT 批量翻译**：上传字幕文件，返回 `translated_原文件名`。
- **RAG 术语检索**：分领域词库 `backend/app/data/terms/*.json`（`gaming` / `cooking` / `general` / `vtuber`）。
- **LLM-as-a-judge**：`eval/llm_judge.py` 对 20 条术语用例自动评分。

## 技术栈

- 前端 / BFF：Node.js、Express、Server-Sent Events
- 后端：Python 3.10+、FastAPI、LangChain、ChromaDB
- 字幕：`youtube-transcript-api`、`yt-dlp`；可选 `faster-whisper`
- 大模型：兼容 OpenAI API（已适配 DeepSeek）
- 评测：LLM-as-a-judge

## 项目结构

```text
.
├── backend/
│   ├── app/
│   │   ├── agent/core.py          # LangChain Agent
│   │   ├── data/terms/            # 分领域术语 JSON
│   │   ├── tools/dictionary.py    # RAG 术语工具
│   │   ├── term_domains.py        # YouTube 背景 → domain 推断
│   │   ├── youtube_utils.py       # 字幕抓取
│   │   ├── whisper_utils.py       # 可选 Whisper
│   │   └── main.py                # FastAPI
│   ├── requirements.txt
│   └── requirements-whisper.txt   # 可选语音识别
├── frontend/
│   ├── public/index.html
│   └── server.js                  # BFF :3000
├── eval/llm_judge.py
├── docs/
│   ├── architecture.md            # 架构与 API
│   ├── WHISPER_SETUP.md           # Whisper 本地模型
│   └── GITHUB.md                  # Git 推送说明
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

脚本会检查/生成 `.env`、创建 `backend\.venv`、安装依赖，启动后端 **8000** 与 BFF **3000**，并打开 http://localhost:3000 。

| 参数 | 说明 |
|------|------|
| `-SkipInstall` / `--skip-install` | 跳过 pip、npm（二次启动更快） |
| `-NoBrowser` / `--no-browser` | 不自动打开浏览器 |
| `-Stop` / `--stop` | 释放 8000、3000 端口 |

**健康检查**：http://127.0.0.1:8000/ 应返回 `{"message":"Hello from Python Backend"}`。

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

访问 http://localhost:3000 。

## 页面功能说明

| 入口 | 说明 |
|------|------|
| 开始翻译 | 单句 SSE 流式翻译 |
| 页面预览翻译 | YouTube 链接，流式显示译文 |
| 下载翻译字幕 (SRT) | 全量字幕翻译后下载 `{videoId}_zh.srt` |
| 上传并翻译文件 | `.srt` / `.txt` 批量翻译 |

默认只显示译文；勾选「显示 Agent 思考过程」可查看推理与工具调用。

### Whisper（可选）

默认不安装 Whisper，优先使用 YouTube 官方 / CC 字幕（**推荐**）。需听写时：

```powershell
cd backend
.\.venv\Scripts\pip install -r requirements-whisper.txt
```

| 页面选项 | 行为 |
|----------|------|
| 都不勾选 | 仅 YouTube 字幕（`youtube-transcript-api` → `yt-dlp`） |
| 无字幕时用 Whisper | 有 CC 仍用 YouTube；无字幕才 `faster-whisper` |
| 强制仅用 Whisper | 忽略 YouTube 字幕（不推荐） |

本地模型：在 `.env` 设置 `WHISPER_MODEL_PATH` 后重启后端。配置步骤见 [docs/WHISPER_SETUP.md](docs/WHISPER_SETUP.md)，或运行 `.\scripts\setup-whisper-model.ps1`。页面会显示 Whisper 是否就绪。

Whisper 相关变量：`WHISPER_MODEL`、`WHISPER_DEVICE`、`WHISPER_LANGUAGE`、`HF_ENDPOINT`、`HF_PROXY` 等，见 `.env.example`。

## 验证用例

**单句** — 输入：

```text
このゲームのデバフがエグい
```

预期：Agent 查询 `デバフ`、`エグい`，译文体现「减益/负面状态」与「强得离谱」等含义。

**YouTube** — 使用有字幕的链接。常见问题：

- `RequestBlocked`：会自动回退 `yt-dlp`，检查代理与 cookies。
- `Sign in to confirm`：在浏览器登录 YouTube，设置 `YOUTUBE_COOKIES_FROM_BROWSER`。
- `未找到字幕轨道`：可安装 Whisper 并勾选「无字幕时用」。

**文件** — 上传 `.srt` / `.txt`，下载 `translated_*` 文件。

## 量化评测

```powershell
# 项目根目录，建议使用 backend 虚拟环境
backend\.venv\Scripts\python eval\llm_judge.py
backend\.venv\Scripts\python eval\llm_judge.py --limit 2
```

输出：逐条译文、通过/失败、0–5 分、理由；汇总通过率与平均分；结果写入 `eval/llm_judge_results.json`。

建议交付标准：`pass_rate >= 80%`，`avg_score >= 4.0 / 5`。

## API 摘要

浏览器经 BFF（`:3000`）访问；直连 Python 后端为 `:8000`。

| 方法 | BFF 路径 | 后端路径 | 说明 |
|------|----------|----------|------|
| `GET` | `/api/translate` | `/stream_translate` | 单句 SSE |
| `GET` | `/api/translate-youtube` | `/stream_translate_youtube` | YouTube 预览 SSE；支持 `use_whisper`、`whisper_force` |
| `GET` | `/api/translate-youtube-srt` | `/stream_translate_youtube_srt` | 流式翻译并下发 SRT |
| `GET` | `/api/whisper-status` | `/whisper_status` | Whisper 配置状态 |
| `POST` | `/api/translate-srt` | `/api/translate-srt` | 上传 SRT/TXT |
| `GET` | — | `/download_translated_srt` | 直连下载 SRT（无进度流） |
| `GET` | — | `/` | 健康检查 |

完整说明见 [docs/architecture.md](docs/architecture.md)。

## 课程评分对应

| 评分项 | 项目对应实现 |
| --- | --- |
| 复杂 Agent 架构 30% | LangChain tool-calling Agent + RAG + 可展示思考过程 |
| AI 结对编程 20% | Cursor / Copilot 迭代开发、调试与文档 |
| 极致分离 20% | Node.js BFF 与 Python AI 层分离，REST / SSE |
| 量化评判 15% | `eval/llm_judge.py` |
| 工程规范 15% | 一键启动脚本、README、架构与 API 文档 |

## Git / GitHub 交付

尚未初始化 Git 时，见 [docs/GITHUB.md](docs/GITHUB.md)。勿推送 `.env`、`backend/.venv`、`node_modules`、`chroma_data/` 等。

## 后续优化

- 继续扩充 `backend/app/data/terms/{domain}.json`；新领域在 `term_domains.py` 补充关键词。
- 翻译 Agent + 校对 Agent 双阶段协作。
- YouTube 超长视频的分段合并与进度优化。
