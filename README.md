# 垂直领域智能翻译 Agent

面向日语游戏/ACG 视频字幕的全栈 AI Agent 翻译产品。项目采用前后端分离架构：Node.js BFF 负责 UI 与 SSE 流式转发，Python FastAPI 后端负责 YouTube 字幕抓取、LangChain Agent 编排、RAG 术语检索和批量字幕翻译。

## 功能

- 单句领域翻译：输入含游戏黑话/专有名词的日语文本，输出地道中文。
- YouTube 字幕翻译：输入 YouTube 链接，自动抓取字幕并调用 Agent 翻译。
- SRT/TXT 批量翻译：上传字幕文件，返回翻译后的文本文件。
- RAG 术语检索：分领域词库 `backend/app/data/terms/*.json`，YouTube 翻译时按视频背景自动选 domain 加载。
- Agent 思考过程：默认只展示译文，可展开查看 ReAct 推理、工具调用和 RAG 返回。
- LLM-as-a-judge：自动评测 20 条术语翻译用例，输出通过率和平均分。

## 技术栈

- 前端/BFF：Node.js、Express、Server-Sent Events
- 后端：Python、FastAPI、LangChain、ChromaDB
- 字幕抓取：`youtube-transcript-api` + `yt-dlp`
- 大模型：兼容 OpenAI API 的模型服务（已适配 DeepSeek）
- 评测：LLM-as-a-judge

## 项目结构

```text
.
├── backend/
│   ├── app/
│   │   ├── agent/core.py          # LangChain Agent 核心
│   │   ├── data/terms/            # 分领域术语库（gaming / cooking / general …）
│   │   │   ├── gaming.json
│   │   │   ├── cooking.json
│   │   │   └── general.json
│   │   ├── tools/dictionary.py    # RAG 术语工具
│   │   ├── youtube_utils.py       # YouTube 字幕抓取
│   │   └── main.py                # FastAPI 接口
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── public/index.html          # UI
│   ├── server.js                  # Node.js BFF
│   ├── Dockerfile
│   └── package.json
├── eval/llm_judge.py              # LLM-as-a-judge 评测脚本
├── docs/architecture.md           # 架构图与 API 文档
├── docs/GITHUB.md                 # Git / GitHub 推送与交付说明
├── scripts/                       # 后端/前端分窗口启动脚本
├── start.bat / start.ps1          # Windows 一键启动
├── docker-compose.yml
└── .env.example
```

## 环境变量

复制 `.env.example` 为 `.env`，填写自己的密钥：

```env
OPENAI_API_KEY=sk-你的密钥
OPENAI_BASE_URL=https://api.deepseek.com/v1

# 国内访问 YouTube 通常需要代理
YOUTUBE_PROXY=socks5://127.0.0.1:10808

# 如果 YouTube 出现登录/机器人校验，读取本机已登录浏览器 cookies
YOUTUBE_COOKIES_FROM_BROWSER=edge
```

注意：`.env` 已被 `.gitignore` 忽略，不要提交真实密钥。

## 本地运行

### 一键启动（推荐）

需已安装 **Python 3.10+** 与 **Node.js**。

**Windows**（PowerShell 或双击）：

```powershell
.\start.ps1
# 或双击 start.bat（结束后窗口会 pause，便于查看报错；首次 pip 安装较慢）
```

停止服务：双击或在命令行运行 `start.bat stop`。

**Linux / macOS / Git Bash**：

```bash
chmod +x start.sh
./start.sh
```

脚本会自动：检查/生成 `.env`、创建 `backend\.venv`、安装依赖、在独立窗口（Windows）或本终端后台（Unix）启动后端 `8000` 与前端 `3000`，并打开 http://localhost:3000 。

常用参数：

| 参数 | 说明 |
|------|------|
| `-SkipInstall` / `--skip-install` | 跳过 pip、npm 安装（二次启动更快） |
| `-NoBrowser` / `--no-browser` | 不自动打开浏览器 |
| `-Stop` / `--stop` | 停止占用 8000、3000 端口的进程 |

### 手动分步启动

### 1. 启动后端

```powershell
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

健康检查：

```text
http://127.0.0.1:8000/
```

正常返回：

```json
{"message":"Hello from Python Backend"}
```

### 2. 启动前端/BFF

另开一个终端：

```powershell
cd frontend
npm install
npm run dev
```

访问：

```text
http://localhost:3000
```

## Docker 运行

```powershell
docker compose up --build
```

如果拉取基础镜像失败，通常是 Docker Desktop 的代理或镜像源问题。可先使用本地运行方式完成开发验证，最终交付前再处理 Docker 网络。

## 验证用例

### 单句翻译

输入：

```text
このゲームのデバフがエグい
```

预期：

- Agent 调用术语工具查询 `デバフ` / `エグい`。
- 译文能体现“负面状态/减益”和“离谱、强得夸张”的含义。

### YouTube 链接翻译

输入有字幕的 YouTube 链接。若出现：

- `RequestBlocked`：YouTube 拦截轻量 API，会自动回退到 `yt-dlp`。
- `Sign in to confirm`：确认浏览器已登录 YouTube，并设置 `YOUTUBE_COOKIES_FROM_BROWSER=edge/chrome/firefox`。
- `未找到字幕轨道`：该视频可能没有公开字幕，换一个带 CC 字幕的视频。

### 文件翻译

上传 `.txt` 或 `.srt` 文件，预期下载 `translated_原文件名`。

## 量化评测

运行：

```powershell
python eval/llm_judge.py
```

快速试跑前 2 条：

```powershell
python eval/llm_judge.py --limit 2
```

输出：

- 每条用例的译文、通过/失败、0-5 分、理由。
- 总通过率和平均分。
- 结果文件：`eval/llm_judge_results.json`。

建议交付标准：

- `pass_rate >= 80%`
- `avg_score >= 4.0 / 5`

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 健康检查 |
| `GET` | `/stream_translate?text=...` | 单句流式翻译 |
| `GET` | `/stream_translate_youtube?url=...` | YouTube 字幕抓取 + 流式翻译 |
| `POST` | `/api/translate-srt` | SRT/TXT 文件批量翻译 |

完整说明见 `docs/architecture.md`。

## 课程评分对应

| 评分项 | 项目对应实现 |
| --- | --- |
| 复杂 Agent 架构 30% | LangChain tool-calling Agent + RAG 术语工具 + 可视化思考过程 |
| AI 结对编程 20% | 使用 Cursor/Copilot 迭代搭建、调试、测试与文档化 |
| 极致分离 20% | Node.js BFF 与 Python AI 编排层分离，REST/SSE 通信 |
| 量化评判 15% | `eval/llm_judge.py` 自动化评测 |
| 工程规范 15% | Dockerfile、docker-compose、README、架构图、API 文档 |

## Git / GitHub 交付

本地尚未初始化 Git 时，按 **[docs/GITHUB.md](docs/GITHUB.md)** 完成 `git init`、首次提交、关联 `origin` 与 `git push`。切勿将 `.env`、虚拟环境或 `chroma_data/` 推送到远程。

## 后续优化

- 增加 `max_iterations` / `handle_parsing_errors`，进一步限制 Agent 死循环风险。
- 继续扩充 `backend/app/data/terms/{domain}.json`；新领域新建同名 JSON 并在 `term_domains.py` 补充关键词。
- 增加“翻译 Agent + 校对 Agent”双 Agent 协作。
- YouTube 长视频可改为分段翻译并合并结果。
