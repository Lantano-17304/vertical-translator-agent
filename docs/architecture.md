# 垂直领域智能翻译 Agent 架构说明

本项目面向“日语游戏/ACG 视频字幕翻译”场景，目标不是做通用机翻，而是通过 Agent + RAG 术语库解决游戏黑话、缩写、版本环境词等垂直领域表达的翻译问题。

## 架构图

```mermaid
flowchart LR
    U[用户浏览器] -->|文本 / YouTube 链接 / SRT 文件| FE[前端页面<br/>frontend/public/index.html]
    FE -->|SSE / multipart| BFF[Node.js BFF<br/>Express server.js]
    FE -->|可选视频下载| BFF

    BFF -->|REST / SSE| API[Python FastAPI<br/>backend/app/main.py]

    API -->|解析 YouTube 链接| YT[YouTube 字幕抓取<br/>youtube-transcript-api + yt-dlp]
    API -->|预扫描| GLOSS[term_glossary<br/>Push 式术语表]
    API -->|调用 Agent| AGENT[LangChain Tool-calling Agent<br/>backend/app/agent/core.py]
    AGENT -->|Pull 检索| RAG[ChromaDB 术语库<br/>dictionary.py + terms/*.json]
    AGENT -->|Pull 检索| WIKI[lookup_wiki<br/>wiki_franchise_scope]
    AGENT -->|ChatOpenAI 兼容接口| LLM[DeepSeek / OpenAI]
    API -.->|ENABLE_PROOFREAD_AGENT=1| PROOF[校对 Agent<br/>proofread.py]
    PROOF --> LLM

    API -->|SSE: thought/token/progress/srt| BFF
    BFF -->|SSE 转发| FE
```

## 模块职责

| 模块 | 技术 | 职责 |
| --- | --- | --- |
| 前端 UI | HTML/CSS/JS | 提供单句翻译、YouTube 链接翻译、SRT/TXT 文件翻译入口；可选原视频下载（需后端开关）；默认只显示译文，可展开 Agent 思考过程。 |
| BFF 层 | Node.js + Express | 作为前端与 Python 后端之间的边界层，负责 SSE 流式转发、文件上传代理与视频下载代理。 |
| 后端编排层 | FastAPI | 暴露翻译 API，处理 YouTube 字幕抓取、SRT/TXT 解析、术语预扫描、Agent 调用与 SSE 输出。 |
| Agent 核心 | LangChain | 使用 tool-calling Agent 实现“分析输入 -> 调用术语/Wiki 工具 -> 结合检索结果翻译”的流程。 |
| 术语工具 | ChromaDB + JSON | 从 `backend/app/data/terms/*.json` 分 domain 加载；YouTube 翻译时按视频背景自动筛选 domain 检索。 |
| Wiki 检索 | `wiki_lookup.py` + `wiki_franchise.py` | Agent 按需查询萌娘百科/维基；YouTube 翻译时按作品 IP 自动扩展查询变体。 |
| 术语预扫描 | `term_glossary.py` | 翻译前从字幕与视频背景匹配词库，构建 glossary prompt 注入（Push），减少 Agent 漏查。 |
| 校对 Agent | `proofread.py` | 可选第二段无工具 LLM，在初译与补译之后串行润色专名与表述。 |

## 数据流

### 1. 单句翻译

1. 用户在页面输入一句日语游戏文本。
2. 前端发起 `GET /api/translate?text=...`。
3. Node BFF 转发到 Python 后端 `GET /stream_translate?text=...`。
4. 后端 `build_session_glossary` 预扫描术语，思考区展示命中词条。
5. 后端调用 LangChain 翻译 Agent；Agent 按需调用 `search_term_dict` / `lookup_wiki`。
6. 若 `ENABLE_PROOFREAD_AGENT=1`：初译完成后串行调用校对 Agent，译文区只输出终稿。
7. 后端用 SSE 返回事件：
   - `thought`：Agent 推理、工具调用、RAG/Wiki 返回、校对阶段提示。
   - `token`：最终译文 token。
8. 前端默认只展示 `token`，用户可展开查看 `thought`。

### 2. YouTube 链接翻译（页面预览）

1. 用户输入 YouTube 链接。
2. 前端发起 `GET /api/translate-youtube?url=...`。
3. 后端并行抓取字幕与视频背景，推断 term domain 与作品 IP。
4. 后端优先使用 `youtube-transcript-api` 获取字幕；若被拦截则回退 `yt-dlp`。
5. `build_session_glossary` 预扫描全字幕术语；`wiki_franchise_scope` 注入 Wiki 查询扩展。
6. 字幕按 `YOUTUBE_TRANSLATE_CHUNK_CHARS` 分块进入 Agent 翻译（流式预览逐块输出）。
7. **不经过校对 Agent**（预览路径保持单 Agent，控制延迟与成本）。

### 3. YouTube 翻译字幕下载（SRT）

1. 前端发起 `GET /api/translate-youtube-srt?url=...`。
2. 后端抓取字幕与视频背景，完成术语预扫描与 domain/IP 推断（同预览路径）。
3. 按 `TRANSLATE_BATCH_SIZE`（默认 20 行）分批翻译，保留行与时间轴一一对应。
4. SSE 推送 `thought`（Agent 推理）与 `progress`（`done`/`total`/`message`）。
5. 全批完成后 `_repair_translations` 检测漏译并逐行补译。
6. 若 `ENABLE_PROOFREAD_AGENT=1`，每批初译后再走校对 Agent。
7. 结束时推送 `srt` 事件（`filename` + `content`），前端触发下载。
8. 导出时可选按句末标点拆条（`SRT_SPLIT_LONG_CUES` 等，见 `.env.example`）。

### 4. SRT/TXT 文件翻译

1. 用户上传 `.srt` 或 `.txt`。
2. BFF 代理到 Python 后端 `POST /api/translate-srt`。
3. 后端解析文件编码和字幕条目。
4. `build_session_glossary` 预扫描全文术语。
5. 按 `TRANSLATE_BATCH_SIZE` 分批调用 Agent 翻译。
6. `_repair_translations` 补译漏翻行。
7. 若 `ENABLE_PROOFREAD_AGENT=1`，每批走校对 Agent。
8. 返回翻译后的文本文件供浏览器下载。

### 5. YouTube 原视频下载（可选）

1. `.env` 设置 `ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1` 后，页面显示画质选择与「下载原视频」。
2. 前端 `GET /api/youtube-video-formats?url=...` 拉取可选画质。
3. 前端 `GET /api/stream-download-youtube-video?url=...&quality=...` 流式接收 `video_progress` / `video_ready` 事件。
4. 收到 `video_ready` 后 `GET /api/download-youtube-video-file?token=...` 下载文件。

## API 文档

浏览器经 BFF（`:3000`）访问；下表同时列出 Python 后端（`:8000`）直连路径。

### `GET /`

健康检查。

响应：

```json
{"message": "Hello from Python Backend"}
```

### `GET /stream_translate`

单句流式翻译接口。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 是 | 待翻译文本。 |

响应：`text/event-stream`

事件示例：

```text
data: {"type":"thought","content":"【LLM大脑】已接收任务..."}

data: {"type":"token","content":"这个角色的减益效果很强。"}

data: [DONE]
```

### `GET /stream_translate_youtube`

YouTube 字幕抓取 + 流式翻译接口（页面预览，单 Agent）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | YouTube 视频链接或视频 ID。 |

响应：`text/event-stream`，事件类型：`thought`、`token`、`[DONE]`。

环境要求：

- 国内环境通常需要配置 `YOUTUBE_PROXY`。
- 若出现 `Sign in to confirm`，需要配置 `YOUTUBE_COOKIES_FROM_BROWSER=edge/chrome/firefox` 或 `YOUTUBE_COOKIE_FILE`。

### `GET /stream_translate_youtube_srt`

YouTube 链接 -> 流式翻译并导出 SRT（推送进度 + Agent 思考过程，结束时下发文件内容）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | YouTube 视频链接或视频 ID。 |

响应：`text/event-stream`

事件类型：

| type | 说明 |
| --- | --- |
| `thought` | Agent 推理、术语预扫描、工具调用等 |
| `progress` | 翻译进度，`done`/`total`/`message` |
| `srt` | 翻译完成，`filename` + `content` |
| `[DONE]` | 流结束 |

`progress` 示例：

```text
data: {"type":"progress","done":40,"total":120,"message":"翻译进度 40/120 行（第 2/6 批，33%）"}
```

`srt` 示例：

```text
data: {"type":"srt","filename":"dQw4w9WgXcQ_zh.srt","content":"1\n00:00:00,000 --> ..."}
```

BFF 路径：`GET /api/translate-youtube-srt`

### `GET /download_translated_srt`

输入 YouTube 链接 -> 抓字幕 -> 批量翻译 -> 直接返回 `.srt` 文件（无 SSE 进度，供脚本/直连使用）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | YouTube 视频链接或视频 ID。 |

响应：`application/x-subrip`，带 `Content-Disposition` 下载头。

### `POST /api/translate-srt`

文件批量翻译接口。

请求：`multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | `.srt` 或 `.txt` 文件。 |

响应：`text/plain`，带 `Content-Disposition` 下载头。

### `GET /youtube_video_formats`

列出 YouTube 视频可选画质（需 `ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1`）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | YouTube 视频链接或视频 ID。 |

响应：`application/json`

BFF 路径：`GET /api/youtube-video-formats`

### `GET /stream_download_youtube_video`

流式下载 YouTube 原视频（含进度 SSE，需视频下载开关）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | YouTube 视频链接或视频 ID。 |
| `quality` | string | 否 | 画质：`best` / `1080` / `720` / `480` / `360` / `audio`，默认 `best`。 |

响应：`text/event-stream`，事件类型：`video_progress`、`video_ready`、`[DONE]`。

BFF 路径：`GET /api/stream-download-youtube-video`

### `GET /download_youtube_video`

直连下载 YouTube 原视频（无进度流，供脚本使用，需视频下载开关）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `url` | string | 是 | YouTube 视频链接或视频 ID。 |
| `quality` | string | 否 | 画质选择，默认 `best`。 |

响应：二进制文件，带 `Content-Disposition`。

BFF 路径：`GET /api/download-youtube-video`

### `GET /download_youtube_video_file`

按 token 拉取已下载到服务端的视频文件（配合 `stream_download_youtube_video` 的 `video_ready` 事件）。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `token` | string | 是 | `video_ready` 事件中的 token。 |

响应：二进制文件。

BFF 路径：`GET /api/download-youtube-video-file`

### `POST /admin/reload-dictionary`

热重载术语库（从 `data/terms/*.json` 重新注入 ChromaDB）。

响应：

```json
{"ok": true, "count": 153}
```

## Agent 设计

Agent 的 system prompt 明确要求：

1. 遇到疑难名词、领域黑话必须调用 `search_term_dict`。
2. 结合工具检索结果输出地道中文，而不是生硬机翻。

当前工具：

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `search_term_dict` | 待查询术语 | ChromaDB 返回的相关术语解释。 |
| `lookup_wiki` | 词条 + 可选数据源 | 萌娘百科/维基摘要；YouTube 翻译时自动按作品 IP 扩展查询。 |

### 术语预扫描（Push 式 glossary）

除 Agent 主动调用 RAG（Pull）外，批量与 YouTube 路径在翻译前由 `term_glossary.py` 扫描字幕全文与视频背景：

1. `collect_session_glossary_hits`：从字幕文本与标题/简介/标签中匹配词库词条。
2. `build_session_glossary`：将命中词条格式化为 glossary block，注入翻译 prompt。
3. 思考区通过 `format_glossary_thought` 展示预扫描结果。

Push 与 Pull 互补：预扫描覆盖高频已知术语，Agent 工具调用处理生僻词与 Wiki 查询。

### Wiki 检索增强

YouTube 翻译时，`wiki_franchise.py` 从视频标题/简介/标签/频道名推断作品 IP（如赛马娘、原神、彩虹社），经 `wiki_franchise_scope` 注入 `lookup_wiki` 工具层：

1. **查询扩展**：自动为 Agent 传入的 query 加 IP 前缀（如 `ウマ娘 PEAK`）、去噪泛词后缀、萌娘百科 `incategory:` 限定。
2. **多策略回退**：对每个变体按「精确标题 → OpenSearch → 全文搜索」依次尝试。
3. **动态源顺序**：识别到 ACG IP 时优先萌娘百科，避免日文维基对昵称类查询 miss。

相关环境变量见 `.env.example` 中 `ENABLE_WIKI_LOOKUP`、`WIKI_SEARCH_LIMIT`、`WIKI_EXTRACT_CHARS`、`WIKI_QUERY_EXPAND`、`WIKI_MULTI_STRATEGY`。

```mermaid
flowchart TD
    Main[main.py YouTube 翻译] --> Scope[wiki_franchise_scope]
    Scope --> Agent[翻译 Agent]
    Agent --> WikiTool[lookup_wiki]
    WikiTool --> Expand[查询变体 + 多策略检索]
    Expand --> Moegirl[萌娘百科]
    Expand --> ZhWiki[中文维基]
    Expand --> JaWiki[日文维基]
```

### 防死循环设计

`AgentExecutor` 配置了多重保护，确保 Agent 不会陷入无限“思考-调用工具”循环：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `max_iterations` | 5 | 限制单次请求内 ReAct 推理/工具调用的最大轮数。 |
| `max_execution_time` | 60 秒 | 单次请求最长执行时间，超时强制结束。 |
| `handle_parsing_errors` | True | 模型输出无法解析时不崩溃，返回提示让其自我修正。 |
| `early_stopping_method` | force | 触发上限时强制收尾，返回当前已有结果而非报错。 |

以上参数可通过 `.env` 中的 `AGENT_MAX_ITERATIONS`、`AGENT_MAX_EXECUTION_TIME` 覆盖。

术语库按 **domain 分文件** 存放在 `backend/app/data/terms/`（如 `gaming.json`、`cooking.json`、`general.json`、`vtuber.json`）。每条术语包含：

- `term`：日语/英文原词。
- `aliases`：别名、缩写、罗马字或常见写法。
- `category`：battle、gacha、ingredient、technique 等细分类（同一 domain 内使用）。
- `translation`：推荐中文译法。
- `meaning`：领域含义解释。
- `examples`：典型例句。
- `notes`：容易误译的点。

**分库加载**：YouTube 翻译时，`term_domains.py` 根据视频标题/简介/标签/频道名推断 domain，`search_term_dict` 只检索对应词库；若无命中则自动回退全库。单句/文件上传翻译默认全库检索。

### 漏译检测与补译

批量翻译后，`_repair_translations` 检测仍含日文或空白的行，结合邻句上下文逐行重试（`TRANSLATE_RETRY_MAX` 控制次数）。SRT 导出路径在全部批次完成后统一补译，思考区可见「补译」提示。

### 双 Agent：翻译 + 校对（可选）

当 `.env` 中 `ENABLE_PROOFREAD_AGENT=1` 时，在初译与规则补译之后串行调用第二个无工具 LLM「校对 Agent」（`backend/app/agent/proofread.py`）：

1. **翻译 Agent**（现有）：ReAct + `search_term_dict` / `lookup_wiki`，产出初稿。
2. **校对 Agent**：仅接收「原文 + 初译 + 术语表/视频背景」，专名统一、去元叙述、ASR 语义复核；不调用 Wiki。

覆盖路径：单句 `/stream_translate`、批量 `_translate_one_batch`（SRT 上传、YouTube SRT 下载）。**不覆盖** YouTube 预览分块 `/stream_translate_youtube`。

批量校对复用 `[序号]` 行号协议；解析失败则回退初译。默认 `ENABLE_PROOFREAD_AGENT=0` 以控制 API 成本。

## 工程边界

- 前端/BFF 层只负责交互和流式转发，不直接调用 LLM。
- Python 后端负责 AI 编排、字幕处理、Agent/RAG 逻辑。
- YouTube 字幕获取只抓字幕轨道，不做语音转写，避免把项目范围扩展成 ASR 管线。
- YouTube 原视频下载为可选功能（默认关闭），需自行确保符合当地法律与平台规则。
- 本地开发使用 `start.ps1` / `start.sh` 一键启动 Python 与 Node 进程。
