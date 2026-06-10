# AI 工程化与 Agent 架构报告

> 项目：垂直领域智能翻译 Agent（日语游戏 / ACG 视频字幕翻译）

## 1. 项目概述

### 1.1 背景与痛点

通用机器翻译在游戏 / ACG 垂直领域表现很差，主要难点在于：

- **游戏黑话**：如 `デバフ`、`バフ`、`ワンパン`，直译会丢失游戏机制含义。
- **缩写与外来语**：如 `CT`、`DPS`、`Tier1`，需要结合语境判断。
- **主播口语 / 网络梗**：如 `エグい`、`渋い`、`沼る`，字面意思和实际含义偏差很大。
- **同词多义**：如 `凸` 在不同游戏里可能是“命座 / 突破 / 星级”。

通用翻译模型缺乏这些领域知识，容易给出“看似通顺但意思错”的译文。

### 1.2 目标

构建一个前后端分离、具备自主规划能力的 AI Agent 翻译产品：

- 用 RAG 术语库为模型补充垂直领域知识。
- 用 Agent 的工具调用能力，让模型在“拿不准”时主动查术语库与 Wiki。
- 支持单句、YouTube 链接、字幕文件三种输入。
- 提供可视化的 Agent 思考过程。

## 2. 系统架构

### 2.1 总体架构

采用前后端分离 + BFF 模式：

```mermaid
flowchart LR
    U[用户浏览器] -->|文本 / YouTube 链接 / SRT 文件| FE[前端页面]
    FE -->|SSE / multipart| BFF[Node.js BFF<br/>Express]
    BFF -->|REST / SSE| API[Python FastAPI]
    API -->|字幕抓取| YT[youtube-transcript-api + yt-dlp]
    API -->|预扫描| GLOSS[term_glossary]
    API -->|Agent 编排| AGENT[LangChain Tool-calling Agent]
    AGENT -->|Pull| RAG[ChromaDB 术语库]
    AGENT -->|Pull| WIKI[lookup_wiki]
    AGENT -->|OpenAI 兼容接口| LLM[DeepSeek]
    API -.->|可选| PROOF[校对 Agent]
    PROOF --> LLM
```

### 2.2 分层职责

| 层 | 技术 | 职责 |
| --- | --- | --- |
| 前端 | HTML/CSS/JS | 交互 UI、SSE 消费、思考过程折叠展示；可选原视频下载。 |
| BFF | Node.js + Express | 流式转发、文件上传代理，作为前后端边界。 |
| 编排层 | Python + FastAPI | 字幕抓取、术语预扫描、Agent 调用、SSE 输出。 |
| 智能层 | LangChain + ChromaDB | ReAct 工具调用 + RAG 术语检索 + Wiki 查询。 |
| 模型层 | DeepSeek（OpenAI 兼容） | 实际语言理解与生成。 |

### 2.3 为什么这样分

- Node.js 擅长高并发 I/O 与流式转发，作为面向用户的 BFF。
- Python 生态对 LangChain / ChromaDB / 字幕处理支持最完善，作为 AI 编排层。
- 两层通过 REST + SSE 通信，边界清晰，可独立部署、独立扩展。

## 3. Agent 设计

### 3.1 ReAct 工具调用

使用 LangChain 的 `create_tool_calling_agent` + `AgentExecutor`，实现：

1. 接收输入文本。
2. 模型判断是否存在领域生词。
3. 若有，调用 `search_term_dict` 工具检索术语库（RAG），或 `lookup_wiki` 查询在线百科。
4. 结合检索结果生成地道译文。

System Prompt 明确要求“遇到疑难名词必须调用工具”，强约束 Agent 走检索流程而非直接机翻。

### 3.2 RAG 术语库

- 术语数据按 domain 分文件维护在 `backend/app/data/terms/`（`gaming` / `cooking` / `general` / `vtuber`），YouTube 翻译时自动选库检索。
- 每条包含：原词、别名、分类、推荐译法、含义、例句、易错提醒。
- 启动时载入 ChromaDB（内嵌进程内，默认 all-MiniLM-L6-v2 embedding）。
- 检索返回 Top-4 相关术语作为模型上下文。

### 3.3 术语预扫描（Push 式 glossary）

除 Agent 主动 Pull 检索外，批量与 YouTube 路径在翻译前由 `term_glossary.py` 扫描字幕全文与视频背景，将命中词条格式化为 glossary block 注入 prompt。思考区可见预扫描结果。Push 与 Pull 互补：预扫描覆盖高频已知术语，工具调用处理生僻词。

### 3.4 Wiki 检索增强

Agent 可调用 `lookup_wiki` 查询萌娘百科与中/日维基。YouTube 翻译时，`wiki_franchise.py` 从视频元数据推断作品 IP，自动扩展查询变体并多策略回退（精确标题 → OpenSearch → 全文搜索）。技术细节见 [architecture.md](architecture.md) 中 Wiki 专节。

### 3.5 可选双 Agent 校对

当 `ENABLE_PROOFREAD_AGENT=1` 时，初译与漏译补译之后串行调用无工具校对 Agent：专名统一、去元叙述、ASR 语义复核。覆盖单句翻译、SRT 上传与 YouTube SRT 下载；YouTube 页面预览仍为单 Agent（控制延迟）。默认关闭以控制 API 成本。

### 3.6 防死循环

`AgentExecutor` 配置多重保护：

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `max_iterations` | 5 | 限制 ReAct 最大轮数。 |
| `max_execution_time` | 60s | 单次请求超时强制结束。 |
| `handle_parsing_errors` | True | 解析失败不崩溃，自我修正。 |
| `early_stopping_method` | force | 触发上限时强制收尾返回结果。 |

### 3.7 流式输出

后端通过 `astream_events` 截获 Agent 事件，转成 SSE 消息：

- `thought`：推理、工具调用、RAG/Wiki 状态、校对阶段（前端默认折叠）。
- `token`：最终译文（前端默认展示）。
- `progress`：批量翻译进度（SRT 下载路径）。
- `srt`：翻译完成后的 SRT 文件内容。

兼顾“可解释性”与“产品体验（默认只看干净译文）”。

## 4. 关键功能实现

### 4.1 单句翻译

`GET /stream_translate?text=...`，流式返回术语预扫描、Agent 推理与译文；可选校对 Agent 二次润色。

### 4.2 YouTube 链接翻译

`GET /stream_translate_youtube?url=...`，难点在于字幕获取：

1. 优先用轻量 `youtube-transcript-api`。
2. 被 YouTube 拦截（`RequestBlocked`）时回退到 `yt-dlp`。
3. `yt-dlp` 通过浏览器 cookies 绕过 `Sign in to confirm` 机器人校验。
4. 过滤 `live_chat` 等非字幕轨道，下载真正字幕文件解析。
5. 完整字幕按字符数分块，逐块交给 Agent 翻译（非截断）。

只抓字幕、不做语音转写，避免范围扩张到 ASR 管线。

### 4.3 YouTube 翻译字幕下载

`GET /stream_translate_youtube_srt?url=...`：

1. 抓取字幕与视频背景，术语预扫描 + domain/IP 推断。
2. 按 `TRANSLATE_BATCH_SIZE` 分批翻译，SSE 推送 `progress` 进度。
3. 全批完成后 `_repair_translations` 检测漏译并补译。
4. 结束时推送 `srt` 事件，前端触发 `{videoId}_zh.srt` 下载。

### 4.4 字幕文件翻译

`POST /api/translate-srt`，支持 `.srt`（逐条）与 `.txt`（逐行）。按批翻译、漏译补译、可选校对后返回下载文件。

### 4.5 漏译检测与补译

批量翻译后检测仍含日文或空白的行，结合邻句上下文逐行重试，SRT 导出路径在思考区可见补译提示。

### 4.6 可选原视频下载

`ENABLE_YOUTUBE_VIDEO_DOWNLOAD=1` 后，页面可选画质流式下载 YouTube 原视频。默认关闭；使用者需自行确保符合当地法律与平台规则。

## 5. AI 结对编程实践

本项目在 Cursor 中与 AI 协作完成，AI 在以下环节发挥了实质作用：

- **代码审计**：识别出原计划中 `create_tool_calling_agent` 与 `langchain==0.1.0` 版本不兼容、缺失 `pysrt` / `python-multipart`、`\n` 转义 Bug 等隐患。
- **依赖治理**：将 LangChain 锁定到稳定的 0.3.x 线，去掉会安装失败的 `sentence-transformers`。
- **疑难排障**：逐步定位并解决 `OPENAI_API_KEY` 加载顺序、网络代理、YouTube `RequestBlocked` / `Sign in to confirm` / `live_chat` 等真实问题。
- **产品打磨**：把术语库从硬编码改为 JSON 数据驱动、思考过程折叠、字幕完整分块翻译、Wiki 增强与可选校对 Agent。

## 6. 工程规范

- `start.ps1` / `start.sh` / `start.bat` 一键启动前后端。
- `requirements.txt` / `package.json` 锁定依赖。
- `.gitignore` 排除 `.env`、`node_modules`、`chroma_data`。
- `.env.example` 提供配置模板，密钥不入库。
- `README.md` 运行说明 + `docs/architecture.md` 架构与 API 文档。

## 7. 课程评分对应

| 评分项 | 占比 | 实现 |
| --- | --- | --- |
| 复杂 Agent 架构 | 30% | ReAct 工具调用 + RAG + Wiki 检索 + 可选双 Agent 校对 + 防死循环 + 流式可视化 |
| AI 结对编程 | 20% | 全流程 Cursor 协作 |
| 极致分离 | 20% | Node BFF 与 Python 编排层分离，REST/SSE 通信 |
| 量化评判 | 15% | 单句术语验证、YouTube 字幕翻译、SRT/TXT 上传、可选校对对比（见 README 验证用例） |
| 工程规范 | 15% | 一键启动脚本、README、架构图、API 文档 |

## 8. 局限与后续优化

- 已实现可选双 Agent（`ENABLE_PROOFREAD_AGENT`）：翻译 Agent + 无工具校对 Agent 串行协作；默认关闭。YouTube 预览分块路径仍为单 Agent。
- ChromaDB 默认 embedding 对日语语义检索一般，可换多语言 embedding 提升召回。
- YouTube 长视频顺序批量翻译耗时较长，可引入并发或缓存。
- 术语库可继续扩充并按视频领域细分分类。

## 9. 结论

项目完整覆盖了现代 AI 软件工程的核心环节：前后端分离架构、Agent + RAG 智能编排、Wiki 增强检索、可选双 Agent 校对、流式交互、防死循环与工程化交付，较好地契合课程“全栈智能体产品开发”的目标与卓越准则。
